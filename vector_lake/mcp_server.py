from vector_lake.runtime_environment import configure_numeric_threads

configure_numeric_threads()

import inspect
import os
import sys
import logging
from typing import Any

# A stdio MCP server must not do startup network I/O: fastmcp's default version
# check calls pypi.org on every host start.  This has to be set before fastmcp is
# imported (its settings are read at import time) and stays overridable from the
# environment.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

from fastmcp import FastMCP

SERVER_API = "FastMCP"

# Global lock against stdout pollution
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', force=True)
from vector_lake import tools, tool_memory
from vector_lake.tool_timeline import search_timeline_events

mcp = FastMCP("vector-lake")


def registered_tool_names(server=None) -> list[str]:
    """Names of the tools registered on ``server`` (defaults to this module's).

    fastmcp exposes ``list_tools()`` only as a coroutine, while the doctor and the
    test suite need the surface from synchronous code.  Prefer the public API,
    discarding the coroutine when an event loop is already running; fall back to
    the local provider's component registry so a health check cannot crash here.
    """
    server = server if server is not None else mcp
    lister = getattr(server, "list_tools", None)
    if callable(lister):
        # Framework implementations may return a sync iterable or an awaitable.
        # Runtime materialization below still validates the dynamic result.
        result: Any
        try:
            result = lister()
        except Exception:
            result = None
        if inspect.isawaitable(result):
            import asyncio

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pending = result

                async def collect_listing() -> Any:
                    return await pending

                try:
                    result = asyncio.run(collect_listing())
                except Exception:
                    result = None
            else:
                # A running loop owns this coroutine; close it instead of blocking.
                closer = getattr(result, "close", None)
                if callable(closer):
                    closer()
                result = None
        try:
            listed = list(result) if result is not None else []
        except TypeError:
            listed = []
        if listed:
            return [str(getattr(tool, "name", tool)) for tool in listed]
    provider = getattr(server, "local_provider", None)
    components = getattr(provider, "_components", None) if provider is not None else None
    if isinstance(components, dict):
        registered = [
            str(key).split(":", 1)[1].rsplit("@", 1)[0]
            for key in components
            if str(key).startswith("tool:")
        ]
        if registered:
            return registered
    return []


@mcp.tool()
def search_timeline(entity_name: str = "", action: str = "", limit: int = 10) -> str:
    """Search material mentions in timeline events, not a strict organizational chronology.

    A match may mention an entity without describing an event of that entity. Source claims
    and dates are not independently verified here. Entries are ordered by event date, newest first.
    A claim that states no date sorts last
    and reads ``Unknown Date``; it is never given its ingestion timestamp as an event date.

    Args:
        entity_name: Mention search over subject IDs/titles and descriptions (e.g., '卫宁健康'),
            not a strict subject filter. Canonical fallback uses the same mention predicate.
            Leave empty to search all.
        action: Filter by the ledger's event tag (e.g., 'Release', 'Observation'), read from
            the claim's structured field or from its ``[date] [Tag]`` prefix. Leave empty for all.
        limit: Number of events to return (default 10).
    """
    clamped_limit = max(1, min(int(limit), 100))
    return search_timeline_events(
        entity_name=entity_name if entity_name else None,
        action=action if action else None,
        limit=clamped_limit
    )

# [CLI-only / Internal]
def backup_retention_report(keep: int = 0, max_bytes: int = 0, dry_run: bool = True) -> str:
    """Report the .meta/backups footprint against its retention bound.

    Backup files are written by the repair, projection and ingest paths and nothing
    used to remove them, so the tree grows without bound (a full copy per backup of
    a multi-GB database).  This reports what lies outside the bound; ``dry_run=False``
    removes exactly those entries.

    Args:
        keep: Newest copies to retain. 0 uses the configured default (3).
        max_bytes: Byte budget for the older copies. 0 uses the configured default (12 GiB).
        dry_run: When True (the default) nothing is removed. The newest copy is
            always retained, and anything unrecognised is never pruned.
    """
    return tools.backup_retention_report(keep=keep, max_bytes=max_bytes, dry_run=dry_run)

