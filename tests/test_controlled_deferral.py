import json
from pathlib import Path
import pytest
from vector_lake import claim_extractor, controlled_recompile as control, db_store, tool_ingest
from vector_lake.schema_validator import SchemaViolationException
from tests.test_controlled_recompile import scope_files, _dispatch, _claim


def _unknown(fixture, index=30):
    row = fixture['rows'][index]
    name = 'Source_public-%02d.md' % index
    frontmatter = {
        'id': f'source_unknown_{index}', 'entity_id': f'unknown-{index}',
        'title': 'Synthetic unknown owner', 'type': 'source', 'domain': 'General',
        'status': 'Active', 'epistemic-status': 'seed', 'categories': ['System_Architecture'],
        'updated': '2026-10-21T00:00:00Z', 'sources': [],
    }
    body = 'UNRELATED_PRIVATE_BODY_SENTINEL\n'
    data = claim_extractor.extract_page_objects(name, frontmatter, body)['entities'][0]
    conn = db_store.get_connection()
    conn.execute('INSERT INTO entities(entity_id,type,data_json) VALUES(?,?,?)',
                 (data['entity_id'], 'source', json.dumps(data)))
    conn.commit()
    page = fixture['memory'] / 'wiki' / name
    page.write_text('---\n' + json.dumps(frontmatter) + '\n---\n' + body, encoding='utf-8')
    return row, page


def test_malformed_unknown_owner_schema_propagates_without_writes(scope_files):
    row, page = _unknown(scope_files)
    page.write_text('---\nsources: []\n---\nUNRELATED_PRIVATE_BODY_SENTINEL\n', encoding='utf-8')
    before = page.read_bytes()
    conn = db_store.get_connection()
    database_before = list(conn.iterdump())
    with pytest.raises(SchemaViolationException, match="Missing required frontmatter field 'id'"):
        tool_ingest.current_source_publication_proven(row['filepath'], row['current_md5'])
    assert list(conn.iterdump()) == database_before
    assert page.read_bytes() == before
    assert scope_files['seen'] == []


def test_explicit_deferral_retains_source_and_allows_safe_suffix(scope_files):
    row, page = _unknown(scope_files)
    before = page.read_bytes()
    first = _dispatch(scope_files, 30)
    assert first['enqueued'] == 30
    result = control.controlled_recompile(*scope_files['args'], apply=True, batch_size=29,
                                         defer_filepath=row['filepath'], defer_sha256=row['current_sha256'])
    assert result['enqueued'] == 29 and result['deferred'] == 1
    conn = db_store.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE task_type='ingest_recompile'").fetchone()[0] == 59
    record = conn.execute('SELECT * FROM jobs WHERE task_type=?', (control.DEFERRAL_TYPE,)).fetchone()
    assert record['status'] == 'completed' and record['result_json'] is None
    assert page.read_bytes() == before
    assert dict(conn.execute('SELECT * FROM processed_files WHERE filepath=?', (row['filepath'],)).fetchone()) == row['old_ledger']
    assert not conn.execute("SELECT 1 FROM jobs WHERE task_type='ingest_recompile' AND json_extract(payload,'$.filepath')=?", (row['filepath'],)).fetchone()
    assert all(dict(conn.execute('SELECT * FROM processed_files WHERE filepath=?',(r['filepath'],)).fetchone()) == r['old_ledger'] for r in scope_files['rows'][60:])
    again = control.controlled_recompile(*scope_files['args'], apply=True, batch_size=60)
    assert again['enqueued'] == 0 and again['deferred'] == 1
    assert conn.execute('SELECT COUNT(*) FROM jobs WHERE task_type=?', (control.DEFERRAL_TYPE,)).fetchone()[0] == 1
    task,p = _claim()
    p.update(filepath=row['filepath'], hash=row['current_md5'], canonical_name='Source_public-30.md')
    p['controlled_recompile']['sha256'] = row['current_sha256']
    with pytest.raises(ValueError, match='explicitly deferred'):
        control.validate_marker(p, task_type=control.TASK_TYPE)


