"""Guard module identifiers, retired imports, and current module-map paths."""
from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
RETIRED_MODULES = frozenset({
    "native_llm", "tool_piea", "runtime_contract", "output_contract",
})
MODULE_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*|__init__")
MAP_PATH = re.compile(r"`((?:vector_lake|scripts)/[a-z0-9_./-]+\.py|cli\.py|watchdog_sync\.py|\.pi/agents/)`")


def _retired_imports(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module == "vector_lake":
                names = [f"vector_lake.{alias.name}" for alias in node.names]
            else:
                names = [node.module or ""]
        else:
            continue
        found.update(name for name in names if name.startswith("vector_lake.")
                     and name.split(".")[1] in RETIRED_MODULES)
    return found


def _missing_map_paths(text: str, root: Path) -> set[str]:
    return {name for name in MAP_PATH.findall(text) if not (root / name).exists()}


def test_source_module_names_are_snake_case():
    paths = [*ROOT.glob("*.py"), *(ROOT / "vector_lake").rglob("*.py"),
             *(ROOT / "scripts").rglob("*.py"),
             *(ROOT / "crates" / "vector_lake_core" / "src").glob("*.rs")]
    assert paths
    assert not [path.relative_to(ROOT).as_posix() for path in paths
                if not MODULE_NAME.fullmatch(path.stem)]


def test_production_and_test_imports_do_not_use_retired_modules():
    paths = [*ROOT.glob("*.py"), *(ROOT / "vector_lake").rglob("*.py"),
             *(ROOT / "scripts").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
    stale = {path.relative_to(ROOT).as_posix(): sorted(found)
             for path in paths
             if (found := _retired_imports(path.read_text(encoding="utf-8-sig")))}
    assert stale == {}
    assert not [name for name in RETIRED_MODULES
                if (ROOT / "vector_lake" / f"{name}.py").exists()]


@pytest.mark.parametrize("module", sorted(RETIRED_MODULES))
@pytest.mark.parametrize("form", ["import vector_lake.{module}",
                                  "from vector_lake.{module} import entrypoint",
                                  "from vector_lake import {module}"])
def test_retired_import_guard_rejects_each_import_form(module, form):
    assert _retired_imports(form.format(module=module)) == {f"vector_lake.{module}"}


def test_payload_contract_field_is_not_a_retired_import():
    source = ('from vector_lake.ingest_output_contract import build_output_contract\n'
              'packet = {"output_contract": build_output_contract()}\n')
    assert _retired_imports(source) == set()


@pytest.mark.parametrize("document", ["README.md", "CONTEXT.md"])
def test_current_module_map_paths_exist(document):
    text = (ROOT / document).read_text(encoding="utf-8")
    assert MAP_PATH.findall(text)
    assert _missing_map_paths(text, ROOT) == set()
    assert "`agents/`" not in text


def test_module_map_guard_rejects_a_missing_module(tmp_path):
    text = "| `vector_lake/deleted_module.py` | obsolete module |"
    assert _missing_map_paths(text, tmp_path) == {"vector_lake/deleted_module.py"}