# [CLI-only / Internal]
def idempotency_index_status() -> str:
    """Report the uniqueness guarantee each idempotency table actually has.

    ``full`` means an idempotency key can never repeat.  ``active`` means the table
    already held duplicate history written by an earlier release, so uniqueness is
    enforced over non-terminal rows only -- the population a concurrent enqueue can
    collide in.  ``absent`` means only the write lock protects enqueue.
    """
    return tools.idempotency_index_report()

# [CLI-only / Internal]
def repair_idempotency_keys(table: str = "mutation_outbox", dry_run: bool = True) -> str:
    """[MAINTENANCE / REPAIR] Clear redundant idempotency keys so the full unique index can be created. Prefer CLI 'cli.py repair-idempotency'.

    Use this after ``idempotency_index_status`` reports ``active`` or ``absent``.
    Only the duplicate *key* is cleared: the row, its status, timestamps, error text
    and superseded_by link are all kept, so no audit history is lost.  Each key stays
    on its canonical (lowest id) row -- the one enqueue already returns for it.

    Args:
        table: 'mutation_outbox' or 'jobs'.
        dry_run: When True (the default) no row is changed.
    """
    return tools.repair_idempotency_keys(table=table, dry_run=dry_run)

@mcp.tool()
def inspect_projections() -> str:
    """Report health, authorities, and degradation status of all derived projections.

    Covers memory_gram, vectors, page_projection, fts_index, tantivy_mirror,
    claim_index, timeline_events, and governance_queue in one unified call.
    """
    from vector_lake import projection_registry

    lines = []
    for name, entry in projection_registry.status().items():
        kind = "auto" if entry["repairable"] else "manual"
        line = f"[{entry['state'].upper():8s}] {name:18s} [{kind:6s}] {entry['detail']} (authority: {entry['authority']})"
        if entry["state"] != "healthy" and entry.get("degrades"):
            line += f" -> degrades: {entry['degrades']}"
        lines.append(line)
    return "\n".join(lines)

@mcp.tool()
def get_query_evidence(query: str, node_keys: list[str], domain: str, cluster: str = '') -> str:
    """Optionally read exact passages from 1-8 already located Source nodes.

    Strict existing canonical Source-version/Raw-byte binding is required; legacy
    or ambiguous binding returns explicit refusal, NOT source absence. This is
    not a replacement for the existing authorized original-reading workflow and
    never repairs metadata or silently labels weaker evidence verified.
    Pilot domains: Artificial_Intelligence and Medical_IT. A verified result
    covers exact selected bytes/binding only, not truth, authorship/date, full
    document/images, publication or authorization. Trusted host configuration
    supplies root, dedicated read-only snapshot and a cooperative 15s budget;
    those resources and embedding/index/write controls are not tool inputs.
    """
    return tools.get_query_evidence(query, node_keys, domain=domain, cluster=cluster or None)


@mcp.tool()
def search_vector_lake(
    query: str,
    top_k: int = 5,
    mode: str = "page",
    domain: str = "",
    cluster: str = "",
    include_history: bool = False,
    as_xml: bool = False,
    allow_external_embedding: bool = False,
    profile: str = 'auto',
    use_chunks: bool = False,
) -> str:
    """Search the Vector Lake index.
    
    Args:
        query: The semantic query string.
        top_k: Number of results to return (default 5).
        mode: Search mode: 'page' (lexical FTS5 + graph PPR + local rerank by default; the vector
            arm joins only when allow_external_embedding=True and the embedding provider is
            configured), 'memory' (operational memory), or 'claim' (facts).
        domain: Exact subject filter, e.g. 'Medical_IT', 'Policy_and_Governance', 'General'.
            General is a literal category, not all domains. 'HIT' and 'Clinical' are not aliases.
            Keep retrieval within task-authorized domains; use separate queries for multiple domains.
        cluster: Optional filter by topic cluster.
        include_history: If True, includes historical/decayed nodes or expired memories.
        as_xml: If True, returns structured XML evidence nodes instead of Markdown.
        allow_external_embedding: Explicit permission to send this query to the embedding provider.
            Default False keeps the query local. Domain scope is required; it is a selection
            constraint, not an authenticated access-control boundary.
        profile: Ranking policy: auto, legacy, balanced, entity, source, author, analysis.
            Auto uses local intent rules; preferences do not establish evidence quality.
        use_chunks: Opt into an already-built scoped chunk sidecar; never builds it.
    """
    if not domain.strip():
        raise ValueError('Explicit domain is required; General is not all domains')
    return tools.search_vector_lake(
        query,
        top_k,
        as_xml=as_xml,
        domain=domain.strip(),
        cluster=cluster,
        include_history=include_history,
        mode=mode,
        allow_external_embedding=allow_external_embedding,
        profile=profile, use_chunks=use_chunks,
    )

