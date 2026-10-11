"""Parity guard for the Rust block extractor that now feeds claim extraction.

Every fixture here is a semantic rule that the port had to reproduce, and each one was a measured
divergence at some point (see CHANGELOG 2026-09-25):

* a blockquote is never entered by mistune's walk, so nothing inside it is a claim;
* only items of a *top-level* list emit a block -- a nested item's text belongs to its parent item;
* a code block contributes one space and none of its contents;
* a soft break becomes a space, a hard break becomes **nothing** (mistune emits the token
  ``linebreak`` for a hard break, which the Python walk's ``("softbreak", "hardbreak")`` check does
  not match);
* a heading nested in a list or blockquote does not move ``current_heading``.

`text` is not compared: it is produced by the same unchanged Python cleaner from `raw_text`.
"""
from __future__ import annotations

import mistune
import pytest

from vector_lake import claim_extractor

core = pytest.importorskip("vector_lake_core", reason="the Rust core is not installed here")
# The parity assertions can only run against a core that implements this contract.  A host whose
# wheel predates it (2026-09-25 and earlier: 280-character cleaning, nested items emitted, nested
# headings moving current_heading) skips them -- `claim_extractor._rust_blocks` refuses that core by
# design, so there is nothing here to compare.  The gate's *negative* cases run everywhere.
_CONTRACT = getattr(core, "blocks_contract", None)
HAS_PARITY_CORE = bool(_CONTRACT) and _CONTRACT() == claim_extractor.BLOCKS_CONTRACT
requires_parity_core = pytest.mark.skipif(
    not HAS_PARITY_CORE, reason="installed vector_lake_core predates the claim-blocks contract"
)


def _signature(blocks):
    return [(b["kind"], b.get("heading"), b.get("raw_text", "")) for b in blocks]


def _rust_signature(body):
    return [(b.kind, b.heading, b.raw_text) for b in core.fast_extract_blocks(body)]


def _mistune_signature(body, monkeypatch):
    parser_calls = []
    create_markdown = mistune.create_markdown

    def parser_spy(*args, **kwargs):
        parser_calls.append(kwargs.get("renderer"))
        return create_markdown(*args, **kwargs)

    def forbid_native(_body):
        raise AssertionError("the independent reference must not call Rust")

    with monkeypatch.context() as reference:
        reference.setattr(claim_extractor, "CLAIM_BLOCK_BACKEND", "python")
        reference.setattr(core, "fast_extract_blocks", forbid_native)
        reference.setattr(mistune, "create_markdown", parser_spy)
        result = _signature(claim_extractor._iter_blocks(body))
    assert parser_calls == ["ast"]
    return result


FIXTURES = {
    "paragraph and heading": "# 标题\n\n一个段落。\n\n## 小节\n\n另一个段落。\n",
    "blockquote is not a claim": "前段。\n\n> **规则**: 引用块里的内容不是断言。\n\n后段。\n",
    "top-level list": "- 第一项\n- 第二项\n",
    "nested list folds into the parent item": "- 父项\n  - 子项甲\n  - 子项乙\n- 第二父项\n",
    "list item with a paragraph": "- 项目\n\n  项目内的段落\n\n- 下一项\n",
    "fenced code block inside an item": "- 项目说明\n\n  ```python\n  print('ignored')\n  ```\n\n- 下一项\n",
    "top-level code block contributes nothing": "前段。\n\n```\ncode only\n```\n\n后段。\n",
    "soft break becomes a space": "一行文字\n另一行文字\n",
    "hard break contributes nothing": "一行文字  \n另一行文字\n",
    "table cells are not claims": "| a | b |\n|---|---|\n| 1 | 2 |\n",
    "footnote definition is not a claim": "正文段落。\n\n[^1]: 脚注定义。\n",
    "inline code and emphasis are text": "段落里有 `代码` 与 **强调**。\n",
    "heading after a blockquote still applies": "> 引用\n\n## 章节\n\n段落内容。\n",
    "thematic break between paragraphs": "第一段。\n\n---\n\n第二段。\n",
}


@requires_parity_core
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_rust_matches_mistune(name, monkeypatch):
    body = FIXTURES[name]
    actual = _rust_signature(body)
    assert actual == _mistune_signature(body, monkeypatch), name


