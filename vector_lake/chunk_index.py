"""Opt-in, scoped chunk sidecar. Reads never create it; builds require explicit pages/apply."""
from __future__ import annotations

from bisect import bisect_right
from contextlib import closing
from hashlib import sha256
import math
from pathlib import Path
import re
import sqlite3
import time

from vector_lake.schema_validator import canonical_domain

MAX_PAGE_BYTES = 512 * 1024
MAX_CHUNKS_PER_PAGE = 512
MAX_BUILD_PAGES = 8
MAX_CANDIDATES = 128


def content_hash(content: str) -> str:
    return sha256(content.encode('utf-8')).hexdigest()


def index_path() -> Path:
    from vector_lake.wiki_utils import get_meta_dir
    return get_meta_dir() / 'retrieval_chunks.sqlite3'


def generation_token():
    path = index_path()
    stamps = []
    for candidate in (path, Path(str(path) + '-wal')):
        try:
            stat = candidate.stat()
            stamps.append((stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            stamps.append(None)
    return tuple(stamps)


def split_document(page_key: str, content: str, chunk_chars: int = 2000, overlap: int = 150) -> list[dict]:
    if not 400 <= chunk_chars <= 4000 or not 0 <= overlap < chunk_chars // 2:
        raise ValueError('Invalid chunk size/overlap')
    if len(content.encode('utf-8')) > MAX_PAGE_BYTES:
        raise ValueError('Page exceeds chunk build byte budget')
    lines = content.splitlines(keepends=True)
    first = 0
    if lines and lines[0].lstrip('\ufeff').strip() == '---':
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                first = i + 1
                break
        else:
            raise ValueError('Unclosed frontmatter')
    boundaries = []
    heading_path = []
    for i in range(first, len(lines)):
        match = re.match(r'^(#{1,6})\s+(.+)', lines[i])
        if match:
            heading_path = heading_path[:len(match[1]) - 1] + [match[2].strip()]
            boundaries.append((i, ' > '.join(heading_path)[:1024]))
    if not boundaries or boundaries[0][0] > first:
        boundaries.insert(0, (first, ''))
    digest = content_hash(content)
    chunks = []
    for part, (start, heading) in enumerate(boundaries):
        end = boundaries[part + 1][0] if part + 1 < len(boundaries) else len(lines)
        text = ''.join(lines[start:end])
        newlines = [m.end() - 1 for m in re.finditer(r'\r\n|\r|\n', text)]
        position = 0
        while position < len(text):
            stop = min(len(text), position + chunk_chars)
            fragment = text[position:stop]
            if fragment.strip():
                key = sha256(f'{page_key}\0{digest}\0{start}\0{position}'.encode()).hexdigest()
                chunks.append({'chunk_id': key, 'page_key': page_key, 'heading': heading,
                               'start_line': start + bisect_right(newlines, position - 1) + 1,
                               'end_line': start + bisect_right(newlines, max(position, stop - 1) - 1) + 1,
                               'text': fragment})
            if stop == len(text):
                break
            position = stop - overlap
    if len(chunks) > MAX_CHUNKS_PER_PAGE:
        raise ValueError('Page exceeds chunk count budget; nothing was truncated')
    return chunks


def read_page(path: Path) -> str:
    from vector_lake import retrieval_runtime
    if retrieval_runtime.current() is not None:
        return retrieval_runtime.read_source(path)
    with path.open('rb') as stream:
        data = stream.read(MAX_PAGE_BYTES + 1)
    if len(data) > MAX_PAGE_BYTES:
        raise ValueError('Page exceeds chunk read byte budget')
    return data.decode('utf-8')


def _unit(vector) -> list[float]:
    values = [float(v) for v in vector]
    norm = math.hypot(*values)
    if not values or not all(math.isfinite(v) for v in values) or not math.isfinite(norm) or norm == 0:
        raise ValueError('Chunk vector must be finite and non-zero')
    return [v / norm for v in values]


def _tokens(text: str) -> str:
    from vector_lake.tokenizer import tokenize_joined
    return tokenize_joined(text)


def _open(path: Path, *, writable: bool) -> sqlite3.Connection:
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=1)
        conn.execute('PRAGMA journal_mode=WAL')
    else:
        from vector_lake import retrieval_runtime
        state = retrieval_runtime.current()
        timeout = min(1.0, retrieval_runtime.remaining_seconds()) if state is not None else 1.0
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=timeout)
        conn.execute('PRAGMA query_only=ON')
        if state is not None:
            import time
            conn.set_progress_handler(lambda: int(time.monotonic() >= state.deadline), 1000)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _load_vec(conn):
    import sqlite_vec
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    return sqlite_vec


