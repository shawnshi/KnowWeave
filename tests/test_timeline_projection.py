import json

from vector_lake import db_store, governance_store
from vector_lake.tool_timeline import (
    rebuild_timeline_events_from_claims,
    repair_timeline_projection,
    search_timeline_events,
    timeline_projection_parity,
)


def test_timeline_projection_rebuilds_from_claims(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "temporal_anchor": "2026-07-13",
        "subject_entity_ids": ["Vendor_Test"],
        "source_ids": ["Source_Test"],
        "action": "Release",
        "sentiment": "positive",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "claim_timeline_1",
                "Vendor_Test released a new product.",
                "active",
                json.dumps(payload),
                "2026-07-13T00:00:00+00:00",
            ),
        )

    dry = rebuild_timeline_events_from_claims(dry_run=True)
    assert "Would rebuild 1 timeline_events" in dry
    result = rebuild_timeline_events_from_claims(dry_run=False)
    assert "Rebuilt 1 timeline_events" in result

    output = search_timeline_events(entity_name="Vendor_Test", limit=5)
    assert "2026-07-13" in output
    assert "Vendor_Test released a new product." in output
    assert "Release" in output


def _claim(claim_id: str, page_key: str, text: str, claim_type: str = "timeline-event") -> dict:
    return {
        "claim_id": claim_id,
        "claim_text": text,
        "claim_type": claim_type,
        "status": "active",
        "temporal_anchor": "2026-07-14",
        "subject_entity_ids": ["Vendor_Shared"],
        "source_ids": [f"Source_{page_key}"],
        "locator": {"page_key": page_key},
        "source_page": f"{page_key}.md",
        "updated_at": "2026-07-14T00:00:00+00:00",
    }


def _apply_page(page_key: str, claims: list[dict]):
    with db_store.transaction():
        governance_store.apply_change_sets_batch([{
            "affected_pages": [f"{page_key}.md"],
            "proposed_entities": [],
            "proposed_claims": claims,
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }])


def test_timeline_projection_tracks_add_update_and_type_conversion(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    first = _claim("claim_delta", "Source_Delta", "First timeline value")
    _apply_page("Source_Delta", [first])
    assert conn.execute("SELECT description FROM timeline_events").fetchone()[0] == "First timeline value"

    ordinary = _claim("claim_delta", "Source_Delta", "Now an ordinary claim", claim_type="assertion")
    _apply_page("Source_Delta", [ordinary])
    assert conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0] == 0

    restored = _claim("claim_delta", "Source_Delta", "Restored timeline value")
    _apply_page("Source_Delta", [restored])
    assert conn.execute("SELECT description FROM timeline_events").fetchone()[0] == "Restored timeline value"


