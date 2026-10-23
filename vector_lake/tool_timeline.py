import hashlib
import json
import re
from datetime import datetime, timezone
from contextlib import nullcontext
from vector_lake.db_store import get_connection, init_db, read_snapshot

# An event-ledger entry carries its date, and optionally its event tag, as a text prefix:
# ``[2026-05-02] [Observation] ...`` (README, section 2).  On the live corpus 7246 of 9974
# timeline claims carry the date only here -- 771 carry a temporal anchor and 2342 have
# neither -- so without this the projection reported the *ingestion* timestamp as the event
# date and ``ORDER BY event_date DESC`` was meaningless.  The extractor now records the
# anchor as well (``claim_extractor._parse_temporal``), but the prefix stays the source for
# every claim compiled before that and the two are read the same way.
_TEXT_EVENT_DATE = re.compile(r"^\s*\[(\d{4}-\d{2}-\d{2})\]")
# The tag in the same prefix.  Only 376 of the 7617 live tag-bearing claims also carry a
# structured ``event_tag``, so reading the field alone left 96% of ``action`` values at the
# literal fallback string ``timeline-event`` while the corpus already said what they were.
_TEXT_EVENT_TAG = re.compile(
    r"^\s*\[(?:\d{4}-\d{2}-\d{2}|\d{4}-[QH]\d|\d{4}-\d{2}|\d{4})\]\s*\[([^\]\n]{1,40})\]"
)

# ``event_date`` holds the event date and nothing else.  When a claim carries no date at all
# the column is NULL -- not the ingestion timestamp: spending ``updated_at`` there put a
# fifth of the corpus at the top of ``ORDER BY event_date DESC`` and made the newest 100
# rows 94% ingestion artifacts.  ``event_date_source`` records which shape of date it is, so
# a month- or quarter-precision anchor is never presented as a day.
DATE_SOURCE_DAY = "day"
DATE_SOURCE_COARSE = "coarse"
DATE_SOURCE_UNKNOWN = "unknown"
UNKNOWN_DATE_LABEL = "Unknown Date"
_DAY_PRECISION = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The fallback path matches an ``action`` tag inside the entry's prefix, which is the first
# two bracket groups.  60 characters is comfortably past the longest live one.
_ACTION_PREFIX_WINDOW = 60

# tool_ingest._apply_integration_disposition emits exactly this MD5-prefix marker.
# Other comments may contain source claims, and code examples are not compiler metadata.
_GENERATED_RELATION_MARKER = re.compile(r"<!-- vector-lake-relation:[0-9a-f]{16} -->")
_DISPLAY_MARKUP = re.compile(
    r"^ {0,3}(?P<fence>(?P<fencechar>`|~)(?P=fencechar){2,})[^\n]*\n"
    r".*?(?:^ {0,3}(?P=fence)(?P=fencechar)*[ \t]*\r?(?:\n|$)|\Z)|"
    r"(?<![\\`])(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)|"
    r"(?<!\\)<!--.*?(?:-->|\Z)",
    re.M | re.S,
)


def _display_event_description(text: str) -> str:
    """Hide generated relation comments without rewriting stored claims or literal examples."""
    return _DISPLAY_MARKUP.sub(
        lambda match: "" if _GENERATED_RELATION_MARKER.fullmatch(match.group()) else match.group(),
        text,
    )


def _stable_event_date(row, data: dict):
    """The event date from a source that an unrelated rewrite cannot move.

    A temporal anchor or a dated text prefix is part of the claim's content;
    ``updated_at`` is not, so it is never allowed here.
    """
    for candidate in (data.get("temporal_anchor"), data.get("event_date")):
        if candidate:
            return str(candidate)
    match = _TEXT_EVENT_DATE.match(str(row["claim_text"] or ""))
    return match.group(1) if match else None


