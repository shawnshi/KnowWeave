"""Interpreter identity, environment preservation and fail-closed launch regressions."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from vector_lake import process_control


def test_contained_payload_preserves_environment_and_interpreter(tmp_path):
    script = tmp_path / "identity with spaces.py"
    script.write_text(
        "import json,os,sys,site\n"
        "print(json.dumps(dict(pid=os.getpid(),prefix=sys.prefix,executable=sys.executable,"
        "sites=site.getsitepackages(),marker=os.environ['VL_TEST_MARKER'])),flush=True)\n",
        encoding="utf-8",
    )
    env = dict(os.environ, VL_TEST_MARKER="synthetic-only")
    before = env.copy()
    process = process_control.start_contained_python(
        [sys.executable, str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        data = json.loads(stdout)
        assert data["pid"] == process.pid
        assert Path(data["prefix"]) == Path(sys.prefix)
        assert Path(data["executable"]) == Path(sys.executable)
        import site
        assert data["sites"] == site.getsitepackages()
        assert data["marker"] == "synthetic-only"
        assert env == before, "The caller's environment must not be mutated"
    finally:
        process_control.stop_contained(process)
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows CPython redirector")
def test_venv_handoff_is_narrow_and_does_not_mutate_env(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "synthetic-venv")
    monkeypatch.setattr(sys, "base_prefix", "synthetic-base")
    env = {"VL_TEST_MARKER": "synthetic-only"}
    executable, copied = process_control._bootstrap_interpreter(sys.executable, env)
    assert executable == getattr(sys, "_base_executable")
    assert copied["__PYVENV_LAUNCHER__"] == sys.executable
    assert env == {"VL_TEST_MARKER": "synthetic-only"}
    assert process_control._bootstrap_interpreter("other-python.exe", env) == (None, env)


@pytest.mark.skipif(os.name != "nt", reason="Windows CPython redirector")
def test_missing_base_interpreter_fails_before_popen(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "synthetic-venv")
    monkeypatch.setattr(sys, "base_prefix", "synthetic-base")
    monkeypatch.setattr(sys, "_base_executable", "absent-synthetic-python.exe")
    def forbidden(*args, **kwargs):
        pytest.fail("Popen called before validating the base interpreter")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(RuntimeError, match="base interpreter is unavailable"):
        process_control.start_contained_python([sys.executable, "never-executed.py"])
