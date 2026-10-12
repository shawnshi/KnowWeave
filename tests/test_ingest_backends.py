"""Backend selection and real subprocess protocols; no provider or production-memory calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import ingest_runner, ingest_runner_service
from vector_lake import ingest_backend, ingest_cli, runner_supervision, wiki_utils
from vector_lake.ingest_model_contract import build_cli_prompt, extract_result, result_schema
from vector_lake.tool_ingest import calculate_hash

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("backend", ["pi", "gemini", "codex"])
def test_config_selects_builtin_backend(backend):
    selection = ingest_backend.resolve_ingest_backend(env={}, config={"ingest": {"backend": backend}})
    assert selection.backend == backend
    assert selection.source == "config"
    assert selection.model_command == ingest_backend.BACKEND_COMMANDS[backend]
    argv = ingest_backend.builtin_model_argv(selection.model_command)
    assert argv[0] == sys.executable and Path(argv[1]).is_absolute()


@pytest.mark.parametrize("value", [None, [], "codex", {"backend": None}, {"backend": []},
                                    {"backend": "auto"}, {"backend": "CODEX"}, {"environment": "codex"}])
def test_bad_ingest_config_is_not_silently_normalized(value):
    with pytest.raises(ValueError):
        ingest_backend.resolve_ingest_backend("custom", config={"ingest": value}, env={})


def test_selection_precedence_and_legacy_defaults():
    config = {"ingest": {"backend": "gemini"}}
    env = {"VECTOR_LAKE_RUNNER_MODEL_CMD": "external seam"}
    assert ingest_backend.resolve_ingest_backend("explicit seam", env=env, config=config).source == "cli"
    assert ingest_backend.resolve_ingest_backend(env=env, config=config).source == "environment"
    assert ingest_backend.resolve_ingest_backend(" ", env={"VECTOR_LAKE_RUNNER_MODEL_CMD": " "}, config=config).backend == "gemini"
    assert ingest_backend.resolve_ingest_backend(env={}, config={}).backend == "pi"
    assert ingest_backend.resolve_ingest_backend(env={}, config={}).source == "default"
    assert ingest_backend.resolve_ingest_backend("custom", env={}, config={}).backend == "custom"


def test_all_startup_paths_share_config_loader(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text('{"ingest": {"backend": "codex"}}', encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ingest_runner_service.py").touch()
    selection = ingest_backend.resolve_ingest_backend(env={}, repo_root=tmp_path)
    plan = runner_supervision.plan_runner_autostart({}, tmp_path)
    assert plan.backend == selection.backend == "codex"
    assert plan.model_command == selection.model_command
    assert plan.argv[plan.argv.index("--model-source") + 1] == "config"
    config.write_text('{"ingest": {"backend": "bogus"}}', encoding="utf-8")
    with pytest.raises(ValueError):
        wiki_utils.load_config(repo_root=tmp_path)
    with pytest.raises(ValueError):
        runner_supervision.plan_runner_autostart({}, tmp_path)


def test_adoption_detects_mismatch_and_unknown_legacy_state():
    plan = runner_supervision.RunnerPlan(True, "test", backend="codex",
                                         model_command=ingest_backend.BACKEND_COMMANDS["codex"])
    report = ingest_backend.supervisor_selection_status(plan, {"model_command": ingest_backend.BACKEND_COMMANDS["pi"]})
    assert report == {"requested_backend": "codex", "effective_backend": "pi", "configuration_mismatch": True}
    assert ingest_backend.supervisor_selection_status(plan, {})["configuration_mismatch"] is None
    assert ingest_backend.supervisor_selection_status(plan, {"model_command": plan.model_command})["configuration_mismatch"] is False


def test_builtin_execution_uses_argv_but_custom_command_contract_is_preserved(monkeypatch):
    invocations = []
    payload = '{"files_written": [], "integration": {"disposition": "rejected", "reason": "Out of scope"}}'

    def run(command, **kwargs):
        invocations.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, payload, "")

    monkeypatch.setattr(ingest_runner, "run_contained", run)
    assert ingest_runner.run_model({}, ingest_backend.BACKEND_COMMANDS["codex"])[1] == ""
    assert isinstance(invocations[-1][0], list) and invocations[-1][1]["shell"] is False
    assert ingest_runner.run_model({}, "user custom seam")[1] == ""
    assert invocations[-1][0] == "user custom seam" and invocations[-1][1]["shell"] is True


@pytest.mark.parametrize("module", [ingest_runner, ingest_runner_service])
def test_unavailable_backend_fails_before_claim_or_child_start(module, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["test", "--no-shadow"])
    monkeypatch.delenv("VECTOR_LAKE_RUNNER_MODEL_CMD", raising=False)
    monkeypatch.setattr(module, "check_ingest_backend", lambda selection: (_ for _ in ()).throw(RuntimeError("missing CLI")))
    if module is ingest_runner:
        monkeypatch.setattr(module, "process_once", lambda *a: pytest.fail("must not claim tasks"))
    else:
        monkeypatch.setattr(module, "start_contained_python", lambda *a, **k: pytest.fail("must not start runner"))
    assert module.main() == 3
    assert "missing CLI" in capsys.readouterr().err


def _packet(memory: Path) -> dict:
    raw = memory / "raw" / "source.md"
    raw.write_text("Public test source: ingestion protocol example.", encoding="utf-8")
    return {"prompt": "INGEST RULES", "metadata": {
        "processed_data": {"filepath": str(raw), "hash": calculate_hash(str(raw)),
                           "canonical_name": "Source_example.md", "integration_candidates": [],
                           "instructions": "discard duplicate instruction field"},
        "output_contract": "CONTRACT-MARKER"}}


def test_tool_free_prompt_carries_source_snapshot_and_repair(isolated_memory):
    packet = _packet(isolated_memory)
    packet["metadata"]["repair"] = {"validation_error": "confidence must be numeric", "previous_output": {"bad": True}}
    prompt = build_cli_prompt(packet)
    assert "Public test source" in prompt and '"integration_candidates": []' in prompt
    assert "confidence must be numeric" in prompt and '"bad": true' in prompt
    assert "discard duplicate instruction field" not in prompt
    assert prompt.endswith("CONTRACT-MARKER")


def test_source_drift_and_escape_do_not_reach_model(isolated_memory):
    packet = _packet(isolated_memory)
    Path(packet["metadata"]["processed_data"]["filepath"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        build_cli_prompt(packet)
    packet["metadata"]["processed_data"]["filepath"] = str(isolated_memory / "private.md")
    with pytest.raises(ValueError, match="escaped"):
        build_cli_prompt(packet)


@pytest.mark.parametrize("field", ["processed_data", "output_contract"])
def test_incomplete_packet_is_a_model_failure(isolated_memory, field):
    packet = _packet(isolated_memory)
    del packet["metadata"][field]
    with pytest.raises(ValueError):
        build_cli_prompt(packet)


def test_shared_output_gate_preserves_rejection_semantics():
    invalid = {"files_written": [], "integration": {"disposition": "rejected", "reason": "missing candidate manifest"}}
    assert extract_result(json.dumps(invalid))[0] is None
    invalid["integration"]["reason"] = "Privacy: publication authorization is absent, candidate manifest is present."
    assert extract_result(json.dumps(invalid))[0] is None
    assert extract_result("```json\n{}\n```")[0] is None
    assert result_schema()["additionalProperties"] is False


def _fake_cli(tmp_path: Path, backend: str, monkeypatch, payload: dict | None = None) -> Path:
    directory = tmp_path / backend
    directory.mkdir()
    result_path = directory / "payload.json"
    result_path.write_text(json.dumps(payload or {"files_written": [{"filename": "Source_example.md", "content": "# Public test"}],
                                               "integration": {"disposition": "standalone", "reason": "Test source", "relations": []}}), encoding="utf-8")
    record = directory / "invocation.json"
    program = directory / "fake_cli.py"
    program.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        f"backend={backend!r}\nfeatures={sorted(ingest_cli.CODEX_REQUIRED_FEATURES)!r}\n"
        "if '--help' in sys.argv:\n print('--sandbox --ephemeral --output-schema --output-last-message --output-format --extensions --policy --prompt'); sys.exit(0)\n"
        "if sys.argv[1:]==['features','list']:\n print('\\n'.join(f'{key} stable true' for key in features+['secret_auth_storage'])); sys.exit(0)\n"
        "prompt=sys.stdin.read()\n"
        "assert 'HOST-SUPPLIED CONTEXT' in prompt\n"
        f"Path({str(record)!r}).write_text(json.dumps({{'argv':sys.argv,'cwd':os.getcwd(),'prompt':prompt,"
        "'settings':json.loads(Path(os.environ['GEMINI_CLI_SYSTEM_SETTINGS_PATH']).read_text()) if backend=='gemini' else None}),encoding='utf-8')\n"
        f"payload=Path({str(result_path)!r}).read_text(encoding='utf-8')\n"
        "if backend=='codex':\n Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(payload,encoding='utf-8'); print('diagnostic event, not the answer')\n"
        "else:\n print(json.dumps({'response':payload,'stats':{}}))\n",
        encoding="utf-8",
    )
    launcher = directory / ("fake.cmd" if os.name == "nt" else "fake")
    if os.name == "nt":
        launcher.write_text(f'@echo off\n"{sys.executable}" "{program}" %*\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!{sys.executable}\nexec(compile(open({str(program)!r}).read(),{str(program)!r},"exec"))\n', encoding="utf-8")
        launcher.chmod(0o700)
    monkeypatch.setenv(f"VECTOR_LAKE_RUNNER_{backend.upper()}_BIN", str(launcher))
    return record


@pytest.mark.parametrize("backend", ["codex", "gemini"])
def test_real_subprocess_seam_protocol_and_cleanup(backend, isolated_memory, tmp_path, monkeypatch):
    packet = _packet(isolated_memory)
    record = _fake_cli(tmp_path, backend, monkeypatch)
    result, error = ingest_runner.run_model(packet, ingest_backend.BACKEND_COMMANDS[backend])
    assert not error and result["files_written"][0]["filename"] == "Source_example.md"
    invocation = json.loads(record.read_text(encoding="utf-8"))
    argv = invocation["argv"]
    assert not Path(invocation["cwd"]).exists(), "task-local output/settings must be cleaned"
    if backend == "codex":
        assert "read-only" in argv and "--ephemeral" in argv
        assert "mcp_servers={}" in argv and "features.shell_tool=false" in argv
        assert "features.secret_auth_storage=false" not in argv
        assert "features.skip_host_skill_discovery=true" in argv
    else:
        settings = invocation["settings"]
        assert settings["admin"]["mcp"]["enabled"] is False
        assert settings["hooksConfig"]["enabled"] is False
        assert settings["tools"]["core"] == ["__vector_lake_no_tools__"]
        assert "--policy" in argv and argv[argv.index("--extensions") + 1] == "none"


@pytest.mark.parametrize("backend", ["codex", "gemini"])
@pytest.mark.parametrize("script", ["ingest_runner.py", "ingest_runner_service.py"])
def test_fresh_check_entrypoints_never_claim_or_write_status(backend, script, isolated_memory, tmp_path, monkeypatch):
    _fake_cli(tmp_path, backend, monkeypatch)
    monkeypatch.setenv("VECTOR_LAKE_RUNNER_MODEL_CMD", ingest_backend.BACKEND_COMMANDS[backend])
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / script), "--check"], cwd=str(tmp_path),
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["effective_backend"] == backend and report["selection_source"] == "environment"
    assert report["authentication_verified"] is False
    assert not (isolated_memory / "wiki" / ".meta" / "runtime").exists()


def test_missing_cli_has_no_implicit_service_fallback(monkeypatch):
    monkeypatch.setattr(ingest_backend.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="unavailable"):
        ingest_cli.check_cli("gemini")


def test_unsupported_cli_is_rejected_before_model_call(monkeypatch):
    monkeypatch.setattr(ingest_cli, "find_cli_binary", lambda name: "fake-cli")
    calls = []
    def probe(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "old unsupported CLI", "")
    monkeypatch.setattr(ingest_cli, "run_contained", probe)
    with pytest.raises(RuntimeError, match="lacks required"):
        ingest_cli.check_cli("codex")
    assert len(calls) == 1


def test_deadline_is_shared_with_capability_probes(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC", str(time.monotonic() - 1))
    monkeypatch.setattr(ingest_cli, "find_cli_binary", lambda name: "fake")
    monkeypatch.setattr(ingest_cli, "run_contained", lambda *a, **k: pytest.fail("expired deadline must not launch CLI"))
    with pytest.raises(RuntimeError, match="deadline exhausted"):
        ingest_cli.invoke_cli(_packet(isolated_memory), "codex")


@pytest.mark.parametrize("backend", ["codex", "gemini"])
def test_real_queue_claim_model_subprocess_and_finalizer(backend, isolated_memory, tmp_path, monkeypatch):
    from vector_lake import db_store, ingest_worker, native_llm, tool_ingest
    from tests.test_ingest_manifest import _raw_and_candidate, _relation
    from tests.test_mutation_coordinator import _source_content

    task_root = tmp_path / "task-packets"
    task_root.mkdir()
    monkeypatch.setattr(native_llm, "_task_root", lambda: task_root)
    _raw_and_candidate(isolated_memory)
    assert "enqueued 1" in tool_ingest.prepare_ingest_batch(batch_size=5)
    ingest_worker.process_jobs()
    tasks = json.loads(tool_ingest.claim_ingest_tasks(limit=1))
    assert len(tasks) == 1
    task = tasks[0]
    processed = task["task_packet"]["metadata"]["processed_data"]
    candidate = processed["integration_candidates"][0]
    payload = {"files_written": [{"filename": processed["canonical_name"], "content": _source_content()}],
               "integration": {"disposition": "integrated", "reason": "Test evidence supports target",
                               "relations": [_relation(candidate["target"], candidate["target_hash"],
                                                       projection=candidate["target_projection_hash"])]}}
    _fake_cli(tmp_path, backend, monkeypatch, payload)
    result, error = ingest_runner._process_task(task, False, ingest_backend.BACKEND_COMMANDS[backend], ingest_runner.raw_publication_index())
    assert result["finalized"] == 1, error
    assert result["model-failed"] == result["errors"] == 0
    assert (isolated_memory / "wiki" / processed["canonical_name"]).is_file()
    row = db_store.get_connection().execute("SELECT status FROM jobs WHERE job_id=?", (task["job_id"],)).fetchone()
    assert row["status"] == "finalized"


@pytest.mark.parametrize("backend,output,code,error", [
    ("gemini", "not json", 0, "envelope is not JSON"),
    ("gemini", '{"error":{"code":"AUTH"},"response":"{}"}', 0, "returned an error"),
    ("gemini", '{"response":"```json\\n{}\\n```"}', 0, "child JSON invalid"),
    ("gemini", '{}', 0, "omitted its final response"),
    ("codex", "", 0, "no final message"),
    ("codex", "", 1, "exited 1"),
    ("gemini", "", 1, "exited 1"),
])
def test_cli_failures_are_not_empty_or_strategic_rejections(backend, output, code, error,
                                                           isolated_memory, monkeypatch):
    capabilities = ingest_cli.CliCapabilities("fake", tuple(ingest_cli.CODEX_REQUIRED_FEATURES))
    monkeypatch.setattr(ingest_cli, "check_cli", lambda *a, **k: capabilities)
    monkeypatch.setattr(ingest_cli, "find_cli_binary", lambda *_: capabilities.binary)
    monkeypatch.setattr(ingest_cli, "run_contained", lambda argv, **k: subprocess.CompletedProcess(argv, code, output, "secret-do-not-echo"))
    with pytest.raises((RuntimeError, ValueError), match=error) as caught:
        ingest_cli.invoke_cli(_packet(isolated_memory), backend)
    assert "secret-do-not-echo" not in str(caught.value)


def test_cli_timeout_propagates_without_allocating_another_budget(isolated_memory, monkeypatch):
    capabilities = ingest_cli.CliCapabilities("fake", tuple(ingest_cli.CODEX_REQUIRED_FEATURES))
    monkeypatch.setattr(ingest_cli, "check_cli", lambda *a, **k: capabilities)
    monkeypatch.setattr(ingest_cli, "find_cli_binary", lambda *_: capabilities.binary)
    calls = []
    def timeout(argv, **kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(ingest_cli, "run_contained", timeout)
    monkeypatch.setenv("VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC", str(time.monotonic() + 2))
    with pytest.raises(subprocess.TimeoutExpired):
        ingest_cli.invoke_cli(_packet(isolated_memory), "gemini")
    assert len(calls) == 1 and 0 < calls[0] <= 2


@pytest.mark.parametrize("deadline", ["bad", "nan", "inf", "-inf", "0"])
def test_bad_deadline_does_not_launch_cli(deadline, isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC", deadline)
    monkeypatch.setattr(ingest_cli, "find_cli_binary", lambda name: "fake")
    monkeypatch.setattr(ingest_cli, "run_contained", lambda *a, **k: pytest.fail("bad deadline must not launch CLI"))
    with pytest.raises((RuntimeError, ValueError)):
        ingest_cli.invoke_cli(_packet(isolated_memory), "codex")


def test_unreadable_source_is_not_reported_as_empty_data(isolated_memory, monkeypatch):
    packet = _packet(isolated_memory)
    def denied(path):
        raise PermissionError("test permission denied")
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PermissionError, match="test permission denied"):
        build_cli_prompt(packet)


# Independent-review contracts VL-INGEST-001/002; all locks and records are temporary.
def test_direct_runner_cannot_consume_or_overwrite_a_locked_owner(isolated_memory, monkeypatch, capsys):
    from filelock import FileLock
    runtime = wiki_utils.get_meta_dir() / "runtime"
    runtime.mkdir(parents=True)
    lock = FileLock(str(runtime / ".runner_consumer.lock"))
    with lock:
        monkeypatch.setattr(sys, "argv", ["runner", "--once", "--no-shadow"])
        monkeypatch.setattr(ingest_runner, "check_ingest_backend", lambda selection: {})
        monkeypatch.setattr(ingest_runner, "write_status", lambda **k: pytest.fail("loser overwrote owner status"))
        monkeypatch.setattr(ingest_runner, "process_once", lambda *a: pytest.fail("loser claimed a task"))
        assert ingest_runner.main() == 4
    assert "configuration_mismatch" in capsys.readouterr().out
    assert not (runtime / "runner.pid").exists()


def test_manual_supervisor_conflict_reports_actual_backend(isolated_memory, monkeypatch, capsys):
    from filelock import FileLock
    runtime = wiki_utils.get_meta_dir() / "runtime"
    runtime.mkdir(parents=True)
    record = {"pid": os.getpid(), "status": "running", "model_command": ingest_backend.BACKEND_COMMANDS["pi"]}
    path = runtime / "runner_supervisor.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    original = path.read_bytes()
    with FileLock(str(runtime / ".runner_service.lock")):
        monkeypatch.setattr(sys, "argv", ["service", "--model-cmd", ingest_backend.BACKEND_COMMANDS["codex"]])
        monkeypatch.setattr(ingest_runner_service, "start_contained_python", lambda *a, **k: pytest.fail("loser spawned child"))
        assert ingest_runner_service.main() == 0
    output = capsys.readouterr().out
    assert '"effective_backend": "pi"' in output
    assert '"configuration_mismatch": true' in output
    assert path.read_bytes() == original


@pytest.mark.parametrize("module,legacy_name,filename", [
    (ingest_runner, "STATUS_PATH", "runner_status.json"),
    (ingest_runner_service, "SUPERVISOR_STATUS", "runner_supervisor.json"),
])
def test_status_write_resolves_current_isolated_root(module, legacy_name, filename, isolated_memory, tmp_path, monkeypatch):
    stale = tmp_path / "old-root" / filename
    monkeypatch.setattr(module, legacy_name, stale, raising=False)
    module.write_status(status="test")
    assert (wiki_utils.get_meta_dir() / "runtime" / filename).exists()
    assert not stale.exists(), "import-time root must not escape current isolation"


@pytest.mark.parametrize("record", [
    {"pid": os.getpid(), "status": "running", "model_command": ingest_backend.BACKEND_COMMANDS["codex"]},
    {"pid": os.getpid(), "status": "running"},
    {"pid": os.getpid(), "status": [], "child_pid": {}, "effective_backend": []},
    "broken-json",
])
def test_service_conflict_matches_or_reports_unknown_without_writing(record, isolated_memory, monkeypatch, capsys):
    from filelock import FileLock
    runtime = wiki_utils.get_meta_dir() / "runtime"
    runtime.mkdir(parents=True)
    path = runtime / "runner_supervisor.json"
    path.write_text(json.dumps(record) if isinstance(record, dict) else record, encoding="utf-8")
    original = path.read_bytes()
    with FileLock(str(runtime / ".runner_service.lock")):
        monkeypatch.setattr(sys, "argv", ["service", "--model-cmd", ingest_backend.BACKEND_COMMANDS["codex"]])
        monkeypatch.setattr(ingest_runner_service, "start_contained_python", lambda *a, **k: pytest.fail("must not spawn"))
        assert ingest_runner_service.main() == 0
    output = capsys.readouterr().out
    expected = "false" if isinstance(record, dict) and record.get("model_command") else "null"
    assert f'"configuration_mismatch": {expected}' in output
    assert path.read_bytes() == original


def test_same_parent_duplicate_child_must_take_consumer_lock(isolated_memory, monkeypatch, capsys):
    from filelock import FileLock
    runtime = wiki_utils.get_meta_dir() / "runtime"
    runtime.mkdir(parents=True)
    with FileLock(str(runtime / ".runner_service.lock")), FileLock(str(runtime / ".runner_consumer.lock")):
        monkeypatch.setattr(sys, "argv", ["runner", "--once"])
        monkeypatch.setattr(ingest_runner, "is_supervisor_child", lambda: True)
        monkeypatch.setattr(ingest_runner, "write_status", lambda **k: pytest.fail("must not write"))
        assert ingest_runner.main() == 4
    assert "consumer already running" in capsys.readouterr().out


def _owner_entries(tmp_path):
    worker = tmp_path / "worker-entry.py"
    release = tmp_path / "release"
    markers = tmp_path / "model-calls"
    markers.mkdir()
    worker.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(REPO)!r})\nfrom scripts import ingest_runner as runner\n"
        "runner.check_ingest_backend=lambda selection:{}\n"
        "def hold_model(limit,shadow,command,stats,concurrency,**kwargs):\n"
        f" Path({str(markers)!r},str(os.getpid())+'.json').write_text(json.dumps({{'pid':os.getpid(),'command':command}}),encoding='utf-8')\n"
        " deadline=time.monotonic()+20\n"
        f" while not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(0.02)\n"
        " return {'claimed':0,'finalized':0,'needs-model':0}\n"
        "runner.process_once=hold_model\nraise SystemExit(runner.main())\n", encoding="utf-8",
    )
    service = tmp_path / "service-entry.py"
    service.write_text(
        "import sys\n"
        f"sys.path.insert(0,{str(REPO)!r})\nfrom scripts import ingest_runner_service as service\n"
        "service.check_ingest_backend=lambda selection:{}\nservice.SUPERVISOR_HEARTBEAT_SECONDS=1\n"
        "launch=service.start_contained_python\n"
        "def worker_start(argv,**kwargs):\n"
        f" argv=list(argv); argv[1]={str(worker)!r}; return launch(argv,**kwargs)\n"
        "service.start_contained_python=worker_start\nraise SystemExit(service.main())\n", encoding="utf-8",
    )
    return worker, service, markers, release


def _wait_for(predicate, processes, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"owned-process handshake timed out; exit codes {[p.poll() for p in processes]}")


@pytest.mark.parametrize("order", ["service-first", "direct-first", "simultaneous"])
def test_process_startup_orders_have_one_consumer_and_preserve_winner(order, isolated_memory, tmp_path):
    from vector_lake.process_control import start_contained_python, stop_contained
    worker, service, markers, release = _owner_entries(tmp_path)
    env = dict(os.environ)
    env.pop("VECTOR_LAKE_RUNNER_MODEL_CMD", None)
    processes = []
    kinds = []
    def launch(kind):
        entry = worker if kind == "direct" else service
        backend = "codex" if kind == "direct" else "pi"
        argv = [sys.executable, str(entry), "--model-cmd", ingest_backend.BACKEND_COMMANDS[backend], "--no-shadow"]
        process = start_contained_python(argv, env=env, cwd=str(tmp_path),
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(process)
        kinds.append(kind)
        return process
    try:
        first_kind = "direct" if order == "direct-first" else "service"
        launch(first_kind)
        if order != "simultaneous":
            _wait_for(lambda: list(markers.glob("*.json")), processes)
        launch("service" if first_kind == "direct" else "direct")
        _wait_for(lambda: list(markers.glob("*.json")), processes)
        loser_index = _wait_for(lambda: next((i + 1 for i,p in enumerate(processes) if p.poll() is not None), None), processes) - 1
        loser = processes[loser_index]
        stdout, stderr = loser.communicate(timeout=5)
        assert loser.returncode == (4 if kinds[loser_index] == "direct" else 0), stderr
        assert "configuration_mismatch" in stdout
        calls = list(markers.glob("*.json"))
        assert len(calls) == 1, "only the locked consumer may enter the model callback"
        owner = json.loads(calls[0].read_text())
        runtime = wiki_utils.get_meta_dir() / "runtime"
        status = json.loads((runtime / "runner_status.json").read_text())
        assert status["pid"] == owner["pid"]
        assert status["resolved_model_command"] == owner["command"]
        assert int((runtime / "runner.pid").read_text()) == owner["pid"]
        assert processes[1-loser_index].poll() is None
        if order != "simultaneous":
            assert '"configuration_mismatch": true' in stdout
            assert owner["command"] == ingest_backend.BACKEND_COMMANDS["pi" if first_kind == "service" else "codex"]
    finally:
        for process in processes:
            if process.poll() is None:
                stop_contained(process)
            process.wait(timeout=5)


def test_live_legacy_pid_blocks_new_entry_without_overwriting(isolated_memory, monkeypatch, tmp_path, capsys):
    from vector_lake.process_control import start_contained_python, stop_contained
    sleeper = tmp_path / "owned-legacy.py"
    sleeper.write_text("import time\ntime.sleep(20)\n", encoding="utf-8")
    process = start_contained_python([sys.executable, str(sleeper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    runtime = wiki_utils.get_meta_dir() / "runtime"
    runtime.mkdir(parents=True)
    pidfile = runtime / "runner.pid"
    pidfile.write_text(str(process.pid), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["runner", "--once"])
    monkeypatch.setattr(ingest_runner, "write_status", lambda **k: pytest.fail("must not overwrite old owner"))
    try:
        assert ingest_runner.main() == 4
        assert pidfile.read_text() == str(process.pid)
        assert "legacy runner is still live" in capsys.readouterr().out
    finally:
        stop_contained(process)
        process.wait(timeout=5)


# VL-INGEST-003: hostile/corrupt numeric fields never reach a process probe.
@pytest.mark.parametrize("token", ["1e999", "NaN", "184467440737095516160", "-1", "0", "true"])
@pytest.mark.parametrize("entry", ["service", "direct", "watchdog"])
def test_invalid_json_owner_pid_reports_unknown_without_writing(token, entry, isolated_memory, monkeypatch, capsys):
    from filelock import FileLock
    runtime = wiki_utils.get_meta_dir() / "runtime"
    runtime.mkdir(parents=True)
    path = runtime / "runner_supervisor.json"
    path.write_text('{"status":"running","pid":' + token + '}', encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(ingest_backend, "pid_is_live", lambda pid: pytest.fail("invalid PID reached OS probe"))
    if entry == "watchdog":
        assert runner_supervision._adopted_supervisor_pid() is None
        assert runner_supervision._adopted_supervisor_record().get("pid") is None
    else:
        module = ingest_runner_service if entry == "service" else ingest_runner
        monkeypatch.setattr(sys, "argv", [entry])
        with FileLock(str(runtime / ".runner_service.lock")):
            assert module.main() == (0 if entry == "service" else 4)
        output = capsys.readouterr().out
        assert '"effective_backend": "unknown"' in output
        assert '"configuration_mismatch": null' in output
    assert path.read_bytes() == original
    assert not (runtime / "runner.pid").exists()
    assert not (runtime / "runner_status.json").exists()


@pytest.mark.parametrize("value", ["1e999", "184467440737095516160", "-1", "0", "True"])
def test_invalid_legacy_pid_file_fails_closed_without_os_probe(value, isolated_memory, monkeypatch, capsys):
    runtime = wiki_utils.get_meta_dir() / "runtime"
    runtime.mkdir(parents=True)
    path = runtime / "runner.pid"
    path.write_text(value, encoding="utf-8")
    monkeypatch.setattr(ingest_backend, "pid_is_live", lambda pid: pytest.fail("invalid PID reached OS probe"))
    monkeypatch.setattr(sys, "argv", ["runner", "--once"])
    monkeypatch.setattr(ingest_runner, "write_status", lambda **k: pytest.fail("invalid legacy owner must not overwrite state"))
    assert ingest_runner.main() == 3
    assert "startup blocked" in capsys.readouterr().err and path.read_text() == value


def test_watchdog_fallback_validates_pid_even_without_active_status(isolated_memory, monkeypatch):
    runtime = wiki_utils.get_meta_dir() / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "runner_supervisor.json").write_text('{"pid":1e999}', encoding="utf-8")
    monkeypatch.setattr(ingest_backend, "pid_is_live", lambda pid: pytest.fail("invalid PID reached OS probe"))
    assert runner_supervision._adopted_supervisor_pid() is None


@pytest.mark.parametrize("family,maximum", [("nt", (1 << 32) - 1), ("posix", (1 << 31) - 1)])
def test_pid_platform_bounds_are_checked_before_native_probe(family, maximum, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(ingest_backend, "os", SimpleNamespace(name=family,
                        kill=lambda *a: pytest.fail("out-of-range PID reached native probe")))
    assert ingest_backend.normalize_pid(maximum) == maximum
    assert ingest_backend.normalize_pid(str(maximum)) == maximum
    for value in (maximum + 1, str(maximum + 1), 0, -1, True, float("inf"), float("nan"), 1.5):
        with pytest.raises(ValueError, match="PID"):
            ingest_backend.pid_is_live(value)


def test_normal_and_dead_owned_pid_probes(tmp_path):
    from vector_lake.process_control import start_contained_python, stop_contained
    entry = tmp_path / "pid-probe-owner.py"
    entry.write_text("import time\ntime.sleep(20)\n", encoding="utf-8")
    process = start_contained_python([sys.executable, str(entry)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert ingest_backend.normalize_pid(str(process.pid)) == process.pid
        assert ingest_backend.pid_is_live(process.pid) is True
    finally:
        stop_contained(process)
        process.wait(timeout=5)
    assert ingest_backend.pid_is_live(process.pid) is False
