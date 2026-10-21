"""Synthetic native outbox receipts: no production data or remote providers."""
import sqlite3

import pytest

from tests.test_mutation_coordinator import _source_content, _write_purpose_contract
from vector_lake import db_store, governance_store, mutation_coordinator
from vector_lake.mutation_receipts import get_mutation_status


def seed(memory, monkeypatch, *, deferred=False):
    _write_purpose_contract(memory)
    if deferred:
        def fail(*args, **kwargs):
            raise OSError('synthetic projection failure')
        monkeypatch.setattr(mutation_coordinator, 'materialize_markdown_projection', fail)
    mutation_coordinator.execute_mutation_plan('Source_Test.md', content=_source_content())
    return db_store.get_connection().execute('SELECT id FROM mutation_outbox').fetchone()[0]


def receipt(identity, **kwargs):
    result = get_mutation_status([identity], **kwargs)
    assert result['state'] == 'observed', result
    return result['receipts'][0]


def finish(identity):
    claim = next(row for row in db_store.claim_mutation_outbox() if row['id'] == identity)
    db_store.complete_mutation_outbox(identity, claim=claim)


def successor(identity, **changes):
    row = dict(db_store.get_connection().execute('SELECT * FROM mutation_outbox WHERE id=?', (identity,)).fetchone())
    new = db_store.enqueue_mutation(row['filename'], row['mutation_type'], row['payload_text'], validation_mode=row['validation_mode'])
    with db_store.transaction():
        db_store.bind_mutation_versions([new], {row['filename'][:-3]: row['base_version']})
        if changes:
            allowed = {'filename', 'mutation_type', 'payload_text', 'base_version', 'validation_mode'}
            assert set(changes) <= allowed
            row.update(changes)
            db_store.get_connection().execute(
                'UPDATE mutation_outbox SET filename=?,mutation_type=?,payload_text=?,base_version=?,validation_mode=? WHERE id=?',
                (row['filename'], row['mutation_type'], row['payload_text'], row['base_version'], row['validation_mode'], new),
            )
    return new


def test_snapshot_filters_exact_canonical_topic_cluster(isolated_memory):
    from vector_lake.mutation_receipts import canonical_snapshot, readonly_snapshot

    _write_purpose_contract(isolated_memory)
    for name, domain, topic_cluster in [
        ('Test', 'General', 'Unit'),
        ('Other', 'General', 'Elsewhere'),
        ('Hidden', 'Medical_IT', 'Unit'),
    ]:
        content = (_source_content().replace('id: source_test', f'id: source_{name.lower()}')
                   .replace('domain: General', f'domain: {domain}\ntopic_cluster: {topic_cluster}'))
        mutation_coordinator.execute_mutation_plan(f'Source_{name}.md', content=content)
    with readonly_snapshot() as conn:
        assert set(canonical_snapshot(conn, domain='General', cluster='Unit')) == {'Source_Test'}
        assert set(canonical_snapshot(conn, domain='General', cluster='Elsewhere')) == {'Source_Other'}
        assert canonical_snapshot(conn, domain='General', cluster='Missing') == {}
        assert set(canonical_snapshot(conn, domain='General')) == {'Source_Test', 'Source_Other'}


def test_missing_database_is_unavailable_and_not_created(isolated_memory, monkeypatch):
    path = isolated_memory / 'missing.db'
    monkeypatch.setattr(db_store, 'get_db_path', lambda: path)
    result = get_mutation_status([1])
    assert result['state'] == 'unavailable' and not result['completed']
    assert not path.exists()


def test_failed_schema_is_error_not_empty_success(isolated_memory, monkeypatch):
    path = isolated_memory / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE mutation_outbox(id INTEGER PRIMARY KEY)')
    monkeypatch.setattr(db_store, 'get_db_path', lambda: path)
    result = get_mutation_status([1])
    assert result['state'] == 'error' and not result['completed']
    assert result['error_type'] == 'OperationalError'


@pytest.mark.parametrize('ids,hops', [([], 32), ([True], 32), ([0], 32), ([1] * 101, 32), ([1], 0), ([1], 33), ([1], True)])
def test_invalid_inputs_rejected_without_database(ids, hops, monkeypatch):
    monkeypatch.setattr(db_store, 'get_db_path', lambda: pytest.fail('must validate first'))
    with pytest.raises(ValueError):
        get_mutation_status(ids, max_hops=hops)


def test_missing_receipt_and_pending_index_are_not_complete(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch)
    assert receipt(identity + 100)['status'] == 'missing'
    result = receipt(identity)
    assert result['canonical'] == 'current_target'
    assert result['projection'] == 'materialized'
    assert result['index'] == 'pending' and not result['completed']


def test_projection_failure_is_deferred_not_completed(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch, deferred=True)
    result = receipt(identity)
    assert result['canonical'] == 'current_target'
    assert result['projection'] == 'deferred' and not result['completed']


def test_native_scoped_index_completion_and_read_only_observation(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch)
    from vector_lake.watchdog_app import process_mutation_outbox_batch
    stats = process_mutation_outbox_batch(backoff_base=0)
    assert stats['completed'] == 1
    from vector_lake import mutation_receipts
    real_connect = mutation_receipts.sqlite3.connect
    statements = []
    def readonly_connect(*args, **kwargs):
        assert 'mode=ro' in args[0]
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(mutation_receipts.sqlite3, 'connect', readonly_connect)
    monkeypatch.setattr(db_store, 'init_db', lambda: pytest.fail('no migrations'))
    monkeypatch.setattr(db_store, 'get_connection', lambda: pytest.fail('no writable connection'))
    result = receipt(identity)
    assert result['completed'] and result['index'] == 'completed'
    assert not any(sql.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'REPLACE'} for sql in statements)


