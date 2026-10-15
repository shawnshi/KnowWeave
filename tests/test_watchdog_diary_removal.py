"""The deprecated diary sync component is gone, and stale components cannot linger.

The diary watcher ran an external ``sync_focus.py`` (the Gemini-era
personal-insights pipeline) on every change under ``raw/privacy/Diary``.  The
script no longer exists on this host, so the component could only ever write
``error``, and because ``write_status`` merges components and the aggregate takes
the worst one, that single stale entry pinned the whole daemon to ``error``.
"""

import json
import threading

from vector_lake import host_env, watchdog_app, watchdog_status
from vector_lake.wiki_utils import get_meta_dir


class _Event:
    is_directory = False

    def __init__(self, src_path: str):
        self.src_path = src_path
        self.dest_path = src_path


def test_the_diary_component_and_its_hook_are_removed():
    assert not hasattr(watchdog_app, "DiaryWatchdogHandler")
    assert not hasattr(watchdog_app, "_run_diary_sync")
    assert not hasattr(host_env, "legacy_diary_sync_script")
    assert not hasattr(host_env, "LEGACY_DIARY_SYNC_SCRIPT")


def test_diary_text_is_still_kept_out_of_the_lake(isolated_memory, monkeypatch):
    """Removing the component must not start ingesting private diary text."""
    from vector_lake import tool_sync

    calls = []
    finished = threading.Event()

    def sync(*args, **kwargs):
        calls.append(args)
        finished.set()

    monkeypatch.setattr(tool_sync, "sync_vector_lake", sync)
    handler = watchdog_app.RawWatchdogHandler()
    try:
        handler.handle_event(_Event(str(isolated_memory / "raw" / "privacy" / "Diary" / "2026-Q3.md")))
        assert calls == [] and not hasattr(handler, "executor")
        ordinary = str(isolated_memory / "raw" / "news" / "2026-Q3.md")
        handler.handle_event(_Event(ordinary))
        assert finished.wait(3), "ordinary source must complete an asynchronous scan"
        assert len(calls) == 1
    finally:
        handler.shutdown(wait=True)


def test_reset_components_drops_a_component_that_no_longer_reports(isolated_memory):
    watchdog_status.write_status("error", 0, 0, "Diary sync script missing", "boom", component="diary")
    file = get_meta_dir() / ".watchdog_status.json"
    assert json.loads(file.read_text(encoding="utf-8"))["status"] == "error"

    watchdog_status.reset_components()

    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["components"] == {}
    assert data["status"] == "idle"
    assert data["last_error"] == ""


def test_write_status_still_merges_and_aggregates_the_worst_component(isolated_memory):
    """The reset must not change how components are recorded afterwards."""
    watchdog_status.reset_components()
    watchdog_status.write_status("idle", 0, 0, "heartbeat", component="watchdog")
    watchdog_status.write_status("processing", 0, 0, "batch", component="outbox")

    data = json.loads((get_meta_dir() / ".watchdog_status.json").read_text(encoding="utf-8"))
    assert set(data["components"]) == {"watchdog", "outbox"}
    assert data["status"] == "processing"
    assert data["current_action"] == "batch"


def test_the_batch_scan_skips_diary_text_too(isolated_memory):
    """The guard has to hold at the *scan*, not only at the event that triggers it.

    ``RawWatchdogHandler`` refused to trigger on a diary edit, but it triggers a scan of the
    whole raw tree for any other file -- and that scan had no diary rule, so the three
    ``raw/privacy/Diary`` files were enqueued for real ingestion by an unrelated raw event.
    """
    import json

    from vector_lake import db_store, tool_ingest

    raw = isolated_memory / "raw"
    (raw / "privacy" / "Diary").mkdir(parents=True, exist_ok=True)
    (raw / "privacy" / "Diary" / "2026-Q3.md").write_text("# private diary\n", encoding="utf-8")
    (raw / "news").mkdir(parents=True, exist_ok=True)
    ordinary = raw / "news" / "briefing.md"
    ordinary.write_text("# ordinary source\n", encoding="utf-8")

    db_store.init_db()
    message = tool_ingest.prepare_ingest_batch(batch_size=50)
    assert "enqueued" in message

    enqueued = [
        json.loads(row["payload"]).get("filepath")
        for row in db_store.get_connection().execute("SELECT payload FROM jobs")
    ]
    assert str(ordinary) in enqueued
    assert not any("Diary" in str(path) for path in enqueued), enqueued


def test_private_source_predicate_compares_segments():
    from vector_lake.wiki_utils import is_private_raw_source

    assert is_private_raw_source(r"C:\MEMORY\raw\privacy\Diary\2026-Q3.md") is True
    assert is_private_raw_source("/mnt/memory/raw/privacy/Diary/audit/2026-Q2.md") is True
    assert is_private_raw_source("raw/privacy/Diary.md") is False
    assert is_private_raw_source("raw/privacy-reports/2026.md") is False
    assert is_private_raw_source("raw/Diarystudies/notes.md") is False
    assert is_private_raw_source("") is False
