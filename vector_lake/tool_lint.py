import datetime
import logging
import os
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake.wiki_utils import (
    VALID_PREFIXES,
    get_raw_dir,
    get_wiki_dir,
    entity_identity_key,
    normalize_entity_name,
    read_markdown_file,
    write_markdown_file,
)
from vector_lake import stub_creator
from vector_lake.link_resolution import build_link_map, resolve_link_target
from vector_lake.schema_validator import (
    VALID_EPISTEMIC_STATUS,
    VALID_STATUS,
    VALID_TYPES,
    asserted_metric_keys,
    category_shape_violation,
    is_registered_domain,
    missing_required_fields,
    synthesis_skeleton_order_report,
    validate_schema,
    SchemaViolationException,
)
from vector_lake.node_vocabulary import (
    NON_NODE_WIKI_FILES,
    STUB_MARKER_TAG,
    is_generated_artifact,
    is_source_anchor_pair,
    strip_prefix,
)

try:
    import vector_lake_core
    HAVE_CORE = True
except ImportError:
    vector_lake_core = None
    HAVE_CORE = False


# ``VALID_STATUS`` is capitalised while the check below lowercases the page value,
# so the comparison view is normalised once here instead of restating the set.
_LOWERCASE_STATUS = {status.lower() for status in VALID_STATUS}

#: Similarity above which two nodes are reported as duplicates.
SIMILARITY_MERGE_THRESHOLD = 0.91

#: One naming series: two keys that differ only in their numbers are members of one convention
#: (``Source_intelligence-20260219-briefing`` and ``...-20260220-...``), not two names for one
#: entity.  Measured on the live corpus, 3 782 of the 3 797 pairs the similarity pass reported
#: were exactly this, while none of the genuinely duplicated names differs *only* by digits -- so
#: folding digits before drawing the distinction is what separates the two cases.
_NUMERIC_RUN = re.compile(r"\d+")

#: A second frontmatter block where body text should start.  Written by the lint when a page ends
#: up carrying two, which is how a placeholder block came to sit above a page's real fields.
_EMBEDDED_FRONTMATTER = re.compile(r"^\ufeff?\s*---\r?\n")


def _naming_series_key(key: str) -> str:
    """``Source_intelligence-20260219-briefing`` -> ``intelligence-#-briefing``.

    Case is folded for the same reason ``entity_identity_key`` folds it -- spelling is not identity
    -- and the type prefix is dropped because a naming convention is a property of the *name*:
    ``Source_Intelligence-20260715-Briefing`` and ``Source_intelligence-20260315-briefing`` are one
    convention, and comparing raw keys called them duplicates of each other.
    """
    return _NUMERIC_RUN.sub("#", strip_prefix(key).casefold())


def _type_prefix(key: str) -> str:
    """``Concept_HIS`` -> ``Concept``; an untyped stem keeps its whole self."""
    return key.split("_")[0] if "_" in key else ""


def _candidate_name_pairs(names: list[str], threshold: float):
    """Yield upper-bound survivors in the legacy length-bucket order, in bounded batches."""
    native = getattr(vector_lake_core, "NamePairIndex", None) if HAVE_CORE else None
    if (native is not None and len(names) <= 100_000
            and sum(map(len, names)) <= 16_000_000
            and not any(re.search(r"[\ud800-\udfff]", name) for name in names)):
        index = native(names, threshold)
        done = False
        while not done:
            pairs, done = index.next_pairs()
            yield from pairs
        return
    lengths = [len(name) for name in names]
    counts = [Counter(name) for name in names]
    masks = [_character_mask(name) for name in names]
    by_length: dict[int, list[int]] = defaultdict(list)
    for index, length in enumerate(lengths):
        by_length[length].append(index)
    ordered_lengths = sorted(by_length)
    for length_index, length in enumerate(ordered_lengths):
        for other_length in ordered_lengths[length_index:]:
            if not _length_ceiling_can_exceed(length, other_length, threshold):
                break
            left, right = by_length[length], by_length[other_length]
            for position, a in enumerate(left):
                for b in right[position + 1 if other_length == length else 0:]:
                    total = length + other_length
                    if total:
                        unmatched = (masks[a] & ~masks[b]).bit_count()
                        if 2.0 * (length - unmatched) / total <= threshold:
                            continue
                        if 2.0 * _common_character_count(counts[a], counts[b]) / total <= threshold:
                            continue
                    yield a, b


def _family_sizes(pairs: list[tuple[str, str]]) -> list[int]:
    """Sizes of the connected components of a pair graph, largest first.

    A pair list is not a finding list: ``Concept_A-1``/``Concept_A-2`` and
    ``Concept_A-2``/``Concept_A-3`` are one family of three names, and reporting three lines for it
    hides that the operator has one thing to decide.  The component sizes are what the headline
    should say.
    """
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for left, right in pairs:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root
    sizes: Counter = Counter()
    for item in list(parent):
        sizes[find(item)] += 1
    return sorted(sizes.values(), reverse=True)


def _histogram(sizes: list[int], top: int = 4) -> str:
    """``[4, 3, 2, 2, 1]`` -> ``4×1, 3×1, 2×2, 1×1`` (largest sizes first)."""
    counts = Counter(sizes)
    return ", ".join(
        f"{size}×{count}" for size, count in sorted(counts.items(), reverse=True)[:top]
    )


#: Bits in the character mask the similarity pass filters by.  256 is enough that two names of
#: realistic length collide rarely (measured: the filter keeps 1.7% of length-band pairs), and a
#: collision only *weakens* the bound, never invalidates it -- which is what lets the filter be a
#: hashed one and still exact.
_CHARACTER_MASK_BITS = 256


def _length_ceiling_can_exceed(length_a: int, length_b: int, threshold: float) -> bool:
    """Whether two names of these lengths can reach ``threshold``.

    ``ratio()`` is ``2 * matches / (len(a) + len(b))`` and ``matches`` cannot exceed
    ``min(len(a), len(b))``, so ``2 * min / (min + max)`` is a hard ceiling on the score.
    Comparing that ceiling before constructing a ``SequenceMatcher`` is exact: it never drops a
    pair that would have scored above the threshold.

    This is the whole of the length rule and it has two callers, so it lives here rather than in
    either: ``_ratio_can_exceed`` for a pair of names it already has, and the similarity pass to
    decide which length buckets can still pair up at all.

    An empty pair is left to ``SequenceMatcher``: ``ratio()`` returns 1.0 when both sides are
    empty, so skipping it would change the findings rather than the cost.
    """
    longest = max(length_a, length_b)
    if longest == 0:
        return True
    return (2.0 * min(length_a, length_b) / (length_a + length_b)) > threshold


