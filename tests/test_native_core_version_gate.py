"""Runtime ABI/version gate tests for optional ``vector_lake_core``."""
from __future__ import annotations

import sys
import types

from vector_lake import native_core, tool_doctor


def test_mismatched_native_core_version_is_rejected_without_purging(monkeypatch):
    fake = types.ModuleType("vector_lake_core")
    fake.__file__ = "C:/fake/vector_lake_core.pyd"
    fake.version = lambda: "0.2.1"
    monkeypatch.setitem(sys.modules, "vector_lake_core", fake)
    monkeypatch.setattr(native_core, "_WARNED_MISMATCHES", set())

    validated, reason = native_core.load_native_core()
    assert validated is None
    assert reason is not None and "0.2.1" in reason

    status = native_core.native_core_status()
    assert status["status"] == "mismatch"
    assert status["installed"] == "0.2.1"
    assert status["expected"] == native_core.EXPECTED_CORE_VERSION


def test_matching_native_core_version_is_accepted(monkeypatch):
    fake = types.ModuleType("vector_lake_core")
    fake.__file__ = "C:/fake/vector_lake_core.pyd"
    fake.version = lambda: native_core.EXPECTED_CORE_VERSION
    monkeypatch.setitem(sys.modules, "vector_lake_core", fake)

    validated, reason = native_core.load_native_core()
    assert validated is fake
    assert reason is None


def test_tool_doctor_flags_version_mismatch_as_unhealthy(isolated_memory, monkeypatch):
    monkeypatch.setattr(
        native_core,
        "native_core_status",
        lambda: {
            "status": "mismatch",
            "installed": "0.2.1",
            "expected": native_core.EXPECTED_CORE_VERSION,
            "reason": "version_mismatch",
        },
    )
    report = tool_doctor.doctor_vector_lake(deep_dependency_check=False)
    assert "Native Acceleration" in report
    assert "0.2.1" in report and "disabled" in report
    assert "[FAIL] Native Acceleration:" in report


def test_active_version_pointer_and_env_override_take_precedence_over_stale_dist_info(tmp_path, monkeypatch):
    import importlib.util

    site_dir = tmp_path / "site-packages"
    pkg_dir = site_dir / "vector_lake_core"
    pkg_dir.mkdir(parents=True)
    init_file = pkg_dir / "__init__.py"
    init_file.write_text("# shim\n", encoding="utf-8")

    dist_info = site_dir / "vector_lake_core-0.2.1.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: vector-lake-core\nVersion: 0.2.1\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("VECTOR_LAKE_CORE_VERSION", raising=False)
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: types.SimpleNamespace(origin=str(init_file)) if name == "vector_lake_core" else None,
    )

    version, origin = native_core._installed_spec_version()
    assert version == "0.2.1"
    assert origin == str(init_file)

    pointer = pkg_dir / "_active_version.txt"
    pointer.write_text(f"{native_core.EXPECTED_CORE_VERSION}\n", encoding="utf-8")
    version, _ = native_core._installed_spec_version()
    assert version == native_core.EXPECTED_CORE_VERSION

    slot_dir = site_dir / "vector_lake_core_0_2_9"
    slot_dir.mkdir()
    monkeypatch.setenv("VECTOR_LAKE_CORE_VERSION", "0.2.9")
    version, _ = native_core._installed_spec_version()
    assert version == "0.2.9"
