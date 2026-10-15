import json
import logging
import os
import queue
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from vector_lake import get_extension_root
from vector_lake.watchdog_status import reset_components, write_status
from vector_lake.wiki_utils import DEFAULT_EXCLUDE_PATHS, is_private_raw_source, load_config

# Config: shipped defaults merged with the optional per-machine ``config.json``.
# Opening that file at import time used to make a checkout without it
# unimportable, and a defaulted empty list used to drop every exclusion.
CONFIG_PATH = get_extension_root() / "config.json"
config = load_config()
EXCLUDE_PATHS = config.get("exclude_paths", list(DEFAULT_EXCLUDE_PATHS))

try:
    from watchfiles import Change as _FileChange, watch as _watch
    _WATCHFILES_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - host without the wheel
    _FileChange = None
    _watch = None
    _WATCHFILES_IMPORT_ERROR = exc

if _WATCHFILES_IMPORT_ERROR is not None:
    print("Error: `watchfiles` library is not installed. Please run `pip install watchfiles`.", flush=True)
    import sys
    sys.exit(1)


class _WatchEvent:
    """The event surface the handlers use, kept from the ``watchdog`` days.

    ``watchfiles`` reports ``(Change, path)`` for added/modified/deleted and has no move event
    (a rename arrives as deleted + added, which is exactly what the handlers do with a move).
    Keeping a tiny event object means the handlers, their per-path debounce and their managed-
    projection filtering stay byte-for-byte the code the tests already exercise.
    """

    __slots__ = ("src_path", "dest_path", "is_directory", "event_type")

    def __init__(self, src_path, change):
        self.src_path = src_path
        self.dest_path = src_path
        self.is_directory = os.path.isdir(src_path)
        self.event_type = change.name


class FileSystemEventHandler:
    """Local base class: the handlers only need their own ``on_*`` methods to exist."""


class _WatchLoop:
    """One ``watchfiles`` watcher for one directory, dispatching into one handler.

    Replaces ``watchdog.observers.Observer``.  The two monitored trees want different
    recursion (wiki: not recursive, raw: recursive) and ``watchfiles`` fixes recursion per
    call, so this is one loop per directory instead of one observer with two schedules.
    """

    def __init__(self, handler, path, recursive: bool, label: str):
        self.name = f"watch-{label}"
        self.handler = handler
        self.path = path
        self.recursive = recursive
        self.label = label
        self._stop_event = threading.Event()

    def start(self):
        from vector_lake import thread_supervision

        thread_supervision.start(self.name, self.run)

    def run(self):
        from vector_lake import thread_supervision

        if self._stop_event.is_set():
            thread_supervision.finish(self.name, "watcher stopped")
            return
        write_status("idle", 0, index_queue.qsize(), f"Watcher {self.label} running", "", component=self.name)
        dispatch = {
            _FileChange.added: getattr(self.handler, "on_created", None),
            _FileChange.modified: getattr(self.handler, "on_modified", None),
            _FileChange.deleted: getattr(self.handler, "on_deleted", None),
        }
        try:
            for changes in _watch(
                self.path,
                recursive=self.recursive,
                stop_event=self._stop_event,
                debounce=int(DEBOUNCE_SECONDS * 500),
            ):
                for change, raw_path in changes:
                    callback = dispatch.get(change)
                    if callback is None:
                        continue
                    try:
                        callback(_WatchEvent(str(raw_path), change))
                    except Exception as exc:  # noqa: BLE001 - one bad event must not kill the loop
                        log.warning("Watch handler %s failed on %s: %s", self.label, raw_path, exc)
        except Exception as exc:  # noqa: BLE001 - a dead watcher has to be visible, not silent
            log.exception("Watcher %s stopped: %s", self.label, exc)
            write_status(
                "error", 0, index_queue.qsize(),
                f"Watcher {self.label} stopped: {type(exc).__name__}", str(exc), component=self.name,
            )
            raise
        finally:
            if self._stop_event.is_set():
                thread_supervision.finish(self.name, "watcher stopped")

    def stop(self):
        self._stop_event.set()

    def join(self, timeout=None):
        from vector_lake import thread_supervision

        thread_supervision.join(self.name, timeout=timeout)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("watchdog_sync")

DEBOUNCE_SECONDS = 3.0
index_queue = queue.Queue()
global_task_lock = threading.Lock()


class WikiIndexHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}
        self.lock = threading.Lock()

    def queue_path(self, filepath_str: str):
        filepath = os.path.abspath(filepath_str)
        filename = os.path.basename(filepath)
        if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md"):
            return

        try:
            from vector_lake import db_store

            if os.path.exists(filepath):
                payload_text = Path(filepath).read_text(encoding="utf-8")
                if db_store.is_managed_projection_state(filename, "update", payload_text):
                    return
            elif db_store.is_managed_projection_state(filename, "delete"):
                return
        except Exception as exc:
            log.warning("Could not classify projection event for %s: %s", filename, exc)

        now = time.time()
        with self.lock:
            if len(self.last_triggered) > 1000:
                self.last_triggered = {k: v for k, v in self.last_triggered.items() if (now - v) <= DEBOUNCE_SECONDS * 2}
            
            if filepath in self.last_triggered and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS:
                return
            self.last_triggered[filepath] = now

        index_queue.put(filename)

    def update_index(self, event):
        if event.is_directory:
            return
        self.queue_path(event.src_path)

    def on_created(self, event):
        self.update_index(event)

    def on_modified(self, event):
        self.update_index(event)

    def on_deleted(self, event):
        self.update_index(event)

    def on_moved(self, event):
        if event.is_directory:
            return
        self.queue_path(event.src_path)
        self.queue_path(event.dest_path)


# The diary watcher used to run an external ``sync_focus.py`` on every change under
# ``raw/privacy/Diary``.  That integration was the Gemini-era personal-insights
# pipeline; it is deprecated and the script no longer exists on this host, so the
# component could only ever write ``error`` into the watchdog status and pin the
# whole daemon to ``error``.  It is removed rather than re-pointed.
#
# Diary files stay out of the graph: ``RawWatchdogHandler`` still skips
# ``raw/privacy/Diary``, so a diary edit is now a no-op for the lake instead of a
# failed external call.  Ingesting private diary text into the wiki would be a
# privacy decision, not a cleanup.


class RawWatchdogHandler(FileSystemEventHandler):
    """Trigger ingestion for raw sources, except private diary text.

    ``raw/privacy/Diary`` is skipped outright.  It used to be excluded so the
    separate diary watcher could handle it; that watcher ran a deprecated external
    sync script and is gone, so the exclusion now means the stronger thing: diary
    text is never ingested into the lake.  Feeding private diary content into the
    wiki is a privacy decision, not a side effect of removing a component.
    """

    _MAX_DEBOUNCE_PATHS = 1024

    def __init__(self):
        self.last_triggered = {}
        self.lock = threading.Lock()
        self._sync_dirty = False
        self._sync_running = False
        self._closing = False
        self._drain_on_close = False

    def handle_event(self, event):
        if event.is_directory:
            return
        filepath = event.src_path

        # One owner for the privacy rule (see wiki_utils.is_private_raw_source): this
        # handler and the batch scan it calls must agree, or the scan re-enqueues what
        # this handler refuses to trigger on.
        if is_private_raw_source(filepath):
            return
            
        filename = os.path.basename(filepath)
        # only md, pdf, txt, etc? Let's just say any file that doesn't start with . and doesn't end with .tmp
        if filename.startswith('.') or filename.endswith('.tmp'):
            return

        now = time.time()
        with self.lock:
            if self._closing:
                return
            if filepath in self.last_triggered and (now - self.last_triggered[filepath]) < DEBOUNCE_SECONDS:
                return
            self.last_triggered.pop(filepath, None)
            self.last_triggered[filepath] = now
            if len(self.last_triggered) > self._MAX_DEBOUNCE_PATHS:
                self.last_triggered.pop(next(iter(self.last_triggered)))
            # Each sync scans all eligible sources; a dirty bit preserves the final
            # event batch without retaining one executor task per source (VL-ARCH-03).
            self._sync_dirty = True
            if self._sync_running:
                return
            self._sync_running = True
            try:
                if not hasattr(self, "executor"):
                    import concurrent.futures
                    self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                self.executor.submit(self._drain_sync)
            except Exception:
                self._sync_running = False
                log.exception("Failed to schedule raw-source sync")

    def _drain_sync(self):
        while True:
            with self.lock:
                if self._closing and not self._drain_on_close:
                    if self._sync_dirty:
                        log.warning("Raw-source sync stopped with pending work; resume sync if periodic catch-up is disabled")
                    self._sync_running = False
                    return
                if not self._sync_dirty:
                    self._sync_running = False
                    return
                self._sync_dirty = False
            try:
                from vector_lake.tool_sync import sync_vector_lake
                progress = {}
                sync_vector_lake(_progress=progress)
                if progress.get("remaining", 0) > 0:
                    if progress.get("enqueued", 0) > 0:
                        with self.lock:
                            self._sync_dirty = True
                    else:
                        log.warning("Raw-source batch made no progress; remaining=%s", progress["remaining"])
            except Exception:
                log.exception("sync_vector_lake failed")

    def on_created(self, event):
        self.handle_event(event)

    def on_modified(self, event):
        self.handle_event(event)

    def on_moved(self, event):
        self.handle_event(event)

    def shutdown(self, wait: bool = False):
        """Cleanly shut down the background worker pool on daemon exit."""
        with self.lock:
            self._closing = True
            self._drain_on_close = self._drain_on_close or wait
            executor = getattr(self, "executor", None)
        # The worker needs the same lock to settle; joining under it deadlocks.
        if executor is not None:
            try:
                executor.shutdown(wait=wait)
            except Exception as exc:
                log.warning("RawWatchdogHandler executor shutdown error: %s", exc)


