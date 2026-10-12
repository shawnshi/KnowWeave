"""Machine-readable strategic-purpose contract for Vector Lake."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from vector_lake.wiki_utils import get_purpose_path, read_ingest_item_content
from vector_lake.yaml_utils import load_yaml
from vector_lake.template_loader import render_template


class PurposeContractError(ValueError):
    """Raised when the strategic-purpose control plane is malformed."""


ALLOWED_SCOPES = {"core", "edge", "other"}


def _as_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PurposeContractError(f"purpose contract field '{field}' must be a non-empty list of strings.")
    return [item.strip() for item in value]


def _parse_frontmatter(content: str) -> dict[str, Any]:
    if not content.startswith("---\n"):
        raise PurposeContractError("purpose.md must start with YAML frontmatter.")
    _, separator, remainder = content.partition("\n---\n")
    if not separator:
        raise PurposeContractError("purpose.md frontmatter is not closed.")
    yaml_text = content[4:content.index("\n---\n")]
    parsed = load_yaml(yaml_text) or {}
    if not isinstance(parsed, dict):
        raise PurposeContractError("purpose.md frontmatter must be a YAML object.")
    return parsed


def _parse_node_frontmatter(content: str, filename: str) -> dict[str, Any]:
    try:
        return _parse_frontmatter(content)
    except PurposeContractError as exc:
        raise PurposeContractError(f"{filename}: {exc}") from exc


def validate_purpose_contract(contract: dict[str, Any]) -> dict[str, Any]:
    version = str(contract.get("purpose_version") or "")
    # Accept the whole 12.x contract family.  Pinning the minor version exactly
    # made the package refuse to write at all against a knowledge base whose
    # contract had moved to a newer 12.x, because the defense hook revalidates
    # the contract on every page write.
    parts = version.split(".")
    if len(parts) != 2 or parts[0] != "12" or not parts[1].isdigit():
        raise PurposeContractError(
            "purpose contract requires purpose_version to be a '12.x' release, "
            f"got {version!r}."
        )
    contract["purpose_version"] = version

    contract["intent_keywords"] = _as_string_list(contract.get("intent_keywords"), "intent_keywords")
    try:
        weight_boost = float(contract.get("intent_weight_boost", 0.0))
    except (TypeError, ValueError) as exc:
        raise PurposeContractError("intent_weight_boost must be numeric.") from exc
    if not 0.0 <= weight_boost <= 1.0:
        raise PurposeContractError("intent_weight_boost must be between 0.0 and 1.0.")
    contract["intent_weight_boost"] = weight_boost

    scope = contract.get("scope")
    if not isinstance(scope, dict):
        raise PurposeContractError("purpose contract requires a scope object.")
    for field in ("core", "edge", "excluded", "marketing_noise"):
        scope[field] = _as_string_list(scope.get(field), f"scope.{field}")

    evidence_tiers = contract.get("evidence_tiers")
    if not isinstance(evidence_tiers, dict) or not evidence_tiers:
        raise PurposeContractError("purpose contract requires evidence_tiers.")
    for tier, definition in evidence_tiers.items():
        if not isinstance(tier, str) or not isinstance(definition, str) or not definition.strip():
            raise PurposeContractError("evidence_tiers must map non-empty names to definitions.")

    sir_registry = contract.get("sir_registry")
    if not isinstance(sir_registry, list) or not sir_registry:
        raise PurposeContractError("purpose contract requires a non-empty sir_registry.")
    sir_ids = set()
    for sir in sir_registry:
        if not isinstance(sir, dict):
            raise PurposeContractError("each SIR entry must be an object.")
        sir_id = str(sir.get("id", "")).strip()
        if not sir_id or sir_id in sir_ids:
            raise PurposeContractError("each SIR requires a unique id.")
        sir_ids.add(sir_id)
        if str(sir.get("status", "")).lower() not in {"active", "deprecated", "draft"}:
            raise PurposeContractError(f"{sir_id}: unsupported status.")
        try:
            date.fromisoformat(str(sir.get("review_after")))
        except (TypeError, ValueError) as exc:
            raise PurposeContractError(f"{sir_id}: review_after must be YYYY-MM-DD.") from exc
        sir["signal_keywords"] = _as_string_list(sir.get("signal_keywords"), f"{sir_id}.signal_keywords")

    policy = contract.get("synthesis_policy")
    if not isinstance(policy, dict):
        raise PurposeContractError("purpose contract requires a synthesis_policy object.")
    try:
        minimum_sources = int(policy.get("min_distinct_sources"))
        minimum_intensity = float(policy.get("min_tension_intensity"))
    except (TypeError, ValueError) as exc:
        raise PurposeContractError("synthesis_policy has invalid thresholds.") from exc
    if minimum_sources < 2 or not 0.0 <= minimum_intensity <= 1.0:
        raise PurposeContractError("synthesis_policy thresholds are out of range.")
    policy["min_distinct_sources"] = minimum_sources
    policy["min_tension_intensity"] = minimum_intensity
    return contract


def load_purpose_contract(path: str | Path | None = None) -> dict[str, Any]:
    purpose_path = Path(path) if path else get_purpose_path()
    try:
        content = purpose_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PurposeContractError(f"Cannot read purpose contract: {purpose_path}") from exc
    return validate_purpose_contract(_parse_frontmatter(content))


def purpose_vectors(contract: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = contract or load_purpose_contract()
    return {
        "keywords": list(contract["intent_keywords"]),
        "weight_boost": contract["intent_weight_boost"],
    }


def render_strategy_directive(contract: dict[str, Any] | None = None) -> str:
    # Render exactly the validated policy; do not mutate a caller-owned snapshot.
    contract = validate_purpose_contract(deepcopy(contract)) if contract is not None else load_purpose_contract()
    scope = contract["scope"]
    tier_names = ", ".join(contract["evidence_tiers"].keys())
    sir_lines = [
        render_template(
            "prompts/strategy_sir.md", id=sir["id"], status=sir["status"],
            keywords=", ".join(sir["signal_keywords"]), review_after=sir["review_after"],
        )
        for sir in contract["sir_registry"]
        if str(sir["status"]).lower() == "active"
    ]
    tier_definitions = "\n".join(
        render_template("prompts/strategy_tier.md", tier=name, definition=definition)
        for name, definition in contract["evidence_tiers"].items()
    )
    policy = contract["synthesis_policy"]
    return render_template(
        "prompts/strategy.md", version=contract["purpose_version"],
        tier_definitions=tier_definitions, min_sources=policy["min_distinct_sources"],
        min_intensity=policy["min_tension_intensity"], core=", ".join(scope["core"]), edge=", ".join(scope["edge"]),
        excluded=", ".join(scope["excluded"]), marketing=", ".join(scope["marketing_noise"]),
        tiers=tier_names, sirs="\n" + "\n".join(sir_lines) if sir_lines else "",
    )


def _normalise_sources(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def validate_ingest_payload(items: list[dict[str, Any]], contract: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    contract = contract or load_purpose_contract()
    permitted_tiers = set(contract["evidence_tiers"])
    records = []
    for item in items:
        if not isinstance(item, dict) or "filename" not in item:
            raise PurposeContractError("Each ingest item requires filename.")
        if "filepath" in item and not item.get("content"):
            # Single owner for the read, and the reason it is not a bare ``open``: the path
            # comes from the ingest packet, so it must resolve inside the raw root before a
            # payload can turn it into a published page.
            item["content"] = read_ingest_item_content(item, required=True)
        if "content" not in item:
            raise PurposeContractError("Each ingest item requires content or filepath.")
        filename = Path(str(item["filename"])).name
        content = str(item["content"])
        frontmatter = _parse_node_frontmatter(content, filename)
        strategic_scope = str(frontmatter.get("strategic_scope", "")).strip().lower()
        if strategic_scope not in ALLOWED_SCOPES:
            raise PurposeContractError(f"{filename}: strategic_scope must be one of {sorted(ALLOWED_SCOPES)}.")
        # ``render_strategy_directive`` scopes this requirement to *new* nodes
        # ("Every new node must declare ...").  Full validation also runs when an
        # existing page is re-saved, and 6311 of the 7178 live pages predate the
        # evidence_tier vocabulary. Administrative memory/recovery also must not
        # fabricate a business grade, so absence is not a purpose contradiction.  A *present* value
        # outside the contract vocabulary is still a hard error -- that is a real
        # conflict with purpose.md, and ``validation_mode="schema"`` remains the
        # escape hatch for bounded legacy maintenance.
        evidence_tier = str(frontmatter.get("evidence_tier", "")).strip()
        if "evidence_tier" in frontmatter and evidence_tier not in permitted_tiers:
            raise PurposeContractError(f"{filename}: evidence_tier must be one of {sorted(permitted_tiers)}.")
            
        aliases = frontmatter.get("aliases")
        if aliases is not None and not isinstance(aliases, list):
            raise PurposeContractError(f"{filename}: aliases must be a list.")
            
        # ``categories`` used to be checked here as well, and that second copy is gone: the rule
        # has one owner now -- ``schema_validator.category_shape_violation``, enforced on every
        # write through ``verify_asset``.  Two copies agreed only by luck, and between them they
        # still missed the case both were supposed to cover: an update in ``schema`` mode, where
        # this gate does not run and the new-node rules do not apply.

        records.append({
            "filename": filename,
            "sources": _normalise_sources(frontmatter.get("sources")),
            "topic_cluster": str(frontmatter.get("topic_cluster") or "General").strip(),
            "tension_edges": frontmatter.get("tension_edges", []),
        })
    return records


def build_synthesis_proposals(records: list[dict[str, Any]], contract: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    contract = contract or load_purpose_contract()
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"sources": set(), "pages": set(), "intensities": []})
    for record in records:
        sources = set(_normalise_sources(record.get("sources")))
        if not sources:
            continue
        for edge in record.get("tension_edges", []):
            if not isinstance(edge, dict) or not str(edge.get("target", "")).strip():
                continue
            target = str(edge["target"]).strip()
            bucket = buckets[target]
            bucket["sources"].update(sources)
            bucket["pages"].add(str(record.get("filename", "")))
            try:
                bucket["intensities"].append(float(edge.get("intensity", 0.0)))
            except (TypeError, ValueError):
                continue

    policy = contract["synthesis_policy"]
    proposals = []
    for target, bucket in buckets.items():
        if len(bucket["sources"]) < policy["min_distinct_sources"]:
            continue
        if not bucket["intensities"] or max(bucket["intensities"]) < policy["min_tension_intensity"]:
            continue
        proposals.append({
            "type": "Synthesis-Proposal",
            "title": f"Synthesis-Proposal: {target}",
            "description": (
                f"{target} has {len(bucket['sources'])} independent sources and a maximum "
                f"tension intensity of {max(bucket['intensities']):.2f}. Generate a bounded synthesis; do not overwrite Compiled Truth."
            ),
            "sources": sorted(bucket["sources"]),
            "affected_pages": sorted(page for page in bucket["pages"] if page),
            "search_queries": [f"{target} contradiction evidence", f"{target} implementation boundary"],
        })
    return proposals


def review_sir_lifecycle(as_of: str | date | None = None, contract: dict[str, Any] | None = None) -> list[dict[str, str]]:
    contract = contract or load_purpose_contract()
    if as_of is None:
        review_date = date.today()
    elif isinstance(as_of, date):
        review_date = as_of
    else:
        review_date = date.fromisoformat(as_of)

    proposals = []
    for sir in contract["sir_registry"]:
        due_date = date.fromisoformat(str(sir["review_after"]))
        if str(sir["status"]).lower() == "active" and due_date <= review_date:
            proposals.append({
                "type": "SIR-Review-Proposal",
                "sir_id": str(sir["id"]),
                "review_after": due_date.isoformat(),
                "reason": "The fixed review window elapsed. A Watchdog must assess evidence before deprecation or replacement.",
            })
    return proposals
