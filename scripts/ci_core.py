"""Task-local native build and fail-closed pytest backend probe (never a runtime service)."""
from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import zipfile

REPO = Path(__file__).resolve().parents[1]
CRATE = REPO / 'crates' / 'vector_lake_core'
_STATE: dict = {}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _crate_inputs() -> dict[str, str]:
    paths = sorted((CRATE / 'src').glob('*.rs')) + [CRATE / name for name in ('Cargo.toml', 'Cargo.lock', 'pyproject.toml')]
    return {path.relative_to(REPO).as_posix(): _digest(path) for path in paths}


def _build(output: Path, offline: bool, timeout: float) -> None:
    if importlib.metadata.version('maturin') != '1.15.0':
        raise RuntimeError('CI build requires audited maturin 1.15.0')
    output.mkdir(parents=True, exist_ok=False)  # never reuse an old wheel or publish globally
    before = _crate_inputs()
    env = dict(os.environ, CARGO_TARGET_DIR=str(output / 'cargo-target'), PYTHONDONTWRITEBYTECODE='1')
    for name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'VECTOR_LAKE_DB_PATH'):
        env[name] = ''
    command = [sys.executable, '-m', 'maturin', 'build', '--release', '--locked', '--manifest-path', str(CRATE / 'Cargo.toml'), '--out', str(output / 'wheels')]
    if offline:
        command.append('--offline')
    process = subprocess.Popen(command, cwd=REPO, env=env, start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=True, timeout=10)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        raise
    if code:
        raise subprocess.CalledProcessError(code, command)
    if before != _crate_inputs():
        raise RuntimeError('Crate inputs changed during the native build')
    wheels = list((output / 'wheels').glob('*.whl'))
    if len(wheels) != 1:
        raise RuntimeError('Expected exactly one freshly built wheel')
    python_root = output / 'python'
    python_root.mkdir()
    with zipfile.ZipFile(wheels[0]) as wheel:
        for entry in wheel.infolist():
            if not (python_root / entry.filename).resolve().is_relative_to(python_root.resolve()):
                raise ValueError('Wheel member escapes task-local extraction root')
        wheel.extractall(python_root)
    binaries = [path for path in python_root.rglob('*') if path.is_file() and path.suffix in {'.pyd', '.so'}]
    if len(binaries) != 1:
        raise RuntimeError('Expected exactly one native library in wheel')
    receipt = {'source_hashes': before, 'wheel_sha256': _digest(wheels[0]), 'native_hashes': {path.relative_to(python_root).as_posix(): _digest(path) for path in binaries}, 'maturin': '1.15.0', 'cargo_locked': True, 'offline': offline}
    (output / 'build-receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt))


def _validate_native(directory: Path) -> dict:
    receipt = json.loads((directory / 'build-receipt.json').read_text(encoding='utf-8'))
    if receipt['source_hashes'] != _crate_inputs() or receipt['maturin'] != '1.15.0' or receipt['cargo_locked'] is not True:
        raise RuntimeError('Native build receipt does not match this checkout')
    if len(receipt['native_hashes']) != 1:
        raise RuntimeError('Native receipt must identify one library')
    root = (directory / 'python').resolve()
    for name, expected in receipt['native_hashes'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path.suffix not in {'.pyd', '.so'} or _digest(path) != expected:
            raise RuntimeError('Task-local native binary hash/path mismatch')
    return receipt


class _BlockCore(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'vector_lake_core' or fullname.startswith('vector_lake_core.'):
            raise ModuleNotFoundError('CI deliberately selects core fallback', name=fullname)
        return None


def _snapshot() -> dict:
    from vector_lake import indexer, tool_search
    mode = os.environ['VL_CI_CORE_BACKEND']
    loaded = sorted(name for name in sys.modules if name == 'vector_lake_core' or name.startswith('vector_lake_core.'))
    if mode == 'fallback':
        if indexer.HAVE_CORE or tool_search.HAVE_CORE or loaded:
            raise RuntimeError('Fallback lane unexpectedly loaded vector_lake_core')
        return {'backend': mode, 'HAVE_CORE': False, 'loaded_core_modules': loaded}
    receipt = _validate_native(Path(os.environ['VL_CI_NATIVE_DIR']))
    import vector_lake_core as core
    root = (Path(os.environ['VL_CI_NATIVE_DIR']) / 'python').resolve()
    actual = Path(core.vector_lake_core.__file__).resolve()
    if not indexer.HAVE_CORE or indexer.vector_lake_core is not core or not tool_search.HAVE_CORE or tool_search.vector_lake_core is not core or not actual.is_relative_to(root):
        raise RuntimeError('Native lane used fallback or an unrelated installed core')
    relative = actual.relative_to(root).as_posix()
    if _digest(actual) != receipt['native_hashes'].get(relative):
        raise RuntimeError('Loaded native library differs from freshly built library')
    required = ('fast_calculate_weighted_edges', 'version', 'fast_personalized_pagerank', 'prepared_personalized_pagerank', 'PprIndex', 'fast_bm25_rerank')
    if not all(callable(getattr(core, name, None)) for name in required):
        raise RuntimeError('Required checkout graph/PPR ABI is missing; native tests must not silently skip')
    return {'backend': mode, 'HAVE_CORE': True, 'native_path': str(actual), 'sha256': _digest(actual), 'version': core.version(), 'loaded_core_modules': loaded}


def pytest_configure(config):
    config.addinivalue_line('markers', 'requires_native_core: requires the real checkout-built core in the native CI gate')


def pytest_sessionstart(session):
    _STATE['start'] = _snapshot()


def pytest_runtest_logreport(report):
    for name, value in report.user_properties:
        if name.startswith('ci_core_'):
            _STATE.setdefault('semantic_observations', {})[name] = value


def pytest_collection_modifyitems(config, items):
    """Native-only contracts run in the mandatory native gate, never against a fake core."""
    _STATE['collected_nodeids'] = [item.nodeid for item in items]
    native_only = [item for item in items if item.get_closest_marker('requires_native_core')]
    _STATE['native_only_nodeids'] = [item.nodeid for item in native_only]
    deselected = native_only if os.environ['VL_CI_CORE_BACKEND'] == 'fallback' else []
    _STATE['backend_deselected_nodeids'] = [item.nodeid for item in deselected]
    if deselected:
        ids = {item.nodeid for item in deselected}
        items[:] = [item for item in items if item.nodeid not in ids]
        config.hook.pytest_deselected(items=deselected)


def pytest_collection_finish(session):
    _STATE['selected_nodeids'] = [item.nodeid for item in session.items]


def pytest_sessionfinish(session, exitstatus):
    _STATE['end'] = _snapshot()
    _STATE['exit_code'] = int(exitstatus)
    Path(os.environ['VL_CI_CORE_RECEIPT']).write_text(json.dumps(_STATE, indent=2), encoding='utf-8')
    summary = {key: value for key, value in _STATE.items() if not key.endswith('_nodeids')}
    summary['collection_counts'] = {key: len(value) for key, value in _STATE.items() if key.endswith('_nodeids')}
    print('\nCI core backend probe: ' + json.dumps(summary))


def main(argv=None) -> int:
    sys.dont_write_bytecode = True  # setting the environment later cannot change this interpreter
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build')
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--offline', action='store_true')
    build.add_argument('--timeout', type=float, default=900)
    test = sub.add_parser('test')
    test.add_argument('--backend', choices=('native', 'fallback'), required=True)
    test.add_argument('--native-dir', type=Path)
    test.add_argument('--receipt', type=Path, required=True)
    test.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == 'build':
        if not 0 < args.timeout <= 1200:
            parser.error('Build timeout must be between 0 and 1200 seconds')
        _build(args.output.resolve(), args.offline, args.timeout)
        return 0
    args.receipt = args.receipt.resolve()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.unlink(missing_ok=True)  # invalidate any previous success before all backend checks
    if args.backend == 'native':
        if args.native_dir is None:
            parser.error('Native lane requires --native-dir, never implicit host fallback')
        _validate_native(args.native_dir.resolve())
        os.environ['VL_CI_NATIVE_DIR'] = str(args.native_dir.resolve())
        sys.path.insert(0, str((args.native_dir / 'python').resolve()))
    else:
        sys.meta_path.insert(0, _BlockCore())
        os.environ.pop('VL_CI_NATIVE_DIR', None)
    bootstrap = args.receipt.parent / 'bootstrap-memory'
    bootstrap.mkdir(exist_ok=True)
    os.environ.update({'VL_CI_CORE_BACKEND': args.backend, 'VL_CI_CORE_RECEIPT': str(args.receipt), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'VECTOR_LAKE_MEMORY_DIR': str(bootstrap)})
    for name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'VECTOR_LAKE_DB_PATH'):
        os.environ[name] = ''
    sys.path.insert(0, str(REPO))
    import pytest
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
    return int(pytest.main(['-p', 'no:cacheprovider', *pytest_args], plugins=[sys.modules[__name__]]))


if __name__ == '__main__':
    raise SystemExit(main())
