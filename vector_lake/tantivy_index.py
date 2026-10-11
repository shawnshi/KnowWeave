"""tantivy-backed FTS projection, switch-gated behind ``VECTOR_LAKE_FTS``.

Why this exists: the lexical half of retrieval was SQLite's FTS5 for candidate membership plus a
separate BM25 reranker for the pool signal.  tantivy is a Rust BM25 engine with a Python binding
that this project's dependency set can carry
(``tantivy==0.26.2`` has a cp313/win_amd64 wheel, verified 2026-09-25), so one engine can serve
both.

The switch is **default ``fts5``** on purpose.  Candidate ordering inside the returned pool is
relevance-affecting, and this repository's rule for retrieval changes is one switch at a time,
measured against the pre-registered eval harness, whose records were retired from the repository
with ``benchmarks/``).
Nothing here changes behaviour until an operator sets ``VECTOR_LAKE_FTS=tantivy``.

Contract preserved from the FTS5 path, because callers depend on each part:

* input text is **already tokenized** by the project tokenizer (jieba-rs) and joined by spaces,
  so the analyzer here is whitespace + lowercase -- re-tokenizing would be a second segmenter;
* terms are **ANDed**, and each term matches across ``title``/``summary``/``text`` -- FTS5's
  implicit AND over its three columns;
* ``rank`` keeps FTS5's sign convention: **negative** (``bm25()`` in SQLite is negative), best
  first when sorted ascending -- ``tool_search`` negates it (``raw_score * -1.0``);
* ``node_key`` is a fast field so exact-key lookups and per-candidate scoring stay cheap.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

log = logging.getLogger("vector-lake-tantivy")

INDEX_DIRNAME = "tantivy_index"
#: Bumped when the schema or analyzer changes.  A stored value that differs forces a rebuild
#: rather than querying an index whose fields no longer mean what the code assumes.
SCHEMA_VERSION = 1
#: The field the pool rerank signal needs in addition to FTS5's three columns.
SEARCH_FIELDS = ("title", "summary", "text")
ANALYZER = "vl_pretokenized"

_index = None
_searcher = None
_index_identity = None
_WRITE_LOCK = threading.RLock()


def enabled() -> bool:
    """Whether reads and writes should go to tantivy instead of FTS5.

    A missing wheel falls back to FTS5 with a warning rather than failing a page read: the switch
    states an intent, and the projection it would replace is still there and still authoritative.
    """
    if str(os.environ.get("VECTOR_LAKE_FTS", "fts5")).strip().lower() != "tantivy":
        return False
    try:
        import tantivy  # noqa: F401
    except ImportError:
        log.warning(
            "VECTOR_LAKE_FTS=tantivy but the tantivy wheel is not installed; using FTS5"
        )
        return False
    return True


def index_dir() -> Path:
    from vector_lake.wiki_utils import get_meta_dir

    return get_meta_dir() / INDEX_DIRNAME


def _version_file() -> Path:
    return index_dir() / "vl_schema_version"


def reset() -> None:
    """Forget the cached handles; the next call re-opens (used by tests and after a rebuild)."""
    with _process_ownership():
        _forget_index()


def _forget_index():
    global _index, _searcher, _index_identity
    _index = _searcher = _index_identity = None


def _incarnation_file():
    return index_dir() / "vl_incarnation"


def _new_incarnation():
    stat = index_dir().stat()
    return f"{uuid.uuid4().hex}:{stat.st_dev:x}:{stat.st_ino:x}"


def _disk_identity():
    """Commit metadata changes, but directory identity/schema/incarnation must not."""
    directory = index_dir()
    try:
        stat = directory.stat()
        version = _version_file().read_text(encoding="utf-8").strip()
        incarnation = _incarnation_file().read_text(encoding="utf-8").strip()
        schema = json.loads((directory / "meta.json").read_text(encoding="utf-8"))["schema"]
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
    expected = []
    for name in ("node_key", "title", "summary", "aliases", "text"):
        expected.append({"name": name, "type": "text", "options": {
            "indexing": {"record": "position", "fieldnorms": True,
                         "tokenizer": "raw" if name == "node_key" else ANALYZER},
            "stored": True, "fast": name == "node_key"}})
    parts = incarnation.split(":")
    if (version != str(SCHEMA_VERSION) or schema != expected or len(incarnation) > 128
            or len(parts) != 3 or parts[1:] != [f"{stat.st_dev:x}", f"{stat.st_ino:x}"]):
        return None
    try:
        uuid.UUID(hex=parts[0])
    except ValueError:
        return None
    return (str(directory.resolve()), stat.st_dev, stat.st_ino, version, incarnation)


def _analyzer(tantivy):
    return (
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.whitespace())
        .filter(tantivy.Filter.lowercase())
        .build()
    )


def _schema(tantivy):
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("node_key", stored=True, tokenizer_name="raw", fast=True)
    for field in ("title", "summary", "aliases", "text"):
        builder.add_text_field(field, stored=True, tokenizer_name=ANALYZER)
    return builder.build()


@contextmanager
def _process_ownership():
    if not _WRITE_LOCK.acquire(timeout=3):
        raise TimeoutError("timed out waiting for this process's tantivy writer")
    try:
        yield
    finally:
        _WRITE_LOCK.release()


def _index_lock():
    directory = index_dir().parent
    directory.mkdir(parents=True, exist_ok=True)
    return FileLock(str(directory / "tantivy-writer.lock"), timeout=3)


def open_index(create: bool = True):
    """Open (or create) the index, registering the analyzer.  Cached per process."""
    identity = _disk_identity()
    if _index is not None and identity is not None and identity == _index_identity:
        return _index
    with _process_ownership(), _index_lock():
        return _open_index_unlocked(create)


def _open_index_unlocked(create: bool = True):
    global _index, _index_identity
    identity = _disk_identity()
    if _index is not None and identity is not None and identity == _index_identity:
        return _index
    _forget_index()
    import tantivy

    directory = index_dir()
    if identity is None:
        if not create:
            return None
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)
        index = tantivy.Index(_schema(tantivy), path=str(directory))
        _version_file().write_text(str(SCHEMA_VERSION), encoding="utf-8")
        _incarnation_file().write_text(_new_incarnation(), encoding="utf-8")
    else:
        index = tantivy.Index.open(str(directory))
    if index.schema != _schema(tantivy):
        raise RuntimeError("Native Tantivy schema differs from the declared schema")
    index.register_tokenizer(ANALYZER, _analyzer(tantivy))
    _index = index
    _index_identity = _disk_identity()
    return index


@contextmanager
def _write_session(heap_size: int = 128 * 1024 * 1024):
    """Own a native writer only for one mutation; acquisition faults stay explicit."""
    with _process_ownership(), _index_lock(), _owned_writer(heap_size=heap_size) as writer:
        yield writer


@contextmanager
def _owned_writer(heap_size: int = 128 * 1024 * 1024):
    """Caller already owns process AND file lock; never reacquire a fresh FileLock."""
    index = _open_index_unlocked()
    if index is None:
        raise RuntimeError("Tantivy writer requires an index")
    writer = index.writer(heap_size=heap_size)
    try:
        yield writer
    except BaseException:
        # A partially consumed input must not survive to a later caller's commit.
        try:
            writer.rollback()
        except Exception:
            log.exception("tantivy rollback failed while handling a mutation error")
        try:
            writer.wait_merging_threads()
        except Exception:
            log.exception("tantivy writer release failed while handling a mutation error")
        raise
    else:
        # The binding consumes the writer here, releasing its cross-process lock.
        writer.wait_merging_threads()


def _refresh_searcher():
    global _searcher
    index = open_index()
    if index is None:
        raise RuntimeError("Tantivy reader requires an index")
    index.reload()
    _searcher = index.searcher()
    return _searcher


def _document(tantivy, node_key, title, summary, text, aliases=""):
    return tantivy.Document(
        node_key=node_key,
        title=title or "",
        summary=summary or "",
        text=text or "",
        aliases=aliases or "",
    )


def upsert(
    node_key: str, title: str, summary: str, text: str, aliases: str = ""
) -> None:
    """Replace one node's document and commit."""
    upsert_many([(node_key, title, summary, text, aliases)])


