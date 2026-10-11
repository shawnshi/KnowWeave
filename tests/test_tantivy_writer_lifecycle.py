"""Native fixture indexes exercise writer ownership and failed-batch isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from vector_lake import tantivy_index as mirror

pytest.importorskip("tantivy")


@pytest.fixture
def index(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    mirror.reset()
    yield isolated_memory
    mirror.reset()


def test_returned_write_releases_native_lock_for_another_process(index):
    mirror.upsert("parent", "parentword", "", "parentword")
    keep = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMP", "TEMP"):
        directory = index / "child-home" / key
        directory.mkdir(parents=True)
        env[key] = str(directory)
    env.update(
        VECTOR_LAKE_MEMORY_DIR=str(index),
        VECTOR_LAKE_CONFIG_FALLBACK_READONLY="0",
        VECTOR_LAKE_FTS="tantivy",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
    )
    child = subprocess.run(
        [
            sys.executable,
            "-B",
            "-s",
            "-c",
            "from vector_lake import tantivy_index as t; t.upsert('child','childword','','childword')",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 0, child.stderr
    assert {row[0] for row in mirror.search(["childword"])} == {"child"}


def test_failed_generator_cannot_be_committed_by_a_later_operation(index):
    mirror.upsert("kept", "safe", "", "safe")

    def broken_rows():
        yield "kept", "leaked", "", "leaked"
        yield "new", "leaked", "", "leaked"
        raise ValueError("fixture row failure")

    with pytest.raises(ValueError, match="fixture row failure"):
        mirror.upsert_many(broken_rows())
    mirror.upsert("later", "laterword", "", "laterword")
    assert mirror.search(["leaked"]) == []
    assert {row[0] for row in mirror.search(["safe"])} == {"kept"}
    assert {row[0] for row in mirror.search(["laterword"])} == {"later"}


def test_concurrent_producers_keep_both_batches(index):
    def produce(prefix):
        rows = [
            (f"{prefix}{i}", f"word{prefix}", "", f"word{prefix}") for i in range(40)
        ]
        return mirror.upsert_many(rows)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(produce, ("a", "b"))) == [40, 40]
    assert len(mirror.search(["worda"], limit=50)) == 40
    assert len(mirror.search(["wordb"], limit=50)) == 40


def test_cached_reader_can_use_last_commit_while_a_batch_is_open(index):
    mirror.upsert("kept", "safe", "", "safe")
    entered = threading.Event()
    release = threading.Event()

    def rows():
        yield "kept", "pending", "", "pending"
        entered.set()
        assert release.wait(8)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(mirror.upsert_many, rows())
        try:
            assert entered.wait(5)
            assert {row[0] for row in mirror.search(["safe"])} == {"kept"}
            assert mirror.search(["pending"]) == []
        finally:
            release.set()
        assert future.result(timeout=5) == 1
    assert {row[0] for row in mirror.search(["pending"])} == {"kept"}


def test_native_acquisition_error_is_not_retried_or_wrapped(index, monkeypatch):
    original = ValueError("fixture corruption, not writer contention")
    calls = []

    class BrokenIndex:
        def writer(self, **kwargs):
            calls.append(kwargs)
            raise original

    monkeypatch.setattr(mirror, "_open_index_unlocked", lambda: BrokenIndex())
    with pytest.raises(ValueError) as caught:
        mirror.upsert("key", "word", "", "word")
    assert caught.value is original
    assert len(calls) == 1


def test_process_admission_is_bounded_and_does_not_release_an_unowned_lock(
    index, monkeypatch
):
    class BusyLock:
        def acquire(self, *, timeout):
            assert timeout == 3
            return False

        def release(self):
            pytest.fail("must not release another operation's lock")

    with monkeypatch.context() as patch:
        patch.setattr(mirror, "_WRITE_LOCK", BusyLock())
        with pytest.raises(TimeoutError, match="process's tantivy writer"):
            mirror.upsert("key", "word", "", "word")


def test_cleanup_failure_preserves_the_original_mutation_error(
    index, monkeypatch, caplog
):
    original = ValueError("fixture mutation error")

    class BrokenWriter:
        def rollback(self):
            raise RuntimeError("fixture rollback failure")

        def wait_merging_threads(self):
            raise RuntimeError("fixture release failure")

    class FakeIndex:
        def writer(self, **kwargs):
            return BrokenWriter()

    monkeypatch.setattr(mirror, "_open_index_unlocked", lambda: FakeIndex())
    with pytest.raises(ValueError) as caught:
        with mirror._write_session():
            raise original
    assert caught.value is original
    assert "rollback failed" in caplog.text and "release failed" in caplog.text


def test_empty_batch_does_not_change_previous_commit(index):
    mirror.upsert("kept", "safe", "", "safe")
    assert mirror.upsert_many(iter(())) == 0
    assert {row[0] for row in mirror.search(["safe"])} == {"kept"}