def test_retiring_a_claim_whose_date_moved_out_of_band_leaves_no_orphan(isolated_memory):
    """The September leak, reproduced end to end.

    Rows are projected in parity, a later pass rewrites the claim's date outside the delta
    path, and then the claim is retired through the delta.  The delete used to recompute the
    event hash from the *current* claim, which no longer named the stored row -- so the row
    outlived its claim (90 rows on the live corpus at 93 -> 0 after repair).  Keying the
    delete on ``claim_id`` is what removes it.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    _apply_page("Source_Moved", [_claim("claim_moved", "Source_Moved", "Event that moves")])
    assert conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0] == 1

    moved = json.loads(
        conn.execute("SELECT data_json FROM claims WHERE claim_id = 'claim_moved'").fetchone()["data_json"]
    )
    moved["temporal_anchor"] = "2026-08-01"
    with db_store.transaction():
        conn.execute(
            "UPDATE claims SET data_json = ? WHERE claim_id = 'claim_moved'", (json.dumps(moved),)
        )

    # The load-bearing fact: what the delta can recompute no longer names the stored row.
    from vector_lake.tool_timeline import _event_from_claim_row

    stored_id = conn.execute("SELECT id FROM timeline_events").fetchone()["id"]
    current = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims "
        "WHERE f_claim_type = 'timeline-event'"
    ).fetchone()
    assert _event_from_claim_row(current)["id"] != stored_id, (
        "the recomputed hash still matches, so this test would not exercise claim_id"
    )

    _apply_page("Source_Moved", [])

    assert conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0] == 0
    assert timeline_projection_parity()["extra"] == 0


def test_projection_rows_record_the_claim_they_came_from(isolated_memory):
    """The column an orphan is attributed by; the event id alone is a one-way hash."""
    db_store.init_db()
    _apply_page("Source_Attr", [_claim("claim_attr", "Source_Attr", "Attributed event")])

    row = db_store.get_connection().execute("SELECT claim_id FROM timeline_events").fetchone()
    assert row["claim_id"] == "claim_attr"


def test_repair_backfills_claim_id_in_place(isolated_memory):
    """A row written before the claim_id column keeps its ``extracted_at`` while converging."""
    db_store.init_db()
    conn = db_store.get_connection()
    _apply_page("Source_Repair", [_claim("claim_repair", "Source_Repair", "Repairable event")])
    stored = conn.execute("SELECT extracted_at FROM timeline_events").fetchone()["extracted_at"]
    with db_store.transaction():
        conn.execute("UPDATE timeline_events SET claim_id = NULL")

    assert "rewrite 1 row" in repair_timeline_projection(dry_run=True)
    repair_timeline_projection(dry_run=False)

    row = conn.execute("SELECT claim_id, extracted_at FROM timeline_events").fetchone()
    assert row["claim_id"] == "claim_repair"
    assert row["extracted_at"] == stored
    assert timeline_projection_parity()["extra"] == 0


def test_page_delete_only_removes_its_own_timeline_event(isolated_memory):
    db_store.init_db()
    first = _claim("claim_page_a", "Source_PageA", "Event A")
    second = _claim("claim_page_b", "Source_PageB", "Event B")
    _apply_page("Source_PageA", [first])
    _apply_page("Source_PageB", [second])

    db_store.delete_node_cascade("Source_PageA")

    rows = db_store.get_connection().execute(
        "SELECT description FROM timeline_events ORDER BY description"
    ).fetchall()
    assert [row["description"] for row in rows] == ["Event B"]


def test_timeline_search_falls_back_when_projection_count_is_stale(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    first = _claim("claim_current", "Source_Current", "Canonical current event")
    _apply_page("Source_Current", [first])
    with db_store.transaction():
        conn.execute("DELETE FROM timeline_events")
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("stale", "2000-01-01", "old", "neutral", "Stale event", "", "", "", "2000-01-01"),
        )
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "claim_second",
                "Second canonical event",
                "active",
                json.dumps(_claim("claim_second", "Source_Second", "Second canonical event")),
                "2026-07-14T01:00:00+00:00",
            ),
        )

    output = search_timeline_events(limit=10)
    assert "Canonical current event" in output
    assert "Second canonical event" in output
    assert "Stale event" not in output


def test_timeline_search_rejects_equal_count_wrong_event_ids(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    current = _claim("claim_equal", "Source_Equal", "Canonical equal-count event")
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                current["claim_id"],
                current["claim_text"],
                "active",
                json.dumps(current),
                current["updated_at"],
            ),
        )
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wrong-id", "2000-01-01", "old", "neutral", "Stale equal-count event", "", "", "", "2000-01-01"),
        )

    output = search_timeline_events(limit=10)

    assert "Canonical equal-count event" in output
    assert "Stale equal-count event" not in output


def test_apply_change_set_rolls_back_claim_when_timeline_projection_fails(isolated_memory, monkeypatch):
    db_store.init_db()
    from vector_lake import tool_timeline

    def fail_projection(old_rows, proposed_claims):
        raise RuntimeError("timeline projection failed")

    monkeypatch.setattr(tool_timeline, "sync_timeline_events_for_claim_delta", fail_projection)
    claim = _claim("claim_rollback", "Source_Rollback", "Rollback event")

    try:
        governance_store.apply_change_sets_batch([{
            "affected_pages": ["Source_Rollback.md"],
            "proposed_entities": [],
            "proposed_claims": [claim],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }])
    except RuntimeError as exc:
        assert "timeline projection failed" in str(exc)
    else:
        raise AssertionError("projection failure must abort the canonical transaction")

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE claim_id = ?", (claim["claim_id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0] == 0


def _insert_timeline_claim(conn, claim_id: str, text: str, payload: dict, updated_at: str) -> None:
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO claims (claim_id, claim_text, status, data_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (claim_id, text, "active", json.dumps(payload), updated_at),
        )


def test_filtered_search_works_on_the_canonical_fallback(isolated_memory):
    """The fallback must not reference ``claims.entity_id``, which does not exist.

    ``timeline_events`` is left deliberately stale so the projection branch is
    skipped, which is the only state in which the canonical fallback runs.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "subject_entity_ids": ["Vendor_Fallback"],
        "source_ids": ["Source_Fallback"],
        "action": "Release",
        "sentiment": "positive",
    }
    _insert_timeline_claim(
        conn,
        "claim_fallback",
        "[2026-05-02] [Release] Vendor_Fallback shipped.",
        payload,
        "2026-07-14T00:00:00+00:00",
    )

    # Leave the projection non-empty but wrong so the drift note is exercised too.
    with db_store.transaction():
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("stale-row", "2000-01-01", "old", "neutral", "Stale", "", "", "", "2000-01-01"),
        )

    output = search_timeline_events(entity_name="Vendor_Fallback", limit=5)

    assert "no such column" not in output
    assert "Vendor_Fallback shipped." in output
    assert output.startswith("[DEGRADED]")

    action_miss = search_timeline_events(action="Earnings", limit=5)
    assert "Vendor_Fallback shipped." not in action_miss
    assert "no such column" not in action_miss

    action_hit = search_timeline_events(action="Release", limit=5)
    assert "Vendor_Fallback shipped." in action_hit
    assert "no such column" not in action_hit


