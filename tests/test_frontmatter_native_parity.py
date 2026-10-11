"""Native frontmatter splitting must preserve the existing Python line contract."""
from __future__ import annotations

import pytest
import yaml

from vector_lake import wiki_utils

core = pytest.importorskip("vector_lake_core", reason="native frontmatter parity")
requires_split = pytest.mark.skipif(
    not hasattr(core, "fast_split_frontmatter"), reason="native frontmatter ABI unavailable"
)

CASES = [
    "---\n---\nBody",
    "---\ntitle: x\n---oops\nBody",
    "---\r\n---\r\nBody",
    "---\ntitle: x\n---\nBody",
    "---\r\ntitle: 标题\r\n---\r\nBody\r\nTail",
    "---\ntitle: x\n---",
    "---\n---\r\nBody",
    "---\ntitle: x\n--- \nBody",
    "---\ntitle: x\n----\nBody",
    "---\ntitle: x\n ---\nBody",
    "---\ntitle: x\nno closing",
    "Plain body\n",
]


def _reference(content, monkeypatch):
    def forbid_native(_content):
        raise AssertionError("the Python frontmatter reference must not call Rust")

    with monkeypatch.context() as reference:
        reference.setattr(wiki_utils, "HAVE_CORE", False)
        reference.setattr(core, "fast_split_frontmatter", forbid_native)
        return wiki_utils.split_frontmatter(content)


@requires_split
@pytest.mark.parametrize("content", CASES)
def test_frontmatter_matches_independent_python(content, monkeypatch):
    expected = _reference(content, monkeypatch)
    calls = []
    native = core.fast_split_frontmatter

    def native_spy(value):
        calls.append(value)
        return native(value)

    with monkeypatch.context() as actual:
        actual.setattr(wiki_utils, "HAVE_CORE", True)
        actual.setattr(core, "fast_split_frontmatter", native_spy)
        result = wiki_utils.split_frontmatter(content)
    expected_calls = [content] if content.startswith(("---\n", "---\r\n")) else []
    assert calls == expected_calls
    assert result == expected


@requires_split
def test_empty_and_malformed_closing_lines_have_literal_results(monkeypatch):
    for content, expected in [
        ("---\n---\nBody", ({}, "Body")),
        ("---\ntitle: x\n---oops\nBody", ({}, "---\ntitle: x\n---oops\nBody")),
    ]:
        assert _reference(content, monkeypatch) == expected
        with monkeypatch.context() as actual:
            actual.setattr(wiki_utils, "HAVE_CORE", True)
            assert wiki_utils.split_frontmatter(content) == expected


@requires_split
def test_invalid_yaml_before_a_valid_closing_line_still_raises(monkeypatch):
    content = "---\ntitle: x\n---oops\n---\nBody"
    with pytest.raises(yaml.YAMLError):
        _reference(content, monkeypatch)
    with monkeypatch.context() as actual:
        actual.setattr(wiki_utils, "HAVE_CORE", True)
        with pytest.raises(yaml.YAMLError):
            wiki_utils.split_frontmatter(content)