def claim_event_date(row, data: dict):
    """The event date a claim carries, or ``None`` when it carries none at all.

    Only :func:`_stable_event_date` qualifies.  This used to fall through to
    ``updated_at`` for the 2342 of 9974 live timeline claims that carry no anchor and no
    dated text, which turned their *ingestion* time into their event date and is why the
    newest rows of ``ORDER BY event_date DESC`` were the last ingested ones.  That fallback
    is gone: the column now says NULL and the writer records ``event_date_source``.  An
    event's date is either known or unknown, and the two are no longer spelled the same way.

    Identity uses the same value (see ``_event_from_claim_row``): the id used to be
    ``sha256(claim_id, event_date, text)`` with ``event_date`` itself falling back to
    ``updated_at``, so every rewrite of a date-less claim minted a new id and orphaned the
    previous ``timeline_events`` row for good.  Displaying a fallback is one thing;
    identifying a row by it is another.
    """
    return _stable_event_date(row, data)


def event_date_source(stable_date) -> str:
    """Which shape of date the projection is storing, so precision survives the column."""
    if not stable_date:
        return DATE_SOURCE_UNKNOWN
    return DATE_SOURCE_DAY if _DAY_PRECISION.match(str(stable_date)) else DATE_SOURCE_COARSE


def _text_event_tag(row) -> str:
    """The event tag the ledger prefix carries, e.g. ``Observation``; ``""`` when absent."""
    match = _TEXT_EVENT_TAG.match(str(row["claim_text"] or ""))
    return match.group(1).strip() if match else ""


def _event_from_claim_row(row, entity_titles: dict[str, str] | None = None) -> dict:
    data = json.loads(row["data_json"])
    entities = data.get("subject_entity_ids") or []
    if isinstance(entities, str):
        entities = [entities]
    sources = data.get("source_ids") or []
    if isinstance(sources, str):
        sources = [sources]
    event_date = claim_event_date(row, data)
    description = row["claim_text"]
    entity_id = entities[0] if entities else ""
    # Identity uses only inputs a rewrite cannot move: the claim id, the event date a source
    # actually states, and the text.  The text stays an input on purpose (a rewritten entry
    # is a different fact); ``updated_at`` never is.
    stable_raw = "\0".join([str(row["claim_id"]), str(event_date or ""), str(description)])
    return {
        "id": hashlib.sha256(stable_raw.encode("utf-8")).hexdigest()[:24],
        "claim_id": str(row["claim_id"]),
        "event_date": str(event_date) if event_date else None,
        "event_date_source": event_date_source(event_date),
        "action": str(
            data.get("action")
            or data.get("event_tag")
            or _text_event_tag(row)
            or data.get("claim_type")
            or "timeline-event"
        ),
        "sentiment": str(data.get("sentiment") or "neutral"),
        "description": description,
        "entity_id": entity_id,
        "entity_title": ", ".join(
            str((entity_titles or {}).get(str(item)) or item)
            for item in entities
        ),
        "source_file": ", ".join(str(item) for item in sources),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def _entity_title_map(entity_ids: set[str]) -> dict[str, str]:
    if not entity_ids:
        return {}
    conn = get_connection()
    placeholders = ",".join("?" for _ in entity_ids)
    rows = conn.execute(
        f"SELECT entity_id, canonical_name FROM entities WHERE entity_id IN ({placeholders})",
        tuple(sorted(entity_ids)),
    ).fetchall()
    return {str(row["entity_id"]): str(row["canonical_name"] or row["entity_id"]) for row in rows}


def _claim_subject_ids(data: dict) -> list[str]:
    subjects = data.get("subject_entity_ids") or []
    if isinstance(subjects, str):
        subjects = [subjects]
    return [str(item) for item in subjects]


# ``timeline_projection_parity`` is a full recomputation of the canonical event
# id set, so it is memoised against a cheap fingerprint of the rows it depends on:
#   * the database identity, so two isolated corpora can never share an entry;
#   * the count / newest ``updated_at`` / highest rowid of the timeline claims;
#   * the count and id extremes of the projection, which catch an out-of-band
#     write to ``timeline_events`` that bypasses the claim delta.
# Anything that can change the id sets moves one of those numbers, including
# writes made by another process, so the cache cannot silently go stale.
_PARITY_CACHE: dict = {"fingerprint": None, "result": None}


def _timeline_claims_fingerprint(conn=None) -> str:
    """A cheap proof that neither input to the id set can have moved.

    Every aggregate here is servable from an index -- ``idx_claims_f_claim_type`` and the
    ``timeline_events`` primary-key index -- so the gate is a walk over index entries and
    never over table rows.

    ``MAX(updated_at)`` used to be part of this and cost ~55 ms on every timeline query on
    the live corpus.  Since event identity stopped depending on ``updated_at`` (see
    :func:`claim_event_date`), a moved ``updated_at`` can no longer change the id set, so
    that term could only ever force a full recomputation that reported ``missing=0
    extra=0`` -- the most expensive way to learn nothing.  A content rewrite still moves the
    id set and is still caught: it is an ``INSERT OR REPLACE``, which raises ``MAX(rowid)``
    unless the row replaced was the highest one (then the projection's id extremes move).

    The five aggregates are subselects of one statement rather than two ``execute`` calls:
    the gate runs on every timeline query, and a round trip that produces one row costs less
    than two that produce two.
    """
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM claims WHERE f_claim_type = 'timeline-event'), "
        "(SELECT COALESCE(MAX(rowid), 0) FROM claims WHERE f_claim_type = 'timeline-event'), "
        "(SELECT COUNT(*) FROM timeline_events), "
        "(SELECT COALESCE(MIN(id), '') FROM timeline_events), "
        "(SELECT COALESCE(MAX(id), '') FROM timeline_events)"
    ).fetchone()
    count, highest, projected, lowest_id, highest_id = row
    from vector_lake.db_store import get_db_path

    return f"{get_db_path()}|{count}:{highest}|{projected}:{lowest_id}:{highest_id}"