def process_mutation_outbox_batch(limit: int = 50, max_attempts: int = 3, backoff_base: float = 2.0):
    """Materialize and index only current, version-bound leased intents."""
    from vector_lake import db_store
    from vector_lake.indexer import update_index_items
    from vector_lake.mutation_coordinator import (
        ProjectionVersionConflict, materialize_markdown_projection, projection_locks,
        require_projection_intent, resolve_wiki_mutation_path,
    )

    rows = db_store.claim_mutation_outbox(limit=limit)
    stats = {"claimed": len(rows), "completed": 0, "retrying": 0, "failed": 0, "superseded": 0, "lease_lost": 0}
    ready = []
    settled = set()

    def lost(row, exc):
        settled.add(row["id"])
        stats["superseded" if isinstance(exc, db_store.MutationSuperseded) else "lease_lost"] += 1

    def failed(row, exc):
        try:
            state = db_store.fail_mutation_outbox(
                row["id"], str(exc), max_attempts=1 if isinstance(exc, ProjectionVersionConflict) else max_attempts,
                backoff_base=backoff_base, claim=row,
            )
        except db_store.MutationClaimLost as stale:
            lost(row, stale)
            return
        settled.add(row["id"])
        stats["failed" if state == "failed" else "retrying"] += 1
        log.error("Outbox %s could not publish %s; status=%s: %s", row["id"], row["filename"], state, exc)

    for row in rows:
        try:
            mode = str(row.get("validation_mode") or "full")
            with projection_locks([row["filename"]]):
                require_projection_intent(row["id"], row["filename"], row["mutation_type"], row["payload_text"], claim=row, validation_mode=mode)
                filepath = resolve_wiki_mutation_path(row["filename"], allow_existing_legacy_name=mode == "schema" or row["mutation_type"] == "delete", validate_new_name=row["mutation_type"] != "delete")
                matched = row["mutation_type"] == "delete" and not filepath.exists()
                if row["mutation_type"] == "update" and row["payload_text"] is not None and filepath.exists():
                    matched = filepath.read_text(encoding="utf-8") == row["payload_text"]
                if not matched:
                    materialize_markdown_projection(row["filename"], row["mutation_type"], row["payload_text"], validation_mode=mode, outbox_id=row["id"], claim=row)
                ready.append(row)
        except db_store.MutationClaimLost as exc:
            lost(row, exc)
        except Exception as exc:
            failed(row, exc)

    if ready:
        try:
            with projection_locks([row["filename"] for row in ready]):
                current = []
                for row in ready:
                    try:
                        require_projection_intent(row["id"], row["filename"], row["mutation_type"], row["payload_text"], claim=row, validation_mode=str(row.get("validation_mode") or "full"))
                        current.append(row)
                    except db_store.MutationClaimLost as exc:
                        lost(row, exc)
                    except Exception as exc:
                        failed(row, exc)
                if current:
                    try:
                        update_index_items(list(dict.fromkeys(row["filename"] for row in current)))
                    except Exception as exc:
                        for row in current:
                            failed(row, exc)
                    else:
                        for row in current:
                            try:
                                db_store.complete_mutation_outbox(row["id"], claim=row)
                                stats["completed"] += 1
                                settled.add(row["id"])
                            except db_store.MutationClaimLost as exc:
                                lost(row, exc)
        except Exception as exc:
            for row in ready:
                if row["id"] not in settled:
                    failed(row, exc)
    return stats

def canonicalise_manual_edits(filenames) -> tuple[int, list[tuple[str, str]]]:
    """Canonicalise hand-edited Markdown through the durable mutation path.

    Returns ``(handled_count, failures)``.  A page that fails schema or purpose
    validation is left byte-for-byte untouched on disk and reported back to the
    caller instead of being silently dropped.
    """
    from vector_lake import db_store
    from vector_lake.mutation_coordinator import execute_mutation_plan, projection_locks
    from vector_lake.wiki_utils import get_wiki_dir

    wiki_dir = get_wiki_dir()
    handled = 0
    failures: list[tuple[str, str]] = []
    for fname in filenames:
        fpath = wiki_dir / fname
        try:
            with projection_locks([fname]):
                if fpath.exists():
                    manual_text = fpath.read_text(encoding="utf-8")
                    if db_store.is_managed_projection_state(fname, "update", manual_text):
                        continue
                    execute_mutation_plan(fname, manual_text)
                else:
                    if db_store.is_managed_projection_state(fname, "delete"):
                        continue
                    execute_mutation_plan(fname, is_delete=True)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            failures.append((fname, reason))
            log.error(
                "Manual edit for %s was NOT canonicalised; the file is left untouched. "
                "Fix the page and re-save it. Reason: %s",
                fname,
                reason,
            )
            continue
        handled += 1
        log.info("Canonicalised manual edit for %s", fname)
    return handled, failures


