import json
import pytest

from vector_lake import db_store, indexer, periodic_catch_up
from types import SimpleNamespace

from filelock import FileLock

from vector_lake import embedding_scheduler
from vector_lake.embedding_scheduler import (
    embedding_backfill,
    estimate_embedding_tokens,
    stale_embedding_keys,
)
from vector_lake.mutation_coordinator import execute_mutation_plan


@pytest.fixture(autouse=True)
def _sdk_transport(monkeypatch):
    """These tests inject a fake client and assert on its call/response semantics.

    They are about the SDK transport's retry and validation behaviour; the REST transport (the
    default) never touches a client, so pinning the transport here keeps the assertions about the
    path they were written for instead of silently issuing a real request.
    """
    monkeypatch.setenv(embedding_scheduler.EMBEDDING_TRANSPORT_ENV, "sdk")
    embedding_scheduler.reset_client_cache(reopen=True)
    yield
    embedding_scheduler.reset_client_cache(reopen=True)


def _purpose(memory_dir):
    (memory_dir / "purpose.md").write_text(
        """---
purpose_version: "12.0"
intent_keywords: [test]
scope:
  core: [test]
  edge: [edge]
  excluded: [excluded]
  marketing_noise: [noise]
evidence_tiers:
  primary: Primary evidence
sir_registry:
  - id: SIR_TEST
    status: active
    review_after: 2099-01-01
    signal_keywords: [test]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Test purpose.
""",
        encoding="utf-8",
    )


def _source_content(entity_id: str, title: str):
    return f"""---
id: {entity_id}
title: {title}
type: source
domain: General
status: Active
epistemic-status: seed
categories: [System_Architecture]
updated: 2026-07-13T00:00:00+00:00
sources: [raw/test.pdf]
strategic_scope: core
evidence_tier: primary
---
Primary source content.
"""


def test_embedding_backfill_dry_run_counts_missing_vectors(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_RPM", "3000")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_TPM", "1000000")
    db_store.init_db()
    index_data = {
        "nodes": {
            "Concept_A": {"title": "A", "summary": "短文本", "raw_text": "alpha"},
            "Concept_B": {"title": "B", "summary": "另一个短文本", "raw_text": "beta"},
        }
    }
    db_store.upsert_embedding("Concept_A", [1.0] * 3072)

    result = embedding_backfill(index_data, dry_run=True)

    assert result["candidates"] == 1
    assert result["coverage_before"]["embedded"] == 1
    assert result["coverage_before"]["missing"] == 1
    assert result["effective_rpm"] == 2400
    assert result["effective_tpm"] == 800000


def test_generate_index_preserves_existing_embeddings_when_compute_returns_empty(isolated_memory, monkeypatch):
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Existing.md", content=_source_content("source_existing", "Existing Source"))
    db_store.upsert_embedding("Source_Existing", [1.0] * 3072)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: (_ for _ in ()).throw(AssertionError("index rebuild called embedding API")),
    )

    indexer.generate_index()

    conn = db_store.get_connection()
    count = conn.execute("SELECT COUNT(*) FROM vec_embeddings WHERE page_key = 'Source_Existing'").fetchone()[0]
    assert count == 1


def test_token_estimator_is_conservative_for_cjk_and_latin():
    assert estimate_embedding_tokens("医疗AI agent memory") >= 4


def test_provider_contents_keep_texts_as_separate_requests():
    contents = embedding_scheduler._provider_contents(["first", "second"])

    assert len(contents) == 2
    assert contents[0].parts[0].text == "first"
    assert contents[1].parts[0].text == "second"


