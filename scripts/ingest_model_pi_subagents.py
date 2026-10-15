#!/usr/bin/env python
"""Model seam for the ingest runner: do the ingest work through pi-subagents.

Contract with the runner (``scripts/ingest_runner.py --model-cmd``):
  stdin   one task packet JSON (as emitted by ``claim_ingest_tasks``)
  stdout  ``{"files_written": [{"filename": ..., "content": ...}], "integration": {...}}``
  exit 0  success; non-zero means "model failed" and the runner records it

Why a read-only pi-subagents profile: the runtime is the only writer (the runner submits
through ``finalize_ingest``, which owns validation, the schema gates and the canonical
store).  An ingest child therefore must read the raw document and *return text*; it must
never write wiki files itself. The default project ``vector-lake-ingestor`` profile
has only ``read`` and fresh context. A reviewer is not an ingest compiler; a write-capable
worker would bypass the governed write path.

The child is a headless Pi session (``pi --print --session-dir <scratch/runner_sessions>``)
whose system prompt tells it to delegate the ingest to the configured subagent and to print
only the JSON object.

The adapter uses an isolated persistent session directory so extensions have a per-run
root for their state and artifacts. Sessions are pruned by age; this is bounded local
retention, not ephemeral execution or an OS sandbox.

Failure evidence is preserved rather than truncated: ``scripts/ingest_runner.py`` keeps only the
first 300 characters of this process's stderr, which one ``pi`` startup banner already exceeds,
so a failure is written whole to ``scratch/runner_model-*-{out,err}.log`` and the message the
runner stores carries that path.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PI_BIN = os.environ.get("VECTOR_LAKE_RUNNER_PI_BIN", "pi")
AGENT = os.environ.get("VECTOR_LAKE_RUNNER_SUBAGENT_AGENT", "vector-lake-ingestor")
AGENT_SCOPE = "project" if AGENT == "vector-lake-ingestor" else "both"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vector_lake.template_loader import read_template, render_template
from vector_lake.ingest_schema_contract import render_schema_contract
from vector_lake.process_control import run_contained, model_timeout_seconds
TIMEOUT = model_timeout_seconds()
SCRATCH = ROOT / "scratch"
# Deliberately not an environment switch: the repo root already locates scratch, and a new
# VECTOR_LAKE_* literal would have to be registered in the README configuration section
# (``tests/test_registries.py``) to stay honest.
SESSION_DIR = SCRATCH / "runner_sessions"
KEEP_LOGS = 20
SESSION_MAX_AGE_DAYS = 3.0
FAILURE_TAIL_CHARS = 180

SYSTEM_PROMPT = render_template("prompts/ingest/relay_system.md", agent=AGENT, agent_scope=AGENT_SCOPE)


def _extract_result(text: str):
    from vector_lake.ingest_model_contract import extract_result
    return extract_result(text)


def _repair_block(repair: dict) -> str:
    """One bounded correction round: the finalizer's own message plus the answer it rejected."""
    error = " ".join(str(repair.get("validation_error") or "").split())[:1200]
    try:
        previous = json.dumps(repair.get("previous_output"), ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        previous = ""
    return render_template(
        "prompts/ingest/repair.md", error=error,
        previous_block=render_template("prompts/ingest/repair_previous.md", previous=previous) if previous else "",
    )


def _brief(packet: dict) -> str:
    metadata = (packet.get("metadata") or {}).get("processed_data") or {}
    if metadata.get("controlled_recompile") is not None or metadata.get("source_read_path") is not None:
        from vector_lake.ingest_model_contract import build_cli_prompt
        # The delegated compiler receives actual SHA-checked inline snapshot bytes.
        # Original filepath remains provenance only; never reuse relay_brief's raw-read instruction.
        return _runtime_schema_contract() + "\n\n" + build_cli_prompt(packet)
    # The prompt contains source/context text, not the authoritative dispatch manifest.
    # Copy the producer's actual snapshot verbatim; never reconstruct target/version tokens.
    fields = ("filepath", "hash", "canonical_name", "source_hash", "source_projection_hash",
              "ingest_contract_version", "integration_candidates")
    processing_data = {key: metadata[key] for key in fields if key in metadata}
    processing_json = json.dumps(processing_data, ensure_ascii=False, sort_keys=True)
    return render_template(
        "prompts/ingest/relay_brief.md", prompt=packet.get("prompt", ""),
        filepath=metadata.get("filepath"), canonical_name=metadata.get("canonical_name"),
        processing_json=processing_json, schema_contract=_runtime_schema_contract(),
        output_contract=_packet_contract(packet), repair=_packet_repair(packet),
    )


def _runtime_schema_contract() -> str:
    """Use the validator's vocabulary, not legacy prose or the model's guesses."""
    return render_schema_contract() + (
        f"\n\nHOST NODE UPDATE DATE (UTC): {datetime.now(timezone.utc).date().isoformat()}\n"
        "A Source page must use literal type: source, not Source; updated must not be null. "
        "Use the host node update date for updated. This is the compilation/edit date, "
        "not the source's publication or event date; never invent an unknown event date."
    )


def _packet_contract(packet: dict) -> str:
    """The packet carries its own contract; fall back to the module copy for older packets.

    Appended last on purpose: it is the final thing the child reads before answering, which is
    the opposite of the ~50 KB prompt's middle where the same rules used to live.
    """
    stored = str((packet.get("metadata") or {}).get("output_contract") or "").strip()
    if stored:
        return stored
    # Pre-v9 packet.  This seam deliberately does not import ``vector_lake``: it runs as a bare
    # script whose ``sys.path[0]`` is ``scripts/``, and the contract's one owner is
    # ``vector_lake.ingest_output_contract`` -- which the packet publisher has already used by the time
    # any packet reaches here.
    return read_template("prompts/ingest/legacy_packet_contract.md")


def _packet_repair(packet: dict) -> str:
    repair = (packet.get("metadata") or {}).get("repair")
    return _repair_block(repair) if isinstance(repair, dict) else ""


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _prune_scratch() -> None:
    """Keep the runner's own scratch bounded: newest logs, sessions by age.

    Sessions are pruned by age rather than by count so a concurrently running child is
    never deleted underneath itself.
    """
    try:
        for stream in ("out", "err"):
            stale = sorted(
                SCRATCH.glob(f"runner_model-*-{stream}.log"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[KEEP_LOGS:]
            for path in stale:
                path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        entries = list(SESSION_DIR.iterdir())
    except OSError:
        return
    cutoff = time.time() - SESSION_MAX_AGE_DAYS * 86400
    for entry in entries:
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _resolve_pi_binary() -> str | None:
    """Keep PATH/explicit overrides, but support the user's managed CLI in S4U."""
    resolved = shutil.which(PI_BIN)
    if resolved:
        return resolved
    # A missing explicit override is a configuration error, not permission to
    # silently pick a different install. Only the default bare command falls back.
    if PI_BIN != "pi":
        return None
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise RuntimeError("cannot resolve the managed Pi CLI user home") from exc
    managed = home / ".pi" / "agent" / "bin" / ("pi.cmd" if os.name == "nt" else "pi")
    try:
        if managed.is_file() and os.access(managed, os.X_OK):
            return str(managed)
    except OSError as exc:
        raise OSError("cannot inspect the managed Pi CLI launcher") from exc
    return None


def _argv(pi_path: str, brief_path: str, system_path: str | None, session_dir: Path) -> list[str]:
    argv = [pi_path, "--print", "--session-dir", str(session_dir)]
    if system_path:
        argv += ["--append-system-prompt", system_path]
    argv.append(f"@{brief_path}")
    # npm installs `pi` as a .CMD shim on Windows and CreateProcess cannot start that
    # directly (FileNotFoundError [WinError 2]).  Route it through the command
    # interpreter rather than shell=True, which would re-introduce quoting hazards.
    if os.name == "nt" and pi_path.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
    return argv


def _as_text(value) -> str:
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _persist_failure(stdout: str, stderr: str) -> str:
    """Write the child's complete output to scratch; return its log path ('' if unwritable)."""
    stem = f"runner_model-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    try:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        (SCRATCH / f"{stem}-out.log").write_text(stdout or "", encoding="utf-8")
        err_path = SCRATCH / f"{stem}-err.log"
        err_path.write_text(stderr or "", encoding="utf-8")
    except OSError:
        return ""
    return _relative(err_path)


def _failure_message(summary: str, stdout: str, stderr: str) -> str:
    """Keep the diagnosis inside the runner's 300-character budget.

    The path is placed before the tail on purpose: the runner truncates from the right, so
    a message that leads with the log location always tells a later reader where to look.
    """
    log = _persist_failure(stdout, stderr)
    tail = " ".join((stderr or "").split())[-FAILURE_TAIL_CHARS:]
    parts = [summary]
    if log:
        parts.append(f"full log: {log}")
    if tail:
        parts.append(f"stderr tail: {tail}")
    return "; ".join(parts)


def main() -> int:
    # Windows consoles default to a legacy codepage; the runner captures this
    # process over a pipe, so an un-reconfigured stdout raises UnicodeEncodeError
    # on any non-ASCII payload and the task silently fails as "model failed".
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raw = sys.stdin.read()
    try:
        packet = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"model seam: invalid packet on stdin: {exc}", file=sys.stderr)
        return 2
    pi_path = _resolve_pi_binary()
    if not pi_path:
        print(f"model seam: pi binary {PI_BIN!r} not found on PATH", file=sys.stderr)
        return 3

    brief = _brief(packet)
    # The rules go to a file and are appended (not passed inline): a long
    # ``--system-prompt`` argument containing newlines is mangled by cmd.exe, which
    # silently produced an empty answer with exit 0.
    system_path = None
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(brief)
        brief_path = handle.name
    try:
        handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        handle.write(SYSTEM_PROMPT)
        handle.close()
        system_path = handle.name
    except OSError:
        system_path = None
    session_dir = SESSION_DIR
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # An unwritable scratch must not become a new failure mode.  Fall back to a per-run
        # temp session root and keep ingesting; nothing is written to stderr, because noise on
        # a run that is succeeding would spend the runner's 300-character error budget.
        session_dir = Path(tempfile.mkdtemp(prefix="runner_sessions-"))
    _prune_scratch()
    try:
        deadline = float(os.environ.get("VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC", time.monotonic() + TIMEOUT))
        proc = run_contained(
            _argv(pi_path, brief_path, system_path, session_dir),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=min(TIMEOUT, deadline - time.monotonic()), shell=False, cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        # A hung child is a model failure with evidence, not a traceback.
        print(
            "model seam: "
            + _failure_message(
                f"pi timed out after {TIMEOUT}s",
                _as_text(getattr(exc, "stdout", "")),
                _as_text(getattr(exc, "stderr", "")),
            ),
            file=sys.stderr,
        )
        return 4
    finally:
        for path in (brief_path, system_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass

    if proc.returncode != 0:
        print(
            "model seam: "
            + _failure_message(f"pi exited {proc.returncode}", proc.stdout, proc.stderr),
            file=sys.stderr,
        )
        return 4
    payload, error = _extract_result(proc.stdout)
    if payload is None:
        # Pi can exit 0 while explaining the problem on stderr, so surface both streams.
        print(
            "model seam: "
            + _failure_message(
                f"{error}; stdout head: {proc.stdout[:200]!r}", proc.stdout, proc.stderr
            ),
            file=sys.stderr,
        )
        return 5
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