def test_event_date_comes_from_the_claim_text_prefix(isolated_memory):
    """Timeline claims carry their event date as a ``[YYYY-MM-DD]`` text prefix."""
    db_store.init_db()
    conn = db_store.get_connection()
    _insert_timeline_claim(
        conn,
        "claim_prefix",
        "[2026-03-01] [Observation] Event with a text date.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Prefix"],
            "source_ids": ["Source_Prefix"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    assert "Rebuilt 1 timeline_events" in rebuild_timeline_events_from_claims(dry_run=False)

    row = conn.execute("SELECT event_date FROM timeline_events").fetchone()

    assert row["event_date"] == "2026-03-01", row["event_date"]


def test_parity_cache_follows_claim_writes(isolated_memory):
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    _insert_timeline_claim(
        conn,
        "claim_cached",
        "[2026-04-04] [Release] Cached event.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Cache"],
            "source_ids": ["Source_Cache"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    rebuild_timeline_events_from_claims(dry_run=False)
    assert tool_timeline.timeline_projection_parity()["missing"] == 0

    # A new canonical claim must invalidate the memoised proof.
    _insert_timeline_claim(
        conn,
        "claim_cached_2",
        "[2026-04-05] [Release] Second cached event.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Cache"],
            "source_ids": ["Source_Cache"],
        },
        "2026-07-15T00:00:00+00:00",
    )

    assert tool_timeline.timeline_projection_parity()["missing"] == 1


def test_parity_cache_follows_out_of_band_projection_writes(isolated_memory):
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    _insert_timeline_claim(
        conn,
        "claim_oob",
        "[2026-04-06] [Release] Out of band event.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Oob"],
            "source_ids": ["Source_Oob"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    rebuild_timeline_events_from_claims(dry_run=False)
    assert tool_timeline.timeline_projection_parity()["missing"] == 0

    with db_store.transaction():
        conn.execute("DELETE FROM timeline_events")
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("zzz-wrong", "2000-01-01", "old", "neutral", "Wrong", "", "", "", "2000-01-01"),
        )

    assert tool_timeline.timeline_projection_parity()["extra"] == 1


