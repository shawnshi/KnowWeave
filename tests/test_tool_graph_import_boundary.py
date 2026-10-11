"""Optional-core absence must not conceal broken installed native libraries."""
import builtins
from pathlib import Path
import types

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'vector_lake' / 'tool_graph.py'


def _load_graph(core_or_error):
    original_import = builtins.__import__

    def scoped_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'vector_lake_core' and level == 0:
            if isinstance(core_or_error, BaseException):
                raise core_or_error
            return core_or_error
        return original_import(name, globals, locals, fromlist, level)

    namespace = {'__name__': 'tool_graph_import_probe',
                 '__builtins__': {**vars(builtins), '__import__': scoped_import}}
    exec(compile(SOURCE.read_text(encoding='utf-8'), str(SOURCE), 'exec'), namespace)
    return namespace


def test_exact_optional_core_absence_uses_fallback():
    namespace = _load_graph(ModuleNotFoundError('optional core absent', name='vector_lake_core'))
    assert namespace['HAVE_CORE'] is False
    assert namespace['vector_lake_core'] is None


def test_available_core_keeps_native_selection():
    core = types.ModuleType('vector_lake_core')
    namespace = _load_graph(core)
    assert namespace['HAVE_CORE'] is True
    assert namespace['vector_lake_core'] is core


@pytest.mark.parametrize('error', [
    ModuleNotFoundError('installed core missing a dependency', name='core_dependency'),
    ImportError('DLL load failed while importing installed core'),
], ids=['transitive-module-missing', 'native-dll-import-failure'])
def test_installed_core_failure_remains_visible(error):
    with pytest.raises(type(error)) as caught:
        _load_graph(error)
    assert caught.value is error