#: JSON kinds that make ``str()`` of a full ``json.loads`` differ from the extracted value
#: (``true`` becomes ``'True'`` in Python and ``1`` through ``json_extract``), or that are not a
#: date at all.  Rows carrying them are decoded the long way in :func:`_expected_event_ids`.
_LEAN_HOSTILE_KINDS = frozenset({"true", "object", "array"})


def _expected_event_ids(conn) -> set[str]:
    """The canonical event ids, without decoding every timeline claim's payload.

    Event identity is ``sha256(claim_id, event_date, text)`` and the date comes from one of two
    JSON fields or the entry's ``[date]`` prefix.  The parity proof used to run
    :func:`_event_from_claim_row` over every timeline claim, which ``json.loads`` the whole
    payload to read two strings -- measured 292 ms for 7 816 claims on the live corpus, paid by
    whichever request first noticed the fingerprint move after an ingest.

    ``json_extract`` reads the same two paths in SQL, so the decode is skipped; only a row whose
    value is a boolean, object or array is decoded in full, because there ``str()`` of the Python
    value and of the extracted one differ.
    """
    expected: set[str] = set()
    # One ``json_extract`` call with both paths: SQLite parses the document per call, and the two
    # dates used to be two parses of a payload that is often a few kilobytes.  ``json_type`` is then
    # asked once per *suspicious* row rather than twice per row: the only values whose ``str()``
    # differs between ``json.loads`` and ``json_extract`` are a JSON boolean (``'True'`` vs ``'1'``)
    # and a container (Python repr vs JSON text).  A container arrives here as a string starting
    # with ``{``/``[`` -- no date does -- and a boolean is the one case an integer value cannot be
    # told apart from, so that row gets its type probed and, if it is one, is decoded in full.
    for row in conn.execute(
        "SELECT claim_id, claim_text, "
        "json_extract(data_json, '$.temporal_anchor', '$.event_date') AS dates "
        "FROM claims WHERE f_claim_type = 'timeline-event'"
    ):
        dates = json.loads(row["dates"] or "[]")
        if len(dates) != 2:
            expected.add(_event_from_claim_row(_full_claim_row(conn, row["claim_id"]))["id"])
            continue
        event_date = dates[0] or dates[1]
        if isinstance(event_date, str) and event_date[:1] in {"{", "["}:
            expected.add(_event_from_claim_row(_full_claim_row(conn, row["claim_id"]))["id"])
            continue
        if isinstance(event_date, int) and _claim_date_is_boolean(conn, row["claim_id"], dates):
            expected.add(_event_from_claim_row(_full_claim_row(conn, row["claim_id"]))["id"])
            continue
        if event_date:
            stable = str(event_date)
        else:
            match = _TEXT_EVENT_DATE.match(str(row["claim_text"] or ""))
            stable = match.group(1) if match else ""
        stable_raw = "\0".join([str(row["claim_id"]), stable, str(row["claim_text"])])
        expected.add(hashlib.sha256(stable_raw.encode("utf-8")).hexdigest()[:24])
    return expected