def upsert_many(rows) -> int:
    """Replace many documents with a single commit (the index-rebuild path)."""
    import tantivy

    with _write_session() as writer:
        count = 0
        for row in rows:
            node_key = row[0]
            writer.delete_documents_by_term("node_key", node_key)
            writer.add_document(
                _document(
                    tantivy,
                    node_key,
                    row[1],
                    row[2],
                    row[3],
                    row[4] if len(row) > 4 else "",
                )
            )
            count += 1
        if count:
            writer.commit()
        return count


def delete_node(node_key: str) -> None:
    with _write_session() as writer:
        writer.delete_documents_by_term("node_key", node_key)
        writer.commit()


def clear() -> None:
    with _write_session() as writer:
        writer.delete_all_documents()
        writer.commit()


def search_keys() -> set[str]:
    """Every node_key materialised in this index."""
    searcher = _refresh_searcher()
    keys = set()
    index = open_index()
    if index is None:
        raise RuntimeError("Tantivy key scan requires an index")
    for address in searcher.search(index.parse_query("*"), 100_000).hits:
        keys.add(searcher.doc(address[1])["node_key"][0])
    return keys


def search(terms: list[str], limit: int = 50, fields: tuple[str, ...] = SEARCH_FIELDS):
    """``[(node_key, rank)]`` with FTS5's sign convention: a term must match AND that term in at
    least one of ``fields``, and ``rank`` is **minussed** BM25 so ascending order is best-first."""
    if not terms:
        return []
    return _search_index(open_index(), terms, limit, fields)


