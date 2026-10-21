"""The package must have no module-level import cycle.

An audit of this repository reported five import cycles around ``wiki_utils``, the
highest fan-in module (30 importers), and read them as layering fragility.  Measured
with an AST walk that separates module-level imports from function-level ones, the
number of cycles at **load time** is zero:

* every one of those five was an SCC computed over the union of module-level and
  function-level edges, i.e. a *conceptual* inversion, not an import defect;
* the package stays loadable because the edges that point "up" the layering are
  deferred to function scope, and they are the only reason it is loadable.

Only two pairs are genuinely bidirectional -- ``wiki_utils`` and ``db_store`` each
reach up through exactly one function into a module that imports them at module level:

    wiki_utils.write_markdown_file        -> mutation_coordinator.execute_mutation_plan
    db_store.delete_node_cascade          -> tool_timeline.sync_timeline_events_for_claim_delta

Neither is an accident that a later cleanup should hoist.  ``wiki_utils`` delegates its
writes to the coordinator so the write gate cannot be bypassed by calling the low-level
helper directly, and the deferral is what makes the delegation expressible at all.
For the timeline pair, the deferred binding is also load-bearing for fault injection:
``tests/test_timeline_projection.py`` monkeypatches ``tool_timeline``'s binding to
simulate a projection failure and asserts the canonical transaction rolls back, which
only intercepts the call because the caller resolves the name at call time.

So the invariant worth pinning is not "no upward edges" -- that would mean an invasive
re-layering of a 30-importer module for no measured defect -- but "no cycle at load
time".

What this guard does and does not buy, measured rather than assumed: hoisting
``wiki_utils``'s deferred import to module level does not merely trip this check, it
makes the package unimportable -- a five-module SCC
(``db_store -> defense_hook -> mutation_coordinator -> purpose_contract -> wiki_utils``)
and ``ImportError: cannot import name ... from partially initialized module``.  Because
``tests/conftest.py`` imports the package, that also makes the whole suite fail at
collection, so this check is **not** what would prevent a cycle: the interpreter already
fails loudly and first.

Its value is narrower and still real.  It reads files and never imports the package, so
it runs in exactly the state where nothing else can (demonstrated by running it through
``importlib`` on a deliberately broken tree), and it names the offending SCC instead of
leaving a cryptic partially-initialized-module chain.  The test below keeps that claim
honest by requiring the checker to detect a synthetic cycle.

Boundary, stated honestly: the walk covers ``vector_lake/*.py`` only.  Import cycles
involving ``scripts/``, the root entry points, or a third-party package are outside it,
and the check covers ``import``/``from ... import`` statements only -- it does not see
imports performed through ``importlib``.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "vector_lake"


def _module_level_edges(path: pathlib.Path, known: frozenset[str]) -> set[str]:
    """Intra-package dependencies that are imported at module scope.

    Imports nested in a function are deliberately *not* counted: they cannot create a
    load-time cycle, which is the property under test.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = path.stem

    def deps(node) -> set[str]:
        found: set[str] = set()
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("vector_lake."):
                found.add(node.module.split(".")[1])
            elif node.module == "vector_lake":
                # ``from vector_lake import host_env`` imports a submodule.
                found.update(a.name for a in node.names if a.name in known)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("vector_lake."):
                    found.add(alias.name.split(".")[1])
        return found

    edges: set[str] = set()
    for statement in tree.body:
        edges |= deps(statement)
    return {dep for dep in edges if dep in known and dep != owner}


def _module_level_graph() -> dict[str, set[str]]:
    known = frozenset(p.stem for p in PACKAGE.glob("*.py"))
    return {p.stem: _module_level_edges(p, known) for p in PACKAGE.glob("*.py")}


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCC; returns only components of more than one node."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = [0]
    found: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for dep in graph.get(node, ()):
            if dep not in index:
                visit(dep)
                low[node] = min(low[node], low[dep])
            elif dep in on_stack:
                low[node] = min(low[node], index[dep])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                found.append(sorted(component))

    for node in graph:
        if node not in index:
            visit(node)
    return found


def _deferred_upward_edges() -> set[tuple[str, str]]:
    """(importer, imported) pairs where the import is in a function and points back."""
    known = frozenset(p.stem for p in PACKAGE.glob("*.py"))
    upward: set[tuple[str, str]] = set()
    for path in PACKAGE.glob("*.py"):
        owner = path.stem
        module_edges = _module_level_edges(path, known)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, (ast.ImportFrom, ast.Import)):
                    continue
                if isinstance(inner, ast.ImportFrom):
                    target = (
                        inner.module.split(".")[1]
                        if inner.module and inner.module.startswith("vector_lake.")
                        else None
                    )
                else:
                    names = [a.name for a in inner.names if a.name.startswith("vector_lake.")]
                    target = names[0].split(".")[1] if names else None
                if target and target != owner and owner in _module_level_edges(
                    PACKAGE / f"{target}.py", known
                ):
                    upward.add((owner, target))
    return upward


def test_there_is_no_module_level_import_cycle():
    cycles = _cycles(_module_level_graph())
    assert not cycles, (
        "module-level import cycle(s): "
        + "; ".join(" <-> ".join(c) for c in cycles)
        + ". Break it the way the existing inversions are broken -- defer the upward "
        "import into the function that needs it -- rather than dropping the import."
    )


def test_the_cycle_checker_detects_a_synthetic_cycle():
    """The guard has to be able to fail, or it proves nothing."""
    assert _cycles({"a": {"b"}, "b": {"a"}}) == [["a", "b"]]
    assert _cycles({"a": {"b"}, "b": {"c"}, "c": set()}) == []


def test_only_documented_pairs_are_deferred_and_upward():
    """Extra upward edges mean a new load-order dependency; review it, do not absorb it.

    Adding a third pair is not automatically wrong, but it is a new structural
    commitment and should be a deliberate change with the reason written down here.
    """
    assert _deferred_upward_edges() == {
        ("wiki_utils", "mutation_coordinator"),
        ("db_store", "tool_timeline"),
        # Node deletion must check Claim references and refresh predecessor memory BEFORE
        # deleting a winner. governance_store owns those rules and already imports db_store,
        # so this bounded deletion-only dependency is deferred, like the timeline delta.
        ("db_store", "governance_store"),
        # ``scoped_memory_search`` imports ``governance_store`` at module level (it is the store's
        # scoped reader), so the store's own two call sites defer the import back to it.  Same
        # shape as the two above: a recorded inversion, not an absorbed one.
        ("governance_store", "scoped_memory_search"),
    }