def test_embedding_backfill_rejects_partial_provider_response(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", "1")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda _seconds: None)

    class FakeModels:
        def embed_content(self, **_kwargs):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] * 3072)])

    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: SimpleNamespace(models=FakeModels(), close=lambda: None),
    )
    result = embedding_backfill(
        {
            "nodes": {
                "Concept_A": {"title": "A"},
                "Concept_B": {"title": "B"},
            }
        },
        dry_run=False,
    )

    assert result["embedded"] == 0
    assert result["failed_batches"] == 1
    assert "count mismatch" in result["last_error"]
    assert result["stopped"] == "consecutive batch failure guard reached"
    run = db_store.get_connection().execute(
        "SELECT status, processed, failed_batches FROM embedding_runs WHERE run_id = ?",
        (result["run_id"],),
    ).fetchone()
    assert dict(run) == {"status": "failed", "processed": 0, "failed_batches": 1}


def test_embedding_backfill_rejects_wrong_dimension(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", "1")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda _seconds: None)

    class FakeModels:
        def embed_content(self, **_kwargs):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] * 8)])

    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: SimpleNamespace(models=FakeModels(), close=lambda: None),
    )
    result = embedding_backfill(
        {"nodes": {"Concept_A": {"title": "A"}}},
        dry_run=False,
    )

    assert result["embedded"] == 0
    assert "dimension mismatch" in result["last_error"]


def test_embedding_backfill_has_single_writer_lock(isolated_memory, monkeypatch):
    db_store.init_db()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    lock = FileLock(str(isolated_memory / "wiki" / ".meta" / ".embedding-backfill.lock"))
    with lock:
        result = embedding_backfill(
            {"nodes": {"Concept_A": {"title": "A"}}},
            dry_run=False,
        )

    assert result["embedded"] == 0
    assert result["skipped"] == "another embedding backfill is already running"


def test_rate_limiter_uses_shared_sqlite_window(isolated_memory, monkeypatch):
    db_store.init_db()
    config = embedding_scheduler.EmbeddingRateConfig(rpm=1, tpm=100, utilization=1.0)
    limiter_a = embedding_scheduler.MinuteRateLimiter(config)
    limiter_b = embedding_scheduler.MinuteRateLimiter(config)
    clock = iter([1000.0, 1000.0, 1060.1])
    sleeps = []
    monkeypatch.setattr(embedding_scheduler.time, "time", lambda: next(clock))
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda seconds: sleeps.append(seconds))

    limiter_a.reserve(10)
    limiter_b.reserve(10)

    assert len(sleeps) == 1
    assert sleeps[0] >= 60.0
    rows = db_store.get_connection().execute(
        "SELECT reserved_at, token_count FROM embedding_rate_reservations ORDER BY reserved_at"
    ).fetchall()
    assert [(row["reserved_at"], row["token_count"]) for row in rows] == [(1060.1, 10)]


def _fake_provider(monkeypatch, calls: list | None = None) -> list:
    """Replace the provider with a recorder and return the call log.

    Recording rather than raising is deliberate.  ``embedding_backfill`` catches ``Exception``
    around each batch on purpose -- one bad batch must not lose the run -- and that also swallows
    an ``AssertionError`` raised from inside it, so a test that leans on the raise passes whether
    or not the call ever happened.
    """
    calls = calls if calls is not None else []

    def _record(client, contents, tokens, config, limiter, **kwargs):
        calls.append(list(contents))
        return [[1.0] * config.dimension for _ in contents]

    monkeypatch.setattr(embedding_scheduler, "_request_embeddings", _record)
    return calls


def _embed_index(isolated_memory) -> dict:
    return json.loads((isolated_memory / "wiki" / "index.json").read_text(encoding="utf-8"))


def _backfill_now(isolated_memory) -> dict:
    """Embed the corpus the way the CLI does, so the stamped digest covers the canonical input.

    Passing no bodies would stamp a body-less digest, and the sweep -- which always reads bodies --
    would then correctly call the row stale.  That is a fixture bug, not a product one, and the
    assertion below the call is what keeps the two paths from drifting apart unnoticed.
    """
    from vector_lake.embedding_scheduler import page_bodies_for_keys

    index_data = _embed_index(isolated_memory)
    bodies = page_bodies_for_keys(list(index_data["nodes"]))
    result = embedding_backfill(index_data, dry_run=False, bodies=bodies)
    assert stale_embedding_keys(index_data, bodies) == set(), (
        "the CLI path and the sweep path disagree about the input digest"
    )
    return result