def _search_index(index, terms, limit, fields):
    import tantivy

    if not terms:
        return []
    schema = index.schema
    # LEX-UPPERCASE-01: use the index's native lowercase, but keep each literal
    # input atomic (whitespace analysis would split it; Python Unicode may differ).
    query_analyzer = None
    clauses = []
    for token in terms:
        if not isinstance(token, str):
            raise ValueError("Tantivy query terms must be strings")
        try:
            str.encode(token, "utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Tantivy query terms must be valid UTF-8") from exc
        normalized = None
        shoulds = []
        for field in fields:
            query_token = token
            if field in ("title", "summary", "aliases", "text"):
                if query_analyzer is None:
                    query_analyzer = (
                        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.raw())
                        .filter(tantivy.Filter.lowercase())
                        .build()
                    )
                if normalized is None:
                    normalized = query_analyzer.analyze(token)[0]
                query_token = normalized
            shoulds.append((tantivy.Occur.Should, tantivy.Query.term_query(schema, field, query_token)))
        clauses.append((tantivy.Occur.Must, tantivy.Query.boolean_query(shoulds)))
    query = tantivy.Query.boolean_query(clauses)

    index.reload()
    searcher = index.searcher()
    results = searcher.search(query, limit)
    rows = []
    for score, address in results.hits:
        document = searcher.doc(address)
        rows.append((document["node_key"][0], -float(score)))
    return rows


class MirrorReplayDeferred(RuntimeError):
    """Durable rebuild is scheduled, but did not finish within its step budget."""


def current_token():
    """Validated fresh token, also usable by callers after SQLite metadata reads."""
    from vector_lake import db_store

    if not enabled():
        return None
    token = db_store.tantivy_mirror_read_token()
    if token is None:
        return None
    identity = _disk_identity()
    if identity is None or identity[-1] != token.incarnation:
        _forget_index()
        return None
    return token


def search_if_current(terms: list[str], limit: int = 50,
                      fields: tuple[str, ...] = SEARCH_FIELDS):
    """Rows or None (SQL fallback); never create a mirror from a reader.

    Callers fetching SQLite metadata must additionally compare current_token()
    before this call and after their metadata read. Native/SQLite faults propagate.
    """
    before = current_token()
    if before is None:
        return None
    identity = _disk_identity()
    index = open_index(create=False)
    if index is None:
        return None
    rows = _search_index(index, terms, limit, fields)
    if current_token() != before or _disk_identity() != identity:
        return None
    return rows


