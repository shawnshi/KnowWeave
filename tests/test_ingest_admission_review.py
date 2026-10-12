"""Independent-review regressions: trusted dispatch binding and canonical publication CAS."""
import pytest

from vector_lake import db_store, governance_store, tool_ingest
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.wiki_utils import projection_hash, canonical_source_name
from tests.test_ingest_admission_all import _claim
from tests.test_mutation_coordinator import _source_content, _write_purpose_contract


def _case(memory, published=False):
    _write_purpose_contract(memory)
    db_store.init_db()
    raw = memory / 'raw' / 'review.md'
    raw.write_text('Original synthetic public source.', encoding='utf-8')
    digest = tool_ingest.calculate_hash(str(raw))
    name = canonical_source_name(str(raw))
    content = tool_ingest._stamp_source_hash(_source_content(), digest, 'raw/review.md')
    if published:
        ok, message = execute_mutation_plan(name, content=content)
        assert ok, message
    version = governance_store.canonical_page_versions({name[:-3]}).get(name[:-3], '')
    payload = {'filepath': str(raw), 'hash': digest, 'canonical_name': name, 'source_hash': version,
               'ingest_contract_version': 10, 'source_projection_hash': projection_hash(raw.read_text(encoding='utf-8')),
               'integration_candidates': [], 'source_read_path': str(raw)}
    job_id = db_store.enqueue_job('ingest', payload)
    claim = _claim(job_id)
    data = {**payload, 'job_id': job_id, **{key: claim[key] for key in ('lease_owner', 'lease_token', 'lease_generation')},
            'integration': {'disposition': 'standalone', 'reason': 'No authorized relation; retain the source independently.'}}
    return raw, name, content, data


def _assert_unfinished(data):
    conn = db_store.get_connection()
    assert conn.execute('SELECT 1 FROM processed_files WHERE filepath=?', (data['filepath'],)).fetchone() is None
    row = conn.execute('SELECT status,result_json FROM jobs WHERE job_id=?', (data['job_id'],)).fetchone()
    assert row['status'] == 'subagent_processing' and row['result_json'] is None


@pytest.mark.parametrize('field,value', [('ingest_contract_version', None), ('ingest_contract_version', 9),
    ('source_projection_hash', None), ('source_projection_hash', 'fake'), ('integration_candidates', None),
    ('integration_candidates', [{'target': 'Concept_Unauthorized.md'}]),
    ('source_read_path', None), ('source_read_path', 'unapproved-read.txt'), ('ingest_contract_version', True)])
@pytest.mark.parametrize('source_state', ['unchanged', 'changed', 'missing'])
def test_ADM001_v10_submission_cannot_downgrade_persistent_dispatch(isolated_memory, field, value, source_state):
    raw, name, content, data = _case(isolated_memory)
    if source_state == 'changed':
        raw.write_text('Changed after dispatch.', encoding='utf-8')
    elif source_state == 'missing':
        raw.unlink()
    if value is None:
        data.pop(field)
    else:
        data[field] = value
    result = tool_ingest.finalize_ingest([{'filename': name, 'content': content}], data)
    assert 'Successfully finalized' not in result, result
    _assert_unfinished(data)
    assert governance_store.canonical_page_versions({name[:-3]}) == {}
    assert not (isolated_memory / 'wiki' / name).exists()


@pytest.mark.parametrize('source_state', ['unchanged', 'changed', 'missing'])
def test_ADM001_simultaneous_version_and_projection_removal(isolated_memory, source_state):
    raw, name, content, data = _case(isolated_memory)
    if source_state == 'changed':
        raw.write_text('Changed after dispatch.', encoding='utf-8')
    elif source_state == 'missing':
        raw.unlink()
    data.pop('ingest_contract_version')
    data.pop('source_projection_hash')
    assert 'Successfully finalized' not in tool_ingest.finalize_ingest([{'filename': name, 'content': content}], data)
    _assert_unfinished(data)
    assert governance_store.canonical_page_versions({name[:-3]}) == {}


@pytest.mark.parametrize('body', ['', 'Full body but never committed to canonical state.'])
def test_ADM002_projection_alone_is_not_publication(isolated_memory, body):
    raw, name, content, data = _case(isolated_memory)
    front = content.split('---', 2)[1]
    (isolated_memory / 'wiki' / name).write_text('---' + front + '---\n' + body, encoding='utf-8')
    assert not tool_ingest.current_source_publication_proven(str(raw), data['hash'])
    data['integration'] = {'disposition': 'duplicate', 'reason': 'Existing projected source allegedly covers this input.'}
    result = tool_ingest.finalize_ingest([], data)
    assert 'Successfully finalized' not in result
    _assert_unfinished(data)


def test_ADM002_projection_must_match_canonical_version(isolated_memory):
    raw, name, content, data = _case(isolated_memory, published=True)
    assert tool_ingest.current_source_publication_proven(str(raw), data['hash'])
    (isolated_memory / 'wiki' / name).write_text(content + '\nUncommitted projection edit.\n', encoding='utf-8')
    assert not tool_ingest.current_source_publication_proven(str(raw), data['hash'])
    data['integration'] = {'disposition': 'duplicate'}
    assert 'Successfully finalized' not in tool_ingest.finalize_ingest([], data)
    _assert_unfinished(data)


def test_ADM002_canonical_deletion_invalidates_projection(isolated_memory):
    raw, name, content, data = _case(isolated_memory, published=True)
    with db_store.transaction():
        db_store.get_connection().execute('DELETE FROM entities WHERE f_page_key=?', (name[:-3],))
    assert not tool_ingest.current_source_publication_proven(str(raw), data['hash'])
    data['integration'] = {'disposition': 'duplicate'}
    assert 'Successfully finalized' not in tool_ingest.finalize_ingest([], data)
    _assert_unfinished(data)


def test_ADM002_duplicate_rechecks_observed_canonical_version_at_commit(isolated_memory, monkeypatch):
    raw, name, content, data = _case(isolated_memory, published=True)
    real_validate = tool_ingest.validate_ingest_payload
    def concurrent_publication(files, contract):
        ok, message = execute_mutation_plan(name, content=content.replace('Primary source content.', 'Updated canonical source summary.'))
        assert ok, message
        return real_validate(files, contract)
    monkeypatch.setattr(tool_ingest, 'validate_ingest_payload', concurrent_publication)
    data['integration'] = {'disposition': 'duplicate'}
    result = tool_ingest.finalize_ingest([], data)
    assert 'Successfully finalized' not in result, result
    _assert_unfinished(data)


def test_ADM002_real_canonical_publication_can_close_duplicate(isolated_memory):
    raw, name, content, data = _case(isolated_memory, published=True)
    data['integration'] = {'disposition': 'duplicate'}
    result = tool_ingest.finalize_ingest([], data)
    assert 'Successfully finalized' in result, result
    row = db_store.get_connection().execute('SELECT status FROM jobs WHERE job_id=?', (data['job_id'],)).fetchone()
    assert row['status'] == 'finalized'
    assert db_store.get_processed_files()[str(raw)] == data['hash']