def _stored_vector_count(node_key: str) -> int:
    return db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE page_key = ?", (node_key,)
    ).fetchone()[0]


def test_incremental_index_invalidates_stale_vector_without_api(isolated_memory, monkeypatch):
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Changed.md", content=_source_content("source_changed", "Old Title"))
    indexer.generate_index()
    db_store.upsert_embedding("Source_Changed", [1.0] * 3072)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: (_ for _ in ()).throw(AssertionError("incremental index called embedding API")),
    )
    execute_mutation_plan("Source_Changed.md", content=_source_content("source_changed", "New Title"))

    indexer.update_index_items(["Source_Changed.md"])

    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embeddings WHERE page_key = 'Source_Changed'"
    ).fetchone()[0] == 0


def test_the_periodic_sweep_restores_a_vector_the_incremental_index_dropped(isolated_memory, monkeypatch):
    """The counterpart the contract test above *assumed* existed.

    Measured on the live lake 2026-09-21: 4 573 of 7 175 nodes had no vector, and all 4 573
    were pages rewritten on or after 2026-09-16 -- i.e. every page the incremental path had
    touched.  ``embedding_backfill`` existed the whole time, but as a manual CLI/MCP call, so
    the projection only ever shrank.
    """
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Changed.md", content=_source_content("source_changed", "Old Title"))
    indexer.generate_index()
    db_store.upsert_embedding("Source_Changed", [1.0] * 3072)
    execute_mutation_plan("Source_Changed.md", content=_source_content("source_changed", "New Title"))
    indexer.update_index_items(["Source_Changed.md"])
    assert _stored_vector_count("Source_Changed") == 0

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    requested: list[list[str]] = []

    def _record(client, contents, tokens, config, limiter, **kwargs):
        requested.append(list(contents))
        return [[1.0] * config.dimension for _ in contents]

    monkeypatch.setattr(embedding_scheduler, "_request_embeddings", _record)

    summary = periodic_catch_up.catch_up_once()

    assert summary["embeddings"]["embedded"] == 1, summary["embeddings"]
    assert _stored_vector_count("Source_Changed") == 1
    assert requested, "the sweep never asked the provider for a vector"
    # Rebuilt from the rewritten page, not resurrected: the title is the one just written.
    assert any("New Title" in text for text in requested[0]), requested[0]


def test_the_sweep_does_not_call_the_provider_when_the_projection_is_current(isolated_memory, monkeypatch):
    """A 15-minute loop must be free when every vector is present and verified."""
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Only.md", content=_source_content("source_only", "Only Page"))
    indexer.generate_index()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = _fake_provider(monkeypatch)
    # Establish the ledger the way production does.  A hand-written vector is unstamped and
    # correctly reads as unverified, so stamping it by hand would test nothing.
    assert _backfill_now(isolated_memory)["embedded"] == 1
    calls.clear()

    summary = periodic_catch_up.catch_up_once()

    assert calls == [], "the sweep embedded a projection that was already current"
    assert summary["embeddings"]["embedded"] == 0
    assert summary["embeddings"]["stale_inputs"] == 0
    assert summary["embeddings"]["coverage_after"]["missing"] == 0