# The outbox consumer is the single writer for the read projection.  A rebuild is
# expensive, and the loop ticks every second, so a stale projection is retried at
# this interval rather than at loop frequency.
PROJECTION_HEAL_INTERVAL_SECONDS = 15.0
_last_projection_heal = 0.0


def heal_page_index_projection(force: bool = False) -> dict:
    """Single-writer repair of the SQLite read projection.

    The reader-side rebuild was removed: a reader that found the projection behind
    used to take the write lock and rebuild it, so every query competed with
    whoever was writing, and the loser returned no answer at all.  Readers now
    fall back to ``index.json`` (see ``page_index_projection.read_catalog``);
    this is the only place the projection is brought back in step out of band,
    and it runs in the process that already owns the index.
    """
    global _last_projection_heal
    from vector_lake import page_index_projection

    if not force:
        if (
            page_index_projection.projection_is_current()
            and page_index_projection.projection_stale() is None
        ):
            return {"healed": False, "reason": "current"}
        now = time.monotonic()
        if (now - _last_projection_heal) < PROJECTION_HEAL_INTERVAL_SECONDS:
            return {"healed": False, "reason": "throttled"}
        _last_projection_heal = now

    report = page_index_projection.heal_projection_if_stale()
    if report.get("healed"):
        log.info("Page index projection rebuilt from index.json: %s", report)
    elif report.get("reason") not in {"current"}:
        # Expected while another writer holds the lock; the next cycle retries.
        log.warning("Page index projection heal deferred: %s", report)
    if report.get("healed") or report.get("marker"):
        write_status(
            "idle",
            0,
            index_queue.qsize(),
            f"Projection: {report.get('reason')}",
            "" if report.get("healed") else str(report.get("reason")),
            component="projection",
        )
    return report


def process_tantivy_mirror_step() -> dict:
    """One lexical step on the existing worker; never drain the whole backlog."""
    from vector_lake import db_store, tantivy_index

    db_store.init_db()
    stats = tantivy_index.replay_pending(max_rows=100, max_bytes=4 * 1024 * 1024, max_attempts=3)
    blocked = stats["blocked"] or (
        stats["phase"] == "needs_clear" and stats["attempt_count"] >= 3
    )
    state = "error" if blocked else "processing" if stats["status"] == "pending" else "idle"
    write_status(
        state, stats["pending"], index_queue.qsize(),
        f"Tantivy {stats['status']}: phase={stats['phase']}, pending={stats['pending']}, "
        f"blocked={stats['blocked']}, control_attempts={stats['attempt_count']}",
        stats["last_error"] or ("mirror intents blocked; explicit repair required" if blocked else ""),
        component="tantivy",
    )
    return stats


def index_worker_loop():
    log.info("Outbox Consumer Thread started.")
    consecutive_failures = 0
    max_failures = 5
    backoff_base = 2
    idle_streak = 0
    last_idle_status_write = 0.0

    while True:
        try:
            # Independent of Markdown's cooldown and failures; cleanup below owns the connection.
            try:
                process_tantivy_mirror_step()
            except Exception as exc:  # noqa: BLE001 - lexical failure cannot stop Markdown work
                log.error("Tantivy worker error: %s: %s", type(exc).__name__, exc)
                write_status(
                    "error", 0, index_queue.qsize(), "Tantivy replay exception",
                    f"{type(exc).__name__}: {exc}"[:2000], component="tantivy",
                )
            if consecutive_failures >= max_failures:
                # ``recovering``, not ``halted``: this is a cooldown that retries, and the
                # aggregate takes the worst component, so the old word pinned the whole
                # daemon to a fault state for a condition that heals itself.
                write_status("recovering", 0, index_queue.qsize(), "Outbox consumer cooling down", "Max consecutive failures reached", component="outbox")
                log.error("Outbox consumer cooling down for 60s before retry.")
                time.sleep(60)
                consecutive_failures = 0
                continue
                
            import os
            from vector_lake.wiki_utils import get_outbox_signal_path

            flag_path = get_outbox_signal_path()
            
            # The signal is only a latency hint. Durable rows are always polled.
            if os.path.exists(flag_path):
                try: os.remove(flag_path)
                except OSError: pass
                idle_streak = 0

            with global_task_lock:
                stats = process_mutation_outbox_batch(limit=50)

            if stats["claimed"]:
                idle_streak = 0
                write_status(
                    "processing",
                    stats["completed"],
                    index_queue.qsize(),
                    f"Outbox batch: {stats}",
                    "",
                    component="outbox",
                )
                log.info(f"Outbox batch completed: {stats}")

            # The projection is this process's responsibility, never a reader's.
            heal_page_index_projection()

            # Manual filesystem edits are bounded so they cannot starve durable outbox work.
            pending_legacy = set()
            while len(pending_legacy) < 25:
                try:
                    pending_legacy.add(index_queue.get_nowait())
                    index_queue.task_done()
                except queue.Empty:
                    break

            if pending_legacy:
                idle_streak = 0
                write_status(
                    "processing",
                    0,
                    index_queue.qsize(),
                    f"Legacy projection batch: {len(pending_legacy)}",
                    "",
                    component="outbox",
                )
                handled, failures = canonicalise_manual_edits(sorted(pending_legacy))
                write_status(
                    "idle",
                    handled,
                    index_queue.qsize(),
                    f"Manual edit batch: {handled}/{len(pending_legacy)} canonicalised",
                    "; ".join(f"{name}: {reason}" for name, reason in failures[:3]),
                    component="outbox",
                )
            elif not stats["claimed"]:
                idle_streak += 1
                now_t = time.time()
                # Write status when entering idle or on periodic 30s heartbeat, rather than every 1s
                if idle_streak == 1 or (now_t - last_idle_status_write) >= 30.0:
                    write_status(
                        "idle",
                        0,
                        index_queue.qsize(),
                        "Outbox idle",
                        "",
                        component="outbox",
                    )
                    last_idle_status_write = now_t
                # Adaptive sleep: 1.0s up to 3.0s backoff during quiet periods
                sleep_time = min(1.0 + (idle_streak - 1) * 0.25, 3.0)
                time.sleep(sleep_time)

            if consecutive_failures:
                write_status("idle", 0, index_queue.qsize(), "Outbox consumer recovered", "", component="outbox")
            consecutive_failures = 0

        except Exception as exc:
            consecutive_failures += 1
            log.error(f"Outbox worker error: {exc}")
            write_status("error", 0, index_queue.qsize(), "Outbox consumer exception", str(exc), component="outbox")
            time.sleep(min(backoff_base ** consecutive_failures, 60))
        finally:
            # Closing the connection is housekeeping, not a reason to end the loop: this used
            # to sit after ``except``, so an error while closing escaped and killed the outbox
            # consumer for the life of the process.
            try:
                from vector_lake.db_store import close_connection

                close_connection()
            except Exception as exc:  # noqa: BLE001 - the loop must survive its own cleanup
                log.warning("Outbox consumer could not close its connection: %s: %s", type(exc).__name__, exc)