@requires_parity_core
@pytest.mark.parametrize("prefix", ["", "## Outer\n\n"])
def test_nested_heading_keeps_parent_item_and_outer_heading(prefix, monkeypatch):
    body = prefix + "- Parent\n\n  # Nested\n\n  Tail\n"
    expected = _mistune_signature(body, monkeypatch)
    heading = "Outer" if prefix else None
    assert expected == [("bullet", heading, "ParentNestedTail")]
    assert _rust_signature(body) == expected


@requires_parity_core
def test_the_corpus_level_result_is_recorded_here():
    """The fixtures above are the *rules*; this pins the measurement they came from.

    Measured over 1500 real page bodies (14 560 blocks): 1496 pages identical, and the 4 that differ
    are exactly the bodies carrying NUL bytes -- which the carve-out below sends to mistune, so
    production input is byte-identical everywhere.  An earlier run claiming 66x speedup and 7
    differing pages was reading `text.split('---',1)[1]` as the body, i.e. including the frontmatter
    (whose YAML list lines parse as top-level lists); the honest figures are 7.8x and these 4 pages.
    """
    nul_bodies = ["a\x00b", "有\ufffd替换"]
    for body in nul_bodies:
        assert claim_extractor._rust_blocks(body) is None
    assert claim_extractor._rust_blocks("干净正文。\n") is not None


def test_nul_and_replacement_bytes_stay_on_mistune(monkeypatch):
    """The two parsers disagree by content on those bodies, and the corpus holds none of those
    bytes -- so the Rust path must decline rather than introduce them."""
    assert claim_extractor._rust_blocks("含\x00空字节\n") is None
    assert claim_extractor._rust_blocks("含\ufffd替换字符\n") is None


@requires_parity_core
def test_a_contract_core_is_used_for_clean_bodies():
    assert claim_extractor._rust_blocks("干净正文。\n") is not None


def test_a_pre_parity_core_is_refused(monkeypatch):
    """Presence of `fast_extract_blocks` is not permission: the old build exported it too."""
    class OldCore:
        @staticmethod
        def fast_extract_blocks(_body):
            raise AssertionError("the old build must not be called")

    monkeypatch.setitem(__import__("sys").modules, "vector_lake_core", OldCore())
    monkeypatch.setattr(claim_extractor, "CLAIM_BLOCK_BACKEND", "rust")
    assert claim_extractor._rust_blocks("任意\n") is None


def test_a_mismatched_contract_is_refused(monkeypatch, caplog):
    class Drifted:
        @staticmethod
        def blocks_contract():
            return "claim-blocks-something-else"

        @staticmethod
        def fast_extract_blocks(_body):
            raise AssertionError("a mismatched build must not be called")

    monkeypatch.setitem(__import__("sys").modules, "vector_lake_core", Drifted())
    monkeypatch.setattr(claim_extractor, "CLAIM_BLOCK_BACKEND", "rust")
    assert claim_extractor._rust_blocks("任意\n") is None
    assert "blocks_contract()" in caplog.text


def test_backend_switch_forces_mistune(monkeypatch):
    monkeypatch.setattr(claim_extractor, "CLAIM_BLOCK_BACKEND", "python")
    assert claim_extractor._rust_blocks("任意\n") is None


def test_missing_symbol_falls_back(monkeypatch):
    class ShortCore:
        pass

    monkeypatch.setitem(__import__("sys").modules, "vector_lake_core", ShortCore())
    monkeypatch.setattr(claim_extractor, "CLAIM_BLOCK_BACKEND", "rust")
    assert claim_extractor._rust_blocks("任意\n") is None


def test_a_faulting_core_falls_back(monkeypatch, caplog):
    class Exploding:
        @staticmethod
        def blocks_contract():
            return claim_extractor.BLOCKS_CONTRACT

        @staticmethod
        def fast_extract_blocks(_body):
            raise RuntimeError("boom")

    monkeypatch.setitem(__import__("sys").modules, "vector_lake_core", Exploding())
    monkeypatch.setattr(claim_extractor, "CLAIM_BLOCK_BACKEND", "rust")
    assert claim_extractor._rust_blocks("任意\n") is None
    assert "using mistune" in caplog.text


