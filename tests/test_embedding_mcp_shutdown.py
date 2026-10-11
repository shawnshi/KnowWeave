"""Execute the actual CLI wrapper AST with fake MCP, never start a service."""
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vector_lake import embedding_scheduler as es


@pytest.mark.parametrize('failure', [None, RuntimeError('synthetic run failure'), KeyboardInterrupt()])
def test_mcp_main_closes_in_finally_normal_and_exception(monkeypatch, failure):
    source = Path(es.__file__).with_name('mcp_server.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    events = []

    def run(**kwargs):
        assert kwargs == {'show_banner': False}
        events.append('run')
        if failure:
            raise failure

    monkeypatch.setattr(es, 'start_prewarm_thread', lambda: events.append('prewarm'))
    monkeypatch.setattr(es, 'shutdown_embedding_clients', lambda: events.append('shutdown'))
    namespace: dict[str, Any] = {'mcp': SimpleNamespace(run=run)}
    # Execute trusted local wrapper only; importing the service is unnecessary here.
    exec(compile(ast.Module(body=[main], type_ignores=[]), str(source), 'exec'), namespace)  # noqa: S102
    if failure:
        with pytest.raises(type(failure)):
            namespace['main']()
    else:
        namespace['main']()
    assert events == ['prewarm', 'run', 'shutdown']


def test_prewarm_start_exception_also_shuts_down(monkeypatch):
    tree = ast.parse(Path(es.__file__).with_name('mcp_server.py').read_text(encoding='utf-8'))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    events = []

    def fail():
        raise RuntimeError('synthetic startup')

    monkeypatch.setattr(es, 'start_prewarm_thread', fail)
    monkeypatch.setattr(es, 'shutdown_embedding_clients', lambda: events.append('shutdown'))
    namespace: dict[str, Any] = {'mcp': SimpleNamespace(run=lambda **_kwargs: pytest.fail('run after startup failure'))}
    exec(compile(ast.Module(body=[main], type_ignores=[]), '<actual-main>', 'exec'), namespace)  # noqa: S102 - trusted local AST
    with pytest.raises(RuntimeError, match='synthetic startup'):
        namespace['main']()
    assert events == ['shutdown']
