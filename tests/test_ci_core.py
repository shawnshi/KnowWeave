"""CI must identify core fallback/native explicitly instead of silently passing."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_plugin_state():
    """``scripts/ci_core.py`` keeps its probe state in a module global.

    The tests here drive it both in-process (as the pytest plugin) and in subprocesses, so a test
    that leaves ``_STATE`` behind changes what the next one observes -- which is how
    ``test_reusing_successful_probe_invalidates_it_before_native_failure`` failed in a full run and
    passed alone.  Snapshot and restore around every test instead of relying on file order.
    """
    module = sys.modules.get("ci_core")
    if module is None:
        sys.path.insert(0, str(REPO / "scripts"))
        import ci_core as module  # noqa: F401
    before = json.loads(json.dumps(getattr(module, "_STATE", {}) or {}))
    try:
        yield
    finally:
        state = getattr(module, "_STATE", None)
        if isinstance(state, dict):
            state.clear()
            state.update(before)
SCRIPT = REPO / 'scripts' / 'ci_core.py'


def test_workflow_runs_both_platforms_and_explicit_backends():
    workflow = yaml.safe_load((REPO / '.github/workflows/test.yml').read_text(encoding='utf-8'))
    jobs = list(workflow['jobs'].values())
    matrices = [job.get('strategy', {}).get('matrix', {}) for job in jobs]
    assert any(set(matrix.get('os', [])) == {'windows-2025', 'ubuntu-26.04', 'macos-26', 'macos-26-intel'} and set(matrix.get('backend', [])) == {'native', 'fallback'} and set(matrix.get('python', [])) == {'3.10', '3.13'} for matrix in matrices)
    assert workflow['permissions'] == {'contents': 'read'}
    assert all(job['strategy']['max-parallel'] == 2 for job in jobs)
    commands = '\n'.join(step.get('run', '') for job in jobs for step in job['steps'])
    assert 'scripts/ci_core.py build' in commands and 'scripts/ci_core.py test' in commands
    assert '--require-hashes' in commands and '--no-deps' in commands
    for job in jobs:
        setup = next(step for step in job['steps'] if step.get('uses', '').startswith('actions/setup-python@'))
        assert setup['with']['python-version'] == '${{ matrix.python }}'
    build_requirements = (REPO / 'scripts/ci-build-requirements.txt').read_text(encoding='utf-8')
    assert 'tomli==2.3.0 ; python_version < "3.11"' in build_requirements
    assert '--hash=sha256:e95b1af3c5b07d9e643909b5abbec77cd9f1217e6d0bca72b0234736b9fb1f1b' in build_requirements


def test_workflow_uses_runner_context_only_in_step_environment():
    # GitHub context-availability table excludes runner from jobs.<job_id>.env.
    workflow = yaml.safe_load((REPO / '.github/workflows/test.yml').read_text(encoding='utf-8'))
    for job in workflow['jobs'].values():
        assert all('runner.' not in str(value) for value in job.get('env', {}).values())
        steps = job['steps']
        native = next(step for step in steps if 'ci_core.py build' in step.get('run', ''))
        test = next(step for step in steps if 'ci_core.py test' in step.get('run', ''))
        assert native['env']['CI_NATIVE_DIR'] == test['env']['CI_NATIVE_DIR']
        assert 'runner.temp' in test['env']['CI_CORE_RECEIPT']


def test_ci_native_gate_exists():
    assert SCRIPT.is_file(), 'current CI never requires a freshly built native core'


def _run(tmp_path, backend, *, native_dir=None):
    assert SCRIPT.is_file()
    probe = tmp_path / 'test_isolated_gate.py'
    probe.write_text('from vector_lake import indexer\ndef test_backend():\n    assert indexer.HAVE_CORE is ' + str(backend == 'native') + '\n', encoding='utf-8')
    command = [sys.executable, '-B', str(SCRIPT), 'test', '--backend', backend, '--receipt', str(tmp_path / 'probe.json')]
    if native_dir is not None:
        command.extend(['--native-dir', str(native_dir)])
    command.extend(['--', '-q', str(probe)])
    env = dict(os.environ)
    # A subprocess must not inherit a parent full-shard selection plugin.
    env.pop('PYTEST_ADDOPTS', None)
    return subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=30)


def test_core_fallback_is_real_even_when_host_has_a_native_module(tmp_path):
    result = _run(tmp_path, 'fallback')
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / 'probe.json').read_text(encoding='utf-8'))
    assert receipt['start']['backend'] == receipt['end']['backend'] == 'fallback'
    assert not receipt['start']['HAVE_CORE'] and not receipt['end']['loaded_core_modules']
    assert receipt['exit_code'] == 0


@pytest.mark.parametrize('backend', ['fallback', 'native'])
def test_native_marker_partition_is_explicit_and_never_filters_native_gate(monkeypatch, backend):
    import ci_core
    class Item:
        def __init__(self, name, native):
            self.nodeid, self.native = name, native
        def get_closest_marker(self, name):
            assert name == 'requires_native_core'
            return self.native
    class Config:
        def __init__(self):
            self.hook = self
            self.deselected = []
        def pytest_deselected(self, items):
            self.deselected.extend(item.nodeid for item in items)
    items = [Item('portable', False), Item('native-only', True)]
    config = Config()
    monkeypatch.setenv('VL_CI_CORE_BACKEND', backend)
    ci_core.pytest_collection_modifyitems(config, items)
    assert ci_core._STATE['collected_nodeids'] == ['portable', 'native-only']
    assert ci_core._STATE['native_only_nodeids'] == ['native-only']
    assert config.deselected == (['native-only'] if backend == 'fallback' else [])
    assert [item.nodeid for item in items] == (['portable'] if backend == 'fallback' else ['portable', 'native-only'])


def test_native_only_deselection_has_an_auditable_subprocess_receipt(tmp_path):
    probe = tmp_path / 'test_partition.py'
    probe.write_text('import pytest\nfrom vector_lake import indexer\ndef test_portable():\n    assert indexer.HAVE_CORE is False\n@pytest.mark.requires_native_core\ndef test_actual_native():\n    assert indexer.HAVE_CORE and indexer.vector_lake_core is not None\n', encoding='utf-8')
    receipt = tmp_path / 'partition.json'
    env = dict(os.environ)
    env.pop('PYTEST_ADDOPTS', None)
    result = subprocess.run([sys.executable, '-B', str(SCRIPT), 'test', '--backend', 'fallback', '--receipt', str(receipt), '--', '-q', str(probe)], cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(receipt.read_text(encoding='utf-8'))
    assert len(data['collected_nodeids']) == 2
    assert len(data['selected_nodeids']) == len(data['backend_deselected_nodeids']) == 1
    assert data['backend_deselected_nodeids'] == data['native_only_nodeids']
    assert data['selected_nodeids'][0].endswith('::test_portable')
    assert data['backend_deselected_nodeids'][0].endswith('::test_actual_native')
    assert data['start']['HAVE_CORE'] is data['end']['HAVE_CORE'] is False


def test_reusing_successful_probe_invalidates_it_before_native_failure(tmp_path):
    success = _run(tmp_path, 'fallback')
    assert success.returncode == 0
    assert json.loads((tmp_path / 'probe.json').read_text())['exit_code'] == 0
    failure = _run(tmp_path, 'native', native_dir=tmp_path / 'missing-native')
    assert failure.returncode != 0
    assert not (tmp_path / 'probe.json').exists(), 'stale successful probe survived failed native preflight'


def test_plain_python_cli_disables_bytecode_in_current_interpreter(tmp_path):
    probe = tmp_path / 'test_bytecode_flag.py'
    probe.write_text('import sys\ndef test_flag():\n    assert sys.dont_write_bytecode is True\n', encoding='utf-8')
    env = dict(os.environ)
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    env.pop('PYTEST_ADDOPTS', None)
    # Red execution cannot pollute repository or host caches even without -B.
    env['PYTHONPYCACHEPREFIX'] = str(tmp_path / 'isolated-cache')
    result = subprocess.run([sys.executable, str(SCRIPT), 'test', '--backend', 'fallback', '--receipt', str(tmp_path / 'probe.json'), '--', '-q', str(probe)], cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('shape', ['missing', 'empty', 'forged'])
def test_native_lane_fails_closed_instead_of_using_host_fallback(tmp_path, shape):
    directory = tmp_path / 'native'
    if shape != 'missing':
        directory.mkdir()
    if shape == 'forged':
        (directory / 'build-receipt.json').write_text('{}', encoding='utf-8')
    result = _run(tmp_path, 'native', native_dir=directory)
    assert result.returncode != 0
    assert not (tmp_path / 'probe.json').exists()


@pytest.mark.parametrize('fault', ['source_changed', 'binary_changed', 'path_escape'])
def test_native_gate_rejects_well_formed_but_stale_or_unsafe_receipts(tmp_path, fault):
    crate = REPO / 'crates' / 'vector_lake_core'
    sources = sorted((crate / 'src').glob('*.rs')) + [crate / name for name in ('Cargo.toml', 'Cargo.lock', 'pyproject.toml')]
    hashes = {path.relative_to(REPO).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    directory = tmp_path / 'native'
    python = directory / 'python'
    python.mkdir(parents=True)
    binary = python / 'fake.pyd'
    binary.write_bytes(b'not executable; rejected before any import')
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if fault == 'source_changed':
        hashes[next(iter(hashes))] = '0' * 64
    if fault == 'binary_changed':
        digest = '0' * 64
    name = '../escape.pyd' if fault == 'path_escape' else 'fake.pyd'
    receipt = {'source_hashes': hashes, 'native_hashes': {name: digest}, 'maturin': '1.15.0', 'cargo_locked': True}
    (directory / 'build-receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
    result = _run(tmp_path, 'native', native_dir=directory)
    assert result.returncode != 0 and not (tmp_path / 'probe.json').exists()


def test_ci_declares_hash_pinned_numpy_test_dependency_for_all_lanes():
    text = (REPO / 'scripts' / 'ci-test-requirements.txt').read_text(encoding='utf-8')
    assert 'numpy==2.2.6' in text and text.count('--hash=sha256:') == 12
    workflow = (REPO / '.github' / 'workflows' / 'test.yml').read_text(encoding='utf-8')
    assert '--no-deps --require-hashes -r scripts/ci-test-requirements.txt' in workflow
    assert workflow.index('-r scripts/ci-test-requirements.txt') < workflow.index('python scripts/ci_core.py test')
    assert (REPO / 'requirements.txt').read_text(encoding='utf-8').find('numpy') == -1


def test_fixed_ci_build_tool_does_not_add_runtime_dependencies():
    text = (REPO / 'scripts' / 'ci-build-requirements.txt').read_text(encoding='utf-8')
    assert 'maturin==1.15.0' in text and text.count('--hash=sha256:') == 5
    assert 'tomli==2.3.0 ; python_version < "3.11"' in text
    assert 'patchelf' not in text and 'ziglang' not in text
