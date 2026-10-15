"""Owned subprocess lifetime: gated Windows Job Objects, POSIX process groups.

The bootstrap executes no target code before the parent assigns containment.
Windows Job Objects use KILL_ON_JOB_CLOSE (Microsoft Job Objects API); handles
are private to the owner. No PID discovery, global process killing, or secrets
are placed in command arguments. POSIX containment covers the process group,
not children deliberately escaping it with setsid().
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

BOOTSTRAP_PATH = str(Path(__file__).resolve())


def model_timeout_seconds():
    value = float(os.environ.get("VECTOR_LAKE_RUNNER_MODEL_TIMEOUT", "900"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("VECTOR_LAKE_RUNNER_MODEL_TIMEOUT must be a positive finite number")
    return min(value, 3300.0)


class _WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", w.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD),
                        ("SchedulingClass", w.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        import ctypes
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                import ctypes
                raise ctypes.WinError(ctypes.get_last_error())

    def __del__(self):
        if getattr(self, "handle", None):
            self.close()


def _bootstrap_interpreter(requested, env):
    """Avoid CPython's Windows venv redirector without leaving the selected venv.

    The redirector creates another PID before our gate. CPython's own launcher
    passes __PYVENV_LAUNCHER__ to the base interpreter to retain venv discovery
    (PC/venvlauncher.c, Modules/getpath.py). Use that same handoff only for the
    current CPython venv; never substitute an explicitly different interpreter.
    """
    if (os.name == "nt" and sys.implementation.name == "cpython"
            and sys.prefix != sys.base_prefix
            and os.path.normcase(os.path.abspath(requested))
            == os.path.normcase(os.path.abspath(sys.executable))):
        base = getattr(sys, "_base_executable", None)
        if not base or not Path(base).is_file():
            raise RuntimeError("CPython venv base interpreter is unavailable")
        child_env = dict(os.environ if env is None else env)
        child_env["__PYVENV_LAUNCHER__"] = sys.executable
        return base, child_env
    return None, env


def _spawn(*, python_entry=None, env=None, stdout=None, stderr=None, cwd=None, creationflags=0):
    """Launch a stdlib-only bootstrap that waits for a parent gate on stdin."""
    argv = [python_entry[0] if python_entry is not None else sys.executable,
            BOOTSTRAP_PATH]
    if python_entry is not None:
        # Same process/PID as the Python entry point, preserving supervisor identity.
        argv += ["--python-entry", json.dumps(list(python_entry)[1:])]
    executable, child_env = _bootstrap_interpreter(argv[0], env)
    process = subprocess.Popen(
        argv, executable=executable, stdin=subprocess.PIPE, stdout=stdout,
        stderr=stderr, cwd=cwd, env=child_env,
        text=True, encoding="utf-8", errors="replace",
        start_new_session=os.name != "nt",
        creationflags=(creationflags | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0,
    )
    try:
        if os.name == "nt":
            job = _WindowsJob()
            try:
                job.assign(process)
            except BaseException:
                job.close()
                raise
            process._vector_lake_job = job
        return process
    except BaseException:
        process.kill()  # Target is still waiting at the gate: no descendants yet.
        process.communicate(timeout=5)
        raise


def stop_contained(process):
    """Stop only this owned task, including descendants in its containment."""
    job = getattr(process, "_vector_lake_job", None)
    if job is not None:
        job.close()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()


def start_contained_python(argv, *, env=None, stdout=None, stderr=None, cwd=None, creationflags=0):
    process = _spawn(python_entry=argv, env=env, stdout=stdout, stderr=stderr, cwd=cwd,
                     creationflags=creationflags)
    try:
        process.stdin.write("launch\n")
        process.stdin.close()
        process.stdin = None
        return process
    except BaseException:
        stop_contained(process)
        process.wait(timeout=5)
        raise


def run_contained(command, *, input=None, timeout=900, shell=False,
                  env=None, cwd=None, capture_output=True, text=True,
                  encoding="utf-8", errors="replace"):
    """Bound both execution and pipe draining; never leave a timed-out Job open."""
    if not capture_output or not text or encoding != "utf-8" or errors != "replace":
        raise ValueError("Contained model commands require UTF-8 captured text output")
    if timeout <= 0:
        raise subprocess.TimeoutExpired(command, timeout)
    started = time.monotonic()
    process = _spawn(env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    request = json.dumps({"command": command, "shell": shell, "input": input}, ensure_ascii=False)
    try:
        stdout, stderr = process.communicate(request, timeout=max(0.001, timeout - (time.monotonic() - started)))
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        stop_contained(process)
        stdout, stderr = process.communicate(timeout=5)
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from exc
    finally:
        stop_contained(process)
        if process.poll() is None:
            process.wait(timeout=5)


def _bootstrap():
    if len(sys.argv) > 1 and sys.argv[1] == "--python-entry":
        if sys.stdin.readline().strip() != "launch":
            return 125
        import runpy
        argv = json.loads(sys.argv[2])
        sys.argv = argv
        sys.path[0] = str(Path(argv[0]).resolve().parent)
        runpy.run_path(argv[0], run_name="__main__")
        return 0
    request = json.load(sys.stdin)  # EOF is the launch gate, sent after containment.
    return subprocess.run(request["command"], shell=request["shell"],
                          input=request["input"], text=True, encoding="utf-8", errors="replace").returncode


if __name__ == "__main__":
    raise SystemExit(_bootstrap())
