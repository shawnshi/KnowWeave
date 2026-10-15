"""Guard the dependency manifest against drifting from the enforced floors.

Several floors are load-bearing facts discovered by running the packages, not
preferences, so they are asserted here rather than only written in a comment:

* ``mistune>=3.3.3`` - ``create_markdown(renderer='ast')`` raises
  ``TypeError: 'str' object is not callable`` on 3.0.0, and 3.3.0-3.3.2 carry
  GHSA-6m44-fpc8-c3rq (RecursionError DoS through ``mistune.html()``).
* ``python-louvain`` is gone; clustering is Leiden via leidenalg + igraph.
"""
import pathlib
import re

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
LOCK = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
# Requirement lines only: drop comments and blanks.
REQUIRED_LINES = [
    line.strip()
    for line in REQUIREMENTS.splitlines()
    if line.strip() and not line.strip().startswith("#")
]


def _specifier(name: str) -> str | None:
    """The raw requirements.txt line declaring ``name``, if any."""
    for line in REQUIRED_LINES:
        if line.lower().startswith(name.lower()):
            return line
    return None


@pytest.mark.parametrize(
    "name",
    ["filelock", "python-dotenv", "igraph", "leidenalg", "PyYAML",
     "watchfiles", "google-genai", "fastmcp", "sqlite-vec", "tantivy", "mistune"],
)
def test_required_dependency_is_declared(name):
    assert _specifier(name) is not None, f"{name} missing from requirements.txt"