def _ratio_can_exceed(name_a: str, name_b: str, threshold: float) -> bool:
    """Whether ``SequenceMatcher(None, a, b).ratio()`` can reach ``threshold``.

    On the live corpus the length ceiling removes 255 073 of the 375 283 comparisons the old
    50-wide window made, which is where the ~30 s the similarity pass used to take went.

    An empty pair is left to ``SequenceMatcher``: ``ratio()`` returns 1.0 when both
    sides are empty, so skipping it would change the findings rather than the cost.
    """
    return _length_ceiling_can_exceed(len(name_a), len(name_b), threshold)


def _character_mask(name: str) -> int:
    """One bit per distinct character, hashed into ``_CHARACTER_MASK_BITS``."""
    mask = 0
    for character in name:
        mask |= 1 << (ord(character) % _CHARACTER_MASK_BITS)
    return mask


def _common_character_count(counts_a: Counter, counts_b: Counter) -> int:
    """The most characters a common subsequence of the two names can possibly use.

    A subsequence uses each character at most as often as either side holds it, so the sum of the
    per-character minima is an upper bound on ``SequenceMatcher.matches`` -- which is what makes
    this usable as a filter rather than a score.
    """
    get = counts_b.get
    common = 0
    for character, count in counts_a.items():
        other = get(character, 0)
        common += count if count < other else other
    return common


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-lint")


def _type_from_filename(filename: str) -> str:
    """The node type the filename's prefix already declares.  Mechanical, not a judgement.

    One owner for the derivation: the missing-field repair and the invalid-type repair both
    need it, and they disagreed -- one read the prefix, the other wrote ``concept`` for every
    page regardless of what its name said.
    """
    return filename.split("_", 1)[0].lower() if "_" in filename else "concept"


def _write_fixed_frontmatter(filepath: str, frontmatter: dict, body: str) -> bool:
    try:
        write_markdown_file(filepath, frontmatter, body, skip_validation=False)
        return True
    except Exception as e:
        log.warning(f"Failed to write fixed frontmatter to {filepath}: {e}")
        return False


def _generate_id(stem: str) -> str:
    """The id a page that has none gets.  One owner: :func:`stub_creator.generate_id`.

    This was a second, random generator -- the opposite convention to the one stubs use, for the
    same thing, so two answers existed for "what is a page's id".  Delegating also makes the
    duplicate-id repair idempotent, and two pages sharing an id get different new ones because
    their stems differ.
    """
    return stub_creator.generate_id(stem, datetime.datetime.now().strftime("%Y-%m-%d"))