def _payload_path_allowed(abs_path) -> bool:
    """Sandbox predicate for agent payload files.

    Roots are additive: ``VECTOR_LAKE_PAYLOAD_ROOT`` *extends* the built-in
    sandboxes instead of replacing them, so an operator override can no longer
    silently revoke the repository-local ``brain/`` sandbox.

    The built-in roots still require the ``<root>/<project>/scratch/...`` shape
    because they are shared with every other host process.  An explicitly
    configured root is trusted as-is: the operator named that exact directory,
    and ``host_env.payload_root_from_env()`` already rejects a filesystem anchor.
    """
    from vector_lake import host_env

    configured_root = host_env.payload_root_from_env()
    if configured_root is not None and abs_path.is_relative_to(configured_root):
        return True
    for root in host_env.payload_sandbox_roots():
        if not abs_path.is_relative_to(root):
            continue
        relative_parts = abs_path.relative_to(root).parts
        if len(relative_parts) >= 3 and relative_parts[1].lower() == "scratch":
            return True
    return False


def _read_payload(payload_file: str) -> str:
    if not payload_file:
        return ""
    from pathlib import Path

    abs_path = Path(payload_file).resolve()
    if not _payload_path_allowed(abs_path):
        raise ValueError(f"[Security Error] Payload file must be within an approved agent sandbox: {payload_file}")
    if not abs_path.exists() or not abs_path.is_file():
        raise ValueError(f"[Sandbox Error] Payload file not found: {payload_file}. Create it inside an approved sandbox first.")
    max_bytes = max(1, int(os.environ.get("VECTOR_LAKE_PAYLOAD_MAX_BYTES", str(5 * 1024 * 1024))))
    if abs_path.stat().st_size > max_bytes:
        raise ValueError(f"[Sandbox Error] Payload file exceeds {max_bytes} bytes: {payload_file}")
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

@mcp.tool()
def update_operational_memory(memory_type: str, payload_file: str = "", content: str = "") -> str:
    """Safely persist an operational memory (preference, decision, fact, task_state) without corrupting the graph.
    
    Args:
        memory_type: Type of memory ('preference', 'decision', 'fact', 'task_state').
        payload_file: Absolute path to a temporary file containing the text content of the memory (optional if content is provided).
        content: Direct text content of the memory to persist (optional if payload_file is provided).
    """
    if not content and not payload_file:
        return "Error: Either 'content' or 'payload_file' must be provided."
    if content:
        text_content = content
    else:
        try:
            text_content = _read_payload(payload_file)
        except Exception as e:
            return str(e)
    return tool_memory.update_operational_memory(memory_type, text_content)

@mcp.tool()
def lint_vector_lake(auto_fix: bool = False) -> str:
    """Run self-healing audit on the Wiki nodes.
    
    Args:
        auto_fix: Automatically fix issues such as decaying notes.
    """
    try:
        return tools.lint_vector_lake(auto_fix=auto_fix)
    except Exception as e:
        import traceback
        logging.error(f"MCP Tool Exception (lint_vector_lake): {e}\n{traceback.format_exc()}")
        return f"MCP Exception: {type(e).__name__}: {str(e)}"

@mcp.tool()
def preview_query_context(
    query_str: str, domain: str = '', cluster: str = '', allow_external_embedding: bool = False,
    profile: str = 'auto', use_chunks: bool = False,
) -> str:
    """Return an inline reasoning context without a proposal payload or Wiki write.

    Retrieval may record an audit-ledger entry. For page proposals, use
    ``query_logic_lake`` and finalize only after independent validation.

    Args:
        query_str: The topic or command for reasoning.
        domain: Required exact subject domain; General is not all domains.
        cluster: Optional exact topic cluster.
        allow_external_embedding: Explicit permission to send the query to the embedding provider.
            Default False uses only local retrieval. This flag does not establish authorization.
    """
    if not domain.strip():
        raise ValueError('Explicit domain is required for context retrieval')
    return tools.preview_query_context(
        query_str, domain=domain, cluster=cluster or None,
        allow_external_embedding=allow_external_embedding,
        profile=profile, use_chunks=use_chunks,
    )


