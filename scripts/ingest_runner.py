#!/usr/bin/env python
"""Host-side ingest runner (route B) - shadow mode first.

Why this lives outside ``vector_lake/``: the runtime must not make non-embedding model
calls (the task packet's ``cost_boundary``).  The runner is the host-side consumer that
claims work, (optionally) invokes a model, and submits through the one governed commit
entrypoint, ``finalize_ingest``.

What it implements from the contract in this session's design note:

  C1  skip work whose raw source is already published - no model call, and the job is
      closed with ``rejected`` + reason instead of being queued forever
  C3  bounded attempts; never re-claim a job to "try once more"
  C4  all-or-nothing payload with an explicit integration disposition
  C7  one task's failure never aborts the batch; every task is accounted for in the
      status file

Shadow mode (default) never writes a page: tasks that would need real content are left
leased and reported as ``needs-model``, so the pass rate can be measured before enabling
writes.  The model seam is ``--model-cmd`` (receives the packet JSON on stdin, must emit
``{"files_written": [...], "integration": {...}}`` on stdout). Built-ins are selected by
``config.json``; explicit commands keep the legacy override protocol.
"""
import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vector_lake.runtime_environment import configure_numeric_threads  # noqa: E402

configure_numeric_threads()

from vector_lake import db_store  # noqa: E402
from vector_lake.process_control import run_contained, model_timeout_seconds  # noqa: E402
from vector_lake.ingest_execution import BackendCircuit, BackendFailure  # noqa: E402
from vector_lake.ingest_backend import (SELECTION_SOURCES, builtin_model_argv,
                                        check_ingest_backend, resolve_ingest_backend,
                                        is_supervisor_child, legacy_runner_is_live,
                                        report_ingest_conflict)  # noqa: E402
from vector_lake.tool_ingest import (  # noqa: E402
    claim_ingest_tasks,
    finalize_ingest,
    current_source_publication_proven,
    raw_publication_index,
    record_ingest_failure,
)

def _runtime_dir() -> Path:
    """Resolved through the library so no install-specific path ships in the source."""
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / "runtime"


from vector_lake.ingest_errors import REJECT_DUPLICATE, REJECT_MISSING_SOURCE  # noqa: E402

RESULT_COUNTERS = (
    "duplicate", "missing-source", "needs-model", "finalized", "errors", "model-failed",
    "published", "content-rejected", "duplicate-closed", "missing-source-closed",
    "backend-failed", "backend-held",
)


def _merge_model_integration(processed: dict, integration: dict) -> dict:
    """Keep lease and source versions host-owned; accept only the model's semantic decision."""
    if not isinstance(integration, dict) or not str(integration.get("disposition") or "").strip():
        raise ValueError("model output requires an explicit integration disposition")
    if str(integration["disposition"]).strip().lower() not in {"integrated", "standalone"}:
        raise ValueError("model output disposition is not successful completion")
    return {**processed, "integration": integration}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Bounded repair rounds after a finalize rejection.  The model seam is a language model, so a
#: rejection is usually a wording or shape slip rather than a missing fact: handing the validator's
#: own message back costs one extra model call and converts a lost attempt (and, at the cap, an
#: abandoned source) into a corrected answer.  Measured 2026-09-28, a single paper source stacked
#: several independent schema violations (a naming violation, then a missing tension slot), which
#: is why the budget is a count and not a flag: one round fixes one fault, and the source still
#: converges through its remaining attempt budget.  Bounded on purpose -- a source that fails the
#: same check on every round has a real problem the budget must not paper over.
REPAIR_ATTEMPTS = max(0, int(os.environ.get("VECTOR_LAKE_RUNNER_REPAIR_ATTEMPTS", "2")))


def _repair_packet(packet: dict, previous_output: dict, validation_error: str) -> dict:
    """Add the rejection to a copy of the packet; the seam turns it into a repair brief."""
    metadata = dict(packet.get("metadata") or {})
    metadata["repair"] = {
        "previous_output": previous_output,
        "validation_error": str(validation_error)[:2000],
    }
    return {**packet, "metadata": metadata}