def projection_metadata(path: Path | None = None) -> dict:
    path = Path(path) if path is not None else index_path()
    if not path.is_file():
        return {}
    with closing(_open(path, writable=False)) as conn:
        _load_vec(conn)
        return dict(conn.execute('SELECT key,value FROM meta'))


def _schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS documents(page_key TEXT PRIMARY KEY,domain TEXT NOT NULL,cluster TEXT NOT NULL,status TEXT NOT NULL,source_hash TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS chunks(chunk_id TEXT PRIMARY KEY,page_key TEXT NOT NULL REFERENCES documents(page_key) ON DELETE CASCADE,start_line INTEGER NOT NULL,end_line INTEGER NOT NULL,heading TEXT NOT NULL,text TEXT NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_key)')
    conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(chunk_id UNINDEXED,tokens)')


def replace_document(path: Path, page_key: str, content: str, domain: str, *, cluster: str = '',
                     status: str = 'active', title: str = '', vectors=None, model: str | None = None) -> int:
    """Atomically replace one explicitly selected page. Validate before opening a writer."""
    from vector_lake.wiki_utils import validate_wiki_filename
    validate_wiki_filename(page_key + '.md')
    if '/' in page_key or '\\' in page_key or not domain.strip():
        raise ValueError('Explicit page key and domain required')
    chunks = split_document(page_key, content)
    normalized = None
    dimension = None
    if vectors is not None:
        if not model or len(vectors) != len(chunks) or not vectors:
            raise ValueError('Chunk embedding count/model mismatch')
        dimension = len(vectors[0])
        if not 1 <= dimension <= 4096 or any(len(v) != dimension for v in vectors):
            raise ValueError('Chunk embedding dimension mismatch or budget exceeded')
        normalized = [_unit(v) for v in vectors]
    domain = canonical_domain(domain).lower()
    with closing(_open(Path(path), writable=True)) as conn:
        vec = _load_vec(conn)
        with conn:
            _schema(conn)
            projection = dict(conn.execute('SELECT key,value FROM meta'))
            if normalized is not None:
                expected = {'model': str(model), 'dimension': str(dimension)}
                if projection and any(projection.get(k) != v for k, v in expected.items()):
                    raise ValueError('Chunk projection mismatch; existing index retained; rebuild is not automatic')
                if not projection:
                    conn.executemany('INSERT INTO meta(key,value) VALUES (?,?)', expected.items())
                    conn.execute(f'CREATE VIRTUAL TABLE chunk_vectors USING vec0(chunk_id TEXT PRIMARY KEY,domain TEXT partition key,cluster TEXT,status TEXT,embedding FLOAT[{dimension}])')
            old = [row[0] for row in conn.execute('SELECT chunk_id FROM chunks WHERE page_key=?', (page_key,))]
            for key in old:
                conn.execute('DELETE FROM chunk_fts WHERE chunk_id=?', (key,))
                if projection or normalized is not None:
                    conn.execute('DELETE FROM chunk_vectors WHERE chunk_id=?', (key,))
            conn.execute('DELETE FROM documents WHERE page_key=?', (page_key,))
            conn.execute('INSERT INTO documents VALUES (?,?,?,?,?)',
                         (page_key, domain, cluster.lower(), status.lower(), content_hash(content)))
            for i, chunk in enumerate(chunks):
                conn.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?)',
                             tuple(chunk[k] for k in ('chunk_id', 'page_key', 'start_line', 'end_line', 'heading', 'text')))
                conn.execute('INSERT INTO chunk_fts(chunk_id,tokens) VALUES (?,?)',
                             (chunk['chunk_id'], _tokens(title[:512] + ' ' + chunk['heading'] + ' ' + chunk['text'])))
                if normalized is not None:
                    conn.execute('INSERT INTO chunk_vectors(chunk_id,domain,cluster,status,embedding) VALUES (?,?,?,?,?)',
                                 (chunk['chunk_id'], domain, cluster.lower(), status.lower(), vec.serialize_float32(normalized[i])))
    return len(chunks)