@mcp.tool()
def query_logic_lake(
    query_str: str, dry_run: bool = False, domain: str = '', cluster: str = '',
    allow_external_embedding: bool = False,
    profile: str = 'auto', use_chunks: bool = False,
) -> str:
    """Prepare budget-controlled context for a separate reasoning/synthesis step.

    Both modes write a local envelope under the extension root's ``tmp`` and may prune
    old query envelopes. This tool does not invoke a reasoning model or publish Wiki
    pages. Use ``preview_query_context`` to avoid a proposal-envelope write; retrieval
    can still record its normal audit-ledger event.

    Args:
        query_str: The topic or command for reasoning.
        domain: Required exact subject domain; General is not all domains.
        cluster: Optional exact topic cluster.
        allow_external_embedding: Explicit permission to send the query to the embedding provider.
            Default False uses only local retrieval. This flag does not establish authorization.
        dry_run: Return the provenance trace instead of the synthesis prompt, after
            the local context envelope has been written. This is not a read-only mode.
    """
    if not domain.strip():
        raise ValueError('Explicit domain is required for context retrieval')
    return tools.prepare_query_context(
        query_str, dry_run, domain=domain, cluster=cluster or None,
        allow_external_embedding=allow_external_embedding,
        profile=profile, use_chunks=use_chunks,
    )

@mcp.tool()
def finalize_query_synthesis(files_written_str: str, query_str: str) -> str:
    """Finalize the logic lake query by verifying proposed pages against schema gates and creating stubs for broken links.

    Durable ingestion and embedding of the accepted pages are handled asynchronously by the background watcher.
    
    Args:
        files_written_str: Comma-separated list of filenames (e.g. 'Synthesis_Topic.md') that were written by the subagent.
        query_str: The original query string for the trace.
    """
    return tools.finalize_query_synthesis(files_written_str, query_str)

@mcp.tool()
def review_governance_list() -> str:
    """List pending items in the governance review queue (contradictions, gaps, merges)."""
    return tools.review_vector_lake(action="list")

@mcp.tool()
def resolve_governance_item(
    item_id: str,
    resolution: str,
    payload_file: str = "",
    manifest_json: str = "",
) -> str:
    """Resolve a governance item.

    Args:
        item_id: The ID or index of the item.
        resolution: Resolution action: 'skip', 'create', 'merge', 'acknowledge'.
        payload_file: Optional absolute path to a temporary JSON file containing the expected outcome manifest (e.g. {"allow_cycles": false}).
        manifest_json: Optional direct JSON string of the manifest, avoiding scratch file creation.
    """
    import json
    manifest = None
    if manifest_json:
        try:
            manifest = json.loads(manifest_json)
        except json.JSONDecodeError as e:
            return f"[JSON Error] Failed to parse manifest_json: {e}. Please fix the JSON and retry."
    elif payload_file:
        try:
            manifest_str = _read_payload(payload_file)
            if manifest_str.strip():
                manifest = json.loads(manifest_str)
        except json.JSONDecodeError as e:
            return f"[Sandbox JSON Error] Failed to parse payload file {payload_file}: {e}. Please fix the JSON and retry."
        except Exception as e:
            return str(e)
    return tools.review_vector_lake(action="resolve", index=item_id, resolution=resolution, change_manifest=manifest)

@mcp.tool()
def get_governance_debt(top: int = 20) -> str:
    """Show governance debt metrics.
    
    Args:
        top: Number of top items to show.
    """
    return tools.debt_vector_lake(top=top)

@mcp.tool()
def trigger_audit_graph() -> str:
    """Synthesize graph topology insights into the unified review surface."""
    return tools.audit_graph()

@mcp.tool()
def runtime_identity() -> str:
    """Observe this server process: selected loaded code, current source, checkout and core origin.

    Read-only: no DB access, module reload, service restart or production-acceptance approval.
    """
    import json
    from vector_lake.runtime_identity import runtime_identity as observe
    return json.dumps(observe(), ensure_ascii=False)


@mcp.tool()
def doctor_vector_lake() -> str:
    """Validate runtime dependencies and filesystem layout health."""
    return tools.doctor_vector_lake()

