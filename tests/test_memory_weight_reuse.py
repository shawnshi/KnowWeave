"""Request-local IDF reuse must preserve the pre-optimization formula and exact views."""
import json
import sqlite3

import pytest

from vector_lake import governance_store as store
from vector_lake import memory_gram_index as gram
from vector_lake import retrieval_runtime as runtime


def legacy_relevance(memory, terms, df=None, total=None):
    fields = {
        'key': str(memory.get('memory_key', '')).lower(),
        'text': str(memory.get('text', '')).lower(),
        'page': str(memory.get('source_page', '')).lower(),
        'type': str(memory.get('memory_type', '')).lower(),
    }
    score = 0.0
    for term in terms:
        weight = gram.idf_weight(df.get(term, 0), total or 0) if df else 1.0
        if term in fields['key']:
            score += weight * gram.FIELD_WEIGHTS[1]
        if term in fields['text']:
            score += weight * gram.FIELD_WEIGHTS[2]
        if term in fields['page']:
            score += weight * gram.FIELD_WEIGHTS[4]
        if term in fields['type']:
            score += weight * 1.0
    return score


@pytest.mark.parametrize('terms', [[], ['医院'], ['agent', '医院'], ['医院', '医院']])
@pytest.mark.parametrize('df', [None, {}, {'医院': 3, 'agent': 7}])
def test_prepared_idf_preserves_original_float_bits(terms, df):
    weights = {t: gram.idf_weight(df.get(t, 0), 31) if df else 1.0 for t in terms}
    for memory in [{}, {'text': None}, {'memory_key': '医院 AGENT', 'text': '医院 agent',
                   'source_page': 'Source_医院.md', 'memory_type': '医院'}]:
        expected = legacy_relevance(memory, terms, df, 31)
        assert store._memory_relevance(memory, terms, df, 31).hex() == expected.hex()
        assert store._memory_relevance(memory, terms, df, 31,
                                       idf_weights=weights).hex() == expected.hex()


def memory_database():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE operational_memory(memory_id TEXT PRIMARY KEY,data_json TEXT)')
    conn.execute('CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT)')
    conn.execute("INSERT INTO page_index_nodes VALUES ('Source_parent','Medical_IT','target')")
    records = []
    for i in range(48):
        m = {'memory_id': f'memory_{i:04}', 'memory_key': f'case_{i % 7}',
             'text': '医院 agent' if i % 4 else '医院', 'memory_type': 'fact',
             'source_page': 'Source_parent.md', 'domain': 'General' if i % 6 == 0 else 'Medical_IT',
             'memory_score': .5, 'updated_at': '2026-01-01',
             'validity_state': 'superseded' if i % 5 == 0 else 'active'}
        conn.execute('INSERT INTO operational_memory VALUES (?,?)', (m['memory_id'], json.dumps(m)))
        if m['domain'] == 'Medical_IT':
            records.append(m)
    conn.commit()
    return conn, records


def test_scoped_views_compute_idf_once_per_term_and_never_share_across_requests(monkeypatch):
    conn, records = memory_database()
    monkeypatch.setattr(store, 'get_connection', lambda: conn)
    terms = store._query_terms('医院 agent')
    df = store._memory_term_frequencies(records, terms)
    expected = [store.score_memory_items(records, terms, 9, include_history=history,
                                        df=df, total=len(records)) for history in [False, True]]
    original = gram.idf_weight
    calls = []

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(gram, 'idf_weight', counted)
    try:
        with runtime.request_scope('医院 agent') as state:
            actual = store.search_memory_packet_views('医院 agent', active_top_k=9,
                                                       history_top_k=9, domain='Medical_IT')
        assert list(actual) == expected
        assert len(calls) == len(set(terms))
        # One read of the scope: the DF and the per-document scores come from the same pass.
        assert state.generations['memory_scope']['passes'] == 1
        conn.execute("UPDATE operational_memory SET data_json=json_set(data_json,'$.text','agent changed')")
        conn.commit()
        before = len(calls)
        store.search_memory_packet_views('医院 agent', domain='Medical_IT')
        assert len(calls) - before == len(set(terms))
    finally:
        conn.close()


def test_coarse_memory_timings_reach_terminal_ledger_without_double_count_claims(monkeypatch):
    from vector_lake import search_ledger
    conn, _ = memory_database()
    monkeypatch.setattr(store, 'get_connection', lambda: conn)
    receipts = []
    monkeypatch.setattr(search_ledger, 'record', lambda **kw: receipts.append(kw))
    try:
        with runtime.request_scope('医院 agent') as state:
            store.search_memory_packet_views('医院 agent', domain='Medical_IT')
        names = {'memory_scope_df', 'memory_scope_weights', 'memory_scope_rank', 'memory_scope_total'}
        assert names <= state.timings.keys()
        assert all(state.timings[k] >= 0 for k in names)
        assert state.timings['memory_scope_total'] >= sum(state.timings[k] for k in names - {'memory_scope_total'})
        assert names <= receipts[0]['timings'].keys()
        assert receipts[0]['status'] == 'complete'
    finally:
        conn.close()


def test_timed_stage_accumulates_and_preserves_original_failure(monkeypatch):
    from vector_lake import search_ledger
    receipts = []
    monkeypatch.setattr(search_ledger, 'record', lambda **kw: receipts.append(kw))
    error = ValueError('synthetic stage failure')
    with pytest.raises(ValueError) as result:
        with runtime.request_scope('synthetic') as state:
            for _ in range(2):
                with runtime.timed_stage('synthetic_stage'):
                    pass
            with runtime.timed_stage('synthetic_failed'):
                raise error
    assert result.value is error
    assert state.timings['synthetic_stage'] >= 0
    assert state.timings['synthetic_failed'] >= 0
    assert receipts[0]['status'] == 'failed'
    assert runtime.current() is None
    with runtime.timed_stage('outside_request'):
        assert runtime.current() is None
