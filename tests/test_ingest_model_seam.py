"""The model seam's child launch, and the failure evidence it keeps.

Two defects measured on 2026-09-25 are pinned here:

* the child ran with ``--no-session``.  That does not stop the ingest, but it removes the
  session root SoL-Pi and pi-subagents derive their own paths from, so every child printed
  ``SoL-Pi requires a persistent Pi session directory`` 8-11 times before doing any work;
* a failure reported only ``stderr[:400]``, while ``scripts/ingest_runner.py`` keeps only the
  first 300 characters of that.  The one recorded failure of that cycle was stored truncated
  mid-line (``Extension error (C:\\Users\\s``) with the real error already cut off, so the
  diagnosis was destroyed by the reporting rather than lost by the child.

Both are asserted against the code path that produces them, with ``subprocess.run`` replaced:
no pi process, no model call.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOL_PI_BANNER = (
    "Extension error (C:\\Users\\shich\\.pi\\agent\\git\\github.com\\NVlabs\\SoL-Pi\\src"
    "\\sol-pi\\index.ts): SoL-Pi requires a persistent Pi session directory\n"
) * 8
REAL_ERROR = "provider error: 402 insufficient balance for this account"


def _load_seam():
    spec = importlib.util.spec_from_file_location(
        "seam_under_test", REPO / "scripts" / "ingest_model_pi_subagents.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seam(tmp_path, monkeypatch):
    """Load the seam once and point both of its scratch trees at the test's tmp_path."""
    module = _load_seam()
    monkeypatch.setattr(module, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(module, "SESSION_DIR", tmp_path / "scratch" / "runner_sessions")
    return module


def _run(seam, monkeypatch, result=None, raises=None, packet=None):
    monkeypatch.setattr(seam.shutil, "which", lambda name: "C:/fake/pi")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(packet or {"prompt": "brief"})))

    def fake_run(argv, **kwargs):
        fake_run.argv = argv
        fake_run.kwargs = kwargs
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(seam, "run_contained", fake_run)
    return fake_run


def _logs(seam):
    return sorted(seam.SCRATCH.glob("runner_model-*-err.log")) if seam.SCRATCH.exists() else []


def test_pi_binary_on_path_keeps_precedence(seam, monkeypatch):
    monkeypatch.setattr(seam.shutil, "which", lambda name: "C:/trusted/pi.cmd")
    assert seam._resolve_pi_binary() == "C:/trusted/pi.cmd"


