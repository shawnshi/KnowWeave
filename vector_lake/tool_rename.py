import re
from pathlib import Path

import yaml

from vector_lake.link_resolution import build_link_map, declared_names_from_nodes, resolve_link_target
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.tool_gc import _in_scope, _snapshot
from vector_lake.wiki_utils import get_wiki_dir, normalize_entity_name, split_frontmatter


def _replace_resolved_links(content: str, old_key: str, new_key: str, old_core: str,
                            link_map: dict, unique_cores: dict) -> str:
    """Edit only unquoted Markdown wikilink target spans, preserving labels/anchors.

    Conservative exclusions are deliberate: quoted examples, code, HTML and YAML are
    not navigation instructions. Ambiguous aliases remain untouched by the resolver.
    """
    protected = [False] * len(content)
    for match in re.finditer(r"<!--.*?(?:-->|\Z)|<[^>\n]+>", content, re.DOTALL):
        protected[match.start():match.end()] = [True] * (match.end() - match.start())
    offset = 0
    fence = None
    frontmatter = content.startswith(("---\n", "---\r\n"))
    for number, line in enumerate(content.splitlines(keepends=True)):
        marker = re.match(r" {0,3}(`{3,}|~{3,})", line)
        skip = frontmatter or fence is not None or marker is not None or bool(re.match(r"(?: {0,3}>| {4}|\t)", line))
        if frontmatter and number > 0 and line.rstrip("\r\n") == "---":
            frontmatter = False
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip():
                fence = None
        if skip:
            protected[offset:offset + len(line)] = [True] * len(line)
        offset += len(line)

    # Inline code and quotations can span line breaks. A word's apostrophe is
    # not an opening quotation; escaped delimiters cannot terminate a span.
    index = 0
    while index < len(content):
        char = content[index]
        if protected[index] or char not in {'`', '"', "'", "“", "‘"}:
            index += 1
            continue
        if char != "`" and index > 0 and content[index - 1].isalnum():
            index += 1
            continue
        end = index + 1
        if char == "`":
            while end < len(content) and content[end] == "`":
                end += 1
        delimiter = content[index:end] if char == "`" else {"“": "”", "‘": "’"}.get(char, char)
        closing = content.find(delimiter, end)
        while closing >= 0:
            preceding = content[:closing]
            if (len(preceding) - len(preceding.rstrip("\\"))) % 2 == 0:
                break
            closing = content.find(delimiter, closing + len(delimiter))
        finish = closing + len(delimiter) if closing >= 0 else len(content)
        protected[index:finish] = [True] * (finish - index)
        index = finish

    pattern = re.compile(r"\[\[([^\[\]\n]+)\]\]")
    replacements = []
    for match in pattern.finditer(content):
        if any(protected[match.start():match.end()]):
            continue
        preceding = content[:match.start()]
        if (len(preceding) - len(preceding.rstrip("\\"))) % 2:
            continue
        target, separator, label = match.group(1).partition("|")
        name, anchor_separator, anchor = target.partition("#")
        if resolve_link_target(name.strip().removesuffix(".md"), link_map, unique_cores) != old_key:
            continue
        suffix = "#" + anchor if anchor_separator else ""
        display = label if separator else old_core
        replacements.append((match.start(), match.end(), f"[[{new_key}{suffix}|{display}]]"))
    for start, end, replacement in reversed(replacements):
        content = content[:start] + replacement + content[end:]
    return content