# Hours of the day at which the autonomous lint (and the only periodic
# ``wal_checkpoint(TRUNCATE)``) is due.
SCHEDULED_LINT_HOURS = (10, 23)
#: Ticks an occurrence may fail before it is recorded as failed and the cadence moves on.
#:
#: A failure used to leave ``last_occurrence`` untouched, so the block retried every 30 s --
#: and because the whole block holds ``global_task_lock``, a permanently failing lint also
#: starved the outbox consumer forever.  Three attempts is enough to ride out a transient
#: cause; after that the next scheduled hour is the retry.
MAX_OCCURRENCE_ATTEMPTS = 3


def scheduled_lint_occurrence(now) -> str:
    """The most recent scheduled instant at or before ``now``, as ``YYYY-MM-DD-HH``.

    The loop used to fire only when ``tm_min == 0``, sampled every 30 s, so a
    restart, a slow tick or a long ingest over that minute skipped the day's lint
    entirely -- and with it the only periodic WAL truncation.  Keying on the most
    recent occurrence lets the loop catch up instead, and running at most once per
    occurrence means a week of downtime still triggers exactly one run.
    """
    hours = sorted(SCHEDULED_LINT_HOURS)
    due_hours = [hour for hour in hours if hour <= now.tm_hour]
    if due_hours:
        return f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d}-{due_hours[-1]:02d}"
    previous = date(now.tm_year, now.tm_mon, now.tm_mday) - timedelta(days=1)
    return f"{previous.year:04d}-{previous.month:02d}-{previous.day:02d}-{hours[-1]:02d}"


def _scheduled_lint_state_path() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / ".scheduled_lint_state.json"


def _load_last_scheduled_lint() -> str:
    """Last successfully completed occurrence, or ``""`` when unknown.

    Persisted rather than in-memory: an unpersisted marker would make every
    watchdog restart trigger an immediate full lint.
    """
    try:
        data = json.loads(_scheduled_lint_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("last_occurrence") or "") if isinstance(data, dict) else ""


