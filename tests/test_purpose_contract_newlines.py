"""Purpose/node frontmatter boundaries accept platform newlines, not weaker gates."""
import pytest

from vector_lake.purpose_contract import (
    PurposeContractError,
    _parse_frontmatter,
    _parse_node_frontmatter,
)


@pytest.mark.parametrize('ending', ['\n', '\r\n'])
def test_frontmatter_parser_accepts_newlines_without_changing_fields(ending):
    content = ending.join(['---', 'title: Example', 'strategic_scope: core',
                           'evidence_tier: code-availability', 'sources: [raw/example.md]',
                           '---', 'Body with --- inside a sentence.', ''])
    assert _parse_frontmatter(content) == {
        'title': 'Example', 'strategic_scope': 'core',
        'evidence_tier': 'code-availability', 'sources': ['raw/example.md'],
    }
    assert _parse_node_frontmatter(content, 'Product_Example.md')['strategic_scope'] == 'core'


@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('lines', [
    ['title: Missing opening', '---', 'body'],
    ['---', 'title: Missing closing', 'body'],
    ['---', '- not a mapping', '---', 'body'],
    ['----', 'title: Wrong delimiter', '---', 'body'],
])
def test_platform_newlines_do_not_bypass_frontmatter_rejections(ending, lines):
    with pytest.raises(PurposeContractError):
        _parse_frontmatter(ending.join(lines))


@pytest.mark.parametrize('content', ['a\nb\n', 'a\r\nb\r\n', '\ufeff中文\r\nnext\n'])
def test_atomic_writer_preserves_the_supplied_utf8_bytes(tmp_path, content):
    from vector_lake.wiki_utils import atomic_write_text
    target = tmp_path / 'payload.txt'
    atomic_write_text(target, content)
    assert target.read_bytes() == content.encode('utf-8')
    assert list(tmp_path.glob('*.tmp')) == []
