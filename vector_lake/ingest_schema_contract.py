"""Model-facing reflection of the actual validator; never a second rule owner."""
from __future__ import annotations

import json
from pathlib import Path

from vector_lake import schema_validator as schema
from vector_lake.node_vocabulary import GENERATED_NODE_TYPES, NODE_PREFIXES, type_for_prefix
from vector_lake.purpose_contract import ALLOWED_SCOPES
from vector_lake.template_loader import render_template


def schema_snapshot() -> dict:
    """Return fresh JSON-safe vocabularies without reading prose or user policy."""
    return {
        "required_frontmatter": list(schema.REQUIRED_FIELDS),
        "types": sorted(schema.VALID_TYPES),
        "knowledge_prefixes": [prefix for prefix in NODE_PREFIXES if type_for_prefix(prefix) not in GENERATED_NODE_TYPES],
        "strategic_scopes": sorted(ALLOWED_SCOPES),
        "categories": sorted(schema.VALID_CATEGORIES),
        "status": sorted(schema.VALID_STATUS),
        "epistemic-status": sorted(schema.VALID_EPISTEMIC_STATUS),
        "domain": sorted(schema.VALID_DOMAINS | schema.DOMAIN_VERTICALS),
        "macro_domains": sorted(schema.VALID_DOMAINS),
        "domain_verticals": sorted(schema.DOMAIN_VERTICALS),
        "domain_aliases": dict(schema.DOMAIN_ALIASES),
        "h3_slots": {kind: list(slots) for kind, slots in schema.VALID_H3_SLOTS.items()},
        "page_predicates": sorted(schema.VALID_PREDICATES),
        "integration_predicates": sorted(schema.INGEST_INTEGRATION_PREDICATES),
        "event_tags": list(schema.INGEST_EVENT_TAGS),
        "controlled_metrics": sorted(schema.CONTROLLED_METRICS),
        "synthesis_headings": list(schema.SYNTHESIS_SKELETON_HEADINGS),
        "max_tags": schema.MAX_TAGS,
        "system_artifact_categories": sorted(schema.SYSTEM_ARTIFACT_CATEGORIES),
        "system_artifact_exempt_fields": sorted(schema.SYSTEM_FILE_EXEMPT_FIELDS),
    }


def render_schema_contract(snapshot: dict | None = None, *, root: Path | None = None) -> str:
    """Render the same current validator reflection for every ingest backend."""
    current = schema_snapshot() if snapshot is None else snapshot
    return render_template(
        "prompts/ingest/runtime_schema.md", root=root,
        schema_json=json.dumps(current, ensure_ascii=False, sort_keys=True),
    )