def search_chunks(query: str, domain: str, *, cluster: str | None = None, include_history: bool = False,
                  query_vector=None, model: str | None = None, top_k: int = 25,
                  supplemental_queries=(), path: Path | None = None) -> tuple[list[dict], list[str]]:
    """FTS plus optional partitioned exact KNN. All selection happens before LIMIT."""
    if not domain or not domain.strip() or not 1 <= top_k <= MAX_CANDIDATES:
        raise ValueError('Explicit chunk domain and bounded top_k required')
    path = Path(path) if path is not None else index_path()
    if not path.is_file():
        return [], ['Chunk index unavailable: not built; no file created']
    domain = canonical_domain(domain).lower()
    notes = []
    with closing(_open(path, writable=False)) as conn:
        vec = _load_vec(conn)
        conn.execute('BEGIN')
        scope, params = ['d.domain=?'], [domain]
        if cluster:
            scope.append('d.cluster=?')
            params.append(cluster.lower())
        if not include_history:
            scope.append("d.status NOT IN ('deprecated','archived')")
        where = ' AND '.join(scope)
        scores = {}
        for text in dict.fromkeys([query, *supplemental_queries]):
            tokens = _tokens(text).split()[:64]
            if not tokens:
                continue
            match = ' '.join('"' + t.replace('"', '""') + '"' for t in tokens)
            rows = conn.execute(
                'SELECT chunk_fts.chunk_id,bm25(chunk_fts) AS rank FROM chunk_fts '
                'JOIN chunks c ON c.chunk_id=chunk_fts.chunk_id JOIN documents d ON d.page_key=c.page_key '
                f'WHERE chunk_fts MATCH ? AND {where} ORDER BY rank,chunk_fts.chunk_id LIMIT ?',
                [match, *params, MAX_CANDIDATES],
            ).fetchall()
            for rank, row in enumerate(rows):
                # Rewrites are alternative lexical queries, not extra independent votes.
                scores[row[0]] = max(scores.get(row[0], 0), 1 / (61 + rank))
            if len(rows) == MAX_CANDIDATES:
                notes.append('Chunk lexical candidate limit reached; results may be incomplete')
        projection = dict(conn.execute('SELECT key,value FROM meta'))
        if query_vector and not projection:
            notes.append('Chunk vectors unavailable: lexical-only sidecar; no embedding was initiated')
        if query_vector and projection:
            values = _unit(query_vector)
            if projection.get('model') != model or int(projection['dimension']) != len(values):
                notes.append('Chunk vector projection mismatch; lexical results retained')
            else:
                conditions = ['embedding MATCH ?', 'domain=?', 'k=?']
                vector_params = [vec.serialize_float32(values), domain, MAX_CANDIDATES]
                if cluster:
                    conditions.append('cluster=?')
                    vector_params.append(cluster.lower())
                if not include_history:
                    conditions.extend(["status!='deprecated'", "status!='archived'"])
                rows = conn.execute('SELECT chunk_id,distance FROM chunk_vectors WHERE '
                                    + ' AND '.join(conditions) + ' ORDER BY distance', vector_params).fetchall()
                if any(not math.isfinite(float(row[1])) for row in rows):
                    raise ValueError('Non-finite distance in chunk vector index')
                # vec0 is exact, scoped KNN in distance order: a below-threshold tail
                # proves later neighbors cannot pass the same strict cosine gate.
                if len(rows) == MAX_CANDIDATES and 1 - float(rows[-1][1]) ** 2 / 2 > 0.5:
                    notes.append('Chunk vector candidate limit reached; results may be incomplete')
                for rank, row in enumerate(rows):
                    if 1 - float(row[1]) ** 2 / 2 > 0.5:
                        scores[row[0]] = scores.get(row[0], 0) + 1 / (61 + rank)
        pages = {}
        for key, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:MAX_CANDIDATES]:
            row = conn.execute('SELECT c.*,d.source_hash FROM chunks c JOIN documents d ON d.page_key=c.page_key '
                               f'WHERE c.chunk_id=? AND {where}', [key, *params]).fetchone()
            if row is None:
                continue
            page = pages.setdefault(row['page_key'], {'page_key': row['page_key'], 'score': score,
                                                     'source_hash': row['source_hash'], 'hits': []})
            if len(page['hits']) < 3:
                page['hits'].append(dict(row))
        return sorted(pages.values(), key=lambda p: (-p['score'], p['page_key']))[:top_k], list(dict.fromkeys(notes))


