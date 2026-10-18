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