def rename_vector_lake_entity(old_name: str, new_name: str, dry_run: bool = True, *,
                              domain: str = "", cluster: str = "") -> str:
    """Atomically move one scoped canonical entity; preserve its canonical identity."""
    from vector_lake import governance_store

    if not all(isinstance(value, str) for value in (old_name, new_name, domain, cluster)):
        raise ValueError("names, domain and cluster must be strings")
    if any(char in old_name + new_name for char in ("/", "\\")) or old_name in {".", ".."}:
        return "[Security Error] Entity name is outside the wiki boundary."
    wiki_dir = Path(get_wiki_dir()).resolve(strict=True)
    entities, _, versions, files, state = _snapshot()
    nodes = {str(node.get("page_key") or entity_id): node for entity_id, node in entities.items()}
    # Lint repairs legacy pages that have not entered canonical storage yet. Their
    # native expected version is absence, not an invented canonical content hash.
    for key in files:
        if key not in nodes:
            metadata, _ = split_frontmatter((wiki_dir / f"{key}.md").read_text(encoding="utf-8"))
            if metadata.get("type") and not key.startswith("System_"):
                nodes[key] = metadata
    link_map, _, unique_cores, _ = build_link_map(nodes, declared_names_from_nodes(nodes))
    old_key = resolve_link_target(old_name.removesuffix(".md"), link_map, unique_cores)
    if old_key is None:
        return "Error: Old entity is missing or ambiguous; no rename performed."
    node = nodes[old_key]
    if not _in_scope(node, domain, cluster):
        return "Error: Old entity is outside the requested scope."
    old_path = wiki_dir / f"{old_key}.md"
    if not old_path.resolve().is_relative_to(wiki_dir):
        return "[Security Error] Entity is outside the wiki boundary."
    if old_key not in files or not old_path.exists():
        return "Error: Canonical entity projection is missing."
    new_key = normalize_entity_name(new_name.removesuffix(".md"))
    normalized_new_name = f"{new_key}.md"
    new_path = wiki_dir / normalized_new_name
    if not new_path.resolve().is_relative_to(wiki_dir):
        return "[Security Error] Target entity is outside the wiki boundary."
    resolved_new = resolve_link_target(new_key, link_map, unique_cores)
    if new_path.exists() or new_key in versions or (resolved_new is not None and resolved_new != old_key):
        return f"Error: Target entity '{normalized_new_name}' already exists or names another entity. Use merge instead."
    ids = [entity_id for entity_id, row in entities.items() if row.get("page_key") == old_key]
    if len(ids) > 1:
        return "Error: Canonical target identity is ambiguous."
    if not ids:
        from vector_lake.claim_extractor import _stable_id
        entity_id = node.get("entity_id") or _stable_id("entity", old_key)
        if entity_id in entities:
            return "Error: Legacy canonical identity conflicts with another page."
        ids = [entity_id]

    contents = {}
    for key, row in sorted(nodes.items()):
        if key in files and _in_scope(row, domain, cluster):
            contents[key] = (wiki_dir / f"{key}.md").read_text(encoding="utf-8")
    for key, content in contents.items():
        if key in versions and governance_store.canonical_page_version_from_content(f"{key}.md", content) != versions[key]:
            return "Error: Stale canonical projection; reconcile before rename."

    frontmatter, body = split_frontmatter(contents[old_key])
    if frontmatter.get("entity_id") not in (None, ids[0]):
        return "Error: Canonical identity conflicts with projection."
    # Legacy entity IDs were derived from the old page key. Persist that same ID
    # explicitly so the move cannot mint a new entity or hijack another entity.
    frontmatter["entity_id"] = ids[0]
    old_core = old_key.split("_", 1)[-1]
    new_core = new_key.split("_", 1)[-1]
    if frontmatter.get("title") == old_core:
        frontmatter["title"] = new_core
    aliases = frontmatter.get("aliases") or []
    aliases = [aliases] if isinstance(aliases, str) else list(aliases)
    if old_core not in aliases:
        aliases.append(old_core)
    frontmatter["aliases"] = aliases

    def replace(content):
        return _replace_resolved_links(content, old_key, new_key, old_core, link_map, unique_cores)

    new_content = "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n" + replace(body)
    mutations = [
        {"filename": f"{old_key}.md", "is_delete": True, "expected_version": versions.get(old_key, "")},
        {"filename": normalized_new_name, "content": new_content, "expected_version": ""},
    ]
    for key, content in contents.items():
        if key == old_key:
            continue
        replaced = replace(content)
        if replaced != content:
            mutations.append({"filename": f"{key}.md", "content": replaced, "expected_version": versions.get(key, "")})
    updated_files = len(mutations) - 2
    if dry_run:
        return f"[DRY-RUN] Would rename '{old_key}.md' to '{normalized_new_name}' and update links in {updated_files} file(s)."

    def precondition():
        if _snapshot()[-1] != state:
            raise ValueError("stale canonical or scanned page state; rename aborted")

    try:
        _, receipt = execute_mutation_batch(mutations, validation_mode="schema", canonical_precondition=precondition)
    except (ValueError, OSError) as exc:
        return f"Error during atomic rename: {exc}"
    if "projections deferred" in receipt:
        return f"Rename canonical commit; {receipt}"
    return f"Successfully renamed '{old_key}.md' to '{normalized_new_name}'. Updated links in {updated_files} files. {receipt}"
