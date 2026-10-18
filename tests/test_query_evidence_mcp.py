"""Optional strict MCP facade; synthetic fixtures only, never a weaker fallback."""
import asyncio
from contextlib import contextmanager
import inspect
import json
from types import SimpleNamespace

import pytest

from vector_lake import query_evidence as evidence
from vector_lake import query_evidence_host as host
from tests.test_query_evidence import published


def request(**kw):
    return json.loads(host.configured_query_evidence('agent harness', ['Source_test'],
                                                    domain='Artificial_Intelligence', **kw))


def test_optional_registered_schema_has_only_model_inputs():
    from vector_lake import mcp_server, tools
    registered = asyncio.run(mcp_server.mcp.list_tools())
    tool = next(t for t in registered if t.name == 'get_query_evidence')
    assert set(tool.parameters['properties']) == {'query', 'node_keys', 'domain', 'cluster'}
    assert set(tool.parameters['required']) == {'query', 'node_keys', 'domain'}
    assert set(inspect.signature(mcp_server.get_query_evidence).parameters) == {'query', 'node_keys', 'domain', 'cluster'}
    assert 'get_query_evidence' in tools.__all__
    assert tools.get_query_evidence is host.configured_query_evidence


def test_mcp_forwards_only_arguments_and_normalizes_empty_cluster(monkeypatch):
    from vector_lake import mcp_server
    seen = []
    monkeypatch.setattr(mcp_server.tools, 'get_query_evidence', lambda *a, **k: seen.append((a, k)) or 'safe')
    assert mcp_server.get_query_evidence('agent', ['Source_test'], 'Artificial_Intelligence') == 'safe'
    assert seen == [(('agent', ['Source_test']), {'domain': 'Artificial_Intelligence', 'cluster': None})]
    with pytest.raises(TypeError):
        getattr(mcp_server, 'get_query_evidence')('agent', ['Source_test'], 'Artificial_Intelligence', canonical_connection={})


def test_configured_host_keeps_exact_binding_and_no_canonical_dml(published, monkeypatch):
    from vector_lake import db_store, wiki_utils
    root, conn, add = published
    add(text='\ufeffauthor: Fixture\r\n\r\nAgent harness 医院😀。\r\n')
    before = list(conn.iterdump())
    def forbidden(*a, **k):
        pytest.fail('borrowed writer/path discovery is forbidden')
    monkeypatch.setattr(db_store, 'get_connection', forbidden)
    monkeypatch.setattr(wiki_utils, 'get_meta_dir', forbidden)
    result = request()
    assert result['status'] == 'complete'
    node = result['evidence'][0]
    assert node['status'] == 'verified'
    raw = (root / node['source']).read_bytes()
    for span in node['passages']:
        assert raw[span['byte_start']:span['byte_end']].decode('utf-8') == span['text']
    assert list(conn.iterdump()) == before


def test_missing_canonical_binding_never_reads_or_downgrades(published, monkeypatch):
    published[2](source_hash=None, source_frontmatter=None)
    monkeypatch.setattr(evidence, '_read_raw', lambda *a, **k: pytest.fail('No implicit unbound original read'))
    result = request()
    assert result['status'] == 'failed'
    assert result['evidence'] == [{'node_key': 'Source_test', 'status': 'refused', 'code': 'binding_unproven'}]


def test_nonstandard_db_override_refused_before_sqlite(published, monkeypatch):
    root, _, _ = published
    monkeypatch.setenv('VECTOR_LAKE_DB_PATH', str(root / 'other.db'))
    monkeypatch.setattr(host.sqlite3, 'connect', lambda *a, **k: pytest.fail('Alternate DB must not open'))
    result = request()
    assert result['evidence'][0]['code'] == 'nonstandard_canonical_location_unsupported'
    assert not (root / 'other.db').exists()


def test_exact_fixed_db_override_is_supported(published, monkeypatch):
    root, _, add = published
    add()
    monkeypatch.setenv('VECTOR_LAKE_DB_PATH', str(root / 'wiki' / '.meta' / 'vector_lake.db'))
    assert request()['status'] == 'complete'


@pytest.mark.parametrize('domain,keys', [('', ['Source_test']), ('General', ['Source_test']),
                                      ('Medical_IT', ['Source_x?token=not-a-real-secret']),
                                      ('Artificial_Intelligence', ['../Source_test'])])
def test_bad_arguments_fail_before_configuration(domain, keys, monkeypatch):
    monkeypatch.setattr(evidence, '_configuration_snapshot', lambda: pytest.fail('bad-input config read'))
    with pytest.raises(ValueError):
        host.configured_query_evidence('agent', keys, domain=domain)


def test_config_failure_type_does_not_leak_path_or_message(monkeypatch):
    def fail():
        raise RuntimeError('private-root-CANARY token=not-a-real-secret')
    monkeypatch.setattr(evidence, '_configuration_snapshot', fail)
    result = request()
    assert result['evidence'][0]['code'] == 'RuntimeError'
    assert 'CANARY' not in json.dumps(result)
    assert 'not-a-real-secret' not in json.dumps(result)


def test_unsupported_platform_does_not_touch_configuration(monkeypatch):
    monkeypatch.setattr(host, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(evidence, '_configuration_snapshot', lambda: pytest.fail('unsupported-platform config read'))
    assert request()['evidence'][0]['code'] == 'safe_read_capability_unavailable'


def test_bootstrap_deadline_is_not_reset_in_owned_snapshot(published, monkeypatch):
    deadlines = []
    observed = []
    original = evidence._Budget
    class RecordingBudget(original):
        def __init__(self, context=None):
            super().__init__(context)
            deadlines.append(self.deadline)
    @contextmanager
    def refuse_snapshot(self, context, cleanup_errors=None):
        observed.append(context.deadline)
        raise evidence.EvidenceRefused('synthetic_deadline_probe')
        yield  # contextmanager protocol; no connection or data access.
    monkeypatch.setattr(evidence, '_Budget', RecordingBudget)
    monkeypatch.setattr(host.ReadOnlyEvidenceHost, '_snapshot', refuse_snapshot)
    assert request()['evidence'][0]['code'] == 'synthetic_deadline_probe'
    assert observed == deadlines[:1]


def test_host_context_cannot_extend_existing_deadline(published, monkeypatch):
    root, _, _ = published
    owner = host.ReadOnlyEvidenceHost(root, {'exclude_paths': [], 'supported_extensions': ['.md']})
    short = host.time.monotonic() + 1
    assert owner._context(None, deadline=short).deadline == short
    start = host.time.monotonic()
    assert owner._context(None, deadline=start + 1000).deadline <= start + 15.1
