import json
import logging
import time
import threading
import uuid
from pathlib import Path
from vector_lake.wiki_utils import get_meta_dir

log = logging.getLogger("vector-lake-watchdog-status")

_status_lock = threading.Lock()

def _write_public_snapshot(status_file: Path, data: dict) -> None:
    from vector_lake.operational_status import write_public_heartbeat

    try:
        write_public_heartbeat(status_file.parent, "watchdog", state=data["status"],
                               task_queue_size=data["task_queue_size"], index_queue_size=data["index_queue_size"])
    except (OSError, ValueError) as exc:
        log.warning("Public watchdog heartbeat unavailable: %s", type(exc).__name__)


def get_status_file() -> Path:
    return get_meta_dir() / ".watchdog_status.json"


def reset_components() -> None:
    """Drop every recorded component, keeping only the file's shared fields.

    ``write_status`` can only merge a component in, never retire one, and the
    aggregate status is the worst of the recorded components.  A component that a
    later release no longer runs would therefore pin the daemon to its last status
    (an ``error``) forever.  A fresh process owns a fresh component set, so it
    starts from an empty one.
    """
    status_file = get_status_file()
    status_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = status_file.with_name(f".watchdog_status_{uuid.uuid4().hex}.tmp")
    with _status_lock:
        existing: dict = {}
        if status_file.exists():
            try:
                existing = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data = {
            "status": "idle",
            "task_queue_size": int(existing.get("task_queue_size") or 0),
            "index_queue_size": int(existing.get("index_queue_size") or 0),
            "current_action": "Watchdog starting",
            "last_error": "",
            "updated_at": now,
            "components": {},
        }
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            temp_file.replace(status_file)
            _write_public_snapshot(status_file, data)
        except Exception as exc:
            log.error("Could not reset the watchdog status file: %s: %s", type(exc).__name__, exc)
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

def write_status(
    state: str,
    task_queue_size: int,
    index_queue_size: int,
    current_action: str = "",
    last_error: str = "",
    component: str = "watchdog",
):
    status_file = get_status_file()
    status_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = status_file.with_name(f".watchdog_status_{uuid.uuid4().hex}.tmp")
    
    with _status_lock:
        for attempt in range(5):
            try:
                existing = {}
                if status_file.exists():
                    try:
                        existing = json.loads(status_file.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        existing = {}
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                components = dict(existing.get("components") or {})
                components[component] = {
                    "status": state,
                    "task_queue_size": task_queue_size,
                    "index_queue_size": index_queue_size,
                    "current_action": current_action,
                    "last_error": last_error,
                    "updated_at": now,
                }
                # ``recovering`` is a self-healing state (a cooldown after transient
                # failures) and must not read as a fault: a transient database-lock loss
                # used to make the whole daemon report ``halted``, which both health
                # surfaces treat as unhealthy, while it recovered by itself.
                priority = {"halted": 3, "error": 2, "recovering": 1, "processing": 1, "idle": 0}
                aggregate = max(
                    components.values(),
                    key=lambda item: priority.get(str(item.get("status", "idle")), 0),
                )
                data = {
                    "status": aggregate.get("status", "idle"),
                    "task_queue_size": task_queue_size,
                    "index_queue_size": index_queue_size,
                    "current_action": aggregate.get("current_action", ""),
                    "last_error": aggregate.get("last_error", ""),
                    "updated_at": now,
                    "components": components,
                }
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                temp_file.replace(status_file)
                _write_public_snapshot(status_file, data)
                break
            except PermissionError:
                time.sleep(0.1)
            except Exception as exc:
                log.error(
                    "Could not persist watchdog status to %s: %s: %s. "
                    "A stale status file makes the runtime report the watchdog as unhealthy.",
                    status_file,
                    type(exc).__name__,
                    exc,
                )
                break
            finally:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass
