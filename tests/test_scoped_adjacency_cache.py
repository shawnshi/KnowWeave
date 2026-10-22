"""Hermetic scoped graph cache contracts, including WAL/invalidation races."""
import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from vector_lake import db_store, page_index_projection as pages, tool_search, wiki_utils


@pytest.fixture(autouse=True)
def fresh_cache(isolated_memory):
    pages.reset_catalog_cache()
    pages._invalidate_adjacency()
    db_store.init_db()
    yield
    pages.reset_catalog_cache()
    pages._invalidate_adjacency()


def payload():
    return {
        "nodes": {
            "a": {"domain": "Healthcare_IT", "topic_cluster": "C"},
            "b": {"domain": "Medical_IT", "topic_cluster": "c"},
            "general": {"domain": "General", "topic_cluster": "C"},
            "other": {"domain": "Medical_IT", "topic_cluster": "D"},
        },
        "weighted_edges": [
            {"source": "a", "target": "b", "weight": 2.0},
            {"source": "a", "target": "general", "weight": 50.0},
            {"source": "a", "target": "b", "weight": 3.0},
            {"source": "b", "target": "other", "weight": 7.0},
        ],
    }


def seed(memory, data):
    (memory / "wiki" / "index.json").write_text(json.dumps(data), encoding="utf-8")
    pages.refresh_page_index_projection(data)


def catalog_for(backend, memory, data):
    if backend == "file":
        return pages._FileCatalog(data)
    seed(memory, data)
    return pages._SqliteCatalog()


def materialize(table):
    return {key: list(neighbors) for key, neighbors in table.items()}


@pytest.mark.parametrize("backend", ["file", "projection"])
def test_reuse_normalized_scope_and_keep_edge_order(isolated_memory, backend):
    catalog = catalog_for(backend, isolated_memory, payload())
    first = catalog.scoped_adjacency("Healthcare_IT", "C")
    assert materialize(first) == {"a": [("b", 2.0), ("b", 3.0)], "b": [("a", 2.0), ("a", 3.0)]}
    assert catalog.scoped_adjacency(" Medical_IT ", "c") is first
    assert list(first) == ["a", "b"]
    assert catalog.scoped_adjacency("Medical_IT", "D") is not first
    general = catalog.scoped_adjacency("General", "C")
    assert all(not neighbors for neighbors in general.values())
    assert "a" not in general and "b" not in general
    all_domains = catalog.scoped_adjacency("", "C")
    assert ("general", 50.0) in all_domains["a"]
    with pytest.raises(TypeError):
        first["a"] = ()  # pyright: ignore[reportIndexIssue] -- intentional runtime rejection
    with pytest.raises(TypeError):
        first["b"][0] = ("poison", 99.0)  # pyright: ignore[reportIndexIssue] -- intentional runtime rejection
    assert catalog.scoped_adjacency("Medical_IT", "C") is first


@pytest.mark.parametrize("backend", ["file", "projection"])
def test_explicit_blank_domain_is_not_unfiltered(isolated_memory, backend):
    data = payload()
    data["nodes"]["a"]["domain"] = ""
    data["nodes"]["b"]["domain"] = " "
    catalog = catalog_for(backend, isolated_memory, data)
    blank = catalog.scoped_adjacency(" ", "C")
    assert materialize(blank) == {"a": [("b", 2.0), ("b", 3.0)], "b": [("a", 2.0), ("a", 3.0)]}
    assert catalog.scoped_adjacency("", "C") is not blank


@pytest.mark.parametrize("field,new_value", [("domain", "General"), ("topic_cluster", "D")])
def test_scope_generation_invalidates_even_when_edges_and_file_stamp_do_not_change(isolated_memory, monkeypatch, field, new_value):
    data = payload()
    seed(isolated_memory, data)
    catalog = pages._SqliteCatalog()
    old = catalog.scoped_adjacency("Medical_IT", "C")
    state = pages.projection_state()
    assert state is not None
    # Prove the persisted generation is sufficient, even for a different writer
    # process which cannot call this process's explicit invalidation hook.
    monkeypatch.setattr(pages, "_invalidate_adjacency", lambda: None)
    data["nodes"]["b"][field] = new_value
    result = pages.refresh_page_index_projection(data, node_keys=["b"])
    assert result["edges_rewritten"] is False
    current_state = pages.projection_state()
    assert current_state is not None and current_state["edge_digest"] == state["edge_digest"]
    assert pages.index_file_stamp() == (state["index_mtime"], state["index_size"])
    current = catalog.scoped_adjacency("Medical_IT", "C")
    assert current is not old and current == {}
    assert old["a"] == (("b", 2.0), ("b", 3.0))


