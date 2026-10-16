"""Real SQLite/vec sidecar tests, confined to pytest directories and synthetic documents."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from vector_lake import chunk_index as chunks, tool_search as search
from vector_lake.retrieval_plan import plan_query


def _replace(path, key, text, domain='Medical_IT', vector=False, model='synthetic', **kwargs):
    split = chunks.split_document(key, text)
    vectors = [[2.0, 0.0] for _ in split] if vector else None
    return chunks.replace_document(path, key, text, domain, vectors=vectors,
                                   model=model if vector else None, **kwargs)


def test_chunking_covers_late_text_and_preserves_line_locators():
    text = '---\ntitle: synthetic\nsecret_header: OMIT_THIS_HEADER\n---\n# Long\n' + ('background words\n' * 2000) + '## Limits\nTAIL_EVIDENCE unique risk\n'
    parts = chunks.split_document('Source_Long', text)
    assert len(parts) > 10
    assert all(len(p['text']) <= 2000 and 'OMIT_THIS_HEADER' not in p['text'] for p in parts)
    assert any('TAIL_EVIDENCE' in p['text'] for p in parts)
    tail = next(p for p in parts if 'TAIL_EVIDENCE' in p['text'])
    assert tail['start_line'] <= text[:text.index('TAIL_EVIDENCE')].count('\n') + 1 <= tail['end_line']
    assert parts == chunks.split_document('Source_Long', text)


def test_missing_sidecar_read_creates_neither_database_nor_parent(tmp_path):
    path = tmp_path / 'missing' / 'chunks.sqlite3'
    rows, notes = chunks.search_chunks('needle', 'Medical_IT', path=path)
    assert rows == [] and 'not built' in notes[0]
    assert not path.parent.exists()


def test_fts_can_recall_beyond_page_embedding_prefix(tmp_path):
    path = tmp_path / 'chunks.sqlite3'
    text = '# Long\n' + ('irrelevant background\n' * 1800) + '## Limits\nTAIL_EVIDENCE specific weakness\n'
    _replace(path, 'Source_Long', text)
    rows, notes = chunks.search_chunks('TAIL_EVIDENCE', 'Medical_IT', path=path)
    assert not notes and rows[0]['page_key'] == 'Source_Long'
    assert any('TAIL_EVIDENCE' in hit['text'] for hit in rows[0]['hits'])


def test_partitioned_knn_applies_domain_cluster_and_history_before_candidates(tmp_path):
    path = tmp_path / 'chunks.sqlite3'
    _replace(path, 'Source_Allowed', 'Allowed body', vector=True, cluster='c')
    _replace(path, 'Source_Other', 'FOREIGN_SCOPE_SENTINEL', domain='Other', vector=True, cluster='c')
    _replace(path, 'Source_Cluster', 'OTHER_CLUSTER_SENTINEL', vector=True, cluster='other')
    _replace(path, 'Source_Decayed', 'Retained lower-priority evidence', vector=True, cluster='c', status='decayed')
    _replace(path, 'Source_Expired', 'HISTORY_SENTINEL', vector=True, cluster='c', status='archived')
    rows, notes = chunks.search_chunks('no_lexical_match', 'Healthcare_IT', cluster='c',
                                       query_vector=[1.0, 0.0], model='synthetic', path=path)
    assert not notes and {r['page_key'] for r in rows} == {'Source_Allowed', 'Source_Decayed'}
    assert 'SENTINEL' not in str(rows)
    history, _ = chunks.search_chunks('no_lexical_match', 'Medical_IT', cluster='c', include_history=True,
                                      query_vector=[1.0, 0.0], model='synthetic', path=path)
    assert {r['page_key'] for r in history} == {'Source_Allowed', 'Source_Decayed', 'Source_Expired'}


@pytest.mark.parametrize('all_relevant', [False, True])
def test_exact_knn_threshold_tail_distinguishes_complete_from_capped(tmp_path, monkeypatch, all_relevant):
    path = tmp_path / 'chunks.sqlite3'
    monkeypatch.setattr(chunks, 'MAX_CANDIDATES', 2)
    for i, name in enumerate(['Source_Alpha', 'Source_Beta', 'Source_Gamma']):
        vector = [1.0, 0.0] if all_relevant or i == 0 else [0.0, 1.0]
        chunks.replace_document(path, name, 'synthetic evidence', 'Medical_IT', vectors=[vector], model='synthetic')
    rows, notes = chunks.search_chunks('nonmatching', 'Medical_IT', query_vector=[1.0, 0.0], model='synthetic', top_k=2, path=path)
    assert bool(any('vector candidate limit' in n for n in notes)) == all_relevant
    if not all_relevant:
        assert [r['page_key'] for r in rows] == ['Source_Alpha']


def test_projection_mismatch_and_failed_replace_preserve_previous_page(tmp_path):
    path = tmp_path / 'chunks.sqlite3'
    old = 'needle original evidence'
    _replace(path, 'Source_Alpha', old, vector=True)
    with pytest.raises(ValueError, match='projection mismatch'):
        _replace(path, 'Source_Alpha', 'replacement', vector=True, model='other-model')
    rows, notes = chunks.search_chunks('needle', 'Medical_IT', query_vector=[1.0, 0.0], model='other-model', path=path)
    assert 'projection mismatch' in notes[0]
    assert rows[0]['source_hash'] == chunks.content_hash(old)
    assert 'original evidence' in rows[0]['hits'][0]['text']


@pytest.mark.parametrize('vectors', [[[0.0, 0.0]], [[float('nan'), 0.0]], [], [[1.0], [1.0]]])
def test_invalid_vectors_fail_before_creating_sidecar(tmp_path, vectors):
    path = tmp_path / 'invalid' / 'chunks.sqlite3'
    with pytest.raises(ValueError):
        chunks.replace_document(path, 'Source_Alpha', 'needle', 'Medical_IT', vectors=vectors, model='synthetic')
    assert not path.exists()


def test_replacement_is_idempotent_and_drops_obsolete_vectors(tmp_path):
    path = tmp_path / 'chunks.sqlite3'
    _replace(path, 'Source_Alpha', 'needle old', vector=True)
    _replace(path, 'Source_Alpha', 'needle old', vector=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] == 1
    _replace(path, 'Source_Alpha', 'needle new')
    rows, _ = chunks.search_chunks('nonmatching', 'Medical_IT', query_vector=[1.0, 0.0], model='synthetic', path=path)
    assert rows == []
    rows, _ = chunks.search_chunks('needle', 'Medical_IT', path=path)
    assert 'new' in rows[0]['hits'][0]['text']


def test_oversized_or_invalid_source_is_not_silently_truncated(tmp_path):
    path = tmp_path / 'chunks.sqlite3'
    with pytest.raises(ValueError):
        _replace(path, 'Source_Alpha', 'x' * (chunks.MAX_PAGE_BYTES + 1))
    with pytest.raises(ValueError):
        chunks.replace_document(path, '../escape', 'text', 'Medical_IT')
    assert not path.exists()


def test_build_preview_is_metadata_only_and_apply_requires_current_scope(monkeypatch, tmp_path):
    from vector_lake import page_index_projection
    node = {'domain': 'Medical_IT', 'status': 'Active', 'title': 'Synthetic'}
    catalog = type('Catalog', (), {'nodes_by_key': lambda self, keys: {'Source_Alpha': node}})()
    monkeypatch.setattr(page_index_projection, 'read_catalog', lambda: (catalog, None))
    monkeypatch.setattr(chunks, 'index_path', lambda: tmp_path / 'chunks.sqlite3')
    result = chunks.build_selected_pages(['Source_Alpha'], 'Medical_IT')
    assert result['applied'] is False and not (tmp_path / 'chunks.sqlite3').exists()
    with pytest.raises(ValueError, match='outside requested domain'):
        chunks.build_selected_pages(['Source_Alpha'], 'Other', apply=True)


def test_main_pipeline_uses_only_fresh_in_scope_chunk_sources(monkeypatch, tmp_path):
    from types import SimpleNamespace
    path = tmp_path / 'chunks.sqlite3'
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    text = '# Long\n' + ('background words\n' * 1800) + '## Limits\nTAIL_EVIDENCE specific weakness\n'
    source = wiki / 'Source_Long.md'
    source.write_text(text, encoding='utf-8', newline='\n')
    _replace(path, 'Source_Long', text)
    monkeypatch.setattr(chunks, 'index_path', lambda: path)
    monkeypatch.setattr(search, 'get_wiki_dir', lambda: wiki)
    node = {'title': 'Long source', 'domain': 'Medical_IT', 'status': 'Active', 'type': 'Source'}
    catalog = SimpleNamespace(nodes_by_key=lambda keys: {'Source_Long': node} if 'Source_Long' in keys else {}, adjacency=lambda: {})
    monkeypatch.setattr(search, '_expand_query_locally', lambda q: [q])
    monkeypatch.setattr(search, '_get_fts_recall_results', lambda *a, **k: [])
    monkeypatch.setattr(search, '_rerank_candidates_locally', lambda q, rows: rows)
    monkeypatch.setattr(search.search_ledger, 'record', lambda *a, **k: None)
    rows, notes, error = search._search_scored_pages('TAIL_EVIDENCE', 3, domain='Medical_IT', catalog=catalog,
                                                   profile='auto', use_chunks=True, allow_external_embedding=False)
    assert error is None and rows[0][1]['_key'] == 'Source_Long'
    assert rows[0][1]['_origin'] == 'chunk'
    source.write_text('changed source, no old evidence', encoding='utf-8')
    rows, notes, error = search._search_scored_pages('TAIL_EVIDENCE', 3, domain='Medical_IT', catalog=catalog,
                                                   profile='auto', use_chunks=True, allow_external_embedding=False)
    assert rows == [] and any('source unavailable or changed' in note for note in notes)


def test_current_metadata_and_path_guard_precede_chunk_body_read(monkeypatch, tmp_path):
    from types import SimpleNamespace
    rows = [{'page_key': '../escape', 'source_hash': 'invalid', 'hits': []}]
    monkeypatch.setattr(chunks, 'search_chunks', lambda *a, **k: (rows, []))
    monkeypatch.setattr(chunks, 'read_page', lambda p: pytest.fail('unsafe read'))
    monkeypatch.setattr(search, 'get_wiki_dir', lambda: tmp_path)
    catalog = SimpleNamespace(nodes_by_key=lambda keys: {'../escape': {'domain': 'Other', 'status': 'Active'}})
    found, notes = search._scoped_chunk_candidates('q', 'Medical_IT', None, False, [], catalog, plan_query('q'))
    assert found == [] and any('excluded' in n for n in notes)
    catalog.nodes_by_key = lambda keys: {'../escape': {'domain': 'Medical_IT', 'status': 'Active'}}
    found, notes = search._scoped_chunk_candidates('q', 'Medical_IT', None, False, [], catalog, plan_query('q'))
    assert found == [] and any('unavailable' in n for n in notes)


def test_apply_is_explicit_and_local_only_unless_egress_is_authorized(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from vector_lake import page_index_projection, embedding_scheduler
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    source = wiki / 'Source_Alpha.md'
    source.write_text('needle synthetic evidence', encoding='utf-8', newline='\n')
    catalog = SimpleNamespace(nodes_by_key=lambda keys: {'Source_Alpha': {'domain': 'Medical_IT', 'status': 'Active'}})
    monkeypatch.setattr(page_index_projection, 'read_catalog', lambda: (catalog, None))
    monkeypatch.setattr('vector_lake.wiki_utils.get_wiki_dir', lambda: wiki)
    path = tmp_path / 'chunks.sqlite3'
    monkeypatch.setattr(chunks, 'index_path', lambda: path)
    monkeypatch.setattr(embedding_scheduler, 'embed_texts', lambda *a, **k: pytest.fail('unauthorized provider call'))
    result = chunks.build_selected_pages(['Source_Alpha'], 'Medical_IT', apply=True)
    assert result['applied'] is True and result['external_embedding'] is False
    assert chunks.search_chunks('needle', 'Medical_IT', path=path)[0]


def test_source_change_during_authorized_embedding_is_not_committed(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from vector_lake import page_index_projection, embedding_scheduler
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    source = wiki / 'Source_Alpha.md'
    source.write_text('needle original evidence', encoding='utf-8', newline='\n')
    catalog = SimpleNamespace(nodes_by_key=lambda keys: {'Source_Alpha': {'domain': 'Medical_IT', 'status': 'Active'}})
    monkeypatch.setattr(page_index_projection, 'read_catalog', lambda: (catalog, None))
    monkeypatch.setattr('vector_lake.wiki_utils.get_wiki_dir', lambda: wiki)
    path = tmp_path / 'chunks.sqlite3'
    monkeypatch.setattr(chunks, 'index_path', lambda: path)
    monkeypatch.setattr(embedding_scheduler, 'load_embedding_rate_config', lambda: SimpleNamespace(model='synthetic'))
    def embed(texts, **kwargs):
        source.write_text('changed', encoding='utf-8')
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(embedding_scheduler, 'embed_texts', embed)
    with pytest.raises(RuntimeError, match='changed'):
        chunks.build_selected_pages(['Source_Alpha'], 'Medical_IT', apply=True, allow_external_embedding=True)
    assert not path.exists()


def test_projection_preflight_refuses_before_body_or_provider(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from vector_lake import page_index_projection, embedding_scheduler
    path = tmp_path / 'chunks.sqlite3'
    _replace(path, 'Source_Alpha', 'needle', vector=True)
    catalog = SimpleNamespace(nodes_by_key=lambda keys: {'Source_Alpha': {'domain': 'Medical_IT'}})
    monkeypatch.setattr(page_index_projection, 'read_catalog', lambda: (catalog, None))
    monkeypatch.setattr(chunks, 'index_path', lambda: path)
    monkeypatch.setattr(embedding_scheduler, 'load_embedding_rate_config', lambda: SimpleNamespace(model='other', dimension=2))
    monkeypatch.setattr(embedding_scheduler, 'embed_texts', lambda *a, **k: pytest.fail('provider invoked'))
    monkeypatch.setattr(chunks, 'read_page', lambda *a: pytest.fail('source body read'))
    with pytest.raises(ValueError, match='projection mismatch'):
        chunks.build_selected_pages(['Source_Alpha'], 'Medical_IT', apply=True, allow_external_embedding=True)


def test_semantic_chunk_quotes_matched_late_span_not_page_prefix(monkeypatch, tmp_path):
    from types import SimpleNamespace
    path = tmp_path / 'chunks.sqlite3'
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    content = '# Intro\n' + ('background material\n' * 1800) + '## Tail\nCats require veterinary follow-up.\n'
    source = wiki / 'Source_Long.md'
    source.write_text(content, encoding='utf-8', newline='\n')
    parts = chunks.split_document('Source_Long', content)
    vectors = [[1.0, 0.0] if 'Tail' in part['heading'] else [0.0, 1.0] for part in parts]
    chunks.replace_document(path, 'Source_Long', content, 'Medical_IT', vectors=vectors, model='synthetic')
    monkeypatch.setattr(chunks, 'index_path', lambda: path)
    monkeypatch.setattr(search, 'get_wiki_dir', lambda: wiki)
    monkeypatch.setattr(search, '_embedding_cache_key', lambda q: ('synthetic', 2, q))
    metadata = {'title': 'Long source', 'domain': 'Medical_IT', 'status': 'Active', 'type': 'Source'}
    catalog = SimpleNamespace(nodes_by_key=lambda keys: {'Source_Long': metadata})
    found, notes = search._scoped_chunk_candidates('feline', 'Medical_IT', None, False, [1.0, 0.0], catalog, plan_query('feline'))
    assert not notes and len(found) == 1
    node = {**metadata, '_chunk_source_hash': found[0]['source_hash'], '_chunk_hits': found[0]['hits']}
    text, locator, warning = search._evidence_for_node(content, 'feline', node)
    assert warning is None and 'veterinary follow-up' in text and 'background material' not in text
    assert '@sha256:' in locator
    node['_chunk_hits'][0]['text'] = 'FABRICATED_EVIDENCE'
    text, _, warning = search._evidence_for_node(content, 'feline', node)
    assert 'FABRICATED_EVIDENCE' not in text and warning


def test_chunk_evidence_hash_and_frontmatter_are_correct_for_bom_crlf(monkeypatch, tmp_path):
    path = tmp_path / 'Source_Alpha.md'
    content = '\ufeff---\r\ntitle: test\r\ninternal: OMIT_THIS_HEADER\r\n---\r\n# Body\r\nneedle evidence\r\n'
    path.write_bytes(content.encode('utf-8'))
    node = {'_key': 'Source_Alpha', 'title': 'Synthetic', '_chunk_source_hash': chunks.content_hash(content)}
    monkeypatch.setattr(search, 'get_wiki_dir', lambda: tmp_path)
    monkeypatch.setattr(search, '_search_scored_pages', lambda *a, **k: ([(1.0, node)], [], None))
    output = search.search_vector_lake('needle', domain='Medical_IT', use_chunks=True)
    assert 'OMIT_THIS_HEADER' not in output and 'needle evidence' in output
    assert 'Source changed' not in output and '@sha256:' in output


def test_long_heading_context_is_bounded_without_dropping_page_tail():
    text = '# ' + ('heading' * 3000) + '\nTAIL_EVIDENCE\n'
    parts = chunks.split_document('Source_Long', text)
    assert max(len(part['heading']) for part in parts) <= 1024
    assert any('TAIL_EVIDENCE' in part['text'] for part in parts)


def test_readers_do_not_observe_partial_page_replacements(tmp_path):
    path = tmp_path / 'chunks.sqlite3'
    _replace(path, 'Source_Alpha', 'needle old', vector=True)
    def writer():
        for i in range(8):
            _replace(path, 'Source_Alpha', f'needle version{i}', vector=True)
    def reader():
        for _ in range(12):
            rows, notes = chunks.search_chunks('needle', 'Medical_IT', query_vector=[1.0, 0.0], model='synthetic', path=path)
            assert not notes and len(rows) == 1
            hit = rows[0]['hits'][0]
            assert chunks.content_hash(hit['text']) == rows[0]['source_hash']
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(writer)] + [pool.submit(reader) for _ in range(3)]
        for future in futures:
            future.result()


def test_concurrent_readers_observe_consistent_scoped_results(tmp_path):
    path = tmp_path / 'chunks.sqlite3'
    _replace(path, 'Source_Alpha', 'needle synthetic evidence', vector=True)
    def query(_):
        rows, notes = chunks.search_chunks('needle', 'Medical_IT', query_vector=[1.0, 0.0], model='synthetic', path=path)
        assert not notes
        return [r['page_key'] for r in rows]
    assert query(0) == ['Source_Alpha']
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(query, range(24))) == [['Source_Alpha']] * 24