def test_the_sweep_rebuilds_a_vector_whose_input_changed(isolated_memory, monkeypatch):
    """The class only a full re-embed could find: the vector is present, and stale.

    Nothing deletes a vector when a full index rebuild changes the aliases and summary that the
    embedding text is built from, so the row survives and keeps answering from text that no
    longer exists.  Measured on the live lake 2026-09-21 by re-embedding all 7 175 nodes and
    diffing bit for bit: 7 172 identical, 3 changed -- and the provider is bit-deterministic, so
    those 3 were stale inputs rather than noise.
    """
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Drift.md", content=_source_content("source_drift", "Original Title"))
    indexer.generate_index()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = _fake_provider(monkeypatch)
    assert _backfill_now(isolated_memory)["embedded"] == 1
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embedding_inputs WHERE node_key = 'Source_Drift'"
    ).fetchone()[0] == 1
    calls.clear()

    # A rebuild rewrites the node's summary and leaves the vector alone -- exactly the live shape,
    # where the input moved under a vector that stayed put.
    path = isolated_memory / "wiki" / "index.json"
    index_data = json.loads(path.read_text(encoding="utf-8"))
    index_data["nodes"]["Source_Drift"]["summary"] = "Rewritten after the last embed."
    path.write_text(json.dumps(index_data, ensure_ascii=False), encoding="utf-8")
    assert _stored_vector_count("Source_Drift") == 1, "must stay present to be the stale case"

    summary = periodic_catch_up.catch_up_once()

    assert summary["embeddings"]["stale_inputs"] == 1, summary["embeddings"]
    assert summary["embeddings"]["embedded"] == 1, summary["embeddings"]
    assert calls, "the sweep left a stale vector in place"
    assert any("Rewritten after the last embed" in text for text in calls[0]), calls[0]


def test_an_unstamped_vector_is_rebuilt_to_establish_its_digest(isolated_memory, monkeypatch):
    """No ledger row is no evidence of currency, so the sweep rebuilds instead of assuming."""
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Legacy.md", content=_source_content("source_legacy", "Legacy"))
    indexer.generate_index()
    db_store.upsert_embedding("Source_Legacy", [1.0] * 3072)  # a vector from before the ledger
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _fake_provider(monkeypatch)

    summary = periodic_catch_up.catch_up_once()

    assert summary["embeddings"]["embedded"] == 1, summary["embeddings"]
    assert db_store.get_connection().execute(
        "SELECT COUNT(*) FROM vec_embedding_inputs WHERE node_key = 'Source_Legacy'"
    ).fetchone()[0] == 1


def test_deleting_a_vector_also_drops_its_input_digest(isolated_memory, monkeypatch):
    """A ledger row that outlives its vector would make every later orphan read as stale."""
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Clean.md", content=_source_content("source_clean", "Clean"))
    indexer.generate_index()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _fake_provider(monkeypatch)
    _backfill_now(isolated_memory)
    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM vec_embedding_inputs").fetchone()[0] == 1

    db_store.delete_embedding("Source_Clean")

    assert conn.execute("SELECT COUNT(*) FROM vec_embedding_inputs").fetchone()[0] == 0


def test_the_sweep_bounds_how_long_one_batch_may_wait(isolated_memory, monkeypatch):
    """A quota error sleeps a flat 60 s per retry, so an unbounded batch can eat the interval."""
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Budget.md", content=_source_content("source_budget", "Budgeted"))
    indexer.generate_index()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_LAKE_CATCHUP_EMBEDDING_BUDGET_SECONDS", "7.5")
    seen: dict = {}

    def _record(client, contents, tokens, config, limiter, **kwargs):
        seen.update(kwargs)
        return [[1.0] * config.dimension for _ in contents]

    monkeypatch.setattr(embedding_scheduler, "_request_embeddings", _record)

    summary = periodic_catch_up.catch_up_once()

    assert summary["embeddings"]["embedded"] == 1, summary["embeddings"]
    assert seen.get("budget_seconds") == 7.5, seen


