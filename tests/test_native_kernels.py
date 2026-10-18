"""Pure-kernel parity, bounded output, and exact-text verification contracts."""
from collections import Counter, defaultdict
import math
import random
import sqlite3

import pytest

from vector_lake import memory_gram_index as gram, tool_graph, tool_lint


def _core(feature):
    core = gram.vector_lake_core if gram.HAVE_CORE else None
    if core is None or not hasattr(core, feature):
        pytest.skip("New kernels are exercised in the required freshly built native lane")
    return core


def _pair_oracle(names, threshold):
    groups = defaultdict(list)
    for i, name in enumerate(names):
        groups[len(name)].append(i)
    lengths = sorted(groups)
    counts = [Counter(name) for name in names]
    for pos, length in enumerate(lengths):
        for other in lengths[pos:]:
            total = length + other
            if total and 2.0 * min(length, other) / total <= threshold:
                break
            for i, a in enumerate(groups[length]):
                for b in groups[other][i + 1 if length == other else 0:]:
                    common = sum(min(value, counts[b][char]) for char, value in counts[a].items())
                    if not total or 2.0 * common / total > threshold:
                        yield a, b


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("threshold", [0.0, 0.5, 0.91, 0.999])
def test_candidate_order_matches_upper_bound_oracle(monkeypatch, native, threshold):
    if native:
        _core("NamePairIndex")
    monkeypatch.setattr(tool_lint, "HAVE_CORE", native)
    rng = random.Random(321)
    names = ["", "", "ab", "ba", "aaa", "aab", "研究数据", "数据研究", "éßσ𐐨", "İi"]
    names += ["".join(rng.choice("研究abc123éßσ中𐐨") for _ in range(rng.randrange(22))) for _ in range(100)]
    assert list(tool_lint._candidate_name_pairs(names, threshold)) == list(_pair_oracle(names, threshold))


def test_native_pair_chunks_are_bounded_and_resume_in_order():
    core = _core("NamePairIndex")
    names = ["same"] * 37 + ["", "", "different"]
    index = core.NamePairIndex(names, 0.91)
    result = []
    for _ in range(10000):
        pairs, done = index.next_pairs(max_pairs=2, max_checks=7)
        assert len(pairs) <= 2
        result.extend(pairs)
        if done:
            break
    else:
        pytest.fail("The bounded iterator did not make progress")
    assert result == list(_pair_oracle(names, 0.91))
    assert index.next_pairs() == ([], True)
    empty = core.NamePairIndex([], 0.91)
    assert empty.next_pairs() == ([], True)


def test_native_pair_limits_reject_invalid_budgets():
    core = _core("NamePairIndex")
    for threshold in [-1, 1, math.nan, math.inf]:
        with pytest.raises(ValueError):
            core.NamePairIndex(["a"], threshold)
    with pytest.raises(ValueError):
        core.NamePairIndex(["a"] * 100001, 0.91)
    index = core.NamePairIndex(["a", "a"], 0.91)
    for kwargs in [{"max_pairs": 0}, {"max_pairs": 65537}, {"max_checks": 0}, {"max_checks": 1000001}]:
        with pytest.raises(ValueError):
            index.next_pairs(**kwargs)


@pytest.fixture
def composite_db(monkeypatch):
    fields = {
        1: ("abcd", "", ""), 2: ("ab", "bc", "cd"),
        3: ("ab-bc-cd", "", ""), 4: ("abc", "", ""),
        5: ("", "", "abcd"), 6: ("ab", "bc", "zzcd"),
        7: ("cd", "", ""), 8: ("bc", "", ""), 9: ("ab", "", ""),
    }
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE operational_memory_index(memory_id TEXT, key_blob TEXT, text_blob TEXT, page_blob TEXT)")
    conn.execute("CREATE TABLE operational_memory_gram(gram TEXT PRIMARY KEY, postings BLOB)")
    conn.execute("CREATE TABLE operational_memory_gram_docs(doc INTEGER PRIMARY KEY, memory_id TEXT NOT NULL UNIQUE)")
    postings = defaultdict(list)
    for doc, blobs in fields.items():
        conn.execute("INSERT INTO operational_memory_index(rowid, memory_id, key_blob, text_blob, page_blob) VALUES (?, ?, ?, ?, ?)", (doc, f"mem_{doc}", *blobs))
        conn.execute("INSERT INTO operational_memory_gram_docs(doc, memory_id) VALUES (?, ?)", (doc, f"mem_{doc}"))
        for token, mask in gram.extract_grams(*blobs).items():
            postings[token].append((doc, mask))
    conn.executemany("INSERT INTO operational_memory_gram VALUES (?, ?)",
                     [(token, gram._pack(rows)) for token, rows in postings.items()])
    monkeypatch.setattr(gram, "get_connection", lambda: conn)
    yield conn, fields
    conn.close()


@pytest.mark.parametrize("native", [False, True])
def test_composite_intersection_still_requires_exact_adjacency(composite_db, monkeypatch, native):
    if native:
        _core("intersect_postings")
    monkeypatch.setattr(gram, "HAVE_CORE", native)
    conn, fields = composite_db
    assert gram._composite_candidates("abcd", conn) == {1, 2, 3, 5, 6}
    assert gram._composite_candidates("absent", conn) == set()
    scores = {}
    gram._accumulate_composite(["abcd"], list(gram.FIELD_WEIGHTS), scores)
    expected = {doc: sum(weight for weight, text in zip((4, 3, 1), blobs) if "abcd" in text)
                for doc, blobs in fields.items() if any("abcd" in text for text in blobs)}
    assert scores == expected == {1: 4, 5: 1}


