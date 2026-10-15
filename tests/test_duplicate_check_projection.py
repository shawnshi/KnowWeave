"""The duplicate check must answer from the projection exactly as it did from ``index.json``.

It walked every node once reading ``title``/``type``/``aliases``/``summary`` and paid a 17 MB parse
to do it; the page projection carries each node's payload verbatim (``node_json``), verified equal
on the live corpus (7 157 nodes, 0 missing, 0 extra, 0 differing).  On top of that the pass
re-tokenised all 7 157 node summaries on *every* call -- 1.07 s of the similarity loop under
cProfile -- so the tokenisation is now cached, which is exact because it is a pure function of one
string.  Both changes are performance-only, so the assertions are equivalence, not speed.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter

import pytest

from vector_lake import db_store, tool_entity_dedup
from vector_lake.wiki_utils import get_index_path

NODES = {
    "Concept_HIS": {
        "title": "HIS",
        "type": "concept",
        "aliases": ["医院信息系统"],
        "summary": "HIS 集成平台 信创 选型",
    },
    "Vendor_Acme": {
        "title": "Acme",
        "type": "vendor",
        "aliases": [],
        "summary": "供应商 影像 云 平台",
    },
}


@pytest.fixture
def indexed(isolated_memory):
    """The file *and* its projection, since the check reads the projection and falls back to the file."""
    from vector_lake import page_index_projection

    db_store.init_db()
    index_data = {"nodes": NODES, "weighted_edges": [], "graph_state": {"dirty": False}}
    path = get_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index_data), encoding="utf-8")
    page_index_projection.refresh_page_index_projection(index_data)
    return isolated_memory


def _plain_tokens(text: str) -> Counter:
    """The tokenisation as it was written inline, for the equivalence assertion."""
    tokens: Counter = Counter()
    lowered = text.lower()
    cjk = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", lowered)
    for char in cjk:
        tokens[char] += 1
    for index in range(len(cjk) - 1):
        tokens[cjk[index] + cjk[index + 1]] += 1
    for word in re.findall(r"[a-z0-9]+", lowered):
        tokens[word] += 2
    return tokens


def _plain_similarity(text1: str, text2: str) -> float:
    vector1, vector2 = _plain_tokens(text1), _plain_tokens(text2)
    shared = set(vector1) & set(vector2)
    numerator = sum(vector1[key] * vector2[key] for key in shared)
    denominator = math.sqrt(sum(value**2 for value in vector1.values())) * math.sqrt(
        sum(value**2 for value in vector2.values())
    )
    return 0.0 if not denominator else numerator / denominator


def test_the_token_vector_is_unchanged_by_being_cached():
    for text in ("HIS 集成平台 信创 选型", "医院 HIS 集成 平台", "", "纯中文标题", "Mixed 中文 123"):
        assert tool_entity_dedup._token_vector(text) == _plain_tokens(text), text


def test_the_similarity_matches_a_plain_recomputation():
    pairs = [
        ("HIS 集成平台", "医院 HIS 集成 平台"),
        ("信创", "信创 集成平台"),
        ("", ""),
        ("", "HIS"),
        ("完全无关", "another thing"),
    ]
    for left, right in pairs:
        assert abs(tool_entity_dedup._calculate_cosine_similarity(left, right) - _plain_similarity(left, right)) < 1e-12


def test_the_token_cache_is_bounded():
    """C-PERF-03: a cache keyed on corpus text must not grow with the corpus."""
    assert tool_entity_dedup._token_vector.cache_info().maxsize == 20_000
    assert tool_entity_dedup._vector_norm_squared.cache_info().maxsize == 20_000


def test_the_projection_payloads_equal_the_file(indexed):
    """The premise of the swap, asserted rather than assumed."""
    file_nodes = json.loads(get_index_path().read_text(encoding="utf-8"))["nodes"]
    projected = tool_entity_dedup._dedup_node_payloads()

    assert projected is not None, "the fixture did not build the projection"
    assert projected == file_nodes


def test_the_check_falls_back_to_the_file_when_the_projection_is_unusable(indexed, monkeypatch):
    """A projection that cannot be read must not become "no duplicates"."""
    monkeypatch.setattr(tool_entity_dedup, "_dedup_node_payloads", lambda: None)

    result = json.loads(tool_entity_dedup.check_duplicate_entity("HIS", "concept", "HIS 集成平台"))

    assert result["is_duplicate"] is True
    assert result["existing_key"] == "Concept_HIS"


def test_a_missing_index_is_still_reported(indexed):
    get_index_path().unlink()

    result = json.loads(tool_entity_dedup.check_duplicate_entity("HIS", "concept"))

    assert result == {"is_duplicate": False, "reason": "No index exists yet."}


def test_an_unindexed_type_is_still_skipped(indexed):
    result = json.loads(tool_entity_dedup.check_duplicate_entity("HIS", "unknown-type"))

    assert result["is_duplicate"] is False
    assert "does not require deduplication" in result["reason"]
