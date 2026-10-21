"""No production reads, no expected-source execution, no source/loaded conflation."""
import json
import sys
import types
from pathlib import Path

import pytest


def _fake(tmp_path, monkeypatch, body):
    path = tmp_path / 'unit.py'
    path.write_text(body, encoding='utf-8')
    mod = types.ModuleType('probe_fixture')
    mod.__file__ = str(path)
    exec(compile(body, str(path), 'exec'), mod.__dict__)
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    return mod, path


def test_code_shape_supports_python310_missing_optional_attributes():
    from vector_lake import runtime_identity as ri
    code = compile('x = 1', '<fixture>', 'exec')
    class LegacyCode:
        def __getattr__(self, name):
            if name in ('co_exceptiontable', 'co_qualname'):
                raise AttributeError(name)
            return getattr(code, name)
    shape = ri._code_shape(LegacyCode())
    assert shape['exceptiontable'] == '' and shape['qualname'] == code.co_name


def test_loaded_bytecode_mismatch_is_not_reported_as_current_disk(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    mod, path = _fake(tmp_path, monkeypatch, 'def sample():\n    return 1\n')
    first = ri._module_record(mod.__name__, path, ('sample',))
    assert first['status'] == 'matches_function_code'
    path.write_text('def sample():\n    return 2\n', encoding='utf-8')
    changed = ri._module_record(mod.__name__, path, ('sample',))
    assert changed['status'] == 'function_code_mismatch'
    assert changed['source_sha256'] != first['source_sha256']
    assert changed['functions']['sample']['loaded_code_sha256'] == first['functions']['sample']['loaded_code_sha256']
    assert mod.sample() == 1


def test_probe_never_executes_current_source(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    mod, path = _fake(tmp_path, monkeypatch, 'def sample():\n    return 1\n')
    path.write_text('probe_must_not_execute()\ndef sample():\n    return 1\n', encoding='utf-8')
    assert ri._module_record(mod.__name__, path, ('sample',))['status'] == 'matches_function_code'


def test_not_loaded_and_wrong_path_fail_honestly(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    path = tmp_path / 'absent.py'
    assert ri._module_record('not_loaded_fixture', path, ('sample',))['status'] == 'not_loaded'
    mod, real = _fake(tmp_path, monkeypatch, 'def sample():\n    return 1\n')
    assert ri._module_record(mod.__name__, path, ('sample',))['status'] == 'module_origin_mismatch'
    assert not path.exists()


def test_unreadable_or_invalid_current_source_is_not_healthy(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    mod, path = _fake(tmp_path, monkeypatch, 'def sample():\n    return 1\n')
    path.write_text('def invalid syntax', encoding='utf-8')
    report = ri._module_record(mod.__name__, path, ('sample',))
    assert report['status'] == 'source_unverifiable' and report['error_type'] == 'SyntaxError'
    path.unlink()
    assert ri._module_record(mod.__name__, path, ('sample',))['status'] == 'source_unverifiable'


def test_wrapped_function_code_and_lineno_only_changes(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    mod, path = _fake(tmp_path, monkeypatch, 'from contextlib import contextmanager\n@contextmanager\ndef sample():\n    yield 1\n')
    assert ri._module_record(mod.__name__, path, ('sample',))['status'] == 'matches_function_code'
    path.write_text('\n\n'+path.read_text(), encoding='utf-8')
    assert ri._module_record(mod.__name__, path, ('sample',))['status'] == 'matches_function_code'


def test_missing_function_and_globals_are_not_overclaimed(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    mod, path = _fake(tmp_path, monkeypatch, 'LIMIT = 1\ndef sample():\n    return LIMIT\n')
    assert ri._module_record(mod.__name__, path, ('absent',))['status'] == 'function_code_mismatch'
    mod.LIMIT = 2
    assert ri._module_record(mod.__name__, path, ('sample',))['status'] == 'matches_function_code'
    assert 'globals' in ri.runtime_identity()['scope_limitations']


def test_live_report_json_and_native_identity_are_observations():
    from vector_lake import runtime_identity as ri
    from vector_lake import db_store, indexer
    report = ri.runtime_identity()
    json.dumps(report)
    assert report['schema_version'] == 1 and report['pid'] > 0
    assert report['modules']['vector_lake.db_store']['status'] == 'matches_function_code'
    assert report['modules']['vector_lake.indexer']['status'] == 'matches_function_code'
    if indexer.HAVE_CORE:
        # The probe reports either a verified binary or an explicit reason why it could not be
        # verified -- never a silent "loaded".  It used to demand 'loaded', which encoded how this
        # host installed the wheel rather than the contract: an install whose extension is reachable
        # through two origins (a package plus its nested binary) is reported 'unverifiable' with
        # that reason, and that is a truthful observation, not a regression.
        assert report['native']['status'] in {'loaded', 'unverifiable'}
        if report['native']['status'] == 'loaded':
            assert len(report['native']['on_disk_binary_sha256']) == 64
            assert report['native']['version'] == indexer.vector_lake_core.version()
        else:
            assert report['native'].get('reason') or report['native'].get('error_type')
    else:
        assert report['native']['status'] == 'not_loaded'
    assert report['production_acceptance'] == 'not_established'


def test_missing_native_file_is_not_a_successful_identity(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    import importlib.machinery
    for name in list(sys.modules):
        if name == 'vector_lake_core' or name.startswith('vector_lake_core.'):
            monkeypatch.delitem(sys.modules, name)
    core = types.ModuleType('vector_lake_core')
    core.__file__ = str(tmp_path / ('missing' + importlib.machinery.EXTENSION_SUFFIXES[0]))
    core.version = lambda: 'fixture'
    monkeypatch.setitem(sys.modules, 'vector_lake_core', core)
    record = ri._native_record()
    assert record['status'] == 'unverifiable'
    assert record['error_type'] == 'FileNotFoundError'


def test_mcp_surface_is_readonly_structured_json():
    from vector_lake import mcp_server
    assert callable(mcp_server.runtime_identity)
    assert json.loads(mcp_server.runtime_identity())['production_acceptance'] == 'not_established'
    import ast
    tree = ast.parse(Path(mcp_server.__file__).read_text(encoding='utf-8'))
    for name in ('runtime_identity', 'doctor_vector_lake'):
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        assert len(function.decorator_list) == 1


DEPENDENCY_PROBES = {
    'vector_lake.wiki_utils': ('split_frontmatter', 'atomic_write_text', 'flush_durable', 'fsync_directory', 'entity_identity_key'),
    'vector_lake.yaml_utils': ('load_yaml', 'dump_yaml'),
    'vector_lake.link_resolution': ('build_link_map', 'resolve_link_target', 'core_name_maps', 'declaration_map', 'declared_names_from_nodes'),
    'vector_lake.relation_catalog': ('build_relation_catalog', 'identity_resolver', 'in_scope', 'node_field', 'validate_limit'),
    'vector_lake.relation_policy': ('predicate_weight', 'pair_relation_weight'),
    'vector_lake.tool_relations': ('get_relations',),
    'vector_lake.tool_graph': ('visualize_vector_lake', 'audit_graph', '_build_page_graph', '_build_graph_payload', '_extract_backbone_edges'),
}


@pytest.mark.parametrize('name,functions', DEPENDENCY_PROBES.items())
def test_dependency_probe_inventory_and_loaded_code_drift(tmp_path, monkeypatch, name, functions):
    from vector_lake import runtime_identity as ri
    assert set(functions) <= set(ri.MODULE_PROBES[name])
    body = ''.join(f'def {function}():\n    return 1\n' for function in functions)
    mod, path = _fake(tmp_path, monkeypatch, body)
    monkeypatch.setitem(sys.modules, name, mod)
    assert ri._module_record(name, path, functions)['status'] == 'matches_function_code'
    path.write_text(body.replace('return 1', 'return 2', 1), encoding='utf-8')
    assert ri._module_record(name, path, functions)['status'] == 'function_code_mismatch'


@pytest.mark.parametrize('name', DEPENDENCY_PROBES)
def test_missing_dependency_coverage_is_explicit_without_import(name, monkeypatch):
    from vector_lake import runtime_identity as ri
    monkeypatch.delitem(sys.modules, name, raising=False)
    report = ri.runtime_identity()
    assert report['modules'][name]['status'] == 'not_loaded'
    assert name not in sys.modules
    assert report['production_acceptance'] == 'not_established'


@pytest.mark.parametrize('name', ('vector_lake.mcp_server', 'vector_lake.watchdog_app', 'vector_lake.indexer'))
def test_known_main_host_is_observed_without_import(tmp_path, monkeypatch, name):
    from vector_lake import runtime_identity as ri
    monkeypatch.delitem(sys.modules, name, raising=False)
    functions = ri.MODULE_PROBES[name]
    body = ''.join(f'def {function}():\n    return 1\n' for function in functions)
    mod, path = _fake(tmp_path, monkeypatch, body)
    monkeypatch.setitem(sys.modules, '__main__', mod)
    record = ri._module_record(name, path, functions)
    assert record['status'] == 'matches_function_code'
    assert record['loaded_as'] == '__main__'
    assert name not in sys.modules
    alias = types.ModuleType(name)
    alias.__file__ = str(path)
    alias.__dict__.update(mod.__dict__)
    def stale():
        return 99
    alias.__dict__[functions[0]] = stale
    monkeypatch.setitem(sys.modules, name, alias)
    assert ri._module_record(name, path, functions)['loaded_as'] == '__main__'
    assert ri._module_record(name, path, functions)['status'] == 'matches_function_code'
    path.write_text(body.replace('return 1', 'return 2', 1), encoding='utf-8')
    assert ri._module_record(name, path, functions)['status'] == 'function_code_mismatch'


def test_unknown_main_origin_and_unlisted_host_are_not_claimed(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    mod, path = _fake(tmp_path, monkeypatch, 'def runtime_identity():\n    return 1\n')
    monkeypatch.setitem(sys.modules, '__main__', mod)
    monkeypatch.delitem(sys.modules, 'vector_lake.mcp_server', raising=False)
    assert ri._module_record('vector_lake.mcp_server', tmp_path / 'other.py', ('runtime_identity',))['status'] == 'not_loaded'
    assert ri._module_record('not_a_known_host', path, ('runtime_identity',))['status'] == 'not_loaded'


def test_explicit_public_constants_detect_drift_without_exposing_values(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    name = 'vector_lake.relation_policy'
    body = 'PRED_WEIGHT_TAXONOMY = frozenset({"parent"})\ndef sample():\n    return 1\n'
    mod, path = _fake(tmp_path, monkeypatch, body)
    monkeypatch.setitem(sys.modules, name, mod)
    record = ri._module_record(name, path, ('sample',), ('PRED_WEIGHT_TAXONOMY',))
    assert record['status'] == 'matches_function_code'
    assert record['constants']['PRED_WEIGHT_TAXONOMY']['matches'] is True
    mod.__dict__['PRED_WEIGHT_TAXONOMY'] = frozenset({'changed'})
    record = ri._module_record(name, path, ('sample',), ('PRED_WEIGHT_TAXONOMY',))
    assert record['status'] == 'constant_mismatch'
    assert record['constants']['PRED_WEIGHT_TAXONOMY']['matches'] is False
    assert 'changed' not in json.dumps(record) and 'parent' not in json.dumps(record)


def test_missing_or_dynamic_allowlisted_constant_is_unverifiable(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    name = 'vector_lake.tool_graph'
    body = 'DEFAULT_MAX_PAGE_EDGES = 5000\ndef sample():\n    return 1\n'
    mod, path = _fake(tmp_path, monkeypatch, body)
    monkeypatch.setitem(sys.modules, name, mod)
    constants = ('DEFAULT_MAX_PAGE_EDGES', 'DEFAULT_MAX_CLAIM_EDGES')
    record = ri._module_record(name, path, ('sample',), constants)
    assert record['constants']['DEFAULT_MAX_CLAIM_EDGES']['status'] == 'missing'
    assert record['status'] == 'constant_unverifiable'
    path.write_text(body.replace('5000', 'never_execute()'), encoding='utf-8')
    record = ri._module_record(name, path, ('sample',), constants)
    assert record['constants']['DEFAULT_MAX_PAGE_EDGES']['status'] == 'unverifiable'


def test_diagnostic_newline_equivalence_preserves_bom_and_lone_cr(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    mod, path = _fake(tmp_path, monkeypatch, 'def sample():\n    return 1\n')
    lf = b'def sample():\n    return 1\n'
    path.write_bytes(lf)
    first = ri._module_record(mod.__name__, path, ('sample',))
    path.write_bytes(lf.replace(b'\n', b'\r\n'))
    crlf = ri._module_record(mod.__name__, path, ('sample',))
    assert first['source_sha256'] != crlf['source_sha256']
    assert first['source_logical_sha256'] == crlf['source_logical_sha256']
    assert first['status'] == crlf['status'] == 'matches_function_code'
    path.write_bytes(b'\xef\xbb\xbf' + lf)
    bom = ri._module_record(mod.__name__, path, ('sample',))
    assert bom['source_logical_sha256'] != first['source_logical_sha256']
    path.write_bytes(lf.replace(b'\n', b'\r'))
    assert ri._module_record(mod.__name__, path, ('sample',))['source_logical_sha256'] != first['source_logical_sha256']


def test_diagnostics_do_not_change_legacy_canonical_versions():
    from tests.test_canonical_change_sets import _concept
    from vector_lake import governance_store
    from vector_lake import runtime_identity as ri
    content = _concept('Concept_Diagnostic')
    expected = {
        'LF': 'f62da621f3e618ca3e002a8067850b6194696899a49b23beed79ebfebefa9c55',
        'CRLF': '916aadada8507e63450ba5addb3ed589410d8f1dd41b43ca3addd31885bcc85f',
    }
    for label, text in [('LF', content), ('CRLF', content.replace('\n', '\r\n'))]:
        assert governance_store.canonical_page_version_from_content('Concept_Diagnostic.md', text) == expected[label]
    ri.runtime_identity()
    for label, text in [('LF', content), ('CRLF', content.replace('\n', '\r\n'))]:
        assert governance_store.canonical_page_version_from_content('Concept_Diagnostic.md', text) == expected[label]


def test_real_dependency_functions_and_literal_constants_match():
    import importlib

    from vector_lake import runtime_identity as ri
    names = set(DEPENDENCY_PROBES) | set(ri.CONSTANT_PROBES)
    for name in names:
        importlib.import_module(name)
    before = set(sys.modules)
    report = ri.runtime_identity()
    for name in names:
        record = report['modules'][name]
        assert record['status'] == 'matches_function_code', (name, record)
        assert all(value.get('matches') is True for value in record.get('constants', {}).values())
    assert set(sys.modules) == before


def test_runtime_report_uses_main_host_without_loading_server(tmp_path, monkeypatch):
    import builtins

    from vector_lake import runtime_identity as ri
    mod, path = _fake(tmp_path, monkeypatch, 'def runtime_identity():\n    return 1\n')
    host_path = tmp_path / 'mcp_server.py'
    path.rename(host_path)
    mod.__file__ = str(host_path)
    monkeypatch.setattr(ri, 'PACKAGE', tmp_path)
    monkeypatch.setitem(sys.modules, '__main__', mod)
    monkeypatch.delitem(sys.modules, 'vector_lake.mcp_server', raising=False)
    original_import = builtins.__import__
    def reject_server(name, *args, **kwargs):
        if name == 'vector_lake.mcp_server':
            raise AssertionError('Live server import is forbidden')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', reject_server)
    record = ri.runtime_identity()['modules']['vector_lake.mcp_server']
    assert record['status'] == 'matches_function_code'
    assert record['loaded_as'] == '__main__'
    assert 'vector_lake.mcp_server' not in sys.modules


def test_non_module_loaded_entry_and_missing_function_are_honest(tmp_path, monkeypatch):
    from vector_lake import runtime_identity as ri
    monkeypatch.setitem(sys.modules, 'invalid_fixture', object())
    assert ri._module_record('invalid_fixture', tmp_path / 'unit.py', ())['status'] == 'source_unverifiable'
    mod, path = _fake(tmp_path, monkeypatch, 'def sample():\n    return 1\n')
    record = ri._module_record(mod.__name__, path, ('absent',))
    assert record['functions']['absent']['status'] == 'missing'
    assert record['status'] == 'function_code_mismatch'
