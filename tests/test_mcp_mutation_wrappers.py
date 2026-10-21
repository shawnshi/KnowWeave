"""Wrapper-only seams and synthetic native CAS link maintenance."""
from unittest.mock import patch

import pytest

from tests.test_mutation_coordinator import _source_content, _write_purpose_contract
from vector_lake import db_store, governance_store, mcp_server, mutation_coordinator


def test_write_wrapper_preserves_canonical_native_message(tmp_path):
    payload = tmp_path / 'payload.md'
    payload.write_text(_source_content(), encoding='utf-8')
    message = 'Canonical mutation batch committed; outbox ids [7]; projections deferred for Source_Test.md.'
    with patch('vector_lake.mcp_server._read_payload', return_value=_source_content()), patch('vector_lake.mutation_coordinator.execute_mutation_plan', return_value=(True, message)):
        result = mcp_server.write_wiki_page('Source_Test.md', str(payload))
        assert result.startswith(message)
        assert 'Index completion pending verification' in result


def test_status_registered_and_delegates_without_writes():
    assert 'get_mutation_status' in mcp_server.registered_tool_names()
    response = {'read_only': True, 'completed': False, 'state': 'error'}
    with patch('vector_lake.mutation_receipts.get_mutation_status', return_value=response) as observe:
        assert mcp_server.get_mutation_status([7], 3) == response
        observe.assert_called_once_with([7], max_hops=3)


def test_gc_safe_legacy_defaults_and_additive_plan_forwarding():
    with patch('vector_lake.mcp_server.tools.gc_vector_lake', return_value='native') as gc:
        assert mcp_server.gc_vector_lake() == 'native'
        gc.assert_called_once_with(days=30, dry_run=True, force=False)
        gc.reset_mock()
        plan = {'versions': {'Source_Test': 'native-token'}}
        assert mcp_server.gc_vector_lake(10, False, True, domain='General', cluster='test', expected_plan=plan) == 'native'
        gc.assert_called_once_with(days=10, dry_run=False, force=True, domain='General', cluster='test', expected_plan=plan)


@pytest.mark.parametrize('kwargs', [{}, {'domain': 'General'}, {'expected_plan': {}}])
def test_unsafe_destructive_gc_intentionally_rejected(kwargs):
    with patch('vector_lake.mcp_server.tools.gc_vector_lake') as gc:
        assert mcp_server.gc_vector_lake(dry_run=False, **kwargs).startswith('Error:')
        gc.assert_not_called()


@pytest.mark.parametrize('old,new', [('Old', 'New'), ('[[Old]]', 'New'), ('', '[[New]]'), ('---', '[[New]]'), ('[[Old]]', '[[ ]]\n')])
def test_arbitrary_text_or_structural_replacement_rejected_before_scan(old, new):
    with patch('vector_lake.mutation_receipts.readonly_snapshot') as read:
        assert mcp_server.batch_replace_links(old, new).startswith('Error:')
        read.assert_not_called()


def test_unscoped_destructive_replacement_rejected():
    assert 'explicit domain' in mcp_server.batch_replace_links('[[Old]]', '[[New]]', False)


def test_replaces_only_actual_link_syntax():
    before = ('---\ntitle: "[[Old]]"\n---\nOld [[Older]] [[Old|label]] '
              '[[Old]] [supports::[[Old]]]\n`[[Old]]` \\[[Old]] <!-- [[Old]] -->\n'
              '```md\n[[Old]]\n````\n~~~md\n[[Old]]\n~~~\n'
              '``a ` literal [[Old]]`` [[Old]]\n')
    after = mcp_server._replace_link_syntax(before, '[[Old]]', '[[New]]')
    expected = before.replace('[[Old]] [supports::[[Old]]]', '[[New]] [supports::[[New]]]')
    assert after == expected.replace('`` [[Old]]', '`` [[New]]')


