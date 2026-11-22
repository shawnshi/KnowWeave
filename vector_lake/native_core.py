"""Runtime ABI/version gate for the optional Rust acceleration extension."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import os
import sys
from pathlib import Path
from types import ModuleType

EXPECTED_CORE_VERSION = "0.2.8"

_LOG = logging.getLogger("vector-lake-native")
_WARNED_MISMATCHES: set[tuple[str, str]] = set()


def _warn_once(installed: str, origin: str) -> None:
    warn_key = (installed, origin)
    if warn_key not in _WARNED_MISMATCHES:
        _WARNED_MISMATCHES.add(warn_key)
        _LOG.warning(
            "Rejecting incompatible vector-lake-core v%s (expected v%s); using pure-python fallback.",
            installed,
            EXPECTED_CORE_VERSION,
        )


def _installed_spec_version() -> tuple[str | None, str]:
    """Inspect the distribution owning ``vector_lake_core`` without loading native binaries."""
    spec = importlib.util.find_spec("vector_lake_core")
    if spec is None:
        return None, ""
    origin = str(spec.origin or "")
    if not origin:
        return None, ""
    origin_path = Path(origin).resolve()
    if origin_path.name == "__init__.py":
        explicit = os.environ.get("VECTOR_LAKE_CORE_VERSION", "").strip()
        if explicit:
            slot_dir = origin_path.parent.parent / f"vector_lake_core_{explicit.replace('.', '_')}"
            if slot_dir.is_dir():
                return explicit, origin
        pointer = origin_path.parent / "_active_version.txt"
        if pointer.is_file():
            try:
                active = pointer.read_text(encoding="utf-8").strip()
            except OSError:
                active = ""
            if active:
                return active, origin
    search_roots = list(dict.fromkeys([str(origin_path.parent.parent), str(origin_path.parent)]))
    for dist in importlib.metadata.distributions(path=search_roots):
        meta_name = str(dist.metadata.get("Name") or "").lower().replace("_", "-")
        if meta_name == "vector-lake-core":
            return str(dist.version), origin
    return None, origin


def validate_native_core(module: ModuleType | None) -> tuple[ModuleType | None, str | None]:
    """Accept ``vector_lake_core`` only when its version matches ``EXPECTED_CORE_VERSION``.

    Synthetic test stubs without a ``version`` callable are preserved so isolated
    unit probes can continue mocking specific native entrypoints.
    """
    if module is None:
        return None, None
    version_fn = getattr(module, "version", None)
    if not callable(version_fn):
        return module, None
    installed = str(version_fn())
    if installed == EXPECTED_CORE_VERSION:
        return module, None
    origin = str(getattr(module, "__file__", "") or "")
    _warn_once(installed, origin)
    return None, f"version_mismatch: installed={installed}, expected={EXPECTED_CORE_VERSION}"


def load_native_core() -> tuple[ModuleType | None, str | None]:
    """Import and version-gate ``vector_lake_core``; propagate broken transitive installs."""
    if "vector_lake_core" in sys.modules:
        return validate_native_core(sys.modules["vector_lake_core"])
    try:
        dist_version, origin = _installed_spec_version()
    except ModuleNotFoundError as exc:
        if exc.name != "vector_lake_core":
            raise
        return None, None
    if dist_version is not None and dist_version != EXPECTED_CORE_VERSION:
        _warn_once(dist_version, origin)
        return None, f"version_mismatch: installed={dist_version}, expected={EXPECTED_CORE_VERSION}"
    try:
        import vector_lake_core as raw_core
    except ModuleNotFoundError as exc:
        if exc.name != "vector_lake_core":
            raise
        return None, None
    return validate_native_core(raw_core)


def native_core_status() -> dict[str, str | None]:
    """Return non-raising readiness metadata for diagnostics and doctor checks."""
    if "vector_lake_core" not in sys.modules:
        try:
            dist_version, origin = _installed_spec_version()
        except ModuleNotFoundError as exc:
            if exc.name == "vector_lake_core":
                return {
                    "status": "missing",
                    "installed": None,
                    "expected": EXPECTED_CORE_VERSION,
                    "reason": None,
                }
            return {
                "status": "error",
                "installed": None,
                "expected": EXPECTED_CORE_VERSION,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if dist_version is not None and dist_version != EXPECTED_CORE_VERSION:
            _warn_once(dist_version, origin)
            return {
                "status": "mismatch",
                "installed": dist_version,
                "expected": EXPECTED_CORE_VERSION,
                "reason": f"version_mismatch: installed={dist_version}, expected={EXPECTED_CORE_VERSION}",
            }
    try:
        import vector_lake_core as raw_core
    except ModuleNotFoundError as exc:
        if exc.name == "vector_lake_core":
            return {
                "status": "missing",
                "installed": None,
                "expected": EXPECTED_CORE_VERSION,
                "reason": None,
            }
        return {
            "status": "error",
            "installed": None,
            "expected": EXPECTED_CORE_VERSION,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    except ImportError as exc:
        return {
            "status": "error",
            "installed": None,
            "expected": EXPECTED_CORE_VERSION,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    version_fn = getattr(raw_core, "version", None)
    installed = str(version_fn()) if callable(version_fn) else None
    validated, reason = validate_native_core(raw_core)
    if validated is None:
        return {
            "status": "mismatch",
            "installed": installed,
            "expected": EXPECTED_CORE_VERSION,
            "reason": reason,
        }
    return {
        "status": "active",
        "installed": installed or EXPECTED_CORE_VERSION,
        "expected": EXPECTED_CORE_VERSION,
        "reason": None,
    }