def build_selected_pages(page_keys: list[str], domain: str, *, apply: bool = False,
                         allow_external_embedding: bool = False, budget_seconds: float = 60) -> dict:
    """Explicit bounded CLI build; dry-run reads metadata only, not page bodies."""
    from vector_lake import page_index_projection
    from vector_lake.wiki_utils import get_wiki_dir, validate_wiki_filename
    if not domain.strip() or not 1 <= len(page_keys) <= MAX_BUILD_PAGES or not 0 < budget_seconds <= 120:
        raise ValueError('Build requires domain, 1..8 explicit pages, and a budget <=120 seconds')
    page_keys = list(dict.fromkeys(key.removesuffix('.md') for key in page_keys))
    catalog, error = page_index_projection.read_catalog()
    if catalog is None or error:
        raise ValueError('Current source catalog required for chunk build')
    lookup = getattr(catalog, 'nodes_by_key', None)
    if not callable(lookup):
        raise ValueError('Current source catalog cannot resolve explicit source metadata')
    nodes = lookup(page_keys)
    if not isinstance(nodes, dict) or any(not isinstance(node, dict) for node in nodes.values()):
        raise ValueError('Current source catalog did not return a source metadata mapping')
    for key in page_keys:
        validate_wiki_filename(key + '.md')
        if '/' in key or '\\' in key or key not in nodes or canonical_domain(nodes[key].get('domain')).lower() != canonical_domain(domain).lower():
            raise ValueError('Chunk source is missing or outside requested domain')
    if not apply:
        return {'applied': False, 'pages': page_keys, 'domain': domain, 'external_embedding': allow_external_embedding}
    deadline = time.monotonic() + budget_seconds
    model = None
    if allow_external_embedding:
        from vector_lake.embedding_scheduler import load_embedding_rate_config
        config = load_embedding_rate_config()
        model = config.model
        projection = projection_metadata()
        if projection and (projection.get('model') != model or int(projection['dimension']) != config.dimension):
            raise ValueError('Chunk projection mismatch; no source body or embedding was read')
    completed = []
    root = get_wiki_dir().resolve()
    for key in page_keys:
        if time.monotonic() >= deadline:
            raise TimeoutError(f'Chunk build budget exhausted after {len(completed)} completed page(s); earlier commits retained')
        path = (root / (key + '.md')).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Chunk source path escapes wiki')
        content = read_page(path)
        chunks = split_document(key, content)
        vectors = None
        if allow_external_embedding and chunks:
            from vector_lake.embedding_scheduler import embed_texts
            vectors = []
            for offset in range(0, len(chunks), 8):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Chunk build budget exhausted; earlier page commits are retained')
                batch = [str(nodes[key].get('title') or key)[:512] + '\n' + c['heading'] + '\n' + c['text'] for c in chunks[offset:offset + 8]]
                vectors.extend(embed_texts(batch, budget_seconds=remaining))
        if time.monotonic() >= deadline or content_hash(read_page(path)) != content_hash(content):
            raise RuntimeError('Chunk source changed or budget expired; current page was not committed')
        count = replace_document(index_path(), key, content, domain,
                                 cluster=str(nodes[key].get('topic_cluster') or ''),
                                 status=str(nodes[key].get('status') or 'active'),
                                 title=str(nodes[key].get('title') or key), vectors=vectors, model=model)
        completed.append({'page_key': key, 'chunks': count})
    return {'applied': True, 'completed': completed, 'domain': domain,
            'external_embedding': allow_external_embedding}