def lint_vector_lake(auto_fix: bool = False):
    wiki_dir = str(get_wiki_dir())
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."

    # Linting scope and link resolution are two different questions, and one list used to
    # answer both.
    #
    # A file the wiki writes about itself is not a node, so it must not be *linted* --
    # checked for a valid prefix, an id, aliases, a type.  The set here held only the
    # first three of the six, so ``orphan_pages.md``, ``wiki_link_stats.md`` and
    # ``Synthesis_log.md`` were linted as nodes: reported as "Does not start with valid
    # prefix" and, under ``auto_fix``, renamed to ``Concept_orphan_pages.md``.
    #
    # But such a file does *exist*, so a link to it must *resolve*: otherwise the link is
    # reported broken and a stub is invented beside the real page.  Because both
    # questions read the same list, ``index``/``log``/``overview`` were exactly the three
    # names that could not resolve a link.
    listed = [name for name in os.listdir(wiki_dir) if name.endswith(".md")]
    files = [name for name in listed if name not in NON_NODE_WIKI_FILES]
    # Every vocabulary is imported from its single owner.  The local copies that
    # used to live here had already drifted: ``valid_status`` was missing
    # ``archived`` and ``contested``, so every page using those two legal statuses
    # was reported as an invalid status even though schema_validator accepts them.
    # tests/test_lint_vocabularies.py fails if a copy reappears.
    #
    # ``categories`` is not aliased here at all: that rule is not read, it is *asked* -- see
    # ``category_shape_violation`` below.  An alias would still be a second copy of the
    # decision, which is what this file's history is a list of.
    valid_types = VALID_TYPES
    valid_status = _LOWERCASE_STATUS
    valid_epistemic = VALID_EPISTEMIC_STATUS
    valid_prefixes = VALID_PREFIXES

    files = [name for name in listed if name not in NON_NODE_WIKI_FILES]
    issues = {key: [] for key in ["frontmatter", "schema", "naming", "type_status", "category", "domain", "duplicate_id", "alias_conflict", "broken_links", "orphan", "similarity", "decay", "semantic_gc", "governance", "alignment", "evidence", "source_path"]}
    fixes_applied = 0
    stubs_refused = 0
    #: how many pages assert a controlled metric, keyed by the tier that supports them.
    metric_tiers: Counter = Counter()
    #: the census line, rendered under section 16 but not a finding.  A healthy census must not
    #: make the section read FAIL -- it did, until this was separated from ``issues``.
    metric_evidence_summary: list[str] = []
    #: the same shape for section 12: how much of the unsupported-claim debt was decided to be
    #: unrecorded provenance rather than left open.  A decision is context, not a finding.
    governance_summary: list[str] = []

    parsed = {}
    id_map = {}
    alias_map = {}
    all_keys = set()
    link_target_map = {}
    #: Declared names (titles and aliases) -> the pages claiming them, resolved after the parse
    #: loop so that only unambiguous declarations reach ``link_target_map``.
    declared: dict[str, list[str]] = defaultdict(list)
    #: The same claims keyed by *normalised* name, for deciding whether a name is contested.  Raw
    #: spelling cannot answer that: ``title: Atrium Health`` and ``[[Atrium-Health]]`` are one name
    #: (``_``/``-`/space), and looking it up literally both mislabelled the link "does not exist"
    #: and bypassed the auto-fix guard below -- which wrote a third page for a live contested name.
    declared_norm: dict[str, set[str]] = defaultdict(set)
    inbound_count = defaultdict(int)

    # Every page on disk is a link target, whether or not it is linted.
    for filename in listed:
        node_key = filename[:-3]
        all_keys.add(node_key)
        link_target_map[node_key] = node_key

    # A link may name a page by its *core* name -- ``[[Concept_CoMET]]`` where only
    # ``Product_CoMET.md`` exists.  That is the same rule the stub creator uses to decide
    # whether a page already covers a target, and without it lint reported live targets as
    # broken that a page already answers (24 distinct targets, 67 link occurrences), while
    # ``--auto-fix`` used to "fix" them by forking a second page beside the real one.
    #
    # Only unambiguous cores are added, and only for pages that are nodes at all.  Two pages
    # sharing a core name is a real defect -- the live wiki has 40 such families -- and
    # resolving such a link to whichever page sorts first would hide it; left unresolved, the
    # report keeps it visible (and now names the contested pages).  Measured on the live wiki,
    # none of the links this rule rescues points at an ambiguous core, so refusing them costs
    # nothing there.  Exact filenames, titles and aliases keep precedence.
    #
    # ``System_*`` pages and the non-node artifacts are left out of the core map even though
    # their files exist: a link resolving to one would satisfy this check while the indexer
    # deletes that page from the graph, which is the same "hide the gap" trade the stub creator
    # refuses when it declines to *create* one.  The exact-spelling route still finds them, as it
    # always did; this only declines to widen that.
    # First Pass: Read and parse the files that are nodes
    for filename in files:
        filepath = os.path.join(wiki_dir, filename)
        node_key = filename[:-3]
        try:
            frontmatter, body, content = read_markdown_file(filepath)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            issues["frontmatter"].append(f"{filename}: Cannot read file ({type(exc).__name__}: {exc})")
            continue

        if not content.startswith("---"):
            issues["frontmatter"].append(f"{filename}: Missing YAML frontmatter entirely")
            continue

        # A second frontmatter block inside the body means the file carries two of them: a write
        # appended new frontmatter on top of a page whose own frontmatter then became body text.
        # ``Concept_Full-Stack-Builder`` sat that way (real title, domain and sources stranded below
        # a placeholder block) and no check reported it -- this pass read the first block, found it
        # well formed, and moved on.  The consequence is not cosmetic: the page is classified by the
        # placeholder (``Uncategorized``, ``sources: []``) while its actual provenance is unreachable.
        if _EMBEDDED_FRONTMATTER.match(body):
            issues["frontmatter"].append(
                f"{filename}: contains a second YAML frontmatter block in the body "
                "(the page was written twice; its real fields may be below the first block)"
            )

        links = set()
        for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content):
            links.add(match.group(1).strip().replace(".md", ""))
        for match in re.finditer(r"\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]", content):
            links.add(match.group(1).strip().split("|")[0].strip().replace(".md", ""))
        # ``[[raw/…]]`` is a provenance annotation, not a page link: ``claim_extractor`` writes
        # ``(Source: [[<declared file>]])`` and ``anchor_backfill`` appends exactly that, so the
        # target is a path under ``raw/`` that no wiki page will ever answer.  Counting them as
        # broken links reported 154 of them the moment the ``sources`` declarations were converted
        # from page names to raw paths -- the same annotation, now resolvable by the extractor and
        # unresolvable by a page index.  Filtered here rather than at the reporting end so the
        # inbound/outbound counts the other checks read are unaffected too.
        links = {target for target in links if not target.startswith("raw/")}
        links.discard("")

        parsed[filename] = {"fm": frontmatter, "body": body, "links": links, "path": filepath}

        node_id = frontmatter.get("id", "")
        if node_id:
            id_map.setdefault(str(node_id), []).append(filename)

        # Titles and aliases are *declarations*, and a declaration is only usable when exactly
        # one page makes it.  These used plain assignment, so a name two pages both claimed
        # resolved to whichever one ``os.listdir`` happened to read last -- and a declaration
        # that collided with an existing page's filename overwrote that page's own stem, which
        # made a link to a real file resolve to a different one.  Now: collect, then add only the
        # unambiguous ones, and never over a filename (``setdefault``).  A contested name stays
        # unresolved, which is the visible outcome the core-name rule already chose; the existing
        # alias-conflict check still reports it.
        title = frontmatter.get("title")
        if title:
            declared[str(title).strip()].append(node_key)
            declared_norm[entity_identity_key(str(title).strip())].add(node_key)

        aliases = frontmatter.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            for alias in aliases:
                alias_str = str(alias).strip()
                declared[alias_str].append(node_key)
                declared_norm[entity_identity_key(alias_str)].add(node_key)
                alias_map.setdefault(alias_str, []).append(filename)

    # Only now can a declaration be judged: every page has been read, so "how many pages claim
    # this name" is known.  Unambiguous declarations join the map without displacing anything.
    # One owner for the rules (``vector_lake.link_resolution``): the linter and the indexer both
    # resolve links, and when they disagreed the same link was fine here and dropped there.
    link_map, core_pages, unique_cores, _contested = build_link_map(all_keys, declared)
    link_target_map = dict(link_map)

    for filename, data in parsed.items():
        for target in data["links"]:
            real_key = resolve_link_target(target, link_target_map, unique_cores)
            inbound_count[real_key or target] += 1

    # Apply Auto-fixes iteratively
    # 1. Naming Compliance
    renamed_files = {}
    for filename in files:
        if not filename.startswith(valid_prefixes):
            issues["naming"].append(f"{filename}: Does not start with valid prefix")
            if auto_fix:
                new_filename = f"Concept_{filename}"
                normalized_new = normalize_entity_name(new_filename[:-3]) + ".md"
                
                from vector_lake.tool_rename import rename_vector_lake_entity
                result = rename_vector_lake_entity(filename, normalized_new, dry_run=False)
                if "Error" in result or "failed" in result.lower():
                    log.error(f"Auto-fix rename failed for {filename}: {result}")
                    continue
                
                renamed_files[filename] = normalized_new
                all_keys.remove(filename[:-3])
                all_keys.add(normalized_new[:-3])
                fixes_applied += 1

    # Update parsed dict if renaming occurred
    if renamed_files:
        new_parsed = {}
        for fname, data in parsed.items():
            if fname in renamed_files:
                new_fname = renamed_files[fname]
                old_key = fname[:-3]
                new_key = new_fname[:-3]
                data["path"] = os.path.join(wiki_dir, new_fname)
                new_parsed[new_fname] = data
                
                for t, rk in list(link_target_map.items()):
                    if rk == old_key:
                        link_target_map[t] = new_key
                # The file exists under its new name now, and a filename outranks a declaration --
                # without this the rest of the pass would still let a declaration spelling it win.
                link_target_map[new_key] = new_key
                if old_key in inbound_count:
                    inbound_count[new_key] += inbound_count.pop(old_key)
            else:
                new_parsed[fname] = data
        parsed = new_parsed
        files = list(parsed.keys())

    # 2. Duplicate IDs
    for node_id, filenames in id_map.items():
        if len(filenames) > 1:
            issues["duplicate_id"].append(f"ID '{node_id}' shared by: {', '.join(filenames)}")
            if auto_fix:
                for fname in filenames[1:]:
                    if fname in parsed:
                        parsed[fname]["fm"]["id"] = _generate_id(fname[:-3])
                        if _write_fixed_frontmatter(parsed[fname]["path"], parsed[fname]["fm"], parsed[fname]["body"]):
                            fixes_applied += 1

    # 3. Alias Conflicts
    for alias, filenames in alias_map.items():
        if len(filenames) > 1:
            issues["alias_conflict"].append(f"Alias '{alias}' claimed by: {', '.join(filenames)}")
            if auto_fix:
                for fname in filenames[1:]:
                    if fname in parsed:
                        aliases = parsed[fname]["fm"].get("aliases", [])
                        if isinstance(aliases, str):
                            aliases = [aliases]
                        if alias in aliases:
                            aliases.remove(alias)
                            parsed[fname]["fm"]["aliases"] = aliases
                            if _write_fixed_frontmatter(parsed[fname]["path"], parsed[fname]["fm"], parsed[fname]["body"]):
                                fixes_applied += 1
                            # The alias is gone from disk now, so it is no longer claimed here:
                            # otherwise one report says both "claim removed" and "two pages
                            # declare this name", about the same run.
                            if len(alias_map[alias]) == 2:
                                declared_norm.pop(entity_identity_key(alias), None)

    # 4. Broken Links (Stub Creation)
    # Names two or more pages declare, computed *here* rather than before check 3: the alias
    # auto-fix above can strip the losing claim from disk, and a set captured earlier would still
    # call the name contested in the same run that reported removing the claim.
    contested_names = stub_creator.contested_names(declared_norm)
    # The stub index is built once for the pass and updated in place by the creator, so a
    # stub created for one link is not "missing" when the next link is looked at.
    stub_index = stub_creator.existence_index(wiki_dir)
    for filename, data in parsed.items():
        for target in data["links"]:
            if resolve_link_target(target, link_target_map, unique_cores) is None:
                # A contested name is reported as broken, but saying only that leaves the
                # operator nothing to act on: the name is not unknown, it is ambiguous.  The
                # item stays in this bucket so the count means the same thing.
                #
                # Declaration first, because it is the more precise of the two causes and names
                # the pages that made the claim.  Both tables now hold a name two pages declare
                # -- the core table folds aliases in so links can reach them -- so testing the
                # core table first swallowed this branch and reported a declaration contest as a
                # core-name collision.
                contested = core_pages.get(entity_identity_key(strip_prefix(target)))
                claimants = sorted(declared_norm.get(entity_identity_key(target), ()))
                if len(claimants) > 1:
                    # A title or alias two pages both claim: not unknown, contested.
                    detail = (
                        f"target does not exist ({len(claimants)} pages declare that name: "
                        f"{', '.join(sorted(claimants))})"
                    )
                elif contested and len(contested) > 1:
                    detail = (
                        f"target does not exist ({len(contested)} pages share that name: "
                        f"{', '.join(sorted(contested))})"
                    )
                else:
                    detail = "target does not exist"
                issues["broken_links"].append(f"{filename} -> [[{target}]]: {detail}")
                if auto_fix:
                    # The filename prefix and the frontmatter ``type`` are one decision, and the
                    # type has to come from the vocabulary.  This used to be written out here,
                    # naming the file after the target's own prefix while always declaring
                    # ``concept``, so ``[[Vendor_X]]`` produced ``Vendor_X.md`` typed
                    # ``concept`` -- which the write refused, and the exception was caught and
                    # logged, so the link was simply never fixed and the report said nothing.
                    # What a stub is now belongs to one owner; see
                    # ``vector_lake.stub_creator`` for the rules and why each was chosen.
                    outcome = stub_creator.create_stub(
                        wiki_dir, target, stub_index, contested=contested_names
                    )
                    if outcome.refused:
                        stubs_refused += 1
                    elif outcome.stem:
                        all_keys.add(outcome.stem)
                        link_target_map[outcome.stem] = outcome.stem
                        # Keep the core map in step: within one pass, a link written before this
                        # stub must resolve against what is now on disk.
                        unique_cores.setdefault(
                            entity_identity_key(strip_prefix(outcome.stem)), outcome.stem
                        )
                        fixes_applied += 1

    # 5. Frontmatter, Type, Status, Category
    for filename, data in parsed.items():
        frontmatter = data["fm"]
        changed = False

        missing = missing_required_fields(frontmatter, filename)
        if missing:
            issues["frontmatter"].append(f"{filename}: Missing fields: {', '.join(missing)}")
            if auto_fix:
                # Repair only what the filename already determines.  This block used to invent a
                # value for every judgement field it found missing -- ``Active``, ``seed``,
                # ``edge``, ``General`` and ``["Uncategorized"]`` -- which is where the corpus's
                # 3 000 unclassified pages came from and why the status axis read 99.5%
                # ``Active``.  A missing judgement is a governance event: it is reported above and
                # left for the author or the migration, not filled in behind their back.
                #
                # ``evidence_tier="derived"`` was worse than the others: ``derived`` is not one
                # of the five tiers ``purpose.md`` declares, so the write this repair produced
                # was refused by the purpose gate and never landed at all.
                if not frontmatter.get("id"):
                    frontmatter["id"] = _generate_id(node_key)
                if not frontmatter.get("title"):
                    frontmatter["title"] = filename[:-3]
                if not frontmatter.get("type"):
                    frontmatter["type"] = _type_from_filename(filename)
                if not frontmatter.get("updated"):
                    frontmatter["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if "sources" not in frontmatter:
                    frontmatter["sources"] = []
                changed = True

        file_type = str(frontmatter.get("type", "")).lower()
        if file_type and file_type not in valid_types:
            issues["type_status"].append(f"{filename}: Invalid type '{file_type}'")
            if auto_fix:
                # An invalid type is corrected to what the *name* says it is.  Writing
                # ``concept`` instead renamed every Vendor_/Product_ page's type to the same
                # value the name already contradicted.
                frontmatter["type"] = _type_from_filename(filename)
                changed = True

        status = str(frontmatter.get("status", "")).lower()
        if status and status not in valid_status:
            issues["type_status"].append(f"{filename}: Invalid status '{status}'")

        epistemic = str(frontmatter.get("epistemic-status", "")).lower()
        if epistemic and epistemic not in valid_epistemic:
            issues["type_status"].append(f"{filename}: Invalid epistemic-status '{epistemic}'")

        system_artifact = is_generated_artifact(frontmatter, filename, data["body"])
        # The category rule is reported from its single owner rather than re-implemented here.
        # When this file carried its own copy it drifted from the gate it was describing.
        shape = category_shape_violation(frontmatter, filename)
        if shape:
            issues["category"].append(f"{filename}: {shape}")

        # A stub has no subject yet and a generated artifact has none by design, so neither is
        # reported for an unregistered one: 13 of the 118 pages this section used to flag were
        # ``auto-stub`` placeholders carrying the literal word ``Uncategorized`` as their domain.
        # The facet has two tiers -- macro domain, or a vertical registered in the schema.
        domain = str(frontmatter.get("domain") or "").strip()
        is_stub = STUB_MARKER_TAG in (frontmatter.get("tags") or [])
        if domain and not system_artifact and not is_stub and not is_registered_domain(domain):
            issues["domain"].append(
                f"{filename}: domain '{domain}' is neither a macro domain nor a registered vertical"
            )

        # Metric evidence.  ``evidence_tier`` was required of every page and filled on 10% of
        # them, because nothing read it.  It is the machine-readable form of "the vendor said so"
        # against "independently verified", so it is asked for where that distinction changes a
        # decision: a page that puts a number into its compiled truth.  The gate refuses a new
        # page that asserts a metric without one; this census is the reader that makes the field
        # worth writing at all.
        metric_keys = asserted_metric_keys(data["body"])
        if metric_keys:
            tier = str(frontmatter.get("evidence_tier") or "").strip()
            metric_tiers[tier or "<none>"] += 1
            if not tier:
                issues["evidence"].append(
                    f"{filename}: asserts {', '.join(sorted(set(metric_keys)))} without an "
                    "evidence_tier"
                )

        if auto_fix and changed:
            if _write_fixed_frontmatter(data["path"], frontmatter, data["body"]):
                fixes_applied += 1
            else:
                issues["frontmatter"].append(f"{filename}: Auto-fix write failed")

        try:
            validate_schema(frontmatter, data["body"], filename)
        except SchemaViolationException as e:
            issues["schema"].append(f"{filename}: {str(e)}")

        # The synthesis skeleton rule is enforced as *presence*; where it sits is reported, so a
        # page whose required sections are buried at the bottom cannot read as compliant with the
        # documented rule (which asks the document to open with them).
        if filename.startswith("Synthesis_"):
            skeleton_report = synthesis_skeleton_order_report(data["body"])
            if skeleton_report:
                issues["schema"].append(f"{filename}: {skeleton_report}")

    # The census line is what makes the field readable at a glance: how many pages put a number
    # into their compiled truth, and how many of them said what supports it.
    if metric_tiers:
        total_asserting = sum(metric_tiers.values())
        missing = metric_tiers.get("<none>", 0)
        spread = ", ".join(
            f"{tier} {count}"
            for tier, count in metric_tiers.most_common()
            if tier != "<none>"
        )
        metric_evidence_summary.append(
            f"coverage: {total_asserting - missing} of {total_asserting} metric-asserting page(s) "
            f"declare a tier ({spread or 'none declared'})"
        )

    # 6. Name collisions: one name under two type prefixes, and near names within one type.    #
    # This check reports name shape, and stops short of calling it duplication.  What counts as
    # the same entity is decided by ``governance_metrics.find_merge_candidates`` -- which reads
    # the canonical entities, weighs declared names and aliases, and carries reasons and an
    # ambiguity guard -- and lands in the governance queue through
    # ``merge_suggestions_vector_lake``.  Two detectors of "are these duplicates" is how the
    # weaker one came to be the one an operator read, so this one no longer claims the answer.
    keys_list = sorted(list(all_keys))
    collision_pairs: list[tuple[str, str]] = []
    series_pairs: list[tuple[str, str]] = []
    #: pairs a reader already opened and decided are distinct entities.  Read once: the ledger is a
    #: file, and the similarity loop below is millions of comparisons.
    from vector_lake.name_collision_ledger import accepted_pair_keys, pair_key

    accepted_collision_keys = accepted_pair_keys()
    accepted_collision_pairs = 0

    # The name-likeness pass below cannot see the corpus's dominant duplication shape: one name
    # under two type prefixes.  It refuses any pair whose type prefix differs, and its window is
    # positional, so ``Concept_DRG-DIP`` and ``Policy_DRG-DIP`` are excluded by that guard *and*
    # far apart in sorted order.  Same name under a different type is not near-name similarity, it
    # is an identity collision, so it is grouped by the identity key the rest of the lake already
    # resolves links by: ``strip_prefix`` removes the type and ``entity_identity_key`` folds case
    # and separators -- the same rule ``link_resolution`` and ``stub_creator`` use, rather than a
    # third answer to "when are two names the same name".
    identity_groups: dict[str, list[str]] = defaultdict(list)
    for key in keys_list:
        identity_groups[entity_identity_key(strip_prefix(key))].append(key)
    #: pairs withheld because the pattern is by design, not because they are the same entity.
    source_anchor_pairs = 0
    artifact_pairs = 0
    #: the pages the wiki generates about itself: a community index is not a duplicate of the page
    #: it was named after (4 of the 33 identity pairs were exactly that, and 3 of them would have
    #: been "merged" into the concept whose cluster they index).
    artifact_keys = {
        name[:-3]
        for name, data in parsed.items()
        if is_generated_artifact(data["fm"], name, data["body"])
    }
    for members in identity_groups.values():
        if len(members) < 2 or len({_type_prefix(member) for member in members}) < 2:
            continue
        ordered = sorted(members)
        for left_index, key_a in enumerate(ordered):
            for key_b in ordered[left_index + 1:]:
                if key_a in artifact_keys or key_b in artifact_keys:
                    artifact_pairs += 1
                    continue
                # A ``Source_*`` page records the *document* a node came from, so a source page and
                # the node derived from it share a name whenever the document is named after its
                # subject -- ``Source_哲学家的工具箱`` beside ``Concept_哲学家的工具箱``.  That is
                # the ingest contract, not a duplicate: merging them would delete either the
                # provenance record or the knowledge.  Measured: 9 of the 42 identity pairs, and 9
                # of the merge detector's 20 candidates for the same reason (alias overlap).
                if is_source_anchor_pair(key_a, key_b):
                    source_anchor_pairs += 1
                    continue
                # No series test here.  A cross-type pair qualifies only because the *name* is the
                # same, and the same name is a duplicate however many digits it contains --
                # ``Concept_2023全国深化医改经验推广会`` and ``Event_2023全国深化医改经验推广会``
                # are one event.  Two names whose digits differ have different identity keys and
                # never reach this loop.
                issues["similarity"].append(
                    f"Identity collision: {key_a}.md <-> {key_b}.md (same name, different type "
                    f"prefix: {_type_prefix(key_a)} vs {_type_prefix(key_b)})"
                )
                collision_pairs.append((key_a, key_b))

    # One name under one type prefix, compared exactly.  The pass this replaced compared each key
    # with the 49 that follow it in sorted order, on the assumption that lexicographic neighbours
    # are the similar ones.  They are not: measured on the live corpus it reported 3 797 of the
    # 6 058 pairs that clear the threshold -- 37% of candidates silently dropped -- and the pairs
    # that look most like real duplicates were among the misses (``Concept_LLM-as-a-Judge`` /
    # ``Concept_VLM-as-a-judge``, ``Concept_Transformer`` / ``Concept_循环Transformer``), because
    # ``Concept_L...`` and ``Concept_V...`` sit ~1 000 positions apart among 3 983 concept pages.
    #
    # Exactness is affordable if the filters are ordered by cost, and all three are *upper bounds*
    # on ``ratio()``, so none of them can drop a pair the score itself would have accepted:
    #
    #  1. length band -- ``_length_ceiling_can_exceed``: a pair whose lengths are further apart than
    #     ``threshold / (2 - threshold)`` cannot reach the threshold at all;
    #  2. character mask -- every character of ``a`` that ``b`` lacks entirely contributes at least
    #     one unmatched character, so ``matches <= len(a) - popcount(mask_a & ~mask_b)``;
    #  3. character multiset -- ``_common_character_count`` bounds ``matches`` by the per-character
    #     minima, which is much tighter and rules out all but a fraction of a percent.
    #
    # Measured on the live corpus: 1 761 216 length-band pairs -> 29 777 after the mask -> 6 532
    # after the multiset -> 6 058 ``SequenceMatcher`` calls, in 2.7 s.  The old window made 375 283
    # comparisons in ~1 s and found 3 797 of those 6 058.
    core_names = {key: (key.split("_", 1)[1] if "_" in key else key).lower() for key in keys_list}
    series_keys = {key: _naming_series_key(key) for key in keys_list}
    by_type_prefix: dict[str, list[str]] = defaultdict(list)
    for key in keys_list:
        by_type_prefix[_type_prefix(key)].append(key)

    for type_members in by_type_prefix.values():
        names = [core_names[key] for key in type_members]
        for left_index, right_index in _candidate_name_pairs(names, SIMILARITY_MERGE_THRESHOLD):
            key_a, key_b = type_members[left_index], type_members[right_index]
            if not names[left_index] and not names[right_index]:
                ratio = 1.0
            elif HAVE_CORE and hasattr(vector_lake_core, "fast_sequence_matcher_ratio"):
                ratio = vector_lake_core.fast_sequence_matcher_ratio(names[left_index], names[right_index])
            else:
                ratio = SequenceMatcher(None, names[left_index], names[right_index]).ratio()
            if ratio > SIMILARITY_MERGE_THRESHOLD:
                if series_keys[key_a] == series_keys[key_b]:
                    # Same convention, different date/issue/version: a distinct entity,
                    # not a second name for this one.  Kept for the summary rather
                    # than reported, and decided after the score so the count means
                    # "pairs the score would have called duplicates".
                    series_pairs.append((key_a, key_b))
                    continue
                if pair_key(key_a, key_b) in accepted_collision_keys:
                    # Read and rejected once already.  Re-reporting it every run is how
                    # a stable count stops carrying information; the ledger holds the
                    # reason, and a pair can be carved back out of it.
                    accepted_collision_pairs += 1
                    continue
                issues["similarity"].append(
                    f"Name collision: {key_a}.md <-> {key_b}.md ({ratio:.0%})"
                )
                collision_pairs.append((key_a, key_b))

    collision_families = _family_sizes(collision_pairs)
    series_families = _family_sizes(series_pairs)
    identity_collisions = sum(
        1 for key_a, key_b in collision_pairs if _type_prefix(key_a) != _type_prefix(key_b)
    )
    collision_summary = [
        f"collisions: {identity_collisions} identity (same name, different type prefix) + "
        f"{len(collision_pairs) - identity_collisions} name (near name, same type)",
        f"families: {len(collision_families)} over {sum(collision_families)} names"
        + (f"; sizes {_histogram(collision_families)}" if collision_families else ""),
        f"excluded as one naming convention: {len(series_pairs)} pairs in "
        f"{len(series_families)} families (largest {max(series_families, default=0)} names)",
        f"excluded as a source page beside its own node: {source_anchor_pairs} pairs "
        "(the provenance record and the node it fed share a name by design)",
        f"excluded as a generated index: {artifact_pairs} pairs "
        "(a community index is not a duplicate of the page it was named after)",
        f"decided distinct and recorded: {accepted_collision_pairs} pairs "
        "(read once, in name_collision_accepted.json)",
        "merge decisions belong to merge_suggestions_vector_lake and the governance queue, "
        "not to this pass",
    ]

    # Remaining checks (Orphans, Decay, Governance, Alignment)
    for filename in files:
        node_key = filename[:-3]
        # A page the wiki generates about itself has no inbound links by design: nothing links to
        # a cluster index, and the indexer skips the namespace entirely.  This check used to
        # exempt only ``Source_*``, so 799 generated indexes -- 98% of the 812 orphans it reported
        # -- were the report.  Identity comes from the same owner the schema exemption uses.
        entry = parsed.get(filename) or {}
        fm = entry.get("fm") or {}
        if is_generated_artifact(fm, filename, entry.get("body")):
            continue
        if fm.get("topology_status") == "acknowledged-orphan":
            continue
        if inbound_count.get(node_key, 0) == 0 and not filename.startswith("Source_"):
            issues["orphan"].append(f"{filename}: No inbound links (orphan)")

    DEFAULT_TTL = {
        "source": 365,
        "synthesis": 730,
        "vendor": 1095,
        "product": 1095,
        "person": 1095,
        "event": 1095,
        "policy": 1095,
        "standard": 1095,
        "concept": 1825,
    }

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    for filename, data in parsed.items():
        frontmatter = data["fm"]
        updated_str = str(frontmatter.get("updated", ""))
        if not updated_str:
            continue
        try:
            updated_dt = datetime.datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=datetime.timezone.utc)
            age_days = (now_utc - updated_dt).days
        except (ValueError, TypeError):
            continue
            
        if age_days > 0:
            node_type = str(frontmatter.get("type", "concept")).lower().strip()
            ttl = frontmatter.get("ttl")
            if not isinstance(ttl, (int, float)):
                ttl = DEFAULT_TTL.get(node_type, 1095)
            
            if ttl > 0:
                decay_weight = 0.5 ** (age_days / ttl)
                if decay_weight < 0.2:
                    issues["decay"].append(f"{filename}: Severe Knowledge Decay (weight: {decay_weight:.2f}, age: {age_days}d, ttl: {ttl})")

        alignment_score = frontmatter.get("alignment_score")
        if isinstance(alignment_score, (int, float)) and alignment_score < 60:
            node_status = str(frontmatter.get("status", "")).lower()
            if node_status == "active":
                issues["alignment"].append(f"{filename}: Alignment Score {alignment_score} < 60 while status is Active; review for Draft, Superseded, or Deprecated.")

    # 7. Semantic Garbage Collection (Auto-GC)
    archive_dir = os.path.join(wiki_dir, ".archive")
    import shutil
    for filename, data in parsed.items():
        frontmatter = data["fm"]
        node_status = str(frontmatter.get("status", "")).lower()
        node_key = filename[:-3]
        
        updated_str = str(frontmatter.get("updated", ""))
        age_days = 0
        if updated_str:
            try:
                updated_dt = datetime.datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=datetime.timezone.utc)
                age_days = (now_utc - updated_dt).days
            except (ValueError, TypeError):
                pass
                
        is_contested = node_status in ["superseded", "deprecated"]
        has_low_inbound = inbound_count.get(node_key, 0) <= 1
        is_empty = len(data["body"].strip()) < 50 and not filename.startswith("Source_")
        
        if (is_contested and age_days > 30 and has_low_inbound) or (is_empty and age_days > 30 and has_low_inbound):
            issues["semantic_gc"].append(f"{filename}: GC triggered (Status: {node_status}, Age: {age_days}d, Inbound: {inbound_count.get(node_key, 0)})")
            if auto_fix:
                if not os.path.exists(archive_dir):
                    os.makedirs(archive_dir)
                try:
                    archive_path = os.path.join(archive_dir, filename)
                    shutil.copy2(data["path"], archive_path)
                    from vector_lake.mutation_coordinator import execute_mutation_plan
                    execute_mutation_plan(filename, is_delete=True)
                    fixes_applied += 1
                    log.info(f"[Semantic GC] Archived stale node: {filename}")
                except Exception as e:
                    log.error(f"Failed to archive {filename}: {e}")

    governance_store.initialize_meta_store()
    metrics = governance_metrics.compute_debt_metrics()
    unsourced = metrics.get("unsourced_claim_count", 0)
    ambiguous = metrics.get("ambiguous_source_claim_count", 0)

    if unsourced > 0:
        issues["governance"].append(f"Unsourced claims (no source declared on page): {unsourced}")

    if ambiguous > 0:
        governance_summary.append(
            f"managed in governance queue (multi-source pages without block-level anchor): "
            f"{ambiguous} claim(s)"
        )
    if metrics.get("legacy_unsourced_claim_count"):
        # Reported under the same section because it is the other half of the same number, but as
        # a census: the provenance of these pages was never recorded and that was decided, so
        # there is nothing left to do about them and they cannot fail the section.
        governance_summary.append(
            f"accepted as legacy (provenance never recorded, decision recorded): "
            f"{metrics['legacy_unsourced_claim_count']} claim(s) over "
            f"{metrics.get('legacy_accepted_page_count', 0)} page(s) -- "
            f"{metrics.get('legacy_acceptance_ledger', '')}"
        )
    if metrics["stale_claim_count"] > 0:
        issues["governance"].append(f"Stale claims: {metrics['stale_claim_count']}")
    if metrics["pending_change_set_count"] > 0:
        issues["governance"].append(f"Pending change sets: {metrics['pending_change_set_count']}")

    # A merge is durable when the consumed page is gone. An item that says ``resolved`` while both
    # of its pages are still on disk is a resolution that did not change the graph: the WiNEX pair
    # sat that way from 2026-06-24 to 2026-09-25 because every reader trusted the status field.
    # Only items that claim a merge count, so the line is a to-do list rather than a census of
    # every merge-typed record the queue has ever held.
    from vector_lake.governance_service import unapplied_merge_items

    outstanding_merges = unapplied_merge_items()
    if outstanding_merges:
        issues["governance"].append(
            "Resolved merge items whose two pages are both still live (unapplied merge or a "
            f"resolution that predates merge application): {len(outstanding_merges)} -- "
            f"{', '.join(outstanding_merges[:10])}"
        )

    # 17. Source Path Resolution -- reported, and deliberately without a repair branch.
    #
    # Nothing in this pass asked whether a page's declared provenance still names a file: the
    # broken-link check walks ``[[...]]`` targets only, and the one other mention of ``sources``
    # is the frontmatter fill-in above.  It matters because the declaration is what a reader
    # follows back to the raw document, and the raw tree has been reorganised under it more than
    # once (``raw/news/*`` -> ``raw/news/2026Qx/*``, ``raw/DHLS-*`` ->
    # ``raw/DigitalHealthLecturesScout/``).
    #
    # Never repaired here.  The declaration and the SQLite ``sources`` row agree with each other
    # -- ``source_id`` is ``_stable_id("source", raw_ref)``, a hash of the path string -- so
    # rewriting the page alone would leave the file naming one path and the database another for
    # the same source, which is the "one question, two answers" shape this module exists to
    # remove.  Repointing belongs to a provenance migration that moves both sides and the rows
    # referencing them together.
    raw_root = get_raw_dir()
    memory_root = raw_root.parent
    source_path_summary: list[str] = []
    checked_paths = 0
    resolved_paths = 0
    corrupted_sources: list[tuple[str, str]] = []
    unresolved_sources: list[tuple[str, str]] = []
    #: ``sources`` entries that are not ``raw/`` paths.  The field is a provenance declaration, so
    #: an entry that is a page name, a ``[[wiki link]]``, a URL or a bare word declares something
    #: the check below cannot verify -- which is why they used to be skipped in silence.  Measured
    #: 2026-09-28: 65 such entries, 52 of them naming a page that exists and 13 naming nothing at
    #: all.  The first kind still carries sourcing intent and is only mis-shaped; the second is a
    #: claim of provenance that no reader can follow, so only that half is reported.
    non_raw_sources: list[tuple[str, str]] = []
    for filename, data in parsed.items():
        declared = data["fm"].get("sources") or []
        if isinstance(declared, str):
            declared = [declared]
        for entry in declared:
            # An entry that does not start with ``raw/`` names a wiki page or a canonical source
            # name, not a file on disk, so existence is not a question it answers.
            ref = str(entry or "").strip().strip("'\"")
            if not ref.startswith("raw/"):
                non_raw_sources.append((filename, ref))
                continue
            checked_paths += 1
            if "?" in ref or "\ufffd" in ref:
                corrupted_sources.append((filename, ref))
            elif os.path.exists(os.path.join(str(memory_root), *ref.split("/"))):
                resolved_paths += 1
            else:
                unresolved_sources.append((filename, ref))

    for filename, ref in corrupted_sources:
        issues["source_path"].append(f"{filename}: the stored path is corrupted: {ref}")

    if unresolved_sources:
        # One walk of the raw tree, only when something is unresolved: ~2.4k files (0.3 s), which
        # a corpus whose declarations all resolve should not pay for.
        by_basename: dict[str, list[str]] = defaultdict(list)
        for candidate in raw_root.rglob("*"):
            if candidate.is_file():
                by_basename[candidate.name].append(
                    str(candidate.relative_to(memory_root)).replace("\\", "/")
                )
        moves: Counter = Counter()
        for filename, ref in unresolved_sources:
            candidates = by_basename.get(Path(ref).name, [])
            if not candidates:
                issues["source_path"].append(
                    f"{filename}: no file named {Path(ref).name} under raw/: {ref}"
                )
            elif len(candidates) > 1:
                # Naming the basename is not enough to repoint it, and guessing would be worse
                # than saying so: two raw files share a name and only a human knows which.
                issues["source_path"].append(
                    f"{filename}: {len(candidates)} files share that name, cannot resolve: {ref}"
                )
            else:
                issues["source_path"].append(f"{filename}: {ref} -> {candidates[0]} (moved)")
                moves[f"{ref.rsplit('/', 1)[0]} -> {candidates[0].rsplit('/', 1)[0]}"] += 1
        if moves:
            source_path_summary.append(
                f"{sum(moves.values())} unresolved path(s) name a file that exists elsewhere under "
                f"raw/, in {len(moves)} move pattern(s):"
            )
            source_path_summary.extend(
                f"    {count:>4}  {pattern}" for pattern, count in moves.most_common(5)
            )

    if checked_paths:
        # The census sits above the samples the way the collision and metric-evidence summaries
        # do: context for the findings, not one of them.
        source_path_summary.insert(
            0,
            f"checked {checked_paths} declared raw path(s): {resolved_paths} resolve, "
            f"{len(unresolved_sources)} do not, {len(corrupted_sources)} corrupted in the page",
        )

    if non_raw_sources:
        page_keys = {name[:-3] for name in parsed}

        def names_an_existing_page(ref: str) -> bool:
            token = ref.strip()
            if token.startswith("[[") and token.endswith("]]"):
                token = token[2:-2]
            token = token.split("|")[0].strip().replace(".md", "")
            return token in page_keys or token.startswith("raw/")

        unverifiable = [
            (filename, ref) for filename, ref in non_raw_sources if not names_an_existing_page(ref)
        ]
        for filename, ref in unverifiable:
            issues["source_path"].append(
                f"{filename}: sources entry is neither a raw/ path nor a page that exists, "
                f"so the provenance it declares cannot be followed: {ref[:90]}"
            )
        source_path_summary.append(
            f"    {len(non_raw_sources)} sources entr(ies) are not raw/ paths: "
            f"{len(non_raw_sources) - len(unverifiable)} name a page that exists (mis-shaped, "
            f"still followed by a reader), {len(unverifiable)} resolve to nothing"
        )

    check_names = {
        "frontmatter": "1. Frontmatter Completeness",
        "naming": "2. Naming Compliance",
        "type_status": "3. Type/Status Legality",
        "category": "4. Category Vocabulary",
        "duplicate_id": "5. Duplicate IDs",
        "alias_conflict": "6. Alias Conflicts",
        "broken_links": "7. Broken Links",
        "orphan": "8. Orphan Pages",
        "similarity": "9. Name Collisions",
        "decay": "10. Knowledge Decay",
        "semantic_gc": "11. Semantic Garbage Collection",
        "governance": "12. Governance Debt",
        "alignment": "13. Alignment Drift",
        "schema": "14. Strict Schema Verification",
        # Appended rather than inserted beside ``category`` so the numbered sections keep their
        # numbers: reports are read across time, and "9. Name Collisions" turning into "10." would
        # invalidate every earlier reference to it.
        "domain": "15. Domain Vocabulary",
        # Appended for the same reason as ``domain``: the numbered sections are referenced across
        # time, so a new check goes last rather than renumbering the ones an operator already reads.
        "evidence": "16. Metric Evidence",
        # Appended for the same reason as ``domain`` and ``evidence``: the numbered sections are
        # referenced across time, so a new check goes last rather than renumbering the ones an
        # operator already reads.
        "source_path": "17. Source Path Resolution",
    }

    total_issues = sum(len(items) for items in issues.values())
    header = f"Scanned: {len(files)} files | Issues: {total_issues} | Auto-fixed: {fixes_applied}"
    if stubs_refused:
        # A refusal is a closed gate (no purpose contract, unreachable coordinator, a path
        # outside the wiki), so every stub in the pass was refused.  That is not the same as
        # "there was nothing to write", and a report that showed only "Auto-fixed: 0" would
        # read as the second.
        header += f" | Stub writes refused: {stubs_refused} (see the log)"
    lines = ["=== Vector Lake Lint Report ===", header, ""]
    for key, name in check_names.items():
        items = issues[key]
        lines.append(f"{name}: {'[PASS]' if not items else f'[FAIL: {len(items)}]'}")
        # A pair list is not a finding list: ten samples out of thousands is what made this number
        # unreadable, so name collisions report how many distinct families there are and hand the
        # merge decision to its owner.  Every other check keeps the flat sample list it had.
        if key == "similarity":
            lines.extend(f"    {line}" for line in collision_summary)
            sample_limit = 5
        elif key == "evidence":
            # Same shape as the collision summary: the census is context for the findings, not
            # one of them, so it must not turn a clean section into a FAIL.
            lines.extend(f"    {line}" for line in metric_evidence_summary)
            sample_limit = 10
        elif key == "source_path":
            # Same shape again: how many declarations resolve, and which moves explain the rest.
            lines.extend(f"    {line}" for line in source_path_summary)
            sample_limit = 5
        elif key == "governance":
            # The decided half of the unsupported-claim debt is context for the findings above it.
            lines.extend(f"    {line}" for line in governance_summary)
            sample_limit = 10
        else:
            sample_limit = 10
        for item in items[:sample_limit]:
            lines.append(f"    {item}")
        if len(items) > sample_limit:
            lines.append(f"    ... and {len(items) - sample_limit} more")
        lines.append("")
    return "\n".join(lines)