@mcp.tool()
def rename_entity(old_name: str, new_name: str, dry_run: bool = True, *,
                  domain: str = "", cluster: str = "") -> str:
    """Rename a Wiki entity (filename/frontmatter) and automatically update all referring markdown links.
    
    Args:
        old_name: Current name of the entity (e.g. 'Concept_Old-Name.md').
        new_name: New name for the entity (e.g. 'Concept_New-Name.md').
        dry_run: Preview changes without writing to disk.
        domain: Exact domain scope; empty retains the existing rename scope.
        cluster: Exact cluster scope within the domain.
    """
    from vector_lake.tool_rename import rename_vector_lake_entity
    return rename_vector_lake_entity(old_name, new_name, dry_run=dry_run, domain=domain, cluster=cluster)

@mcp.tool()
def get_relations(entity_name: str, domain: str, cluster: str = "", depth: int = 1,
                  limit: int = 200, include_relatedness: bool = False) -> str:
    """Read complete document declarations in an exact domain (General is not global).

    Direction, predicates, unresolved diagnostics and truncation come from the shared
    reader. Relatedness is opt-in and is not an asserted fact. Dependency errors propagate.
    """
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("get_relations requires a nonempty exact domain")
    import json

    from vector_lake.tool_relations import get_relations as read_relations

    result = read_relations(entity_name=entity_name, domain=domain, cluster=cluster,
                            depth=depth, limit=limit, include_relatedness=include_relatedness)
    return json.dumps(result, ensure_ascii=False)

@mcp.tool()
def trace_vector_lake(query_or_id: str, domain: str = "", cluster: str = "") -> str:
    """Show legacy provenance, or document relations within an explicit exact domain.

    Scoped traces never mix in the legacy unscoped claim search.
    """
    if domain or cluster:
        return tools.trace_vector_lake(query_or_id, domain=domain, cluster=cluster)
    return tools.trace_vector_lake(query_or_id)

@mcp.tool()
def merge_suggestions_vector_lake(limit: int = 20, enqueue: bool = False) -> str:
    """Detect and surface candidate entity merges.
    
    Args:
        limit: Maximum number of merge candidates to surface.
        enqueue: If True, enqueue the candidates into the governance review queue.
    """
    return tools.merge_suggestions_vector_lake(limit=limit, enqueue=enqueue)

# [CLI-only / Internal]
def gc_vector_lake(
    days: int = 30, dry_run: bool = True, force: bool = False, *,
    domain: str = "", cluster: str = "", expected_plan: dict | None = None,
) -> str:
    """Preview GC safely; execution requires an exact domain and a version-bound plan.

    ``force`` retains the native bulk-limit meaning, not permission to bypass CAS.
    Unscoped legacy destructive calls are intentionally rejected. Use the scoped
    preview's expected_plan for execution; the native tool validates its freshness.
    """
    if not dry_run and (not domain.strip() or expected_plan is None):
        return "Error: destructive GC requires an explicit domain and expected_plan from preview."
    if not domain and not cluster and expected_plan is None:
        return tools.gc_vector_lake(days=days, dry_run=dry_run, force=force)
    parameters = inspect.signature(tools.gc_vector_lake).parameters
    if not ({"domain", "cluster", "expected_plan"} <= parameters.keys() or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )):
        return "Error: native GC does not yet support scoped version-bound plans."
    kwargs: dict[str, Any] = {
        "days": days, "dry_run": dry_run, "force": force,
        "domain": domain, "cluster": cluster, "expected_plan": expected_plan,
    }
    return tools.gc_vector_lake(**kwargs)

# [CLI-only / Internal]
def finalize_ingest(
    files_written: list | None = None,
    processed_data: dict | None = None,
    files_written_payload_file: str = "",
    raw_files_payload_file: str = "",
) -> str:
    """Finalize ingestion after a subagent has produced validated wiki pages.
    
    Args:
        files_written: Direct list of dicts with 'filename' and 'content'.
        processed_data: Claimed job dict with filepath/hash/source_hash/lease fields plus an integration disposition manifest.
        files_written_payload_file: Sandbox JSON file containing files_written.
        raw_files_payload_file: Sandbox JSON file containing processed_data.
    """
    import json
    try:
        if files_written_payload_file or raw_files_payload_file:
            if not files_written_payload_file or not raw_files_payload_file:
                return "Error: Both payload files are required when using the file-based ingest contract."
            files_written = json.loads(_read_payload(files_written_payload_file))
            processed_data = json.loads(_read_payload(raw_files_payload_file))
        if not isinstance(files_written, list) or not isinstance(processed_data, dict):
            return "Error: finalize_ingest requires a files list and processed-data object."
        return tools.finalize_ingest(files_written, processed_data)
    except Exception as e:
        return str(e)