def test_the_backfill_stamps_which_model_produced_the_stored_vectors(isolated_memory, monkeypatch):
    """``embedding_runs`` records the model per run, so it cannot answer this per projection.

    A changed ``VECTOR_LAKE_EMBEDDING_MODEL`` moves the query encoder while the stored vectors
    stay put: every similarity becomes meaningless with no visible symptom.  The marker is what
    gives doctor something to compare against.
    """
    _purpose(isolated_memory)
    execute_mutation_plan("Source_Stamped.md", content=_source_content("source_stamped", "Stamped"))
    indexer.generate_index()
    assert db_store.embedding_projection_state() == {}

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv(embedding_scheduler.EMBEDDING_TRANSPORT_ENV, "sdk")
    monkeypatch.setattr(
        embedding_scheduler,
        "_create_client",
        lambda: SimpleNamespace(
            models=SimpleNamespace(
                embed_content=lambda model, contents, config=None: SimpleNamespace(
                    embeddings=[SimpleNamespace(values=[1.0] * 3072) for _ in contents]
                )
            ),
            close=lambda: None,
        ),
    )

    result = embedding_backfill(
        json.loads((isolated_memory / "wiki" / "index.json").read_text(encoding="utf-8")),
        dry_run=False,
    )

    assert result["embedded"] == 1, result
    state = db_store.embedding_projection_state()
    assert state["model"] == embedding_scheduler.DEFAULT_MODEL, state
    assert state["dimension"] == 3072, state
    assert state["written_at"], state


def test_a_database_without_the_projection_marker_is_not_reported_complete(isolated_memory):
    """The sentinel is what makes ``init_db`` still run its DDL on an existing database.

    ``_schema_is_complete`` short-circuits ``_init_db_once``, so a new table that is not a
    sentinel is never created on the databases that already hold vectors -- which is exactly
    where the model and staleness questions matter.  A sentinel that is present must also stop
    being the thing that forces a DDL transaction on every read, hence the True/False pair.
    """
    assert "vec_embedding_projection" in db_store._SCHEMA_SENTINELS
    assert "vec_embedding_inputs" in db_store._SCHEMA_SENTINELS
    db_store.init_db()
    db_path = db_store.get_db_path()
    assert db_store._schema_is_complete(db_path) is True

    conn = db_store.get_connection()
    for table in ("vec_embedding_projection", "vec_embedding_inputs"):
        with db_store.transaction():
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        assert db_store._schema_is_complete(db_path) is False


def test_start_embedding_run_marks_crashed_run_abandoned(isolated_memory, monkeypatch):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute(
            "INSERT INTO embedding_runs "
            "(run_id, status, model, candidates, processed, failed_batches, started_at, updated_at) "
            "VALUES ('stale-run', 'running', 'model', 10, 2, 0, '2000-01-01', '2000-01-01')"
        )
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_RUN_STALE_SECONDS", "60")

    db_store.start_embedding_run("new-run", "model", 1)

    rows = {
        row["run_id"]: row["status"]
        for row in conn.execute("SELECT run_id, status FROM embedding_runs")
    }
    assert rows == {"stale-run": "abandoned", "new-run": "running"}


# --- caller-supplied budget -------------------------------------------------
#
# A quota error used to sleep a flat 60 s per retry, which exceeds the MCP
# client's 60 s call ceiling on its own.  These pin the deadline check that turns
# an unaffordable wait into an explicit, immediate failure.


class _StubLimiter:
    def __init__(self, wait_seconds=0.0):
        self.wait_seconds = wait_seconds
        self.calls = 0
        self.deadlines = []
        self.durable = []

    def reserve(self, request_tokens, deadline=None, durable=True):
        self.calls += 1
        self.deadlines.append(deadline)
        self.durable.append(durable)
        if self.wait_seconds:
            raise embedding_scheduler.EmbeddingBudgetExceeded(
                f"rate-limit window would hold this request for {self.wait_seconds}s"
            )


class _QuotaClient:
    """Raises the provider's quota error on every call."""

    def __init__(self):
        self.models = SimpleNamespace(embed_content=self._embed)
        self.calls = 0

    def _embed(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")


def test_quota_retry_inside_a_budget_fails_fast(monkeypatch):
    slept = []
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda seconds: slept.append(seconds))
    client = _QuotaClient()

    with pytest.raises(embedding_scheduler.EmbeddingBudgetExceeded) as caught:
        embedding_scheduler._request_embeddings(
            client,
            ["梅奥"],
            10,
            embedding_scheduler.load_embedding_rate_config(),
            _StubLimiter(),
            budget_seconds=1.0,
        )

    assert slept == []  # the 60 s quota sleep was refused, not taken
    assert client.calls >= 1
    assert "budget remains" in str(caught.value)


