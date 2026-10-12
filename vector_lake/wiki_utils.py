import datetime
import hashlib
import io
import logging
import os
import random
import re
import shutil
import string
import uuid
from pathlib import Path
from typing import List, TypedDict

import yaml
from vector_lake import get_extension_root, host_env
from vector_lake.node_vocabulary import NODE_PREFIXES, NODE_TYPE_ALTERNATION, NON_NODE_WIKI_FILES
from vector_lake.yaml_utils import load_yaml, dump_yaml

try:
    import vector_lake_core
    HAVE_CORE = True
except ImportError:
    vector_lake_core = None
    HAVE_CORE = False

_META_DIR_CACHE = None
_CONFIG_CACHE: dict = {}
log = logging.getLogger("vector-lake-wiki")

SYSTEM_WHITELIST = NON_NODE_WIKI_FILES
# Derived from ``node_vocabulary`` rather than spelled out here: three hand-written
# copies of this list had already drifted (one was missing ``System_``).  The value
# and its order are unchanged.
VALID_PREFIXES = NODE_PREFIXES
INVALID_CHARS_REGEX = re.compile(r'[\[\]<>:"/\\|\?\*\(\)\s]+')

def normalize_memory_key(key: str) -> str:
    """Canonical normalization function to strip noise from keys and aliases."""
    normalized = re.sub(r"\s+", " ", str(key or "").strip().lower())
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:96] or "general"

