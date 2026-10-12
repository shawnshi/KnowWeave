"""Canonical admission facts and controlled recovery write-boundary regressions."""
import json
from pathlib import Path

import pytest
import yaml

from vector_lake import db_store, governance_store, tool_ingest
from vector_lake.claim_extractor import _jsonable
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.wiki_utils import split_frontmatter, projection_hash
from tests.test_ingest_admission_review import _case, _assert_unfinished
from tests.test_ingest_admission_all import _claim as _ordinary_claim
from tests.test_controlled_recompile import scope_files, _dispatch, _claim
from tests.test_mutation_coordinator import _source_content


def test_ADM003_canonical_source_retains_admission_frontmatter(isolated_memory):
    raw, name, content, data = _case(isolated_memory, published=True)
    row = db_store.get_connection().execute('SELECT data_json FROM entities WHERE f_page_key=?', (name[:-3],)).fetchone()
    persisted = json.loads(row['data_json'])
    assert persisted['source_hash'] == data['hash']
    fm, _ = split_frontmatter(content)
    assert persisted['source_frontmatter'] == _jsonable(fm)


def test_ADM003_disk_only_hash_cannot_upgrade_old_source_to_new_bytes(isolated_memory):
    raw, name, content, original = _case(isolated_memory, published=True)
    original_version = governance_store.canonical_page_versions({name[:-3]})
    raw.write_text('Entirely new raw version B; prior Source still describes A.', encoding='utf-8')
    digest = tool_ingest.calculate_hash(str(raw))
    disk = tool_ingest._stamp_source_hash(content, digest, 'raw/review.md')
    (isolated_memory / 'wiki' / name).write_text(disk, encoding='utf-8')
    payload = {key: original[key] for key in ('filepath','canonical_name','source_hash','ingest_contract_version','integration_candidates','source_read_path')}
    payload.update(hash=digest, source_projection_hash=projection_hash(raw.read_text(encoding='utf-8')))
    job_id = db_store.enqueue_job('ingest', payload)
    claim = _ordinary_claim(job_id)
    data = {**payload, 'job_id': job_id, **{k: claim[k] for k in ('lease_token','lease_owner','lease_generation')},
            'integration': {'disposition': 'duplicate'}}
    assert not tool_ingest.current_source_publication_proven(str(raw), digest)
    assert 'Successfully finalized' not in tool_ingest.finalize_ingest([], data)
    _assert_unfinished(data)
    assert governance_store.canonical_page_versions({name[:-3]}) == original_version


@pytest.mark.parametrize('field,value', [('strategic_scope','other'), ('evidence_tier','new unsupported disk tier'), ('unparsed_attribute','injected projection attribute')])
def test_ADM003_full_admission_frontmatter_cannot_drift_only_on_disk(isolated_memory, field, value):
    raw, name, content, data = _case(isolated_memory, published=True)
    frontmatter, body = split_frontmatter(content)
    frontmatter[field] = value
    content = '---\n' + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + '---\n' + body
    (isolated_memory / 'wiki' / name).write_text(content, encoding='utf-8')
    assert not tool_ingest.current_source_publication_proven(str(raw), data['hash'])


def test_ADM003_legacy_canonical_without_durable_stamp_is_not_adopted_from_disk(isolated_memory):
    raw, name, content, data = _case(isolated_memory, published=True)
    conn = db_store.get_connection()
    row = conn.execute('SELECT entity_id,data_json FROM entities WHERE f_page_key=?', (name[:-3],)).fetchone()
    old = json.loads(row['data_json']); old.pop('source_hash', None); old.pop('source_frontmatter', None)
    with db_store.transaction():
        conn.execute('UPDATE entities SET data_json=? WHERE entity_id=?', (json.dumps(old), row['entity_id']))
    assert not tool_ingest.current_source_publication_proven(str(raw), data['hash'])


def test_ADM004_controlled_recovery_of_full_disk_projection_without_canonical(scope_files):
    _dispatch(scope_files)
    _, data = _claim()
    name = data['canonical_name']
    raw_ref = 'raw/' + Path(data['filepath']).name
    content = tool_ingest._stamp_source_hash(_source_content(), data['hash'], raw_ref)
    (scope_files['memory'] / 'wiki' / name).write_text(content, encoding='utf-8')
    assert governance_store.canonical_page_versions({name[:-3]}) == {}
    data['integration'] = {'disposition':'standalone', 'reason':'Recover the approved public source with no relation.'}
    result = tool_ingest.finalize_ingest([{'filename':name, 'content':content}], data)
    assert 'Successfully finalized' in result, result
    assert tool_ingest.current_source_publication_proven(data['filepath'], data['hash'])
    row = db_store.get_connection().execute('SELECT status FROM jobs WHERE job_id=?', (data['job_id'],)).fetchone()
    assert row['status'] == 'finalized'
    assert db_store.get_processed_files()[data['filepath']] == data['hash']


def test_ADM004_external_publication_still_fences_controlled_precondition(scope_files, monkeypatch):
    _dispatch(scope_files)
    _, data = _claim()
    name = data['canonical_name']; raw_ref = 'raw/' + Path(data['filepath']).name
    content = tool_ingest._stamp_source_hash(_source_content(), data['hash'], raw_ref)
    real_validate = tool_ingest.validate_ingest_payload
    def competing_commit(files, contract):
        ok, message = execute_mutation_plan(name, content=content)
        assert ok, message
        return real_validate(files, contract)
    monkeypatch.setattr(tool_ingest, 'validate_ingest_payload', competing_commit)
    data['integration'] = {'disposition':'standalone', 'reason':'Recover the approved public source with no relation.'}
    result = tool_ingest.finalize_ingest([{'filename':name, 'content':content}], data)
    assert 'Successfully finalized' not in result
    assert 'reassess' in result
    row = db_store.get_connection().execute('SELECT status,result_json FROM jobs WHERE job_id=?', (data['job_id'],)).fetchone()
    assert row['status'] == 'subagent_processing' and row['result_json'] is None
    assert db_store.get_processed_files()[data['filepath']] == scope_files['rows'][0]['old_ledger']['file_hash']


def test_ADM005_live_dispatch_prompts_allow_other_and_attributed_marketing():
    root = Path(__file__).resolve().parents[1]
    for filename in ('main.md','handoff.md'):
        text = (root / 'templates' / 'prompts' / 'ingest' / filename).read_text(encoding='utf-8')
        assert 'other' in text
        assert 'marketing-only material must not become' not in text
        assert 'exactly `core` or `edge`;' not in text