def write_status(**fields) -> None:
    """Write a clean snapshot.

    Every caller passes the complete ``stats`` mapping, so merging with the previous
    file only leaked stale keys (a leftover ``reason: "once"`` was reported while the
    resident runner was mid-cycle).
    """
    path = _runtime_dir() / "runner_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(fields)
    payload["updated_at"] = _utc_now()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)
    from vector_lake.operational_status import write_public_heartbeat

    try:
        write_public_heartbeat(path.parent.parent, "runner", state=payload.get("status", "unknown"),
                               shadow=payload.get("shadow"))
    except (OSError, ValueError) as exc:
        # A missing public heartbeat must remain unknown; never copy legacy errors/commands.
        logging.getLogger("vector-lake-runner").warning("Public runner heartbeat unavailable: %s", type(exc).__name__)


def _validate_controlled_claim(processed: dict, claim: dict | None = None) -> bool:
    """Controlled admission is determined by the durable claim, not packet opt-out."""
    from vector_lake.db_store import get_connection, init_db, validate_ingest_job_finalization
    job_id = str((claim or processed).get("job_id") or "")
    if job_id:
        init_db()
    row = get_connection().execute("SELECT task_type FROM jobs WHERE job_id = ?", (job_id,)).fetchone() if job_id else None
    controlled = ((row is not None and row["task_type"] == "ingest_recompile")
                  or (claim or {}).get("task_type") == "ingest_recompile"
                  or processed.get("controlled_recompile") is not None)
    if controlled:
        if str(processed.get("job_id") or "") != job_id:
            raise ValueError("Controlled packet identity differs from its actual claim")
        checked = validate_ingest_job_finalization(job_id, processed)
        if checked["task_type"] != "ingest_recompile":
            raise ValueError("Recompile exception requires its dedicated native job")
        if claim and any(processed.get(k) != claim.get(k) for k in ("lease_owner", "lease_token", "lease_generation")):
            raise ValueError("Controlled packet ownership differs from its actual claim")
        return True
    return False


def classify(processed: dict, index: dict) -> str:
    """C1: ordinary classification plus protected explicit recompile admission."""
    if _validate_controlled_claim(processed):
        return "needs-model"
    filepath = str(processed.get("filepath") or "")
    if not filepath or not Path(filepath).exists():
        return "missing-source"
    if current_source_publication_proven(filepath, str(processed.get("hash") or ""), index):
        return "duplicate"
    return "needs-model"


def _version_conflict(message: str) -> bool:
    from vector_lake.ingest_errors import failure_needs_redispatch

    return failure_needs_redispatch(message)