def _claim_date_is_boolean(conn, claim_id, dates) -> bool:
    """Whether a numeric date value is really a JSON boolean for this claim.

    Asked only for rows whose selected date extracted as an integer -- the one shape ``str()`` cannot
    settle on its own.  Probes the path that actually supplied the value.
    """
    path = '$.temporal_anchor' if dates[0] else '$.event_date'
    kind = conn.execute(
        "SELECT json_type(data_json, ?) FROM claims WHERE claim_id = ?", (path, claim_id)
    ).fetchone()[0]
    return kind in {"true", "false"}


def _full_claim_row(conn, claim_id):
    """The row shape :func:`_event_from_claim_row` reads, for the rare hostile-kind claim."""
    return conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()


def _timeline_parity_disk_path():
    from vector_lake.wiki_utils import get_runtime_tmp_dir
    return get_runtime_tmp_dir() / "timeline_parity_state.json"


def invalidate_timeline_parity_cache() -> None:
    """Drop the memoised parity proof after this process rewrites the projection."""
    _PARITY_CACHE["fingerprint"] = None
    _PARITY_CACHE["result"] = None
    try:
        _timeline_parity_disk_path().unlink(missing_ok=True)
    except OSError:
        pass


def sync_timeline_events_for_claim_delta(old_claim_rows: list, proposed_claims: list[dict]) -> dict:
    """Apply a claim-scoped Timeline projection delta inside the caller's transaction.

    Rows are deleted by ``claim_id``, not by the event id.  The event id hashes the claim's
    *content* (its id, its event date, its text), so it only names the row a rewrite is about
    to remove as long as the stored claim still says exactly what it said when the row was
    written -- and that is what failed in September: rows were minted in parity, the claims
    were then retired by a bulk pass that recomputed their dates, and the delete kept
    recomputing a hash that no longer matched any row.  90 rows survived their claims that
    way.  ``claim_id`` is the input a rewrite cannot move, so it is what the delete keys on
    now; the recomputed-id delete stays for rows written before the column existed (they
    carry NULL there and their hash is the only evidence left of what they were).
    """
    conn = get_connection()
    old_events = [
        _event_from_claim_row(row)
        for row in old_claim_rows
        if json.loads(row["data_json"]).get("claim_type") == "timeline-event"
    ]
    old_event_ids = [event["id"] for event in old_events]
    old_claim_ids = sorted({event["claim_id"] for event in old_events})
    if old_claim_ids:
        conn.executemany(
            "DELETE FROM timeline_events WHERE claim_id = ?",
            [(claim_id,) for claim_id in old_claim_ids],
        )
    if old_event_ids:
        conn.executemany("DELETE FROM timeline_events WHERE id = ?", [(event_id,) for event_id in old_event_ids])

    proposed_rows = []
    entity_ids: set[str] = set()
    for claim in proposed_claims:
        if claim.get("claim_type") != "timeline-event":
            continue
        subjects = claim.get("subject_entity_ids") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        entity_ids.update(str(item) for item in subjects)
        proposed_rows.append({
            "claim_id": claim["claim_id"],
            "claim_text": claim.get("claim_text", ""),
            "data_json": json.dumps(claim, ensure_ascii=False),
            "updated_at": claim.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        })
    entity_titles = _entity_title_map(entity_ids)
    new_events = [_event_from_claim_row(row, entity_titles=entity_titles) for row in proposed_rows]
    if new_events:
        conn.executemany(
            "INSERT OR REPLACE INTO timeline_events "
            "(id, claim_id, event_date, event_date_source, action, sentiment, description, entity_id, "
            "entity_title, source_file, extracted_at) "
            "VALUES (:id, :claim_id, :event_date, :event_date_source, :action, :sentiment, :description, "
            ":entity_id, :entity_title, :source_file, :extracted_at)",
            new_events,
        )
    invalidate_timeline_parity_cache()
    return {"deleted": len(old_event_ids), "upserted": len(new_events)}