def test_timeline_repair_closes_drift_and_leaves_correct_rows_untouched(isolated_memory):
    """Repair converges the projection without replacing the whole table.

    Only orphan ids are deleted and only missing ids inserted, so a row that was
    already correct keeps its ``extracted_at`` -- which is how this test proves a
    full-table rewrite did not happen.
    """
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "subject_entity_ids": ["Concept_Repair"],
        "source_ids": ["Source_Repair"],
    }
    _insert_timeline_claim(conn, "claim_repair_a", "[2026-04-07] [Release] First event.", dict(payload), "2026-07-14T00:00:00+00:00")
    _insert_timeline_claim(conn, "claim_repair_b", "[2026-04-08] [Release] Second event.", dict(payload), "2026-07-14T00:00:00+00:00")
    rebuild_timeline_events_from_claims(dry_run=False)
    assert tool_timeline.timeline_projection_parity()["missing"] == 0

    survivor = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE claim_id = 'claim_repair_b'"
    ).fetchone()
    survivor_id = tool_timeline._event_from_claim_row(survivor)["id"]
    extracted_before = conn.execute(
        "SELECT extracted_at FROM timeline_events WHERE id = ?", (survivor_id,)
    ).fetchone()["extracted_at"]

    # Drift of both kinds: an out-of-band orphan row, and a claim whose content
    # moved (its id is content-addressed, so the old id becomes an orphan too).
    with db_store.transaction():
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("orphan-id", "2000-01-01", "old", "neutral", "Orphan", "", "", "", "2000-01-01"),
        )
    _insert_timeline_claim(conn, "claim_repair_a", "[2026-04-09] [Release] First event moved.", dict(payload), "2026-07-16T00:00:00+00:00")

    drift = tool_timeline.timeline_projection_parity()
    assert drift["missing"] == 1, drift
    assert drift["extra"] == 2, drift

    assert "[DRY RUN]" in tool_timeline.repair_timeline_projection(dry_run=True)
    assert tool_timeline.timeline_projection_parity() == drift

    message = tool_timeline.repair_timeline_projection(dry_run=False)
    assert "deleted 2" in message and "inserted 1" in message, message

    after = tool_timeline.timeline_projection_parity()
    assert after["missing"] == 0, after
    assert after["extra"] == 0, after
    assert conn.execute(
        "SELECT extracted_at FROM timeline_events WHERE id = ?", (survivor_id,)
    ).fetchone()["extracted_at"] == extracted_before


def test_event_identity_ignores_updated_at_for_a_date_less_claim(isolated_memory):
    """Regression gate: an ``updated_at`` change must not move a claim's event id.

    The id used to be derived from ``claim_event_date``, which falls back to
    ``updated_at`` when a claim carries no anchor and no dated text.  Every rewrite
    of such a claim therefore minted a new id and orphaned the previous
    ``timeline_events`` row unless the delta sync was handed the exact previous
    row -- the drift class that kept regenerating on the live corpus (7 pairs on
    ``Concept_OperationalFacts`` were the last specimen).
    """
    from vector_lake import tool_timeline as tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "subject_entity_ids": ["Concept_Ops"],
        "source_ids": ["Source_Ops"],
    }
    text = "战略价值：多模态医疗推理可通过中间层的状态缓存实现连续推演。"
    _insert_timeline_claim(conn, "claim_dateless", text, dict(payload), "2026-06-01T00:00:00+00:00")
    rebuild_timeline_events_from_claims(dry_run=False)
    assert tool_timeline.timeline_projection_parity()["missing"] == 0
    before = conn.execute("SELECT id FROM timeline_events").fetchone()["id"]

    # The same claim written again with a fresh timestamp, and the delta sync is
    # deliberately NOT called -- the id must not have moved, so nothing is orphaned.
    _insert_timeline_claim(conn, "claim_dateless", text, dict(payload), "2026-09-17T04:33:42+00:00")

    parity = tool_timeline.timeline_projection_parity()
    assert parity["missing"] == 0, parity
    assert parity["extra"] == 0, parity
    assert conn.execute("SELECT id FROM timeline_events").fetchone()["id"] == before


