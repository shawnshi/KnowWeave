"""The filtered path must reject on metadata *before* it pays for the exact rerank -- invariantly.

The optimisation changes which eligible page fills a slot (the candidate pool becomes the binary
shortlist), so what has to hold is stated as invariants rather than as byte-equality with the old
ordering:

* every returned page satisfies the caller's selectors;
* the widening loop still widens when a filter rejects everything, and still stops when the source
  itself came back short;
* an absent shadow index falls back to the plain arm instead of returning nothing;
* the switch really disables the new order.
"""

from __future__ import annotations

import numpy as np
import pytest

from vector_lake import db_store, tool_search as ts, two_stage_index

DIM = 3072


def _seed(monkeypatch, count: int = 40) -> dict[str, np.ndarray]:
    """A closed index whose pages split across two domains, with the shadow built."""
    db_store.init_db()
    db_store.record_embedding_projection("test-embedding", DIM)
    rng = np.random.default_rng(5)
    base = rng.normal(size=DIM)
    base /= np.linalg.norm(base)
    scale = 0.2 / DIM**0.5
    vectors = {}
    for i in range(count):
        vec = (base + rng.normal(scale=scale, size=DIM))
        vectors[f"Doc_{i:03d}"] = vec / np.linalg.norm(vec)
    conn = db_store.get_connection()
    for key, vec in vectors.items():
        db_store.upsert_embedding(key, [float(x) for x in vec])
        domain = "Alpha" if int(key[-3:]) % 2 == 0 else "Beta"
        # Its own transaction: ``upsert_embedding`` opens one per call, and leaving an implicit
        # transaction open here makes the next BEGIN IMMEDIATE fail.
        with db_store.transaction():
            conn.execute(
                "INSERT OR REPLACE INTO page_index_nodes (node_key, node_id, title, type, status,"
                " domain, topic_cluster, node_json) VALUES (?, ?, ?, 'Concept', 'Active', ?, 'c', ?)",
                (key, key, key, domain, _node_json(key, domain)),
            )
    return vectors


def _node_json(key: str, domain: str) -> str:
    """A node payload as the projection stores it: ``nodes_by_key`` reads the JSON, not the columns."""
    import json

    return json.dumps({
        "_key": key,
        "title": key,
        "type": "Concept",
        "status": "Active",
        "domain": domain,
        "topic_cluster": "c",
    })


class _Catalog:
    """Just enough catalog for the filtered arm: node metadata from the projection."""

    def nodes_by_key(self, keys):
        from vector_lake import page_index_projection

        return page_index_projection.nodes_by_key(list(keys))

    def filter_fields(self, keys):
        from vector_lake import page_index_projection

        return page_index_projection.filter_fields(list(keys))


def test_filter_fields_projection_matches_full_nodes(monkeypatch):
    """The cheap lookup must carry exactly what the selectors would have read from the JSON.

    If a selector ever starts reading a fourth field, this comparison fails rather than the filter
    silently seeing ``None``.
    """
    vectors = _seed(monkeypatch)
    from vector_lake import page_index_projection

    keys = [*vectors, "Doc_missing"]
    cheap = page_index_projection.filter_fields(keys)
    full = page_index_projection.nodes_by_key(keys)
    assert set(cheap) == set(full), "missing keys must be absent from both lookups"
    for key, fields in cheap.items():
        assert set(fields) == set(page_index_projection.FILTER_FIELDS)
        for name in page_index_projection.FILTER_FIELDS:
            assert fields[name] == full[key].get(name), f"{key}.{name} differs from the node payload"


