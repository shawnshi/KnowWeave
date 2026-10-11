import json

import pytest

from vector_lake import db_store, indexer, mutation_coordinator
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.watchdog_app import WikiIndexHandler, index_queue, process_mutation_outbox_batch

from tests.test_mutation_coordinator import _source_content, _write_purpose_contract


def test_worker_recovers_projection_without_signal(isolated_memory):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Test.md", content=_source_content())
    target = isolated_memory / "wiki" / "Source_Test.md"
    target.unlink()

    stats = process_mutation_outbox_batch(limit=10)

    assert stats == {"claimed": 1, "completed": 1, "retrying": 0, "failed": 0, "superseded": 0, "lease_lost": 0}
    assert target.read_text(encoding="utf-8") == _source_content()
    row = db_store.get_connection().execute("SELECT status FROM mutation_outbox").fetchone()
    assert row["status"] == "completed"
    index_data = json.loads((isolated_memory / "wiki" / "index.json").read_text(encoding="utf-8"))
    assert "Source_Test" in index_data["nodes"]


def test_worker_continues_after_one_row_fails(isolated_memory):
    db_store.init_db()
    first_id = db_store.enqueue_mutation("Concept_Missing.md", "update", payload_text=None)
    second_id = db_store.enqueue_mutation("Concept_Delete.md", "delete")

    stats = process_mutation_outbox_batch(limit=10, max_attempts=3, backoff_base=0)

    assert stats == {"claimed": 2, "completed": 1, "retrying": 1, "failed": 0, "superseded": 0, "lease_lost": 0}
    rows = {
        row["id"]: row["status"]
        for row in db_store.get_connection().execute("SELECT id, status FROM mutation_outbox")
    }
    assert rows[first_id] == "pending"
    assert rows[second_id] == "completed"


def test_worker_batches_index_update_once_for_all_ready_rows(isolated_memory, monkeypatch):
    db_store.init_db()
    db_store.enqueue_mutation("Concept_First.md", "delete")
    db_store.enqueue_mutation("Concept_Second.md", "delete")
    calls = []
    monkeypatch.setattr(indexer, "update_index_items", lambda filenames: calls.append(list(filenames)))

    stats = process_mutation_outbox_batch(limit=10)

    assert stats == {"claimed": 2, "completed": 2, "retrying": 0, "failed": 0, "superseded": 0, "lease_lost": 0}
    assert calls == [["Concept_First.md", "Concept_Second.md"]]


def test_worker_skips_duplicate_markdown_projection(isolated_memory, monkeypatch):
    _write_purpose_contract(isolated_memory)
    execute_mutation_plan("Source_Test.md", content=_source_content())
    monkeypatch.setattr(
        mutation_coordinator,
        "materialize_markdown_projection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("projection should be reused")),
    )
    monkeypatch.setattr(indexer, "update_index_items", lambda filenames: None)

    stats = process_mutation_outbox_batch(limit=10)

    assert stats["completed"] == 1


def test_watchdog_ignores_managed_projection_event(isolated_memory):
    _write_purpose_contract(isolated_memory)
    while not index_queue.empty():
        index_queue.get_nowait()
        index_queue.task_done()
    execute_mutation_plan("Source_Test.md", content=_source_content())
    target = isolated_memory / "wiki" / "Source_Test.md"

    WikiIndexHandler().queue_path(str(target))

    assert index_queue.empty()


@pytest.mark.parametrize("failure", ["markdown", "lexical", "none"])
def test_existing_worker_lexical_fairness_and_connection_cleanup(isolated_memory, monkeypatch, failure):
    from vector_lake import tantivy_index, watchdog_app

    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    db_store.init_db()
    db_store.upsert_search_index("a", "sharedword", "", "sharedword")
    tantivy_index.reset()
    calls = {"markdown": 0, "lexical": 0, "close": 0, "sleep": 0}
    statuses = []
    replay = tantivy_index.replay_pending
    close = db_store.close_connection

    def lexical(**kwargs):
        calls["lexical"] += 1
        assert kwargs == {"max_rows": 100, "max_bytes": 4194304, "max_attempts": 3}
        if failure == "lexical":
            raise ValueError("fixture lexical fault")
        return replay(**kwargs)

    def markdown(**kwargs):
        calls["markdown"] += 1
        if failure == "markdown":
            raise RuntimeError("fixture Markdown fault")
        return {"claimed": 0}

    def cleanup():
        calls["close"] += 1
        close()

    def sleep(seconds):
        calls["sleep"] += 1
        if calls["sleep"] == 2:
            raise SystemExit("fixture stop after two ticks")

    monkeypatch.setattr(tantivy_index, "replay_pending", lexical)
    monkeypatch.setattr(watchdog_app, "process_mutation_outbox_batch", markdown)
    monkeypatch.setattr(watchdog_app, "heal_page_index_projection", lambda: None)
    monkeypatch.setattr(watchdog_app, "write_status", lambda *args, **kwargs: statuses.append((args, kwargs)))
    monkeypatch.setattr(watchdog_app.time, "sleep", sleep)
    monkeypatch.setattr(db_store, "close_connection", cleanup)
    try:
        with pytest.raises(SystemExit, match="fixture stop"):
            watchdog_app.index_worker_loop()
        assert calls == {"markdown": 2, "lexical": 2, "close": 2, "sleep": 2}
        lexical_status = [args for args, kw in statuses if kw.get("component") == "tantivy"]
        assert len(lexical_status) == 2
        if failure == "lexical":
            assert all(args[0] == "error" and "ValueError" in args[4] for args in lexical_status)
        else:
            assert db_store.tantivy_mirror_state()["phase"] == "replay"
            assert db_store.tantivy_mirror_state()["pending"] == 1
            assert all(args[0] == "processing" for args in lexical_status)
    finally:
        tantivy_index.reset()


def test_worker_reports_blocked_without_hot_retry(isolated_memory, monkeypatch):
    from vector_lake import tantivy_index, watchdog_app
    from vector_lake.watchdog_status import get_status_file

    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    tantivy_index.reset()
    try:
        db_store.upsert_search_index("a", "sharedword", "", "sharedword")
        watchdog_app.process_tantivy_mirror_step()
        watchdog_app.process_tantivy_mirror_step()
        conn = db_store.get_connection()
        conn.execute("UPDATE tantivy_mirror_queue SET attempt_count=3, last_error='fixture exhausted'")
        conn.commit()
        result = watchdog_app.process_tantivy_mirror_step()
        assert result["commits"] == 0 and result["blocked"] == 1
        status = json.loads(get_status_file().read_text())["components"]["tantivy"]
        assert status["status"] == "error" and "blocked=1" in status["current_action"]
        assert "blocked" in status["last_error"]
    finally:
        tantivy_index.reset()