def test_event_identity_still_follows_the_claim_content(isolated_memory):
    """Content addressing stays meaningful: changed text is still a new identity."""
    from vector_lake import tool_timeline as tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "subject_entity_ids": ["Concept_Ops"],
        "source_ids": ["Source_Ops"],
    }
    _insert_timeline_claim(conn, "claim_dated", "[2026-04-07] [Release] Original.", dict(payload), "2026-06-01T00:00:00+00:00")
    first = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE claim_id = 'claim_dated'"
    ).fetchone()
    first_id = tool_timeline._event_from_claim_row(first)["id"]

    _insert_timeline_claim(conn, "claim_dated", "[2026-04-07] [Release] Rewritten.", dict(payload), "2026-06-01T00:00:00+00:00")
    second = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE claim_id = 'claim_dated'"
    ).fetchone()

    assert tool_timeline._event_from_claim_row(second)["id"] != first_id


def test_timeline_repair_is_a_noop_when_parity_is_clean(isolated_memory):
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    _insert_timeline_claim(
        conn,
        "claim_clean",
        "[2026-04-10] [Release] Clean event.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Clean"],
            "source_ids": ["Source_Clean"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    rebuild_timeline_events_from_claims(dry_run=False)

    message = tool_timeline.repair_timeline_projection(dry_run=False)

    assert "deleted 0" in message and "inserted 0" in message, message
    assert tool_timeline.timeline_projection_parity()["missing"] == 0


def test_an_undated_claim_is_not_given_its_ingestion_time_as_an_event_date(isolated_memory):
    """``event_date`` states the event date or nothing -- never the ingestion time.

    The projection used to spend ``claim.updated_at`` there for the 2342 of 9974 live
    timeline claims that state no date, which is what put a fifth of the corpus -- and 94 of
    the newest 100 rows -- at the top of ``ORDER BY event_date DESC``.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "subject_entity_ids": ["Concept_Undated"],
        "source_ids": ["Source_Undated"],
    }
    _insert_timeline_claim(
        conn,
        "claim_undated",
        "An event the source never dated.",
        dict(payload),
        "2026-09-17T04:33:42+00:00",
    )
    _insert_timeline_claim(
        conn,
        "claim_dated",
        "[2026-05-02] [Release] A dated event.",
        dict(payload),
        "2026-01-01T00:00:00+00:00",
    )

    assert "Rebuilt 2 timeline_events" in rebuild_timeline_events_from_claims(dry_run=False)

    rows = {
        row["description"]: (row["event_date"], row["event_date_source"])
        for row in conn.execute("SELECT description, event_date, event_date_source FROM timeline_events")
    }
    assert rows["An event the source never dated."] == (None, "unknown")
    assert rows["[2026-05-02] [Release] A dated event."] == ("2026-05-02", "day")

    output = search_timeline_events(limit=5)
    # The undated entry is listed last, not dated 2026-09-17 and listed first.
    assert output.index("2026-05-02") < output.index("Unknown Date")


def test_the_action_falls_back_to_the_tag_in_the_ledger_prefix(isolated_memory):
    """Only 376 of the 7617 tag-bearing live claims carry a structured ``event_tag``.

    Reading that field alone left 9598 of 9974 ``action`` values at the literal fallback
    string, while the tag sat in the entry's ``[date] [Tag]`` prefix the whole time.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    payload = {
        "claim_type": "timeline-event",
        "subject_entity_ids": ["Concept_Tag"],
        "source_ids": ["Source_Tag"],
    }
    _insert_timeline_claim(
        conn,
        "claim_tag_text",
        "[2026-05-02] [Observation] Tagged in the text.",
        dict(payload),
        "2026-07-14T00:00:00+00:00",
    )
    structured = dict(payload, event_tag="Pivot")
    _insert_timeline_claim(
        conn,
        "claim_tag_field",
        "[2026-05-03] [Observation] The structured field wins.",
        structured,
        "2026-07-15T00:00:00+00:00",
    )
    assert "Rebuilt 2 timeline_events" in rebuild_timeline_events_from_claims(dry_run=False)

    actions = {
        row["description"]: row["action"]
        for row in conn.execute("SELECT description, action FROM timeline_events")
    }
    assert actions["[2026-05-02] [Observation] Tagged in the text."] == "Observation"
    assert actions["[2026-05-03] [Observation] The structured field wins."] == "Pivot"
    assert "Action: Observation" in search_timeline_events(action="Observation", limit=5)