def replay_pending(max_rows: int = 100, max_bytes: int = 4 * 1024 * 1024,
                   max_attempts: int = 3) -> dict:
    """One bounded durable step, at most one commit; errors persist then propagate.

    Lock covers selection, admission, native commit/release and conditional ACK.
    SQLite producer transactions do not acquire this lock. No internal retries.
    """
    from vector_lake import db_store as db

    db._tantivy_limits(max_rows, max_bytes, max_attempts)
    state = db.tantivy_mirror_state(max_attempts)
    result = {"selected": 0, "admitted": 0, "acked": 0, "conflicts": 0,
              "rejected": 0, "payload_bytes": 0, "commits": 0, "scanned": 0}
    if not enabled():
        return dict(result, status="disabled", **state)
    with _process_ownership(), _index_lock():
        state = db.tantivy_mirror_state(max_attempts)
        identity = _disk_identity()
        if state["phase"] != "needs_clear" and (identity is None or identity[-1] != state["incarnation"]):
            _forget_index()
            db.request_tantivy_rebuild()
            state = db.tantivy_mirror_state(max_attempts)
        epoch = state["epoch"]
        if state["phase"] == "needs_clear":
            if db.tantivy_mirror_admit_clear(epoch, max_attempts):
                try:
                    with _owned_writer() as writer:
                        writer.delete_all_documents()
                        writer.commit()
                    result["commits"] = 1
                    incarnation = _new_incarnation()
                    _incarnation_file().write_text(incarnation, encoding="utf-8")
                    _forget_index()
                    result["conflicts"] = int(not db.tantivy_mirror_ack_clear(epoch, incarnation))
                except Exception as exc:
                    db.tantivy_mirror_fail_clear(epoch, f"{type(exc).__name__}: {exc}")
                    raise
        elif state["phase"] == "seeding":
            seed = db.tantivy_mirror_seed(epoch, max_rows)
            result["scanned"] = seed["scanned"]
            result["conflicts"] = int(seed["conflict"])
        elif state["phase"] == "replay":
            batch = db.tantivy_mirror_select(max_rows, max_bytes, max_attempts)
            result["selected"] = len(batch.intents)
            result["rejected"] = len(batch.rejected)
            admitted = db.tantivy_mirror_admit(batch, max_attempts)
            result["admitted"] = len(admitted)
            result["payload_bytes"] = sum(item.payload_bytes for item in admitted)
            result["conflicts"] = len(batch.intents) - len(admitted)
            if admitted:
                try:
                    import tantivy

                    with _owned_writer() as writer:
                        for item in admitted:
                            writer.delete_documents_by_term("node_key", item.node_key)
                            if item.operation == "upsert":
                                writer.add_document(_document(tantivy, item.node_key, item.title,
                                                              item.summary, item.text))
                        writer.commit()
                    result["commits"] = 1
                    result["acked"] = db.tantivy_mirror_ack(admitted)
                    result["conflicts"] += len(admitted) - result["acked"]
                except Exception as exc:
                    db.tantivy_mirror_fail(admitted, f"{type(exc).__name__}: {exc}")
                    raise
            # Missing/replaced native storage cannot be blessed as a clean mirror.
            if _disk_identity() == identity:
                db.tantivy_mirror_mark_ready(epoch, state["incarnation"])
        final = db.tantivy_mirror_state(max_attempts)
        return dict(result, status="ready" if final["phase"] == "ready" else "pending", **final)


def rebuild_from_sqlite(batch: int = 500, *, max_steps: int = 32) -> int:
    """Request a durable rebuild; incomplete work raises rather than reporting success.

    Legacy batch sizes above 100 are accepted but each actual step is capped at
    100 rows. Resume deferred work with replay_pending(), not another rebuild.
    """
    from vector_lake import db_store

    if type(batch) is not int or batch < 1:
        raise ValueError("batch must be a positive integer")
    if type(max_steps) is not int or not 1 <= max_steps <= 32:
        raise ValueError("max_steps must be an integer in [1, 32]")
    epoch = db_store.request_tantivy_rebuild()
    for _ in range(max_steps):
        result = replay_pending(max_rows=min(batch, 100))
        if result["epoch"] != epoch:
            raise MirrorReplayDeferred("Rebuild superseded by a newer epoch; mirror remains deferred")
        if result["status"] == "ready":
            token = current_token()
            snapshot = stats()
            if token is not None and token.epoch == epoch and current_token() == token and snapshot["exists"]:
                return snapshot["docs"]
            raise MirrorReplayDeferred("Mirror changed while verifying rebuild completion")
        if result["status"] == "disabled" or result["blocked"] or result["attempt_count"] >= 3:
            break
    raise MirrorReplayDeferred("Rebuild scheduled but not ready; resume with replay_pending()")


def stats() -> dict:
    index = open_index(create=False)
    if index is None:
        return {"exists": False, "docs": 0, "dir": str(index_dir())}
    # Reload first: a searcher built before the newest commit does not see it, and this is a
    # reporting surface -- it under-reported by exactly the last batch (7139 written, 7000 visible)
    # until the reload was added.
    index.reload()
    return {"exists": True, "docs": index.searcher().num_docs, "dir": str(index_dir())}