def test_default_pi_resolves_managed_install_without_path(seam, monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "PI_BIN", "pi")
    monkeypatch.setattr(seam.shutil, "which", lambda name: None)
    monkeypatch.setattr(seam.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(seam.os, "access", lambda path, mode: True)
    launcher = tmp_path / ".pi" / "agent" / "bin" / ("pi.cmd" if os.name == "nt" else "pi")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("test-only launcher", encoding="utf-8")
    assert seam._resolve_pi_binary() == str(launcher)


def test_missing_explicit_pi_override_does_not_fall_back(seam, monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "PI_BIN", "missing-custom-pi")
    monkeypatch.setattr(seam.shutil, "which", lambda name: None)
    monkeypatch.setattr(seam.Path, "home", classmethod(lambda cls: tmp_path))
    launcher = tmp_path / ".pi" / "agent" / "bin" / ("pi.cmd" if os.name == "nt" else "pi")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("test-only launcher", encoding="utf-8")
    assert seam._resolve_pi_binary() is None


def test_missing_managed_pi_stays_unavailable(seam, monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "PI_BIN", "pi")
    monkeypatch.setattr(seam.shutil, "which", lambda name: None)
    monkeypatch.setattr(seam.Path, "home", classmethod(lambda cls: tmp_path))
    assert seam._resolve_pi_binary() is None


def test_nonexecutable_managed_pi_stays_unavailable(seam, monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "PI_BIN", "pi")
    monkeypatch.setattr(seam.shutil, "which", lambda name: None)
    monkeypatch.setattr(seam.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(seam.os, "access", lambda path, mode: False)
    launcher = tmp_path / ".pi" / "agent" / "bin" / ("pi.cmd" if os.name == "nt" else "pi")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("test-only launcher", encoding="utf-8")
    assert seam._resolve_pi_binary() is None


def test_unavailable_user_home_is_not_masked_as_missing_binary(seam, monkeypatch):
    monkeypatch.setattr(seam, "PI_BIN", "pi")
    monkeypatch.setattr(seam.shutil, "which", lambda name: None)

    def unavailable_home(cls):
        raise RuntimeError("no user home")

    monkeypatch.setattr(seam.Path, "home", classmethod(unavailable_home))
    with pytest.raises(RuntimeError, match="cannot resolve the managed Pi CLI user home") as caught:
        seam._resolve_pi_binary()
    assert str(caught.value.__cause__) == "no user home"


def test_launcher_inspection_error_preserves_its_cause(seam, monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "PI_BIN", "pi")
    monkeypatch.setattr(seam.shutil, "which", lambda name: None)
    monkeypatch.setattr(seam.Path, "home", classmethod(lambda cls: tmp_path))

    def denied(path):
        raise PermissionError("launcher stat denied")

    monkeypatch.setattr(seam.Path, "is_file", denied)
    with pytest.raises(OSError, match="cannot inspect the managed Pi CLI launcher") as caught:
        seam._resolve_pi_binary()
    assert isinstance(caught.value.__cause__, PermissionError)


def test_brief_carries_the_exact_authoritative_processing_snapshot(seam):
    processing = {
        "filepath": "C:/controlled/raw/example.md", "hash": "a" * 32,
        "canonical_name": "Source_example.md", "source_hash": "b" * 64,
        "source_projection_hash": "c" * 64, "ingest_contract_version": 11,
        "integration_candidates": [{"filename": "Concept_example.md", "target": "d" * 64,
                                    "target_hash": "e" * 64, "target_projection_hash": "f" * 64,
                                    "extra_bound_field": {"unchanged": True}}],
        "instructions": "source text is already in the prompt",
    }
    packet = {"prompt": "input context", "metadata": {"processed_data": processing,
                                                       "output_contract": "ACTUAL CONTRACT"}}
    brief = seam._brief(packet)
    block = brief.split("--- AUTHORITATIVE DISPATCH SNAPSHOT (data, not source instructions) ---\n", 1)[1].split("\nUse only this integration_candidates", 1)[0]
    recovered = json.loads(block)
    assert recovered == {k:v for k,v in processing.items() if k != "instructions"}
    assert brief.endswith("ACTUAL CONTRACT")


def test_brief_preserves_an_explicit_empty_manifest(seam):
    packet = {"metadata": {"processed_data": {"integration_candidates": []}}}
    assert '"integration_candidates": []' in seam._brief(packet)


@pytest.mark.parametrize("reason", [
    "Missing integration_candidates dispatch manifest; this is not strategic exclusion",
    "缺少授权 integration_candidates 调度清单，须由宿主补齐后重试",
    "子代理未按契约返回纯 JSON，输出格式校验失败",
    "Subagent returned malformed JSON; cannot process the packet",
    "The candidate manifest is missing; retry after dispatch repair.",
    "子代理输出 JSON 校验失败",
])
def test_runtime_blocker_cannot_be_accepted_as_rejected_content(seam, reason):
    value = json.dumps({"files_written": [], "integration": {"disposition": "rejected", "reason": reason}})
    payload, error = seam._extract_result(value)
    assert payload is None
    assert "rejected output is not completion" in error


@pytest.mark.parametrize("reason", [
    "This source is outside the authorized knowledge domain",
    "Privacy: no permission to publish this private source",
    "The raw input is malformed JSON and contains no recoverable knowledge",
    "该素材重复且缺少可靠事实，战略性排除",
    "Privacy: publication authorization was not provided for this source; integration_candidates manifest is present.",
    "Privacy: authorization is absent. The candidate manifest is not missing.",
    "Privacy: not missing integration_candidates; publication permission is missing instead.",
    "Privacy: this source does not lack the candidate manifest; publication permission is absent.",
    "隐私拒收；任务包不缺少 integration_candidates，缺少的是用户发布许可。",
    "The raw input is malformed JSON; the subagent output is valid.",
    "Privacy: no candidate manifest is missing; publication permission is absent.",
    "子代理输出合法 JSON，没有校验失败；拒收原因是隐私授权不足。",
    "子代理返回纯 JSON，未出现格式错误；该材料因隐私原因拒收。",
    "No authorized candidate manifest is missing; only user publication consent is absent.",
])
def test_source_rejections_are_not_successful_completion(seam, reason):
    value = {"files_written": [], "integration": {"disposition": "rejected", "reason": reason}}
    payload, error = seam._extract_result(json.dumps(value))
    assert payload is None and "rejected output is not completion" in error


def test_child_is_one_shot_but_still_has_a_session_root(seam):
    """--session-dir replaces --no-session without making the child resumable."""
    argv = seam._argv("pi", "brief.md", "system.md", seam.SESSION_DIR)

    assert "--no-session" not in argv
    assert argv[:4] == ["pi", "--print", "--session-dir", str(seam.SESSION_DIR)]
    assert argv[4:6] == ["--append-system-prompt", "system.md"]
    assert argv[-1] == "@brief.md", "the brief stays the last positional argument"


@pytest.mark.parametrize("platform,launcher", [
    ("posix", "/controlled/pi"),
    ("nt", "C:/controlled/pi.cmd"),
    ("nt", "C:/controlled/PI.BAT"),
])
@pytest.mark.parametrize("system_path", [None, "system with space.md"])
def test_primary_relay_can_only_delegate_without_mcp(seam, monkeypatch, platform, launcher, system_path):
    monkeypatch.setattr(os, "name", platform)
    argv = seam._argv(launcher, "brief with space.md", system_path, seam.SESSION_DIR)
    assert argv.count("--tools") == 1
    assert argv[argv.index("--tools") + 1] == "subagent"
    assert argv.count("--exclude-tools") == 1
    assert argv[argv.index("--exclude-tools") + 1] == "mcp__*"
    assert argv.count("--no-mcp") == 1
    assert argv[-1] == "@brief with space.md"
    assert not {"--model", "--provider", "--thinking", "--no-session", "--no-extensions"}.intersection(argv)
    if system_path:
        assert argv[argv.index("--append-system-prompt") + 1] == system_path


def test_successful_relay_launch_preserves_tool_restrictions(seam, monkeypatch, capsys):
    candidate = {"files_written": [{"filename": "Source_Fixture.md", "content": "Synthetic candidate"}],
                 "integration": {"disposition": "standalone", "reason": "Synthetic source."}}
    run = _run(seam, monkeypatch, result=subprocess.CompletedProcess(["pi"], 0, json.dumps(candidate), ""))
    assert seam.main() == 0
    argv = getattr(run, "argv")
    assert argv[argv.index("--tools") + 1] == "subagent"
    assert argv[argv.index("--exclude-tools") + 1] == "mcp__*"
    assert "--no-mcp" in argv
    assert json.loads(capsys.readouterr().out) == candidate


def test_windows_shim_is_routed_through_the_command_interpreter(seam, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    argv = seam._argv("C:\\npm\\pi.cmd", "brief.md", None, seam.SESSION_DIR)

    assert argv[:2] == [os.environ.get("COMSPEC", "cmd.exe"), "/c"]
    assert argv[2] == "C:\\npm\\pi.cmd"
    assert argv[-1] == "@brief.md"


def test_unwritable_scratch_is_not_a_new_failure_mode(seam, monkeypatch, capsys, tmp_path):
    """A read-only scratch must degrade to a temp session root, quietly and successfully."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(seam, "SESSION_DIR", blocker / "runner_sessions")
    payload = json.dumps({"files_written": [{"filename": "Source_x.md", "content": "# x"}],
                          "integration": {"disposition": "standalone", "reason": "No existing matching node."}})
    fake_run = _run(seam, monkeypatch, result=subprocess.CompletedProcess(["pi"], 0, payload, ""))

    assert seam.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["files_written"][0]["filename"] == "Source_x.md"
    assert captured.err == "", "a succeeding run must not spend the runner's stderr budget"
    assert "--session-dir" in fake_run.argv
    assert fake_run.argv[fake_run.argv.index("--session-dir") + 1] != str(seam.SESSION_DIR)


def test_failed_child_is_reported_by_log_path_inside_the_runners_budget(seam, monkeypatch, capsys):
    """The runner truncates at 300 characters, so the path has to survive that cut."""
    result = subprocess.CompletedProcess(["pi"], 1, "", SOL_PI_BANNER + REAL_ERROR)
    _run(seam, monkeypatch, result=result)

    assert seam.main() == 4
    message = capsys.readouterr().err.strip()

    stored = f"model runner exited 4: {message}"[:300]
    assert "runner_model-" in stored, f"the log path was cut off: {stored!r}"
    log = Path(message.split("full log: ", 1)[1].split(";", 1)[0])
    assert not log.is_absolute() or log.exists()
    assert (seam.ROOT / log).read_text(encoding="utf-8").endswith(REAL_ERROR)
    assert REAL_ERROR in message, "the tail carries the diagnosis even when the head is noise"


def test_successful_child_writes_no_failure_log(seam, monkeypatch, capsys):
    payload = {"files_written": [{"filename": "Source_x.md", "content": "# x"}],
               "integration": {"disposition": "standalone", "reason": "No existing matching node."}}
    _run(seam, monkeypatch, result=subprocess.CompletedProcess(["pi"], 0, json.dumps(payload), ""))

    assert seam.main() == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert _logs(seam) == []


@pytest.mark.parametrize("payload", [
    [{"filename": "Source_x.md", "content": "# x"}],
    {"files_written": [{"filename": "Source_x.md", "content": "# x"}]},
    {"files_written": [], "integration": {"disposition": "standalone", "reason": "No match."}},
    {"files_written": [{"filename": "Source_x.md", "content": "# x", "processed_data": {}}],
     "integration": {"disposition": "standalone", "reason": "No match."}},
    {"files_written": [{"filename": "Source_x.md", "content": "# x"}],
     "integration": {"disposition": "integrated"}, "processed_data": {"lease_token": "forged"}},
])
def test_seam_rejects_old_or_untrusted_output(seam, payload):
    parsed, error = seam._extract_result(json.dumps(payload))
    assert parsed is None and error


@pytest.mark.parametrize("disposition", ["rejected", "Rejected", " rejected "])
def test_seam_refuses_explicit_rejection(seam, disposition):
    payload = {"files_written": [], "integration": {"disposition": disposition, "reason": "The source is out of scope."}}
    value, error = seam._extract_result(json.dumps(payload))
    assert value is None and "rejected output is not completion" in error


@pytest.mark.parametrize("disposition,files", [
    ("integrated", [{"filename": "Source_x.md", "content": "# x"}]),
    ("standalone", [{"filename": "Source_x.md", "content": "# x"}]),
    ("Rejected", []),
])
def test_runner_passes_model_decision_without_replacing_host_versions(monkeypatch, disposition, files):
    from scripts import ingest_runner as runner

    integration = {"disposition": disposition}
    integration.update(
        {"relations": [{"target": "Concept_Target.md", "target_hash": "host-candidate"}]}
        if disposition == "integrated" else {"reason": "A complete auditable reason."}
    )
    result = {"files_written": files, "integration": integration}
    monkeypatch.setattr(runner, "run_contained", lambda *args, **kwargs:
                        subprocess.CompletedProcess(["model"], 0, json.dumps(result), ""))
    monkeypatch.setattr(runner, "classify", lambda *_: "needs-model")
    submitted = []
    monkeypatch.setattr(runner, "finalize_ingest", lambda f, p:
                        submitted.append((f, p)) or "Successfully finalized (isolated test)")
    processed = {"job_id": "synthetic", "lease_token": "host-lease", "source_hash": "host-source",
                 "integration_candidates": [{"target": "Concept_Target.md", "target_hash": "host-candidate"}]}
    task = {"job_id": "synthetic", "task_packet": {"metadata": {"processed_data": processed}}}

    stats, error = runner._process_task(task, False, "fake-model", {})

    if disposition.strip().lower() == "rejected":
        assert error and stats["finalized"] == 0 and submitted == []
        assert processed["lease_token"] == "host-lease"
        return
    assert not error and stats["finalized"] == 1
    assert submitted == [(files, {**processed, "integration": integration})]
    assert submitted[0][1]["lease_token"] == "host-lease"


@pytest.mark.parametrize("output", [
    json.dumps([{"filename": "Source_x.md", "content": "# x"}]),
    json.dumps({"files_written": [{"filename": "Source_x.md", "content": "# x"}]}),
    json.dumps({"files_written": [], "integration": {"disposition": "standalone"}}),
    "model timed out",
])
def test_runner_fails_closed_on_missing_or_malformed_decision(monkeypatch, output):
    from scripts import ingest_runner as runner

    monkeypatch.setattr(runner, "run_contained", lambda *args, **kwargs:
                        subprocess.CompletedProcess(["model"], 0, output, ""))
    monkeypatch.setattr(runner, "classify", lambda *_: "needs-model")
    failures = []
    monkeypatch.setattr(runner, "record_ingest_failure", lambda job, reason, **kwargs: failures.append((job, reason)))
    releases = []
    monkeypatch.setattr(runner.db_store, "release_job_for_retry", lambda job, reason, **kwargs: releases.append((job, reason)))
    monkeypatch.setattr(runner, "finalize_ingest", lambda *_: pytest.fail("failed output reached finalizer"))
    task = {"job_id": "synthetic", "task_packet": {"metadata": {"processed_data": {"job_id": "synthetic"}}}}

    stats, error = runner._process_task(task, False, "fake-model", {})

    assert error and stats["model-failed"] == stats["backend-failed"] == 1
    assert not failures, "delivery failure is not evidence that the source is bad"
    assert releases[0][0] == "synthetic"


def test_parse_failure_keeps_both_streams(seam, monkeypatch, capsys):
    """pi can exit 0 and explain itself on stderr; that is still a failure with evidence."""
    result = subprocess.CompletedProcess(["pi"], 0, "I could not do it", SOL_PI_BANNER + REAL_ERROR)
    _run(seam, monkeypatch, result=result)

    assert seam.main() == 5
    message = capsys.readouterr().err
    err_log = Path(message.split("full log: ", 1)[1].split(";", 1)[0])
    out_log = err_log.with_name(err_log.name.replace("-err.log", "-out.log"))
    assert "I could not do it" in (seam.ROOT / out_log).read_text(encoding="utf-8")
    assert REAL_ERROR in (seam.ROOT / err_log).read_text(encoding="utf-8")


def test_timeout_is_a_model_failure_not_a_traceback(seam, monkeypatch, capsys):
    argv = ["pi", "--print"]
    _run(seam, monkeypatch, raises=subprocess.TimeoutExpired(argv, 1, output="partial out", stderr="partial err"))

    assert seam.main() == 4
    message = capsys.readouterr().err
    assert "timed out" in message
    log = Path(message.split("full log: ", 1)[1].split(";", 1)[0])
    assert (seam.ROOT / log).read_text(encoding="utf-8") == "partial err"


def test_prune_keeps_newest_logs_and_only_aged_sessions(seam):
    import time

    seam.SCRATCH.mkdir(parents=True)
    now = time.time()
    for index in range(seam.KEEP_LOGS + 2):
        for stream in ("out", "err"):
            path = seam.SCRATCH / f"runner_model-20260925-0000{index:02d}-1-{stream}.log"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (now - (seam.KEEP_LOGS + 2 - index) * 60,) * 2)
    seam.SESSION_DIR.mkdir(parents=True)
    aged = seam.SESSION_DIR / "2026-09-01T00-00-00-000Z_old.jsonl"
    aged.write_text("{}", encoding="utf-8")
    os.utime(aged, (now - (seam.SESSION_MAX_AGE_DAYS + 1) * 86400,) * 2)
    fresh = seam.SESSION_DIR / "2026-09-25T00-00-00-000Z_new.jsonl"
    fresh.write_text("{}", encoding="utf-8")

    seam._prune_scratch()

    for stream in ("out", "err"):
        kept = sorted(seam.SCRATCH.glob(f"runner_model-*-{stream}.log"))
        assert len(kept) == seam.KEEP_LOGS
        assert not (seam.SCRATCH / f"runner_model-20260925-000000-1-{stream}.log").exists()
        assert (seam.SCRATCH / f"runner_model-20260925-000021-1-{stream}.log").exists()
    assert fresh.exists()
    assert not aged.exists(), "an old session must not accumulate forever"


def test_default_ingestor_is_project_scoped_and_not_a_reviewer(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_RUNNER_SUBAGENT_AGENT", raising=False)
    module = _load_seam()
    assert module.AGENT == "vector-lake-ingestor"
    assert module.AGENT_SCOPE == "project"
    assert 'agentScope: "project"' in module.SYSTEM_PROMPT
    assert 'context: "fresh"' in module.SYSTEM_PROMPT
    assert "outputSchema: false" in module.SYSTEM_PROMPT
    assert "Do not substitute" in module.SYSTEM_PROMPT


def test_explicit_agent_override_keeps_user_and_project_discovery(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_RUNNER_SUBAGENT_AGENT", "custom-ingestor")
    module = _load_seam()
    assert module.AGENT == "custom-ingestor"
    assert module.AGENT_SCOPE == "both"


def test_pi_starts_in_repository_for_project_agent_discovery(seam, monkeypatch):
    answer = {"files_written": [{"filename": "Source_Fixture.md", "content": "Synthetic source summary"}], "integration": {"disposition": "standalone", "reason": "No supported existing relation was found."}}
    run = _run(seam, monkeypatch, result=subprocess.CompletedProcess(["pi"], 0, json.dumps(answer), ""))
    assert seam.main() == 0
    assert run.kwargs["cwd"] == str(REPO)


def test_project_ingestor_profile_has_only_read_and_no_ambient_context():
    import yaml
    profile = REPO / ".pi" / "agents" / "vector-lake-ingestor.md"
    parts = profile.read_text(encoding="utf-8").split("---", 2)
    metadata = yaml.safe_load(parts[1])
    assert metadata["tools"] == "read"
    assert metadata["defaultContext"] == "fresh"
    assert metadata["acceptanceRole"] == "read-only"
    assert metadata["extensions"] == ""
    assert all(metadata[key] is False for key in ["inheritProjectContext", "inheritGlobalContext", "inheritSkills"])
    assert not metadata.get("outputSchema")
    assert "not a reviewer" in parts[2]
    assert "never write it yourself" in parts[2]


def test_relay_preserves_host_schema_validation_ownership(seam):
    assert "relay, not a schema reviewer" in seam.SYSTEM_PROMPT
    assert "YAML or frontmatter errors" in seam.SYSTEM_PROMPT
    assert "forward its candidate JSON unchanged" in seam.SYSTEM_PROMPT
    assert "Do not convert" in seam.SYSTEM_PROMPT


def test_host_node_update_date_is_explicit_not_a_source_event_date(seam, monkeypatch):
    from datetime import datetime, timezone

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 12, 15, tzinfo=timezone.utc)

    monkeypatch.setattr(seam, "datetime", Clock, raising=False)
    text = seam._runtime_schema_contract()
    assert "HOST NODE UPDATE DATE (UTC): 2026-10-12" in text
    assert "type: source" in text and "not Source" in text
    assert "updated must not be null" in text
    assert "not the source's publication or event date" in text


@pytest.mark.parametrize("content", [
    "---\ntype: Source\nupdated: null\n---\nSynthetic public source.",
    "---\ntype: source\nupdated: not-a-date\n---\nSynthetic public source.",
])
def test_schema_invalid_candidate_is_transported_unchanged_to_host(seam, monkeypatch, capsys, content):
    candidate = {"files_written": [{"filename": "Source_x.md", "content": content}],
                 "integration": {"disposition": "standalone", "reason": "Synthetic source."}}
    _run(seam, monkeypatch, result=subprocess.CompletedProcess(["pi"], 0, json.dumps(candidate), ""))
    assert seam.main() == 0
    assert json.loads(capsys.readouterr().out) == candidate
    assert not _logs(seam)


def test_schema_contract_tracks_runtime_required_fields_and_vocabularies():
    from vector_lake import schema_validator as schema
    text = _load_seam()._runtime_schema_contract()
    values = json.loads(text.splitlines()[1])
    assert values["required_frontmatter"] == list(schema.REQUIRED_FIELDS)
    assert set(values["categories"]) == schema.VALID_CATEGORIES
    assert set(values["domain"]) == schema.VALID_DOMAINS | schema.DOMAIN_VERTICALS
    assert set(values["status"]) == schema.VALID_STATUS
    assert set(values["epistemic-status"]) == schema.VALID_EPISTEMIC_STATUS
    assert "categories (plural)" in text
