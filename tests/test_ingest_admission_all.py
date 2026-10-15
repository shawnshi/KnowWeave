import json

import pytest

from vector_lake import db_store, tool_ingest
from vector_lake.ingest_model_contract import extract_result
from vector_lake.purpose_contract import load_purpose_contract, validate_ingest_payload
from vector_lake.wiki_utils import projection_hash
from vector_lake.subagent_handoff import create_subagent_task
from tests.test_mutation_coordinator import _source_content, _write_purpose_contract
from tests.test_controlled_recompile import scope_files, _save


def _claim(job_id):
    packet = create_subagent_task('ingest', 'synthetic fixture', 'JSON', {'job_id': job_id})
    db_store.mark_job_awaiting_subagent(job_id, str(packet))
    return json.loads(tool_ingest.claim_ingest_tasks(limit=1))[0]


@pytest.mark.parametrize('reason', ['Pure Science is outside strategic scope.', 'No matching evidence tier or production deployment.', 'reviewer returned invalid JSON'])
def test_semantic_rejection_is_not_completion(reason):
    result, error = extract_result(json.dumps({'files_written': [], 'integration': {'disposition': 'rejected', 'reason': reason}}))
    assert result is None
    assert error


def test_other_scope_without_evidence_tier_is_publishable(isolated_memory):
    _write_purpose_contract(isolated_memory)
    content = _source_content().replace('strategic_scope: core', 'strategic_scope: other').replace('evidence_tier: primary\n', '')
    records = validate_ingest_payload([{'filename': 'Source_Test.md', 'content': content}], load_purpose_contract())
    assert len(records) == 1


@pytest.mark.parametrize('body', ['Unrelated pure science paper.', 'A secondary commentary with no deployment evidence.', '', 'No selected events in this report.'])
def test_eligible_source_reaches_canonical_and_ledger(isolated_memory, body):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    raw = isolated_memory / 'raw' / 'test.md'
    raw.write_text(body, encoding='utf-8')
    payload = {'filepath': str(raw), 'hash': tool_ingest.calculate_hash(str(raw)), 'canonical_name': 'Source_Test.md', 'source_hash': '', 'source_projection_hash': projection_hash(body), 'ingest_contract_version': 10, 'integration_candidates': []}
    job_id = db_store.enqueue_job('ingest', payload)
    claim = _claim(job_id)
    content = _source_content().replace('raw/test.pdf', 'raw/test.md').replace('strategic_scope: core', 'strategic_scope: other').replace('evidence_tier: primary\n', '').replace('Primary source content.', 'The source reports: ' + (body or 'The original source has no substantive body; no claims were extracted.'))
    data = {**payload, 'job_id': job_id, 'lease_owner': claim['lease_owner'], 'lease_token': claim['lease_token'], 'lease_generation': claim['lease_generation'], 'integration': {'disposition': 'standalone', 'reason': 'The source has no supported relation to existing nodes.'}}
    result = tool_ingest.finalize_ingest([{'filename': 'Source_Test.md', 'content': content}], data)
    assert 'Successfully finalized' in result
    assert db_store.get_connection().execute('SELECT file_hash FROM processed_files WHERE filepath=?', (str(raw),)).fetchone()['file_hash'] == payload['hash']
    assert payload['hash'] in (isolated_memory/'wiki'/'Source_Test.md').read_text(encoding='utf-8')
    assert db_store.get_connection().execute('SELECT status FROM jobs WHERE job_id=?', (job_id,)).fetchone()['status'] == 'finalized'


def test_rejected_finalize_does_not_write_ledger(isolated_memory):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    raw = isolated_memory/'raw'/'test.md'; raw.write_text('a source', encoding='utf-8')
    payload = {'filepath': str(raw), 'hash': tool_ingest.calculate_hash(str(raw)), 'canonical_name': 'Source_Test.md'}
    job_id=db_store.enqueue_job('ingest',payload); claim=_claim(job_id)
    result=tool_ingest.finalize_ingest([], {**payload, 'job_id': job_id, 'lease_owner': claim['lease_owner'], 'lease_token': claim['lease_token'], 'lease_generation': claim['lease_generation'], 'integration': {'disposition':'rejected','reason':'The document is unrelated to medicine.'}})
    assert 'Successfully finalized' not in result
    assert db_store.get_connection().execute('SELECT 1 FROM processed_files WHERE filepath=?',(str(raw),)).fetchone() is None


@pytest.mark.parametrize('stamp, ref, expected', [('', 'raw/test.md', False), ('correct', 'raw/other/test.md', False), ('correct', 'raw/test.md', True)])
def test_publication_proof_requires_exact_source_and_bytes(isolated_memory, stamp, ref, expected):
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    raw=isolated_memory/'raw'/'test.md'; raw.write_text('test source', encoding='utf-8')
    digest=tool_ingest.calculate_hash(str(raw))
    content=_source_content().replace('raw/test.pdf', ref)
    if stamp:
        content=tool_ingest._stamp_source_hash(content, digest)
    (isolated_memory/'raw'/'another.md').write_text('public synthetic source', encoding='utf-8')
    from vector_lake.mutation_coordinator import execute_mutation_plan
    ok, message = execute_mutation_plan('Source_test.md', content=content)
    assert ok, message
    db_store.mark_file_processed(str(raw),digest)
    assert tool_ingest.current_source_publication_proven(str(raw),digest) is expected
    if not expected:
        with pytest.raises(ValueError,match='current Source publication'):
            tool_ingest._apply_integration_disposition([],{'filepath':str(raw),'hash':digest,'integration':{'disposition':'duplicate'}})


@pytest.mark.parametrize('relative', ['privacy/Diary/test.md', 'stocks/test.md', 'garmin/test.md', 'test.pdf', 'test.json'])
def test_input_directory_and_format_exclusions_remain(isolated_memory, relative):
    from vector_lake.wiki_utils import validate_raw_ingest_source_path
    path=isolated_memory/'raw'/relative
    with pytest.raises(ValueError,match='excluded'):
        validate_raw_ingest_source_path(str(path))


def test_new_scope_accepts_three_exact_approved_sources(scope_files):
    from vector_lake import controlled_recompile as cr
    rows=scope_files['rows'][:3]
    plan, plan_sha=_save(scope_files['memory']/'small-plan.json',{'rows':rows})
    approval={**scope_files['approval'],'version':2,'request_id':'small-scope','plan_sha256':plan_sha,'entries':scope_files['approval']['entries'][:3]}
    approved, approved_sha=_save(scope_files['memory']/'small-approval.json',approval)
    scope=cr.load_scope(plan,plan_sha,approved,approved_sha)
    assert len(scope['sources'])==3 and scope['version']==2
    result=cr.dispatch_scope(scope,batch_size=1)
    assert result['selected']==3 and result['enqueued']==1


def test_unrelated_source_has_honest_general_category(isolated_memory):
    _write_purpose_contract(isolated_memory)
    content=_source_content().replace('strategic_scope: core','strategic_scope: other').replace('System_Architecture','General_Knowledge').replace('evidence_tier: primary\n','')
    from vector_lake.mutation_coordinator import execute_mutation_plan
    ok, message=execute_mutation_plan('Source_Physics.md',content=content)
    assert ok, message
