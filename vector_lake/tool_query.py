import hashlib
import logging
import os
import re
import time

from vector_lake import get_extension_root, provenance
from vector_lake.template_loader import read_template, render_template_text
from vector_lake.tool_search import assemble_context, split_retrieval_notes
from vector_lake import stub_creator, retrieval_runtime
from vector_lake.wiki_utils import (
    get_wiki_dir,
    normalize_entity_name,
    sanitize_wiki_node,
    validate_wiki_filename,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-query")

# This file used to keep its own copy of the prefix list, and a second, inline copy of
# the same list further down.  Both omitted ``System_`` while the comment claimed to
# mirror ``wiki_utils.VALID_PREFIXES``.  The vocabulary now has one owner, and so does what a
# stub is: ``node_vocabulary`` for the
# prefixes and types, ``stub_creator`` for the page a broken link needs.  This file used to
# keep its own copy of the prefix list, a second inline copy of it, a stub writer, and a
# covering-page check that only it had -- which is why ``tool_lint`` forked entities that this
# file refused to fork.


MAX_QUERY_CONTEXT_FILES = 50
QUERY_CONTEXT_TTL_SECONDS = int(os.environ.get("VECTOR_LAKE_QUERY_CONTEXT_TTL", str(2 * 3600)))


def _prune_stale_query_contexts(tmp_dir) -> None:
    """Drop payload files left by finished queries, so the directory cannot grow without bound.

    Prunes by age (default 2 hours) and caps total count (max 50 files, LRU eviction).
    """
    cutoff = time.time() - QUERY_CONTEXT_TTL_SECONDS
    try:
        entries = sorted(
            tmp_dir.glob("query_context_*.md"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError as exc:
        log.warning("Could not scan %s for stale query payloads: %s", tmp_dir, exc)
        return

    # 1. Prune expired entries
    retained = []
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
            else:
                retained.append(entry)
        except OSError as exc:
            log.warning("Could not remove expired query payload %s: %s", entry.name, exc)
            retained.append(entry)

    # 2. Cap capacity (evict oldest if still exceeding MAX_QUERY_CONTEXT_FILES)
    if len(retained) > MAX_QUERY_CONTEXT_FILES:
        excess = len(retained) - MAX_QUERY_CONTEXT_FILES
        for entry in retained[:excess]:
            try:
                entry.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not evict excess query payload %s: %s", entry.name, exc)


COMPARATIVE_QUERY_PATTERN = re.compile(
    # A word-boundary match, because ``"vs" in query_str`` also fires on "always", "canvas",
    # "reviews" and "obvious".  This is a prompt hint only: the comment it replaced called this
    # "V11.2 Multi-Hop Parallel Retrieval", but no parallel retrieval exists here -- the flag adds
    # one sentence asking the model to weight both sides evenly.
    r"(?<![a-z0-9])vs\.?(?![a-z0-9])|versus|对比",
    re.IGNORECASE,
)


def _render_context_payload(query_str: str, context: dict) -> str:
    """Budget the serialized text and UTF-8 bytes, including headings and notes.

    The byte bound is conservative for byte-fallback tokenizers; it is not an
    exact count for the eventual model's tokenizer.
    """
    max_chars = int(context["budget_max"])
    if max_chars <= 0:
        raise ValueError("budget_max must be positive")
    wiki = str(context.get("wiki_context") or "")
    original_wiki = wiki
    serialization_omissions = []
    spans = context.get("_wiki_spans")
    index = str(context.get("index_summary") or "")
    purpose = str(context.get("purpose") or "")
    notes = [str(note) for note in (context.get("retrieval_notes") or []) if note]
    omitted = int(context.get("memory_omitted_count") or 0)
    if omitted:
        notes.append(f"{omitted} operational memory item(s) were dropped to fit the budget.")

    def render() -> str:
        parts = []
        if COMPARATIVE_QUERY_PATTERN.search(query_str):
            parts.append(read_template("prompts/query/comparison.md", root=get_extension_root()))
        if context.get("memory_packet"):
            parts.append(
                f"\n\n--- OPERATIONAL MEMORY PACKET "
                f"({context.get('memory_count', 0)} items, {context.get('memory_warning_count', 0)} warnings) ---\n"
                f"{context['memory_packet']}"
            )
        if wiki:
            pages = (sum(0 <= start < len(wiki) for _, start in spans) if spans is not None else
                     context.get("wiki_page_count", 0) if wiki == context.get("wiki_context") else wiki.count("- **"))
            parts.append(
                f"\n\n--- RELEVANT WIKI PAGES ({pages} pages, {len(wiki)}/{max_chars} chars) ---\n{wiki}"
            )
        if index:
            parts.append(f"\n\n--- NODE INDEX SUMMARY ---\n{index}")
        information, degradations = split_retrieval_notes(notes)
        for label, items in (("degradations", degradations), ("information", information)):
            if items:
                parts.append(
                    f"\n\n--- RETRIEVAL NOTES ({label}) ---\n" + "\n".join(f"- {note}" for note in items)
                )
        if purpose:
            parts.append(f"\n\n--- PURPOSE ---\n{purpose}")
        return "".join(parts)

    def over_budget(text: str) -> bool:
        return len(text) > max_chars or len(text.encode("utf-8")) > max_chars

    payload = render()
    while over_budget(payload) and wiki:
        starts = [start for _, start in spans if 0 <= start < len(wiki)] if spans is not None else None
        previous = starts[-1] if starts else wiki.rfind("\n- **")
        wiki = wiki[:previous].rstrip() if previous >= 0 else ""
        if not any("Wiki pages omitted" in note for note in notes):
            notes.append("Wiki pages omitted from serialized payload to meet the budget.")
        payload = render()
    while over_budget(payload) and index:
        index = index.rsplit("\n", 1)[0] if "\n" in index else ""
        if not serialization_omissions:
            serialization_omissions.append("Index summary omitted from serialized payload to meet the budget.")
        payload = render()
    if over_budget(payload) and purpose:
        purpose = ""
        notes.append("Purpose omitted from serialized payload to meet the budget.")
        payload = render()
    if over_budget(payload):
        raise ValueError("query metadata, memory and retrieval notes exceed serialized payload budget")
    state = retrieval_runtime.current()
    if state is not None:
        for note in [*notes, *serialization_omissions]:
            if note not in (context.get("retrieval_notes") or []):
                state.problem(note)
        if wiki != original_wiki:
            survivors = {key for key, start in spans if 0 <= start < len(wiki)} if spans is not None else set()
            if state.emitted_keys is not None:
                state.emitted_keys.intersection_update(survivors)
    return payload


@retrieval_runtime.request_entry
def preview_query_context(
    query_str: str, *, domain: str | None = None, cluster: str | None = None,
    allow_external_embedding: bool = False,
    profile: str = 'auto', use_chunks: bool = False,
) -> str:
    """Return a query context inline, without writing a proposal payload or Wiki page.

    Retrieval may record an audit-ledger event; it is not a filesystem-wide dry run.
    """
    options = {"domain": domain, "cluster": cluster,
               "allow_external_embedding": allow_external_embedding,
               "profile": profile, "use_chunks": use_chunks} if domain or cluster else {}
    return _render_context_payload(query_str, assemble_context(query_str, **options))


@retrieval_runtime.request_entry
def prepare_query_context(
    query_str: str, dry_run: bool = False, *, domain: str | None = None,
    cluster: str | None = None, allow_external_embedding: bool = False,
    profile: str = 'auto', use_chunks: bool = False,
):
    options = {"domain": domain, "cluster": cluster,
               "allow_external_embedding": allow_external_embedding,
               "profile": profile, "use_chunks": use_chunks} if domain or cluster else {}
    context = assemble_context(query_str, **options)
    context_block = _render_context_payload(query_str, context)

    # The template is resolved and read *before* the payload is written.  It used to be read
    # afterwards, and a missing template merely set the template text to its own error message --
    # which was then returned as the instruction with no placeholder substituted at all.  The
    # ingest prompt builder already resolves this by raising; the query path was the outlier.
    prompt_template = read_template("prompts/query/main.md", root=get_extension_root())

    # Write context to a temporary payload file
    tmp_dir = get_extension_root() / "tmp"
    # Create a unique hash for this query with anti-collision
    import uuid
    unique_str = f"{query_str}_{time.time()}_{uuid.uuid4().hex}"
    query_hash = hashlib.md5(unique_str.encode("utf-8")).hexdigest()[:12]
    payload_path = tmp_dir / f"query_context_{query_hash}.md"
    
    instructions = render_template_text(prompt_template, payload_path=str(payload_path), query_str=query_str)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _prune_stale_query_contexts(tmp_dir)
    with open(payload_path, "w", encoding="utf-8") as f:
        f.write(context_block)
        
    if dry_run:
        if options:
            trace = f"Domain: {domain}; cluster: {cluster or '(any)'}\n" + str(context.get('wiki_context') or '')
        else:
            trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        return f"[DRY RUN] Context assembled at {payload_path}\n\nTrace:\n{trace}"

    # ``{{wiki_dir}}`` used to be substituted into a template that never contained it. It is
    # removed rather than given a home in the template: this prompt's own trust model forbids the
    # model from naming paths at all ("Do not return paths..."), so handing it a directory to
    # write into would contradict the contract it is being asked to honour.
    return instructions

def finalize_query_synthesis(files_written_str: str, query_str: str) -> str:
    if not files_written_str.strip():
        return "No files were written."
        
    wiki_dir = str(get_wiki_dir())
    changed_node_files = set([f.strip() for f in files_written_str.split(",") if f.strip()])
    
    valid_files = set()
    absent: list[str] = []
    withheld: list[tuple[str, str]] = []
    first_refusal: Exception | None = None
    import pathlib
    wiki_path = pathlib.Path(wiki_dir).resolve()
    
    for filename in changed_node_files:
        # Boundary check to prevent path traversal
        try:
            target_path = (wiki_path / filename).resolve()
            if not target_path.is_relative_to(wiki_path):
                log.warning(f"Security: Path traversal attempt detected: {filename}")
                continue
        except Exception as e:
            log.warning(f"Security: Invalid path {filename}: {e}")
            continue

        # The prefix rule defers to its single owner (``node_vocabulary`` via
        # ``validate_wiki_filename``).  This used to re-derive the rule inline as
        # ``filename.split('_')[0].isupper()`` and then *repair* the name with a bare
        # ``os.rename`` -- a raw filesystem write with no CAS, no outbox row, no change set and no
        # lease, in a tree whose safety model is that writes go through ``execute_mutation_plan``.
        # The repair was also wrong on its own terms: it produced ``Orphan_<name>.md``, and
        # ``Orphan_`` is not in ``VALID_PREFIXES``, so the coordinator's own validator would have
        # refused the name it invented.  A name the canonical validator rejects is now withheld
        # and reported rather than silently rewritten behind the coordinator's back.
        try:
            validate_wiki_filename(filename)
        except ValueError as exc:
            log.warning(
                "Query finalization withheld %s: %s. Rename it through ``rename_entity``, which "
                "also rewrites the internal links a bare rename leaves dangling.",
                filename,
                exc,
            )
            continue

        file_path = os.path.join(wiki_dir, filename)
        if not os.path.exists(file_path):
            absent.append(filename)
            continue

        # A proposal the write gate refuses is a result, not a crash: the page is left exactly
        # as the proposal wrote it, it stays out of the verified set, and the rest of the batch
        # still runs.  The raise used to leave the function from inside the loop -- every later
        # file went unchecked and no stub was generated for the pages that had passed.
        #
        # The refusal class is deliberately not pinned to a tuple.  The gate is a stack:
        # ``defense_hook`` reports schema violations, ``write_markdown_file`` raises
        # ``SafeWriteError`` for its content and path rules, and the YAML parser raises its own
        # error.  Measured on this host, the reachable class is the schema violation -- an
        # earlier guess that named only ``SafeWriteError`` was wrong, and a tuple of today's
        # classes is the same guess.  The exception's own type and message reach the caller, so
        # a refusal here is reported rather than folded into a count.
        try:
            sanitize_wiki_node(file_path)
        except Exception as exc:  # noqa: BLE001 - one refused proposal must not cost the batch
            refusal = f"{type(exc).__name__}: {exc}"
            withheld.append((filename, refusal))
            if first_refusal is None:
                first_refusal = exc
            log.error("Query finalization refused %s: %s", filename, refusal, exc_info=True)
            continue

        valid_files.add(filename)
            
    # Nothing accepted is not a per-page refusal, it is an environment that refused every page
    # (a missing purpose contract is the standing example).  A "0 verified, N withheld" summary
    # would be the silent-degradation shape this tree forbids, so the first refusal is raised
    # instead.  A batch that accepted at least one page remains a report.
    if not valid_files and first_refusal is not None:
        raise first_refusal

    if valid_files:
        # ``valid_files`` means "present on disk", nothing more.  The write belongs to the subagent,
        # so this function cannot attest a change set: it used to print
        # "Canonical change set: mutation_coordinator_handled" while never calling the coordinator.
        #
        # A canonical-version probe was tried as the missing check and **rejected**: a page written
        # with a plain ``write_text`` still reports a version, because the version is derived from
        # the page's own extracted entities rather than from a write record.  It would have passed
        # unconditionally while claiming to prove provenance.  What is reported here is therefore
        # only what was actually observed -- presence, stub counts, and absent names.
        stubs_created, stubs_refused = _generate_stubs_for_broken_links(wiki_dir, valid_files)
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))

        notes = []
        if withheld:
            notes.append(
                f"{len(withheld)} page(s) were refused by a write gate and left exactly as "
                f"proposed: {', '.join(name for name, _ in withheld[:3])} "
                f"(the refusal reason is in the log)."
            )
        if stubs_refused:
            notes.append(f"{stubs_refused} stub write(s) were refused -- a gate rejected them (see the log).")
        if absent:
            notes.append(f"{len(absent)} named file(s) were absent from the wiki: {', '.join(absent[:3])}.")

        return (
            f"Query finalization completed. {len(valid_files)} page(s) verified present. "
            f"{stubs_created} stub(s) generated."
            + (" " + " ".join(notes) if notes else "")
            + f"\n\n{trace}"
        )
    return "Query finalization completed with no valid wiki files synced."


def _generate_stubs_for_broken_links(wiki_dir: str, files_to_scan: set) -> tuple[int, int]:
    """``(created, refused)`` for the broken links in ``files_to_scan``.

    ``refused`` counts writes a gate rejected -- as opposed to names that needed no page, which
    are the normal case and are counted nowhere.  Before this split, a run in which every write
    was refused returned 0 and read exactly like a wiki with nothing to fix.
    """
    existing_files, normalized_existing, existing_cores = stub_creator.existence_index(wiki_dir)
    existing = (existing_files, normalized_existing, existing_cores)
    broken_targets = set()

    for filename in files_to_scan:
        filepath = os.path.join(wiki_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                content = handle.read()
        except Exception:
            continue

        # P1-1: Pre-strip code blocks to avoid fragile stubbing
        content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        content = re.sub(r'`.*?`', '', content)

        for match in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", content):
            raw_target = match.group(1).strip().replace(".md", "")
            target = normalize_entity_name(raw_target)
            covering = stub_creator.covering_page(target, existing)
            if covering:
                if covering != target:
                    log.warning(
                        "Not creating %s.md: %s already covers that name; fix the link instead.",
                        target,
                        covering,
                    )
                continue
            broken_targets.add(target)
        for match in re.finditer(r"\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]", content):
            raw_target = match.group(1).strip().split("|")[0].strip().replace(".md", "")
            target = normalize_entity_name(raw_target)
            covering = stub_creator.covering_page(target, existing)
            if covering:
                if covering != target:
                    log.warning(
                        "Not creating %s.md: %s already covers that name; fix the link instead.",
                        target,
                        covering,
                    )
                continue
            broken_targets.add(target)

    if not broken_targets:
        return 0, 0

    stubs = 0
    refused = 0
    # A name two pages declare is contested, not missing: the owner refuses it, and this caller
    # has to say so too -- it writes stubs as well, and with neither claimant's core matching the
    # name, ``covering_page`` cannot refuse for it.  Measured on the live wiki: 31 such names.
    contested = stub_creator.contested_names(stub_creator.declared_names(wiki_dir))
    # What a stub is -- its name, type, fields and the write path -- belongs to one owner.
    # This used to build the page here and hand ``<target>.md`` to ``execute_mutation_plan``,
    # which no node may be named: the write was refused and the exception swallowed, so this
    # function created no stub at all for an untyped target (and, as it turned out, none for
    # a typed one either).  See ``vector_lake.stub_creator``.
    #
    # ``index`` is the one built before the scan, not a fresh one: ``create_stub`` updates it
    # in place, so a stub written for one target is seen as covering its own name by the next
    # iteration.  A second index here would leave that update on a set nothing reads again.
    for target in sorted(broken_targets):
        outcome = stub_creator.create_stub(wiki_dir, target, existing, contested=contested)
        if outcome.stem:
            stubs += 1
        elif outcome.refused:
            refused += 1
    if refused:
        # The count is about closed gates, not about missing links; folding it into ``stubs``
        # would hide a run in which nothing could be written.
        log.error(
            "%s stub write(s) refused: every stub in this pass failed the same gate "
            "(the log above has the traceback).",
            refused,
        )
    return stubs, refused