def test_arm_prefers_the_cheap_lookup_and_falls_back_for_expressions(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = [float(x) for x in vectors["Doc_000"]]

    class Recording:
        def __init__(self):
            self.calls: list[str] = []

        def nodes_by_key(self, keys):
            self.calls.append("nodes_by_key")
            return _Catalog().nodes_by_key(keys)

        def filter_fields(self, keys):
            self.calls.append("filter_fields")
            return _Catalog().filter_fields(keys)

    plain = Recording()
    ts._get_vector_search_results_filtered(query, 20, plain, "Alpha", None, False, None)
    assert plain.calls == ["filter_fields"], "a plain filter must not deserialise node payloads"

    expressed = Recording()
    results, error, _raw = ts._get_vector_search_results_filtered(
        query, 20, expressed, None, None, False, "title == 'Doc_000'"
    )
    assert error is None
    assert expressed.calls == ["nodes_by_key"], "a filter_expr can read any field, so it needs nodes"
    assert list(results) == ["Doc_000"]


def test_filtered_arm_returns_only_eligible_pages(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = [float(x) for x in vectors["Doc_000"]]

    results, error, examined = ts._get_vector_search_results_filtered(
        query, 20, _Catalog(), "Alpha", None, False, None
    )
    assert error is None and results
    assert examined > 0
    domains = {
        row["domain"]
        for row in conn.execute("SELECT domain FROM page_index_nodes WHERE node_key != ''")
    }
    assert domains == {"Alpha", "Beta"}
    for key in results:
        row = conn.execute("SELECT domain FROM page_index_nodes WHERE node_key = ?", (key,)).fetchone()
        assert row["domain"] == "Alpha", f"{key} does not satisfy the domain filter"
    assert len(results) <= 20


def test_filtered_arm_reports_the_raw_depth_not_the_survivor_count(monkeypatch):
    """Widening must not read a heavy filter as an exhausted corpus."""
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = [float(x) for x in vectors["Doc_000"]]

    results, _error, examined = ts._get_vector_search_results_filtered(
        query, 10, _Catalog(), "NoSuchDomain", None, False, None
    )
    assert results == {}
    assert examined >= 10, "the shortlist was full; only the filter rejected everything"


@pytest.mark.parametrize("domain,threshold", [("Alpha", 0.5), ("NoSuchDomain", 0.5), ("Alpha", 1.1)])
def test_filtered_arm_records_metadata_and_similarity_survivors(monkeypatch, domain, threshold):
    vectors = _seed(monkeypatch, count=20)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    monkeypatch.setattr(ts, "VECTOR_MIN_COSINE", threshold)
    query = [float(x) for x in vectors["Doc_000"]]
    reuse = {}

    results, error, examined = ts._get_vector_search_results_filtered(
        query, 20, _Catalog(), domain, None, False, None, reuse=reuse
    )

    assert error is None and examined == 20
    assert reuse["diagnostics"] == {
        "metadata_passed": 0 if domain == "NoSuchDomain" else 10,
        "similarity_passed": len(results),
    }
    assert len(results) == (10 if domain == "Alpha" and threshold == 0.5 else 0)


def test_filtered_arm_does_not_reuse_diagnostics_after_failure(monkeypatch):
    vectors = _seed(monkeypatch, count=20)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    reuse = {}
    ts._get_vector_search_results_filtered(
        [float(x) for x in vectors["Doc_000"]], 20, _Catalog(), "Alpha", None, False, None,
        reuse=reuse,
    )

    results, error, _ = ts._get_vector_search_results_filtered(
        [0.0], 20, _Catalog(), "Alpha", None, False, None, reuse=reuse,
    )

    assert results is None and error
    assert "diagnostics" not in reuse


def test_vector_cap_includes_filter_counts_and_selector_context(monkeypatch):
    vectors = _seed(monkeypatch, count=20)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    monkeypatch.setattr(ts, "MAX_FILTERED_RECALL_CANDIDATES", 16)
    monkeypatch.setattr(ts, "VECTOR_MIN_COSINE", 1.1)
    monkeypatch.setattr(ts, "_candidate_depth", lambda: 5)
    monkeypatch.setattr(ts, "_expand_query_locally", lambda query: [query])
    monkeypatch.setattr(ts, "_get_fts_recall_results", lambda *a, **k: [])
    monkeypatch.setattr(ts, "_vector_projection_error", lambda: None)
    monkeypatch.setattr(
        ts, "_get_query_embedding", lambda query: ([float(x) for x in vectors["Doc_000"]], None)
    )

    rows, notes, error = ts._search_scored_pages(
        "probe", top_k=3, domain="Alpha", catalog=_Catalog()
    )

    assert error is None and rows == []
    note = next(note for note in notes if "filtered recall cap" in note)
    assert "raw_examined=16" in note
    assert "target_depth=5" in note and "selected=0" in note
    assert "metadata_passed=" in note and "similarity_passed=0" in note
    assert "min_cosine>1.1" in note and "domain=Alpha" in note
    assert "include_history=False" in note
    assert not any("embedding-backfill" in note for note in notes)


def test_filtered_recall_widens_to_the_cap_when_everything_is_rejected():
    calls: list[int] = []

    def fetch(limit):
        calls.append(limit)
        return {"k": 1.0}, None

    raw = {"value": 10**6}
    selected, error, note = ts._filtered_recall(
        fetch, _RejectingCatalog(), 5, "Alpha", None, False, None, raw_count=lambda: raw["value"]
    )
    assert error is None
    assert len(calls) > 1, "a filter that rejects everything must widen"
    assert selected == {}
    assert note and "cap" in note, "widening that reaches the cap must say so"


def test_filtered_recall_names_the_selectors_when_the_source_is_exhausted():
    """A short *raw* list with keys present means the filter decided, not an empty corpus."""

    def fetch(limit):
        return {"k": 1.0}, None

    selected, _error, note = ts._filtered_recall(
        fetch, _RejectingCatalog(), 5, "Alpha", None, False, None, raw_count=lambda: 5
    )
    assert selected == {}
    assert note and "selectors" in note


def test_filtered_recall_stops_when_the_raw_source_is_short():
    calls: list[int] = []

    def fetch(limit):
        calls.append(limit)
        return {"k": 1.0}, None

    selected, _error, _note = ts._filtered_recall(
        fetch, _RejectingCatalog(), 5, "Alpha", None, False, None, raw_count=lambda: 1
    )
    assert len(calls) == 1, "a short raw list means the source itself is exhausted"
    assert selected == {}


def test_absent_shadow_falls_back_to_the_plain_arm(monkeypatch):
    """No shadow index: the arm reports itself unavailable and the caller keeps working."""
    db_store.init_db()
    results, error, examined = ts._get_vector_search_results_filtered(
        [0.0] * DIM, 5, _Catalog(), "Alpha", None, False, None
    )
    assert results is None and examined == 0
    assert error is None, "unavailability is not an error the caller should surface"


def test_switch_off_skips_the_metadata_first_arm(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    monkeypatch.setenv("VECTOR_LAKE_METADATA_FIRST", "0")

    called = []
    monkeypatch.setattr(
        ts, "_get_vector_search_results_filtered",
        lambda *a, **k: called.append(a) or (None, None, 0),
    )
    real = ts._get_query_embedding
    ts._get_query_embedding = lambda q, _v=None: ([float(x) for x in vectors["Doc_000"]], None)
    try:
        ts._search_scored_pages("switch off", top_k=3, domain="Alpha")
    finally:
        ts._get_query_embedding = real
    assert called == [], "the switch must restore the legacy order without consulting the new arm"


class _RejectingCatalog:
    def nodes_by_key(self, keys):
        return {key: {"_key": key, "domain": "Other", "status": "Active"} for key in keys}


def test_widening_reuses_prefix_and_only_filters_new_keys(monkeypatch):
    vectors = _seed(monkeypatch, count=80)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = list(vectors["Doc_000"])
    catalog = _Catalog()
    expected = {
        depth: ts._get_vector_search_results_filtered(
            query, depth, catalog, "Alpha", None, False, None
        )
        for depth in (5, 10, 20, 40, 80)
    }
    calls, checked = [], []
    original = two_stage_index.shortlist_keys

    def shortlist(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)

    class Recording(_Catalog):
        def filter_fields(self, keys):
            checked.extend(keys)
            return super().filter_fields(keys)

    monkeypatch.setattr(two_stage_index, "shortlist_keys", shortlist)
    reuse = {}
    recorded = Recording()
    for depth, oracle in expected.items():
        actual = ts._get_vector_search_results_filtered(
            query, depth, recorded, "Alpha", None, False, None, reuse=reuse
        )
        assert actual == oracle
        assert list(actual[0]) == list(oracle[0]), "exact L2 order must be unchanged"
    assert calls == [5, ts.MAX_FILTERED_RECALL_CANDIDATES]
    assert len(checked) == len(set(checked)) == len(vectors)


def test_reuse_keeps_full_nodes_for_filter_expr_and_resets_selectors(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = list(vectors["Doc_000"])
    reuse = {}
    catalog = _Catalog()
    for domain, expr in (("Alpha", None), ("Beta", None), (None, "title == 'Doc_000'")):
        for depth in (5, 10, 40):
            actual = ts._get_vector_search_results_filtered(
                query, depth, catalog, domain, None, False, expr, reuse=reuse
            )
            oracle = ts._get_vector_search_results_filtered(
                query, depth, catalog, domain, None, False, expr
            )
            assert actual == oracle


def test_reuse_invalidates_after_local_write_and_projection_change(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = list(vectors["Doc_000"])
    reuse, catalog = {}, _Catalog()
    ts._get_vector_search_results_filtered(
        query, 40, catalog, "Alpha", None, False, None, reuse=reuse
    )
    with db_store.transaction():
        conn.execute("DELETE FROM page_index_nodes WHERE node_key = 'Doc_000'")
    actual = ts._get_vector_search_results_filtered(
        query, 40, catalog, "Alpha", None, False, None, reuse=reuse
    )
    assert "Doc_000" not in actual[0]
    assert actual == ts._get_vector_search_results_filtered(
        query, 40, catalog, "Alpha", None, False, None
    )
    db_store.record_embedding_projection("changed-model", DIM)
    assert ts._get_vector_search_results_filtered(
        query, 40, catalog, "Alpha", None, False, None, reuse=reuse
    ) == (None, None, 0)


def test_reuse_invalidates_after_external_write(monkeypatch):
    import sqlite3

    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = list(vectors["Doc_000"])
    reuse, catalog = {}, _Catalog()
    ts._get_vector_search_results_filtered(
        query, 40, catalog, "Alpha", None, False, None, reuse=reuse
    )
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path)
    try:
        with other:
            other.execute("DELETE FROM page_index_nodes WHERE node_key = 'Doc_000'")
    finally:
        other.close()
    actual = ts._get_vector_search_results_filtered(
        query, 40, catalog, "Alpha", None, False, None, reuse=reuse
    )
    assert "Doc_000" not in actual[0]
    assert actual == ts._get_vector_search_results_filtered(
        query, 40, catalog, "Alpha", None, False, None
    )


def test_prefix_mismatch_falls_back_without_unioning_candidates(monkeypatch):
    vectors = _seed(monkeypatch, count=80)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = list(vectors["Doc_000"])
    catalog = _Catalog()
    expected = {
        depth: ts._get_vector_search_results_filtered(
            query, depth, catalog, "Alpha", None, False, None
        ) for depth in (5, 10, 20)
    }
    original, calls = two_stage_index.shortlist_keys, []

    def inconsistent(*args, **kwargs):
        calls.append(args[2])
        keys = original(*args, **kwargs)
        return list(reversed(keys)) if args[2] == ts.MAX_FILTERED_RECALL_CANDIDATES else keys

    monkeypatch.setattr(two_stage_index, "shortlist_keys", inconsistent)
    reuse = {}
    for depth, oracle in expected.items():
        assert ts._get_vector_search_results_filtered(
            query, depth, catalog, "Alpha", None, False, None, reuse=reuse
        ) == oracle
    assert calls == [5, ts.MAX_FILTERED_RECALL_CANDIDATES, 10, 20]


def test_reuse_retains_cap_note_and_raw_exhaustion_semantics(monkeypatch):
    vectors = _seed(monkeypatch, count=80)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    query = list(vectors["Doc_000"])
    monkeypatch.setattr(ts, "MAX_FILTERED_RECALL_CANDIDATES", 40)
    reuse, catalog, raw = {}, _Catalog(), {}

    def fetch(depth):
        rows, error, raw["count"] = ts._get_vector_search_results_filtered(
            query, depth, catalog, "Absent", None, False, None, reuse=reuse
        )
        return rows, error

    rows, error, note = ts._filtered_recall(
        fetch, catalog, 5, "Absent", None, False, None, raw_count=lambda: raw["count"]
    )
    assert rows == {} and error is None
    assert note and "cap (40) reached" in note
    monkeypatch.setattr(ts, "MAX_FILTERED_RECALL_CANDIDATES", 4096)
    reuse.clear()
    rows, error, note = ts._filtered_recall(
        fetch, catalog, 5, "Absent", None, False, None, raw_count=lambda: raw["count"]
    )
    assert rows == {} and error is None
    assert not note or "cap" not in note, "a short raw source really is exhausted"


def test_supported_binary_backend_keeps_tied_prefixes_across_chunks():
    import sqlite3
    import sqlite_vec

    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        if conn.execute("SELECT vec_version()").fetchone()[0] != "v0.1.9":
            pytest.skip("unknown backends use demand-sized scans, not pooled prefixes")
        conn.execute("CREATE VIRTUAL TABLE bits_test USING vec0(page_key TEXT PRIMARY KEY, bits BIT[16])")
        rng = np.random.default_rng(9)
        for i in range(2050):
            blob = bytes([i % 3] * 2) if i % 2 else rng.integers(0, 256, 2, dtype=np.uint8).tobytes()
            conn.execute("INSERT INTO bits_test VALUES (?, vec_bit(?))", (f"p{i:04d}", blob))
        sql = "SELECT page_key FROM bits_test WHERE bits MATCH vec_bit(?) AND k = ? ORDER BY distance"
        for query in (bytes([0, 0]), bytes([255, 255]), bytes([0, 255])):
            pool = list(conn.execute(sql, (query, 4096)))
            for depth in (5, 25, 50, 100, 256, 512, 1024, 2048):
                assert list(conn.execute(sql, (query, depth))) == pool[:depth]
    finally:
        conn.close()


def test_unknown_backend_uses_original_depths(monkeypatch):
    monkeypatch.setenv('VECTOR_LAKE_VECTOR_INDEX', 'two_stage')
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    calls = []
    original = two_stage_index.shortlist_keys

    class VersionProxy:
        def execute(self, sql, *args):
            if sql == "SELECT vec_version()":
                class Row:
                    def fetchone(self):
                        return ("vFuture",)
                return Row()
            return conn.execute(sql, *args)

        @property
        def total_changes(self):
            return conn.total_changes

    proxy = VersionProxy()
    monkeypatch.setattr(db_store, "get_connection", lambda: proxy)

    def recording(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(two_stage_index, "shortlist_keys", recording)
    reuse, catalog = {}, _Catalog()
    for depth in (5, 10, 20):
        result, error, raw = ts._get_vector_search_results_filtered(
            list(vectors["Doc_000"]), depth, catalog, "Alpha", None, False, None, reuse=reuse
        )
        assert error is None and result and raw == depth
        assert len(reuse["verdicts"]) <= depth
    assert calls == [5, 10, 20]


def test_reuse_does_not_leak_between_query_vectors(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    reuse, catalog = {}, _Catalog()
    for name in ("Doc_000", "Doc_001", "Doc_000"):
        for depth in (5, 20):
            query = list(vectors[name])
            assert ts._get_vector_search_results_filtered(
                query, depth, catalog, "Alpha", None, False, None, reuse=reuse
            ) == ts._get_vector_search_results_filtered(
                query, depth, catalog, "Alpha", None, False, None
            )


def test_reuse_failure_is_reported_and_cleared(monkeypatch):
    vectors = _seed(monkeypatch)
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)
    reuse, catalog = {}, _Catalog()
    query = list(vectors["Doc_000"])
    ts._get_vector_search_results_filtered(
        query, 5, catalog, "Alpha", None, False, None, reuse=reuse
    )

    def broken(*args, **kwargs):
        raise RuntimeError("shortlist dependency failed")

    monkeypatch.setattr(two_stage_index, "shortlist_keys", broken)
    result, error, raw = ts._get_vector_search_results_filtered(
        query, 10, catalog, "Alpha", None, False, None, reuse=reuse
    )
    assert result is None and raw == 0
    assert error and 'RuntimeError' in error
    assert 'shortlist dependency failed' not in error
    assert reuse == {}, "a failed expansion must not leave a reusable pool behind"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