@mcp.tool()
def check_duplicate_entity(candidate_title: str, candidate_type: str, candidate_summary: str = "") -> str:
    """Check if an entity or concept already exists in the graph to prevent duplicates.
    
    Args:
        candidate_title: The title of the entity to create.
        candidate_type: The specific type of the entity (e.g. 'vendor', 'product', 'person', 'event', 'concept').
        candidate_summary: A brief summary of the entity to use for similarity matching.
    """
    return tools.check_duplicate_entity(candidate_title, candidate_type, candidate_summary)

@mcp.tool()
def visualize_vector_lake(output_dir: str | None = None) -> str:
    """Visualize the LLM-Wiki topology as an interactive 3D HTML dashboard.

    ``output_dir`` is an *output* directory, not an input sandbox: this call
    writes exactly one file (``vector_lake_graph.html``) into it, so the gate
    only requires a subdirectory inside an approved agent sandbox or host home
    (never the home/host root itself).  When ``output_dir`` is omitted the file
    goes to the lake's own ``<MEMORY>/scratch`` tree.
    """
    if output_dir:
        from pathlib import Path
        from vector_lake import host_env

        resolved = Path(output_dir).resolve()
        if resolved in set(host_env.host_home_dirs()) or resolved == Path.home().resolve() or not (
            _payload_path_allowed(resolved) or host_env.is_within_host_home(resolved)
        ):
            return "Error: Output directory must be inside an approved agent sandbox."
    return tools.visualize_vector_lake(output_dir, open_browser=False)

@mcp.tool()
def write_wiki_page(filename: str, payload_file: str) -> str:
    """Write or update a single Vector Lake wiki page safely (atomic mutation for manual or ad-hoc page updates).

    Runs schema validation and the mutation coordinator. For bulk synthesis output resulting from
    query_logic_lake reasoning, write files in scratch/ and use finalize_query_synthesis instead.
    
    Args:
        filename: The filename (e.g. 'Concept_Example.md').
        payload_file: Absolute path to a temporary file containing the full markdown content including YAML frontmatter.
    """
    try:
        content = _read_payload(payload_file)
    except Exception as e:
        return str(e)
    from vector_lake.wiki_utils import SafeWriteError
    try:
        from vector_lake.mutation_coordinator import execute_mutation_plan
        ok, message = execute_mutation_plan(filename, content=content, is_delete=False)
        return f"{message} Index completion pending verification via get_mutation_status." if ok else message
    except SafeWriteError as e:
        return f"[Write Rejected] {str(e)}"
    except Exception as e:
        return f"Error writing file: {str(e)}"

import uuid
from vector_lake.governance_store import load_governance_queue, save_governance_queue, _utc_now

@mcp.tool()
def get_mutation_status(outbox_ids: list[int], max_hops: int = 32) -> dict:
    """Read native receipt status without repair, enqueue, SQL writes or blanket completion.

    Superseded receipts follow only bounded same-page equivalent native successors.
    Completion requires the exact current canonical target and native completion;
    missing/unreadable data is unavailable/error, never fabricated success.
    """
    from vector_lake.mutation_receipts import get_mutation_status as observe
    return observe(outbox_ids, max_hops=max_hops)


def _replace_link_syntax(content: str, old_text: str, new_text: str) -> str:
    import re

    # Preserve frontmatter, comments, escaped literals and Markdown code examples.
    protected = re.compile(
        r"\A\ufeff?---\r?\n.*?\r?\n---(?:\r?\n|$)|<!--.*?(?:-->|\Z)|"
        r"^ {0,3}(?P<fence>(?P<fencechar>`|~)(?P=fencechar){2,})[^\n]*\n"
        r".*?(?:^ {0,3}(?P=fence)(?P=fencechar)*[ \t]*(?:\n|$)|\Z)|"
        r"(?<!`)(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)|"
        r"(?P<typed>(?<![\\\[])\[[^\[\]\r\n]+::\s*\[\[[^\[\]\r\n]+\]\]\])|"
        r"(?<![\\\[])\[\[[^\[\]\r\n]+\]\](?!\])",
        re.M | re.S,
    )
    def replace(match):
        text = match.group(0)
        if match.group("typed"):
            return text.replace(old_text, new_text)
        return new_text if text == old_text else text

    return protected.sub(replace, content)


