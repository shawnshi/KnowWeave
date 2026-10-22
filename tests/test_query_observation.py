import json

import pytest

from vector_lake import search_ledger, tool_query


@pytest.fixture
def query_context(isolated_memory, tmp_path, monkeypatch):
    marker = 'SYNTHETIC_BODY_NOT_FOR_LEDGER'
    root = tmp_path / 'extension'
    root.mkdir()
    monkeypatch.setenv('VECTOR_LAKE_SEARCH_LEDGER', '1')
    monkeypatch.setattr(tool_query, 'get_extension_root', lambda: root)
    monkeypatch.setattr(tool_query, 'read_template', lambda *args, **kwargs: 'Payload {{payload_path}} for {{query_str}}')
    monkeypatch.setattr(tool_query, 'assemble_context', lambda *args, **kwargs: {
        'memory_packet': marker, 'memory_count': 1, 'memory_warning_count': 0,
        'memory_omitted_count': 0, 'wiki_context': 'synthetic source', 'wiki_page_count': 1,
        'index_summary': '', 'purpose': '', 'retrieval_notes': [], 'budget_used': 100,
        'budget_max': 4096})
    return root, marker


def test_preview_observes_context_stages_without_payload_write(query_context):
    root, marker = query_context
    assert marker in tool_query.preview_query_context('synthetic query', domain='Medical_IT')
    event = search_ledger.entries()[-1]
    assert event['timings_ms']['query_context_assembly'] >= 0
    assert event['timings_ms']['query_context_render'] >= 0
    assert event.get('cache_events', {}).get('query_payload_write', 0) == 0
    assert not (root / 'tmp').exists()
    assert marker not in search_ledger.ledger_path().read_text(encoding='utf-8')


@pytest.mark.parametrize('dry_run', [True, False])
def test_both_query_modes_report_actual_payload_writes(query_context, dry_run):
    root, marker = query_context
    result = tool_query.prepare_query_context('synthetic query', dry_run=dry_run, domain='Medical_IT')
    payloads = list((root / 'tmp').glob('query_context_*.md'))
    assert len(payloads) == 1 and marker in payloads[0].read_text(encoding='utf-8')
    assert result.startswith('[DRY RUN]') is dry_run
    event = search_ledger.entries()[-1]
    assert event['timings_ms']['query_context_assembly'] >= 0
    assert event['timings_ms']['query_context_render'] >= 0
    assert event['timings_ms']['query_payload_write'] >= 0
    assert event['cache_events']['query_payload_write'] == 1
    assert event['cache_events']['query_payload_bytes'] == payloads[0].stat().st_size
    assert marker not in json.dumps(event)


def test_payload_write_failure_keeps_the_error_and_no_success_counter(query_context, monkeypatch):
    from pathlib import Path
    root, _ = query_context
    # The writer uses builtins.open; deny that exact target rather than ledger I/O.
    import builtins
    real_open = builtins.open
    def deny(file, mode='r', *args, **kwargs):
        if Path(file).parent == root / 'tmp' and mode.startswith('w'):
            raise PermissionError('synthetic write refusal')
        return real_open(file, mode, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', deny)
    with pytest.raises(PermissionError):
        tool_query.prepare_query_context('synthetic query', domain='Medical_IT')
    event = search_ledger.entries()[-1]
    assert event['status'] == 'failed'
    assert event.get('cache_events', {}).get('query_payload_write', 0) == 0


def test_payload_metadata_failure_does_not_replace_success(query_context, monkeypatch):
    from pathlib import Path
    original = Path.stat
    def stat(self, *args, **kwargs):
        if self.name.startswith('query_context_'):
            raise OSError('synthetic metadata refusal')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    assert tool_query.prepare_query_context('synthetic query', domain='Medical_IT')
    event = search_ledger.entries()[-1]
    assert event['status'] == 'complete'
    assert event['cache_events']['query_payload_write'] == 1
    assert 'query_payload_bytes' not in event['cache_events']
