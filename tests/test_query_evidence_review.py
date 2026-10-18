"""Regression oracles for independent review round1; synthetic values only."""
import hashlib
import json
import re
import types

import pytest

from vector_lake import query_evidence as evidence
from vector_lake import retrieval_runtime as runtime
from vector_lake import wiki_utils
from tests.test_query_evidence import published, query, canonical_snapshot


def test_sensitive_node_identifier_is_rejected_without_echo(published):
    marker = 'SYNTHETIC_IDENTIFIER_CANARY'
    published[2](key='Source_-access_token=' + marker)
    with pytest.raises(ValueError, match='sensitive_source_identifier'):
        query(['Source_-access_token=' + marker])


def test_sensitive_canonical_raw_identifier_is_not_returned(published):
    marker = 'SYNTHETIC_RAW_ID_CANARY'
    published[2](reference='raw/人工智能/api_key=' + marker + '.md')
    result = query()
    assert item_status(result) == 'refused'
    assert marker not in json.dumps(result)


def item_status(result):
    return result['evidence'][0]['status']


def test_withheld_paragraph_does_not_reappear_in_attribution(published):
    marker = 'SYNTHETIC_ATTRIBUTION_CANARY'
    published[2](text='author: ' + marker + '\nhttps://example.invalid/?token=synthetic\n\nAgent harness safe.')
    result = query()
    assert marker not in json.dumps(result)
    assert result['evidence'][0]['sensitive_spans_withheld']


@pytest.mark.parametrize('field', ['author', '作者', 'published_at', 'date'])
@pytest.mark.parametrize('separator', ['\n', '\r\n'])
def test_empty_attribution_does_not_consume_the_next_line(published, field, separator):
    published[2](text=field + ':  ' + separator + 'Agent harness ordinary body.')
    candidates = query()['evidence'][0]['attribution_candidates']
    assert not any(c['field'] == field for c in candidates)


def test_existing_sha256_prefix_is_supported_without_admission_override(published):
    text = 'Agent harness source with canonical SHA256 prefix.'
    published[2](text=text, digest='sha256:' + hashlib.sha256(text.encode()).hexdigest())
    assert item_status(query()) == 'verified'


def test_actual_read_bytes_are_charged_before_post_handle_failure(published, monkeypatch):
    root, _, add = published
    path, _ = add(text='Agent harness byte accounting canary.')
    with runtime.request_scope('accounting') as state:
        budget = evidence._Budget()
        original = evidence._windows_path
        seen = []

        def fail_after_read(fd):
            seen.append(1)
            if len(seen) == 2:
                raise evidence.EvidenceRefused('source_changed_during_read')
            return original(fd)

        monkeypatch.setattr(evidence, '_windows_path', fail_after_read)
        with pytest.raises(evidence.EvidenceRefused):
            evidence._read_raw(root, ['raw', '人工智能', path.name], budget)
        assert state.source_bytes == len(path.read_bytes())
        assert budget.used == len(path.read_bytes())


def test_each_canonical_body_transfer_obeys_current_remaining_budget(published, monkeypatch):
    _, conn, add = published
    _, record = add(text='Agent harness.')
    filler = {'id': 'extra', 'type': 'concept', 'padding': 'x' * 1600}
    raw = json.dumps(filler)
    conn.execute('INSERT INTO entities VALUES (?,?,?)', ('extra', 'Source_test', raw))
    conn.commit()
    first = conn.execute('SELECT length(CAST(data_json AS BLOB)) FROM entities WHERE entity_id=?', ('Source_test',)).fetchone()[0]
    second = len(raw.encode())
    monkeypatch.setattr(runtime, 'MAX_TOTAL_SOURCE_BYTES', first + second - 1)
    excess = []

    def observe(cursor, row):
        first_column = cursor.description[0][0].lower()
        if ('case' in first_column or first_column == 'payload') and len(row) >= 2:
            body, size = row[:2]
            state = runtime.current()
            assert state is not None
            if body is not None and isinstance(size, int) and size > runtime.MAX_TOTAL_SOURCE_BYTES - state.source_bytes:
                excess.append(size)
        return row

    conn.row_factory = observe
    with runtime.request_scope('canonical bytes'):
        budget = evidence._Budget()
        with pytest.raises(evidence.EvidenceRefused):
            evidence._canonical_source(conn, 'Source_test', 'Artificial_Intelligence', None, budget)
    assert excess == []


def test_configuration_is_one_snapshot_for_the_batch(published, monkeypatch):
    _, _, add = published
    add(key='Source_one')
    add(key='Source_two')
    calls = []

    def config():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError('SYNTHETIC_CONFIG_VALUE_MUST_NOT_LEAK')
        return {'exclude_paths': [], 'supported_extensions': ['.md', '.txt']}

    monkeypatch.setattr(wiki_utils, 'load_config', config)
    result = query(['Source_one', 'Source_two'])
    assert calls == [1]
    assert all(x['status'] == 'verified' for x in result['evidence'])


def test_reader_never_opens_database_or_invokes_global_root_resolver(published, monkeypatch):
    root, _, add = published
    add()
    with canonical_snapshot(root) as conn:
        before_timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        progress = []
        conn.set_progress_handler(lambda: progress.append(1) or 0, 1)
        statements = []
        conn.set_trace_callback(statements.append)
        monkeypatch.setattr(evidence.sqlite3, 'connect', lambda *a, **k: pytest.fail('DB pathname open'))
        monkeypatch.setattr(wiki_utils, 'get_memory_dir', lambda: pytest.fail('global root resolve'))
        result = evidence.get_query_evidence('agent harness', ['Source_test'],
                                            domain='Artificial_Intelligence', canonical_connection=conn)
        assert item_status(result) == 'verified'
        assert conn.in_transaction
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == before_timeout
        assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
        assert not any(re.match(r'(BEGIN|COMMIT|ROLLBACK|CREATE|UPDATE|DELETE|INSERT)', s, re.I)
                       or re.match(r'PRAGMA.*=', s, re.I) for s in statements)
        observed = len(progress)
        conn.execute('SELECT 1').fetchone()
        assert len(progress) > observed
        conn.set_progress_handler(None, 0)


def test_unverified_platform_fails_before_config_or_io(published, monkeypatch):
    published[2]()
    monkeypatch.setattr(evidence, 'os', types.SimpleNamespace(name='posix'))
    monkeypatch.setattr(wiki_utils, 'load_config', lambda: pytest.fail('unsupported platform config'))
    result = evidence.get_query_evidence('agent harness', ['Source_test'], domain='Artificial_Intelligence')
    assert result['status'] == 'failed'
    assert result['evidence'][0]['code'] == 'safe_read_capability_unavailable'


def test_configured_root_symlink_is_not_resolved_before_capability_check(published, monkeypatch):
    root, _, add = published
    add()
    alias = root.parent / 'configured-alias'
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError as exc:
        pytest.skip('Directory symlink capability unavailable: ' + type(exc).__name__)
    with canonical_snapshot(root) as conn:
        monkeypatch.setenv('VECTOR_LAKE_MEMORY_DIR', str(alias))
        monkeypatch.setattr(wiki_utils, 'get_memory_dir', lambda: pytest.fail('resolved root'))
        result = evidence.get_query_evidence('agent harness', ['Source_test'],
                                            domain='Artificial_Intelligence', canonical_connection=conn)
    assert result['evidence'][0]['code'] == 'source_reparse_or_path_change'


def test_configuration_failure_is_typed_and_does_not_leak_text(published, monkeypatch):
    root, _, add = published
    add()
    marker = 'SYNTHETIC_CONFIG_EXCEPTION_CANARY'
    def fail():
        raise RuntimeError(marker)
    with canonical_snapshot(root) as conn:
        monkeypatch.setattr(wiki_utils, 'load_config', fail)
        result = evidence.get_query_evidence('agent harness', ['Source_test'],
                                            domain='Artificial_Intelligence', canonical_connection=conn)
    assert result['status'] == 'failed'
    assert result['evidence'][0]['code'] == 'RuntimeError'
    assert marker not in json.dumps(result)


def test_unowned_or_mutable_connection_is_refused_without_lifecycle_changes(published):
    _, conn, add = published
    add()
    conn.execute('BEGIN')
    result = evidence.get_query_evidence('agent harness', ['Source_test'],
                                        domain='Artificial_Intelligence', canonical_connection=conn)
    assert result['evidence'][0]['code'] == 'readonly_canonical_snapshot_required'
    assert conn.in_transaction
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 0
    conn.rollback()


@pytest.mark.parametrize('key', ['Source_access_token=CANARY', 'Source_API%5Fkey%3DCANARY'])
def test_encoded_identifier_values_are_not_echoed(published, key):
    published[2]()
    with pytest.raises(ValueError, match='sensitive_source_identifier') as failure:
        evidence.get_query_evidence('agent', [key], domain='Artificial_Intelligence')
    assert 'CANARY' not in str(failure.value)


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('label', ['api_key', 'access_token'])
@pytest.mark.parametrize('separator', ['=', ':'])
@pytest.mark.parametrize('indent', ['', '  '])
@pytest.mark.parametrize('wrap_label', [False, True])
def test_wrapped_credential_paragraph_and_attribution_stay_withheld(
        published, newline, label, separator, indent, wrap_label):
    marker = 'SYNTHETIC_WRAPPED_CREDENTIAL_CANARY'
    author = 'SYNTHETIC_WITHHELD_AUTHOR'
    gap = newline if wrap_label else ''
    published[2](text=(f'author: {author}{newline}{label}{gap}{separator}{newline}{indent}{marker}'
                       f'{newline}{newline}Agent harness safe paragraph.'))
    result = query()
    serialized = json.dumps(result)
    assert marker not in serialized
    assert author not in serialized
    node = result['evidence'][0]
    assert result['status'] == 'partial' and node['status'] == 'verified'
    assert node['sensitive_spans_withheld'] is True
    assert any('safe paragraph' in p['text'] for p in node['passages'])
    assert node['attribution_candidates'] == []
