"""Synthetic end-to-end SQLite/filesystem cases for the unregistered offline candidate."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from vector_lake import query_evidence as evidence
from vector_lake import retrieval_runtime as runtime
from vector_lake import wiki_utils


@pytest.fixture
def published(isolated_memory, monkeypatch):
    root = isolated_memory
    meta = root / 'wiki' / '.meta'
    meta.mkdir()
    db = meta / 'vector_lake.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE entities(entity_id TEXT PRIMARY KEY,f_page_key TEXT,data_json TEXT)')
    conn.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT)')
    monkeypatch.setattr(wiki_utils, 'load_config', lambda: {'exclude_paths': [], 'supported_extensions': ['.md', '.txt']})

    def add(key='Source_test', text='Agent harness uses a tool loop.\n\nA second useful paragraph.',
            domain='Artificial_Intelligence', reference=None, digest=None, **overrides):
        prefix = 'raw/人工智能' if domain == 'Artificial_Intelligence' else 'raw/article'
        reference = reference or prefix + '/' + key + '.md'
        path = root / reference
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = text if isinstance(text, bytes) else text.encode('utf-8')
        path.write_bytes(raw)
        record = {'id': key, 'type': 'source', 'domain': domain, 'status': 'Active',
                  'topic_cluster': 'test', 'sources': [reference], 'source_frontmatter': {},
                  'source_hash': digest or hashlib.md5(raw, usedforsecurity=False).hexdigest()}
        record.update(overrides)
        conn.execute('INSERT OR REPLACE INTO entities VALUES (?,?,?)', (key, key, json.dumps(record)))
        conn.execute('INSERT OR REPLACE INTO page_index_nodes VALUES (?,?,?)', (key, domain, 'test'))
        conn.commit()
        return path, record

    yield root, conn, add
    conn.close()


@contextmanager
def canonical_snapshot(root):
    # Test host owns the trusted fixture path and connection lifecycle, not the reader.
    conn = sqlite3.connect((root / 'wiki' / '.meta' / 'vector_lake.db').as_uri() + '?mode=ro',
                           uri=True, timeout=0)
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def query(keys=None, domain='Artificial_Intelligence', **kw):
    root = Path(os.environ['VECTOR_LAKE_MEMORY_DIR'])
    with canonical_snapshot(root) as conn, runtime.request_scope('agent harness'):
        return evidence.get_query_evidence('agent harness', keys or ['Source_test'], domain=domain,
                                           canonical_connection=conn, **kw)


def item(result):
    return result['evidence'][0]


def test_real_readonly_binding_exact_bytes_and_no_database_mutation(published):
    root, conn, add = published
    raw = '\ufeffauthor: Synthetic Author\r\npublished_at: 2026-01-01\r\ncreated: 2026-02-01\r\n\r\nAgent harness 医院😀工具。\r\n'
    path, _ = add(text=raw)
    before = list(conn.iterdump())
    result = query()
    node = item(result)
    assert result['status'] == 'complete'
    assert node['status'] == 'verified'
    assert node['source_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert node['author_and_publication_truth'] == 'not_adjudicated'
    assert node['binding_level'].endswith('not_full_publication')
    decoded = path.read_bytes().decode('utf-8')
    for p in node['passages']:
        assert decoded[p['char_start']:p['char_end']] == p['text']
        assert path.read_bytes()[p['byte_start']:p['byte_end']].decode('utf-8') == p['text']
        assert p['semantic_support'] == 'not_adjudicated'
    assert list(conn.iterdump()) == before
    assert path.read_bytes() == raw.encode('utf-8')
    assert runtime.current() is None


@pytest.mark.parametrize('domain', ['', 'General', 'Personal_Health', 'bogus'])
def test_explicit_pilot_domain_is_required_before_raw_access(published, monkeypatch, domain):
    published[2]()
    monkeypatch.setattr(evidence, '_read_raw', lambda *a: pytest.fail('out-of-domain read'))
    with pytest.raises(ValueError):
        query(domain=domain)


@pytest.mark.parametrize('keys', [[], ['Source_x'] * 9, ['Source_test', 'Source_test'],
                                  ['../Source_x'], ['Concept_x'], ['Source_x/../auth'],
                                  ['Source_x?token=synthetic'], [None]])
def test_bad_node_arguments_are_rejected(published, keys):
    with pytest.raises(ValueError):
        evidence.get_query_evidence('agent', keys, domain='Artificial_Intelligence')


def test_cluster_and_canonical_scope_precede_raw_read(published, monkeypatch):
    _, conn, add = published
    add()
    monkeypatch.setattr(evidence, '_read_raw', lambda *a: pytest.fail('scope bypass'))
    assert item(query(cluster='other'))['code'] == 'not_available'
    record = json.loads(conn.execute('SELECT data_json FROM entities').fetchone()[0])
    record['domain'] = 'Medical_IT'
    conn.execute('UPDATE entities SET data_json=?', (json.dumps(record),))
    conn.commit()
    assert item(query())['code'] == 'not_available'


@pytest.mark.parametrize('status', ['Deprecated', 'Archived', 'unknown', ''])
def test_history_only_or_unknown_status_is_not_silently_read(published, status):
    published[2](status=status)
    assert item(query())['code'] == 'not_available'


@pytest.mark.parametrize('change', [{'sources': []}, {'source_frontmatter': None},
                                   {'source_hash': ''}, {'source_hash': 'not-a-hash'}])
def test_disk_or_hash_only_binding_is_not_admitted(published, change):
    root, conn, add = published
    path, _ = add(**change)
    projection = root / 'wiki' / 'Source_test.md'
    projection.write_text('---\nsources: [raw/人工智能/Source_test.md]\nsource_hash: disk-only\n---\n', encoding='utf-8')
    before = list(conn.iterdump())
    assert item(query())['code'] == 'binding_unproven'
    assert list(conn.iterdump()) == before
    assert path.is_file()
    assert 'disk-only' in projection.read_text(encoding='utf-8')


@pytest.mark.parametrize('reference', ['raw/人工智能/../outside.md', 'raw/privacy/Diary/x.md',
                                        'raw/人工智能/auth.md', '/outside.md',
                                        'raw/人工智能/secret.md', 'raw/医疗政策/x.md'])
def test_private_or_escaping_references_are_never_read(published, monkeypatch, reference):
    _, conn, add = published
    _, record = add()
    record['sources'] = [reference]
    conn.execute('UPDATE entities SET data_json=?', (json.dumps(record),))
    conn.commit()
    monkeypatch.setattr(evidence, '_read_raw', lambda *a: pytest.fail('refused path read'))
    assert item(query())['status'] == 'refused'


def test_reuses_configured_ingest_exclusions(published, monkeypatch):
    published[2]()
    monkeypatch.setattr(wiki_utils, 'load_config', lambda: {'exclude_paths': ['Source_test.md'],
                                                         'supported_extensions': ['.md']})
    result = query()
    assert item(result)['code'] == 'configured_source_refused'


def test_stale_digest_and_malformed_utf8_are_errors_not_empty_data(published):
    _, _, add = published
    add(digest='0' * 32)
    result = query()
    assert result['status'] == 'failed' and item(result)['code'] == 'stale_source_binding'
    add(text=b'agent \xff harness')
    result = query()
    assert item(result)['status'] == 'error'
    assert item(result)['code'] == 'UnicodeDecodeError'


def test_oversized_raw_and_canonical_transfer_are_bounded(published, monkeypatch):
    _, conn, add = published
    add(text='agent ' + 'x' * runtime.MAX_SOURCE_BYTES)
    monkeypatch.setattr(evidence.os, 'read', lambda *a: pytest.fail('oversized source transfer'))
    assert item(query())['code'] == 'source_read_budget'
    obj = json.loads(conn.execute('SELECT data_json FROM entities').fetchone()[0])
    obj['padding'] = 'x' * runtime.MAX_SOURCE_BYTES
    conn.execute('UPDATE entities SET data_json=?', (json.dumps(obj),))
    conn.commit()
    assert item(query())['code'] == 'canonical_read_budget'


def test_prior_success_survives_aggregate_budget_refusal(published, monkeypatch):
    _, _, add = published
    add(key='Source_one', text='agent first')
    add(key='Source_two', text='agent ' + 'x' * 2000)
    monkeypatch.setattr(runtime, 'MAX_TOTAL_SOURCE_BYTES', 1000)
    result = query(['Source_one', 'Source_two'])
    assert result['status'] == 'partial'
    assert result['evidence'][0]['status'] == 'verified'
    assert result['evidence'][1]['status'] == 'refused'


def test_sensitive_footer_and_attribution_values_do_not_escape(published):
    root, _, add = published
    marker = 'SYNTHETIC_DO_NOT_EXPORT'
    text = ('Agent harness safe paragraph.\n\n'
            'author: https://example.invalid/?sessionid=' + marker + '\n\n'
            '[old share](https://example.invalid/?key=' + marker + '&amp;pass_ticket=' + marker + ')\n\n'
            'access_token=' + marker)
    add(text=text)
    result = query()
    encoded = json.dumps(result, ensure_ascii=False)
    assert marker not in encoded
    assert item(result)['sensitive_spans_withheld'] is True
    assert result['status'] == 'partial'
    assert any('safe paragraph' in p['text'] for p in item(result)['passages'])
    ledger = root / 'wiki' / '.meta' / 'runtime' / 'search_ledger.jsonl'
    assert marker not in ledger.read_text(encoding='utf-8')


def test_all_sensitive_text_is_withheld_not_modified_into_an_exact_quote(published):
    published[2](text='https://example.invalid/?pass_ticket=SYNTHETIC_ONLY')
    result = query()
    assert item(result)['code'] == 'no_safe_text_passage'
    assert 'passages' not in item(result)


def test_images_mean_partial_text_coverage_without_reading_images(published):
    published[2](text='Agent harness textual evidence.\n\n![unread](private-image.png)')
    result = query()
    assert result['status'] == 'partial'
    assert item(result)['coverage'] == 'text_only_partial'
    assert all('private-image.png' not in p['text'] for p in item(result)['passages'])


def test_attribution_candidates_do_not_resolve_conflicting_dates_or_import_dates(published):
    published[2](text='published_at: 2026-01-01\n\npublished_at: 2026-02-01\n\ncreated: 2026-03-01\n\nAgent harness.')
    candidates = item(query())['attribution_candidates']
    assert len([x for x in candidates if x['field'] == 'published_at']) == 2
    assert next(x for x in candidates if x['field'] == 'created')['role'] == 'record_metadata_only'
    assert all(x['status'] == 'explicit_text_not_adjudicated' for x in candidates)


def test_expired_deadline_never_enters_raw_read(published, monkeypatch):
    published[2]()
    monkeypatch.setattr(runtime, 'REQUEST_SECONDS', 0)
    monkeypatch.setattr(evidence, '_read_raw', lambda *a: pytest.fail('expired read'))
    result = query()
    assert result['status'] == 'failed'
    assert item(result)['code'] == 'RetrievalBudgetExceeded'


def test_missing_canonical_snapshot_is_not_initialized_or_normalized_as_no_data(isolated_memory):
    result = evidence.get_query_evidence('agent harness', ['Source_test'], domain='Artificial_Intelligence')
    assert result['status'] == 'failed'
    assert item(result)['code'] == 'canonical_snapshot_required'
    assert not (isolated_memory / 'wiki' / '.meta' / 'vector_lake.db').exists()


def test_source_change_during_read_is_detected(published, monkeypatch):
    path, _ = published[2](text='Agent MUTATION_MARKER harness')
    original = evidence.os.read
    changed = []

    def mutate(fd, n):
        data = original(fd, n)
        if b'MUTATION_MARKER' in data and not changed:
            changed.append(True)
            path.write_bytes(path.read_bytes() + b' concurrent mutation')
        return data

    monkeypatch.setattr(evidence.os, 'read', mutate)
    result = query()
    assert item(result)['code'] in {'source_changed_during_read', 'stale_source_binding'}
    assert 'passages' not in item(result)


def test_hardlinked_source_is_refused_before_bytes_are_read(published, monkeypatch):
    path, _ = published[2]()
    alias = path.with_name('alias.md')
    try:
        os.link(path, alias)
    except OSError as exc:
        pytest.skip('Hardlink capability unavailable: ' + type(exc).__name__)
    monkeypatch.setattr(evidence.os, 'read', lambda *a: pytest.fail('hardlink read'))
    assert item(query())['code'] == 'regular_single_link_source_required'


@pytest.mark.parametrize('outside', [False, True])
def test_symlink_source_never_returns_target_bytes(published, monkeypatch, outside):
    root, _, add = published
    path, _ = add(text='Agent approved original')
    target = (root.parent / 'outside.md') if outside else path.with_name('target.md')
    target.write_text('SYNTHETIC_UNAUTHORIZED_TARGET', encoding='utf-8')
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip('Symlink creation unavailable: ' + type(exc).__name__)
    monkeypatch.setattr(evidence.os, 'read', lambda *a: pytest.fail('symlink target read'))
    result = query()
    assert item(result)['status'] in {'refused', 'error'}
    assert 'SYNTHETIC_UNAUTHORIZED_TARGET' not in json.dumps(result)


def test_strict_line_only_authority_is_not_a_supported_pilot_argument(published, monkeypatch):
    published[2]()
    monkeypatch.setattr(evidence, '_read_raw', lambda *a: pytest.fail('unexpected range read'))
    with pytest.raises(TypeError):
        query(line_start=1, line_end=45)


def test_paragraph_selection_retains_only_bounded_top_windows(published):
    text = '\n\n'.join(('agent target ' if i % 101 == 0 else 'other ') + str(i) for i in range(10000))
    published[2](text=text)
    result = query()
    passages = item(result)['passages']
    assert len(passages) <= evidence.MAX_PASSAGES
    assert sum(len(p['text']) for p in passages) <= evidence.MAX_EXCERPT_CHARS


def test_nested_request_budget_and_mode_are_preserved(published, monkeypatch):
    published[2]()
    monkeypatch.setattr(evidence, '_read_raw', lambda *a: pytest.fail('exhausted parent read'))
    with runtime.request_scope('outer request') as state:
        state.mode = 'page'
        state.source_bytes = runtime.MAX_TOTAL_SOURCE_BYTES
        result = query()
        assert item(result)['code'] == 'aggregate_budget'
        assert state.mode == 'page'
        assert state.source_bytes == runtime.MAX_TOTAL_SOURCE_BYTES


def test_sha256_binding_and_medical_domain_alias(published):
    _, _, add = published
    text = 'Agent harness bounded source.'
    add(domain='Medical_IT', digest=hashlib.sha256(text.encode()).hexdigest(), text=text, status='Decayed')
    result = query(domain='Healthcare_IT')
    assert item(result)['status'] == 'verified'
    assert item(result)['domain'] == 'Medical_IT'
    assert item(result)['canonical_status'] == 'Decayed'


def test_malformed_canonical_payload_is_an_error_not_no_data(published):
    _, conn, add = published
    add()
    conn.execute("UPDATE entities SET data_json='{malformed'")
    conn.commit()
    result = query()
    assert item(result)['status'] == 'error'
    assert item(result)['code'] == 'JSONDecodeError'


def test_parent_substitution_is_blocked_or_detected_on_real_handles(published, monkeypatch):
    path, _ = published[2](text='Agent PARENT_SWAP_MARKER harness')
    original = evidence.os.read
    outcome = []

    def replace_parent(fd, n):
        data = original(fd, n)
        if b'PARENT_SWAP_MARKER' in data and not outcome:
            parent = path.parent
            try:
                parent.rename(parent.with_name('moved-parent'))
            except OSError:
                outcome.append('rename_blocked')
            else:
                outcome.append('renamed')
                parent.mkdir()
                path.write_text('SYNTHETIC_SUBSTITUTED_SOURCE', encoding='utf-8')
        return data

    monkeypatch.setattr(evidence.os, 'read', replace_parent)
    result = query()
    assert outcome
    if outcome == ['renamed']:
        assert item(result)['status'] in {'refused', 'error'}
    else:
        assert item(result)['status'] == 'verified'
    assert 'SYNTHETIC_SUBSTITUTED_SOURCE' not in json.dumps(result)


def test_long_paragraph_windows_cover_visible_terms_with_exact_offsets(published):
    text = 'İ' * 4000 + ' Agent harness important evidence. ' + 'x' * 4000
    path, _ = published[2](text=text)
    result = query()
    passages = item(result)['passages']
    assert any('harness' in p['text'] for p in passages)
    for p in passages:
        assert p['text'] == text[p['char_start']:p['char_end']]
        assert p['has_query_term_match'] == any(t in p['text'].lower() for t in ['agent', 'harness'])
        assert path.read_bytes()[p['byte_start']:p['byte_end']].decode() == p['text']


def test_bom_author_candidate_has_raw_locator_without_attribution_claim(published):
    published[2](text='\ufeffauthor: Synthetic Author\r\n\r\nAgent harness.')
    candidates = item(query())['attribution_candidates']
    assert candidates[0]['field'] == 'author'
    assert candidates[0]['value'] == 'Synthetic Author'
    assert candidates[0]['status'] == 'explicit_text_not_adjudicated'


def test_source_checks_do_not_follow_a_pathname_resolver(published, monkeypatch):
    published[2]()
    monkeypatch.setattr(wiki_utils, 'validate_raw_ingest_source_path',
                        lambda *a: pytest.fail('Raw pathname resolver must not run'))
    assert item(query())['status'] == 'verified'


def test_reparse_parent_is_opened_without_traversing_its_target(published, monkeypatch):
    root, _, add = published
    path, _ = add()
    parent = path.parent
    parent.rename(parent.with_name('original-parent'))
    target = root.parent / 'outside-directory'
    target.mkdir()
    (target / path.name).write_text('SYNTHETIC_REPARSE_TARGET', encoding='utf-8')
    try:
        parent.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip('Directory symlink creation unavailable: ' + type(exc).__name__)
    monkeypatch.setattr(evidence.os, 'read', lambda *a: pytest.fail('reparse target read'))
    result = query()
    assert item(result)['status'] in {'refused', 'error'}
    if os.name == 'nt':
        assert item(result)['code'] == 'source_reparse_or_path_change'
    assert 'SYNTHETIC_REPARSE_TARGET' not in json.dumps(result)


def test_native_io_code_is_retained_without_source_exception_text(published):
    path, _ = published[2]()
    path.unlink()
    result = query()
    failure = item(result)
    assert failure['status'] == 'error' and failure['code'] == 'FileNotFoundError'
    assert failure['native_codes']['errno'] == 2
    assert str(path) not in json.dumps(result)


def test_malformed_recursive_record_keeps_prior_success(published):
    _, conn, add = published
    add(key='Source_one')
    _, record = add(key='Source_two')
    malformed = json.dumps(record)[:-1] + ', "nested":' + '[' * 3000 + '0' + ']' * 3000 + '}'
    conn.execute('UPDATE entities SET data_json=? WHERE entity_id=?', (malformed, 'Source_two'))
    conn.commit()
    result = query(['Source_one', 'Source_two'])
    assert result['status'] == 'partial'
    assert result['evidence'][0]['status'] == 'verified'
    assert result['evidence'][1]['code'] == 'RecursionError'


def test_authorized_mcp_facade_does_not_expose_private_host_resources():
    import inspect
    from vector_lake import mcp_server
    assert 'get_query_evidence' in mcp_server.registered_tool_names(mcp_server.mcp)
    assert set(inspect.signature(mcp_server.get_query_evidence).parameters) == {'query', 'node_keys', 'domain', 'cluster'}
