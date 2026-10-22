"""The retrieval ledger: what it records, what it refuses to record, and its ceiling.

It exists because the A1/A2 ranking work needed a baseline that this lake never kept (no
``log.md``, no query table).  Two properties carry that purpose, and both are asserted here: the
query is identified by digest rather than stored, and the file cannot grow without limit.  A third
is the reason it is safe to put on a read path at all: a write failure must not fail the search.
"""

from __future__ import annotations

import json

import pytest

from vector_lake import search_ledger


@pytest.fixture(autouse=True)
def ledger_enabled(monkeypatch):
    monkeypatch.setenv('VECTOR_LAKE_SEARCH_LEDGER', '1')


def test_the_query_is_identified_not_stored(isolated_memory):
    """The line answers "same query?" without retaining caller text."""
    query = "梅奥诊所 的 数据平台"

    search_ledger.record(
        query,
        mode="page",
        top_k=5,
        returned=[{"key": "Concept_A", "origin": "fts", "score": 0.9}],
        notes=["fell back"],
        elapsed_ms=12.34,
    )

    raw = search_ledger.ledger_path().read_text(encoding="utf-8")
    assert "梅奥" not in raw
    entry = json.loads(raw.strip())
    assert entry["q_hash"] == search_ledger.query_digest(query)
    # The length is what a digest cannot tell a reader looking at the file by eye.
    assert entry["q_chars"] == len(query)
    assert entry["returned"] == [{"key": "Concept_A", "origin": "fts", "score": 0.9}]
    assert entry["top_k"] == 5 and entry["notes"] == ["fell back"] and entry["ms"] == 12.3


def test_the_digest_is_stable_and_distinguishes_queries(isolated_memory):
    assert search_ledger.query_digest("a") == search_ledger.query_digest("a")
    assert search_ledger.query_digest("a") != search_ledger.query_digest("b")
    assert len(search_ledger.query_digest("anything")) == 16


def test_the_switch_turns_it_off_entirely(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", "0")

    search_ledger.record("q", mode="page", top_k=1, returned=[])

    assert search_ledger.enabled() is False
    assert search_ledger.entries() == []
    assert search_ledger.summary()["entries"] == 0


def test_summary_counts_entries_and_distinct_queries(isolated_memory):
    for query in ("one", "one", "two"):
        search_ledger.record(query, mode="page", top_k=1, returned=[])

    summary = search_ledger.summary()

    assert summary["entries"] == 3
    assert summary["distinct_queries"] == 2


def test_it_rotates_one_generation_at_the_ceiling(isolated_memory, monkeypatch):
    """Bounded, not merely small: past the ceiling the old file is moved aside once."""
    monkeypatch.setattr(search_ledger, "MAX_BYTES", 400)
    for index in range(40):
        search_ledger.record(f"query {index}", mode="page", top_k=1, returned=[])

    path = search_ledger.ledger_path()
    rotated = path.with_name(path.name + ".1")

    assert rotated.exists(), "the ceiling was passed without rotating"
    assert path.stat().st_size < 400 * 2, "both generations together exceeded the 2x ceiling"


def test_a_write_failure_never_reaches_the_caller(isolated_memory, monkeypatch):
    """Fail-open: a measurement may not become a reason a search fails."""

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _boom)

    search_ledger.record("q", mode="page", top_k=1, returned=[])  # must not raise


def test_entries_reads_back_what_was_written_and_skips_torn_lines(isolated_memory):
    search_ledger.record("first", mode="page", top_k=1, returned=[])
    with search_ledger.ledger_path().open("a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")
    search_ledger.record("second", mode="page", top_k=1, returned=[])

    rows = search_ledger.entries()

    assert [row["q_hash"] for row in rows] == [
        search_ledger.query_digest("first"),
        search_ledger.query_digest("second"),
    ]
    assert len(search_ledger.entries(limit=1)) == 1


@pytest.mark.parametrize("value", ["1", "", "true", "yes"])
def test_anything_but_zero_leaves_it_on(isolated_memory, monkeypatch, value):
    monkeypatch.setenv("VECTOR_LAKE_SEARCH_LEDGER", value)
    assert search_ledger.enabled() is True


def test_the_summary_reports_the_readiness_numbers_not_just_the_count(isolated_memory):
    """A count says the ledger is on; empty rate, latency and origin say what it answered.

    Before the evaluation traffic was moved out, 893 of 901 entries were batch-1/2 rows, so a
    reading taken from this file described the evaluation sets rather than the lake in use.
    """
    search_ledger.record("q1", mode="page", top_k=5,
                         returned=[{"key": "Concept_A", "origin": "vec"}, {"key": "Concept_B", "origin": "fts"}],
                         elapsed_ms=100.0)
    search_ledger.record("q2", mode="page", top_k=5, returned=[], elapsed_ms=300.0,
                         notes=["no stored vectors matched"])
    search_ledger.record("q1", mode="page", top_k=5, returned=[{"key": "Concept_A", "origin": "vec"}],
                         elapsed_ms=200.0, error="boom")

    summary = search_ledger.summary()

    assert summary["entries"] == 3
    assert summary["distinct_queries"] == 2, "the same query twice is one distinct query"
    assert summary["answered"] == 2
    assert summary["empty"] == 1
    assert summary["empty_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert summary["errors"] == 1
    assert summary["latency_ms_p50"] == 200.0
    assert summary["latency_ms_p90"] == 300.0
    assert summary["origins"] == {"vec": 2, "fts": 1}


def test_an_empty_ledger_reports_no_rate_rather_than_zero(isolated_memory):
    """``0%`` empty and "nothing recorded" are different readiness statements."""
    summary = search_ledger.summary()

    assert summary["entries"] == 0
    assert summary["empty_rate"] is None
    assert summary["latency_ms_p50"] is None


def test_summary_separates_the_surfaces_that_share_the_file(isolated_memory):
    """A page search and a context assembly differ by an order of magnitude in one file."""
    from vector_lake import search_ledger

    for name, ms in (("search", 40.0), ("search", 60.0), ("context", 460.0), ("context", 640.0)):
        search_ledger.record("q", mode="page", top_k=5, returned=[], elapsed_ms=ms,
                             entry_point=name)

    summary = search_ledger.summary()

    assert summary["entries"] == 4
    assert summary["by_surface"]["search"]["entries"] == 2
    assert summary["by_surface"]["search"]["latency_ms_p50"] <= 60.0
    assert summary["by_surface"]["context"]["latency_ms_p50"] >= 460.0
    # The mixed headline number is still there, and is exactly what should not be read alone.
    assert summary["latency_ms_p50"] is not None


def test_rows_written_before_entry_point_are_attributed_to_their_mode(isolated_memory):
    from vector_lake import search_ledger

    search_ledger.record("q", mode="memory", top_k=5, returned=[], elapsed_ms=30.0)

    assert search_ledger.summary()["by_surface"]["memory"]["entries"] == 1
