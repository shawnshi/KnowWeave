"""Resolve the ingest model seam without changing the queue or publication protocol."""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

BACKEND_COMMANDS = {
    "pi": "python scripts/ingest_model_pi_subagents.py",
    "gemini": "python scripts/ingest_model_cli.py --backend gemini",
    "codex": "python scripts/ingest_model_cli.py --backend codex",
    "agy": "python scripts/ingest_model_cli.py --backend agy",
}
_RUNNER_BIN_ENV = {
    "pi": "VECTOR_LAKE_RUNNER_PI_BIN",
    "gemini": "VECTOR_LAKE_RUNNER_GEMINI_BIN",
    "codex": "VECTOR_LAKE_RUNNER_CODEX_BIN",
    "agy": "VECTOR_LAKE_RUNNER_AGY_BIN",
}
SELECTION_SOURCES = ("cli", "environment", "config", "default")
_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class IngestBackend:
    backend: str
    model_command: str
    source: str

    def as_dict(self) -> dict:
        return {"effective_backend": self.backend, "model_command": self.model_command,
                "selection_source": self.source}


def validate_ingest_config(config: dict) -> None:
    if "ingest" not in config:
        return
    ingest = config["ingest"]
    if not isinstance(ingest, dict):
        raise ValueError("config.ingest must be an object")
    if set(ingest) - {"backend"}:
        raise ValueError("config.ingest supports only the backend field")
    backend = ingest.get("backend", "pi")
    if not isinstance(backend, str) or backend not in BACKEND_COMMANDS:
        raise ValueError("config.ingest.backend must be pi, gemini, codex or agy")


def resolve_ingest_backend(model_cmd: str | None = None, *, env: Mapping | None = None,
                           config: dict | None = None, repo_root: Path | None = None) -> IngestBackend:
    if config is None:
        from vector_lake.wiki_utils import load_config
        config = load_config(repo_root=repo_root)
    validate_ingest_config(config)
    env = os.environ if env is None else env
    command = (model_cmd or "").strip()
    source = "cli"
    if not command:
        command = str(env.get("VECTOR_LAKE_RUNNER_MODEL_CMD") or "").strip()
        source = "environment"
    if command:
        backend = next((name for name, value in BACKEND_COMMANDS.items() if value == command), "custom")
        return IngestBackend(backend, command, source)
    backend = config.get("ingest", {}).get("backend", "pi")
    source = "config" if "backend" in config.get("ingest", {}) else "default"
    return IngestBackend(backend, BACKEND_COMMANDS[backend], source)


def builtin_model_argv(command: str, repo_root: Path | None = None) -> list[str] | None:
    root = repo_root or _REPO_ROOT
    for backend, builtin in BACKEND_COMMANDS.items():
        if command == builtin:
            script = "ingest_model_pi_subagents.py" if backend == "pi" else "ingest_model_cli.py"
            argv = [sys.executable, str(root / "scripts" / script)]
            return argv if backend == "pi" else argv + ["--backend", backend]
    return None


def find_cli_binary(backend: str) -> str | None:
    env_var = _RUNNER_BIN_ENV.get(backend)
    name = os.environ.get(env_var, backend) if env_var else backend
    found = shutil.which(name)
    if found or backend != "pi" or name != "pi":
        return found
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise RuntimeError("cannot resolve the managed Pi CLI user home") from exc
    managed = home / ".pi" / "agent" / "bin" / ("pi.cmd" if os.name == "nt" else "pi")
    try:
        return str(managed) if managed.is_file() and os.access(managed, os.X_OK) else None
    except OSError as exc:
        raise OSError("cannot inspect the managed Pi CLI launcher") from exc


def check_ingest_backend(selection: IngestBackend) -> dict:
    """Local capability check only; never claim jobs or make a model/authentication call."""
    boundaries = {
        "pi": {
            "capability_scope": "executable_resolution_only",
            "execution_boundary": "read_only_ingestor_child; parent_uses_host_pi_policy",
            "local_retention": "pi_sessions_and_complete_failure_logs",
        },
        "gemini": {
            "capability_scope": "headless_flags",
            "execution_boundary": "tool_deny_policy; no_os_sandbox_added_by_adapter",
            "local_retention": "temporary_adapter_files; host_cli_provider_policy_is_separate",
        },
        "codex": {
            "capability_scope": "headless_flags_and_feature_gates",
            "execution_boundary": "tools_disabled; cli_read_only_sandbox_requested",
            "local_retention": "ephemeral_cli_and_temporary_adapter_files; provider_policy_is_separate",
        },
        "agy": {
            "capability_scope": "headless_flags_and_settings",
            "execution_boundary": "plan_mode_sandbox_and_deny_all_tools_policy",
            "local_retention": "ephemeral_adapter_files; host_cli_provider_policy_is_separate",
        },
        "custom": {
            "capability_scope": "not_checked",
            "execution_boundary": "custom_shell_command; privileges_and_tools_unverified",
            "local_retention": "custom_command_defined",
        },
    }
    report = {**selection.as_dict(), **boundaries[selection.backend],
              "boundary_verified": False}
    if selection.backend == "custom":
        return {**report, "capability_checked": False, "authentication_verified": False}
    if selection.backend == "pi":
        binary = find_cli_binary("pi")
        if not binary:
            raise RuntimeError("pi ingest backend: CLI executable is unavailable")
        return {**report, "binary": binary, "capability_checked": True,
                "authentication_verified": False}
    from vector_lake.ingest_cli import check_cli
    capabilities = check_cli(selection.backend)
    return {**report, "binary": capabilities.binary, "capability_checked": True,
            "authentication_verified": False}