def test_the_degraded_path_keeps_the_order_of_the_indexed_path(isolated_memory):
    """A degraded answer is the same answer, slower.

    The fallback ordered by ``updated_at`` where the indexed path ordered by ``event_date``,
    so with the projection one row out of parity the same ``limit`` query returned the
    *oldest* events instead of the newest.
    """
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(12):
            claim = {
                "claim_id": f"claim_order_{index:02d}",
                "claim_type": "timeline-event",
                "claim_text": f"[2026-06-{index + 1:02d}] [Release] Ordered event {index:02d}.",
                "subject_entity_ids": ["Concept_Order"],
                "source_ids": ["Source_Order"],
            }
            conn.execute(
                "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    claim["claim_id"],
                    claim["claim_text"],
                    "active",
                    json.dumps(claim),
                    # Deliberately the reverse of the event order, so an ingestion-ordered
                    # answer cannot pass by accident.
                    f"2026-01-{12 - index:02d}T00:00:00+00:00",
                ),
            )
    rebuild_timeline_events_from_claims(dry_run=False)
    indexed = [
        line for line in search_timeline_events(limit=3).splitlines() if line.startswith("[2026-")
    ]

    with db_store.transaction():
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, event_date_source, action, sentiment, description, entity_id, "
            "entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("orphan-order", "2000-01-01", "day", "old", "neutral", "Orphan", "", "", "", "2000-01-01"),
        )
    degraded = [
        line for line in search_timeline_events(limit=3).splitlines() if line.startswith("[2026-")
    ]

    assert indexed == degraded, (indexed, degraded)
    assert "2026-06-12" in indexed[0]


