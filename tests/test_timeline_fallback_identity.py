"""Canonical fallback must preserve the indexed mention predicate before LIMIT."""
import json

import pytest

from vector_lake import db_store, tool_timeline as timeline


@pytest.fixture
def identity_timeline(isolated_memory):
    db_store.init_db()
    c = db_store.get_connection()
    with db_store.transaction():
        for entity_id, title in [('first-id', 'Shared_Title'), ('twin-id', 'Shared_Title'),
                                 ('second-id', 'Secondary Label'), ('other-id', 'Other')]:
            c.execute('INSERT INTO entities(entity_id,canonical_name,data_json) VALUES(?,?,?)',
                      (entity_id, title, '{}'))
        rows = [
            ('unknown-a', None, ['first-id'], 'undated alpha', 'Observation'),
            ('unknown-b', None, ['twin-id'], 'undated twin', 'Observation'),
            ('known-a', '2026-06-01', ['first-id'], 'known alpha', 'Release'),
            ('known-b', '2026-06-01', ['first-id', 'second-id'], 'known multi', 'Release'),
            ('mention', '2026-05-01', ['other-id'], 'mentions Shared_Title explicitly', 'Observation'),
            ('unrelated', '2026-07-01', ['other-id'], 'unrelated', 'ReleaseCandidate'),
        ]
        for key, date, subjects, text, action in rows:
            data = {'claim_type': 'timeline-event', 'temporal_anchor': date,
                    'subject_entity_ids': subjects, 'source_ids': ['Source_Synthetic'], 'action': action}
            c.execute('INSERT INTO claims(claim_id,claim_text,status,data_json,updated_at) VALUES(?,?,?,?,?)',
                      (key, text, 'active', json.dumps(data), '2026-09-01'))
    timeline.rebuild_timeline_events_from_claims(dry_run=False)
    return c


def payload(text):
    return text.split('\n\n', 1)[1] if text.startswith('[DEGRADED]') else text


@pytest.mark.parametrize('damage', ['missing-one', 'missing-all', 'wrong-id'])
@pytest.mark.parametrize('name,action,limit,expected', [
    ('Shared_Title', None, 10, 5),
    ('Shared_Title', None, 1, 1),
    ('Shared_Title', 'Observation', 10, 3),
    ('Shared_Title', 'Release', 10, 2),
    ('Shared_Title', 'Relea*', 10, 2),
    ('SHARED_TITLE', None, 10, 5),
    ('Shared%Title', None, 10, 5),
    ('first-id', None, 10, 3),
    ('second-id', None, 10, 0),
    ('Secondary Label', None, 10, 1),
    ('absent', None, 10, 0),
    (None, 'Release', 10, 2),
])
def test_identity_mentions_and_complete_payload_survive_drift(identity_timeline, damage, name, action, limit, expected):
    c = identity_timeline
    healthy = timeline.search_timeline_events(name, action, limit)
    assert healthy.count('  Action: ') == expected
    if expected == 0:
        assert healthy == 'No timeline events found matching the criteria.'
    with db_store.transaction():
        if damage == 'missing-one':
            c.execute("DELETE FROM timeline_events WHERE claim_id='unknown-a'")
        elif damage == 'missing-all':
            c.execute('DELETE FROM timeline_events')
        else:
            c.execute("UPDATE timeline_events SET id='wrong-id' WHERE claim_id='unknown-a'")
    canonical_before = [tuple(r) for r in c.execute('SELECT * FROM claims ORDER BY claim_id')]
    result = timeline.search_timeline_events(name, action, limit)
    assert result.startswith('[DEGRADED]')
    assert payload(result) == healthy
    assert [tuple(r) for r in c.execute('SELECT * FROM claims ORDER BY claim_id')] == canonical_before


def test_title_resolution_uses_same_read_snapshot(identity_timeline, monkeypatch):
    c = identity_timeline
    with db_store.transaction():
        c.execute('DELETE FROM timeline_events')
    original = timeline._entity_title_map
    observations = []
    def lookup(ids):
        observations.append((c.in_transaction, c.execute('PRAGMA query_only').fetchone()[0]))
        return original(ids)
    monkeypatch.setattr(timeline, '_entity_title_map', lookup)
    result = timeline.search_timeline_events('Shared_Title')
    assert '[Unknown Date]' in result
    assert observations and all(transaction and readonly for transaction, readonly in observations)
    assert not c.in_transaction and c.execute('PRAGMA query_only').fetchone()[0] == 0
