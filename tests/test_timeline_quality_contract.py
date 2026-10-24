"""KW-04/05/06: chronology, projection integrity and snapshot-bound cache regressions."""
import hashlib
import json
import sqlite3

import pytest

from vector_lake import db_store, tool_timeline as timeline


@pytest.fixture
def corpus(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        conn.execute("INSERT INTO entities(entity_id,canonical_name,data_json) VALUES('target','Target','{}')")
        for i in range(8):
            data = {'claim_type': 'timeline-event', 'temporal_anchor': '2026-01-01',
                    'subject_entity_ids': ['target'], 'source_ids': ['Source_Synthetic'], 'event_tag': 'Observation'}
            conn.execute('INSERT INTO claims(claim_id,claim_text,status,data_json,updated_at) VALUES(?,?,?,?,?)',
                         (f'claim-{i}', f'Original event {i}', 'active', json.dumps(data), '2026-09-01'))
    timeline.rebuild_timeline_events_from_claims(dry_run=False)
    return conn


def add_event(key, date, text):
    data = {'claim_type': 'timeline-event', 'subject_entity_ids': ['target'],
            'source_ids': ['Source_Synthetic'], 'event_tag': 'Observation'}
    if date is not None:
        data['temporal_anchor'] = date
    with db_store.transaction():
        db_store.get_connection().execute(
            'INSERT INTO claims(claim_id,claim_text,status,data_json,updated_at) VALUES(?,?,?,?,?)',
            (key, text, 'active', json.dumps(data), '2099-01-01'))
    timeline.rebuild_timeline_events_from_claims(dry_run=False)


def payload(text):
    return text.split('\n\n', 1)[1] if text.startswith('[DEGRADED]') else text


def test_quarter_precedes_december_only_in_ascending_time(corpus):
    add_event('quarter', '2026-Q1', 'QUARTER_MARK')
    add_event('december', '2026-12-01', 'DECEMBER_MARK')
    text = timeline.search_timeline_events('Target', limit=2)
    assert text.index('DECEMBER_MARK') < text.index('QUARTER_MARK')
    assert '[2026-Q1]' in text and 'period' in text.lower()
    assert '2026-03-31' not in text


@pytest.mark.parametrize('date', ['2026', '2026-02', '2026-Q1', '2026-H1'])
def test_coarse_prefix_keeps_precision_without_changing_identity(corpus, date):
    text = f'[{date}] [Observation] Prefix event'
    row = {'claim_id': 'prefix', 'claim_text': text, 'data_json': '{}'}
    old_id = hashlib.sha256('\0'.join(['prefix', '', text]).encode()).hexdigest()[:24]
    event = timeline._event_from_claim_row(row)
    assert event['id'] == old_id
    assert event['event_date'] == date
    assert event['event_date_source'] == 'coarse'
    add_event('prefix', None, text)
    assert old_id in timeline._expected_event_ids(corpus)
    result = timeline.search_timeline_events('Target')
    assert f'[{date}] <Target>' in result


def test_same_day_ties_keep_id_order(corpus):
    expected = [r[0] for r in corpus.execute('SELECT description FROM timeline_events ORDER BY id ASC')]
    result = timeline.search_timeline_events('Target')
    positions = [result.index(item) for item in expected]
    assert positions == sorted(positions)


def test_middle_id_change_cannot_reuse_parity_proof(corpus):
    assert timeline.timeline_projection_parity()['missing'] == 0
    ids = sorted(r[0] for r in corpus.execute('SELECT id FROM timeline_events'))
    old = ids[3]
    new = f'{int(old, 16) + 1:024x}'
    assert new not in ids and ids[0] < new < ids[-1]
    with db_store.transaction():
        corpus.execute('UPDATE timeline_events SET id=? WHERE id=?', (new, old))
    result = timeline.timeline_projection_parity()
    assert result['missing'] == result['extra'] == 1
    assert timeline.search_timeline_events('Target').startswith('[DEGRADED]')


@pytest.mark.parametrize('field,value', [
    ('description', 'POISONED_DESCRIPTION'), ('action', 'POISONED_ACTION'),
    ('source_file', 'Source_Wrong'), ('entity_id', 'wrong-id'), ('entity_title', 'Wrong title'),
    ('event_date', '1900-01-01'), ('event_date_source', 'unknown'),
    ('sentiment', 'wrong'), ('claim_id', 'wrong-claim'),
])
def test_same_id_payload_drift_uses_canonical_data(corpus, field, value):
    healthy = timeline.search_timeline_events('Target')
    canonical_before = [tuple(row) for row in corpus.execute('SELECT * FROM claims ORDER BY claim_id')]
    projected_at = {row['id']: row['extracted_at'] for row in corpus.execute('SELECT id, extracted_at FROM timeline_events')}
    with db_store.transaction():
        corpus.execute(f'UPDATE timeline_events SET {field}=? WHERE claim_id=?', (value, 'claim-0'))
    result = timeline.search_timeline_events('Target')
    assert result.startswith('[DEGRADED]')
    assert payload(result) == healthy
    assert [tuple(row) for row in corpus.execute('SELECT * FROM claims ORDER BY claim_id')] == canonical_before
    timeline.repair_timeline_projection(dry_run=False)
    assert timeline.search_timeline_events('Target') == healthy
    assert {row['id']: row['extracted_at'] for row in corpus.execute('SELECT id, extracted_at FROM timeline_events')} == projected_at
    assert [tuple(row) for row in corpus.execute('SELECT * FROM claims ORDER BY claim_id')] == canonical_before


def test_external_commit_invalidates_cached_health(corpus):
    healthy = timeline.search_timeline_events('Target')
    writer = sqlite3.connect(str(db_store.get_db_path()))
    try:
        writer.execute("UPDATE timeline_events SET description='EXTERNAL_POISON' WHERE claim_id='claim-0'")
        writer.commit()
    finally:
        writer.close()
    result = timeline.search_timeline_events('Target')
    assert result.startswith('[DEGRADED]')
    assert payload(result) == healthy


def test_entity_title_change_cannot_reuse_old_payload(corpus):
    timeline.search_timeline_events('Target')
    with db_store.transaction():
        corpus.execute("UPDATE entities SET canonical_name='New Target' WHERE entity_id='target'")
    result = timeline.search_timeline_events('New Target')
    assert result.startswith('[DEGRADED]')
    assert '<New Target>' in result and result.count('  Action: ') == 8


def test_rolled_back_write_does_not_publish_a_cache_proof(corpus):
    timeline.timeline_projection_parity()
    with pytest.raises(RuntimeError, match='roll back'):
        with db_store.transaction():
            corpus.execute("UPDATE timeline_events SET id='wrong-id' WHERE claim_id='claim-0'")
            assert timeline.timeline_projection_parity()['missing'] == 1
            raise RuntimeError('roll back')
    assert timeline.timeline_projection_parity()['missing'] == 0
    assert not timeline.search_timeline_events('Target').startswith('[DEGRADED]')


def test_foreign_commit_during_snapshot_is_seen_only_next_request(corpus, monkeypatch):
    timeline.invalidate_timeline_parity_cache()
    healthy = timeline.search_timeline_events('Target')
    timeline.invalidate_timeline_parity_cache()
    original = timeline._timeline_claims_fingerprint
    done = []

    def fingerprint(conn=None):
        result = original(conn)
        if not done:
            done.append(True)
            writer = sqlite3.connect(str(db_store.get_db_path()))
            try:
                writer.execute("UPDATE timeline_events SET description='LATER_COMMIT' WHERE claim_id='claim-0'")
                writer.commit()
            finally:
                writer.close()
        return result

    monkeypatch.setattr(timeline, '_timeline_claims_fingerprint', fingerprint)
    assert timeline.search_timeline_events('Target') == healthy
    result = timeline.search_timeline_events('Target')
    assert result.startswith('[DEGRADED]')
    assert payload(result) == healthy


def test_warm_snapshot_does_not_rescan_fingerprint(corpus, monkeypatch):
    timeline.search_timeline_events('Target')

    def unexpected(*args, **kwargs):
        pytest.fail('An unchanged connection generation must reuse its verified health')

    monkeypatch.setattr(timeline, '_timeline_claims_fingerprint', unexpected)
    assert 'Original event' in timeline.search_timeline_events('Target')


def test_actual_mcp_transport_preserves_chronology_and_reports_errors(corpus):
    import asyncio
    from fastmcp import Client
    from vector_lake import mcp_server

    add_event('quarter', '2026-Q1', 'QUARTER_MARK')
    add_event('december', '2026-12-01', 'DECEMBER_MARK')

    async def run():
        async with Client(mcp_server.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert set(tools['search_timeline'].input_schema['properties']) == {'entity_name', 'action', 'limit'}
            result = await client.call_tool('search_timeline', {'entity_name': 'Target', 'limit': 2})
            text = '\n'.join(block.text for block in result.content if block.type == 'text')
            assert not result.is_error
            assert text.index('DECEMBER_MARK') < text.index('QUARTER_MARK')
            bad = await client.call_tool('search_vector_lake', {
                'query': 'synthetic', 'domain': 'General', 'mode': 'claim', 'profile': 'source',
            }, raise_on_error=False)
            assert bad.is_error
            error = '\n'.join(block.text for block in bad.content if block.type == 'text')
            assert 'page mode only' in error
            assert 'No matching' not in error

    asyncio.run(run())


def test_invalid_inputs_fail_closed_without_an_absence_result(corpus):
    with pytest.raises(ValueError):
        timeline.search_timeline_events('Target', limit='not-a-number')
    with pytest.raises(json.JSONDecodeError):
        timeline._event_from_claim_row({'claim_id': 'bad', 'claim_text': 'bad', 'data_json': '{invalid'})
    assert not corpus.in_transaction
    assert corpus.execute('PRAGMA query_only').fetchone()[0] == 0


def test_repair_rechecks_its_snapshot_inside_the_write_transaction(corpus, monkeypatch):
    from contextlib import contextmanager

    with db_store.transaction():
        corpus.execute("UPDATE timeline_events SET description='BEFORE_PLAN' WHERE claim_id='claim-0'")
    original = db_store.transaction
    intercepted = []

    @contextmanager
    def changed_before_lock():
        if not intercepted:
            intercepted.append(True)
            writer = sqlite3.connect(str(db_store.get_db_path()))
            try:
                writer.execute("UPDATE timeline_events SET description='AFTER_PLAN' WHERE claim_id='claim-0'")
                writer.commit()
            finally:
                writer.close()
        with original():
            yield

    monkeypatch.setattr(db_store, 'transaction', changed_before_lock)
    with pytest.raises(RuntimeError, match='changed after repair planning'):
        timeline.repair_timeline_projection(dry_run=False)
    assert corpus.execute("SELECT description FROM timeline_events WHERE claim_id='claim-0'").fetchone()[0] == 'AFTER_PLAN'
    assert corpus.execute("SELECT claim_text FROM claims WHERE claim_id='claim-0'").fetchone()[0] == 'Original event 0'
