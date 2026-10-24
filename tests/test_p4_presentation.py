"""P4 display-only contracts, using synthetic claims and task-local payload files."""

import json

import pytest

from vector_lake import (
    db_store,
    tool_query,
    tool_search,
    tool_timeline,
)


MARKER = "<!-- vector-lake-relation:0123456789abcdef -->"
UNKNOWN = "<!-- source disputes the date; verify before use -->"


@pytest.mark.parametrize("fallback", [False, True], ids=["projection", "canonical"])
@pytest.mark.parametrize(("text", "display"), [
    pytest.param(None, "None", id="nullable-description"),
    ("", ""),
    (f"\\{MARKER}", f"\\{MARKER}"),
    (f"A source claim {MARKER}", "A source claim "),
    (f"{MARKER} A source claim {MARKER}", " A source claim "),
    (UNKNOWN, UNKNOWN),
    ("<!-- vector-lake-relation:not-a-digest -->", "<!-- vector-lake-relation:not-a-digest -->"),
    ("<!-- vector-lake-relation:0123456789ABCDEF -->", "<!-- vector-lake-relation:0123456789ABCDEF -->"),
    ("<!-- vector-lake-relation:0123456789abcdef0 -->", "<!-- vector-lake-relation:0123456789abcdef0 -->"),
    (f"<!-- quoted comment: {MARKER}", f"<!-- quoted comment: {MARKER}"),
    (f"`{MARKER}` outside {MARKER}", f"`{MARKER}` outside "),
    (f"``a ` {MARKER} `` outside {MARKER}", f"``a ` {MARKER} `` outside "),
    (f"```html\n{MARKER}\n```\n{MARKER}", f"```html\n{MARKER}\n```\n"),
    (f"~~~html\n{MARKER}\n~~~~\n{MARKER}", f"~~~html\n{MARKER}\n~~~~\n"),
    (f"   ````md\n```\n{MARKER}\n   `````\n{MARKER}", f"   ````md\n```\n{MARKER}\n   `````\n"),
    (f"```html\n{MARKER}", f"```html\n{MARKER}"),
    (f"~~~html\r\n{MARKER}\r\n~~~\r\n{MARKER}", f"~~~html\r\n{MARKER}\r\n~~~\r\n"),
])
def test_timeline_removes_only_generated_nonliteral_markers(isolated_memory, fallback, text, display):
    db_store.init_db()
    conn = db_store.get_connection()
    data = {
        "claim_type": "timeline-event", "temporal_anchor": "2026-03",
        "subject_entity_ids": ["Vendor_Subject"], "source_ids": ["Source_Synthetic"],
        "action": "Observation",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("synthetic_event", text, "active", json.dumps(data), "2026-09-01"),
        )
    tool_timeline.rebuild_timeline_events_from_claims(dry_run=False)
    if fallback:
        with db_store.transaction():
            conn.execute("UPDATE timeline_events SET id = 'synthetic-wrong-id'")
    before = [tuple(row) for row in conn.execute("SELECT * FROM claims")]
    projection_before = [tuple(row) for row in conn.execute("SELECT * FROM timeline_events")]

    result = tool_timeline.search_timeline_events(limit=1)
    if fallback:
        assert result.startswith("[DEGRADED]")
        result = result.split("\n\n", 1)[1]
    assert result == (
        "[INFO] Coarse dates are ordered by the stated period end for display only; "
        "overlapping periods do not establish exact event order.\n\n"
        f"[2026-03] <Vendor_Subject>\n  -> {display}\n"
        "  Action: Observation | Source: Source_Synthetic"
    )
    assert [tuple(row) for row in conn.execute("SELECT * FROM claims")] == before
    assert [tuple(row) for row in conn.execute("SELECT * FROM timeline_events")] == projection_before