def seed(memory):
    _write_purpose_contract(memory)
    content = _source_content().replace('Primary source content.', '[[Old]] and Old and `[[Old]]`.')
    mutation_coordinator.execute_mutation_plan('Source_Test.md', content=content)
    return content


def test_scoped_native_replace_passes_cas_and_preserves_receipt(isolated_memory, monkeypatch):
    original = seed(isolated_memory)
    hidden = original.replace('id: source_test', 'id: source_hidden').replace('domain: General', 'domain: Medical_IT')
    mutation_coordinator.execute_mutation_plan('Source_Hidden.md', content=hidden)
    real_execute = mutation_coordinator.execute_mutation_batch
    captured = []
    def execute(mutations, **kwargs):
        captured.extend(mutations)
        assert callable(kwargs['canonical_precondition'])
        return real_execute(mutations, **kwargs)
    monkeypatch.setattr(mutation_coordinator, 'execute_mutation_batch', execute)
    preview = mcp_server.batch_replace_links('[[Old]]', '[[New]]', domain='General')
    assert '1 files' in preview and (isolated_memory / 'wiki' / 'Source_Test.md').read_text(encoding='utf-8') == original
    result = mcp_server.batch_replace_links('[[Old]]', '[[New]]', False, domain='General')
    assert 'Canonical mutation batch committed' in result and 'outbox ids' in result
    assert captured[0]['expected_version']
    assert len(captured) == 1 and captured[0]['filename'] == 'Source_Test.md'
    assert captured[0]['content'].endswith('[[New]] and Old and `[[Old]]`.\n')
    assert (isolated_memory / 'wiki' / 'Source_Hidden.md').read_text(encoding='utf-8') == hidden


@pytest.mark.parametrize('conflict', ['canonical', 'new_scoped_page', 'markdown'])
def test_stale_scan_aborts_atomic_batch_with_zero_link_writes(isolated_memory, monkeypatch, conflict):
    original = seed(isolated_memory)
    real_execute = mutation_coordinator.execute_mutation_batch
    before = db_store.get_connection().execute('SELECT COUNT(*) FROM mutation_outbox').fetchone()[0]
    def race(mutations, **kwargs):
        if conflict == 'canonical':
            entity = next(item for item in governance_store.load_entities()['items'].values() if item['page_key'] == 'Source_Test')
            entity['raw_text'] = 'concurrent canonical state'
            governance_store.upsert_entity(entity['entity_id'], entity)
        elif conflict == 'new_scoped_page':
            entity = next(item for item in governance_store.load_entities()['items'].values() if item['page_key'] == 'Source_Test').copy()
            entity.update(entity_id='other', page_key='Source_Other')
            governance_store.upsert_entity('other', entity)
        else:
            (isolated_memory / 'wiki' / 'Source_Test.md').write_text(original + 'concurrent markdown', encoding='utf-8')
        return real_execute(mutations, **kwargs)
    monkeypatch.setattr(mutation_coordinator, 'execute_mutation_batch', race)
    result = mcp_server.batch_replace_links('[[Old]]', '[[New]]', False, domain='General')
    assert result.startswith('Error during scoped link replacement:') and 'conflict' in result
    assert db_store.get_connection().execute('SELECT COUNT(*) FROM mutation_outbox').fetchone()[0] == before
    assert '[[New]]' not in (isolated_memory / 'wiki' / 'Source_Test.md').read_text(encoding='utf-8')


def test_unreadable_projection_aborts_not_partial_success(isolated_memory):
    seed(isolated_memory)
    (isolated_memory / 'wiki' / 'Source_Test.md').unlink()
    result = mcp_server.batch_replace_links('[[Old]]', '[[New]]', False, domain='General')
    assert result.startswith('Error during scoped link replacement: FileNotFoundError')
    assert db_store.get_connection().execute('SELECT COUNT(*) FROM mutation_outbox').fetchone()[0] == 1
