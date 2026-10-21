"""Runtime/shape checks for the eight scoped MCP typing findings; synthetic only."""
import asyncio
import inspect
from contextlib import nullcontext

import pytest

from types import NoneType, SimpleNamespace
from typing import get_args, get_type_hints
from unittest.mock import patch

from vector_lake import governance_service, mcp_server, tool_review, tool_timeline


class AwaitableListing:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def collect():
            return self.value
        return collect().__await__()


class LegacySequence:
    def __getitem__(self, index):
        if index == 0:
            return SimpleNamespace(name='legacy-item')
        raise IndexError(index)


def test_sync_legacy_sequence_still_materializes():
    server = SimpleNamespace(list_tools=lambda: LegacySequence())
    assert mcp_server.registered_tool_names(server) == ['legacy-item']


def test_generic_awaitable_is_awaited_via_owned_coroutine():
    server = SimpleNamespace(list_tools=lambda: AwaitableListing([SimpleNamespace(name='safe-item')]))
    assert mcp_server.registered_tool_names(server) == ['safe-item']


def test_standard_coroutine_listing_still_works():
    async def listing():
        return [SimpleNamespace(name='coroutine-item')]
    assert mcp_server.registered_tool_names(SimpleNamespace(list_tools=listing)) == ['coroutine-item']


def test_active_loop_closes_unawaited_listing_without_nested_run():
    made = []
    async def listing():
        return [SimpleNamespace(name='not-executed')]
    def get_listing():
        value = listing()
        made.append(value)
        return value
    async def check():
        assert mcp_server.registered_tool_names(SimpleNamespace(list_tools=get_listing)) == []
    asyncio.run(check())
    assert inspect.getcoroutinestate(made[0]) == inspect.CORO_CLOSED


def test_noniterable_listing_preserves_fallback_behavior():
    assert mcp_server.registered_tool_names(SimpleNamespace(list_tools=lambda: object())) == []


def test_timeline_nullable_annotation_matches_existing_none_forwarding():
    hints = get_type_hints(tool_timeline.search_timeline_events)
    assert NoneType in get_args(hints['entity_name'])
    assert NoneType in get_args(hints['action'])
    with patch('vector_lake.mcp_server.search_timeline_events', return_value='stub') as delegate:
        assert mcp_server.search_timeline() == 'stub'
    delegate.assert_called_once_with(entity_name=None, action=None, limit=10)


def test_review_none_manifest_contract_is_not_changed():
    for function in [tool_review.review_vector_lake, tool_review._resolve_combined_item,
                     governance_service.resolve_governance_item, governance_service._resolve_locked]:
        assert NoneType in get_args(get_type_hints(function)['change_manifest'])
    with patch('vector_lake.mcp_server.tools.review_vector_lake', return_value='stub') as delegate:
        assert mcp_server.resolve_governance_item('item', 'skip') == 'stub'
    delegate.assert_called_once_with(action='resolve', index='item', resolution='skip', change_manifest=None)


def test_none_manifest_keeps_queue_session_and_delegate_contract():
    with patch('vector_lake.governance_service.governance_store.governance_queue_session',
               return_value=nullcontext()) as session, \
         patch('vector_lake.governance_service._resolve_locked', return_value={'item_id': 'item'}) as locked:
        assert governance_service.resolve_governance_item('item', 'skip', None) == {'item_id': 'item'}
    session.assert_called_once_with()
    locked.assert_called_once_with('item', 'skip', None)


def test_missing_merge_identity_still_refuses_without_mutation():
    queue = {'items': [{'item_id': 'item', 'status': 'pending', 'type': 'merge', 'merge_candidate': {}}]}
    with patch('vector_lake.governance_service.governance_store.load_governance_queue', return_value=queue), \
         patch('vector_lake.governance_service.execute_mutation_batch') as mutate:
        with pytest.raises(RuntimeError, match='requires both entity ids'):
            governance_service._resolve_locked('item', 'merge', None)
        mutate.assert_not_called()


def test_finalize_none_arguments_still_fail_before_delegate():
    hints = get_type_hints(mcp_server.finalize_ingest)
    assert NoneType in get_args(hints['files_written'])
    assert NoneType in get_args(hints['processed_data'])
    with patch('vector_lake.mcp_server.tools.finalize_ingest') as delegate:
        assert mcp_server.finalize_ingest() == 'Error: finalize_ingest requires a files list and processed-data object.'
        delegate.assert_not_called()


def test_visualize_default_and_explicit_none_preserve_runtime_dispatch():
    assert NoneType in get_args(get_type_hints(mcp_server.visualize_vector_lake)['output_dir'])
    assert NoneType in get_args(get_type_hints(mcp_server.tools.visualize_vector_lake)['output_dir'])
    with patch('vector_lake.mcp_server.tools.visualize_vector_lake', return_value='stub') as delegate:
        assert mcp_server.visualize_vector_lake() == 'stub'
        assert mcp_server.visualize_vector_lake(output_dir=None) == 'stub'
    assert delegate.call_count == 2
    assert all(c.args == (None,) for c in delegate.call_args_list)


def test_registered_surface_count_and_nullable_visualizer_schema():
    listed = asyncio.run(mcp_server.mcp.list_tools())
    assert len(listed) == 23
    assert any(tool.name == 'get_relations' for tool in listed)
    assert any(tool.name == 'get_mutation_status' for tool in listed)
    field = next(t for t in listed if t.name == 'visualize_vector_lake').parameters['properties']['output_dir']
    assert field['default'] is None
    assert any(v.get('type') == 'null' for v in field['anyOf'])
    evidence = next(t for t in listed if t.name == 'get_query_evidence')
    assert set(evidence.parameters['properties']) == {'query', 'node_keys', 'domain', 'cluster'}
