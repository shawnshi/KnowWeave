#!/usr/bin/env python
"""Install a native-core build additively, so upgrading does not require stopping anything.

Why not just overwrite ``vector_lake_core/vector_lake_core.pyd`` (what was done twice on 2026-09-25):
Windows refuses to replace a loaded DLL, so every upgrade meant disabling the scheduled task, killing
the watchdog, the ingest runner and the MCP servers, installing, then restarting -- measured.  A PyO3
module cannot dodge this by renaming the file either: its init symbol is ``PyInit_<lib name>``, i.e.
fixed at build time, so the file must be called ``vector_lake_core.pyd``.

What works instead: keep one directory per build and let the *package directory* carry the version.

    site-packages/vector_lake_core/__init__.py      <- shim, selects one build (this script writes it)
    site-packages/vector_lake_core_0_2_0/vector_lake_core.pyd
    site-packages/vector_lake_core_0_1_0/vector_lake_core.pyd   (kept for rollback)

The loader matches the *last* path component (``vector_lake_core``) against the init symbol, so the
file inside may live in any versioned directory.  Installing is then a new file plus a pointer, which
touches nothing a running process holds open; consumers pick the new build up when they restart.

Usage:
    python scripts/install_core.py                 # build (MSVC) + install + activate
    python scripts/install_core.py --activate 0.1.0
    python scripts/install_core.py --list
    python scripts/install_core.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from filelock import FileLock

REPO = Path(__file__).resolve().parents[1]
CRATE = REPO / "crates" / "vector_lake_core"
def extension_filename(platform_name: str) -> str:
    return "vector_lake_core.pyd" if platform_name == "win32" else "vector_lake_core.so"


NATIVE_FILENAME = extension_filename(sys.platform)
DLL = CRATE / "target" / "release" / ("vector_lake_core.dll" if sys.platform == "win32" else "libvector_lake_core.so")
PYPROJECT = CRATE / "pyproject.toml"
POINTER = "_active_version.txt"

SHIM = '''"""Active native-core build.  Written by ``scripts/install_core.py`` -- do not edit by hand.

One directory per build (``vector_lake_core_<version>/vector_lake_core.pyd``), because a PyO3
extension's init symbol is fixed at build time and a loaded DLL cannot be replaced on Windows.  This
shim re-exports exactly one build, so upgrading is additive and rollback is one pointer change.

Selection order: ``VECTOR_LAKE_CORE_VERSION`` (explicit) -> ``_active_version.txt`` (what the
installer activated).  A named build that fails to import raises instead of silently falling back:
a quiet fallback to a different build is the failure mode this whole mechanism was built to stop.
"""
import importlib as _importlib
import os as _os
from pathlib import Path as _Path

_DEFAULT = "{default}"


def _selected() -> str:
    explicit = _os.environ.get("VECTOR_LAKE_CORE_VERSION", "").strip()
    if explicit:
        return explicit
    pointer = _Path(__file__).parent / "{pointer}"
    if pointer.exists():
        try:
            return pointer.read_text(encoding="utf-8").strip() or _DEFAULT
        except OSError:
            pass
    return _DEFAULT


_VERSION = _selected()
_MODULE = f"vector_lake_core_{{_VERSION.replace('.', '_')}}.vector_lake_core"
try:
    _impl = _importlib.import_module(_MODULE)
except Exception as exc:  # noqa: BLE001 - the message is the value here
    raise ImportError(
        f"vector_lake_core {{_VERSION}} is not importable from {{_MODULE!r}}: {{type(exc).__name__}}: {{exc}}. "
        "Installed builds: " + ", ".join(
            sorted(p.name for p in _Path(__file__).parent.parent.glob("vector_lake_core_*"))
        )
    ) from exc
core_version = _VERSION
globals().update({{k: v for k, v in vars(_impl).items() if not k.startswith("__")}})


def __getattr__(name):
    return getattr(_impl, name)
'''


def source_version() -> str:
    """The version in the crate's pyproject.toml -- the source of truth, not the installed one."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise SystemExit(f"no version in {PYPROJECT}")
    return match.group(1)


def site_packages() -> Path:
    import site

    for candidate in (*site.getsitepackages(), site.getusersitepackages()):
        path = Path(candidate) / "vector_lake_core"
        if path.exists():
            return Path(candidate)
    raise SystemExit("vector_lake_core is not installed in any site-packages; install the wheel first")


def build() -> Path:
    """Build the host extension without changing any installed version or pointer."""
    if sys.platform != "win32":
        # Maturin supplies macOS extension linking flags and the correct wheel ABI;
        # a bare cargo cdylib is not a portable Python extension build.
        with tempfile.TemporaryDirectory(prefix="vector-lake-core-build-") as directory:
            output = Path(directory) / "build"
            subprocess.run([sys.executable, str(REPO / "scripts" / "ci_core.py"), "build", "--output", str(output)], check=True, timeout=960)
            receipt = json.loads((output / "build-receipt.json").read_text(encoding="utf-8"))
            binaries = receipt["native_hashes"]
            if len(binaries) != 1:
                raise SystemExit("build receipt must identify one native extension")
            name, digest = next(iter(binaries.items()))
            root = (output / "python").resolve()
            binary = (root / name).resolve()
            if not binary.is_relative_to(root) or binary.suffix != ".so" or _file_digest(binary).hex() != digest:
                raise SystemExit("build receipt native path/hash mismatch")
            DLL.parent.mkdir(parents=True, exist_ok=True)
            _atomic_bytes(DLL, binary.read_bytes())
        return DLL
    toolchain = "stable-x86_64-pc-windows-msvc" if sys.platform == "win32" else None
    argv = ["cargo"]
    if toolchain:
        argv.append(f"+{toolchain}")
    argv += ["build", "--release"]
    print(f"building: {' '.join(argv)} (in {CRATE})")
    result = subprocess.run(argv, cwd=CRATE, capture_output=True, text=True)
    if result.returncode != 0:
        # The GNU toolchain's failure is not obvious from "linker failed"; say what to use.
        tail = (result.stderr or "").strip().splitlines()[-6:]
        raise SystemExit("build failed:\n  " + "\n  ".join(tail))
    if not DLL.exists():
        raise SystemExit(f"build reported success but {DLL} is missing")
    return DLL