def test_native_postings_intersection_matches_sets():
    core = _core("intersect_postings")
    rng = random.Random(22)
    for _ in range(100):
        sets = [set(rng.choices(range(400), k=rng.randrange(100))) for _ in range(rng.randrange(1, 12))]
        blobs = [core.pack_postings([(doc, rng.randrange(16)) for doc in sorted(docs)]) for docs in sets]
        assert core.intersect_postings(blobs) == sorted(set.intersection(*sets))
    assert core.intersect_postings([]) == []
    assert core.intersect_postings([b""]) == []
    with pytest.raises(ValueError, match="complete uint32"):
        core.intersect_postings([b"", b"broken"])
    with pytest.raises(ValueError, match="overflow"):
        core.intersect_postings([b"\xf0\xff\xff\xff" * 17])


def test_backbone_matches_python_for_random_graphs(monkeypatch):
    _core("fast_backbone_indices")
    rng = random.Random(39)
    for _ in range(200):
        nodes = {f"node_{i}" for i in range(rng.randrange(1, 30))}
        pool = sorted(nodes) + ["missing", ""]
        edges = [{"source": rng.choice(pool), "target": rng.choice(pool),
                  "weight": rng.choice([0.0, -0.0, -1.0, 1.0, 5.1235, 10.0005, "6"]),
                  "relation": rng.choice(["", "explicit_relation", None])}
                 for _ in range(rng.randrange(180))]
        edges += [{"source": "", "target": "missing", "weight": "invalid"}]
        budget, minimum = rng.randrange(40), rng.choice([-2.0, 0.0, 5.0])
        monkeypatch.setattr(tool_graph, "HAVE_CORE", False)
        expected = tool_graph._extract_backbone_edges(edges, nodes, budget, minimum)
        monkeypatch.setattr(tool_graph, "HAVE_CORE", True)
        actual = tool_graph._extract_backbone_edges(edges, nodes, budget, minimum)
        assert actual == expected


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("budget", [1 << 100, 1 << 1000, 2.5])
def test_backbone_preserves_large_and_legacy_numeric_budgets(monkeypatch, native, budget):
    if native:
        _core("fast_backbone_indices")
    edges = [{"source": "a", "target": "b", "weight": 10.0},
             {"source": "b", "target": "c", "weight": 9.0},
             {"source": "a", "target": "c", "weight": 8.0}]
    monkeypatch.setattr(tool_graph, "HAVE_CORE", False)
    expected = tool_graph._extract_backbone_edges(edges, {"a", "b", "c"}, budget)
    monkeypatch.setattr(tool_graph, "HAVE_CORE", native)
    assert tool_graph._extract_backbone_edges(edges, {"a", "b", "c"}, budget) == expected
    assert len(expected[0]) == 3


def test_native_backbone_stable_duplicate_and_endpoint_ties():
    core = _core("fast_backbone_indices")
    # Dense ids are lexical ranks. Direction and stable row order decide the representative.
    edges = [(1, 0, 5.0, 0), (0, 1, 5.0, 1), (0, 1, 5.0, 2), (1, 2, 5.0, 3)]
    assert core.fast_backbone_indices(3, edges, 20) == [1, 3]
    assert core.fast_backbone_indices(3, edges, 1) == [1]
    assert core.fast_backbone_indices(3, edges, 0) == []
    for bad in [[(0, 3, 1.0, 0)], [(1, 1, 1.0, 0)], [(0, 1, math.nan, 0)]]:
        with pytest.raises(ValueError):
            core.fast_backbone_indices(3, bad, 10)
    with pytest.raises(ValueError):
        core.fast_backbone_indices(100001, [], 0)


def test_nonfinite_graph_weights_keep_legacy_path(monkeypatch):
    core = _core("fast_backbone_indices")

    def forbidden(*args):
        pytest.fail("Nonfinite weights must retain Python sorting semantics")

    monkeypatch.setattr(core, "fast_backbone_indices", forbidden)
    for value in [math.nan, math.inf, -math.inf]:
        result, degrees = tool_graph._extract_backbone_edges(
            [{"source": "a", "target": "b", "weight": value}], {"a", "b"}, 2, -math.inf
        )
        assert degrees == {"a": 1, "b": 1}
        assert math.isnan(result[0]["weight"]) if math.isnan(value) else result[0]["weight"] == value


def test_kernel_errors_are_not_disguised_as_empty_results(composite_db, monkeypatch):
    core = _core("NamePairIndex")

    def fail(*args, **kwargs):
        raise RuntimeError("injected native failure")

    monkeypatch.setattr(core, "NamePairIndex", fail)
    monkeypatch.setattr(core, "intersect_postings", fail)
    monkeypatch.setattr(core, "fast_backbone_indices", fail)
    with pytest.raises(RuntimeError, match="injected"):
        list(tool_lint._candidate_name_pairs(["a", "a"], 0.91))
    with pytest.raises(RuntimeError, match="injected"):
        gram._composite_candidates("abcd", composite_db[0])
    with pytest.raises(RuntimeError, match="injected"):
        tool_graph._extract_backbone_edges([{"source": "a", "target": "b"}], {"a", "b"})