def timeline_projection_parity() -> dict:
    """Compare the exact stable event IDs in canonical claims and the SQL projection.

    Memoised against :func:`_timeline_claims_fingerprint`; the recomputation is a
    SHA-256 over every timeline claim and used to run on every timeline query.
    """
    conn = get_connection()
    fingerprint = _timeline_claims_fingerprint(conn)
    if _PARITY_CACHE["fingerprint"] == fingerprint and _PARITY_CACHE["result"] is not None:
        return dict(_PARITY_CACHE["result"])

    disk_path = _timeline_parity_disk_path()
    if disk_path.exists():
        try:
            with open(disk_path, "r", encoding="utf-8") as handle:
                disk_record = json.load(handle)
            if disk_record.get("fingerprint") == fingerprint and "result" in disk_record:
                _PARITY_CACHE["fingerprint"] = fingerprint
                _PARITY_CACHE["result"] = dict(disk_record["result"])
                return dict(_PARITY_CACHE["result"])
        except Exception:
            pass

    expected_ids = _expected_event_ids(conn)
    actual_ids = {str(row[0]) for row in conn.execute("SELECT id FROM timeline_events")}
    result = {
        "canonical": len(expected_ids),
        "projection": len(actual_ids),
        "missing": len(expected_ids - actual_ids),
        "extra": len(actual_ids - expected_ids),
    }
    _PARITY_CACHE["fingerprint"] = fingerprint
    _PARITY_CACHE["result"] = dict(result)
    try:
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        with open(disk_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"fingerprint": fingerprint, "result": result}, handle)
    except Exception:
        pass
    return result


def rebuild_timeline_events_from_claims(dry_run: bool = True, limit: int | None = None) -> str:
    """Rebuild the timeline_events projection from timeline-event claims."""
    init_db()
    conn = get_connection()
    query = (
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims "
        "WHERE f_claim_type = 'timeline-event' "
        "ORDER BY updated_at DESC"
    )
    params: list = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
    rows = conn.execute(query, params).fetchall()
    entity_ids = set()
    for row in rows:
        entity_ids.update(_claim_subject_ids(json.loads(row["data_json"])))
    entity_titles = _entity_title_map(entity_ids)
    events = [_event_from_claim_row(row, entity_titles=entity_titles) for row in rows]
    if dry_run:
        return f"[DRY RUN] Would rebuild {len(events)} timeline_events row(s) from timeline-event claims."

    from vector_lake.db_store import transaction

    with transaction():
        conn.execute("DELETE FROM timeline_events")
        conn.executemany(
            "INSERT OR REPLACE INTO timeline_events "
            "(id, claim_id, event_date, event_date_source, action, sentiment, description, entity_id, "
            "entity_title, source_file, extracted_at) "
            "VALUES (:id, :claim_id, :event_date, :event_date_source, :action, :sentiment, :description, "
            ":entity_id, :entity_title, :source_file, :extracted_at)",
            events,
        )
    invalidate_timeline_parity_cache()
    return f"Rebuilt {len(events)} timeline_events row(s) from timeline-event claims."


