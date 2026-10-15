"""Prebuilt core bundles: build machines package; fresh user venvs install offline, never compile."""
from __future__ import annotations
import argparse
import email.parser
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_core

PACKAGE = 'vector-lake-core'
MAX_WHEEL = 100 * 1024 * 1024


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(stream.read()).hexdigest()


def source_version() -> str:
    text = (ci_core.CRATE / 'Cargo.toml').read_text(encoding='utf-8')
    matches = re.findall(r'^version\s*=\s*"(\d+\.\d+\.\d+)"', text, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError('Expected one final native-core source version')
    return matches[0]


def inspect_wheel(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > MAX_WHEEL or not re.fullmatch(r'vector_lake_core-[A-Za-z0-9_.+-]+\.whl', path.name):
        raise ValueError('Expected one bounded vector_lake_core wheel')
    with zipfile.ZipFile(path) as wheel:
        entries = wheel.infolist()
        if len(entries) > 256 or len({e.filename for e in entries}) != len(entries) or sum(e.file_size for e in entries) > 400 * 1024 * 1024:
            raise ValueError('Wheel size/entry limits or duplicate paths')
        for entry in entries:
            name = PurePosixPath(entry.filename)
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if name.is_absolute() or '..' in name.parts or '\\' in entry.filename or kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError('Unsafe wheel path/type')
            if not name.parts or not (name.parts[0] == 'vector_lake_core' or re.fullmatch(r'vector_lake_core-[0-9.]+\.dist-info', name.parts[0])) or name.suffix == '.pth':
                raise ValueError('Unexpected wheel package, data directory or interpreter hook')
        metadata = [e for e in entries if e.filename.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or metadata[0].file_size > 65536:
            raise ValueError('Expected bounded wheel metadata')
        info = email.parser.BytesParser().parsebytes(wheel.read(metadata[0]))
        version = info.get('Version', '')
        if str(info.get('Name', '')).lower().replace('_', '-') != PACKAGE or version != source_version() or info.get_all('Requires-Dist'):
            raise ValueError('Wheel project/version/dependency mismatch')
        natives = {e.filename: hashlib.sha256(wheel.read(e)).hexdigest() for e in entries if PurePosixPath(e.filename).suffix in {'.pyd', '.so'}}
        if len(natives) != 1 or not next(iter(natives)).startswith('vector_lake_core/'):
            raise ValueError('Expected one native payload in wheel')
    return {'version': version, 'native_hashes': natives}


def bundle(native_dir: Path, output: Path) -> dict:
    receipt = ci_core._validate_native(native_dir.resolve())
    wheels = list((native_dir / 'wheels').glob('*.whl'))
    if len(wheels) != 1 or digest(wheels[0]) != receipt['wheel_sha256']:
        raise ValueError('Built wheel is missing, ambiguous or differs from build receipt')
    info = inspect_wheel(wheels[0])
    if info['native_hashes'] != receipt['native_hashes']:
        raise ValueError('Wheel payload differs from tested build receipt')
    output.mkdir(parents=True, exist_ok=False)
    target = output / wheels[0].name
    shutil.copy2(wheels[0], target)
    if digest(target) != receipt['wheel_sha256']:
        raise ValueError('Copied wheel hash mismatch')
    manifest = {'schema_version': 1, 'package': PACKAGE, **info, 'wheel': target.name,
                'wheel_sha256': digest(target), 'source_hashes': receipt['source_hashes'],
                'distribution_clearance': 'not_granted; testing/private-local-use bundle only',
                'publisher_authentication': 'not established by SHA; obtain bundle from a trusted owner'}
    (output / 'native-bundle.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


def validate_bundle(directory: Path) -> tuple[dict, Path]:
    directory = directory.resolve()
    manifest_path = directory / 'native-bundle.json'
    if not manifest_path.resolve().is_relative_to(directory) or manifest_path.stat().st_size > 262144:
        raise ValueError('Unsafe or oversized bundle manifest')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError('Malformed native-bundle.json') from exc
    if not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1 or manifest.get('package') != PACKAGE:
        raise ValueError('Unsupported bundle manifest')
    if manifest.get('source_hashes') != ci_core._crate_inputs():
        raise ValueError('Bundle Rust inputs do not match this checkout; obtain its matching prebuilt bundle')
    name = manifest.get('wheel')
    if not isinstance(name, str) or not re.fullmatch(r'vector_lake_core-[A-Za-z0-9_.+-]+\.whl', name):
        raise ValueError('Unsafe wheel basename')
    wheel = (directory / name).resolve()
    if not wheel.is_relative_to(directory) or digest(wheel) != manifest.get('wheel_sha256'):
        raise ValueError('Bundle wheel path/hash mismatch')
    info = inspect_wheel(wheel)
    if manifest.get('version') != info['version'] or manifest.get('native_hashes') != info['native_hashes']:
        raise ValueError('Bundle wheel/native metadata mismatch')
    return manifest, wheel


def fresh_prefix() -> Path:
    prefix = Path(sys.prefix).resolve()
    site = Path(sysconfig.get_path('platlib')).resolve()
    if prefix == Path(sys.base_prefix).resolve() or not site.is_relative_to(prefix):
        raise RuntimeError('Use a NEW project venv; global/system Python installation is forbidden')
    try:
        importlib.metadata.distribution(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError('Core already installed; create a new venv, never replace a possibly loaded DLL')
    if list(site.glob('vector_lake_core*')) or importlib.util.find_spec('vector_lake_core') is not None:
        raise RuntimeError('Existing/unmanaged core detected; create a new venv')
    return prefix


def install(directory: Path) -> dict:
    manifest, wheel = validate_bundle(directory)
    prefix = fresh_prefix()
    # Disable configuration as well as index/dependency lookup: a user/global
    # pip target, index, proxy or source-build option must not redirect this install.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('PIP_')}
    env['PIP_CONFIG_FILE'] = os.devnull
    with tempfile.TemporaryDirectory(prefix='.native-wheel-', dir=prefix) as temporary:
        requirements = Path(temporary) / 'native.txt'
        requirements.write_text(wheel.as_uri() + ' --hash=sha256:' + manifest['wheel_sha256'] + '\n', encoding='utf-8')
        command = [sys.executable, '-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-input', '--no-cache-dir',
                   'install', '--no-index', '--no-deps', '--only-binary=:all:', '--require-hashes', '--no-compile', '--prefix', str(prefix), '-r', str(requirements)]
        subprocess.run(command, env=env, check=True, timeout=120)
    native_sha = next(iter(manifest['native_hashes'].values()))
    code = """import hashlib,json,pathlib,sys,vector_lake_core as core
p=pathlib.Path(core.vector_lake_core.__file__).resolve()
if core.version()!=sys.argv[1] or not p.is_relative_to(pathlib.Path(sys.prefix).resolve()) or hashlib.sha256(p.read_bytes()).hexdigest()!=sys.argv[2]:
 raise RuntimeError('Installed native identity mismatch')
if ''.join(core.cut('合成 ABI check'))!='合成 ABI check':
 raise RuntimeError('Installed native Unicode probe failed')
print(json.dumps({'version':core.version(),'native_file':str(p),'native_sha256':sys.argv[2],'prefix':sys.prefix}))
"""
    probe = subprocess.run([sys.executable, '-I', '-B', '-c', code, manifest['version'], native_sha], env=env, check=True, capture_output=True, text=True, timeout=30)
    try:
        identity = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Installed native probe returned malformed JSON; do not activate this venv') from exc
    if not isinstance(identity, dict):
        raise RuntimeError('Installed native probe returned an unexpected identity; do not activate this venv')
    return {'status': 'prebuilt_core_installed', 'compiler_invoked': False, 'index_or_dependency_lookup': False,
            'wheel_sha256': manifest['wheel_sha256'], 'identity': identity,
            'scope': 'fresh venv core only; no service activation, runtime dependency install or production acceptance'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    package = commands.add_parser('bundle', help='Build/CI operator exports a validated ci_core build; never overwrites a bundle')
    package.add_argument('--native-dir', type=Path, required=True)
    package.add_argument('--output', type=Path, required=True)
    user = commands.add_parser('install', help='User installs a trusted local prebuilt bundle; no Rust/maturin required')
    user.add_argument('--bundle', type=Path, required=True)
    user.add_argument('--receipt', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'install' and args.receipt:
        if args.receipt.resolve().is_relative_to(args.bundle.resolve()):
            raise ValueError('Receipt must be outside the immutable bundle')
        args.receipt.unlink(missing_ok=True)
    result = bundle(args.native_dir, args.output) if args.command == 'bundle' else install(args.bundle)
    if args.command == 'install' and args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
