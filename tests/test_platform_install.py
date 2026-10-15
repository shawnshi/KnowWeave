import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import pytest

SPEC = importlib.util.spec_from_file_location('platform_installer', Path(__file__).resolve().parents[1] / 'scripts/install_core.py')
if SPEC is None or SPEC.loader is None:
    raise ImportError('Native-installer module loader unavailable')
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


@pytest.mark.parametrize(('system', 'name'), [('win32', 'vector_lake_core.pyd'), ('darwin', 'vector_lake_core.so'), ('linux', 'vector_lake_core.so')])
def test_extension_names_match_python_loader(system, name):
    assert INSTALL.extension_filename(system) == name


@pytest.fixture
def unix_slot(tmp_path, monkeypatch):
    parent = tmp_path / 'site'
    package = parent / 'vector_lake_core'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('# original\n', encoding='utf-8')
    monkeypatch.setattr(INSTALL, 'site_packages', lambda: parent)
    monkeypatch.setattr(INSTALL, 'NATIVE_FILENAME', 'vector_lake_core.so', raising=False)
    return parent


def test_unix_slots_discover_install_activate_and_reject_overwrite(unix_slot, tmp_path):
    source = tmp_path / 'abi3.so'
    source.write_bytes(b'synthetic-not-executable')
    target = INSTALL.install(source, '0.2.5')
    assert target.name == 'vector_lake_core.so'
    assert INSTALL.installed_versions() == ['0.2.5']
    INSTALL.activate('0.2.5', check=False)
    pointer = unix_slot / 'vector_lake_core' / INSTALL.POINTER
    assert pointer.read_text(encoding='utf-8') == '0.2.5\n'
    original = target.read_bytes()
    source.write_bytes(b'different')
    with pytest.raises(SystemExit):
        INSTALL.install(source, '0.2.5')
    assert target.read_bytes() == original
    assert pointer.read_text(encoding='utf-8') == '0.2.5\n'


def test_real_host_binary_activates_only_in_isolated_site(tmp_path, monkeypatch):
    if os.environ.get('VL_CI_CORE_BACKEND') != 'native':
        pytest.skip('Requires explicit attested native CI lane; never use an ambient installation')
    import vector_lake_core as core
    source = Path(core.vector_lake_core.__file__).resolve()
    parent = tmp_path / 'isolated-site'
    (parent / 'vector_lake_core').mkdir(parents=True)
    monkeypatch.setattr(INSTALL, 'site_packages', lambda: parent)
    target = INSTALL.install(source, core.version())
    INSTALL.activate(core.version())
    assert target.name == INSTALL.extension_filename(sys.platform)
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest()
    assert INSTALL.installed_versions() == [core.version()]
    with pytest.raises(SystemExit):
        INSTALL.activate('0.0.0')
    assert (parent / 'vector_lake_core' / INSTALL.POINTER).read_text(encoding='utf-8').strip() == core.version()


@pytest.mark.parametrize('mode', ['ok', 'hash_mismatch', 'escape'])
def test_unix_builder_reuses_wheel_linker_and_validates_receipt(tmp_path, monkeypatch, mode):
    import json
    from types import SimpleNamespace
    output_binary = tmp_path / 'libvector_lake_core.so'
    output_binary.write_bytes(b'previous build')
    monkeypatch.setattr(INSTALL, 'DLL', output_binary)
    monkeypatch.setattr(INSTALL, 'sys', SimpleNamespace(platform='darwin', executable=sys.executable))

    def build_wheel(argv, **kwargs):
        assert argv[1] == str(INSTALL.REPO / 'scripts/ci_core.py')
        assert kwargs['check'] is True and kwargs['timeout'] == 960
        output = Path(argv[argv.index('--output') + 1])
        binary = output / 'python/vector_lake_core/vector_lake_core.abi3.so'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'wheel extension')
        name = '../../outside.so' if mode == 'escape' else 'vector_lake_core/vector_lake_core.abi3.so'
        digest = '0' * 64 if mode == 'hash_mismatch' else hashlib.sha256(binary.read_bytes()).hexdigest()
        (output / 'build-receipt.json').write_text(json.dumps({'native_hashes': {name: digest}}), encoding='utf-8')

    monkeypatch.setattr(INSTALL.subprocess, 'run', build_wheel)
    if mode == 'ok':
        assert INSTALL.build() == output_binary
        assert output_binary.read_bytes() == b'wheel extension'
    else:
        with pytest.raises(SystemExit):
            INSTALL.build()
        assert output_binary.read_bytes() == b'previous build'
