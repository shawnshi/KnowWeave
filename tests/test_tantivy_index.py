"""The tantivy FTS backend: switch-gated, contract-compatible with the FTS5 path.

Every assertion here is about the *contract* ``db_store`` promises its callers, because that is
what a backend swap can silently break:

* default stays FTS5 -- nothing changes until an operator asks;
* the returned rows keep ``{node_key, title, summary, rank}`` with FTS5's **negative** rank
  (ascending = best first), which ``tool_search`` depends on when it does ``raw_score * -1.0``;
* terms are ANDed, each matching across title/summary/text;
* delete / clear / rebuild keep the mirror and the authoritative FTS5 tables in step.

No model, no network: text is passed pre-tokenized the way the indexer passes it.
"""
from __future__ import annotations

import pytest

from vector_lake import db_store, tantivy_index, tokenizer

pytest.importorskip("tantivy", reason="the tantivy wheel is not installed on this host")

DOCS = [
    ("Concept_信创医院", "信创 医院 HIS", "信创 集成平台 解耦", "信创 医院 HIS 集成平台 解耦 架构"),
    ("Concept_电子病历", "电子病历 六级 评级", "电子病历 评级 评审", "电子病历 六级 评级 评审 材料"),
    ("Concept_DRG医保", "DRG 医保 支付", "DRG DIP 合规", "DRG 医保 DIP 支付 合规 运营"),
]


def _seed(rows=DOCS):
    """Write the fixtures the way the indexer does: text already tokenized by jieba-rs.

    Feeding raw prose here would test an index nobody builds -- jieba segments ``解耦`` into
    ``解`` + ``耦`` and ``电子病历`` into ``电子`` + ``病历``, so an untokenized fixture only ever
    matches a whole-word query, which is the opposite of what ``search_wiki`` sends.
    """
    for key, title, summary, text in rows:
        db_store.upsert_search_index(
            key,
            tokenizer.tokenize_joined(title),
            tokenizer.tokenize_joined(summary),
            tokenizer.tokenize_joined(text),
        )


@pytest.fixture
def mirror(isolated_memory, monkeypatch):
    """A hermetic MEMORY root with the tantivy backend switched on."""
    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    tantivy_index.reset()  # drop handles cached against a previous test's root
    yield
    tantivy_index.reset()