def test_python_louvain_is_removed_everywhere():
    """Only requirement/pin lines count; explanatory comments are ignored."""
    assert not [line for line in REQUIRED_LINES if "louvain" in line.lower()]
    pinned = [
        line.strip()
        for line in LOCK.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [line for line in pinned if "louvain" in line.lower()]
    # And nothing in the runtime may import it either.
    offenders = []
    for path in list((ROOT / "vector_lake").glob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "community import community_louvain" in text or "community_louvain." in text:
            offenders.append(path.name)
    assert not offenders, f"python-louvain still imported by: {offenders}"


@pytest.mark.parametrize(
    "name, minimum",
    [
        ("filelock", "3.15"),
        ("igraph", "0.11.0"),
        ("leidenalg", "0.10.0"),
        ("PyYAML", "6.0.1"),
        ("google-genai", "2.0.0"),
        ("sqlite-vec", "0.1.3"),
        ("tantivy", "0.26"),
    ],
)
def test_declared_floor_matches_the_requested_minimum(name, minimum):
    line = _specifier(name)
    assert line is not None
    assert f">={minimum}" in line.replace(" ", ""), f"{name} floor drifted: {line}"


def test_fastmcp_floor_is_on_the_4x_line():
    """The runtime imports ``fastmcp.FastMCP``; the SDK import path is gone.

    The reversal this guards: the tree used to import ``mcp.server.MCPServer``
    (mcp 2.x) with a 1.x ``mcp.server.fastmcp`` fallback, so a floor that let a
    clean install resolve elsewhere emptied the tool surface.  fastmcp 4 also
    dropped ``_tool_manager``, which is why the floor is 4.0 and not 3.x.
    """
    line = _specifier("fastmcp")
    assert line is not None
    assert line.replace(" ", "") in {"fastmcp>=4.0.0", "fastmcp>=4.0"}, (
        f"fastmcp floor must track the 4.x FastMCP API: {line}"
    )
    assert _specifier("mcp") is None, (
        "mcp is resolved transitively by fastmcp-slim; declaring it directly "
        "reintroduces the 2.x SDK import this project moved away from"
    )
    source = (ROOT / "vector_lake" / "mcp_server.py").read_text(encoding="utf-8")
    assert "from fastmcp import FastMCP" in source
    assert "mcp.server" not in source, "the removed SDK import path came back"


def test_mistune_floor_is_above_the_broken_3_0_0():
    """3.0.0 fails at import time, and 3.3.0-3.3.2 carry a recursion DoS."""
    line = _specifier("mistune")
    assert line is not None
    spec = Requirement(line).specifier
    assert not any(Version(value) in spec for value in ("3.0.0", "3.3.0", "3.3.1", "3.3.2")), (
        f"The mistune requirement permits known insecure builds: {line}"
    )
    pinned = [item.strip() for item in LOCK.splitlines() if item.strip().startswith("mistune==")]
    assert pinned, "mistune must stay pinned in requirements.lock.txt"
    version = tuple(int(part) for part in pinned[0].split("==", 1)[1].split(".")[:3])
    assert version >= (3, 3, 3), f"the locked mistune build must exclude the advisory range: {pinned[0]}"
    assert Version(pinned[0].split("==", 1)[1]) in spec


def test_direct_test_dependencies_are_declared_and_exactly_pinned():
    lines = (ROOT / "requirements-test.txt").read_text(encoding="utf-8").splitlines()
    requirements = {r.name.lower(): r for raw in lines
                    if (line := raw.strip()) and not line.startswith("#")
                    for r in [Requirement(line)]}
    assert set(requirements) == {"numpy", "pytest"}
    for requirement in requirements.values():
        specs = list(requirement.specifier)
        assert len(specs) == 1 and specs[0].operator == "=="
        assert "*" not in specs[0].version and not requirement.url
    assert _specifier("numpy") is None, "Test-only numpy must not enter the runtime manifest"


def test_watchdog_is_gone_and_watchfiles_is_imported():
    """The filesystem watcher moved to the Rust ``notify`` crate on 2026-09-25.

    Same ritual as the jieba/networkx removal: the pin is gone *and* the tree is checked, so a
    stale ``watchdog`` pin or import cannot quietly come back.  The handlers' event surface is
    a local shim now, which is why the check is on the import, not on the handler shape.
    """
    assert _specifier("watchdog") is None, "watchdog is no longer the watcher"
    pinned = [
        line.strip()
        for line in LOCK.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [line for line in pinned if line.startswith("watchdog==")]
    source = (ROOT / "vector_lake" / "watchdog_app.py").read_text(encoding="utf-8")
    assert "from watchfiles import" in source
    assert "import watchdog" not in source and "from watchdog" not in source


def test_bm25s_is_gone_and_the_rerank_is_mandatory_rust():
    """The pool reranker moved into the Rust core on 2026-09-25.

    Same ritual as the jieba/networkx/watchdog removals: the pin is gone *and* the tree is checked,
    so a stale ``bm25s`` pin or import cannot come back silently.  The behaviour without the core is
    pinned in ``tests/test_rerank_candidates.py`` (fail open, upstream order, WARNING logged).
    """
    assert _specifier("bm25s") is None, "the pool rerank no longer has a Python engine"
    pinned = [
        line.strip()
        for line in LOCK.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [line for line in pinned if line.startswith("bm25s==")]
    source = (ROOT / "vector_lake" / "tool_search.py").read_text(encoding="utf-8")
    assert "fast_bm25_rerank" in source
    assert "import bm25s" not in source and "bm25s." not in source


def test_every_required_package_is_pinned_in_the_lock():
    for line in REQUIRED_LINES:
        name = re.split(r"[><=~\s]", line, maxsplit=1)[0]
        assert re.search(rf"^{re.escape(name)}==(\S+)", LOCK, re.M), (
            f"{name} is required but not pinned in requirements.lock.txt"
        )


def test_the_removed_fallbacks_are_not_pinned_or_checked():
    """jieba and networkx are leaves of dependencies this project no longer has.

    jieba was the pure-Python tokenizer fallback removed on 2026-09-18; networkx
    was python-louvain's requirement before Leiden replaced it.  Both survived that
    removal for a while, in two places at once: the lock still pinned them, and
    ``tool_doctor`` still listed jieba in its dependency map -- so doctor reported
    ``[OK] jieba: installed`` for a package the runtime cannot use, which is also
    how the pin kept looking justified.  A removal the tree does not check is a
    removal that comes back, which is what this pins shut.

    ``rjieba`` is the tokenizer that stayed, so the pin pattern is anchored to the
    line, not a substring.
    """
    pinned = [
        line.strip()
        for line in LOCK.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for gone in ("jieba", "networkx"):
        assert not [line for line in pinned if line.startswith(f"{gone}==")], (
            f"{gone} is pinned in requirements.lock.txt but nothing depends on it"
        )
    assert any(line.startswith("rjieba==") for line in pinned), (
        "rjieba is the tokenizer backend and must stay pinned"
    )

    doctor = (ROOT / "vector_lake" / "tool_doctor.py").read_text(encoding="utf-8")
    assert '"jieba": "jieba"' not in doctor, (
        "tool_doctor reports a dependency the tokenizer no longer has; its backend "
        "line already names the real one"
    )


def test_no_document_describes_the_abandoned_projection_store():
    """A layout that was designed and never built must not be advertised.

    schema.md described ``index.json`` / ``claim_graph.json`` as projection-v2
    locators whose immutable components live under
    ``MEMORY/wiki/.projection-store/objects/sha256/``.  That store has zero
    references in the tree and no directory on disk, and the artifact is
    ``claim_topology.json``.  Documents are cheaper to fix than a reader misled by
    them, so the names are checked where they are written.
    """
    for name in ("schema.md", "CONTEXT.md", "README.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "projection-store" not in text, f"{name} advertises a store that was never built"
        assert "claim_graph.json" not in text, f"{name} names the superseded projection file"
        assert "operational_memory.json" not in text, (
            f"{name} names a runtime-memory file; it is a SQLite table"
        )
