from vector_lake import governance_store
from vector_lake.template_loader import render_template


def _truncate_text(text: str, limit: int = 120) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _summarize_values(values, limit: int = 5) -> str:
    cleaned = []
    seen = set()
    for value in (values or []):
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    preview = ", ".join(cleaned[:limit])
    return f"{preview} (+{len(cleaned) - limit} more)"


def _combined_pending_items() -> list[dict]:
    combined = []
    for item in governance_store.pending_governance_items():
        enriched = dict(item)
        enriched["queue_kind"] = "governance"
        combined.append(enriched)
    combined.sort(key=lambda item: item.get("created_at") or item.get("created") or "")
    return combined


def _format_combined_report(items: list[dict]) -> str:
    if not items:
        return "[OK] No pending review items."

    type_icons = {
        "contradiction": "[!]",
        "duplicate": "[D]",
        "missing-page": "[?]",
        "suggestion": "[*]",
        "merge": "[M]",
        "publish-candidate": "[P]",
        "community_naming": "[N]",
        # The unsupported-claim cohorts arrive under this type; without an icon they read as the
        # generic ``[*]`` and disappear among the suggestions they are not.
        "evidence-gap": "[E]",
    }
    lines = [
        f"[REVIEW] {len(items)} Pending Governance Items",
        "",
    ]
    for index, item in enumerate(items):
        icon = type_icons.get(item.get("type"), "[*]")
        lines.append(f"  [{index}] {icon} **{item.get('title', 'Untitled')}** ({item.get('type', 'unknown')})")
        lines.append(f"      ID: {item.get('item_id', 'unknown')}")
        lines.append(f"      Source: {item.get('source', 'unknown')}")
        if item.get("description"):
            lines.append(f"      {_truncate_text(item['description'])}")
        if item.get("search_queries"):
            lines.append(f"      Research queries: {_summarize_values(item['search_queries'], limit=3)}")
        if item.get("affected_pages"):
            lines.append(f"      Pages: {_summarize_values(item['affected_pages'], limit=5)}")
        lines.append("")
    lines.append("Actions: `python cli.py review resolve <index|item_id> [--resolution skip|create|merge|acknowledge]`")
    return "\n".join(lines)


def _resolve_combined_item(identifier, resolution: str = "skip", change_manifest: dict | None = None):
    pending = _combined_pending_items()
    target = None
    if isinstance(identifier, str) and not identifier.isdigit():
        target = next((item for item in pending if item.get("item_id") == identifier), None)
    else:
        index = int(identifier)
        if 0 <= index < len(pending):
            target = pending[index]
    if not target:
        return None

    from vector_lake import governance_service
    return governance_service.resolve_governance_item(target["item_id"], resolution, change_manifest=change_manifest)


def review_vector_lake(action: str = "list", index="-1", resolution: str = "skip", change_manifest: dict | None = None):
    if action == "list":
        return _format_combined_report(_combined_pending_items())
    if action == "ground":
        if index in (-1, "-1", None, ""):
            return "Error: specify review item index or item_id. Usage: cli.py review ground <index|item_id>"
        pending = _combined_pending_items()
        target = None
        if isinstance(index, str) and not index.isdigit():
            target = next((item for item in pending if item.get("item_id") == index), None)
        else:
            idx = int(index)
            if 0 <= idx < len(pending):
                target = pending[idx]
        if not target:
            return f"Failed to resolve item '{index}'."
        if target.get("type") != "missing-page":
            return f"Item {target.get('item_id')} is not a missing-page item. Cannot ground."

        queries = " | ".join(target.get("search_queries", []))
        result = render_template("prompts/review/ground.md", item_id=target.get("item_id"), queries=queries)
        return result
    if action == "name_community":
        if index in (-1, "-1", None, ""):
            return "Error: specify review item index or item_id. Usage: cli.py review name_community <index|item_id>"
        pending = _combined_pending_items()
        target = next((item for item in pending if item.get("item_id") == index), None) if not str(index).isdigit() else pending[int(index)] if 0 <= int(index) < len(pending) else None
        if not target or target.get("type") != "community_naming":
            return f"Item '{index}' is not a valid community_naming item."
            
        hubs = target.get("hubs", [])
        page = target.get("affected_pages", [""])[0]
        result = render_template("prompts/review/name_community.md", item_id=target.get("item_id"), hubs=", ".join(hubs), page=page)
        return result
    if action == "resolve":
        if index in (-1, "-1", None, ""):
            return "Error: specify review item index or item_id. Usage: cli.py review resolve <index|item_id>"
        item = _resolve_combined_item(index, resolution, change_manifest=change_manifest)
        if not item:
            return f"Failed to resolve item '{index}'."

        result = f"Resolved item {item.get('item_id')} ('{item.get('title')}') → {resolution}."
        if resolution == "create" and item.get("search_queries"):
            queries = " | ".join(item["search_queries"])
            result += render_template("prompts/review/research.md", queries=queries)
        return result
    return f"Unknown review action: {action}. Use 'list', 'resolve', or 'ground'."