@pytest.mark.parametrize('damage', ['missing-sha', 'bad-sha', 'rejected-member', 'nonmember', 'changed-bytes'])
def test_deferral_validation_is_bounded_and_zero_write(scope_files, damage):
    row,_ = _unknown(scope_files)
    path,sha = row['filepath'],row['current_sha256']
    if damage == 'missing-sha':sha = None
    elif damage == 'bad-sha':sha = '0'*64
    elif damage == 'rejected-member':path=scope_files['rows'][60]['filepath'];sha=scope_files['rows'][60]['current_sha256']
    elif damage == 'nonmember':path=str(scope_files['memory']/'raw/nonmember.md')
    elif damage == 'changed-bytes':Path(path).write_text('Changed synthetic public version',encoding='utf-8')
    with pytest.raises(ValueError):
        control.controlled_recompile(*scope_files['args'], apply=True, defer_filepath=path, defer_sha256=sha)
    assert db_store.get_connection().execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_deferral_dry_run_and_no_implicit_skipping(scope_files):
    row,_ = _unknown(scope_files)
    result=control.controlled_recompile(*scope_files['args'],defer_filepath=row['filepath'],defer_sha256=row['current_sha256'])
    assert result['state']=='VALIDATED_NOT_DISPATCHED'
    assert db_store.get_connection().execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    with pytest.raises(ValueError,match='ownership is not proven'):
        _dispatch(scope_files,60)
    assert db_store.get_connection().execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_deferral_requires_unknown_owner_and_cannot_cancel_live_job(scope_files):
    row=scope_files['rows'][0]
    with pytest.raises(ValueError,match='unproven-ownership'):
        control.controlled_recompile(*scope_files['args'],apply=True,defer_filepath=row['filepath'],defer_sha256=row['current_sha256'])
    _dispatch(scope_files)
    with pytest.raises(ValueError,match='unfinished native work'):
        control.controlled_recompile(*scope_files['args'],apply=True,defer_filepath=row['filepath'],defer_sha256=row['current_sha256'])


def test_deferral_tampering_fails_closed(scope_files):
    row,_=_unknown(scope_files,0)
    control.controlled_recompile(*scope_files['args'],apply=True,defer_filepath=row['filepath'],defer_sha256=row['current_sha256'])
    conn=db_store.get_connection();rec=conn.execute('SELECT job_id,payload FROM jobs WHERE task_type=?',(control.DEFERRAL_TYPE,)).fetchone()
    payload=json.loads(rec['payload']);payload['sha256']='0'*64
    conn.execute('UPDATE jobs SET payload=? WHERE job_id=?',(json.dumps(payload),rec['job_id']));conn.commit()
    with pytest.raises(ValueError,match='deferral binding'):
        control.controlled_recompile(*scope_files['args'],apply=True)


@pytest.mark.parametrize('damage', ['cancelled', 'queued', 'bad-task-type', 'bad-request-job', 'missing-request-job', 'bad-request-id'])
@pytest.mark.parametrize('mode', ['default', 'repeat-flags', 'change-target'])
def test_RC_DEFER_001_revoked_or_damaged_identity_never_disappears_or_revives(scope_files, damage, mode):
    from vector_lake import subagent_handoff
    row,page = _unknown(scope_files,0)
    other,other_page = _unknown(scope_files,2)
    control.controlled_recompile(*scope_files['args'],apply=True,
        defer_filepath=row['filepath'],defer_sha256=row['current_sha256'])
    conn=db_store.get_connection()
    rec=conn.execute('SELECT * FROM jobs WHERE idempotency_key=?',
        ('controlled-deferral:'+scope_files['approval']['request_id'],)).fetchone()
    if damage in {'cancelled','queued'}:
        conn.execute('UPDATE jobs SET status=? WHERE job_id=?',(damage,rec['job_id']))
    elif damage=='bad-task-type':
        conn.execute('UPDATE jobs SET task_type=? WHERE job_id=?',('ingest_recompile_request',rec['job_id']))
    else:
        payload=json.loads(rec['payload'])
        if damage=='bad-request-job':payload['request_job_id']='not-the-authorized-request'
        elif damage=='missing-request-job':payload.pop('request_job_id')
        else:payload['request_id']='another-request'
        conn.execute('UPDATE jobs SET payload=? WHERE job_id=?',(json.dumps(payload),rec['job_id']))
    conn.commit()
    jobs_before=[dict(r) for r in conn.execute('SELECT * FROM jobs ORDER BY job_id')]
    ledger_before=[dict(r) for r in conn.execute('SELECT * FROM processed_files ORDER BY filepath')]
    entities_before=[dict(r) for r in conn.execute('SELECT * FROM entities ORDER BY entity_id')]
    packets_before={str(p):p.read_bytes() for p in subagent_handoff._task_root().glob('*.json')}
    source_before=(page.read_bytes(),other_page.read_bytes())
    seen_before=list(scope_files['seen'])
    flags={}
    if mode!='default':
        target=other if mode=='change-target' else row
        flags={'defer_filepath':target['filepath'],'defer_sha256':target['current_sha256']}
    with pytest.raises(ValueError,match='deferral'):
        control.controlled_recompile(*scope_files['args'],apply=True,**flags)
    assert [dict(r) for r in conn.execute('SELECT * FROM jobs ORDER BY job_id')]==jobs_before
    assert [dict(r) for r in conn.execute('SELECT * FROM processed_files ORDER BY filepath')]==ledger_before
    assert [dict(r) for r in conn.execute('SELECT * FROM entities ORDER BY entity_id')]==entities_before
    assert {str(p):p.read_bytes() for p in subagent_handoff._task_root().glob('*.json')}==packets_before
    assert (page.read_bytes(),other_page.read_bytes())==source_before
    assert scope_files['seen']==seen_before
