from vector_lake.tool_delete import delete_source
from vector_lake.tool_doctor import doctor_vector_lake
from vector_lake.tool_debt import debt_vector_lake
from vector_lake.tool_gc import gc_vector_lake
from vector_lake.tool_graph import audit_graph, visualize_vector_lake
from vector_lake.tool_lint import lint_vector_lake
from vector_lake.tool_merge import merge_suggestions_vector_lake
from vector_lake.tool_entity_dedup import check_duplicate_entity
from vector_lake.tool_ingest import (
    claim_ingest_tasks,
    clear_abandoned_ingest_sources,
    close_terminal_failed_ingest_jobs,
    expire_ingest_tasks,
    finalize_ingest,
    list_abandoned_ingest_sources,
    list_terminal_failed_ingest_jobs,
    list_ingest_tasks,
    prepare_ingest_batch,
)
from vector_lake.tool_query import prepare_query_context, preview_query_context, finalize_query_synthesis
from vector_lake.query_evidence_host import configured_query_evidence as get_query_evidence
from vector_lake.tool_review import review_vector_lake
from vector_lake.tool_search import assemble_context, search_vector_lake
from vector_lake.tool_sync import sync_vector_lake
from vector_lake.two_stage_index import rebuild_report as rebuild_two_stage_vector_index
from vector_lake.tool_trace import trace_vector_lake
from vector_lake.tool_timeline import (
    rebuild_timeline_events_from_claims,
    repair_timeline_projection,
)
from vector_lake.memory_gram_index import (
    maybe_rebuild_memory_gram_index,
    memory_gram_index_report,
    rebuild_memory_gram_index,
)
from vector_lake.tool_projection import (
    canonical_backfill_missing_wiki,
    embedding_backfill_projection,
    projection_diff_report,
    rebuild_index_projection,
    restore_missing_wiki_from_canonical,
)
from vector_lake.tool_research import research_vector_lake
from vector_lake.tool_purpose import review_strategic_purpose
from vector_lake.tool_maintenance import (
    IDEMPOTENCY_TABLES,
    backup_retention_report,
    claim_projection_drift_report,
    idempotency_index_report,
    repair_claim_pointers,
    repair_idempotency_keys,
)
from vector_lake.evidence_gap_dispatch import claim_evidence_queue
from vector_lake.provenance_backfill import backfill_provenance, revert_provenance_backfill
from vector_lake.provenance_legacy import accept_unrecorded_provenance
from vector_lake.anchor_backfill import backfill_anchors, draft_anchors


__all__ = [
    "assemble_context",
    "get_query_evidence",
    "audit_graph",
    "check_duplicate_entity",
    "claim_ingest_tasks",
    "delete_source",
    "debt_vector_lake",
    "doctor_vector_lake",
    "finalize_ingest",
    "expire_ingest_tasks",
    "list_abandoned_ingest_sources",
    "list_terminal_failed_ingest_jobs",
    "close_terminal_failed_ingest_jobs",
    "clear_abandoned_ingest_sources",
    "finalize_query_synthesis",
    "gc_vector_lake",
    "lint_vector_lake",
    "merge_suggestions_vector_lake",
    "list_ingest_tasks",
    "prepare_ingest_batch",
    "prepare_query_context",
    "preview_query_context",
    "canonical_backfill_missing_wiki",
    "embedding_backfill_projection",
    "projection_diff_report",
    "rebuild_index_projection",
    "rebuild_two_stage_vector_index",
    "restore_missing_wiki_from_canonical",
    "research_vector_lake",
    "review_vector_lake",
    "review_strategic_purpose",
    "rebuild_timeline_events_from_claims",
    "repair_timeline_projection",
    "rebuild_memory_gram_index",
    "maybe_rebuild_memory_gram_index",
    "memory_gram_index_report",
    "IDEMPOTENCY_TABLES",
    "backup_retention_report",
    "claim_projection_drift_report",
    "claim_evidence_queue",
    "backfill_provenance",
    "revert_provenance_backfill",
    "accept_unrecorded_provenance",
    "draft_anchors",
    "backfill_anchors",
    "idempotency_index_report",
    "repair_idempotency_keys",
    "repair_claim_pointers",
    "search_vector_lake",
    "sync_vector_lake",
    "trace_vector_lake",
    "visualize_vector_lake",
]

