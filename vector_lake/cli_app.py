import argparse
import json
import io
import sys

try:
    import dotenv
except ImportError:
    dotenv = None

from vector_lake import host_env


def _configure_stdout():
    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _load_env():
    if dotenv is None:
        return
    env_path = host_env.legacy_env_file()
    if env_path.exists():
        dotenv.load_dotenv(str(env_path))


def _ops_arguments(parser):
    parser.add_argument("--db", required=True, help="Explicit existing SQLite path; no writable configuration helpers.")
    parser.add_argument("--meta-dir", help="Public heartbeat root; defaults to database parent.")
    parser.add_argument("--budget-seconds", type=float, default=2.0, help="SQL observation budget, maximum 30 seconds.")
    parser.add_argument("--json", action="store_true", help="Structured aggregate observations; no private identifiers or content.")


def build_parser() -> argparse.ArgumentParser:
    from vector_lake import tools

    parser = argparse.ArgumentParser(
        description="Vector Lake CLI Gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage Examples:
  python cli.py sync
  python cli.py search "MSL" --domain Medical_IT --top_k 5
  python cli.py search "deployment target" --domain System_Architecture --mode memory
  python cli.py review
  python cli.py review resolve 0
  python cli.py review resolve review_ab12cd34ef56
  python cli.py delete "/path/to/raw/file.pdf"            # dry-run by default
  python cli.py delete "/path/to/raw/file.pdf" --apply     # perform the delete
  python cli.py doctor
""",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Available wiki operations")

    ops_parser = subparsers.add_parser("ops-status", help="[OBSERVE] Bounded read-only backlog, leases, projection and public heartbeat observations.")
    _ops_arguments(ops_parser)

    subparsers.add_parser("sync", help="[INGEST] Generates MCP ingestion instructions for Native Subagents.")
    recompile_parser = subparsers.add_parser("recompile-ingest", help="[INGEST] Validate/hand off an explicitly approved 60-source current-version recompile.")
    recompile_parser.add_argument("--plan", required=True)
    recompile_parser.add_argument("--plan-sha256", required=True)
    recompile_parser.add_argument("--approval", required=True)
    recompile_parser.add_argument("--approval-sha256", required=True)
    recompile_parser.add_argument("--apply", action="store_true", help="Hand off native tasks; default is validation only.")
    recompile_parser.add_argument("--batch-size", type=int, default=1)
    recompile_parser.add_argument("--defer-filepath", help="Explicitly retain one unowned Source without recompiling its raw.")
    recompile_parser.add_argument("--defer-sha256", help="Approved current raw SHA-256 paired with --defer-filepath.")

    ingest_tasks_parser = subparsers.add_parser("ingest-tasks", help="[INGEST] List or expire subagent ingest tasks.")
    ingest_tasks_parser.add_argument("--limit", type=int, default=20, help="Maximum number of jobs to list.")
    ingest_tasks_parser.add_argument("--awaiting-only", action="store_true", help="Hide queued jobs and show only awaiting-subagent jobs.")
    ingest_tasks_parser.add_argument("--expire-stale", action="store_true", help="Expire stale awaiting-subagent jobs instead of listing.")
    ingest_tasks_parser.add_argument("--claim", action="store_true", help="Lease awaiting task packets to this host runtime.")
    ingest_tasks_parser.add_argument("--max-age-seconds", type=int, default=86400, help="Age threshold for --expire-stale.")
    ingest_tasks_parser.add_argument("--lease-seconds", type=int, default=3600, help="Lease duration for --claim.")
    ingest_tasks_parser.add_argument("--abandoned", action="store_true", help="List sources withheld after repeated deterministic failures.")
    ingest_tasks_parser.add_argument("--terminal-failed", action="store_true", help="List jobs that spent their attempt budget.")
    ingest_tasks_parser.add_argument("--close-terminal-failed", action="store_true", help="Mark terminal-failed jobs superseded once their source is ingested.")
    ingest_tasks_parser.add_argument("--clear-abandoned", nargs="?", const="", default=None, metavar="FILE", help="Allow abandoned source(s) to be dispatched again (all, or one FILE).")

    lint_parser = subparsers.add_parser("lint", help="[LINT] Run self-healing audit on the Wiki nodes.")
    lint_parser.add_argument("--auto-fix", action="store_true", help="Automatically fix issues such as decaying notes.")

    search_parser = subparsers.add_parser("search", help="[SEARCH] CJK-aware search with graph expansion.")
    search_parser.add_argument("query", help="Semantic query string.")
    search_parser.add_argument("--top_k", type=int, default=5, help="Number of results (default: 5).")
    search_parser.add_argument("--domain", type=str, required=True, help="Exact authorized domain; General is not all domains.")
    search_parser.add_argument("--allow-external-embedding", action="store_true", help="Permit sending this query to the embedding provider.")
    search_parser.add_argument("--cluster", type=str, default=None, help="Filter by topic cluster.")
    search_parser.add_argument("--include-history", action="store_true", help="Bypass temporal invalidation to search deprecated facts.")
    search_parser.add_argument("--mode", choices=["page", "memory", "claim"], default="page", help="Search page index, operational memory, or fact claims.")
    search_parser.add_argument("--profile", choices=['auto','legacy','balanced','entity','source','author','analysis'], default='auto')
    search_parser.add_argument("--use-chunks", action='store_true', help='Use an already-built scoped chunk sidecar; never builds it.')

    query_parser = subparsers.add_parser("query", help="[QUERY] Deep reasoning with budget-controlled context.")
    query_parser.add_argument("query_str", help="The topic or command for reasoning.")
    query_parser.add_argument("--domain", required=True, help="Exact authorized domain, e.g. Medical_IT.")
    query_parser.add_argument("--cluster", default=None, help="Optional exact topic cluster.")
    query_parser.add_argument("--allow-external-embedding", action="store_true", help="Permit sending this query to the embedding provider.")
    query_parser.add_argument("--dry-run", action="store_true", help="Prepare context/provenance without publishing Wiki pages; still creates a temporary payload.")
    query_parser.add_argument("--profile", choices=['auto','legacy','balanced','entity','source','author','analysis'], default='auto')
    query_parser.add_argument("--use-chunks", action='store_true')

    chunks_parser = subparsers.add_parser('chunk-index', help='[SEARCH] Preview/build chunks for explicit approved pages; no automatic corpus scan.')
    chunks_parser.add_argument('--domain', required=True)
    chunks_parser.add_argument('--page', action='append', required=True, help='Existing indexed page key; repeat for at most eight pages.')
    chunks_parser.add_argument('--apply', action='store_true')
    chunks_parser.add_argument('--allow-external-embedding', action='store_true')
    chunks_parser.add_argument('--budget-seconds', type=float, default=60)
    eval_parser = subparsers.add_parser('retrieval-eval', help='[SEARCH] Offline evaluation of explicit local judgments/results; never calls a model.')
    eval_parser.add_argument('--dataset', required=True)
    eval_parser.add_argument('--results', required=True)
    eval_parser.add_argument('--k', type=int, default=10)
    indexes_parser = subparsers.add_parser('retrieval-indexes', help='[MAINTENANCE] Inspect explicit SQLite retrieval accelerators; only --apply writes.')
    indexes_parser.add_argument('--db', required=True, help='Explicit existing SQLite file; never initialized automatically.')
    indexes_parser.add_argument('--apply', action='store_true')
    indexes_parser.add_argument('--budget-seconds', type=float, default=60)

    subparsers.add_parser("graph", help="[GRAPH] Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard.")
    timeline_rebuild_parser = subparsers.add_parser("timeline-rebuild", help="[TIMELINE] Rebuild timeline_events from timeline-event claims.")
    timeline_rebuild_parser.add_argument("--apply", action="store_true", help="Persist the rebuilt projection. Defaults to dry-run.")
    timeline_rebuild_parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of claims to project.")
    timeline_repair_parser = subparsers.add_parser(
        "timeline-repair",
        help="[TIMELINE] Close timeline_events parity drift in place instead of rebuilding the whole table.",
    )
    timeline_repair_parser.add_argument("--apply", action="store_true", help="Persist the repair. Defaults to dry-run.")

    gram_index_parser = subparsers.add_parser(
        "gram-index",
        help="[SEARCH] Report or rebuild the exact n-gram index used by operational-memory search.",
    )
    gram_index_parser.add_argument("--apply", action="store_true", help="Run the bulk rebuild. Defaults to dry-run.")
    gram_index_parser.add_argument("--if-due", action="store_true", dest="if_due", help="Rebuild only if REBUILD_AFTER_WRITES documents have been written since the last rebuild.")

    projection_report_parser = subparsers.add_parser("projection-report", help="[MAINTENANCE] Report Wiki / canonical / index drift.")
    projection_report_parser.add_argument("--limit", type=int, default=20, help="Sample size per drift bucket.")

    projections_parser = subparsers.add_parser("projections", help="[MAINTENANCE] Report derived-projection health; optionally reconcile.")
    projections_parser.add_argument("--reconcile", action="store_true", help="Repair the degraded projections. Defaults to a dry-run preview.")
    projections_parser.add_argument("--apply", action="store_true", help="With --reconcile, actually repair (default is dry-run).")
    projections_parser.add_argument("--only", default="", metavar="NAME", help="Restrict to one projection by name.")

    canonical_backfill_parser = subparsers.add_parser("canonical-backfill", help="[MAINTENANCE] Backfill missing canonical rows from Wiki pages.")
    canonical_backfill_parser.add_argument("--apply", action="store_true", help="Persist the backfill. Defaults to dry-run.")
    canonical_backfill_parser.add_argument("--limit", type=int, default=50, help="Maximum number of missing pages to process.")

    index_rebuild_parser = subparsers.add_parser("projection-rebuild-index", help="[MAINTENANCE] Rebuild index projection from canonical SQLite.")
    index_rebuild_parser.add_argument("--apply", action="store_true", help="Persist rebuilt index projection. Defaults to dry-run.")

    vector_index_parser = subparsers.add_parser("vector-index-rebuild", help="[MAINTENANCE] Rebuild the two-stage vector shadow index (binary prefilter + exact rerank).")
    vector_index_parser.add_argument("--apply", action="store_true", help="Persist the rebuild. Defaults to dry-run.")
    vector_index_parser.add_argument("--shortlist", type=int, default=None, help="Binary shortlist depth before the exact rerank (default: module default).")

    embedding_backfill_parser = subparsers.add_parser("embedding-backfill", help="[MAINTENANCE] Backfill missing vector embeddings under rate limits.")
    embedding_backfill_parser.add_argument("--apply", action="store_true", help="Persist embeddings. Defaults to dry-run.")
    embedding_backfill_parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of nodes to embed.")
    embedding_backfill_parser.add_argument("--include-existing", action="store_true", help="Re-embed nodes that already have vectors.")

    wiki_restore_parser = subparsers.add_parser("wiki-restore", help="[MAINTENANCE] Restore missing Wiki pages from canonical metadata.")
    wiki_restore_parser.add_argument("--apply", action="store_true", help="Persist restored Markdown pages. Defaults to dry-run.")
    wiki_restore_parser.add_argument("--limit", type=int, default=10, help="Maximum number of canonical-only pages to restore.")

    review_parser = subparsers.add_parser("review", help="[REVIEW] Inspect and resolve the unified legacy/governance review surface.")
    review_parser.add_argument("action", nargs="?", default="list", choices=["list", "resolve", "ground"], help="Action: 'list' (default), 'resolve', or 'ground'.")
    review_parser.add_argument("index", nargs="?", default="-1", help="Index or item_id of review item to resolve (for 'resolve' action).")
    review_parser.add_argument("--resolution", type=str, default="skip", help="Resolution type: 'skip', 'create', 'merge', 'acknowledge' (default: skip).")

    subparsers.add_parser("audit-graph", help="[AUDIT-GRAPH] Synthesize graph topology insights into the unified review surface.")
    subparsers.add_parser("doctor", help="[DOCTOR] Validate runtime dependencies and filesystem layout.")

    research_parser = subparsers.add_parser("research", help="[RESEARCH] Autonomously scan graph gaps and governance queue to formulate web research directives.")
    research_parser.add_argument("--dry-run", action="store_true", help="Preview the research queries without the SYSTEM DIRECTIVE execution hook.")


    debt_parser = subparsers.add_parser("debt", help="[DEBT] Show governance debt metrics.")
    debt_parser.add_argument("--top", type=int, default=20, help="Top debt window size.")

    trace_parser = subparsers.add_parser("trace", help="[TRACE] Show provenance trace for a query or identifier.")
    trace_parser.add_argument("query_or_id", help="Query text or object identifier.")

    merge_parser = subparsers.add_parser("merge-suggestions", help="[MERGE] Detect and enqueue candidate entity merges.")
    merge_parser.add_argument("--limit", type=int, default=20, help="Maximum number of merge candidates to surface.")
    merge_parser.add_argument("--preview", action="store_true", help="Do not enqueue governance items; only preview candidates.")

    delete_parser = subparsers.add_parser("delete", help="[DELETE] Cascade-delete a raw source and all related wiki pages.")
    delete_parser.add_argument("raw_path", help="Path to the raw source file to remove.")
    delete_parser.add_argument("--apply", action="store_true", help="Persist the cascade delete. Defaults to dry-run.")

    gc_parser = subparsers.add_parser("gc", help="[GC] Automatically prune isolated/orphan entities.")
    gc_parser.add_argument("--days", type=int, default=30, help="Prune entities older than this many days (default: 30).")
    gc_parser.add_argument("--apply", action="store_true", help="Persist the prune. Defaults to dry-run.")
    gc_parser.add_argument("--force", action="store_true", help="Bypass the 50%% mass-deletion safety threshold.")

    backup_parser = subparsers.add_parser(
        "backup-retention",
        help="[MAINTENANCE] Report or enforce the bound on .meta/backups.",
    )
    backup_parser.add_argument("--keep", type=int, default=0, help="Newest copies to retain (0 = configured default).")
    backup_parser.add_argument("--max-bytes", type=int, default=0, help="Byte budget for the older copies (0 = configured default).")
    backup_parser.add_argument("--apply", action="store_true", help="Remove the entries past the bound. Defaults to dry-run; the newest copy is always kept.")

    subparsers.add_parser(
        "idempotency-status",
        help="[MAINTENANCE] Report the uniqueness guarantee each idempotency table has.",
    )

    repair_parser = subparsers.add_parser(
        "repair-idempotency",
        help="[MAINTENANCE] Clear redundant idempotency keys so the full unique index can be created.",
    )
    repair_parser.add_argument("--table", choices=list(tools.IDEMPOTENCY_TABLES), default="mutation_outbox", help="Table to repair (default: mutation_outbox).")
    repair_parser.add_argument("--apply", action="store_true", help="Clear the redundant keys. Defaults to dry-run; no row is deleted either way.")

    subparsers.add_parser(
        "claim-pointer-report",
        help="[MAINTENANCE] Report pointers on claim-derived surfaces that name a missing claim.",
    )

    claim_pointer_parser = subparsers.add_parser(
        "claim-pointer-repair",
        help="[MAINTENANCE] Prune dead claim pointers (evidence), optionally re-key resolvable edges.",
    )
    claim_pointer_parser.add_argument("--apply", action="store_true", help="Write the repair. Defaults to dry-run; the rollback file is written first.")
    claim_pointer_parser.add_argument("--edges", action="store_true", help="Also re-key claim_graph_edges rows whose endpoint the link resolver can answer.")

    claim_evidence_parser = subparsers.add_parser(
        "claim-evidence-queue",
        help="[MAINTENANCE] Dispatch the unsupported-claim debt to the governance queue as cohort batches.",
    )
    claim_evidence_parser.add_argument("--apply", action="store_true", help="Enqueue the batches. Defaults to dry-run.")
    claim_evidence_parser.add_argument("--group", choices=["prefix", "month"], default="prefix", help="Cohort axis: page prefix (default) or the claim's created month.")
    claim_evidence_parser.add_argument("--batch-pages", type=int, default=100, help="Pages per governance item (default: 100).")
    claim_evidence_parser.add_argument("--page-limit", type=int, default=25, help="Page names stored on each item (default: 25).")

    provenance_backfill_parser = subparsers.add_parser(
        "provenance-backfill",
        help="[MAINTENANCE] Restore `sources:` declarations the ingest ledgers still record.",
    )
    provenance_backfill_parser.add_argument("--apply", action="store_true", help="Write the restorations. Defaults to dry-run; every page's prior text is written to the rollback file first.")
    provenance_backfill_parser.add_argument("--batch", type=int, default=50, help="Pages per mutation batch (default: 50).")
    provenance_backfill_parser.add_argument("--limit", type=int, default=None, help="Restore at most this many pages (for a canary run).")
    provenance_backfill_parser.add_argument("--revert", default=None, metavar="ROLLBACK_FILE", help="Instead of restoring, put back every page named in this rollback file.")

    provenance_accept_parser = subparsers.add_parser(
        "provenance-accept",
        help="[MAINTENANCE] Record that the pages whose provenance was never recorded are accepted as legacy debt.",
    )
    provenance_accept_parser.add_argument("--apply", action="store_true", help="Write the acceptance ledger. Defaults to dry-run.")

    subparsers.add_parser(
        "anchor-draft",
        help="[MAINTENANCE] Draft anchors for multi-source blocks that never named which source they used.",
    )

    anchor_backfill_parser = subparsers.add_parser(
        "anchor-backfill",
        help="[MAINTENANCE] Write the confirmed anchors and re-extract, so evidence can attach.",
    )
    anchor_backfill_parser.add_argument("--apply", action="store_true", help="Write the anchors. Defaults to dry-run; each page's prior text lands in the rollback file first.")
    anchor_backfill_parser.add_argument("--only", default=None, metavar="LIST", help="Confirm only these review numbers, e.g. 1,2,5,9-14 (default: every proposal).")
    anchor_backfill_parser.add_argument("--batch", type=int, default=10, help="Pages per mutation batch (default: 10).")
    return parser


def main() -> int:
    _configure_stdout()
    if sys.argv[1:2] == ["ops-status"]:
        parser = argparse.ArgumentParser(prog="cli.py ops-status", description="Bounded read-only aggregate observations")
        _ops_arguments(parser)
        args = parser.parse_args(sys.argv[2:])
        from vector_lake.operational_status import collect_status, render_status

        report = collect_status(args.db, meta_dir=args.meta_dir, budget_seconds=args.budget_seconds)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_status(report))
        return 0 if report["database"]["state"] == "healthy" else 1
    parser = build_parser()
    args = parser.parse_args()
    _load_env()
    from vector_lake import tools

    try:
        if args.command == "sync":
            print(tools.sync_vector_lake())
        elif args.command == "recompile-ingest":
            from vector_lake.controlled_recompile import controlled_recompile
            result = controlled_recompile(args.plan, args.plan_sha256, args.approval, args.approval_sha256,
                                          apply=args.apply, batch_size=args.batch_size,
                                          defer_filepath=args.defer_filepath, defer_sha256=args.defer_sha256)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "ingest-tasks":
            if getattr(args, "close_terminal_failed", False):
                print(tools.close_terminal_failed_ingest_jobs())
            elif getattr(args, "terminal_failed", False):
                print(tools.list_terminal_failed_ingest_jobs())
            elif getattr(args, "clear_abandoned", None) is not None:
                target = getattr(args, "clear_abandoned") or None
                print(tools.clear_abandoned_ingest_sources(target))
            elif getattr(args, "abandoned", False):
                print(tools.list_abandoned_ingest_sources())
            elif getattr(args, "expire_stale", False):
                print(tools.expire_ingest_tasks(getattr(args, "max_age_seconds", 86400)))
            elif getattr(args, "claim", False):
                print(tools.claim_ingest_tasks(
                    limit=getattr(args, "limit", 20),
                    lease_seconds=getattr(args, "lease_seconds", 3600),
                ))
            else:
                print(tools.list_ingest_tasks(
                    limit=getattr(args, "limit", 20),
                    include_queued=not getattr(args, "awaiting_only", False),
                ))
        elif args.command == "search":
            if not args.domain.strip():
                raise ValueError('Explicit non-empty domain is required')
            print(tools.search_vector_lake(
                args.query,
                args.top_k,
                domain=args.domain,
                cluster=args.cluster or '',
                include_history=getattr(args, "include_history", False),
                mode=getattr(args, "mode", "page"),
                allow_external_embedding=args.allow_external_embedding,
                profile=args.profile, use_chunks=args.use_chunks,
            ))
        elif args.command == "lint":
            print(tools.lint_vector_lake(getattr(args, "auto_fix", False)))
        elif args.command == "query":
            if not args.domain.strip():
                raise ValueError('Explicit non-empty domain is required')
            print(tools.prepare_query_context(
                args.query_str, getattr(args, "dry_run", False), domain=args.domain,
                cluster=args.cluster, allow_external_embedding=args.allow_external_embedding,
                profile=args.profile, use_chunks=args.use_chunks,
            ))
        elif args.command == 'chunk-index':
            from vector_lake.chunk_index import build_selected_pages
            print(json.dumps(build_selected_pages(args.page, args.domain, apply=args.apply,
                allow_external_embedding=args.allow_external_embedding, budget_seconds=args.budget_seconds), ensure_ascii=False, indent=2))
        elif args.command == 'retrieval-indexes':
            from vector_lake.scoped_retrieval_index import inspect_file
            print(json.dumps(inspect_file(args.db, apply_changes=args.apply, budget_seconds=args.budget_seconds), ensure_ascii=False, indent=2))
        elif args.command == 'retrieval-eval':
            from vector_lake.retrieval_eval import evaluate_files
            print(json.dumps(evaluate_files(args.dataset, args.results, args.k), ensure_ascii=False, indent=2))
        elif args.command == "graph":
            print(tools.visualize_vector_lake())
        elif args.command == "timeline-rebuild":
            print(tools.rebuild_timeline_events_from_claims(
                dry_run=not getattr(args, "apply", False),
                limit=getattr(args, "limit", None),
            ))
        elif args.command == "timeline-repair":
            print(tools.repair_timeline_projection(
                dry_run=not getattr(args, "apply", False),
            ))
        elif args.command == "gram-index":
            print(tools.memory_gram_index_report())
            if getattr(args, "if_due", False):
                # The cadence's operator entry point: rebuild only when the write count
                # says so, so this can be run on a schedule without rebuilding a corpus
                # that has not moved.
                print(tools.maybe_rebuild_memory_gram_index(dry_run=not getattr(args, "apply", False)))
            elif getattr(args, "apply", False):
                print(tools.rebuild_memory_gram_index(dry_run=False))
            else:
                print(tools.rebuild_memory_gram_index(dry_run=True))
        elif args.command == "projections":
            from vector_lake import projection_registry

            if getattr(args, "reconcile", False):
                results = projection_registry.reconcile(
                    only=getattr(args, "only", "") or None,
                    dry_run=not getattr(args, "apply", False),
                )
                for name, entry in results.items():
                    line = f"{name}: {entry['action']} -- {str(entry.get('detail'))[:90]}"
                    if entry.get("entry"):
                        line += f" -> {entry['entry']}"
                    if entry.get("error"):
                        line += f" !! {entry['error']}"
                    print(line)
            else:
                for name, entry in projection_registry.status().items():
                    kind = "auto" if entry["repairable"] else "manual"
                    print(
                        f"{name:18s} {entry['state']:9s} [{kind:6s}] {entry['detail']}"
                        f"   (authority: {entry['authority']})"
                    )
        elif args.command == "projection-report":
            print(tools.projection_diff_report(limit=getattr(args, "limit", 20)))
        elif args.command == "canonical-backfill":
            print(tools.canonical_backfill_missing_wiki(
                dry_run=not getattr(args, "apply", False),
                limit=getattr(args, "limit", 50),
            ))
        elif args.command == "projection-rebuild-index":
            print(tools.rebuild_index_projection(dry_run=not getattr(args, "apply", False)))
        elif args.command == "vector-index-rebuild":
            from vector_lake.two_stage_index import DEFAULT_SHORTLIST
            print(tools.rebuild_two_stage_vector_index(
                dry_run=not getattr(args, "apply", False),
                shortlist=getattr(args, "shortlist", None) or DEFAULT_SHORTLIST,
            ))
        elif args.command == "embedding-backfill":
            print(tools.embedding_backfill_projection(
                dry_run=not getattr(args, "apply", False),
                limit=getattr(args, "limit", None),
                include_existing=getattr(args, "include_existing", False),
            ))
        elif args.command == "wiki-restore":
            print(tools.restore_missing_wiki_from_canonical(
                dry_run=not getattr(args, "apply", False),
                limit=getattr(args, "limit", 10),
            ))
        elif args.command == "review":
            print(tools.review_vector_lake(action=args.action, index=args.index, resolution=getattr(args, "resolution", "skip")))
        elif args.command == "audit-graph":
            print(tools.audit_graph())
        elif args.command == "doctor":
            print(tools.doctor_vector_lake())
        elif args.command == "research":
            print(tools.research_vector_lake(getattr(args, "dry_run", False)))
        elif args.command == "debt":
            print(tools.debt_vector_lake(getattr(args, "top", 20)))
        elif args.command == "trace":
            print(tools.trace_vector_lake(args.query_or_id))
        elif args.command == "merge-suggestions":
            print(tools.merge_suggestions_vector_lake(limit=getattr(args, "limit", 20), enqueue=not getattr(args, "preview", False)))
        elif args.command == "delete":
            print(tools.delete_source(args.raw_path, dry_run=not getattr(args, "apply", False)))
        elif args.command == "gc":
            print(tools.gc_vector_lake(
                days=getattr(args, "days", 30),
                dry_run=not getattr(args, "apply", False),
                force=getattr(args, "force", False),
            ))
        elif args.command == "backup-retention":
            print(tools.backup_retention_report(
                keep=getattr(args, "keep", 0),
                max_bytes=getattr(args, "max_bytes", 0),
                dry_run=not getattr(args, "apply", False),
            ))
        elif args.command == "idempotency-status":
            print(tools.idempotency_index_report())
        elif args.command == "repair-idempotency":
            print(tools.repair_idempotency_keys(
                table=getattr(args, "table", "mutation_outbox"),
                dry_run=not getattr(args, "apply", False),
            ))
        elif args.command == "claim-pointer-report":
            print(tools.claim_projection_drift_report())
        elif args.command == "claim-pointer-repair":
            print(tools.repair_claim_pointers(
                dry_run=not getattr(args, "apply", False),
                edges=getattr(args, "edges", False),
            ))
        elif args.command == "claim-evidence-queue":
            print(tools.claim_evidence_queue(
                dry_run=not getattr(args, "apply", False),
                group=getattr(args, "group", "prefix"),
                batch_pages=getattr(args, "batch_pages", 100),
                page_limit=getattr(args, "page_limit", 25),
            ))
        elif args.command == "provenance-backfill":
            if getattr(args, "revert", None):
                print(tools.revert_provenance_backfill(
                    args.revert,
                    batch=getattr(args, "batch", 50),
                    dry_run=not getattr(args, "apply", False),
                ))
            else:
                print(tools.backfill_provenance(
                    dry_run=not getattr(args, "apply", False),
                    batch=getattr(args, "batch", 50),
                    limit=getattr(args, "limit", None),
                ))
        elif args.command == "provenance-accept":
            print(tools.accept_unrecorded_provenance(
                dry_run=not getattr(args, "apply", False),
            ))
        elif args.command == "anchor-draft":
            print(tools.draft_anchors())
        elif args.command == "anchor-backfill":
            print(tools.backfill_anchors(
                only=getattr(args, "only", None),
                apply=getattr(args, "apply", False),
                batch=getattr(args, "batch", 10),
            ))
    except Exception as exc:
        print(f"Error executing command '{args.command}': {exc}", file=sys.stderr)
        return 1
    return 0
