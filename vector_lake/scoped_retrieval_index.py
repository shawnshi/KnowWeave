"""Explicitly applied retrieval accelerators. Readers never repair schema or indexes."""
from collections import OrderedDict
import re
import sqlite3
import time

DOMAIN = "lower(COALESCE(NULLIF(json_extract(data_json, '$.domain'), ''), ''))"
CLUSTER = "lower(COALESCE(NULLIF(json_extract(data_json, '$.topic_cluster'), ''), ''))"
SOURCE = "CASE WHEN lower(substr(json_extract(data_json, '$.source_page'), -3)) = '.md' THEN substr(json_extract(data_json, '$.source_page'), 1, length(json_extract(data_json, '$.source_page')) - 3) ELSE json_extract(data_json, '$.source_page') END"
INDEXES = {
    'idx_rsp_memory_domain': f'CREATE INDEX IF NOT EXISTS idx_rsp_memory_domain ON operational_memory({DOMAIN})',
    'idx_rsp_memory_cluster': f'CREATE INDEX IF NOT EXISTS idx_rsp_memory_cluster ON operational_memory({CLUSTER})',
    'idx_rsp_memory_source': f'CREATE INDEX IF NOT EXISTS idx_rsp_memory_source ON operational_memory({SOURCE})',
    'idx_rsp_page_domain': 'CREATE INDEX IF NOT EXISTS idx_rsp_page_domain ON page_index_nodes(lower(domain))',
    'idx_rsp_page_cluster': 'CREATE INDEX IF NOT EXISTS idx_rsp_page_cluster ON page_index_nodes(lower(topic_cluster))',
}
TRIGGERS = {f'rsp_vector_{op.lower()}': f'CREATE TRIGGER IF NOT EXISTS rsp_vector_{op.lower()} AFTER {op} ON vec_emb_float BEGIN UPDATE retrieval_vector_revision SET revision=revision+1 WHERE singleton=1; END' for op in ('INSERT', 'UPDATE', 'DELETE')}
REVISION_TABLE = 'CREATE TABLE IF NOT EXISTS retrieval_vector_revision(singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)'
_CACHE = OrderedDict()


def _sql_tokens(sql):
    # Preserve SQL string literals: whitespace/case within a JSON path is significant.
    tokens = re.findall(r"'(?:''|[^'])*'|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]", sql or '')
    tokens = [t if t.startswith("'") else t.casefold() for t in tokens]
    if tokens[2:5] == ['if', 'not', 'exists']:
        del tokens[2:5]
    return tokens


def _managed_objects(conn):
    definitions = {**INDEXES, **TRIGGERS, 'retrieval_vector_revision': REVISION_TABLE}
    present, incompatible = set(), set()
    for kind, name, sql in conn.execute('SELECT type,name,sql FROM sqlite_master'):
        name = name.casefold()
        if name not in definitions:
            continue
        expected_kind = 'index' if name in INDEXES else 'trigger' if name in TRIGGERS else 'table'
        if name in present or kind != expected_kind or _sql_tokens(sql) != _sql_tokens(definitions[name]):
            incompatible.add(name)
        present.add(name)
    return present, incompatible


def status(conn):
    version = conn.execute('PRAGMA schema_version').fetchone()[0]
    cached = _CACHE.get(conn)
    if cached is not None and cached[0] == version:
        _CACHE.move_to_end(conn)
        return dict(cached[1])
    names, incompatible = _managed_objects(conn)
    valid = names - incompatible
    result = {'memory_acceleration': set(INDEXES) <= valid,
              'vector_revision': set(TRIGGERS) | {'retrieval_vector_revision'} <= valid,
              'schema_version': version,
              'incompatible_objects': sorted(incompatible)}
    _CACHE[conn] = (version, result)
    _CACHE.move_to_end(conn)
    while len(_CACHE) > 8:
        _CACHE.popitem(last=False)
    return dict(result)


def vector_revision(conn):
    if not status(conn)['vector_revision']:
        return None
    row = conn.execute('SELECT revision FROM retrieval_vector_revision WHERE singleton=1').fetchone()
    return int(row[0]) if row is not None else None


def scoped_sql(conn, domains=None, cluster=None, *, max_payload_bytes=None, include_rowid=False):
    if not status(conn)['memory_acceleration']:
        return None
    clauses, params = [], []
    for values, direct, page_field, memory_index, page_index in (
        (domains, DOMAIN, 'domain', 'idx_rsp_memory_domain', 'idx_rsp_page_domain'),
        ([cluster.lower()] if cluster else None, CLUSTER, 'topic_cluster', 'idx_rsp_memory_cluster', 'idx_rsp_page_cluster'),
    ):
        if not values:
            continue
        marks = ','.join('?' for _ in values)
        qualified_direct = direct.replace('data_json', 'm.data_json')
        qualified_source = SOURCE.replace('data_json', 'm.data_json')
        clauses.append(
            f'SELECT rowid FROM operational_memory INDEXED BY {memory_index} WHERE {direct} IN ({marks}) '
            'UNION '
            f'SELECT m.rowid FROM page_index_nodes p INDEXED BY {page_index} '
            # Remove column affinity so SQLite can seek the JSON expression index.
            f'JOIN operational_memory m INDEXED BY idx_rsp_memory_source ON +p.node_key={qualified_source} '
            f"WHERE lower(p.{page_field}) IN ({marks}) AND {qualified_direct}=''"
        )
        params.extend(values)
        params.extend(values)
    if not clauses:
        return None
    sets = ' INTERSECT '.join('SELECT rowid FROM (' + sql + ')' for sql in clauses)
    projection = 'data_json'
    if max_payload_bytes is not None:
        projection = ('CASE WHEN length(CAST(data_json AS BLOB)) <= ? THEN data_json END, '
                      'length(CAST(data_json AS BLOB))')
        params.insert(0, max_payload_bytes)
    if include_rowid:
        projection = 'rowid, ' + projection
    return 'SELECT ' + projection + ' FROM operational_memory WHERE rowid IN (' + sets + ') ORDER BY rowid', params