def test_timeline_repair_rewrites_rows_written_before_event_date_source(isolated_memory):
    """A row the current writer would no longer produce is drift, even with a correct id.

    Before ``event_date_source`` existed, a claim stating no date stored its ingestion
    timestamp as ``event_date``: the id was right and the date shown was an artifact.  The
    id-parity check calls that projection clean, so repair has to look at the column too --
    otherwise converging needs a full rebuild.
    """
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    _insert_timeline_claim(
        conn,
        "claim_legacy",
        "An event the source never dated.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Legacy"],
            "source_ids": ["Source_Legacy"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    rebuild_timeline_events_from_claims(dry_run=False)
    event_id = conn.execute("SELECT id FROM timeline_events").fetchone()["id"]
    projected_at = conn.execute(
        "SELECT extracted_at FROM timeline_events WHERE id = ?", (event_id,)
    ).fetchone()["extracted_at"]

    # A row written by the previous writer, when the column did not exist yet.
    with db_store.transaction():
        conn.execute(
            "UPDATE timeline_events SET event_date = ?, event_date_source = NULL WHERE id = ?",
            ("2026-07-14T00:00:00+00:00", event_id),
        )

    assert tool_timeline.timeline_projection_parity()["missing"] == 0
    assert "rewrite 1 row(s)" in tool_timeline.repair_timeline_projection(dry_run=True)

    message = tool_timeline.repair_timeline_projection(dry_run=False)

    assert "rewrote 1 legacy row(s)" in message, message
    row = conn.execute(
        "SELECT event_date, event_date_source, extracted_at FROM timeline_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    assert (row["event_date"], row["event_date_source"]) == (None, "unknown")
    # Only the three derived columns move: the row was not re-inserted, so the time it was
    # first projected survives.
    assert row["extracted_at"] == projected_at


def test_the_parity_gate_follows_the_id_inputs_and_not_updated_at(isolated_memory):
    """The gate must move when the id set can move, and stop moving when it cannot.

    ``MAX(updated_at)`` was dropped from the fingerprint: since event identity stopped
    depending on ``updated_at``, a moved timestamp can only force a full recomputation that
    reports ``missing=0 extra=0``.  It cost ~55 ms on every timeline query to do that.
    """
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    empty = tool_timeline._timeline_claims_fingerprint(conn)

    _insert_timeline_claim(
        conn,
        "claim_fp",
        "[2026-04-11] [Release] Fingerprint event.",
        {
            "claim_type": "timeline-event",
            "subject_entity_ids": ["Concept_Fp"],
            "source_ids": ["Source_Fp"],
        },
        "2026-07-14T00:00:00+00:00",
    )
    inserted = tool_timeline._timeline_claims_fingerprint(conn)
    assert inserted != empty

    # An ``updated_at`` bounce cannot move an id, so it must not invalidate the memo.
    with db_store.transaction():
        conn.execute(
            "UPDATE claims SET updated_at = ? WHERE claim_id = 'claim_fp'",
            ("2026-08-01T00:00:00+00:00",),
        )
    assert tool_timeline._timeline_claims_fingerprint(conn) == inserted

    # A write to the projection outside the delta path must invalidate it.
    with db_store.transaction():
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, event_date_source, action, sentiment, description, entity_id, "
            "entity_title, source_file, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("zzz-out-of-band", "2000-01-01", "day", "old", "neutral", "Out of band", "", "", "", "2000-01-01"),
        )
    assert tool_timeline._timeline_claims_fingerprint(conn) != inserted


def test_init_db_adds_event_date_source_to_a_complete_schema_that_lacks_it(isolated_memory):
    """A column added to an existing table needs its own staleness probe.

    ``init_db()`` skips the DDL transaction when every sentinel already exists, so an
    ``ALTER`` that is not gated by a probe never runs on a database that lacks only the new
    column -- which is every already-migrated corpus in the field, and it is how
    ``timeline-repair`` came to fail with "no such column" instead of migrating.  Dropping
    the column from a schema that is otherwise complete reproduces that state exactly;
    building a half-empty database (as this test used to) takes the DDL path and proves
    nothing about the fast one.
    """
    import sqlite3

    if sqlite3.sqlite_version_info < (3, 35, 0):  # pragma: no cover - old SQLite has no DROP COLUMN
        import pytest

        pytest.skip("ALTER TABLE ... DROP COLUMN needs SQLite 3.35+")

    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO timeline_events "
            "(id, event_date, event_date_source, action, sentiment, description, entity_id, "
            "entity_title, source_file, extracted_at) "
            "VALUES ('legacy', '2026-09-17T00:00:00+00:00', NULL, 'x', 'neutral', 'Legacy row', "
            "'', '', '', '2026-09-17')"
        )
    with db_store.transaction():
        conn.execute("ALTER TABLE timeline_events DROP COLUMN event_date_source")
    assert db_store._timeline_format_is_stale(conn)

    db_store._INITIALIZED_DB_PATHS.clear()
    db_store.init_db()

    row = conn.execute(
        "SELECT event_date, event_date_source FROM timeline_events WHERE id = 'legacy'"
    ).fetchone()
    assert row["event_date"] == "2026-09-17T00:00:00+00:00"
    # NULL is what tells repair this row's ``event_date`` still means "ingestion time".
    assert row["event_date_source"] is None