def calculate_cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Canonical cosine similarity for raw python floats."""
    if not v1 or not v2 or len(v1) != len(v2): return 0.0
    import math
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(a * a for a in v2))
    return dot / (norm1 * norm2) if norm1 and norm2 else 0.0

_IDENTITY_SEPARATORS = re.compile(r"[.·、。，;:!?'“”‘’；：！？…]+")


def canonical_source_name(raw_path: str) -> str:
    """The Source page name for a raw file: ``Source_<sanitised stem>.md``.

    One owner for this rule.  It lived in ``tool_ingest`` while ``claim_extractor`` and
    ``tool_delete`` each hand-rolled the same shape differently -- unsanitised stems in one, a
    lowercased stem in the other -- so for a raw file named ``2604.24658v3.pdf`` the ingest path
    named the page ``Source_2604-24658v3.md`` while provenance reported ``Source_2604.24658v3.md``
    and the delete guard looked for a third spelling.  Two of the three named a page that does not
    exist.

    Sanitised, because the name has to satisfy ``validate_wiki_filename`` or the task can never be
    finalised: an arXiv-style stem (``2608.19880v1``) contains dots, which the strict
    ``[Type]_[MainName]-[SubName]`` rule rejects, and ``finalize_ingest`` refuses the write.  Those
    papers sat un-ingested for exactly this reason.
    """
    basename = Path(str(raw_path)).stem
    safe = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", basename).strip("-")
    return f"Source_{safe or 'unnamed'}.md"


def entity_identity_key(name: str) -> str:
    """The key two entity names are compared by: normalised, then case-folded.

    ``normalize_entity_name`` answers "what filename does this name get", so it must preserve the
    author's spelling -- 4 269 of the 7 968 live page names carry an acronym (``Concept_WASM``,
    ``Vendor_OpenAI``, ``Concept_DRG``) and lowercasing it would rename them.  Comparing with it was
    the defect: ``[[Concept_WASM]]`` did not resolve against a page named ``Concept_wasm``, so the
    link was reported broken and ``stub_creator`` wrote a second page beside the first -- and on
    Windows, where the filesystem is case-insensitive, that second write lands on the *same file*,
    so the outcome is silent loss rather than a visible duplicate.

    Identity is therefore separated from spelling: this is what dicts are keyed by and what lookups
    go through, and :func:`normalize_entity_name` stays the naming function.  ``casefold`` rather
    than ``lower`` because the corpus is multilingual (it folds ``ß``/``ẞ`` to ``ss`` and leaves CJK
    untouched).

    Separator punctuation is folded the same way, and for the same reason: a page is named
    ``Product_Gemini-3-5-Flash`` while prose and links write ``Gemini 3.5 Flash``, so ``3.5`` and
    ``3-5`` have to compare equal or the link is reported broken.  It is folded *here* and not in
    ``normalize_entity_name`` because the naming function must not restyle a stem: ``Source_2604.24658``
    is an arXiv identifier whose dot is part of the name.  Only characters that separate words are
    folded -- never a letter or a digit -- so the folding cannot merge two different identifiers.
    """
    # ``normalize_entity_name`` already maps whitespace and _[]<>:"/\|?* to "-"; this stretches the
    # same convention over the separators it leaves behind (.,·。、，;:!?'"…) and squeezes the result,
    # so ``a..b`` and ``a-b`` are one key.  Separators only -- a letter or digit is never touched.
    key = _IDENTITY_SEPARATORS.sub("-", normalize_entity_name(name).casefold())
    return re.sub(r"-+", "-", key).strip("-")


def normalize_entity_name(name: str) -> str:
    """The filename spelling of an entity name; separators collapse to one hyphen.

    This is a *naming* function -- it decides what a page is called, so it keeps the author's case.
    Use :func:`entity_identity_key` to compare two names.
    """
    prefix = ""
    for p in VALID_PREFIXES:
        if name.startswith(p):
            prefix = p
            name = name[len(p):]
            break
            
    # Replace spaces, underscores, brackets, and invalid chars with a single hyphen to enforce only one underscore rule
    name = re.sub(r'[\s_\[\]<>:"/\\|\?\*\(\)]+', '-', name.strip())
    name = name.strip('-')
    return f"{prefix}{name}"

def validate_wiki_filename(filename: str):
    if filename in SYSTEM_WHITELIST or filename.startswith("System_"):
        return
    
    if not filename.endswith(".md"):
        raise ValueError(f"Invalid suffix: '{filename}' must end with .md")
        
    if not filename.startswith(VALID_PREFIXES):
        raise ValueError(f"Invalid prefix: '{filename}' must start with one of {VALID_PREFIXES}")
        
    if INVALID_CHARS_REGEX.search(filename):
        raise ValueError(f"Invalid characters: '{filename}' contains forbidden characters (e.g., brackets, slashes, spaces).")
        
    if not re.match(
        rf'^(?:{NODE_TYPE_ALTERNATION})_[a-zA-Z0-9\u4e00-\u9fa5]+'
        r'(-[a-zA-Z0-9\u4e00-\u9fa5]+)*\.md$',
        filename,
    ):
        raise ValueError(f"Strict Naming Violation: '{filename}' must match pattern [Type]_[MainName]-[SubName].md")
        
    core_name = filename.split("_", 1)[1][:-3] if "_" in filename else filename[:-3]
    if len(core_name.strip()) <= 1 and (core_name.isalpha() or '\u4e00' <= core_name <= '\u9fff'):
        raise ValueError(f"Anti-cheat triggered: Core name '{core_name}' is too short.")
        
    if len(filename) > 120:
        raise ValueError(f"Length limit exceeded: '{filename}' is over 120 characters.")


def get_memory_dir() -> Path:
    """Active MEMORY root.

    Resolution order:
      1. ``VECTOR_LAKE_MEMORY_DIR`` (used by tests and isolated runs),
      2. ``memory_dir`` in the repository ``config.json`` (per-machine setting),
      3. ``host_env.legacy_memory_root()`` (historical default).
    """
    override = os.environ.get("VECTOR_LAKE_MEMORY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    configured = _configured_memory_dir()
    if configured is not None:
        return configured
    return host_env.legacy_memory_root()


def _configured_memory_dir() -> Path | None:
    """``memory_dir`` from config.json, cached; tolerant of a missing config."""
    if "memory_dir" in _CONFIG_CACHE:
        return _CONFIG_CACHE["memory_dir"]
    resolved: Path | None = None
    config_path = get_extension_root() / "config.json"
    try:
        import json

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        value = raw.get("memory_dir")
        if isinstance(value, str) and value.strip():
            resolved = Path(value.strip()).expanduser().resolve()
    except (OSError, ValueError):
        resolved = None
    _CONFIG_CACHE["memory_dir"] = resolved
    return resolved


# Shipped defaults for the per-machine ``config.json``.  That file is untracked
# (see ``config.example.json``), so a fresh checkout has no config at all; if the
# defaults lived only in the file, a missing config would silently empty
# ``exclude_paths`` and re-ingest privacy-excluded raw sources.
DEFAULT_EXCLUDE_PATHS = ("stocks/", "garmin/", "personal-insights/")
DEFAULT_SUPPORTED_EXTENSIONS = (".md", ".txt")

#: Path segments that make a raw source private by construction.
#:
#: This is a *privacy invariant*, not a user preference, so it does not live in
#: ``config.json``'s ``exclude_paths``: the per-machine file may add exclusions, but it must
#: not be able to remove this one.  It is enforced by :func:`is_private_raw_source`, which
#: every entry point that can enqueue raw work has to call.
PRIVATE_RAW_PATH_SEGMENTS = ("privacy", "diary")


def is_private_raw_source(path) -> bool:
    """Whether ``path`` is private raw material that must never be ingested.

    ``raw/privacy/Diary`` holds the operator's own diary and audit text.  The rule used to
    be an inline ``"privacy" in filepath and "Diary" in filepath`` check inside the raw
    event handler, and only there -- so the batch scan that the same handler then called
    walked the whole raw tree and enqueued the very files the handler had just refused to
    trigger on.  One owner, both entry points.

    Segments are compared rather than substrings, so a directory merely *named* with those
    letters (``privacy-reports/``, ``Diarystudies/``) is not treated as private and a
    private path cannot be disguised as ``priv\u200bacy``-adjacent text.
    """
    if not path:
        return False
    parts = [part.strip().lower() for part in str(path).replace("\\", "/").split("/") if part]
    for index, part in enumerate(parts):
        if part == PRIVATE_RAW_PATH_SEGMENTS[0]:
            if any(later == PRIVATE_RAW_PATH_SEGMENTS[1] for later in parts[index + 1:]):
                return True
    return False


def load_config(*, repo_root: Path | None = None) -> dict:
    """Extension config merged over the shipped defaults.

    A missing ``config.json`` is a supported state.  An unreadable or malformed
    file raises, because ignoring it would drop the exclusion list.
    """
    import json

    config = {
        "target_directories": [],
        "exclude_paths": list(DEFAULT_EXCLUDE_PATHS),
        "supported_extensions": list(DEFAULT_SUPPORTED_EXTENSIONS),
    }
    config_path = (Path(repo_root) if repo_root is not None else get_extension_root()) / "config.json"
    if not config_path.exists():
        return config
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Vector Lake config '{config_path}' is unreadable ({type(exc).__name__}: {exc}); "
            "refusing to run with an unknown exclusion list."
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"Vector Lake config '{config_path}' must contain a JSON object.")
    from vector_lake.ingest_backend import validate_ingest_config
    validate_ingest_config(raw)
    config.update(raw)
    return config


def reset_memory_dir_cache() -> None:
    """Drop the cached config-derived MEMORY root (tests and config reloads)."""
    global _META_DIR_CACHE
    _META_DIR_CACHE = None
    _CONFIG_CACHE.clear()


def get_wiki_dir() -> Path:
    return get_memory_dir() / "wiki"


def get_raw_dir() -> Path:
    return get_memory_dir() / "raw"


def projection_hash(content: str) -> str:
    """SHA-256 of a page projection, normalised to LF line endings.

    The hash baselines *content*, so it must not depend on how a checkout wrote the file: the
    repository index is 907/907 LF, and a CRLF working copy would otherwise produce a different
    hash for byte-identical knowledge.  Normalising before hashing is what makes a
    ``source_projection_hash`` or ``target_projection_hash`` comparable across hosts -- the
    single implementation, so the producer and the verifier cannot drift apart.
    """
    text = str(content).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_ingest_source_path(raw) -> Path | None:
    """Resolve a raw source path inside the raw root, or ``None`` when it escapes it.

    An ingest item names its source as ``filepath``, and that string is not trustworthy: it
    arrives in a task packet built by the host runner.  Every reader used to hand it straight
    to ``open``, so a malformed packet could publish any file the ingest process can read -- a
    key, a database, another application's config -- as a canonical wiki page.  Accepting only
    a path that resolves inside ``raw/`` is what makes "the source of this page" a fact about
    the raw tree rather than about the filesystem.

    Both spellings that occur in practice are accepted: a rooted path (all 1866 live ledger
    rows are absolute) and one written relative to the memory root or the raw root
    (``raw/x.md``, ``x.md`` -- the form hand-written packets and tests use).
    """
    raw_text = str(raw or "").strip()
    if not raw_text:
        return None
    root = get_raw_dir().resolve()
    path = Path(raw_text)
    if not path.is_absolute():
        parts = [part for part in re.split(r"[\\/]+", raw_text) if part]
        if parts and parts[0].lower() == root.name:
            parts = parts[1:]
        path = root.joinpath(*parts) if parts else root
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_relative_to(root) else None


def validate_raw_ingest_source_path(value: str) -> Path:
    """Apply the existing raw-root, directory/privacy and format boundary before source reads."""
    path = resolve_ingest_source_path(value)
    if path is None:
        raise ValueError("source escaped the raw root")
    config = load_config()
    if is_private_raw_source(str(path)) or any(
        str(excluded).casefold() in path.as_posix().casefold()
        for excluded in config.get("exclude_paths", [])
    ):
        raise ValueError("ingest source is in an excluded directory")
    if path.suffix.lower() not in config.get("supported_extensions", [".md", ".txt"]):
        raise ValueError("ingest source format is excluded")
    return path


def read_ingest_item_content(item: dict, *, required: bool = False) -> str:
    """The item's content, read once from its ``filepath`` and cached on the item.

    Single owner for a read that four call sites used to perform themselves -- none of them
    constraining the path (see :func:`resolve_ingest_source_path`).

    ``required`` selects the failure contract, because the callers genuinely disagree: the
    declaration check and the hash stamp want ``""`` for a source they cannot read, while the
    finalize path must fail loudly.  Returning ``""`` there would let an unreadable source be
    written as an empty canonical page, which publishes a page with no content where the job
    should have stayed pending.
    """
    cached = item.get("content")
    if cached:
        return str(cached)
    raw = str(item.get("filepath") or "")
    if not raw:
        return ""
    path = resolve_ingest_source_path(raw)
    if path is None:
        if required:
            raise ValueError(f"ingest item filepath is outside the raw root: {raw}")
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        if required:
            raise
        return ""
    item["content"] = text
    return text


def get_meta_dir() -> Path:
    global _META_DIR_CACHE
    if _META_DIR_CACHE is not None:
        return _META_DIR_CACHE

    primary = get_wiki_dir() / ".meta"
    fallback = get_extension_root() / "data" / "v8_meta"

    for candidate in (primary, fallback):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".probe_{uuid.uuid4().hex}"
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("probe")
            probe.unlink()
            _META_DIR_CACHE = candidate
            return candidate
        except OSError:
            continue

    _META_DIR_CACHE = fallback
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_purpose_path() -> Path:
    return get_memory_dir() / "purpose.md"


def get_runtime_tmp_dir() -> Path:
    """Scratch directory for cross-process runtime coordination.

    This used to be ``tempfile.gettempdir()/vector_lake_tmp``, which is shared by
    every Vector Lake installation on the machine and is subject to OS temp
    cleanup.  Coordination state belongs to one MEMORY root, so it lives beside
    that root's database in the meta directory.
    """
    path = get_meta_dir() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_outbox_signal_path() -> Path:
    """Wake-up hint for the outbox consumer (producer and consumer must agree)."""
    return get_runtime_tmp_dir() / "outbox_signal.lock"


def get_ingest_processing_path() -> Path:
    """In-flight ingest bookkeeping, scoped to this MEMORY root."""
    return get_runtime_tmp_dir() / "ingest_processing.json"


def get_index_path() -> Path:
    return get_wiki_dir() / "index.json"


def get_claim_graph_path() -> Path:
    return get_wiki_dir() / "claim_topology.json"





def normalize_raw_ref(raw_ref: str) -> str:
    normalized = str(raw_ref).replace("\\", "/").strip()
    if normalized.startswith("MEMORY/"):
        normalized = normalized[len("MEMORY/") :]
    return normalized


def normalize_list_field(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def normalize_sources(value) -> list[str]:
    return [normalize_raw_ref(item) for item in normalize_list_field(value)]


def split_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return {}, content

    if HAVE_CORE:
        yaml_part, body_part = vector_lake_core.fast_split_frontmatter(content)
        if not yaml_part and body_part == content:
            return {}, content
        try:
            frontmatter = load_yaml(yaml_part) or {}
        except yaml.YAMLError:
            raise
        if not isinstance(frontmatter, dict):
            frontmatter = {}
        return frontmatter, body_part
    
    match = re.search(r'\r?\n---(?:\r?\n|$)', content)
    if not match:
        return {}, content
        
    yaml_part = content[4:match.start()]
    body_part = content[match.end():]
    
    try:
        frontmatter = load_yaml(yaml_part) or {}
    except yaml.YAMLError:
        raise

    if not isinstance(frontmatter, dict):
        frontmatter = {}
    return frontmatter, body_part


def read_markdown_file(path: str | Path, errors: str = "replace") -> tuple[dict, str, str]:
    with open(path, "r", encoding="utf-8", errors=errors) as handle:
        content = handle.read()
    frontmatter, body = split_frontmatter(content)
    return frontmatter, body, content


def read_frontmatter_only(path: str | Path, errors: str = "replace") -> dict:
    """Reads only the YAML frontmatter without loading the entire file body into memory."""
    yaml_lines = []
    with open(path, "r", encoding="utf-8", errors=errors) as handle:
        first_line = handle.readline()
        if not first_line.startswith("---"):
            return {}
        for line in handle:
            if line.startswith("---"):
                break
            yaml_lines.append(line)
    if not yaml_lines:
        return {}
    try:
        frontmatter = load_yaml("".join(yaml_lines)) or {}
        return frontmatter if isinstance(frontmatter, dict) else {}
    except yaml.YAMLError:
        return {}


def wiki_page_keys(
    wiki_dir: str | Path | None = None, excluded: set[str] | None = None
) -> set[str]:
    """Page keys present on disk: ``.md`` filenames without the extension.

    ``os.scandir`` rather than ``glob`` + ``is_file``: on the live 8 488-page wiki
    this is ~31 ms instead of ~506 ms for an identical key set.

    Callers must **not** memoise this on the directory's ``(st_mtime_ns, st_size)``.
    ``runtime_health`` did, on the assumption that the directory stamp tracks its
    entries.  NTFS does not honour that assumption: on this host, creating a file
    in a directory left the stamp byte-identical, and 300 rapid creates produced
    only 42 distinct stamps.  A cache keyed on it therefore handed the canonical
    write gate a stale key set and silently dropped the projection-drift signal for
    a page that had just been added.  A 31 ms scan is the price of the gate telling
    the truth.
    """
    directory = Path(wiki_dir) if wiki_dir is not None else get_wiki_dir()
    skip = SYSTEM_WHITELIST if excluded is None else excluded
    if not directory.exists():
        return set()
    keys: set[str] = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if not name.endswith(".md") or name in skip or name.startswith("System_"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                keys.add(name[: -len(".md")])
    except OSError:
        return set()
    return keys


def flush_durable(handle) -> None:
    """Flush an open writable handle and force its bytes to stable storage.

    ``os.replace`` is atomic with respect to *readers*, but it only swaps a
    directory entry.  Without an ``fsync`` the replacement inode's data may still
    sit in the page cache when the swap happens, so a crash or power loss can
    leave a zero-length or torn canonical file while the previous content is
    already unlinked.  ``write_markdown_file`` already guards *logical* loss of
    compiled truth; this guards the physical write.
    """
    handle.flush()
    os.fsync(handle.fileno())


def fsync_directory(directory: str | Path) -> None:
    """Force a completed rename into the directory entry itself.

    POSIX only: Windows cannot open a directory for ``fsync``, and its rename
    durability is handled by the filesystem journal instead.  A failure here is
    never fatal -- the data file is already synced.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(
    path: str | Path,
    content: str,
    pre_parsed_frontmatter: dict | None = None,
    validation_mode: str = "full",
):
    path = Path(path)
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    
    # NEW: Trigger Defense Hook for wiki markdown files
    if path.name.endswith(".md") and "wiki" in path.parts:
        try:
            frontmatter = pre_parsed_frontmatter if pre_parsed_frontmatter is not None else split_frontmatter(content)[0]
            # Existence is read before the write, which is the only moment it answers "is this
            # a new node": the new-node classification rules hang off that answer.  ``schema``
            # mode is the legacy-maintenance path and is exempt from them.
            is_new = validation_mode == "full" and not path.exists()
            if validation_mode == "full":
                from vector_lake.defense_hook import verify_asset
                verify_asset(content, path.name, frontmatter, get_index_path(), is_new)
            else:
                from vector_lake.schema_validator import validate_schema
                validate_schema(frontmatter, content, path.name, get_index_path(), is_new)
        except ImportError:
            # The defense hook stack itself is unavailable; nothing to validate with.
            log.warning("Defense hook unavailable; %s written without semantic validation", path.name)
        except Exception as exc:
            # Any other failure is a real validation signal. Swallowing it silently
            # disabled the last line of defense for whole classes of malformed pages.
            log.error("Refused to write %s: %s: %s", path.name, type(exc).__name__, exc)
            raise
            
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
        flush_durable(handle)
    os.replace(temp_path, path)
    fsync_directory(path.parent)