def test_equivalent_successor_preserves_superseded_and_effective_completion(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch)
    newer = successor(identity)
    finish(newer)
    result = receipt(identity)
    assert result['status'] == 'superseded'
    assert result['effective_status'] == 'completed' and result['completed']
    assert result['successor_chain'] == [newer]


@pytest.mark.parametrize('changes', [
    {'filename': 'Source_Other.md'}, {'mutation_type': 'delete'},
    {'payload_text': _source_content() + 'changed'}, {'base_version': 'different'},
    {'validation_mode': 'schema'},
])
def test_non_equivalent_successors_never_complete(isolated_memory, monkeypatch, changes):
    identity = seed(isolated_memory, monkeypatch)
    newer = successor(identity, **changes)
    finish(newer)
    result = receipt(identity)
    assert not result['completed'] and result['reason'] == 'non_equivalent_successor'


def test_hop_exhaustion_missing_successor_and_cycle_fail_closed(isolated_memory, monkeypatch):
    first = seed(isolated_memory, monkeypatch)
    second = successor(first)
    third = successor(second)
    assert receipt(first, max_hops=1)['reason'] == 'successor_hop_limit'
    with db_store.transaction():
        db_store.get_connection().execute('UPDATE mutation_outbox SET status=?,superseded_by=? WHERE id=?', ('superseded', first, third))
    assert receipt(first)['reason'] == 'invalid_successor_chain'
    with db_store.transaction():
        db_store.get_connection().execute('UPDATE mutation_outbox SET superseded_by=99999 WHERE id=?', (third,))
    assert receipt(first)['reason'] == 'successor_missing'


def test_completed_old_revision_is_not_current_success(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch)
    finish(identity)
    successor(identity)
    result = receipt(identity)
    assert result['status'] == 'completed' and not result['completed']
    assert result['reason'] == 'not_current_intent'


def test_exact_current_canonical_and_projection_required(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch)
    finish(identity)
    (isolated_memory / 'wiki' / 'Source_Test.md').write_text('drift', encoding='utf-8')
    assert receipt(identity)['reason'] == 'completed_projection_drift'
    entity = next(item for item in governance_store.load_entities()['items'].values() if item['page_key'] == 'Source_Test')
    entity['raw_text'] = 'canonical drift'
    governance_store.upsert_entity(entity['entity_id'], entity)
    result = receipt(identity)
    assert not result['completed'] and result['reason'] == 'canonical_target_changed'


def test_failed_native_intent_remains_failed(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch)
    claim = db_store.claim_mutation_outbox()[0]
    db_store.fail_mutation_outbox(identity, 'synthetic failure', max_attempts=1, claim=claim)
    result = receipt(identity)
    assert result['status'] == 'failed' and result['index'] == 'failed' and not result['completed']


def test_delete_completion_requires_current_absence(isolated_memory, monkeypatch):
    seed(isolated_memory, monkeypatch)
    mutation_coordinator.execute_mutation_plan('Source_Test.md', is_delete=True)
    identity = db_store.get_connection().execute('SELECT MAX(id) FROM mutation_outbox').fetchone()[0]
    finish(identity)
    assert receipt(identity)['completed']
    (isolated_memory / 'wiki' / 'Source_Test.md').write_text('reappeared', encoding='utf-8')
    assert receipt(identity)['reason'] == 'completed_projection_drift'


@pytest.mark.parametrize('case,reason', [
    ('unbound', 'unbound_canonical_target'), ('payload', 'payload_version_mismatch'),
    ('mode', 'invalid_validation_mode'), ('generation', 'invalid_current_generation'),
])
def test_invalid_current_binding_never_completes(isolated_memory, monkeypatch, case, reason):
    identity = seed(isolated_memory, monkeypatch)
    finish(identity)
    with db_store.transaction():
        conn = db_store.get_connection()
        if case == 'unbound':
            conn.execute('UPDATE mutation_outbox SET base_version=NULL WHERE id=?', (identity,))
        elif case == 'payload':
            conn.execute('UPDATE mutation_outbox SET payload_text=payload_text || ? WHERE id=?', ('changed', identity))
        elif case == 'mode':
            conn.execute('UPDATE mutation_outbox SET validation_mode=NULL WHERE id=?', (identity,))
        else:
            conn.execute('UPDATE mutation_outbox SET projection_generation=0 WHERE id=?', (identity,))
    result = receipt(identity)
    assert not result['completed'] and result['reason'] == reason


def test_corrupt_canonical_data_is_error_not_success(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch)
    finish(identity)
    with db_store.transaction():
        # Keep the generated page lookup intact while corrupting the entity binding.
        db_store.get_connection().execute(
            'UPDATE entities SET data_json=?',
            ('{"page_key":"Source_Test","entity_id":"wrong"}',),
        )
    result = get_mutation_status([identity])
    assert result['state'] == 'error' and not result['completed']


def test_payload_budget_and_deadline_fail_honestly(isolated_memory, monkeypatch):
    identity = seed(isolated_memory, monkeypatch)
    from vector_lake import mutation_receipts
    monkeypatch.setattr(mutation_receipts, 'MAX_PAYLOAD_BYTES', 1)
    result = get_mutation_status([identity])
    assert result['state'] == 'error' and not result['completed']
    monkeypatch.setattr(mutation_receipts.ReadBudget, 'check', lambda *args: (_ for _ in ()).throw(TimeoutError('synthetic deadline')))
    result = get_mutation_status([identity])
    assert result['state'] == 'error' and result['error_type'] == 'TimeoutError'