def run_model(packet: dict, model_cmd: str) -> tuple:
    """Invoke the pluggable model seam; return its bounded semantic result or an error."""
    timeout = model_timeout_seconds()
    deadline = float(packet.get("_host_deadline_monotonic", time.monotonic() + timeout))
    remaining = min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        return None, BackendFailure("model execution deadline exhausted", "timeout")
    env = dict(os.environ)
    env["VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC"] = str(deadline)
    builtin = builtin_model_argv(model_cmd)
    try:
        proc = run_contained(
            builtin or model_cmd, shell=builtin is None, input=json.dumps(packet, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=remaining, env=env,
        )
    except subprocess.TimeoutExpired:
        return None, BackendFailure("model runner timed out; no source verdict accepted", "timeout")
    except OSError as exc:
        return None, BackendFailure(f"model runner launch failed: {type(exc).__name__}", "launch_error")
    if proc.returncode != 0:
        if builtin and "ValueError: source changed before model dispatch" in proc.stderr:
            return None, BackendFailure("source changed before model dispatch", "source_changed")
        # Keep authentication/source output out of status and persistent circuit state.
        return None, BackendFailure(f"model runner exited {proc.returncode}; no source verdict accepted", "process_exit")
    try:
        result = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        return None, BackendFailure(f"model runner JSON invalid: {exc.msg}", "output_invalid")
    if not isinstance(result, dict) or set(result) != {"files_written", "integration"}:
        return None, BackendFailure("model runner must return files_written and integration only", "output_invalid")
    files = result["files_written"]
    integration = result["integration"]
    if not isinstance(files, list) or not all(
        isinstance(item, dict) and isinstance(item.get("filename"), str)
        and isinstance(item.get("content"), str) for item in files
    ):
        return None, BackendFailure("model runner files_written must contain filename/content objects", "output_invalid")
    if not isinstance(integration, dict) or not str(integration.get("disposition") or "").strip():
        return None, BackendFailure("model runner requires an explicit integration disposition", "output_invalid")
    if not files and str(integration["disposition"]).strip().lower() != "rejected":
        return None, BackendFailure("model runner produced no files for a non-rejected disposition", "output_invalid")
    return result, ""


def _process_task(task: dict, shadow: bool, model_cmd: str, publication_index: dict,
                  execution: BackendCircuit | None = None) -> tuple[dict, str]:
    """Process a single claimed ingest task in isolation (C7 error containment)."""
    res = dict.fromkeys(RESULT_COUNTERS, 0)
    last_err = ""
    if execution and task.get("created_at"):
        try:
            created = datetime.fromisoformat(str(task["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            execution.observe("queue_wait", max(0.0, execution.clock() - created.timestamp()) * 1000)
        except (TypeError, ValueError):
            execution.observe("queue_wait", float("nan"))
    packet = dict(task.get("task_packet") or {})
    # One host deadline includes all repair rounds; retain a lease/finalization margin.
    budget = model_timeout_seconds()
    lease_until = task.get("lease_until")
    if lease_until:
        expires = datetime.fromisoformat(str(lease_until).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        budget = min(budget, expires.timestamp() - datetime.now(timezone.utc).timestamp() - 120.0)
        if budget <= 0:
            # Do not mutate a potentially expired/newly-owned claim or spend a source attempt.
            res["needs-model"] += 1
            return res, "claim has insufficient execution window; awaiting lease recovery"
    packet["_host_deadline_monotonic"] = time.monotonic() + budget
    processed = (packet.get("metadata") or {}).get("processed_data")
    job_id = str(task.get("job_id") or "")
    if packet.get("error") or not processed:
        reason = str(packet.get("error") or "task packet carries no processed_data")
        res["errors"] += 1
        last_err = reason
        if job_id:
            record_ingest_failure(job_id, f"unusable task packet: {reason}", claim=task)
        return res, last_err

    def model_round(proposal):
        if execution and not execution.ready():
            return None, BackendFailure("backend paused; task retained", "paused")
        current = ((proposal.get("metadata") or {}).get("processed_data") or {})
        _validate_controlled_claim(current, task)  # Also fences each repair round.
        started = time.perf_counter()
        try:
            answer, error = run_model(proposal, model_cmd)
            if execution and not error:
                execution.success()
            return answer, error
        finally:
            if execution:
                execution.observe("model", (time.perf_counter() - started) * 1000)

    def submit(files, data):
        started = time.perf_counter()
        try:
            return finalize_ingest(files, data)
        finally:
            if execution:
                execution.observe("finalize", (time.perf_counter() - started) * 1000)

    def backend_failed(error):
        if getattr(error, "code", "") == "paused":
            res["backend-held"] += 1
            res["needs-model"] += 1
            if job_id:
                db_store.release_job_for_retry(job_id, "backend paused; task retained", claim=task,
                                               delay_seconds=execution.delay(120) if execution else 5)
            return
        res["model-failed"] += 1
        if getattr(error, "code", "") == "source_changed":
            if job_id:
                record_ingest_failure(job_id, f"model seam: {error}", claim=task)
            return
        res["backend-failed"] += 1
        if execution:
            execution.failure(getattr(error, "code", "delivery_error"))
        if job_id:
            db_store.release_job_for_retry(job_id, "backend delivery failure; source budget untouched", claim=task,
                                           delay_seconds=execution.delay(120) if execution else 5)

    try:
        _validate_controlled_claim(processed, task)
        verdict = classify(processed, publication_index)
    except OSError as exc:
        res["errors"] += 1
        return res, f"dispatch resource unavailable: {type(exc).__name__}"
    except ValueError as exc:
        res["errors"] += 1
        last_err = f"dispatch validation failed: {exc}"
        if job_id:
            record_ingest_failure(job_id, last_err, claim=task)
        return res, last_err
    try:
        if verdict == "missing-source":
            res["missing-source"] += 1
            res["errors"] += 1
            last_err = "raw source is missing; ingestion is not complete"
            if job_id:
                record_ingest_failure(job_id, last_err, claim=task)
        elif verdict == "duplicate":
            result = submit([], {**processed, "integration": {"disposition": "duplicate", "reason": REJECT_DUPLICATE}})
            res[verdict] += 1
            if "Successfully finalized" in result:
                res["finalized"] += 1
                res["duplicate-closed"] += 1
            else:
                res["errors"] += 1
                last_err = result
                if job_id:
                    record_ingest_failure(job_id, f"rejection could not be finalized: {result}", claim=task)
        elif shadow or not model_cmd:
            res["needs-model"] += 1
            if job_id and os.environ.get("VECTOR_LAKE_RUNNER_HOLD_SHADOW_LEASE", "0") != "1":
                from vector_lake.db_store import release_job_for_retry
                release_job_for_retry(job_id, "shadow run: model call skipped", claim=task)
        else:
            if execution and not execution.ready():
                res["backend-held"] += 1
                res["needs-model"] += 1
                db_store.release_job_for_retry(job_id, "backend paused; task retained", claim=task,
                                               delay_seconds=execution.delay(120))
                return res, "backend paused; task retained"
            model_result, error = model_round(packet)
            if error:
                last_err = error
                backend_failed(error)
                return res, last_err
            result = submit(
                model_result["files_written"],
                _merge_model_integration(processed, model_result["integration"]),
            )
            # Rejection is information, not a verdict: hand the model its own validator message
            # back, up to the bounded budget, before spending the job's attempt.
            rejection = "" if "Successfully finalized" in result else result
            previous_output = model_result
            rounds_used = 0
            while rejection and not _version_conflict(rejection) and rounds_used < REPAIR_ATTEMPTS:
                rounds_used += 1
                repaired, repair_error = model_round(_repair_packet(packet, previous_output, rejection))
                if repair_error or not repaired:
                    error = repair_error or BackendFailure("repair produced no model output", "output_invalid")
                    backend_failed(error)
                    return res, str(error)
                previous_output = repaired
                result = submit(
                    repaired["files_written"],
                    _merge_model_integration(processed, repaired["integration"]),
                )
                rejection = "" if "Successfully finalized" in result else result
            if rounds_used and rejection:
                result = f"{result} [after {rounds_used} repair round(s)]"
            if "Successfully finalized" in result:
                res["finalized"] += 1
                res["published"] += 1
            else:
                res["errors"] += 1
                last_err = result
                if job_id:
                    record_ingest_failure(job_id, f"finalize rejected: {result}", claim=task)
    except Exception as exc:
        res["errors"] += 1
        last_err = f"host runtime failure: {type(exc).__name__}; source budget untouched"
        if execution:
            execution.failure("host_runtime_error")
        if job_id:
            db_store.release_job_for_retry(job_id, last_err, claim=task,
                                           delay_seconds=execution.delay(120) if execution else 5)
    return res, last_err


def process_once(limit: int, shadow: bool, model_cmd: str, stats: dict, concurrency: int = 1,
                 execution: BackendCircuit | None = None, progress=None) -> dict:
    batch = {"claimed": 0, **dict.fromkeys(RESULT_COUNTERS, 0)}
    if execution and not execution.ready() and not shadow:
        return batch
    limit = max(1, int(limit))
    capacity = min(limit, max(1, int(concurrency)))
    publication_index = None

    def claim_slots(slots):
        nonlocal publication_index
        if execution and not execution.ready() and not shadow:
            return []
        started = time.perf_counter()
        try:
            tasks = json.loads(claim_ingest_tasks(limit=slots))
        except json.JSONDecodeError as exc:
            raise ValueError("Ingest task claim response is invalid JSON") from exc
        finally:
            if execution:
                execution.observe("claim", (time.perf_counter() - started) * 1000)
        if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
            raise ValueError("Ingest task claim response must be an array of task objects")
        batch["claimed"] += len(tasks)
        if tasks and publication_index is None:
            publication_index = raw_publication_index()
        return tasks

    def collect(result):
        task_res, error = result
        for key, value in task_res.items():
            batch[key] += value
        if error:
            stats["last_error"] = str(error)
        if execution:
            stats["backend"] = execution.snapshot()
            stats["timings"] = execution.timings()
        if progress:
            progress(batch)

    if capacity == 1:
        for _ in range(limit):
            tasks = claim_slots(1)
            if not tasks:
                break
            collect(_process_task(tasks[0], shadow, model_cmd, publication_index, execution))
        return batch

    # A queued future must not consume its lease while every execution slot is busy.
    with ThreadPoolExecutor(max_workers=capacity) as pool:
        pending = set()
        while True:
            slots = min(capacity - len(pending), limit - batch["claimed"])
            if slots:
                for task in claim_slots(slots):
                    pending.add(pool.submit(_process_task, task, shadow, model_cmd, publication_index, execution))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                collect(future.result())
    return batch


def next_cycle_delay(batch: dict, maximum: float, idle_cycles: int,
                     execution: BackendCircuit, *, shadow: bool = False) -> float:
    """Continue ready work after a yield; back off empty queues without hot polling."""
    maximum = max(5.0, maximum)
    if not shadow and not execution.ready():
        return execution.delay(maximum)
    if not shadow and batch["claimed"] and db_store.ingest_tasks_ready():
        return 1.0
    return min(maximum, 5.0 * (2 ** min(5, max(0, idle_cycles - 1))))


def main() -> int:
    parser = argparse.ArgumentParser(description="Vector Lake ingest runner (shadow first)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--once", action="store_true", help="run a single batch and exit")
    parser.add_argument("--interval", type=int, default=60, help="maximum idle wait; ready backlog continues after a bounded yield")
    parser.add_argument("--concurrency", "-c", type=int,
                        default=int(os.environ.get("VECTOR_LAKE_RUNNER_CONCURRENCY", "1")),
                        help="concurrent worker threads for model execution (default 1)")
    parser.add_argument("--shadow", action="store_true", default=True)
    parser.add_argument("--no-shadow", dest="shadow", action="store_false",
                        help="allow real writes through the configured model backend")
    parser.add_argument("--model-cmd", default=None)
    parser.add_argument("--model-source", choices=SELECTION_SOURCES, help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="Check backend capabilities without claiming tasks.")
    parser.add_argument("--reset-backend", action="store_true", help="Explicit owner reset of paused backend after repair; does not verify authentication.")
    args = parser.parse_args()
    if args.check and args.reset_backend:
        parser.error("--check cannot reset backend state")
    try:
        selection = resolve_ingest_backend(args.model_cmd)
        if args.check:
            print(json.dumps(check_ingest_backend(selection), ensure_ascii=False))
            return 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Ingest backend unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    args.model_cmd = selection.model_command

    # Direct launches reserve the same root gate as the service; only its actual child
    # may proceed through an already-held gate. Every consumer also takes its own lock.
    from filelock import FileLock, Timeout

    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    gate = FileLock(str(runtime / ".runner_service.lock"))
    consumer = FileLock(str(runtime / ".runner_consumer.lock"))
    gate_owned = False
    consumer_owned = False
    try:
        try:
            gate.acquire(timeout=0)
            gate_owned = True
        except Timeout:
            if not is_supervisor_child():
                report_ingest_conflict(selection, "Ingest runner already running for this MEMORY root")
                return 4
        try:
            consumer.acquire(timeout=0)
            consumer_owned = True
        except Timeout:
            report_ingest_conflict(selection, "Ingest consumer already running for this MEMORY root")
            return 4
        if legacy_runner_is_live():
            report_ingest_conflict(selection, "Recorded legacy runner is still live; drain it before startup")
            return 4
        if not args.shadow:
            check_ingest_backend(selection)
        return _run_consumer(args, selection, None if gate_owned else os.getppid())
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Ingest startup blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        if consumer_owned:
            consumer.release()
        if gate_owned:
            gate.release()


def _run_consumer(args, selection, supervisor_pid) -> int:
    """Run only while main holds the root gate/consumer lock; never called by contenders."""
    pid_path = _runtime_dir() / "runner.pid"
    execution = BackendCircuit(_runtime_dir() / "runner_backend_state.json", selection.backend,
                               selection.model_command, reset=getattr(args, "reset_backend", False))
    idle_cycles = 0
    stats = {"started_at": _utc_now(), "pid": os.getpid(), "shadow": args.shadow,
             "model_command": bool(args.model_cmd), "effective_backend": selection.backend,
             "selection_source": args.model_source or selection.source,
             "resolved_model_command": selection.model_command, "supervisor_pid": supervisor_pid,
             "interval": args.interval,
             "last_error": "", "consecutive_failures": 0, "cycles": 0,
             "totals": {"claimed": 0, **dict.fromkeys(RESULT_COUNTERS, 0)},
             "backend": execution.snapshot(), "timings": execution.timings()}
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    def _shutdown(signum, _frame):
        write_status(status="stopped", reason=f"signal {signum}", **stats)
        try:
            pid_path.unlink()
        except OSError:
            pass
        raise SystemExit(0)

    for _sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, _sig):
            try:
                signal.signal(getattr(signal, _sig), _shutdown)
            except (ValueError, OSError):
                pass
    write_status(status="running", **stats)

    def progress(batch):
        write_status(status="processing", last_batch=batch, **stats)

    while True:
        batch = {"claimed": 0}
        started = time.perf_counter()
        try:
            batch = process_once(args.limit, args.shadow, args.model_cmd, stats, args.concurrency,
                                 execution=execution, progress=progress)
            execution.observe("cycle", (time.perf_counter() - started) * 1000)
            stats["backend"] = execution.snapshot()
            stats["timings"] = execution.timings()
            idle_cycles = idle_cycles + 1 if batch["claimed"] == 0 else 0
            totals = stats["totals"]
            for key, value in batch.items():
                totals[key] = int(totals.get(key, 0)) + int(value)
            # In shadow mode "needs-model" is the expected outcome, not a failure:
            # counting it as no-progress made every shadow pass report runner_failing.
            stats["cycles"] = int(stats.get("cycles", 0)) + 1
            progressed = (batch["finalized"] + batch["needs-model"]) > 0
            stats["consecutive_failures"] = 0 if progressed or batch["claimed"] == 0 else stats["consecutive_failures"] + 1
            paused = not args.shadow and not execution.ready()
            write_status(status="backend_paused" if paused else ("idle" if batch["claimed"] == 0 else "processing"),
                         last_batch=batch, last_batch_at=_utc_now(), **stats)
            if args.once:
                write_status(status="stopped", reason="once", **stats)
                try:
                    pid_path.unlink()
                except OSError:
                    pass
                return 0
            if stats["consecutive_failures"] >= 5:
                # Resident process: cool down and keep the heartbeat alive instead of
                # exiting, so a transient outage does not silently end supervision.
                cooldown = max(60, int(os.environ.get("VECTOR_LAKE_RUNNER_COOLDOWN", "300")))
                # ``recovering``: a cooldown that retries is not a terminal state (S4).
                write_status(status="recovering", reason=f"consecutive failures; cooling down {cooldown}s", **stats)
                time.sleep(cooldown)
                stats["consecutive_failures"] = 0
            if batch["claimed"] == 0 and args.once:
                return 0
        except Exception as exc:  # never die silently
            stats["consecutive_failures"] += 1
            stats["last_error"] = f"host cycle failure: {type(exc).__name__}"
            execution.failure("host_runtime_error")
            execution.observe("cycle", (time.perf_counter() - started) * 1000)
            stats["backend"] = execution.snapshot()
            stats["timings"] = execution.timings()
            write_status(status="error", **stats)
            if args.once:
                return 1
        time.sleep(next_cycle_delay(batch, args.interval, idle_cycles, execution, shadow=args.shadow))


if __name__ == "__main__":
    raise SystemExit(main())