def repair_timeline_projection(dry_run: bool = True) -> str:
    """Close the parity gap in place: drop orphan ids, project the missing ones.

    The event id is content-addressed -- ``sha256(claim_id, event_date, text)`` -- on
    purpose, so a row rewritten out of band with the same row count is detectable
    (``test_timeline_search_rejects_equal_count_wrong_event_ids``).  Content addressing is
    not the same as churn: the id is derived from inputs a *source* states (the claim id,
    the date a source gives, the text), never from ``updated_at``, so an unrelated rewrite
    no longer moves it (see :func:`claim_event_date`).  A row whose id moved therefore
    means the claim's content or date actually changed, or that something wrote outside the
    delta path -- ``sync_timeline_events_for_claim_delta`` is the only writer, and both
    places that write ``claims`` call it inside the same transaction, so what is left to
    catch is an out-of-band SQL write.

    A row the current writer would no longer produce is drift too, even when its id is
    correct: a projection row predating ``event_date_source`` carries NULL there, its
    ``event_date`` may still be showing an ingestion timestamp, and its ``action`` may still
    be the literal claim-type fallback.  Those three columns are updated in place -- the row
    is not re-inserted, so ``extracted_at`` survives -- and the projection converges without
    replacing ``timeline_events``.
    """
    # The column this reads is added by ``init_db``'s migration, and this entry point used to
    # depend on no schema at all: without this call a CLI that only runs ``timeline-repair``
    # fails with "no such column: event_date_source" instead of migrating.
    init_db()
    conn = get_connection()
    # A no-op repair used to materialise every event anyway -- decode 7 816 payloads, resolve entity
    # titles, hash every id -- measured 3.2 s to report "nothing to do".  Two structural probes settle
    # that case without decoding: the parity gap (ids) and the row shape the current writer produces
    # (``event_date_source`` must be present and consistent with ``event_date``).  Anything the probes
    # cannot settle still takes the full path below, which is what makes this a fast path and not a
    # new rule -- ``test_timeline_repair_rewrites_rows_written_before_event_date_source`` still passes.
    parity = timeline_projection_parity()
    stale_shape = conn.execute(
        "SELECT COUNT(*) FROM timeline_events WHERE event_date_source IS NULL "
        "OR event_date_source NOT IN ('day', 'coarse', 'unknown') "
        "OR (event_date_source = 'unknown' AND event_date IS NOT NULL) "
        "OR (event_date_source <> 'unknown' AND event_date IS NULL) "
        "OR claim_id IS NULL OR claim_id = ''"
    ).fetchone()[0]
    if not parity["missing"] and not parity["extra"] and not stale_shape:
        return (f"Repaired timeline_events: deleted 0 orphan row(s), inserted 0 missing row(s); "
                f"{parity['projection']} row(s) already in parity with the current writer.")
    claim_rows = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims "
        "WHERE f_claim_type = 'timeline-event'"
    ).fetchall()
    entity_ids: set[str] = set()
    for row in claim_rows:
        entity_ids.update(_claim_subject_ids(json.loads(row["data_json"])))
    entity_titles = _entity_title_map(entity_ids)
    expected = {}
    for row in claim_rows:
        event = _event_from_claim_row(row, entity_titles=entity_titles)
        expected[event["id"]] = event
    actual = {
        str(row["id"]): {"claim_id": row["claim_id"], "event_date_source": row["event_date_source"]}
        for row in conn.execute("SELECT id, claim_id, event_date_source FROM timeline_events")
    }
    canonical_claim_ids = {str(row["claim_id"]) for row in claim_rows}
    # Attribution first, hash second.  A row that names a claim canonical no longer holds is
    # an orphan whatever its hash says -- that is the September case, where 90 rows were minted
    # from claims a later bulk pass retired, and the recomputed hash they were deleted by no
    # longer named them.  A row that predates ``claim_id`` names nothing, so its hash is the
    # only evidence left and stays the test for those.  A row naming a claim that *is*
    # canonical but hashing to a different id is the remnant of an identity that moved (the
    # claim's date or text changed out of band): it is deleted here and re-inserted below.
    orphan_ids = sorted(
        event_id
        for event_id, row in actual.items()
        if (
            str(row["claim_id"]) not in canonical_claim_ids
            if row["claim_id"] is not None
            else event_id not in expected
        )
        or (row["claim_id"] is not None and str(row["claim_id"]) in canonical_claim_ids and event_id not in expected)
    )
    missing_ids = sorted(set(expected) - set(actual))
    # "Older than the current writer's rules" is exactly "cannot say which claim it came from":
    # both the 2026-09 date columns and the claim id are written together, so a NULL in either
    # means the row needs converging.  The three legacy columns plus ``claim_id`` are rewritten
    # in place.
    stale_ids = sorted(
        event_id
        for event_id, row in actual.items()
        if event_id in expected and (row["claim_id"] is None or row["event_date_source"] is None)
    )
    if dry_run:
        return (
            "[DRY RUN] Would delete "
            f"{len(orphan_ids)} orphan row(s), insert {len(missing_ids)} missing row(s) and "
            f"rewrite {len(stale_ids)} row(s) written before the claim_id/event_date_source columns."
        )

    from vector_lake.db_store import transaction

    with transaction():
        if orphan_ids:
            conn.executemany(
                "DELETE FROM timeline_events WHERE id = ?", [(i,) for i in orphan_ids]
            )
        to_insert = missing_ids
        if to_insert:
            conn.executemany(
                "INSERT OR REPLACE INTO timeline_events "
                "(id, claim_id, event_date, event_date_source, action, sentiment, description, entity_id, "
                "entity_title, source_file, extracted_at) "
                "VALUES (:id, :claim_id, :event_date, :event_date_source, :action, :sentiment, :description, "
                ":entity_id, :entity_title, :source_file, :extracted_at)",
                [expected[i] for i in to_insert],
            )
        if stale_ids:
            # Deliberately not a re-insert: the row is right except for the columns the
            # writer's rules changed, and rewriting the whole row would discard
            # ``extracted_at`` -- the only field recording when this row was first projected.
            conn.executemany(
                "UPDATE timeline_events SET claim_id = :claim_id, event_date = :event_date, "
                "event_date_source = :event_date_source, action = :action WHERE id = :id",
                [
                    {
                        "id": event_id,
                        "claim_id": expected[event_id]["claim_id"],
                        "event_date": expected[event_id]["event_date"],
                        "event_date_source": expected[event_id]["event_date_source"],
                        "action": expected[event_id]["action"],
                    }
                    for event_id in stale_ids
                ],
            )
    invalidate_timeline_parity_cache()
    return (
        f"Repaired timeline_events: deleted {len(orphan_ids)} orphan row(s), "
        f"inserted {len(missing_ids)} missing row(s), rewrote {len(stale_ids)} legacy row(s)."
    )

