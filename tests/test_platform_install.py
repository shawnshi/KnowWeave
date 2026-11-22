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


def test_windows_build_failure_preserves_context_and_redacts_proxy_credentials(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(INSTALL, 'sys', SimpleNamespace(platform='win32', executable=sys.executable))
    monkeypatch.setattr(INSTALL, 'CRATE', tmp_path)
    monkeypatch.setenv('HTTPS_PROXY', 'http://alice:super-secret@127.0.0.1:7898')
    stderr_lines = [
        'error: failed to get `pulldown-cmark` as a dependency of package `vector-lake-core v0.2.8`',
        'Caused by:',
        '  download of pu/ll/pulldown-cmark failed',
        'Caused by:',
        '  failed to download from https://index.crates.io/pu/ll/pulldown-cmark',
        'Caused by:',
        '  network error context line 7',
        '  network error context line 8',
        '  network error context line 9',
        '  network error context line 10',
        '  Caused by:',
        '    [7] Could not connect to server (Failed to connect to index.crates.io:443 over proxy 127.0.0.1 after 2017 ms)',
    ]

    def fail_cargo(argv, **kwargs):
        assert kwargs['encoding'] == 'utf-8'
        assert kwargs['errors'] == 'replace'
        return subprocess.CompletedProcess(argv, 101, '', '\n'.join(stderr_lines))

    monkeypatch.setattr(INSTALL.subprocess, 'run', fail_cargo)
    with pytest.raises(SystemExit) as failure:
        INSTALL.build()
    msg = str(failure.value)
    assert 'error: failed to get `pulldown-cmark`' in msg
    assert 'HTTPS_PROXY=http://<redacted>@127.0.0.1:7898' in msg
    assert 'super-secret' not in msg
    assert 'alice' not in msg
    assert os.environ['HTTPS_PROXY'] == 'http://alice:super-secret@127.0.0.1:7898'


def test_windows_build_offline_passes_offline_flag(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(INSTALL, 'sys', SimpleNamespace(platform='win32', executable=sys.executable))
    monkeypatch.setattr(INSTALL, 'CRATE', tmp_path)
    fake_dll = tmp_path / 'vector_lake_core.dll'
    fake_dll.write_bytes(b'fake-dll')
    monkeypatch.setattr(INSTALL, 'DLL', fake_dll)
    captured = []

    def run_cargo(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, '', '')

    monkeypatch.setattr(INSTALL.subprocess, 'run', run_cargo)
    assert INSTALL.build(offline=True) == fake_dll
    assert captured == [['cargo', '+stable-x86_64-pc-windows-msvc', 'build', '--release', '--locked', '--offline']]


def test_no_build_rejects_stale_artifact_unless_allowed(tmp_path, monkeypatch):
    crate_dir = tmp_path / 'crate'
    (crate_dir / 'src').mkdir(parents=True)
    for rel in ('Cargo.toml', 'Cargo.lock'):
        p = crate_dir / rel
        p.write_text('# stub\n', encoding='utf-8')
        os.utime(p, (1000, 1000))
    pyproject = crate_dir / 'pyproject.toml'
    pyproject.write_text('[project]\nname = "vector-lake-core"\nversion = "0.2.8"\n', encoding='utf-8')
    os.utime(pyproject, (1000, 1000))
    rs_file = crate_dir / 'src' / 'lib.rs'
    rs_file.write_text('// updated\n', encoding='utf-8')
    dll = crate_dir / 'target' / 'release' / 'vector_lake_core.dll'
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b'old-dll')
    os.utime(dll, (1000, 1000))
    os.utime(rs_file, (2000, 2000))

    assert INSTALL._stale_source_files(dll, crate_dir) == [Path('src/lib.rs')]

    monkeypatch.setattr(INSTALL, 'CRATE', crate_dir)
    monkeypatch.setattr(INSTALL, 'PYPROJECT', pyproject)
    monkeypatch.setattr(INSTALL, 'DLL', dll)
    installed = []
    activated = []
    monkeypatch.setattr(INSTALL, 'install', lambda d, v: installed.append((d, v)) or d)
    monkeypatch.setattr(INSTALL, 'activate', lambda v, check=True: activated.append(v))

    with pytest.raises(SystemExit) as failure:
        INSTALL.main(['--no-build'])
    msg = str(failure.value)
    assert 'stale build artifact' in msg
    assert 'src/lib.rs' in msg
    assert installed == []
    assert activated == []

    assert INSTALL.main(['--no-build', '--allow-stale']) == 0
    assert installed == [(dll, '0.2.8')]
    assert activated == ['0.2.8']
