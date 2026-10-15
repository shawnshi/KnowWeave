"""The ingest output contract, published with every task packet.

Why this module exists
----------------------
``schema_validator`` and ``tool_ingest`` enforce the ingest contract field by field, but the
rendered ingest prompt is ~50 KB: it carries the whole schema, the category taxonomy, the
candidate index and the purpose contract.  Measured 2026-09-28, the integration-relation rules
sat at ~79% of that prompt and the model broke them anyway, six times in a row --
``has_part`` -> ``evolved-from`` -> a missing ``.md`` on the target -> an undated timeline bullet
-> ``event_tag: paper-analysis`` -> a non-numeric ``confidence``.  Every fix was correct and every
one moved the failure to the next unchecked field, because the model was being asked to obey
rules buried in a long document instead of beside the object it had to produce.

So the contract now travels with the packet, and the model seam appends it to the *end* of the
brief -- the last thing the child reads before answering.  The vocabularies are imported, never
retyped, so this text cannot drift from the validators that enforce it.
"""

from __future__ import annotations

from vector_lake.ingest_schema_contract import schema_snapshot
from vector_lake.template_loader import render_template


def build_output_contract() -> str:
    """The machine-checked shape of one ingest answer, in one screenful."""
    runtime = schema_snapshot()
    predicates = ", ".join(runtime["integration_predicates"])
    tags = " | ".join(runtime["event_tags"])
    return render_template("prompts/ingest/output_contract.md", predicates=predicates, tags=tags)
