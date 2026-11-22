"""Runtime health checks used by write gates and doctor surfaces."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from vector_lake.node_vocabulary import NON_NODE_WIKI_FILES

log = logging.getLogger("vector-lake-runtime-health")

# The wiki key set must be read fresh on every health assessment.  The cache that
# used to live here was memoised on the containing directory's
# ``(st_mtime_ns, st_size)`` on the assumption that the directory stamp tracks its
# entries; NTFS does not honour that (see ``wiki_utils.wiki_page_keys``), so a page
# added or deleted since the previous call could be invisible to the write gate.


def _parse_dt(value: Any):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def assess_runtime_health(
    max_watchdog_age_seconds: int = 120,
    deep_projection_checks: bool = False,
) -> dict[str, Any]:
    """Classify runtime conditions as hard blockers vs. repairable degradation.

    ``issues`` holds only hard faults that a canonical write must not proceed
    past.  ``degraded`` holds self-healing or repairable conditions (stale
    heartbeat, un-drained outbox, projection drift).  Degradation must never
    block a write: the write path is the only way to repair it.
    """
    from vector_lake.db_store import get_connection, get_db_path, init_db
    from vector_lake.wiki_utils import (
        get_index_path,
        get_meta_dir,
        get_wiki_dir,
        wiki_page_keys,
    )

    issues: list[str] = []
    degraded: list[str] = []
    warnings: list[str] = []
    detail: dict[str, Any] = {}

    try:
        db_path = get_db_path()
        # Unconditionally: this check runs *before* the mutation coordinator calls ``init_db``, and
        # it reads columns that a database predating a migration does not have yet.  Guarding on
        # "the file exists" meant a pre-migration database raised ``no such column`` here instead of
        # converging -- the write gate failing before the code that would fix the schema.
        init_db()
        conn = get_connection()
    except Exception as exc:
        return {"ok": False, "issues": [f"database_unavailable:{exc}"], "warnings": [], "detail": {}}

    detail["db_path"] = str(db_path)

    outbox_counts = {
        row["status"]: row["count"]
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM mutation_outbox GROUP BY status")
    }
    detail["outbox_counts"] = outbox_counts
    # A failed or backed-up outbox row is a data condition that the write path itself
    # repairs: blocking writes here removes the only remedy and deadlocks the system
    # (measured: one transient materialisation failure wedged every wiki write for
    # hours).  Kept visible as degradation, with an env opt-in for strict operators.
    if outbox_counts.get("failed", 0):
        failed_filenames = sorted(
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT filename FROM mutation_outbox WHERE status = 'failed' AND filename IS NOT NULL"
            ).fetchall()
        )
        detail["failed_outbox_filenames"] = failed_filenames
        message = f"mutation_outbox_failed:{outbox_counts.get('failed', 0)}"
        # A failed mutation entry is a hard fault by contract: it blocks canonical writes
        # until it is cleared, which is also what the write-gate test asserts.  The env
        # switch only exists so an operator can trade that safety for availability.
        if os.environ.get("VECTOR_LAKE_OUTBOX_FAILURE_NONBLOCKING") == "1":
            degraded.append(message)
        else:
            issues.append(message)
    max_backlog = max(1, int(os.environ.get("VECTOR_LAKE_OUTBOX_MAX_BACKLOG", "2000")))
    pending_backlog = outbox_counts.get("pending", 0) + outbox_counts.get("processing", 0)
    detail["outbox_backlog"] = pending_backlog
    if pending_backlog > max_backlog:
        message = f"mutation_outbox_backlog:{pending_backlog}>{max_backlog}"
        if os.environ.get("VECTOR_LAKE_OUTBOX_BACKLOG_BLOCKING") == "1":
            issues.append(message)
        else:
            degraded.append(message)
    oldest_pending = conn.execute(
        "SELECT MIN(COALESCE(available_at, created_at)) FROM mutation_outbox "
        "WHERE status IN ('pending', 'processing')"
    ).fetchone()[0]
    oldest_pending_dt = _parse_dt(oldest_pending)
    if oldest_pending_dt is not None:
        pending_age = max(0, int((datetime.now(timezone.utc) - oldest_pending_dt).total_seconds()))
        detail["oldest_pending_outbox_age_seconds"] = pending_age
        max_pending_age = max(1, int(os.environ.get("VECTOR_LAKE_OUTBOX_MAX_PENDING_AGE_SECONDS", "300")))
        if pending_age > max_pending_age:
            degraded.append(f"mutation_outbox_stalled:{pending_age}s")

    # Write-lock contention telemetry: SQLite cannot name the holder, so a stalled holder
    # used to look identical to an idle system.  A recent timeout/collision is surfaced
    # here (with the caller that hit it) so a wedge is visible without hand-forensics.
    contention_path = get_meta_dir() / "runtime" / "write_lock_contention.json"
    if contention_path.exists():
        try:
            contention = json.loads(contention_path.read_text(encoding="utf-8"))
            last = contention.get("last") if isinstance(contention, dict) else None
            last_dt = _parse_dt((last or {}).get("at")) if isinstance(last, dict) else None
            if last_dt is not None:
                contention_age = max(0, int((datetime.now(timezone.utc) - last_dt).total_seconds()))
                detail["write_lock_contention"] = {
                    "age_seconds": contention_age,
                    "outcome": last.get("outcome"),
                    "caller": last.get("caller"),
                    "attempts": last.get("attempts"),
                    "waited_seconds": last.get("waited_seconds"),
                }
                window = max(60, int(os.environ.get("VECTOR_LAKE_WRITE_LOCK_CONTENTION_WINDOW_SECONDS", "600")))
                if contention_age <= window:
                    degraded.append(
                        f"db_write_lock_contention:{last.get('outcome')}"
                        f"@{contention_age}s by {last.get('caller')}"
                    )
        except (OSError, json.JSONDecodeError):
            pass

    # Host-side ingest runner: the runtime cannot consume its own ingest packets (the
    # task packet's cost_boundary forbids model calls in-process), so "nothing is
    # consuming ingest" is a real operational state.  It stayed invisible for six days
    # once.  Reported as degradation, never as a write-blocking fault.
    # A host-side process the runtime does not own: surfacing its absence is important
    # (a six-day ingest stall once stayed invisible), but it must not flip ``ok`` for a
    # runtime that is healthy on its own terms.  Strict mode promotes it to degradation.
    runner_advisories: list[str] = []
    runner_path = get_meta_dir() / "runtime" / "runner_status.json"
    runner_expected = os.environ.get("VECTOR_LAKE_RUNNER_EXPECTED", "1") != "0"
    if runner_path.exists():
        try:
            runner = json.loads(runner_path.read_text(encoding="utf-8"))
            runner_dt = _parse_dt(runner.get("updated_at"))
            runner_age = (
                max(0, int((datetime.now(timezone.utc) - runner_dt).total_seconds()))
                if runner_dt is not None else None
            )
            totals = runner.get("totals")
            detail["runner"] = {
                "status": runner.get("status"),
                "age_seconds": runner_age,
                "shadow": runner.get("shadow"),
                "model_command": runner.get("model_command"),
                "totals": runner.get("totals"),
                "last_error": runner.get("last_error"),
                "outcomes": {
                    key: totals.get(key) if isinstance(totals, dict) else None
                    for key in ("published", "content-rejected", "duplicate-closed", "missing-source-closed")
                },
                # Global outbox work, not a per-job receipt or proof that vectors are current.
                "projection_outbox_pending": pending_backlog,
                "projection_outbox_failed": outbox_counts.get("failed", 0),
                "backend": runner.get("backend"),
                "timings": runner.get("timings"),
            }
            backend_state = runner.get("backend")
            if isinstance(backend_state, dict) and backend_state.get("status") in {"open", "cooldown"}:
                runner_advisories.append(f"runner_backend_paused:{backend_state['status']}")
            stale_after = max(60, int(os.environ.get("VECTOR_LAKE_RUNNER_STALE_SECONDS", "2400")))
            if runner_age is None or runner_age > stale_after:
                runner_advisories.append(
                    f"runner_stalled:{runner_age if runner_age is not None else 'unknown'}s")
            elif int(runner.get("consecutive_failures") or 0) > 0:
                runner_advisories.append(f"runner_failing:{runner.get('consecutive_failures')}")
        except (OSError, json.JSONDecodeError):
            runner_advisories.append("runner_status_unreadable")
    elif runner_expected:
        runner_advisories.append("runner_absent")

    # Supervisor for the resident runner.  Distinguishes "the runner died and is being
    # restarted" (supervised, recoverable) from "no runner exists at all" above.
    supervisor_path = get_meta_dir() / "runtime" / "runner_supervisor.json"
    if supervisor_path.exists():
        try:
            supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
            supervisor_dt = _parse_dt(supervisor.get("updated_at"))
            supervisor_age = (
                max(0, int((datetime.now(timezone.utc) - supervisor_dt).total_seconds()))
                if supervisor_dt is not None else None
            )
            detail["runner_supervisor"] = {
                "status": supervisor.get("status"),
                "age_seconds": supervisor_age,
                "child_pid": supervisor.get("child_pid"),
                "restarts": supervisor.get("restarts"),
                "reason": supervisor.get("reason"),
            }
            supervisor_state = str(supervisor.get("status") or "")
            supervisor_stale = max(60, int(os.environ.get("VECTOR_LAKE_RUNNER_STALE_SECONDS", "2400")))
            if supervisor_state == "failed":
                runner_advisories.append(f"runner_supervisor_failed:{supervisor.get('reason')}")
            elif supervisor_age is None or supervisor_age > supervisor_stale:
                runner_advisories.append(
                    f"runner_supervisor_stalled:{supervisor_age if supervisor_age is not None else 'unknown'}s"
                )
            elif supervisor_state == "restarting" and int(supervisor.get("restarts") or 0) > 1:
                runner_advisories.append(f"runner_restarts:{supervisor.get('restarts')}")
        except (OSError, json.JSONDecodeError):
            runner_advisories.append("runner_supervisor_status_unreadable")
    if runner_advisories:
        if os.environ.get("VECTOR_LAKE_RUNNER_STRICT", "0") == "1":
            degraded.extend(runner_advisories)
        else:
            warnings.extend(runner_advisories)

    terminal_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND retries >= 3"
    ).fetchone()[0]
    detail["terminal_failed_jobs"] = int(terminal_jobs)
    # Same reasoning as ``subagent_backlog`` above: an ingest job that exhausted its
    # retries is a data condition in a *different* subsystem from wiki write integrity.
    # Hard-failing here locked every wiki mutation behind 8 stale ingest jobs.
    if terminal_jobs:
        message = f"terminal_failed_jobs:{terminal_jobs}"
        if os.environ.get("VECTOR_LAKE_TERMINAL_FAILED_JOBS_BLOCKING") == "1":
            issues.append(message)
        else:
            degraded.append(message)
    awaiting_row = conn.execute(
        "SELECT COUNT(*) AS count, MIN(updated_at) AS oldest FROM jobs WHERE status = 'awaiting_subagent'"
    ).fetchone()
    awaiting_count = int(awaiting_row["count"] or 0)
    detail["awaiting_subagent_jobs"] = awaiting_count
    backlog_messages = []
    max_awaiting = max(1, int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS", "500")))
    if awaiting_count > max_awaiting:
        backlog_messages.append(f"count={awaiting_count}")
    oldest_awaiting = _parse_dt(awaiting_row["oldest"])
    if oldest_awaiting is not None:
        awaiting_age = max(0, int((datetime.now(timezone.utc) - oldest_awaiting).total_seconds()))
        detail["oldest_awaiting_subagent_age_seconds"] = awaiting_age
        max_age = max(60, int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "86400")))
        if awaiting_age > max_age:
            backlog_messages.append(f"oldest={awaiting_age}s")
    if backlog_messages:
        message = "subagent_backlog:" + ",".join(backlog_messages)
        if os.environ.get("VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING") == "1":
            issues.append(message)
        else:
            warnings.append(message)

    status_path = get_meta_dir() / ".watchdog_status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            updated_at = _parse_dt(status.get("updated_at"))
            age = None
            if updated_at is not None:
                age = max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))
            detail["watchdog_age_seconds"] = age
            detail["watchdog_status"] = status.get("status")
            if age is None or age > max_watchdog_age_seconds:
                degraded.append(f"watchdog_stale:{age if age is not None else 'unknown'}")
            unhealthy_components = [
                name
                for name, component in (status.get("components") or {}).items()
                if str(component.get("status", "")).lower() in {"error", "halted"}
            ]
            if str(status.get("status", "")).lower() in {"error", "halted"} or unhealthy_components:
                degraded.append(
                    "watchdog_unhealthy:"
                    + (",".join(sorted(unhealthy_components)) or str(status.get("status")))
                )
        except Exception as exc:
            issues.append(f"watchdog_status_unreadable:{exc}")
    else:
        warnings.append("watchdog_status_missing")

    wiki_dir = get_wiki_dir()
    wiki_keys = wiki_page_keys(wiki_dir, NON_NODE_WIKI_FILES) if wiki_dir.exists() else set()
    canonical_keys = {
        row["page_key"] for row in conn.execute(
            "SELECT f_page_key AS page_key FROM entities "
            "WHERE f_page_key IS NOT NULL"
        )
        if row["page_key"] and not str(row["page_key"]).startswith("System_")
    }
    index_path = get_index_path()
    index_available = index_path.exists()
    if index_path.exists():
        try:
            index_keys = {
                key for key in json.loads(index_path.read_text(encoding="utf-8")).get("nodes", {})
                if not str(key).startswith("System_")
            }
        except Exception as exc:
            index_keys = set()
            issues.append(f"index_unreadable:{exc}")
    else:
        index_keys = set()
        warnings.append("index_missing")

    drift = {
        "wiki": len(wiki_keys),
        "index": len(index_keys),
        "canonical": len(canonical_keys),
        "missing_index": len(canonical_keys - index_keys) if index_available else 0,
        "extra_index": len(index_keys - canonical_keys) if index_available else 0,
        "missing_canonical": len(wiki_keys - canonical_keys),
        "extra_canonical": len(canonical_keys - wiki_keys),
    }
    detail["projection_drift"] = drift
    if any(drift[key] for key in ("missing_index", "extra_index", "missing_canonical", "extra_canonical")):
        degraded.append(
            "projection_drift:"
            f"missing_index={drift['missing_index']},"
            f"extra_index={drift['extra_index']},"
            f"missing_canonical={drift['missing_canonical']},"
            f"extra_canonical={drift['extra_canonical']}"
        )

    # The page index projection feeds query reads.  Reporting it here is what
    # keeps a dropped eager refresh from being invisible: the writer records the
    # failure in a marker, readers silently fall back to index.json, and without
    # this line the only symptom would be slower queries.  Repairable and never
    # blocking -- the repair is a write, and readers still answer from the file.
    try:
        from vector_lake import page_index_projection

        stamp = page_index_projection.index_file_stamp()
        if stamp is not None:
            state = page_index_projection.projection_state() or {}
            marker = page_index_projection.projection_stale()
            detail["page_index_projection"] = {
                "current": page_index_projection.projection_is_current(),
                "index_stamp": list(stamp),
                "recorded_stamp": [state.get("index_mtime"), state.get("index_size")],
                "edge_count": state.get("edge_count"),
                "recorded_at": state.get("updated_at"),
                "writer_failure": (marker or {}).get("reason"),
            }
            if not page_index_projection.projection_is_current():
                behind = (
                    "page_index_projection_behind:"
                    f"recorded={state.get('index_mtime')}/{state.get('index_size')} "
                    f"file={stamp[0]}/{stamp[1]}"
                )
                if marker:
                    behind += f"; writer failure: {marker.get('reason')}"
                degraded.append(behind)
            elif marker is not None:
                degraded.append(f"page_index_projection_stale_marker:{marker.get('reason')}")
    except Exception as exc:  # noqa: BLE001 - a health probe must not fail the assessment
        warnings.append(f"page_index_projection_probe_failed:{type(exc).__name__}")

    strict_timeline_parity = os.environ.get("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING") == "1"
    if deep_projection_checks or strict_timeline_parity:
        from vector_lake.tool_timeline import timeline_projection_parity

        timeline_drift = timeline_projection_parity()
        detail["timeline_projection_drift"] = timeline_drift
        if timeline_drift["missing"] or timeline_drift["extra"]:
            # Always repairable: a timeline rebuild is a write, so blocking here
            # would prevent the only available remedy.
            degraded.append(
                "timeline_projection_drift:"
                f"missing={timeline_drift['missing']},extra={timeline_drift['extra']}"
            )

        # The surfaces that point *at* claims had no reader that would notice a dead pointer:
        # ``evidence.supports_claim_ids`` named retired claims for two months (25 220 of them,
        # all but 6 from one 2026-07-14 bulk pass) and the ``claim_graph_edges`` delta filtered a
        # page-key table by claim ids, so it never removed a retired page's links.  Counted here
        # rather than left for an operator to find.  Edge endpoints the resolver cannot answer
        # are reported but not degraded: keeping the raw target is what the edge writer does on
        # purpose, so it is a link-quality signal, not drift.
        try:
            from vector_lake.tool_maintenance import claim_projection_drift

            pointer_drift = claim_projection_drift(prepare=False)
            detail["claim_pointer_drift"] = {
                key: value for key, value in pointer_drift.items() if key != "sample_edges"
            }
            if pointer_drift["dead_pointers"] or pointer_drift["memory_rows"]:
                degraded.append(
                    "claim_pointer_drift:"
                    f"evidence_rows={pointer_drift['evidence_rows']},"
                    f"dead_pointers={pointer_drift['dead_pointers']},"
                    f"memory_rows={pointer_drift['memory_rows']}"
                )
        except Exception as exc:  # noqa: BLE001 - a health probe must not fail the assessment
            warnings.append(f"claim_pointer_drift_probe_failed:{type(exc).__name__}")

    return {
        "ok": not issues and not degraded,
        "hard_ok": not issues,
        "issues": issues,
        "degraded": degraded,
        "warnings": warnings,
        "detail": detail,
    }


def enforce_runtime_write_health(validation_mode: str = "full", *, target_filenames: list[str] | tuple[str, ...] | set[str] | None = None):
    """Block a canonical write only on hard faults, never on repairable drift.

    Blocking on degradation would deadlock the system: projection drift, a
    stale watchdog heartbeat and an un-drained outbox are all repaired *by*
    the write path.  They are logged and surfaced instead.
    """
    if os.environ.get("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE") == "1":
        return
    if validation_mode == "schema":
        return
    health = assess_runtime_health()
    if health.get("degraded"):
        log.warning("Runtime degradation (non-blocking): %s", "; ".join(health["degraded"]))
    issues = list(health.get("issues") or [])
    if (
        len(issues) == 1
        and issues[0].startswith("mutation_outbox_failed:")
        and target_filenames
    ):
        failed_files = set(health.get("detail", {}).get("failed_outbox_filenames") or [])
        if failed_files and failed_files.issubset({str(f) for f in target_filenames if f}):
            log.warning("Allowing self-healing write for failed outbox target(s): %s", sorted(failed_files))
            return
    if not health.get("hard_ok", health["ok"]):
        raise RuntimeError(
            "Vector Lake write gate blocked this mutation on a hard runtime fault. "
            "Repair the listed fault, then retry. "
            f"Issues: {'; '.join(health['issues'])}"
        )