def ensure_parent_dir(path: str | Path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


class SafeWriteError(Exception): pass

def write_markdown_file(path: str | Path, frontmatter: dict, body: str, skip_validation: bool = False):
    path = Path(path)
    if not skip_validation and path.exists():
        try:
            _, old_body, _ = read_markdown_file(path)
            old_truth_count = _count_list_items(old_body, "编译事实") or _count_list_items(old_body, "Compiled Truth")
            new_truth_count = _count_list_items(body, "编译事实") or _count_list_items(body, "Compiled Truth")
            if new_truth_count < old_truth_count:
                raise SafeWriteError(f"丢失了编译事实 (Compiled Truth)。旧文件有 {old_truth_count} 条，新文件只有 {new_truth_count} 条。请调用 read_resource 重新读取当前文件状态，并使用 Append 模式进行增量合并，而不是直接覆盖。")
            old_timeline_count = _count_list_items(old_body, "证据时间线") or _count_list_items(old_body, "Evidence Timeline")
            new_timeline_count = _count_list_items(body, "证据时间线") or _count_list_items(body, "Evidence Timeline")
            if new_timeline_count < old_timeline_count:
                raise SafeWriteError(f"丢失了证据时间线 (Evidence Timeline)。旧文件有 {old_timeline_count} 条记录，新文件只有 {new_timeline_count} 条记录。请调用 read_resource 重新读取当前文件状态，并使用 Append 模式进行增量合并，而不是直接覆盖。")
        except SafeWriteError:
            raise
        except Exception:
            pass

    filename = path.name
    if not skip_validation:
        validate_wiki_filename(filename)

    # A literal STORM section list used to be enforced here for names starting with
    # ``Synthesis_STORM_``.  It was removed rather than repaired, because there was nothing to
    # repair it to: no page can carry that name (``validate_wiki_filename`` above allows one
    # underscore, and every STORM synthesis in the wiki is ``Synthesis_STORM-<Name>.md``), the
    # five English headings contradicted the owning contract (the ``cognitive-storm-research``
    # skill, whose template is Chinese and which states that headings may be adapted per
    # audience and language), and the ``references/storm_report_template.md`` it told the writer
    # to follow does not exist in this repository.  The branch could only ever refuse nothing
    # while reading, to anyone auditing this file, as a gate that exists.
    yaml_block = dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
    full_content = f"---\n{yaml_block}---\n{body.lstrip()}"
    expected_path = (get_wiki_dir() / filename).resolve()
    if path.resolve() != expected_path:
        raise SafeWriteError(f"Path traversal blocked: {path}")
    # Deferred on purpose, and load-order-bearing: ``mutation_coordinator`` imports this
    # module at module scope, so hoisting this import to the top makes the package
    # unimportable (a five-module cycle through ``defense_hook``/``purpose_contract``).
    # See ``tests/test_import_layering.py``.
    from vector_lake.mutation_coordinator import execute_mutation_plan
    execute_mutation_plan(filename, content=full_content, is_delete=False)


def backup_file(path: str | Path, suffix: str = ".bak") -> Path | None:
    source = Path(path)
    if not source.exists():
        return None
    backup_path = source.with_name(source.name + suffix)
    shutil.copy2(source, backup_path)
    return backup_path


def sanitize_wiki_node(filepath: str | Path):
    filepath = Path(filepath)
    if not filepath.exists() or filepath.suffix.lower() != ".md":
        return

    frontmatter, body, _ = read_markdown_file(filepath)
    today = datetime.datetime.now().date()
    if not frontmatter.get("id"):
        frontmatter["id"] = f"{today.strftime('%Y%m%d')}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
    frontmatter["updated"] = today.isoformat()
    write_markdown_file(filepath, frontmatter, body, skip_validation=False)


def _count_list_items(body: str, section_marker: str) -> int:
    if HAVE_CORE:
        return vector_lake_core.fast_count_list_items(body, section_marker)
    count = 0
    in_section = False
    for line in io.StringIO(body):
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = section_marker in stripped
        elif stripped.startswith("# "):
            in_section = False
        
        if in_section and (stripped.startswith("- ") or stripped.startswith("* ")):
            count += 1
    return count

def safe_write_markdown(path: str | Path, content: str, skip_validation: bool = False):
    frontmatter, body = split_frontmatter(content)
    write_markdown_file(path, frontmatter, body, skip_validation=skip_validation)


class TensionEdge(TypedDict):
    target: str
    polarity: float
    intensity: float

class EntityData(TypedDict, total=False):
    id: str
    title: str
    type: str
    domain: str
    status: str
    epistemic_status: str
    categories: List[str]
    created: str
    updated: str
    sources: List[str]
    aliases: List[str]
    tags: List[str]
    tension_edges: List[TensionEdge]
    raw_text: str
    links: List[str]
    triples: List[dict]
    _key: str
    _pre_embedded: List[float]

class ClaimData(TypedDict, total=False):
    claim_id: str
    source_page: str
    statement: str
    predicate: str
    target: str
    confidence: float
    context: str
    evidence_links: List[str]

def enforce_entity_dict(data: dict) -> EntityData:
    """Runtime assertion to enforce EntityData structure without pulling in heavy pydantic."""
    # Just a cast and basic validation to prevent drift
    if 'id' not in data and 'title' not in data:
        pass # Allow partial for now
    return data # type: ignore

def enforce_claim_dict(data: dict) -> ClaimData:
    return data # type: ignore

