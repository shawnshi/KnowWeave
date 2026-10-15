import importlib
import os
import sqlite3
import sys
import ast
import json
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.node_vocabulary import NON_NODE_WIKI_FILES
from vector_lake.wiki_utils import (
    get_index_path,
    get_memory_dir,
    get_raw_dir,
    get_wiki_dir,
    get_meta_dir,
    wiki_page_keys,
)
from vector_lake.db_store import (
    applied_schema_prunes,
    embedding_input_digests,
    embedding_projection_state,
    published_edge_projection_drift,
    get_db_path,
    get_connection,
    idempotency_index_state,
    legacy_schema_prune_names,
)
from vector_lake import get_extension_root
from vector_lake import memory_gram_index
from vector_lake.subagent_handoff import in_process_text_generation_ready
from vector_lake.runtime_health import assess_runtime_health

def _check_ast(module_path: Path) -> tuple[bool, str]:
    if not module_path.exists():
        return False, "file not found"
    try:
        with open(module_path, "r", encoding="utf-8") as f:
            ast.parse(f.read(), filename=module_path.name)
        return True, "AST OK"
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"
    except Exception as e:
        return False, f"Error: {e}"

def doctor_vector_lake(deep_dependency_check: bool = False) -> str:
    """Health report for the runtime.

    ``deep_dependency_check`` imports every declared dependency instead of resolving it,
    which catches a package that is present but whose own imports fail -- at a measured
    4.2 s, ``google.genai`` being 3.5 s of it.  The default resolves the module on the
    import path, which answers the same "installed?" question for ~0.5 ms per package.
    """
    checks = []
    warnings: list[str] = []

    # 1. Environment & Config
    python_ok = sys.version_info >= (3, 10)
    checks.append(("Python", python_ok, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))

    has_api_key = bool(os.environ.get("GEMINI_API_KEY"))
    checks.append((
        "GEMINI_API_KEY",
        has_api_key,
        "Set" if has_api_key else "Not set - vector/hybrid search degrades to BM25-only",
    ))

    # 2. Dependencies
    #
    # The presence probe is ``find_spec``, which resolves the module on the import path
    # without executing its body.  Importing each package for real answered the same
    # "installed?" question at a measured 4.2 s -- ``google.genai`` alone is 3.5 s -- and
    # that cost landed on every ``doctor``, including the MCP one.  The deep check (does it
    # actually import, so a broken transitive dependency is caught) is still available and
    # opt-in, because it is the same information at the same price; ``find_spec`` cannot
    # see a package whose own imports fail.
    dependencies = {
        "google.genai": "google-genai",
        "filelock": "filelock",
        "yaml": "PyYAML",
        "watchfiles": "watchfiles",
        "igraph": "igraph",
        "leidenalg": "leidenalg",
        "dotenv": "python-dotenv",
        "fastmcp": "fastmcp",
        "sqlite_vec": "sqlite-vec",
        "tantivy": "tantivy",
        "mistune": "mistune"
    }
    for module_name, package_name in dependencies.items():
        if deep_dependency_check:
            try:
                importlib.import_module(module_name)
                checks.append((package_name, True, "installed (imported)"))
            except Exception as exc:  # noqa: BLE001 - report the failure, do not raise it
                checks.append((package_name, False, f"import failed: {type(exc).__name__}"))
            continue
        try:
            found = importlib.util.find_spec(module_name) is not None
        except Exception as exc:  # noqa: BLE001 - a broken parent package is a missing one
            checks.append((package_name, False, f"unresolvable: {type(exc).__name__}"))
            continue
        checks.append((package_name, found, "installed" if found else "missing"))

    from vector_lake import tokenizer as _tokenizer
    tokenizer_backend = _tokenizer.backend_name()
    if tokenizer_backend == "unavailable":
        checks.append((
            "Tokenizer Backend",
            False,
            "no CJK tokenizer available; FTS pre-tokenization is disabled",
        ))
    else:
        detail = _tokenizer.backend_version()
        if not _tokenizer.supports_add_word():
            detail += "; no add_word() on this backend (custom dictionary terms ignored)"
        checks.append(("Tokenizer Backend", True, detail))

    try:
        import vector_lake_core
        checks.append(("Native Acceleration", True, f"vector-lake-core v{vector_lake_core.version()} (Rust fast-core active)"))
    except ImportError:
        checks.append(("Native Acceleration", True, "pure-python (optional vector-lake-core not installed)"))

    # The retrieval ledger is the only record of what search answered, so whether it is on and
    # how much it holds belongs in the same report as everything else that can silently be off.
    try:
        from vector_lake import search_ledger as _ledger

        if not _ledger.enabled():
            checks.append(("Search Ledger", True, "disabled by VECTOR_LAKE_SEARCH_LEDGER=0"))
        else:
            ledger = _ledger.summary()
            empty_rate = ledger.get("empty_rate")
            # A count alone says the ledger is on.  What makes it a readiness reading is whether
            # answers come back empty and how long they take; a ledger full of evaluation rows
            # (893 of 901 before the split) reported neither about the lake in use.
            checks.append((
                "Search Ledger",
                True,
                f"{ledger['entries']} production entr(ies), {ledger['distinct_queries']} distinct "
                f"quer(ies); empty {ledger.get('empty')} "
                f"({'' if empty_rate is None else str(round(empty_rate * 100)) + '%'}), "
                f"p50 {ledger.get('latency_ms_p50')}ms, errors {ledger.get('errors')}",
            ))
    except Exception as exc:  # noqa: BLE001 - a status line must not fail the report
        checks.append(("Search Ledger", False, f"unavailable: {type(exc).__name__}: {exc}"))

    # Community detection: Leiden via igraph + leidenalg (was Louvain).
    try:
        import igraph as _ig
        import leidenalg as _leiden

        checks.append((
            "Clustering Backend",
            True,
            f"leidenalg {getattr(_leiden, '__version__', '?')} (igraph {getattr(_ig, '__version__', '?')})",
        ))
    except ImportError as exc:
        checks.append(("Clustering Backend", False, f"leidenalg/igraph missing: {exc}"))

    llm_ok, llm_detail = in_process_text_generation_ready()
    if not llm_ok:
        # Text generation is delegated to the host agent by design, so this is an
        # expected operating mode rather than a failure.
        warnings.append(f"Subagent Text Runtime: {llm_detail}")
    else:
        checks.append(("Subagent Text Runtime", True, llm_detail or "available"))

    # 3. Paths & Basic Files
    for label, path in [("MEMORY", get_memory_dir()), ("Raw", get_raw_dir()), ("Wiki", get_wiki_dir())]:
        checks.append((label, path.exists(), str(path)))

    index_exists = get_index_path().exists()
    checks.append(("Index", index_exists, str(get_index_path()) if index_exists else "Lake is drying (Empty)"))
    for label, path in [("Meta", get_meta_dir()), ("SQLite DB", get_db_path())]:
        checks.append((label, path.exists(), str(path)))

    # 3b. Backup footprint against the retention bound.
    try:
        from vector_lake.backup_retention import plan_backup_retention

        plan = plan_backup_retention(get_meta_dir() / "backups")
        detail = (
            f"{plan['entry_count']} entr(ies), {plan['total_bytes'] / 1024 ** 3:.2f} GiB total; "
            f"bound keep<={plan['keep_count']} and <= {plan['max_bytes'] / 1024 ** 3:.1f} GiB"
        )
        over_bytes_bound = plan["total_bytes"] > plan["max_bytes"]
        if plan["remove"]:
            detail += (
                f"; {len(plan['remove'])} prunable ({plan['removable_bytes'] / 1024 ** 3:.2f} GiB)"
            )
            # Two different conditions used to share one name: "there is pruning pending
            # because more than ``keep_count`` entries exist" (normal between scheduled
            # occurrences, and the reported GiB was under the byte bound) and "the byte budget
            # is actually exceeded" (retention cannot keep up).  Naming them apart is the
            # difference between a routine note and a real gate.
            if over_bytes_bound:
                warnings.append(
                    f"backup_bytes_over_bound:{plan['total_bytes'] / 1024 ** 3:.2f}GiB > "
                    f"{plan['max_bytes'] / 1024 ** 3:.1f}GiB; {len(plan['remove'])} entr(ies) prunable"
                )
            else:
                warnings.append(
                    f"backup_retention_pending:{len(plan['remove'])} entr(ies) prunable by the next "
                    f"scheduled prune (total {plan['total_bytes'] / 1024 ** 3:.2f} GiB, "
                    f"keep<={plan['keep_count']})"
                )
        if plan["unrecognized"]:
            detail += f"; unrecognized (never pruned): {', '.join(plan['unrecognized'][:3])}"
        # Only a byte-budget breach is a check failure: a count-only backlog is what retention
        # removes on its next run, and failing on it made an ordinary state look broken.
        checks.append(("Backups", not over_bytes_bound, detail))
    except Exception as e:
        checks.append(("Backups", False, f"Check failed: {e}"))

    # 4. AST Compilation Checks
    ext_root = get_extension_root()
    for mod in ["mcp_server.py", "watchdog_app.py", "tool_ingest.py"]:
        mod_path = ext_root / "vector_lake" / mod
        ok, detail = _check_ast(mod_path)
        checks.append((f"AST Compile {mod}", ok, detail))

    # 5. MCP Discovery / Import check
    try:
        from vector_lake.mcp_server import mcp, registered_tool_names

        tools_count = len(registered_tool_names(mcp))
        checks.append(("MCP Server", tools_count > 0, f"Import OK, {tools_count} tools exposed"))
    except Exception as e:
        checks.append(("MCP Server", False, f"Startup Exception: {e}"))

    # 6. Watchdog Heartbeat
    status_path = get_meta_dir() / ".watchdog_status.json"
    if status_path.exists():
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                status = json.load(f)
            updated_at = status.get("updated_at")
            age_seconds = None
            if updated_at:
                updated_dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                age_seconds = max(0, int((datetime.now(timezone.utc) - updated_dt).total_seconds()))
            unhealthy_components = [
                name
                for name, component in (status.get("components") or {}).items()
                if str(component.get("status", "")).lower() in {"error", "halted"}
            ]
            heartbeat_ok = (
                age_seconds is not None
                and age_seconds <= 120
                and str(status.get("status", "")).lower() not in {"error", "halted"}
                and not unhealthy_components
            )
            detail = f"[{status.get('status', 'unknown')}] {status.get('current_action', '')}; age={age_seconds if age_seconds is not None else 'unknown'}s"
            checks.append(("Watchdog Status", heartbeat_ok, detail))
        except Exception as e:
            checks.append(("Watchdog Status", False, f"Parse error: {e}"))
    else:
        checks.append(("Watchdog Status", False, "No status file found (not running?)"))

    # 7. State Projection Consistency
    try:
        wiki_keys = wiki_page_keys(get_wiki_dir(), NON_NODE_WIKI_FILES)
        with open(get_index_path(), "r", encoding="utf-8") as f:
            index_keys = {
                key for key in json.load(f).get("nodes", {})
                if not str(key).startswith("System_")
            }
        conn = get_connection()
        try:
            canonical_keys = {
                row["page_key"] for row in conn.execute(
                    "SELECT f_page_key AS page_key FROM entities "
                    "WHERE f_page_key IS NOT NULL"
                )
                if not str(row["page_key"]).startswith("System_")
            }
        except sqlite3.OperationalError as exc:
            # The doctor is read-only by contract and must not run the migration that would add the
            # column; the surrounding handler turns this into a check that names the state instead
            # of a bare SQL error.
            raise RuntimeError(
                f"entities.f_page_key is missing ({exc}); the schema is not converged -- "
                "run any command that calls init_db()"
            ) from exc
        missing_index = canonical_keys - index_keys
        extra_index = index_keys - canonical_keys
        missing_canonical = wiki_keys - canonical_keys
        extra_canonical = canonical_keys - wiki_keys
        consistent = not (missing_index or extra_index or missing_canonical or extra_canonical)
        checks.append((
            "State Consistency",
            consistent,
            f"Wiki:{len(wiki_keys)} JSON:{len(index_keys)} SQLite:{len(canonical_keys)} "
            f"missing_index:{len(missing_index)} extra_index:{len(extra_index)} "
            f"missing_canonical:{len(missing_canonical)} extra_canonical:{len(extra_canonical)}",
        ))
    except Exception as e:
        checks.append(("State Consistency", False, f"Check failed: {e}"))

    # 7b. Database state.  Kept in its own block because the file projection above
    # can be missing or half-written (a drying lake, a rebuild in flight), and a
    # single shared ``try`` used to hide the whole SQLite surface -- outbox, jobs
    # and the write gate -- behind one missing ``index.json``.
    try:
        conn = get_connection()
        outbox_counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM mutation_outbox GROUP BY status")
        }
        outbox_ok = outbox_counts.get("failed", 0) == 0
        checks.append(("Mutation Outbox", outbox_ok, json.dumps(outbox_counts, ensure_ascii=False, sort_keys=True)))

        terminal_jobs = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND retries >= 3"
        ).fetchone()[0]
        queued_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
        awaiting_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'awaiting_subagent'").fetchone()[0]
        checks.append((
            "Ingest Jobs",
            terminal_jobs == 0,
            f"queued:{queued_jobs} awaiting_subagent:{awaiting_jobs} terminal_failed:{terminal_jobs}",
        ))

        health = assess_runtime_health(deep_projection_checks=True)
        checks.append((
            "Write Gate",
            health["hard_ok"],
            (
                "clean"
                + (f"; warnings: {'; '.join(health['warnings'])}" if health["warnings"] else "")
                if health["hard_ok"]
                else "; ".join(health["issues"])
            ),
        ))
        degraded = health.get("degraded") or []
        for item in degraded:
            warnings.append(item)
    except Exception as e:
        checks.append(("Database State", False, f"Check failed: {e}"))

    # 7c. The uniqueness guarantee these tables actually end up with.
    # ``full`` = a key may never repeat.  ``active`` = the table already held
    # duplicate history, so uniqueness is enforced over non-terminal rows only
    # (the population a concurrent enqueue can collide in).  ``absent`` means only
    # the BEGIN IMMEDIATE write lock is left.
    try:
        idempotency = idempotency_index_state()
        absent = [table for table, state in idempotency.items() if state["uniqueness"] == "absent"]
        checks.append((
            "Idempotency Index",
            not absent,
            ", ".join(
                f"{table}={state['uniqueness']}(dups={state['duplicate_groups']})"
                for table, state in sorted(idempotency.items())
            ),
        ))
        for table, state in sorted(idempotency.items()):
            if state["uniqueness"] != "full":
                warnings.append(
                    f"idempotency_index_degraded:{table}={state['uniqueness']} "
                    f"({state['duplicate_groups']} duplicate group(s); "
                    f"repair_idempotency_keys('{table}') reclaims the full index)"
                )
    except Exception as e:
        checks.append(("Idempotency Index", False, f"Check failed: {e}"))

    try:
        from vector_lake.db_store import processed_checksum_anomalies
        anomalies = processed_checksum_anomalies()
        checks.append(("Source Checksum Observations", True, f"invalid historical checksums={anomalies}; history unchanged"))
        if anomalies:
            warnings.append(f"invalid_source_checksums:{anomalies} (automatic dispatch withheld; verify provenance)")
    except Exception as e:
        checks.append(("Source Checksum Observations", False, f"Check failed: {e}"))

    # ``page_index_edges`` is the read projection of the published ``weighted_edges``.  A partial
    # update rewrites only the nodes it touches, so drift between the file and the projection is
    # invisible until something compares them -- which is what this check does, against the file.
    try:
        drift = published_edge_projection_drift()
        clean = not drift["extra"] and not drift["missing"] and not drift["duplicate_pairs"]
        detail = f"mirrors the published {drift['published_rows']} edge(s)"
        if not clean:
            parts = []
            if drift["published_read_error"]:
                parts.append(f"index.json unreadable ({drift['published_read_error']})")
            if drift["duplicate_pairs"]:
                parts.append("duplicate pairs in the projection")
            parts += [
                f"projection={drift['projection_rows']}",
                f"published={drift['published_rows']}",
                f"difference={drift['difference']}",
            ]
            if drift["extra_examples"]:
                shown = ", ".join(f"{a}->{b}" for a, b in drift["extra_examples"])
                parts.append(f"not published, e.g. {shown}")
            if drift["missing_example"]:
                parts.append(f"missing from the projection, e.g. {drift['missing_example'][0]}")
            detail = "; ".join(parts)
        checks.append(("Page Edge Projection", clean, detail))
        if not clean:
            warnings.append("page_edge_projection_drift")
    except Exception as e:
        checks.append(("Page Edge Projection", False, f"Check failed: {e}"))

    # Legacy schema residue is invisible from inside the tree: an object that no
    # release creates, reads or writes still sits in every database that a past
    # release wrote it into.  ``init_db()`` drops the recorded set; this check is
    # how an operator can see whether that actually happened.
    try:
        applied_prunes = applied_schema_prunes()
        pending_prunes = [
            name for name in legacy_schema_prune_names() if name not in applied_prunes
        ]
        checks.append((
            "Schema Migrations",
            not pending_prunes,
            (
                f"{len(applied_prunes)} prune(s) applied"
                if not pending_prunes
                else "pending: " + ", ".join(pending_prunes)
            ),
        ))
        for name in pending_prunes:
            warnings.append(f"schema_prune_pending:{name}")
    except Exception as e:
        checks.append(("Schema Migrations", False, f"Check failed: {e}"))

    # The exact n-gram index serves a read only when its base is a complete, current
    # snapshot; anything else falls back to the full scan and returns the same answer.
    # The predicate is not restated here: it used to be, as "a backlog small enough to
    # drain on the read path", and that mirror kept reporting the indexed path as live
    # after the read path stopped draining -- a 1-document backlog on a 146k-document
    # lake would have been called usable while every search scanned.
    #
    # Report the numbers that say *why* it is or is not live, so a 100 MB index that
    # is never consulted is visible instead of merely inferable.
    #
    # A database written before the index existed has no ``gram_state`` at all.
    # That is a supported degradation -- the same one ``gram_index_usable`` reports
    # -- not a doctor failure, so a missing table lands in the warning path too.
    try:
        conn = get_connection()
        try:
            gram_state = memory_gram_index.gram_index_state()
            gram_ready = bool(gram_state["ready"])
            gram_count = int(gram_state["gram_count"] or 0)
        except sqlite3.OperationalError:
            gram_ready, gram_count = False, 0
        try:
            total, live_backlog, retired = memory_gram_index.dirty_breakdown(conn)
        except sqlite3.OperationalError:
            total = live_backlog = retired = 0
        usable = memory_gram_index.gram_index_usable()
        try:
            due = memory_gram_index.rebuild_due()
        except sqlite3.OperationalError:
            due = False
        checks.append((
            "Memory Gram Index",
            True,
            f"usable={usable} ready={gram_ready} grams={gram_count} "
            f"queued={total} live_backlog={live_backlog} retired={retired} "
            f"cap={memory_gram_index.AUTO_REBUILD_MAX_DOCS} "
            f"due={due} of {memory_gram_index.REBUILD_AFTER_WRITES}",
        ))
        if not usable:
            warnings.append(
                f"memory_gram_index_unusable:live_backlog={live_backlog} retired={retired} "
                f"due={due} of "
                f"{memory_gram_index.REBUILD_AFTER_WRITES} "
                "(rebuild: python cli.py gram-index --if-due --apply)"
            )
    except Exception as e:
        checks.append(("Memory Gram Index", False, f"Check failed: {e}"))

    # The vector projection is the second ranking arm, and a gap in it is invisible from the
    # outside: a 36%-full ``vec_embeddings`` still answers every query, just with fewer
    # semantic candidates and a systematic bias toward whichever pages happened to keep a
    # vector.  Measured 2026-09-21: 2 602 of 7 175 nodes had one, every page rewritten on or
    # after 2026-09-16 was among the missing, and nothing in this report mentioned it --
    # ``embedding_coverage`` was referenced only from its own module.
    #
    # An incomplete projection is a repairable degradation, not a failure: the periodic
    # catch-up sweep refills it in batches, so the check stays OK and the gap is a warning
    # (same shape as the gram index above).
    try:
        from vector_lake.embedding_scheduler import (
            embedding_coverage,
            page_bodies_for_keys,
            stale_embedding_keys,
        )

        index_path = get_index_path()
        if index_path.exists():
            with open(index_path, "r", encoding="utf-8") as handle:
                index_data = json.load(handle)
        else:
            index_data = {"nodes": {}}
        coverage = embedding_coverage(index_data)
        node_keys = list((index_data.get("nodes") or {}).keys())
        try:
            recorded = embedding_input_digests()
        except sqlite3.OperationalError:
            # A database that has not run ``init_db()`` since the ledger was introduced has no
            # ledger, and the doctor must not run the migration.  Everything then reads as
            # unstamped, which is the honest statement: no vector here has verified provenance.
            recorded = {}
        unstamped = {key for key in node_keys if key not in recorded}
        stale_inputs = stale_embedding_keys(
            index_data, page_bodies_for_keys(node_keys), recorded=recorded
        ) - unstamped
        checks.append((
            "Vector Projection",
            True,
            f"nodes={coverage['nodes']} embedded={coverage['embedded']} "
            f"missing={coverage['missing']} stale={coverage['stale']} "
            f"stale_inputs={len(stale_inputs)} unstamped={len(unstamped)}",
        ))
        if coverage["missing"]:
            warnings.append(
                f"vector_projection_incomplete:missing={coverage['missing']} of "
                f"{coverage['nodes']} nodes (refilled in batches by the periodic catch-up "
                "sweep; force now: python cli.py embedding-backfill --apply)"
            )
        if stale_inputs:
            warnings.append(
                f"vector_input_stale:{len(stale_inputs)} node(s) have a vector that was built from "
                "input which has since changed -- present, but answering from old text "
                "(rebuilt in batches by the periodic catch-up sweep; force now: python cli.py "
                "embedding-backfill --apply)"
            )
        if unstamped:
            warnings.append(
                f"vector_input_unstamped:{len(unstamped)} node(s) predate the input ledger, so "
                "their vectors cannot be verified as current and are rebuilt to establish it "
                "(in batches by the periodic catch-up sweep; force now: python cli.py "
                "embedding-backfill --apply)"
            )
        if coverage["stale"]:
            warnings.append(
                f"vector_projection_orphaned:{coverage['stale']} row(s) with no node "
                "(the next index rebuild removes them)"
            )
    except Exception as e:
        checks.append(("Vector Projection", False, f"Check failed: {e}"))

    # Which model produced the stored vectors.  ``embedding_runs`` records the model per run, so
    # on its own it cannot answer this once more than one run exists -- and a changed
    # ``VECTOR_LAKE_EMBEDDING_MODEL`` moves the query encoder while the stored vectors stay put,
    # turning every similarity into a number with no meaning and no visible symptom.
    #
    # The marker is the exact answer, but it is only written when a backfill writes rows, so a
    # projection that is already complete has no marker until the next one runs.  Falling back to
    # a single-model run ledger is the best available statement in that case, and it still catches
    # the misconfiguration: a homogeneous ledger that disagrees with the configured model means
    # the config moved after the last write, which is exactly the silent case.  The check line
    # names which basis it used, so an inference is never read as a recording.
    try:
        from vector_lake.embedding_scheduler import load_embedding_rate_config

        config = load_embedding_rate_config()
        try:
            state = embedding_projection_state()
        except sqlite3.OperationalError:
            # A database that has not run ``init_db()`` since the marker was introduced has no
            # table yet, and the doctor is read-only by contract: it reports the state instead of
            # running the migration.  Same supported degradation as a pre-gram-index database.
            state = {}
        recorded = state.get("model")
        run_models = {
            str(row[0]) for row in get_connection().execute(
                "SELECT DISTINCT model FROM embedding_runs WHERE model IS NOT NULL AND model <> ''"
            )
        }
        if recorded:
            basis, claimed = "marker", str(recorded)
        elif len(run_models) == 1:
            basis, claimed = "run-ledger", next(iter(run_models))
        else:
            basis, claimed = "", ""
        checks.append((
            "Vector Model",
            True,
            f"stored={claimed or '<unknown>'} (by {basis or 'nothing'}) "
            f"dimension={state.get('dimension') or '<unrecorded>'} "
            f"configured={config.model}/{config.dimension} runs={sorted(run_models)}",
        ))
        if not claimed:
            warnings.append(
                "vector_model_unrecorded:no marker and no single-model run ledger, so the model "
                "behind the stored vectors cannot be checked (stamp it: python cli.py "
                "embedding-backfill --apply)"
            )
        elif claimed != str(config.model):
            warnings.append(
                f"vector_model_changed:stored={claimed} (by {basis}) configured={config.model}; "
                "the query encoder and the stored vectors are in different spaces, so rankings "
                "are meaningless (rebuild: python cli.py embedding-backfill --apply --include-existing)"
            )
        if len(run_models) > 1:
            warnings.append(
                f"vector_model_mixed_history:{sorted(run_models)} appear in embedding_runs, so "
                "rows from more than one space may be stored together (rebuild: python cli.py "
                "embedding-backfill --apply --include-existing)"
            )
    except Exception as e:
        checks.append(("Vector Model", False, f"Check failed: {e}"))

    lines = ["=== Vector Lake Doctor ==="]
    all_ok = True
    for label, ok, detail in checks:
        lines.append(f"[{'OK' if ok else 'FAIL'}] {label}: {detail}")
        all_ok = all_ok and ok
    lines.append("")
    if warnings:
        lines.append(f"[WARN] Runtime degradation ({len(warnings)}): " + "; ".join(warnings))
        lines.append("        These are self-healing or repairable and do not block writes.")
        lines.append("")
    if not all_ok:
        lines.append("Summary: issues detected")
    elif warnings:
        lines.append("Summary: healthy with degradation")
    else:
        lines.append("Summary: healthy")
    lines.append(f"VECTOR_LAKE_MEMORY_DIR={os.environ.get('VECTOR_LAKE_MEMORY_DIR', '<default>')}")
    return "\n".join(lines)