def supervisor_selection_status(plan, record: dict) -> dict:
    """Do not mistake adoption of a locked supervisor for activation of our backend."""
    actual_command = record.get("model_command")
    if not isinstance(actual_command, str):
        actual_command = record.get("resolved_model_command")
    actual = record.get("effective_backend")
    if not isinstance(actual, str) or actual not in {*BACKEND_COMMANDS, "custom"}:
        actual = None
    if not actual and isinstance(actual_command, str):
        actual = next((k for k, v in BACKEND_COMMANDS.items() if v == actual_command), "custom")
    known = isinstance(actual_command, str) and bool(actual_command)
    return {"requested_backend": plan.backend, "effective_backend": actual or "unknown",
            "configuration_mismatch": actual_command != plan.model_command if known else None}


def normalize_pid(value: object) -> int:
    """Accept integral PID tokens only, bounded before any native conversion/probe."""
    if os.name == "nt":
        maximum = (1 << 32) - 1  # OpenProcess takes a DWORD.
    else:
        import ctypes
        maximum = (1 << (8 * ctypes.sizeof(ctypes.c_int) - 1)) - 1  # Supported POSIX pid_t.
    message = "PID must be a positive integer within platform range"
    if isinstance(value, bool):
        raise ValueError(message)
    if isinstance(value, int):
        pid = value
    elif isinstance(value, str):
        token = value.strip()
        if not token.isascii() or not token.isdigit() or len(token) > len(str(maximum)):
            raise ValueError(message)
        try:
            pid = int(token)
        except (ValueError, OverflowError) as exc:
            raise ValueError(message) from exc
    else:
        # No int(float): JSON 1e999, NaN and fractional values are invalid PID records.
        raise ValueError(message)
    if not 0 < pid <= maximum:
        raise ValueError(message)
    return pid


def pid_is_live(pid: int) -> bool:
    """Probe only a validated PID recorded by this root; never signal 0 on Windows."""
    pid = normalize_pid(pid)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:
                return True  # Access denied is not proof the recorded owner died.
            if error == 87:
                return False
            raise OSError(error, "cannot inspect recorded ingest owner PID")
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_create_time_ns(pid: int) -> int | None:
    """Return process creation timestamp (100ns/tick normalized to ns) when inspectable."""
    pid = normalize_pid(pid)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        ]
        api.GetProcessTimes.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation, exited, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
            if not api.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return int(ticks) * 100
        finally:
            api.CloseHandle(handle)
    return None


def read_runtime_record(filename: str) -> dict:
    from vector_lake.wiki_utils import get_meta_dir
    path = get_meta_dir() / "runtime" / filename
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        return {"record_state": "unreadable", "record_error_type": type(exc).__name__}
    return record if isinstance(record, dict) else {"record_state": "invalid"}


def read_ingest_owner_record() -> dict:
    """Report the live root owner, including a directly started runner, without writing."""
    active = {"starting", "running", "processing", "idle", "recovering", "restarting"}
    candidates = []
    unreadable = []
    for filename in ("runner_supervisor.json", "runner_status.json"):
        record = read_runtime_record(filename)
        if record.get("record_state") in {"unreadable", "invalid"}:
            unreadable.append({"file": filename, **record})
        state = record.get("status")
        if not isinstance(state, str) or state not in active:
            continue
        try:
            pid = normalize_pid(record.get("pid"))
        except ValueError as exc:
            unreadable.append({"file": filename, "record_state": "invalid",
                               "record_error_type": type(exc).__name__})
            continue
        if not pid_is_live(pid):
            continue
        recorded_ct = record.get("pid_create_time_ns")
        if isinstance(recorded_ct, int) and not isinstance(recorded_ct, bool):
            actual_ct = process_create_time_ns(pid)
            if actual_ct is not None and actual_ct != recorded_ct:
                continue
        try:
            stamp = datetime.fromisoformat(str(record.get("updated_at") or "").replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except ValueError:
            stamp = datetime.min.replace(tzinfo=timezone.utc)
        candidates.append((stamp, {**record, "pid": pid}))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return {"record_state": "unreadable", "record_errors": unreadable} if unreadable else {}


def report_ingest_conflict(selection: IngestBackend, context: str) -> None:
    record = read_ingest_owner_record()
    report = supervisor_selection_status(selection, record)
    report.update(owner_pid=record.get("pid"), outcome="already_running")
    if record.get("record_state"):
        report.update(record_state=record["record_state"], record_errors=record.get("record_errors", []))
    print(context + "; " + json.dumps(report, ensure_ascii=False), flush=True)


def is_supervisor_child() -> bool:
    record = read_runtime_record("runner_supervisor.json")
    state = record.get("status")
    try:
        parent = normalize_pid(record.get("pid"))
        child = record.get("child_pid")
        child = normalize_pid(child) if child is not None else None
    except ValueError:
        return False
    return (parent == os.getppid()
            and isinstance(state, str) and state in {"starting", "running", "restarting"}
            and child in (None, os.getpid()))


def legacy_runner_is_live() -> bool:
    """Fail closed for an old consumer that predates our lock; never delete its PID file."""
    from vector_lake.wiki_utils import get_meta_dir
    path = get_meta_dir() / "runtime" / "runner.pid"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    try:
        pid = normalize_pid(raw)
    except ValueError as exc:
        raise ValueError("recorded runner PID is invalid or out of range; drain/verify the old owner before startup") from exc
    return pid != os.getpid() and pid_is_live(pid)
