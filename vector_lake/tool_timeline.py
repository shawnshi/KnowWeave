import hashlib
import calendar
import pickle
from functools import lru_cache
import json
import re
from datetime import date, datetime, timezone
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
_TEXT_COARSE_DATE = re.compile(r"^\s*\[(\d{4}(?:-(?:0[1-9]|1[0-2]|Q[1-4]|H[12]))?)\]")
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
    """Read stated display precision; coarse legacy prefixes do not change event IDs."""
    stable = _stable_event_date(row, data)
    if stable is not None:
        return stable
    match = _TEXT_COARSE_DATE.match(str(row["claim_text"] or ""))
    return match.group(1) if match else None


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
    # KW-04: the pre-existing identity contract used anchors/day prefixes only.
    # Recognizing a legacy quarter/year for display must not mint replacement IDs.
    identity_date = _stable_event_date(row, data)
    stable_raw = "\0".join([str(row["claim_id"]), str(identity_date or ""), str(description)])
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
    conn = get_connection()
    result = {}
    ordered = sorted(entity_ids)
    for start in range(0, len(ordered), 500):
        batch = ordered[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT entity_id, canonical_name FROM entities WHERE entity_id IN ({placeholders})", tuple(batch)
        ):
            result[str(row["entity_id"])] = str(row["canonical_name"] or row["entity_id"])
    return result


def _claim_subject_ids(data: dict) -> list[str]:
    subjects = data.get("subject_entity_ids") or []
    if isinstance(subjects, str):
        subjects = [subjects]
    return [str(item) for item in subjects]


# One complete health proof, bounded to one connection generation/content digest.
_PARITY_CACHE: dict = {"fingerprint": None, "result": None}
_PAYLOAD_FIELDS = (
    "claim_id", "event_date", "event_date_source", "action", "sentiment",
    "description", "entity_id", "entity_title", "source_file",
)


def _timeline_claims_fingerprint(conn=None) -> str:
    """Hash every input to identity and displayed payload, excluding ingestion timestamps."""
    conn = conn or get_connection()
    digest = hashlib.sha256(b"timeline-health-v3\0")
    statements = (
        "SELECT claim_id, claim_text, json_extract(data_json, '$.temporal_anchor', '$.event_date', "
        "'$.action', '$.event_tag', '$.claim_type', '$.sentiment', '$.subject_entity_ids', '$.source_ids') "
        "FROM claims WHERE f_claim_type = 'timeline-event' ORDER BY claim_id",
        "SELECT id, claim_id, event_date, event_date_source, action, sentiment, description, "
        "entity_id, entity_title, source_file FROM timeline_events ORDER BY id",
        "SELECT entity_id, canonical_name FROM entities ORDER BY entity_id",
    )
    for statement in statements:
        digest.update(statement.encode("utf-8"))
        for row in conn.execute(statement):
            # Serialization-only C encoding avoids the measured JSON/UTF-8 copy cost;
            # SQLite supplies primitives and no pickle is ever loaded or persisted.
            encoded = pickle.dumps(tuple(row), protocol=4)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    from vector_lake.db_store import get_db_path
    return f"{get_db_path()}|{digest.hexdigest()}"


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
    """Drop the verified snapshot/content proof after rewriting the projection."""
    global _PARITY_CACHE
    _PARITY_CACHE = {"fingerprint": None, "result": None}
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
    """Return the protected ID-set counts; payload health is tracked separately."""
    return dict(_timeline_projection_health()["parity"])


def _canonical_timeline_events(conn) -> dict:
    rows = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE f_claim_type = 'timeline-event'"
    ).fetchall()
    subjects = set()
    for row in rows:
        subjects.update(_claim_subject_ids(json.loads(row["data_json"])))
    titles = _entity_title_map(subjects)
    events = (_event_from_claim_row(row, titles) for row in rows)
    return {event["id"]: event for event in events}


def _timeline_projection_health() -> dict:
    """Verify identities and payloads in one read generation, never cache an uncommitted write."""
    global _PARITY_CACHE
    conn = get_connection()
    if not conn.in_transaction:
        with read_snapshot():
            return _timeline_projection_health()
    readonly = bool(conn.execute("PRAGMA query_only").fetchone()[0])
    generation = (conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes)
    cached = _PARITY_CACHE
    if readonly and cached.get("connection") is conn and cached.get("generation") == generation:
        return cached["health"]
    fingerprint = _timeline_claims_fingerprint(conn)
    if readonly and cached.get("fingerprint") == fingerprint and cached.get("health") is not None:
        health = cached["health"]
    else:
        actual_ids = {str(row[0]) for row in conn.execute("SELECT id FROM timeline_events")}
        expected_ids = set()
        mismatches = coarse = 0
        columns = ", ".join(_PAYLOAD_FIELDS)

        @lru_cache(maxsize=4096)
        def title(key):
            row = conn.execute("SELECT canonical_name FROM entities WHERE entity_id = ?", (key,)).fetchone()
            return str(row[0] or key) if row else key

        rows = conn.execute("SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE f_claim_type = 'timeline-event'")
        for row in rows:
            subjects = _claim_subject_ids(json.loads(row["data_json"]))
            event = _event_from_claim_row(row, {key: title(key) for key in subjects})
            expected_ids.add(event["id"])
            actual = conn.execute(f"SELECT {columns} FROM timeline_events WHERE id = ?", (event["id"],)).fetchone()
            mismatches += bool(actual and any(actual[field] != event[field] for field in _PAYLOAD_FIELDS))
            coarse += event["event_date"] is not None and event["event_date_source"] == DATE_SOURCE_COARSE
        health = {
            "parity": {"canonical": len(expected_ids), "projection": len(actual_ids),
                       "missing": len(expected_ids - actual_ids), "extra": len(actual_ids - expected_ids)},
            "payload_mismatches": mismatches,
            "coarse": coarse,
        }
    if readonly:
        # Connection-local data_version is not portable across connections/processes.
        # Only a full content digest may carry a proof across generations; no disk proof is trusted.
        _PARITY_CACHE = {"connection": conn, "generation": generation, "fingerprint": fingerprint,
                         "health": health, "result": dict(health["parity"])}
    return health


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
    """Repair derived IDs/payloads without changing claims, event IDs or first-projection times."""
    init_db()
    conn = get_connection()
    with read_snapshot():
        health = _timeline_projection_health()
        parity = health["parity"]
        if not parity["missing"] and not parity["extra"] and not health["payload_mismatches"]:
            return (f"Repaired timeline_events: deleted 0 orphan row(s), inserted 0 missing row(s); "
                    f"{parity['projection']} row(s) already in parity with the current writer.")
        fingerprint = _timeline_claims_fingerprint(conn)
        expected = _canonical_timeline_events(conn)
        columns = ", ".join(("id",) + _PAYLOAD_FIELDS + ("extracted_at",))
        actual = {str(row["id"]): dict(row) for row in conn.execute(f"SELECT {columns} FROM timeline_events")}
        for key in expected.keys() & actual.keys():
            expected[key]["extracted_at"] = actual[key]["extracted_at"]
        canonical_claim_ids = {event["claim_id"] for event in expected.values()}
        orphan_ids = sorted(key for key, row in actual.items() if key not in expected
                            or row["claim_id"] is not None and str(row["claim_id"]) not in canonical_claim_ids)
        missing_ids = sorted((expected.keys() - actual.keys()) | (expected.keys() & set(orphan_ids)))
        stale_ids = sorted(key for key in expected.keys() & actual.keys() if key not in orphan_ids
                           and any(actual[key][field] != expected[key][field] for field in _PAYLOAD_FIELDS))
    if dry_run:
        return (f"[DRY RUN] Would delete {len(orphan_ids)} orphan row(s), insert {len(missing_ids)} missing row(s) and "
                f"rewrite {len(stale_ids)} row(s) written before the claim_id/event_date_source columns or with payload drift.")
    from vector_lake.db_store import transaction
    with transaction():
        if _timeline_claims_fingerprint(conn) != fingerprint:
            raise RuntimeError("Timeline inputs changed after repair planning; no repair applied")
        conn.executemany("DELETE FROM timeline_events WHERE id = ?", [(key,) for key in orphan_ids])
        conn.executemany(
            "INSERT OR REPLACE INTO timeline_events "
            "(id, claim_id, event_date, event_date_source, action, sentiment, description, entity_id, "
            "entity_title, source_file, extracted_at) "
            "VALUES (:id, :claim_id, :event_date, :event_date_source, :action, :sentiment, :description, "
            ":entity_id, :entity_title, :source_file, :extracted_at)", [expected[key] for key in missing_ids])
        updates = ", ".join(f"{field} = :{field}" for field in _PAYLOAD_FIELDS)
        conn.executemany(f"UPDATE timeline_events SET {updates} WHERE id = :id", [expected[key] for key in stale_ids])
    invalidate_timeline_parity_cache()
    return (f"Repaired timeline_events: deleted {len(orphan_ids)} orphan row(s), "
            f"inserted {len(missing_ids)} missing row(s), rewrote {len(stale_ids)} legacy row(s).")