def test_quota_retry_will_wait_when_no_budget_was_given(monkeypatch):
    """Batch callers (backfill) must keep the patient behaviour."""
    slept = []
    monkeypatch.setattr(embedding_scheduler.time, "sleep", lambda seconds: slept.append(seconds))
    config = embedding_scheduler.load_embedding_rate_config()

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        embedding_scheduler._request_embeddings(
            _QuotaClient(), ["梅奥"], 10, config, _StubLimiter(), budget_seconds=None
        )

    assert slept == [60.0] * config.max_retries


def test_rate_limiter_refuses_an_unaffordable_window_wait(isolated_memory, monkeypatch):
    """A full rolling window must not be waited out past the caller's deadline."""
    db_store.init_db()
    monkeypatch.setattr(
        embedding_scheduler.time, "sleep", lambda seconds: pytest.fail("slept past the budget")
    )
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)
    now = embedding_scheduler.time.time()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(config.effective_rpm):
            conn.execute(
                "INSERT INTO embedding_rate_reservations (reservation_id, reserved_at, token_count) "
                "VALUES (?, ?, ?)",
                (f"res_{index}", now, 1),
            )

    with pytest.raises(embedding_scheduler.EmbeddingBudgetExceeded):
        limiter.reserve(1, deadline=embedding_scheduler.time.monotonic())


def test_rate_limiter_still_blocks_without_a_deadline(isolated_memory, monkeypatch):
    """No deadline means the limiter waits, as the batch paths require."""
    db_store.init_db()
    slept = []
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)
    now = embedding_scheduler.time.time()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(config.effective_rpm):
            conn.execute(
                "INSERT INTO embedding_rate_reservations (reservation_id, reserved_at, token_count) "
                "VALUES (?, ?, ?)",
                (f"res_{index}", now, 1),
            )

    def _stop_after_one_wait(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(embedding_scheduler.time, "sleep", _stop_after_one_wait)
    with pytest.raises(KeyboardInterrupt):
        limiter.reserve(1)
    assert slept and slept[0] > 59.0


def test_interactive_reservation_does_not_take_the_write_lock(isolated_memory, monkeypatch):
    """A query embedding must not put the write lock back on the read path."""
    db_store.init_db()
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)

    def _forbidden(*_a, **_k):
        raise AssertionError("an interactive reservation attempted the write lock")

    monkeypatch.setattr(db_store, "_acquire_write_lock", _forbidden)

    limiter.reserve(10, durable=False)  # must not raise

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM embedding_rate_reservations").fetchone()[0] == 0


def test_batch_reservation_still_records_durably(isolated_memory):
    db_store.init_db()
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)

    limiter.reserve(10)
    limiter.reserve(10, durable=False)

    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM embedding_rate_reservations").fetchone()[0] == 1


def test_interactive_reservation_still_refuses_a_saturated_window(isolated_memory, monkeypatch):
    """Read-only does not mean unguarded: a full durable window still degrades."""
    db_store.init_db()
    config = embedding_scheduler.load_embedding_rate_config()
    limiter = embedding_scheduler.MinuteRateLimiter(config)
    now = embedding_scheduler.time.time()
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(config.effective_rpm):
            conn.execute(
                "INSERT INTO embedding_rate_reservations (reservation_id, reserved_at, token_count) "
                "VALUES (?, ?, ?)",
                (f"res_{index}", now, 1),
            )
    monkeypatch.setattr(
        embedding_scheduler.time, "sleep", lambda seconds: pytest.fail("slept past the budget")
    )

    with pytest.raises(embedding_scheduler.EmbeddingBudgetExceeded):
        limiter.reserve(1, deadline=embedding_scheduler.time.monotonic(), durable=False)
