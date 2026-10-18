import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile
import pytest
import yaml

SPEC = importlib.util.spec_from_file_location('native_bundle', Path(__file__).resolve().parents[1] / 'scripts/native_bundle.py')
if SPEC is None or SPEC.loader is None:
    raise ImportError('Native bundle module loader unavailable')
NATIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE)


@pytest.fixture
def native_build(tmp_path):
    directory = tmp_path / 'build'
    native = directory / 'python/vector_lake_core/vector_lake_core.pyd'
    native.parent.mkdir(parents=True)
    native.write_bytes(b'synthetic-not-executable')
    wheel = directory / 'wheels/vector_lake_core-0.2.8-cp38-abi3-win_amd64.whl'
    wheel.parent.mkdir()
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('vector_lake_core/__init__.py', 'from .vector_lake_core import *\n')
        archive.writestr('vector_lake_core/vector_lake_core.pyd', native.read_bytes())
        archive.writestr('vector_lake_core-0.2.8.dist-info/METADATA', 'Metadata-Version: 2.1\nName: vector_lake_core\nVersion: 0.2.8\n')
    receipt = {'source_hashes': NATIVE.ci_core._crate_inputs(), 'maturin': '1.15.0', 'cargo_locked': True,
               'native_hashes': {'vector_lake_core/vector_lake_core.pyd': NATIVE.digest(native)}, 'wheel_sha256': NATIVE.digest(wheel)}
    (directory / 'build-receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
    return directory


@pytest.fixture
def ready_bundle(native_build, tmp_path):
    output = tmp_path / 'bundle'
    NATIVE.bundle(native_build, output)
    return output


def test_export_validates_payload_and_refuses_existing_output(native_build, ready_bundle):
    manifest, wheel = NATIVE.validate_bundle(ready_bundle)
    assert manifest['version'] == '0.2.8' and wheel.suffix == '.whl'
    assert manifest['distribution_clearance'].startswith('not_granted')
    before = wheel.read_bytes()
    with pytest.raises(FileExistsError):
        NATIVE.bundle(native_build, ready_bundle)
    assert wheel.read_bytes() == before


@pytest.mark.parametrize('fault', ['wheel_hash', 'source', 'native_hash', 'path', 'schema_bool'])
def test_bundle_faults_fail_before_install(ready_bundle, monkeypatch, fault):
    path = ready_bundle / 'native-bundle.json'
    manifest = json.loads(path.read_text())
    if fault == 'wheel_hash': manifest['wheel_sha256'] = '0' * 64
    if fault == 'source': manifest['source_hashes'] = {}
    if fault == 'native_hash': manifest['native_hashes'] = {'vector_lake_core/vector_lake_core.pyd': '0' * 64}
    if fault == 'path': manifest['wheel'] = '../outside.whl'
    if fault == 'schema_bool': manifest['schema_version'] = True
    path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(NATIVE.subprocess, 'run', lambda *a, **k: pytest.fail('Invalid bundle reached an installer'))
    with pytest.raises(ValueError):
        NATIVE.install(ready_bundle)


@pytest.mark.parametrize('fault', ['escape', 'hook', 'dependency', 'symlink'])
def test_unsafe_wheel_rejected_before_code_execution(tmp_path, fault):
    wheel = tmp_path / 'vector_lake_core-0.2.8-cp38-abi3-win_amd64.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('vector_lake_core-0.2.8.dist-info/METADATA', 'Name: vector_lake_core\nVersion: 0.2.8\n' + ('Requires-Dist: example\n' if fault == 'dependency' else ''))
        archive.writestr('vector_lake_core/vector_lake_core.pyd', b'not-executable')
        if fault == 'escape': archive.writestr('../escape', 'unsafe')
        if fault == 'hook': archive.writestr('vector_lake_core/hook.pth', 'unsafe')
        if fault == 'symlink':
            entry = zipfile.ZipInfo('vector_lake_core/link');entry.external_attr = 0o120777 << 16
            archive.writestr(entry, 'outside')
    with pytest.raises(ValueError):
        NATIVE.inspect_wheel(wheel)


def test_global_python_is_never_targeted(monkeypatch):
    monkeypatch.setattr(NATIVE.sys, 'prefix', NATIVE.sys.base_prefix)
    with pytest.raises(RuntimeError, match='NEW project venv'):
        NATIVE.fresh_prefix()


def test_existing_core_is_never_replaced(tmp_path, monkeypatch):
    prefix = tmp_path / 'venv';site = prefix / 'site';site.mkdir(parents=True)
    monkeypatch.setattr(NATIVE.sys, 'prefix', str(prefix))
    monkeypatch.setattr(NATIVE.sysconfig, 'get_path', lambda _: str(site))
    monkeypatch.setattr(NATIVE.importlib.metadata, 'distribution', lambda _: SimpleNamespace())
    with pytest.raises(RuntimeError, match='already installed'):
        NATIVE.fresh_prefix()


def test_install_is_offline_hashed_binary_only_and_never_calls_a_compiler(ready_bundle, tmp_path, monkeypatch):
    prefix = tmp_path / 'new-venv';prefix.mkdir()
    monkeypatch.setattr(NATIVE, 'fresh_prefix', lambda: prefix)
    monkeypatch.setenv('PIP_TARGET', str(tmp_path / 'forbidden'))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['check'] is True
        assert kwargs['env']['PIP_CONFIG_FILE'] == NATIVE.os.devnull and 'PIP_TARGET' not in kwargs['env']
        if '-m' in argv:
            for flag in ['--isolated', '--no-index', '--no-deps', '--only-binary=:all:', '--require-hashes', '--no-compile']:
                assert flag in argv
            assert argv[argv.index('--prefix') + 1] == str(prefix)
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(stdout=json.dumps({'version': '0.2.8', 'prefix': str(prefix)}))
    monkeypatch.setattr(NATIVE.subprocess, 'run', run)
    result = NATIVE.install(ready_bundle)
    assert result['compiler_invoked'] is False and len(calls) == 2
    assert all(not any(word in Path(arg).name for word in ['cargo', 'rustc', 'maturin']) for argv in calls for arg in argv)


def test_failed_pip_never_generates_a_successful_receipt(ready_bundle, tmp_path, monkeypatch):
    prefix = tmp_path / 'new-venv';prefix.mkdir()
    monkeypatch.setattr(NATIVE, 'fresh_prefix', lambda: prefix)
    monkeypatch.setattr(NATIVE.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, a[0])))
    receipt = tmp_path / 'receipt.json';receipt.write_text('{"status":"old_success"}')
    with pytest.raises(subprocess.CalledProcessError):
        NATIVE.main(['install', '--bundle', str(ready_bundle), '--receipt', str(receipt)])
    assert not receipt.exists()


def test_receipt_cannot_modify_immutable_bundle(ready_bundle):
    with pytest.raises(ValueError, match='outside the immutable bundle'):
        NATIVE.main(['install', '--bundle', str(ready_bundle), '--receipt', str(ready_bundle / 'native-bundle.json')])
    assert NATIVE.validate_bundle(ready_bundle)[0]['version'] == '0.2.8'


def test_prebuilt_workflow_compiles_but_never_auto_publishes():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/native-wheels.yml').read_text(encoding='utf-8'))
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['on']['workflow_dispatch'] == {}
    steps = workflow['jobs']['wheel']['steps']
    assert {s['uses'] for s in steps if 'uses' in s} == {'actions/checkout@v4', 'actions/setup-python@v5'}
    assert '--only-binary=:all:' in '\n'.join(s.get('run', '') for s in steps)
    assert not any('publish' in s.get('run', '') or 'gh release' in s.get('run', '') for s in steps)