def _sql_stable_date_expr() -> str:
    """The ledger date of a canonical claim, expressed in SQL for the fallback ordering.

    Mirrors :func:`_stable_event_date`: ``temporal_anchor``, else ``event_date``, else a
    ``[YYYY-MM-DD]`` text prefix.  ``GLOB`` does the digit test because ``LIKE`` has no
    character ranges.  ``ltrim`` strips spaces where the Python pattern also accepts a tab,
    which is not a shape the ledger format uses.
    """
    head = "ltrim(claim_text)"
    return (
        "COALESCE("
        "NULLIF(json_extract(data_json, '$.temporal_anchor'), ''), "
        "NULLIF(json_extract(data_json, '$.event_date'), ''), "
        f"CASE WHEN substr({head}, 1, 1) = '[' "
        f"AND substr({head}, 2, 10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        f"THEN substr({head}, 2, 10) END)"
    )


def search_timeline_events(entity_name: str | None = None, action: str | None = None, limit: int = 10) -> str:
    """Search mentions in the timeline projection, falling back to claims if it drifts.

    entity_name matches subject IDs/titles or description mentions, not a strict subject
    chronology. Canonical fallback uses the same subject/title/description mention filter.
    Source claims and dates are reported, not independently verified by this tool.

    ``sentiment`` is deliberately not a parameter.  The projection still stores the column
    written from ``claim.sentiment``, but nothing in the pipeline produces that field --
    9974 of 9974 live events are ``neutral`` -- so a filter over it could only ever match
    everything or nothing while reading as if it discriminated.  The column stays for a
    future producer; the parameter comes back with it.
    """
    conn = get_connection()
    with nullcontext(conn) if conn.in_transaction else read_snapshot():
        return _search_timeline_events(entity_name, action, limit)