# [CLI-only / Internal]
def batch_replace_links(
    old_text: str, new_text: str, dry_run: bool = True, *, domain: str = "", cluster: str = "",
) -> str:
    """Replace exact complete wikilinks only, in a native version-bound canonical scope.

    Plain text/structural replacements and unscoped destructive legacy calls are
    intentionally rejected. Code, frontmatter and comments are not link assertions.
    Dry-run remains the default; read errors abort rather than commit a partial scan.
    """
    import re
    import sqlite3

    from vector_lake import db_store, governance_store
    from vector_lake.mutation_coordinator import execute_mutation_batch
    from vector_lake.mutation_receipts import (
        ReadBudget,
        _safe_filename,
        canonical_snapshot,
        readonly_snapshot,
    )
    from vector_lake.wiki_utils import get_wiki_dir

    link_pattern = r"\[\[[^\[\]\r\n|#]+(?:#[^\[\]\r\n|]+)?(?:\|[^\[\]\r\n]+)?\]\]"
    if not all(isinstance(text, str) and re.fullmatch(link_pattern, text) for text in (old_text, new_text)):
        return "Error: replacements must be complete [[target]] or [[target|label]] links; arbitrary text is unsafe."
    if not dry_run and not domain.strip():
        return "Error: destructive link replacement requires an explicit domain."
    try:
        budget = ReadBudget()
        with readonly_snapshot(budget) as conn:
            scanned_versions = canonical_snapshot(conn, domain=domain, cluster=cluster, budget=budget)
        if len(scanned_versions) > 2000:
            raise ValueError("Link replacement page budget exceeded")
        wiki_dir = get_wiki_dir()
        mutations, scanned_text = [], {}
        for page_key, version in sorted(scanned_versions.items()):
            filename = _safe_filename(page_key + ".md")
            path = wiki_dir / filename
            size = path.stat().st_size
            budget.check(size)
            if path.is_symlink() or size > 4 * 1024 * 1024:
                raise ValueError("Unsafe or oversized Markdown projection")
            content = path.read_text(encoding="utf-8")
            if governance_store.canonical_page_version_from_content(filename, content) != version:
                raise ValueError(f"Canonical/projection version conflict for {filename}")
            scanned_text[filename] = content
            replaced = _replace_link_syntax(content, old_text, new_text)
            if replaced != content:
                mutations.append({"filename": filename, "content": replaced, "expected_version": version})
        if dry_run:
            return f"[DRY RUN] Would replace links in {len(mutations)} files; scope domain={domain!r}, cluster={cluster!r}."
        if not mutations:
            return "No matching links; no canonical mutation committed."

        def require_scanned_versions():
            current = canonical_snapshot(db_store.get_connection(), domain=domain, cluster=cluster)
            if current != scanned_versions:
                raise ValueError("Canonical scoped scan version conflict")
            for filename, content in scanned_text.items():
                path = wiki_dir / filename
                if path.is_symlink() or path.read_text(encoding="utf-8") != content:
                    raise ValueError(f"Markdown scanned version conflict for {filename}")

        ok, message = execute_mutation_batch(
            mutations, validation_mode="schema", canonical_precondition=require_scanned_versions,
        )
        return f"{message} Index completion pending verification via get_mutation_status." if ok else message
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return f"Error during scoped link replacement: {type(exc).__name__}: {exc}"

def main():
    """Own synchronous embedding clients for this CLI server lifetime."""
    from vector_lake.embedding_scheduler import shutdown_embedding_clients, start_prewarm_thread

    try:
        start_prewarm_thread()
        # stdout carries JSON-RPC; no banner or version check.
        mcp.run(show_banner=False)
    finally:
        shutdown_embedding_clients()


if __name__ == "__main__":
    main()