def _put_timeline_claim(conn, claim_id: str, text: str, anchor: str) -> None:
    payload = {
        "claim_type": "timeline-event",
        "temporal_anchor": anchor,
        "subject_entity_ids": ["Vendor_Test"],
        "source_ids": ["Source_Test"],
        "action": "Release",
    }
    conn.execute(
        "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
        (claim_id, text, "active", json.dumps(payload), f"{anchor}T00:00:00+00:00"),
    )


def test_the_watchdog_closes_timeline_drift_in_place(isolated_memory):
    """Drift makes every timeline query answer from claims (120-211 ms); the owner repairs it."""
    from vector_lake import watchdog_app

    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _put_timeline_claim(conn, "claim_drift_1", "Vendor_Test shipped release one.", "2026-07-13")
    assert rebuild_timeline_events_from_claims(dry_run=False).startswith("Rebuilt 1")

    with db_store.transaction():
        # An out-of-band write that removes the projected row: the id set loses a member.
        conn.execute("DELETE FROM timeline_events")
    assert timeline_projection_parity()["missing"] == 1

    healed = watchdog_app.heal_timeline_projection(force=True)

    assert healed["healed"] is True
    assert healed["parity"]["missing"] == 1
    after = timeline_projection_parity()
    assert after["missing"] == 0 and after["extra"] == 0
    assert "release one" in search_timeline_events(limit=5)


def test_the_watchdog_leaves_a_parity_projection_alone(isolated_memory):
    from vector_lake import watchdog_app

    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _put_timeline_claim(conn, "claim_ok_1", "Vendor_Test shipped release one.", "2026-07-13")
    rebuild_timeline_events_from_claims(dry_run=False)

    healed = watchdog_app.heal_timeline_projection(force=True)

    assert healed == {"healed": False, "reason": "parity",
                      "parity": {"canonical": 1, "projection": 1, "missing": 0, "extra": 0}}


def test_a_second_heal_inside_the_interval_is_throttled_before_the_parity_proof(isolated_memory, monkeypatch):
    """The proof is 79 ms cold on the live corpus; it must not run every cycle."""
    from vector_lake import watchdog_app, tool_timeline

    db_store.init_db()
    watchdog_app.heal_timeline_projection(force=True)  # establishes the throttle stamp
    calls = []
    monkeypatch.setattr(tool_timeline, "timeline_projection_parity",
                        lambda: calls.append(1) or {"canonical": 0, "projection": 0, "missing": 0, "extra": 0})

    assert watchdog_app.heal_timeline_projection()["reason"] == "throttled"
    assert calls == []


def test_lean_parity_ids_match_the_full_decode(isolated_memory):
    """The lean id set must be the same set the payload-decoding path produces."""
    from vector_lake import tool_timeline

    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        _put_timeline_claim(conn, "claim_a", "Vendor_Test shipped release one.", "2026-07-13")
        _put_timeline_claim(conn, "claim_b", "Vendor_Test shipped release two.", "2026-08-01")
        # No temporal_anchor and no event_date: identity comes from the text prefix.
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("claim_c", "[2026-09-09] [Release] Vendor_Test shipped release three.", "active",
             json.dumps({"claim_type": "timeline-event"}), "2026-09-09T00:00:00+00:00"),
        )
        # A boolean anchor: the one shape ``str()`` settles differently on the two paths.
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("claim_d", "Vendor_Test shipped release four.", "active",
             json.dumps({"claim_type": "timeline-event", "temporal_anchor": True}),
             "2026-09-10T00:00:00+00:00"),
        )
        # A container anchor, which arrives from json_extract as JSON text rather than repr.
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("claim_e", "Vendor_Test shipped release five.", "active",
             json.dumps({"claim_type": "timeline-event", "temporal_anchor": {"y": 1}}),
             "2026-09-11T00:00:00+00:00"),
        )

    full = {
        tool_timeline._event_from_claim_row(row)["id"]
        for row in conn.execute(
            "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE f_claim_type = 'timeline-event'"
        ).fetchall()
    }
    assert tool_timeline._expected_event_ids(conn) == full