@pytest.mark.parametrize("fallback", [False, True], ids=["projection", "canonical"])
def test_timeline_mention_search_is_not_a_strict_subject_filter(isolated_memory, fallback):
    db_store.init_db()
    conn = db_store.get_connection()
    data = {
        "claim_type": "timeline-event", "subject_entity_ids": ["Vendor_Other"],
        "source_ids": ["Source_Synthetic"], "action": "Observation",
    }
    with db_store.transaction():
        conn.execute(
            "INSERT INTO claims (claim_id, claim_text, status, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("mention", "The source compares MentionedCo with the subject.", "active", json.dumps(data), "2026-09-01"),
        )
    tool_timeline.rebuild_timeline_events_from_claims(dry_run=False)
    if fallback:
        with db_store.transaction():
            conn.execute("DELETE FROM timeline_events")
    result = tool_timeline.search_timeline_events(entity_name="MentionedCo", action="Observation")
    assert "[Unknown Date] <Vendor_Other>" in result
    assert "compares MentionedCo" in result
    assert "Source: Source_Synthetic" in result
    assert "No timeline events" in tool_timeline.search_timeline_events(entity_name="AbsentCo")


def _context(notes):
    return {
        "memory_packet": "[WARNING] synthetic unresolved source conflict", "memory_count": 1,
        "memory_warning_count": 1, "memory_omitted_count": 2,
        "wiki_context": "SYNTHETIC SOURCE CLAIM", "wiki_page_count": 1,
        "index_summary": "", "purpose": "", "retrieval_notes": notes, "budget_max": 8000,
    }


@pytest.mark.parametrize("notes", [
    ["External embedding disabled by request policy"],
    ["Retrieval profile: auto; ranking preference is not evidence confidence", "unknown dependency failure"],
    ["vector arm failed: TimeoutError"],
    [],
])
def test_query_uses_search_severity_without_hiding_memory_warnings(notes):
    context = _context(notes)
    result = tool_query._render_context_payload("probe", context)
    information, degradations = tool_search.split_retrieval_notes(notes)
    for label, items in (("INFO", information), ("DEGRADED", degradations)):
        for note in items:
            assert f"- {note}" in result.split(f"--- RETRIEVAL NOTES ({label}) ---\n", 1)[1]
    assert "(1 items, 1 warnings)" in result
    assert context["memory_packet"] in result
    assert "--- RETRIEVAL NOTES (DEGRADED) ---" in result
    assert "2 operational memory item(s) were dropped" in result
    assert "--- RETRIEVAL NOTES (information) ---" not in result
    assert "--- RETRIEVAL NOTES (degradations) ---" not in result


def test_query_info_only_is_not_a_degradation():
    context = _context(["External embedding disabled by request policy"])
    context.update(memory_omitted_count=0, memory_warning_count=0, memory_packet="")
    result = tool_query._render_context_payload("probe", context)
    assert "--- RETRIEVAL NOTES (INFO) ---" in result
    assert "DEGRADED" not in result


@pytest.mark.parametrize("dry_run", [False, True])
def test_query_payload_keeps_severity_and_existing_write_semantics(tmp_path, monkeypatch, dry_run):
    context = _context(["Retrieval profile: auto", "synthetic backend unavailable"])
    monkeypatch.setattr(tool_query, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(tool_query, "assemble_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(tool_query, "read_template", lambda *args, **kwargs: "Payload {{payload_path}} for {{query_str}}")
    preview = tool_query.preview_query_context("probe", domain="General")
    assert not (tmp_path / "tmp").exists()
    result = tool_query.prepare_query_context("probe", dry_run=dry_run, domain="General")
    payloads = list((tmp_path / "tmp").glob("query_context_*.md"))
    assert len(payloads) == 1
    assert payloads[0].read_text(encoding="utf-8") == preview
    assert "--- RETRIEVAL NOTES (INFO) ---" in preview
    assert "--- RETRIEVAL NOTES (DEGRADED) ---" in preview
    assert result.startswith("[DRY RUN]") is dry_run


@pytest.mark.parametrize("entry", ["preview_query_context", "prepare_query_context"])
def test_query_assembly_error_is_not_an_empty_result_or_payload(tmp_path, monkeypatch, entry):
    def fail(*args, **kwargs):
        raise TimeoutError("synthetic backend timeout")
    monkeypatch.setattr(tool_query, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(tool_query, "assemble_context", fail)
    with pytest.raises(TimeoutError, match="synthetic backend timeout"):
        getattr(tool_query, entry)("probe", domain="General")
    assert not (tmp_path / "tmp").exists()