def installed_versions() -> list[str]:
    """Dotted versions found on disk.

    The *directory* label must use underscores (``vector_lake_core_0_2_0``): the import system reads a
    dot in a path component as a separator, so ``vector_lake_core_0.2.0`` would not be importable as
    one package.  Callers think in dotted versions; the conversion lives here.
    """
    parent = site_packages()
    versions = [
        path.name.removeprefix("vector_lake_core_").replace("_", ".")
        for path in sorted(parent.glob("vector_lake_core_*"))
        if (path / NATIVE_FILENAME).is_file()
    ]
    return sorted(versions, key=lambda v: [int(part) for part in re.findall(r"\d+", v)] or [0])


def active_version() -> str | None:
    pointer = site_packages() / "vector_lake_core" / POINTER
    if pointer.exists():
        try:
            return pointer.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def _atomic_bytes(target: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _version_directory(parent: Path, version: str) -> Path:
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        raise SystemExit(f"invalid native-core version: {version!r}")
    return parent / f"vector_lake_core_{version.replace('.', '_')}"


def write_shim(default: str) -> Path:
    target = site_packages() / "vector_lake_core" / "__init__.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(target, SHIM.format(default=default, pointer=POINTER).encode("utf-8"))
    return target


def activate(version: str, *, check: bool = True) -> None:
    parent = site_packages()
    candidate = _version_directory(parent, version) / NATIVE_FILENAME
    with FileLock(str(parent / ".vector-lake-core-install.lock"), timeout=30):
        if not candidate.is_file():
            raise SystemExit(f"{version} is not installed; have: {installed_versions() or 'none'}")
        if check:
            module = f"vector_lake_core_{version.replace('.', '_')}.vector_lake_core"
            code = (
                "import importlib, pathlib, sys\n"
                f"sys.path.insert(0, {str(parent)!r})\n"
                f"c = importlib.import_module({module!r})\n"
                f"if pathlib.Path(c.__file__).resolve() != pathlib.Path({str(candidate)!r}).resolve():\n"
                "    raise RuntimeError('wrong candidate path')\n"
                f"if c.version() != {version!r}:\n"
                "    raise RuntimeError('candidate version mismatch')\n"
                "print(c.version(), getattr(c, 'blocks_contract', lambda: 'none')())\n"
            )
            probe = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                env=dict(os.environ, VECTOR_LAKE_CORE_VERSION=version), timeout=30,
            )
            if probe.returncode:
                raise SystemExit(f"activation probe failed for {version}: {probe.stderr.strip()[-300:]}")
        shim = parent / "vector_lake_core" / "__init__.py"
        previous_shim = shim.read_bytes() if shim.exists() else None
        # Keep the old default until the pointer publishes the checked candidate.
        # Failed publication must not change fresh consumers (VL-ARCH-01).
        try:
            write_shim(active_version() or version)
            _atomic_bytes(parent / "vector_lake_core" / POINTER, (version + "\n").encode("utf-8"))
        except OSError:
            if previous_shim is None:
                shim.unlink(missing_ok=True)
            else:
                _atomic_bytes(shim, previous_shim)
            raise
        print(f"activated {version}" + (f": {probe.stdout.strip()}" if check else " (unchecked)"))


def install(dll: Path, version: str) -> Path:
    """Copy the build into its own versioned directory -- a new file, never an overwrite."""
    parent = site_packages()
    target_dir = _version_directory(parent, version)
    target = target_dir / NATIVE_FILENAME
    with FileLock(str(parent / ".vector-lake-core-install.lock"), timeout=30):
        digest = _file_digest(dll)
        if target.exists():
            if _file_digest(target) != digest:
                raise SystemExit(f"{version} already has different bytes; use a new version")
            return target
        if target_dir.exists():
            raise SystemExit(f"{target_dir} is incomplete; preserve it and use a new version")
        staging = Path(tempfile.mkdtemp(prefix=".install-core-", dir=parent))
        try:
            staged = staging / NATIVE_FILENAME
            shutil.copy2(dll, staged)
            if _file_digest(staged) != digest:
                raise OSError("native build changed during installation")
            with staged.open("rb+") as handle:
                os.fsync(handle.fileno())
            staging.rename(target_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true", help="install the existing build artifact")
    parser.add_argument("--activate", metavar="VERSION", help="switch the pointer only")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--check", action="store_true", help="report the active build and exit")
    args = parser.parse_args()

    if args.list:
        print(f"installed: {installed_versions() or 'none'}")
        print(f"active   : {active_version() or 'unset'}")
        return 0

    if args.check:
        import vector_lake_core as core

        print(f"core_version   : {getattr(core, 'core_version', 'unset (direct .pyd install)')}")
        print(f"version()      : {core.version()}")
        print(f"blocks_contract: {getattr(core, 'blocks_contract', lambda: 'absent')()}")
        print(f"file           : {core.__file__}")
        return 0

    if args.activate:
        activate(args.activate)
        return 0

    version = source_version()
    dll = DLL if args.no_build else build()
    if not dll.exists():
        raise SystemExit(f"{dll} does not exist; run without --no-build")
    target = install(dll, version)
    print(f"installed {version} -> {target}")
    activate(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