def test_graph_generation_invalidates_without_node_changes(isolated_memory, monkeypatch):
    data = payload()
    seed(isolated_memory, data)
    catalog = pages._SqliteCatalog()
    old = catalog.scoped_adjacency("Medical_IT", "C")
    monkeypatch.setattr(pages, "_invalidate_adjacency", lambda: None)
    data["weighted_edges"][0]["weight"] = 11.0
    pages.refresh_page_index_projection(data)
    current = catalog.scoped_adjacency("Medical_IT", "C")
    assert current is not old and current["a"][0] == ("b", 11.0)


@pytest.mark.parametrize("change_root", [False, True])
def test_database_and_root_identity_separate_identical_graph_versions(isolated_memory, monkeypatch, change_root):
    data = payload()
    seed(isolated_memory, data)
    catalog = pages._SqliteCatalog()
    first = catalog.scoped_adjacency("Medical_IT", "C")
    first_state = dict(db_store.get_connection().execute("SELECT * FROM page_index_state").fetchone())
    monkeypatch.setattr(pages, "_invalidate_adjacency", lambda: None)
    db_store.close_connection()
    if change_root:
        memory = isolated_memory.parent / "second-memory"
        (memory / "wiki").mkdir(parents=True)
        monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(memory))
        wiki_utils.reset_memory_dir_cache()
    else:
        memory = isolated_memory
        monkeypatch.setenv("VECTOR_LAKE_DB_PATH", str(memory / "wiki" / ".meta" / "second.db"))
    db_store.init_db()
    seed(memory, data)
    conn = db_store.get_connection()
    # Hold every version field equal, so only source identity can distinguish it.
    conn.execute("UPDATE page_index_nodes SET domain='Private' WHERE node_key='b'")
    columns = list(first_state)
    conn.execute("UPDATE page_index_state SET " + ",".join(f"{key}=?" for key in columns), list(first_state.values()))
    conn.commit()
    assert catalog.scoped_adjacency("Medical_IT", "C") == {}
    assert first["a"] == (("b", 2.0), ("b", 3.0))


def test_file_reparse_throttle_never_crosses_memory_roots(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_FALLBACK_REPARSE_SECONDS", "60")
    data = payload()
    (isolated_memory / "wiki" / "index.json").write_text(json.dumps(data), encoding="utf-8")
    first, _ = pages.read_catalog()
    assert isinstance(first, pages._FileCatalog)
    first_graph = first.scoped_adjacency("Medical_IT", "C")
    second_root = isolated_memory.parent / "other-file-root"
    (second_root / "wiki").mkdir(parents=True)
    data["nodes"]["b"]["domain"] = "Private"
    (second_root / "wiki" / "index.json").write_text(json.dumps(data), encoding="utf-8")
    db_store.close_connection()
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(second_root))
    wiki_utils.reset_memory_dir_cache()
    second, _ = pages.read_catalog()
    assert isinstance(second, pages._FileCatalog) and second is not first
    assert all(not values for values in second.scoped_adjacency("Medical_IT", "C").values())
    assert first_graph["a"] == (("b", 2.0), ("b", 3.0))


def test_sqlite_file_fallback_and_recovery_never_share_snapshots(isolated_memory):
    data = payload()
    seed(isolated_memory, data)
    catalog, note = pages.read_catalog()
    assert note is None and isinstance(catalog, pages._SqliteCatalog)
    projected = catalog.scoped_adjacency("Medical_IT", "C")
    data["nodes"]["b"]["domain"] = "Private"
    (isolated_memory / "wiki" / "index.json").write_text(json.dumps(data), encoding="utf-8")
    fallback, note = pages.read_catalog()
    assert note and isinstance(fallback, pages._FileCatalog)
    file_table = fallback.scoped_adjacency("Medical_IT", "C")
    assert all(not values for values in file_table.values())
    assert fallback.scoped_adjacency("Medical_IT", "c") is file_table
    assert file_table is not projected
    pages.refresh_page_index_projection(data)
    recovered, note = pages.read_catalog()
    assert note is None and isinstance(recovered, pages._SqliteCatalog)
    assert recovered.scoped_adjacency("Medical_IT", "C") == {}
    assert pages._CATALOG_CACHE["catalog"] is None