def _save_last_scheduled_lint(occurrence: str, outcome: str = "completed", detail: str = "") -> None:
    """Record an occurrence, completed or given up on.

    ``outcome`` exists because "the occurrence ran" and "the occurrence succeeded" are
    different facts: a failing lint used to leave the marker alone so it retried every 30 s
    forever, with no record that anything had been given up on.
    """
    path = _scheduled_lint_state_path()
    payload = {
        "last_occurrence": occurrence,
        "outcome": outcome,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if detail:
        payload["detail"] = detail[:500]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        from vector_lake.wiki_utils import flush_durable

        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            flush_durable(handle)
        os.replace(temp_path, path)
    except OSError as exc:
        # Worst case the next restart re-runs one lint, which is idempotent.
        log.warning(f"Could not persist the scheduled-lint marker: {exc}")


def checkpoint_wal(conn):
    """SQLite reports busy through a result row, not necessarily an exception."""
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is None or len(row) != 3:
        raise RuntimeError("WAL checkpoint returned no valid status row")
    busy, log_frames, checkpointed = map(int, row)
    if busy or log_frames != checkpointed:
        raise RuntimeError(f"WAL checkpoint incomplete: busy={busy}, frames={log_frames}, checkpointed={checkpointed}")
    return {"busy": busy, "frames": log_frames, "checkpointed": checkpointed}


def scheduled_lint_loop():
    log.info("Scheduled Lint Worker Thread started.")
    if not os.environ.get("VECTOR_LAKE_IGNORE_SCHEDULED_LINT_STATE"):
        last_occurrence = _load_last_scheduled_lint()
    else:
        # Operator escape hatch for a deliberate forced re-run.
        last_occurrence = ""

    occurrence_failures = 0

    while True:
        try:
            # DB connection opens only inside actual work blocks now
            now = time.localtime()
            due = scheduled_lint_occurrence(now)

            if due != last_occurrence:
                write_status("processing", 0, index_queue.qsize(), "Running Scheduled Auto-Lint", "", component="scheduler")
                log.info(f"Triggering Scheduled Autonomous Auto-Lint for occurrence {due}...")

                from vector_lake.tool_lint import lint_vector_lake
                from vector_lake import indexer
                # The lint gets its own blast radius.  It used to sit bare at the top of the
                # block, so a lint failure skipped the WAL checkpoint, the gram rebuild and the
                # backup retention that follow it -- the only periodic maintenance this system
                # has.  Record the failure and run them anyway.
                failures: list[str] = []
                # Only the graph refresh writes, and it takes the database write lock for its
                # whole body (it opens a write transaction *before* deciding whether anything is
                # dirty -- measured: it raises ``attempt to write a readonly database``
                # immediately under ``PRAGMA query_only``).  It therefore stays under the mutex,
                # which queues the outbox drain behind it instead of letting the drain fail
                # against the 20 s lock budget.
                with global_task_lock:
                    try:
                        if indexer.refresh_graph_topology_if_dirty():
                            log.info("Graph topology refreshed during scheduled lint.")
                    except Exception as exc:  # noqa: BLE001 - maintenance below must still run
                        failures.append(f"graph refresh: {type(exc).__name__}: {exc}")
                        log.error("Scheduled graph refresh failed: %s: %s", type(exc).__name__, exc)

                # The lint is read-only: under ``PRAGMA query_only=ON`` it completes a 13.1 s scan
                # of the live corpus without attempting a single write (the only writers in it are
                # guarded by ``auto_fix``).  It used to hold the same mutex as the outbox drain,
                # which serialised a ~13 s (and growing) read against the mutation path for no
                # reason at all.
                try:
                    lint_vector_lake(auto_fix=False)
                except Exception as exc:  # noqa: BLE001 - maintenance below must still run
                    failures.append(f"lint: {type(exc).__name__}: {exc}")
                    log.error("Scheduled auto-lint failed: %s: %s", type(exc).__name__, exc)
                lint_failure = "; ".join(failures)

                # The gram rebuild deliberately runs *outside* ``global_task_lock``.  It used to
                # hold the in-process mutex for its whole duration, which blocked the outbox
                # consumer for ~8-10 minutes and made every scheduled rebuild an outage for the
                # mutation path (observed twice on 2026-09-18: the drain froze at 1 110 then 610
                # pending rows and logged lock failures throughout).  It now commits one short
                # transaction per batch instead of one transaction for the whole build, so the
                # consumer interleaves; exactness under that interleaving is enforced by the
                # snapshot fence in
                # :func:`vector_lake.memory_gram_index.rebuild_memory_gram_index`, which keeps a
                # change marker for every document whose staged bytes are no longer current.
                try:
                    from vector_lake import memory_gram_index

                    if memory_gram_index.rebuild_due():
                        # The build is still the largest single piece of work this process runs,
                        # so it has to be visible on the status surface while it runs rather than
                        # only in the log line written after it returns.
                        write_status("processing", 0, index_queue.qsize(), "Rebuilding memory gram index", "", component="scheduler")
                    log.info(
                        "Scheduled gram-index maintenance: %s",
                        memory_gram_index.maybe_rebuild_memory_gram_index(),
                    )
                except Exception as e:
                    failures.append(f"gram-index: {type(e).__name__}: {e}")
                    log.error(f"Scheduled gram-index rebuild failed: {e}")

                # Truncate WAL to prevent unbounded growth.  It follows the rebuild because the
                # rebuild defers its own auto-checkpoint (see the bulk-load note in
                # ``rebuild_memory_gram_index``), so this is the truncate that reclaims it.
                from vector_lake.db_store import get_connection
                try:
                    conn = get_connection()
                    checkpoint_wal(conn)
                    log.info("SQLite WAL checkpoint (TRUNCATE) completed successfully.")
                except Exception as e:
                    failures.append(f"WAL checkpoint: {type(e).__name__}: {e}")
                    log.error(f"Failed to truncate WAL: {e}")

                # Storage: strip the payload weight out of the outbox ledger, then report what a
                # VACUUM would return.  The outbox keeps a *row* per mutation forever on purpose
                # (``enqueue_mutation`` revives a terminal row with the same idempotency key instead
                # of inserting a second one), so only the payload is dropped; and the reclaim itself
                # stays operator-window-only, because VACUUM rewrites the whole file and holding the
                # write lock for it would be an outage (``VECTOR_LAKE_RECLAIM_FREE_SPACE=1`` opts
                # in, and this host runs it in the deployment window instead).
                try:
                    from vector_lake import outbox_retention

                    stripped = outbox_retention.prune_outbox_payloads(dry_run=False)
                    if stripped["stripped"]:
                        log.info(
                            "Outbox payload retention stripped %d row(s) older than %d day(s), "
                            "~%.1f MB freed to the freelist.",
                            stripped["stripped"], stripped["keep_days"],
                            stripped["bytes_to_strip"] / 1e6,
                        )
                except Exception as e:
                    failures.append(f"outbox retention: {type(e).__name__}: {e}")
                    log.error(f"Outbox payload retention failed: {e}")

                try:
                    from vector_lake import db_store

                    space = db_store.free_space_report()
                    log.info(
                        "Database space: %.0f MB file, %.0f MB free pages (auto_vacuum=%d).",
                        space["file_bytes"] / 1e6, space["free_bytes"] / 1e6, space["auto_vacuum"],
                    )
                    reclaim = db_store.reclaim_free_space()
                    if reclaim["vacuumed"]:
                        log.info("Database reclaim freed %.0f MB.", reclaim["freed_bytes"] / 1e6)
                    else:
                        log.info("Database reclaim skipped: %s", reclaim["reason"])
                except Exception as e:
                    failures.append(f"database space: {type(e).__name__}: {e}")
                    log.error(f"Database space report failed: {e}")

                # Backups are written by the repair, projection and ingest paths and nothing ever
                # removed them, so the tree grows without bound.  Housekeeping belongs here, next
                # to the other scheduled maintenance; the bound never drops below the newest copy
                # (see vector_lake.backup_retention).
                try:
                    from vector_lake.backup_retention import prune_backups
                    from vector_lake.wiki_utils import get_meta_dir

                    retention = prune_backups(get_meta_dir() / "backups", dry_run=False)
                    if retention["deleted"]:
                        log.info(
                            "Backup retention removed %s entr(ies), %s bytes; %s kept.",
                            len(retention["deleted"]),
                            retention["deleted_bytes"],
                            len(retention["keep"]),
                        )
                    for failure in retention["failures"]:
                        failures.append(f"backup retention: {failure}")
                        log.warning(f"Backup retention left {failure}")
                except Exception as e:
                    failures.append(f"backup retention: {type(e).__name__}: {e}")
                    log.error(f"Backup retention failed: {e}")

                lint_failure = "; ".join(failures)
                log.info("Scheduled maintenance attempt finished; failed stages=%d.", len(failures))
                # A failing occurrence must not be retried forever: an unbounded retry every
                # 30 s held ``global_task_lock`` each time, which starved the outbox consumer
                # behind a permanently failing lint.
                if lint_failure:
                    occurrence_failures += 1
                    if occurrence_failures >= MAX_OCCURRENCE_ATTEMPTS:
                        log.error(
                            "Scheduled occurrence %s failed %d time(s); recording it as failed "
                            "so the cadence waits for the next scheduled hour instead of "
                            "retrying every tick: %s",
                            due, occurrence_failures, lint_failure,
                        )
                        last_occurrence = due
                        _save_last_scheduled_lint(
                            due, outcome="failed", detail=lint_failure
                        )
                        occurrence_failures = 0
                        write_status(
                            "error", 0, index_queue.qsize(),
                            f"Scheduled Lint FAILED ({due}); occurrence recorded",
                            lint_failure, component="scheduler",
                        )
                    else:
                        write_status(
                            "error", 0, index_queue.qsize(),
                            f"Scheduled Lint failed ({occurrence_failures}/{MAX_OCCURRENCE_ATTEMPTS})",
                            lint_failure, component="scheduler",
                        )
                else:
                    occurrence_failures = 0
                    last_occurrence = due
                    _save_last_scheduled_lint(due)
                    write_status("idle", 0, index_queue.qsize(), "Scheduled Lint finished", "", component="scheduler")

            time.sleep(30)

        except Exception as exc:
            log.error(f"Scheduled lint worker error: {exc}")
            write_status("error", 0, index_queue.qsize(), "Scheduled lint exception", str(exc), component="scheduler")
            time.sleep(60)


def _start_watchdog_locked():
    # A fresh process owns a fresh component set.  Without this, a component that a
    # previous run reported on -- but that no longer runs -- keeps its last status
    # forever, and because the aggregate takes the worst status, one stale "error"
    # pins the whole daemon to error.
    reset_components()
    # Every loop is owned by the registry, so a thread that stops running is restartable and
    # reportable instead of silent.  The main loop below is the supervisor of last resort and
    # is the one thread that never returns.
    from vector_lake import thread_supervision

    thread_supervision.start("outbox", index_worker_loop)
    thread_supervision.start("scheduler", scheduled_lint_loop)

    # The ingest runner is what consumes the task packets this process publishes.  It is a
    # separate process on purpose (the model call must not happen inside the runtime), but
    # its *lifecycle* belongs here: without an owner, "start the daemon" leaves packets
    # piling up in awaiting_subagent with nothing claiming them.  See
    # vector_lake.runner_supervision for the switches and the boundary this keeps.
    from vector_lake.runner_supervision import runner_supervisor_loop

    thread_supervision.start("runner-supervisor", runner_supervisor_loop)

    # The recovery sweep: re-enqueue un-ingested sources and retire stale tasks on a timer.
    # Without it a lost, cancelled or never-enqueued source has no path back into the queue
    # short of a human call (see vector_lake.periodic_catch_up).
    from vector_lake.periodic_catch_up import catch_up_loop

    thread_supervision.start("catch-up", catch_up_loop)

    from vector_lake.ingest_worker import start_worker

    thread_supervision.start("ingest-worker", start_worker)

    watchers: list[_WatchLoop] = []

    from vector_lake.wiki_utils import get_wiki_dir

    wiki_dir = str(get_wiki_dir())
    if os.path.exists(wiki_dir):
        wiki_handler = WikiIndexHandler()
        watcher = _WatchLoop(wiki_handler, wiki_dir, False, "wiki")
        watcher.start()
        watchers.append(watcher)
        log.info(f"Wiki AST monitor active on directory: {wiki_dir}")

    from vector_lake.wiki_utils import get_memory_dir

    memory_dir = get_memory_dir()

    raw_handler = None
    raw_dir = str(memory_dir / "raw")
    if os.path.exists(raw_dir):
        raw_handler = RawWatchdogHandler()
        watcher = _WatchLoop(raw_handler, raw_dir, True, "raw")
        watcher.start()
        watchers.append(watcher)
        log.info(f"Raw source monitor active on directory: {raw_dir}")
    log.info("Vector Lake Watchdog Agent is now running in Background Index/Lint mode.")
    write_status("idle", 0, index_queue.qsize(), "Watchdog started", "", component="watchdog")
    last_heartbeat = 0.0
    try:
        last_heartbeat = 0
        while True:
            now = time.time()
            if now - last_heartbeat >= 30:
                # Liveness of every loop, then the heartbeat itself.  This is what makes a
                # dead thread observable: the aggregate timestamp stays fresh either way, so
                # the *threads* component is the only place the death can show up.
                state, message = thread_supervision.describe(thread_supervision.supervise_once())
                write_status(state, 0, index_queue.qsize(), message, "", component="threads")
                write_status("idle", 0, index_queue.qsize(), "Watchdog heartbeat", "", component="watchdog")
                last_heartbeat = now
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Termination signal received. Shutting down Watchdog...")
        for watcher in watchers:
            watcher.stop()
    finally:
        # Taking the supervised runner down with the watchdog keeps one owner for the
        # ingest queue; the supervisor's own atexit hook covers an abrupt exit.
        from vector_lake.runner_supervision import stop as stop_runner_supervision

        try:
            stop_runner_supervision()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("Could not stop the ingest runner supervisor: %s: %s", type(exc).__name__, exc)
        try:
            if raw_handler is not None:
                raw_handler.shutdown(wait=False)
        except Exception:
            pass
        try:
            for watcher in watchers:
                watcher.stop()
        except Exception:
            pass
    for watcher in watchers:
        watcher.join(timeout=5)


def start_watchdog():
    """Run exactly one watchdog instance for the active MEMORY root."""
    from filelock import FileLock, Timeout
    from vector_lake.wiki_utils import get_meta_dir

    lock_path = get_meta_dir() / ".watchdog.instance.lock"
    pid_path = get_meta_dir() / "runtime" / ".watchdog.pid"
    instance_lock = FileLock(str(lock_path))
    try:
        instance_lock.acquire(timeout=0)
    except Timeout as exc:
        holder_pid = None
        if pid_path.exists():
            try:
                holder_pid = pid_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        pid_hint = f" (holder PID: {holder_pid})" if holder_pid else ""
        raise RuntimeError(f"A Vector Lake watchdog instance is already running for this MEMORY root{pid_hint}.") from exc
    try:
        try:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        return _start_watchdog_locked()
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        instance_lock.release()

if __name__ == "__main__":
    start_watchdog()