def _search_timeline_events(entity_name, action, limit):
    conn = get_connection()
    cursor = conn.cursor()

    parity = timeline_projection_parity()
    if parity["projection"] and not parity["missing"] and not parity["extra"]:
        query = (
            "SELECT event_date, action, description, entity_id, entity_title, source_file "
            "FROM timeline_events WHERE 1=1"
        )
        params = []
        if entity_name:
            query += " AND (entity_id LIKE ? OR entity_title LIKE ? OR description LIKE ?)"
            params.extend([f"%{entity_name}%", f"%{entity_name}%", f"%{entity_name}%"])
        if action:
            action_clean = action.strip()
            if any(ch in action_clean for ch in "%*?"):
                query += " AND action LIKE ?"
                params.append(f"%{action_clean.replace('*', '%')}%")
            else:
                # Use exact match with COLLATE NOCASE so SQLite uses idx_timeline_action_date_id
                query += " AND action = ? COLLATE NOCASE"
                params.append(action_clean)
        # NULLs sort last for DESC without a computed leading key, so the date index
        # remains usable.  Stable event IDs break ties before LIMIT, including unknown dates.
        query += " ORDER BY event_date DESC, id ASC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        except Exception as e:
            return f"Error executing timeline query: {e}"
        if not rows:
            return "No timeline events found matching the criteria."
        return "\n\n".join(
            f"[{r['event_date'] or UNKNOWN_DATE_LABEL}] <{r['entity_title'] or r['entity_id']}>\n"
            f"  -> {_display_event_description(str(r['description']))}\n"
            f"  Action: {r['action']} | Source: {r['source_file']}"
            for r in rows
        )

    # The projection is stale: answer from canonical claims and say so, so a
    # degraded result is never mistaken for an authoritative one.
    drift_note = ""
    if parity["missing"] or parity["extra"]:
        drift_note = (
            f"[DEGRADED] timeline_events projection is out of parity with canonical claims "
            f"(missing={parity['missing']}, extra={parity['extra']}); answering from canonical claims. "
            f"Run rebuild_timeline_events(dry_run=False) to restore the indexed path.\n\n"
        )

    query = "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE f_claim_type = 'timeline-event'"
    params = []

    def subject_field(raw, title):
        subjects = _claim_subject_ids(json.loads(raw))
        if not title:
            return subjects[0] if subjects else ''
        titles = _entity_title_map(set(subjects))
        return ', '.join(str(titles.get(str(item)) or item) for item in subjects)

    if entity_name:
        # T12 drift regression: use canonical identities in this read snapshot, not
        # the incomplete projection. LIKE remains SQLite's own wildcard/case rule.
        conn.create_function('timeline_subject_id', 1, lambda raw: subject_field(raw, False))
        conn.create_function('timeline_subject_title', 1, lambda raw: subject_field(raw, True))
        query += " AND (timeline_subject_id(data_json) LIKE ? OR timeline_subject_title(data_json) LIKE ? OR claim_text LIKE ?)"
        params.extend([f"%{entity_name}%"] * 3)
    if action:
        conn.create_function('timeline_action', 3, lambda claim_id, claim_text, data_json:
                             _event_from_claim_row({'claim_id': claim_id, 'claim_text': claim_text,
                                                    'data_json': data_json})['action'], deterministic=True)
        action_clean = action.strip()
        if any(ch in action_clean for ch in '%*?'):
            query += ' AND timeline_action(claim_id, claim_text, data_json) LIKE ?'
            params.append(f"%{action_clean.replace('*', '%')}%")
        else:
            query += ' AND timeline_action(claim_id, claim_text, data_json) = ? COLLATE NOCASE'
            params.append(action_clean)

    # Same ordering rule as the indexed path, expressed over canonical claims: a degraded
    # answer is the same answer, slower.  This used to be ``ORDER BY updated_at DESC``, so
    # with the projection one row out of parity the same query returned the *oldest* events
    # instead of the newest.  Keep the canonical date expression unchanged; the
    # additional tie key must agree with the projection before selecting the top K.
    # Reuse the writer's identity formula, not claim_id or SQL's date coercion: JSON
    # booleans and other date shapes must retain exactly the IDs used by the projection.
    conn.create_function(
        "timeline_event_id", 3,
        lambda claim_id, claim_text, data_json: _event_from_claim_row({
            "claim_id": claim_id, "claim_text": claim_text, "data_json": data_json,
        })["id"],
        deterministic=True,
    )
    stable_date = _sql_stable_date_expr()
    query += (
        f" ORDER BY ({stable_date} IS NULL), {stable_date} DESC, "
        "timeline_event_id(claim_id, claim_text, data_json) ASC LIMIT ?"
    )
    params.append(max(1, int(limit)))
    
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    except Exception as e:
        return f"Error executing timeline query: {e}"
        
    if not rows:
        return drift_note + "No timeline events found matching the criteria."

    subject_ids: set[str] = set()
    decoded_events = []
    for r in rows:
        data = json.loads(r["data_json"])
        subjects = _claim_subject_ids(data)
        subject_ids.update(subjects)
        decoded_events.append(r)
    entity_titles = _entity_title_map(subject_ids)

    results = []
    for r in decoded_events:
        event = _event_from_claim_row(r, entity_titles)
        results.append(
            f"[{event['event_date'] or UNKNOWN_DATE_LABEL}] <{event['entity_title'] or event['entity_id']}>\n"
            f"  -> {_display_event_description(str(event['description']))}\n"
            f"  Action: {event['action']} | Source: {event['source_file']}"
        )

    return drift_note + "\n\n".join(results)