def _sql_stable_date_expr() -> str:
    """Use the same date reader for canonical ordering and rendering, including coarse prefixes."""
    return "timeline_claim_date(claim_text, data_json)"


@lru_cache(maxsize=4096)
def _timeline_date_order(value):
    """An ordering bound, never an assertion that the event occurred at the period end."""
    text = str(value or "")
    try:
        if _DAY_PRECISION.fullmatch(text):
            return date.fromisoformat(text).isoformat()
        match = re.fullmatch(r"(\d{4})(?:-(\d{2}|Q[1-4]|H[12]))?", text)
        if not match:
            return text or None
        year, period = int(match.group(1)), match.group(2)
        if period is None:
            month = 12
        elif period[0] in "QH":
            month = int(period[1]) * (3 if period[0] == "Q" else 6)
        else:
            month = int(period)
        return date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    except (ValueError, OverflowError):
        return text or None


def _timeline_period_note(rows) -> str:
    if any(row["event_date"] and not _DAY_PRECISION.fullmatch(str(row["event_date"])) for row in rows):
        return ("[INFO] Coarse dates are ordered by the stated period end for display only; "
                "overlapping periods do not establish exact event order.\n\n")
    return ""


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

    health = _timeline_projection_health()
    parity = health["parity"]
    conn.create_function("timeline_date_order", 1, _timeline_date_order, deterministic=True)
    if parity["projection"] and not parity["missing"] and not parity["extra"] and not health["payload_mismatches"]:
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
        order = "timeline_date_order(event_date)" if health["coarse"] else "event_date"
        query += f" ORDER BY {order} DESC, id ASC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        except Exception as e:
            return f"Error executing timeline query: {e}"
        if not rows:
            return "No timeline events found matching the criteria."
        return _timeline_period_note(rows) + "\n\n".join(
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

    if health["payload_mismatches"]:
        drift_note = (f"[DEGRADED] timeline_events payload differs from canonical claims "
                      f"(rows={health['payload_mismatches']}); answering from canonical claims.\n\n")
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
    conn.create_function("timeline_claim_date", 2, lambda text, raw:
                         claim_event_date({"claim_text": text}, json.loads(raw)), deterministic=True)
    stable_date = f"timeline_date_order({_sql_stable_date_expr()})"
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
    rendered_events = []
    for r in decoded_events:
        event = _event_from_claim_row(r, entity_titles)
        rendered_events.append(event)
        results.append(
            f"[{event['event_date'] or UNKNOWN_DATE_LABEL}] <{event['entity_title'] or event['entity_id']}>\n"
            f"  -> {_display_event_description(str(event['description']))}\n"
            f"  Action: {event['action']} | Source: {event['source_file']}"
        )

    return drift_note + _timeline_period_note(rendered_events) + "\n\n".join(results)