def scoped_memory_scope_sql(conn, domains, cluster=None):
    """``operational_memory_index`` row ids whose resolved scope matches.

    The payload predicate resolves each axis independently -- the row's own ``$.domain`` when it
    states one, else its page's; likewise ``$.topic_cluster`` -- so a row can take its domain from
    itself and its cluster from its page.  This builds one query per combination of sources, all of
    them answerable from columns: ``memory_domain``/``memory_cluster`` for the own half, ``page_key``
    joined to ``page_index_nodes.node_key`` for the page half.  Measured on the live corpus: 115-130
    ms for the 30 613-row Medical_IT scope against 285-470 ms for the JSON-expression form, and it
    returns ids in the namespace the n-gram postings use, so nothing has to be translated between
    ``operational_memory.rowid`` and ``operational_memory_index.rowid``.

    ``domains`` must already be lowercased and alias-expanded by the caller; ``cluster`` is matched
    exactly, as the payload predicate does.  Either may be omitted, not both.  Hints are applied
    only for indexes that exist: a database that has not run the accelerator is still answerable,
    just slower, and ``INDEXED BY`` on a missing index is an error rather than a hint.
    """
    names = [str(name).lower() for name in domains or () if str(name)]
    cluster_value = str(cluster).lower() if cluster else None
    if not names and not cluster_value:
        return None
    if not status(conn)['memory_acceleration']:
        return None

    key_hint = ' INDEXED BY idx_om_index_page_key' if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_om_index_page_key'"
    ).fetchone() else ''
    marks = ','.join('?' for _ in names)
    selects, params = [], []

    # Each axis either comes from the row (and must match) or is absent there (and the page decides).
    for domain_from_own in ((True, False) if names else (False,)):
        for cluster_from_own in ((True, False) if cluster_value else (True,)):
            where, bound, needs_page = [], [], False
            if domain_from_own:
                where.append(f'i.memory_domain IN ({marks})')
                bound.extend(names)
            elif names:
                where.append("i.memory_domain = ''")
                needs_page = True
            if cluster_value:
                if names and not domain_from_own:
                    where.append(f'lower(p.domain) IN ({marks})')
                    bound.extend(names)
                if cluster_from_own:
                    where.append('i.memory_cluster = ?')
                    bound.append(cluster_value)
                else:
                    where.append("i.memory_cluster = ''")
                    needs_page = True
                    where.append('lower(p.topic_cluster) = ?')
                    bound.append(cluster_value)
            elif not domain_from_own and names:
                where.append(f'lower(p.domain) IN ({marks})')
                bound.extend(names)
            if needs_page:
                # The page side drives the branch: a few thousand page rows seek the
                # page_key index, instead of 74k index rows reaching for a page.
                selects.append(
                    'SELECT i.rowid FROM page_index_nodes p INDEXED BY idx_rsp_page_domain '
                    f'JOIN operational_memory_index i{key_hint} ON i.page_key = p.node_key '
                    'WHERE ' + ' AND '.join(where)
                )
            else:
                selects.append(
                    'SELECT i.rowid FROM operational_memory_index i WHERE ' + ' AND '.join(where)
                )
            params.extend(bound)
    return ' UNION '.join(selects), params


def apply(conn, *, budget_seconds=60.0):
    if not 0 < budget_seconds <= 120 or conn.in_transaction:
        raise ValueError('Apply requires an idle connection and a budget in (0,120]')
    started = time.monotonic()
    conn.set_progress_handler(lambda: int(time.monotonic() - started >= budget_seconds), 1000)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _, incompatible = _managed_objects(conn)
        if incompatible:
            raise ValueError('Incompatible retrieval accelerator schema: ' + ', '.join(sorted(incompatible)))
        for sql in INDEXES.values():
            conn.execute(sql)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='vec_emb_float'").fetchone():
            conn.execute(REVISION_TABLE)
            conn.execute('INSERT OR IGNORE INTO retrieval_vector_revision VALUES(1,0)')
            for sql in TRIGGERS.values():
                conn.execute(sql)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.set_progress_handler(None, 0)
        _CACHE.pop(conn, None)
    return status(conn)


def inspect_file(path, *, apply_changes=False, budget_seconds=60.0):
    from contextlib import closing
    from pathlib import Path
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Explicit existing SQLite database required')
    uri = path.as_uri() + ('?mode=rw' if apply_changes else '?mode=ro')
    with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
        if not apply_changes:
            conn.execute('PRAGMA query_only=ON')
            return {'applied': False, **status(conn)}
        return {'applied': True, **apply(conn, budget_seconds=budget_seconds)}