@pytest.mark.parametrize("backend", ["file", "projection"])
def test_lru_eviction_and_reference_budget(isolated_memory, monkeypatch, backend):
    assert pages.SCOPED_ADJACENCY_CACHE_LIMIT == 8
    assert pages.SCOPED_ADJACENCY_REFERENCE_LIMIT == 200_000
    data = {"nodes": {}, "weighted_edges": []}
    for i in range(10):
        for suffix in ("a", "b"):
            data["nodes"][f"{i}{suffix}"] = {"domain": f"D{i}", "topic_cluster": "C"}
        data["weighted_edges"].append({"source": f"{i}a", "target": f"{i}b", "weight": 1})
    catalog = catalog_for(backend, isolated_memory, data)
    tables = [catalog.scoped_adjacency(f"D{i}", "C") for i in range(8)]
    assert len(pages._scoped_adjacencies) == 8
    assert catalog.scoped_adjacency("D0", "c") is tables[0]  # touch LRU
    catalog.scoped_adjacency("D8", "C")
    assert catalog.scoped_adjacency("D1", "C") is not tables[1]
    assert len(pages._scoped_adjacencies) == 8
    pages._invalidate_adjacency()
    monkeypatch.setattr(pages, "SCOPED_ADJACENCY_REFERENCE_LIMIT", 4)
    first = catalog.scoped_adjacency("D0", "C")
    catalog.scoped_adjacency("D1", "C")
    catalog.scoped_adjacency("D2", "C")
    assert len(pages._scoped_adjacencies) == 2
    assert sum(entry[1] for entry in pages._scoped_adjacencies.values()) == 4
    assert catalog.scoped_adjacency("D0", "C") is not first


@pytest.mark.parametrize("backend", ["file", "projection"])
def test_default_oversized_result_is_complete_and_not_cached(isolated_memory, backend):
    data = {"nodes": {key: {"domain": "General", "topic_cluster": "C"} for key in ("a", "b")},
            "weighted_edges": [{"source": "a", "target": "b", "weight": i + 1} for i in range(100001)]}
    catalog = catalog_for(backend, isolated_memory, data)
    first = catalog.scoped_adjacency("General", "C")
    assert sum(map(len, first.values())) == 200002
    assert first["a"][0] == ("b", 1.0) and first["a"][-1] == ("b", 100001.0)
    assert not pages._scoped_adjacencies
    assert catalog.scoped_adjacency("General", "C") is not first
    assert not pages._scoped_adjacencies
    if isinstance(catalog, pages._FileCatalog):
        assert catalog._adjacency is None


@pytest.mark.parametrize("backend", ["file", "projection"])
@pytest.mark.parametrize("invalidate", ["reset_catalog_cache", "_invalidate_adjacency"])
def test_explicit_invalidation_releases_and_rebuilds(isolated_memory, backend, invalidate):
    catalog = catalog_for(backend, isolated_memory, payload())
    old = catalog.scoped_adjacency("Medical_IT", "C")
    getattr(pages, invalidate)()
    assert not pages._scoped_adjacencies
    new = catalog.scoped_adjacency("Medical_IT", "C")
    assert new == old and new is not old


def run_reader(catalog):
    try:
        return catalog.scoped_adjacency("Medical_IT", "C")
    finally:
        db_store.close_connection()


@pytest.mark.parametrize("backend", ["file", "projection"])
def test_concurrent_readers_share_one_build(isolated_memory, monkeypatch, backend):
    catalog = catalog_for(backend, isolated_memory, payload())
    original = catalog._build_scoped_adjacency
    builds = []
    def build(*args):
        builds.append(1)
        return original(*args)
    monkeypatch.setattr(catalog, "_build_scoped_adjacency", build)
    barrier = threading.Barrier(4)
    def reader():
        barrier.wait(5)
        return run_reader(catalog)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(reader) for _ in range(4)]
        tables = [future.result(timeout=10) for future in futures]
    assert len(builds) == 1
    assert all(table is tables[0] for table in tables)


