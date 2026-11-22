"""Bounded optional capability cache; cache faults fall back to FULL safe probing."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import time

CACHE_TTL_SECONDS = 300
PACKAGE_NAMES = {"codex": "@openai/codex", "gemini": "@google/gemini-cli"}


def _signature(path: Path):
    stat = path.stat()
    if not path.is_file():
        raise ValueError("CLI identity component is not a file")
    return [str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def cli_identity(binary: str, backend: str) -> str | None:
    """Recognize native executables or declared npm packages; unknown shims stay uncached."""
    if backend not in PACKAGE_NAMES and backend != "agy":
        return None
    path = Path(binary).resolve()
    try:
        parts: list = [_signature(path)]
        if backend == "agy":
            if path.suffix.lower() != ".exe" and path.stat().st_size <= 65536:
                parts.append(hashlib.sha256(path.read_bytes()).hexdigest())
            return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
        if path.suffix.lower() == ".exe":
            return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
        roots = list(path.parents)[:5]
        roots.insert(0, Path(binary).parent / "node_modules" / PACKAGE_NAMES[backend])
        for root in roots:
            manifest = root / "package.json"
            if not manifest.is_file():
                continue
            with manifest.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                continue
            package = json.loads(raw)
            if not isinstance(package, dict) or package.get("name") != PACKAGE_NAMES[backend]:
                continue
            bins = package.get("bin")
            entry = bins.get(backend) if isinstance(bins, dict) else bins
            if not isinstance(entry, str) or not entry:
                return None
            target = (root / entry).resolve()
            if not target.is_relative_to(root.resolve()):
                return None
            parts += [_signature(manifest), hashlib.sha256(raw).hexdigest(), _signature(target)]
            optional = package.get("optionalDependencies") or {}
            if not isinstance(optional, dict):
                return None
            if backend == "codex" and optional:
                arch = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
                suffix = "pc-windows-msvc" if os.name == "nt" else ("apple-darwin" if platform.system() == "Darwin" else "unknown-linux-musl")
                native = []
                for dependency in optional:
                    if isinstance(dependency, str) and dependency.startswith("@openai/codex-"):
                        base = root.parent / dependency.split("/")[-1]
                        candidate = base / "vendor" / f"{arch}-{suffix}" / "codex" / ("codex.exe" if os.name == "nt" else "codex")
                        if candidate.is_file():
                            native += [_signature(base / "package.json"), _signature(candidate)]
                legacy = root / "vendor" / f"{arch}-{suffix}" / "codex" / ("codex.exe" if os.name == "nt" else "codex")
                if legacy.is_file():
                    native += [_signature(legacy)]
                if not native:
                    return None
                parts += native
            return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return None


def cached_capabilities(backend: str, binary: str, policy: str, deadline: float, probe,
                        factory, required_features=frozenset()):
    """One entry per backend/root. Never cache auth, failures or unrecognized identities."""
    from filelock import FileLock, Timeout
    from vector_lake.wiki_utils import get_meta_dir

    identity = cli_identity(binary, backend)
    if identity is None:
        return probe()
    if deadline <= time.monotonic():
        raise RuntimeError("model execution deadline exhausted")
    directory = get_meta_dir() / "runtime"
    path = directory / f"cli_capabilities_{backend}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path) + ".lock")
        lock.acquire(timeout=min(2.0, max(0.0, deadline - time.monotonic())))
    except (OSError, Timeout):
        return probe()
    try:
        now = time.time()
        try:
            with path.open("r", encoding="utf-8") as handle:
                text = handle.read(65537)
            saved = json.loads(text) if len(text) <= 65536 else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            saved = None
        if isinstance(saved, dict):
            checked, features = saved.get("checked_at"), saved.get("features")
            if (type(saved.get("version")) is int and saved.get("version") == 1 and saved.get("identity") == identity and saved.get("policy") == policy
                    and saved.get("binary") == binary
                    and not isinstance(checked, bool) and isinstance(checked, (int, float)) and math.isfinite(checked)
                    and 0 <= now - checked < CACHE_TTL_SECONDS
                    and isinstance(features, list) and len(features) <= 256 and all(isinstance(item, str) for item in features)
                    and required_features.issubset(features)):
                return factory(binary, tuple(features))
        capabilities = probe()  # Exceptions propagate: a failed probe is never accepted/cached.
        if capabilities.binary != binary or cli_identity(binary, backend) != identity:
            raise RuntimeError("CLI identity changed during capability validation")
        payload = {"version": 1, "binary": capabilities.binary, "identity": identity, "policy": policy,
                   "checked_at": time.time(), "features": list(capabilities.features)}
        encoded = json.dumps(payload, sort_keys=True)
        if len(encoded.encode("utf-8")) > 65536:
            return capabilities
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(encoded, encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            # Only an optional optimization failed; the successful full probe remains valid.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return capabilities
    finally:
        lock.release()