def test_default_backend_is_fts5(isolated_memory, monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_FTS", raising=False)
    assert tantivy_index.enabled() is False
    assert tantivy_index.stats()["exists"] is False


def test_switch_is_honoured(mirror):
    assert tantivy_index.enabled() is True


def _drain():
    for _ in range(32):
        result = tantivy_index.replay_pending()
        assert result["commits"] <= 1
        if result["status"] == "ready":
            return result
    pytest.fail("fixture replay did not converge")


@pytest.mark.parametrize("caught_up", [False, True])
def test_upsert_then_search_keeps_the_row_shape_and_the_rank_sign(mirror, caught_up):
    _seed()
    if caught_up:
        _drain()

    rows = db_store.search_wiki("信创 医院", limit=10)

    assert [row["node_key"] for row in rows] == ["Concept_信创医院"]
    assert set(rows[0]) == {"node_key", "title", "summary", "rank"}
    assert rows[0]["rank"] < 0, "FTS5's bm25() is negative and tool_search negates it"


@pytest.mark.parametrize("caught_up", [False, True])
def test_terms_are_anded_across_fields(mirror, caught_up):
    _seed()
    if caught_up:
        _drain()

    # '信创' is in the title, '解耦' only in the summary: both must be required.  The query goes
    # through the project tokenizer, so '解耦' arrives as '解' + '耦'.
    assert [row["node_key"] for row in db_store.search_wiki("信创 解耦", limit=10)] == [
        "Concept_信创医院"
    ]
    # A term nobody carries must empty the result rather than widen it.
    assert db_store.search_wiki("信创 不存在的词", limit=10) == []


@pytest.mark.parametrize("caught_up", [False, True])
def test_best_first_ordering_is_ascending_by_rank(mirror, caught_up):
    _seed()
    if caught_up:
        _drain()

    rows = db_store.search_wiki("DRG 医保", limit=10)
    ranks = [row["rank"] for row in rows]
    assert ranks == sorted(ranks), "ascending rank is best-first, as ORDER BY rank was"


def test_delete_and_clear_keep_the_mirror_in_step(mirror):
    _seed()
    assert db_store.search_index_keys() == {key for key, *_ in DOCS}

    db_store.delete_search_index("Concept_DRG医保")
    assert "Concept_DRG医保" not in db_store.search_index_keys()
    assert db_store.search_wiki("DRG 医保", limit=10) == []

    db_store.clear_search_index()
    assert db_store.search_index_keys() == set()
    assert db_store.search_wiki("信创 医院", limit=10) == []


def test_rebuild_from_the_authoritative_fts_table(mirror):
    """The migration/recovery path: seed FTS5 only, then rebuild the mirror from it."""
    _seed()
    db_store.clear_search_index()  # durable native clear is replayed later
    conn = db_store.get_connection()
    for key, title, summary, text in DOCS:
        conn.execute(
            "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
            (
                key,
                tokenizer.tokenize_joined(title),
                tokenizer.tokenize_joined(summary),
                tokenizer.tokenize_joined(text),
            ),
        )
    conn.commit()
    tantivy_index.reset()

    assert tantivy_index.rebuild_from_sqlite() == len(DOCS)
    assert [row["node_key"] for row in db_store.search_wiki("电子病历 六级", limit=10)] == [
        "Concept_电子病历"
    ]


def test_a_mirror_failure_never_loses_the_authoritative_write(mirror, monkeypatch):
    """Fail-open: the FTS5 row is the write; the mirror is an optimisation."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("mirror down")

    monkeypatch.setattr(tantivy_index, "upsert", boom)
    db_store.upsert_search_index("Concept_A", "标题", "摘要", "文本")
    assert db_store.tantivy_mirror_state()["pending"] == 1
    assert not tantivy_index.stats()["exists"], "producer must not create a native mirror"

    conn = db_store.get_connection()
    row = conn.execute(
        "SELECT node_key FROM wiki_search_index WHERE node_key = 'Concept_A'"
    ).fetchone()
    assert row is not None, "the authoritative projection must still have the node"


@pytest.mark.parametrize("caller", ["wiki", "hybrid"])
@pytest.mark.parametrize("condition", ["dirty", "ready", "missing", "marker", "disabled", "unavailable"])
def test_production_reads_use_verified_mirror_or_sql(mirror, monkeypatch, caller, condition):
    from vector_lake import tool_search

    db_store.upsert_search_index("a", "sharedword", "", "sharedword")
    if condition != "dirty":
        _drain()
    if condition == "missing":
        tantivy_index.reset()
        import shutil
        shutil.rmtree(tantivy_index.index_dir())
    elif condition == "marker":
        tantivy_index._incarnation_file().write_text("invalid", encoding="utf-8")
    elif condition == "disabled":
        monkeypatch.setenv("VECTOR_LAKE_FTS", "fts5")
    elif condition == "unavailable":
        monkeypatch.setattr(tantivy_index, "enabled", lambda: False)
    tool_search._LAST_FTS_ERROR.backends = set()
    fetch = db_store.search_wiki if caller == "wiki" else tool_search._get_fts_search_results
    rows = fetch("sharedword")
    assert [row["node_key"] for row in rows] == ["a"]
    if caller == "hybrid":
        assert tool_search._fts_backend_name() == ("tantivy" if condition == "ready" else "fts5")
        assert tool_search._LAST_FTS_ERROR.msg is None
    assert db_store.search_index_keys() == {"a"}


@pytest.mark.parametrize("caller", ["wiki", "hybrid"])
def test_metadata_final_token_rejects_fast_catchup(mirror, monkeypatch, caller):
    from vector_lake import tool_search

    db_store.upsert_search_index("a", "oldword", "", "oldword")
    _drain()
    original = tantivy_index.current_token
    calls = []

    def raced_token():
        calls.append(1)
        if len(calls) == 4:  # caller final token, after native gate and SQLite metadata
            db_store.upsert_search_index("a", "newword", "", "newword")
            _drain()
        return original()

    monkeypatch.setattr(tantivy_index, "current_token", raced_token)
    tool_search._LAST_FTS_ERROR.backends = set()
    fetch = db_store.search_wiki if caller == "wiki" else tool_search._get_fts_search_results
    assert fetch("oldword") == []
    assert len(calls) == 4, "race must happen after metadata and must not retry native"
    if caller == "hybrid":
        assert tool_search._fts_backend_name() == "fts5"


@pytest.mark.parametrize("caller", ["wiki", "hybrid"])
def test_verified_empty_is_not_sql_fallback(mirror, caller):
    from vector_lake import tool_search

    _drain()
    statements = []
    db_store.get_connection().set_trace_callback(statements.append)
    try:
        fetch = db_store.search_wiki if caller == "wiki" else tool_search._get_fts_search_results
        assert fetch("missingword") == []
        assert not any(" MATCH " in sql for sql in statements)
    finally:
        db_store.get_connection().set_trace_callback(None)


@pytest.mark.parametrize("caller", ["wiki", "hybrid"])
@pytest.mark.parametrize("begin", ["BEGIN", "SAVEPOINT caller"])
def test_production_reads_preserve_caller_transaction(mirror, monkeypatch, caller, begin):
    from vector_lake import tool_search

    db_store.upsert_search_index("a", "oldword", "", "oldword")
    _drain()
    conn = db_store.get_connection()
    conn.execute(begin)
    try:
        db_store.delete_search_index("a")
        db_store.upsert_search_index("b", "newword", "", "newword")
        monkeypatch.setattr(tantivy_index, "search_if_current", lambda *a, **k: pytest.fail("transaction must use SQL"))
        fetch = db_store.search_wiki if caller == "wiki" else tool_search._get_fts_search_results
        assert fetch("oldword") == []
        assert [row["node_key"] for row in fetch("newword")] == ["b"]
        assert conn.in_transaction
    finally:
        conn.rollback()
    assert db_store.search_index_keys() == {"a"}


@pytest.mark.parametrize("caller", ["wiki", "hybrid"])
def test_native_fault_is_logged_then_sql_serves(mirror, monkeypatch, caplog, caller):
    from vector_lake import tool_search

    db_store.upsert_search_index("a", "sharedword", "", "sharedword")
    _drain()

    def corrupt(*args, **kwargs):
        raise ValueError("fixture native corruption")

    monkeypatch.setattr(tantivy_index, "search_if_current", corrupt)
    tool_search._LAST_FTS_ERROR.backends = set()
    fetch = db_store.search_wiki if caller == "wiki" else tool_search._get_fts_search_results
    assert [row["node_key"] for row in fetch("sharedword")] == ["a"]
    # ``safe_error`` returns the exception type and, for sqlite, a numeric code -- never the
    # message, because that text can carry caller data (see retrieval_runtime.safe_error).  The
    # assertion demanded the message, so it asserted a behaviour the helper deliberately does not
    # have.
    assert "ValueError" in caplog.text
    if caller == "hybrid":
        assert tool_search._fts_backend_name() == "fts5"
        assert tool_search._LAST_FTS_ERROR.msg is None