@pytest.mark.parametrize("backend", ["file", "projection"])
def test_invalidation_cannot_be_overtaken_by_inflight_publication(isolated_memory, monkeypatch, backend):
    catalog = catalog_for(backend, isolated_memory, payload())
    original = catalog._build_scoped_adjacency
    entered, release, invalidating, invalidated = (threading.Event() for _ in range(4))
    def build(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(catalog, "_build_scoped_adjacency", build)
    def invalidate():
        invalidating.set()
        pages._invalidate_adjacency()
        invalidated.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(run_reader, catalog)
        try:
            assert entered.wait(5)
            clearing = pool.submit(invalidate)
            assert invalidating.wait(5)
            assert not invalidated.is_set()
        finally:
            release.set()
        assert reader.result(timeout=10)["a"][0] == ("b", 2.0)
        clearing.result(timeout=10)
    assert invalidated.is_set() and not pages._scoped_adjacencies


def test_sqlite_snapshot_does_not_mix_edges_and_scope_generations(isolated_memory, monkeypatch):
    data = payload()
    seed(isolated_memory, data)
    catalog = pages._SqliteCatalog()
    original = catalog._build_scoped_adjacency
    entered, release = threading.Event(), threading.Event()
    def build(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(catalog, "_build_scoped_adjacency", build)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_reader, catalog)
        try:
            assert entered.wait(5)  # read transaction has already read the old state
            # Independent WAL writer, like a separate process (no process hook).
            conn = sqlite3.connect(str(db_store.get_db_path()))
            try:
                conn.execute("UPDATE page_index_nodes SET domain='Private' WHERE node_key='b'")
                conn.execute("UPDATE page_index_edges SET weight=99 WHERE sequence=0")
                conn.execute("UPDATE page_index_state SET updated_at='next-generation',edge_digest='changed'")
                conn.commit()
            finally:
                conn.close()
        finally:
            release.set()
        old = future.result(timeout=10)
    assert old["a"] == (("b", 2.0), ("b", 3.0))
    assert catalog.scoped_adjacency("Medical_IT", "C") == {}


def test_refresh_rolls_back_nodes_edges_and_state_together(isolated_memory, monkeypatch):
    data = payload()
    seed(isolated_memory, data)
    conn = db_store.get_connection()
    before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
              for table in ("page_index_nodes", "page_index_edges", "page_index_state")}
    updated = copy.deepcopy(data)
    updated["nodes"]["b"]["domain"] = "Private"
    updated["weighted_edges"][0]["weight"] = 99
    def fail(*args):
        raise RuntimeError("synthetic state publication failure")
    monkeypatch.setattr(pages, "_write_projection_state", fail)
    with pytest.raises(RuntimeError, match="synthetic state publication"):
        pages.refresh_page_index_projection(updated)
    after = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in before}
    assert after == before and not conn.in_transaction


def test_uncommitted_caller_transaction_cannot_poison_global_cache(isolated_memory):
    data = payload()
    seed(isolated_memory, data)
    catalog = pages._SqliteCatalog()
    old = catalog.scoped_adjacency("Medical_IT", "C")
    conn = db_store.get_connection()
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE page_index_nodes SET domain='Private' WHERE node_key='b'")
        assert catalog.scoped_adjacency("Medical_IT", "C") == {}
    finally:
        conn.rollback()
    assert catalog.scoped_adjacency("Medical_IT", "C") is old


def test_concurrent_prepared_readers_build_the_single_slot_once(isolated_memory, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    barrier, calls = threading.Barrier(4), []
    def prepare(adj):
        calls.append(adj)
        entered.set()
        assert release.wait(5)
        return object()
    monkeypatch.setattr(tool_search, "vector_lake_core", SimpleNamespace(PprIndex=prepare))
    monkeypatch.setattr(tool_search, "_PPR_INDEX", {"generation": None, "adj": None, "index": None})
    table = pages._FileCatalog(payload()).scoped_adjacency("Medical_IT", "C")
    def reader():
        barrier.wait(5)
        return tool_search._ppr_index(table, 7)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(reader) for _ in range(4)]
        try:
            assert entered.wait(5)
        finally:
            release.set()
        indices = [future.result(timeout=10) for future in futures]
    assert len(calls) == 1 and all(index is indices[0] for index in indices)


def test_prepared_index_remains_one_slot_and_does_not_cache_oversized_scopes(isolated_memory, monkeypatch):
    # Boundary contract is exercised even without an installed native wheel.
    calls = []
    def prepare(adj):
        assert isinstance(adj, dict)
        calls.append(adj)
        return object()
    monkeypatch.setattr(tool_search, "vector_lake_core", SimpleNamespace(PprIndex=prepare))
    monkeypatch.setattr(tool_search, "_PPR_INDEX", {"generation": None, "adj": None, "index": None})
    catalog = pages._FileCatalog(payload())
    first = catalog.scoped_adjacency("Medical_IT", "C")
    native = tool_search._ppr_index(first, 7)
    assert tool_search._ppr_index(first, 7) is native and len(calls) == 1
    second = catalog.scoped_adjacency("", "C")
    tool_search._ppr_index(second, 7)
    assert tool_search._PPR_INDEX["adj"] is second and len(calls) == 2
    monkeypatch.setattr(pages, "SCOPED_ADJACENCY_REFERENCE_LIMIT", 1)
    pages._invalidate_adjacency()
    oversized = catalog.scoped_adjacency("Medical_IT", "C")
    tool_search._ppr_index(oversized, 8)
    tool_search._ppr_index(oversized, 8)
    assert tool_search._PPR_INDEX["adj"] is second and len(calls) == 4
