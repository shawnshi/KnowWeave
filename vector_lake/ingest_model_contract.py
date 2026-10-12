"""Shared semantic result gate for host-side ingest model adapters."""
from __future__ import annotations

import hashlib
import json

from vector_lake.template_loader import render_template
from vector_lake.path_safety import is_link_or_junction


def extract_result(text: str):
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        return None, f"child JSON invalid: {exc}"
    if not isinstance(payload, dict) or set(payload) != {"files_written", "integration"}:
        return None, "child must return files_written and integration only"
    files = payload["files_written"]
    integration = payload["integration"]
    if not isinstance(files, list) or not all(
        isinstance(item, dict) and set(item) == {"filename", "content"}
        and isinstance(item["filename"], str) and isinstance(item["content"], str)
        for item in files
    ):
        return None, "child files_written must contain filename/content objects only"
    if not isinstance(integration, dict) or not str(integration.get("disposition") or "").strip():
        return None, "child requires an explicit integration disposition"
    disposition = str(integration["disposition"]).strip().lower()
    if disposition not in {"integrated", "standalone"}:
        return None, "child disposition must be integrated or standalone; rejected output is not completion"
    if not files:
        return None, "child returned no files for a successful disposition"
    return payload, ""


def read_evidence_tiers_only() -> dict[str, str]:
    """Read only bounded canonical YAML frontmatter; never read purpose prose."""
    from vector_lake.wiki_utils import get_purpose_path
    from vector_lake.yaml_utils import load_yaml

    path = get_purpose_path()
    if is_link_or_junction(path):
        raise ValueError("Purpose policy cannot be a link")
    limit = 16 * 1024
    # Unbuffered reads stop exactly at the closing YAML delimiter, not in the body.
    with path.open("rb", buffering=0) as handle:
        first = handle.readline(limit + 1)
        if first.rstrip(b"\r\n") != b"---":
            raise ValueError("Purpose policy requires YAML frontmatter")
        used = len(first)
        lines = []
        while used <= limit:
            line = handle.readline(limit - used + 1)
            used += len(line)
            if used > limit or not line:
                raise ValueError("Purpose frontmatter missing delimiter or exceeds budget")
            if line.rstrip(b"\r\n") == b"---":
                break
            lines.append(line)
        else:
            raise ValueError("Purpose frontmatter exceeds budget")
    header = load_yaml(b"".join(lines).decode("utf-8"))
    tiers = header.get("evidence_tiers") if isinstance(header, dict) else None
    if (not isinstance(tiers, dict) or not tiers or len(tiers) > 32
            or not all(isinstance(k, str) and 0 < len(k.strip()) <= 128
                       and isinstance(v, str) and 0 < len(v.strip()) <= 2000
                       for k, v in tiers.items())):
        raise ValueError("Purpose evidence_tiers must map bounded names to definitions")
    return dict(tiers)


def build_cli_prompt(packet: dict) -> str:
    """Hydrate only the dispatched raw source; CLI workers need no filesystem tools."""
    from vector_lake.wiki_utils import validate_raw_ingest_source_path

    if not isinstance(packet, dict) or not isinstance(packet.get("metadata"), dict):
        raise ValueError("task packet requires metadata")
    metadata = packet["metadata"]
    processed = metadata.get("processed_data")
    if not isinstance(processed, dict) or not isinstance(processed.get("integration_candidates"), list):
        raise ValueError("task packet requires an explicit integration_candidates manifest")
    controlled = processed.get("controlled_recompile") is not None or processed.get("source_read_path") is not None
    expected = str(processed.get("hash") or "")
    if controlled:
        from vector_lake.controlled_recompile import _snapshot_path
        marker = processed.get("controlled_recompile")
        if not isinstance(marker, dict) or not isinstance(processed.get("source_read_path"), str):
            raise ValueError("Controlled prompt requires its complete snapshot binding")
        source_path = _snapshot_path(processed["source_read_path"])
        if source_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Controlled prompt snapshot exceeds the byte budget")
        raw = source_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != marker.get("sha256"):
            raise ValueError("Controlled prompt bytes differ from the approved SHA-256")
    else:
        filepath = str(processed.get("filepath") or "")
        source_path = validate_raw_ingest_source_path(filepath)
        if source_path is None or not expected:
            raise ValueError("source escaped the raw root or omitted its dispatch fingerprint")
        raw = source_path.read_bytes()
    # Bind actual inline bytes, not a prior read/hash of the same path.
    if not expected or hashlib.md5(raw).hexdigest() != expected:
        raise ValueError("source changed before model dispatch")
    source = raw.decode("utf-8")
    prompt = packet.get("prompt")
    contract = metadata.get("output_contract")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(contract, str) or not contract.strip():
        raise ValueError("task packet requires its prompt and output_contract")
    excluded = {"instructions"}
    if controlled:
        excluded.update({"lease_owner", "lease_token", "lease_generation"})
    snapshot = {key: value for key, value in processed.items() if key not in excluded}
    context = {"dispatch_snapshot": snapshot, "raw_source": source}
    if controlled:
        context["source_read_boundary"] = "Inline bytes are the approved source snapshot. filepath is original provenance only; do not open it or any source file."
        tiers = read_evidence_tiers_only()
        context["evidence_tier_policy"] = {
            "evidence_tiers": tiers,
            "sha256": hashlib.sha256(json.dumps(tiers, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            "instruction": "These are the current canonical evidence-tier names and definitions. Assign an evidence tier only when justified by the supplied source; otherwise omit evidence_tier and explicitly describe the limitation. All eligible sources must produce a Source page, including unrelated, secondary, speculative, or empty sources. Classify outside-focus material as strategic_scope: other. Do not promote a secondary source or invent a tier. Private purpose prose and other policy fields are not supplied or authorized to read.",
        }
    repair = metadata.get("repair")
    if repair is not None:
        if not isinstance(repair, dict):
            raise ValueError("task packet repair must be an object")
        context["repair"] = repair
    return render_template(
        "prompts/ingest/cli_worker.md", prompt=prompt, contract=contract,
        context_json=json.dumps(context, ensure_ascii=False),
    )


def result_schema() -> dict:
    """Codex's strict output schema; finalizer remains the semantic authority."""
    relation = {key: {"type": "string"} for key in (
        "target", "target_hash", "target_projection_hash", "predicate", "evidence", "event_date", "event_tag"
    )}
    relation["confidence"] = {"type": "number"}
    integration = {"disposition": {"type": "string", "enum": ["integrated", "standalone"]},
                   "reason": {"type": "string"},
                   "relations": {"type": "array", "items": {"type": "object", "properties": relation,
                                 "required": list(relation), "additionalProperties": False}}}
    return {"type": "object", "additionalProperties": False, "required": ["files_written", "integration"],
            "properties": {
                "files_written": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                  "required": ["filename", "content"],
                                  "properties": {"filename": {"type": "string"}, "content": {"type": "string"}}}},
                "integration": {"type": "object", "additionalProperties": False,
                                "required": list(integration), "properties": integration},
            }}
