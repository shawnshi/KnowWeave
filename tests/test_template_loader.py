"""Repository template ownership and fail-closed rendering, without live ingest."""
import ast
import re
from pathlib import Path

import pytest

from vector_lake.template_loader import read_template, render_template, render_template_text


ROOT = Path(__file__).resolve().parents[1]


def test_render_is_single_pass_and_preserves_nested_json():
    assert render_template_text('value={{value}}; {"outer":{"inner":1}}', value='{{untrusted}}') == 'value={{untrusted}}; {"outer":{"inner":1}}'


@pytest.mark.parametrize('text,values', [
    ('{{missing}}', {}), ('plain', {'unused': 1}), ('{{bad-name}}', {}),
])
def test_invalid_variables_fail_closed(text, values):
    with pytest.raises(ValueError):
        render_template_text(text, **values)


@pytest.mark.parametrize('name', ['../schema.md', '/schema.md', 'C:/schema.md', '..\\schema.md'])
def test_template_cannot_escape_root(name):
    with pytest.raises(ValueError):
        read_template(name)


def test_missing_template_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_template('absent.md', root=tmp_path)


def test_loading_and_rendering_use_the_supplied_root(tmp_path):
    (tmp_path / 'templates').mkdir()
    path = tmp_path / 'templates' / 'custom.md'
    path.write_text('first {{item}}', encoding='utf-8')
    assert render_template('custom.md', root=tmp_path, item='value') == 'first value'
    path.write_text('changed {{item}}', encoding='utf-8')
    assert render_template('custom.md', root=tmp_path, item='value') == 'changed value'


def test_ingest_injects_both_page_skeletons(monkeypatch, tmp_path):
    from vector_lake import tool_ingest
    monkeypatch.setattr(tool_ingest, '_read_purpose', lambda: 'purpose fixture')
    monkeypatch.setattr(tool_ingest, 'parse_static_skeleton', lambda _: '')
    source = tmp_path / 'source.md'
    source.write_text('synthetic source', encoding='utf-8')
    prompt = tool_ingest._build_ingest_instructions(str(source), 'raw-md5', 'Source_fixture.md', index_context='{{source_data}}')
    assert read_template('wiki/entity.md').strip() in prompt
    assert render_template('wiki/source.md', filepath=str(source), file_hash='raw-md5', canonical_name='Source_fixture.md').strip() in prompt
    assert '{{source_data}}' in prompt  # inserted candidate data is never a new template
    assert 'Raw MD5' in prompt
    assert 'Follow `templates/Source.md`' not in prompt


@pytest.mark.parametrize('name,content', [
    ('fixture.json', '{"a": 1}'), ('fixture.py', 'def example(): pass\n'), ('fixture.yaml', 'a: 1\n'),
])
def test_structured_source_prompt_explicitly_allows_the_supplied_skeleton(name, content, monkeypatch, tmp_path):
    from vector_lake import tool_ingest, skeleton_parser
    monkeypatch.setattr(tool_ingest, '_read_purpose', lambda: '')
    source = tmp_path / name
    source.write_text(content, encoding='utf-8')
    block = skeleton_parser.parse_static_skeleton(str(source))
    assert block.startswith('## 确定性结构 (Static Skeleton)')
    prompt = tool_ingest._build_ingest_instructions(str(source), tool_ingest.calculate_hash(str(source)), 'Source_fixture.md', index_context='')
    assert block in prompt
    assert 'Host-supplied Static Skeleton H2 is explicitly allowed on this Source page' in prompt
    assert 'Do not put it on an entity page' in prompt
    assert 'not an additional entity H2' in prompt
    assert 'Do not add a fourth content H2' not in prompt


def test_missing_wiki_skeleton_refuses_ingest(monkeypatch, tmp_path):
    from vector_lake import tool_ingest
    monkeypatch.setattr(tool_ingest, 'get_extension_root', lambda: tmp_path)
    monkeypatch.setattr(tool_ingest, '_read_purpose', lambda: '')
    monkeypatch.setattr(tool_ingest, 'parse_static_skeleton', lambda _: '')
    with pytest.raises(FileNotFoundError):
        tool_ingest._build_ingest_instructions('synthetic.md', 'hash', 'Source_fixture.md', index_context='')


def test_event_tags_are_injected_not_copied(monkeypatch):
    from vector_lake import tool_ingest, ingest_worker, ingest_output_contract, schema_validator
    monkeypatch.setattr(tool_ingest, '_read_purpose', lambda: '')
    monkeypatch.setattr(tool_ingest, 'parse_static_skeleton', lambda _: '')
    monkeypatch.setattr(schema_validator, 'INGEST_EVENT_TAGS', ('SyntheticEvent',))
    prompt = tool_ingest._build_ingest_instructions('fixture.md', 'hash', 'Source_fixture.md', index_context='')
    assert '[SyntheticEvent]' in prompt
    assert '[SyntheticEvent]' in ingest_worker._subagent_ingest_prompt('brief')
    assert 'SyntheticEvent' in ingest_output_contract.build_output_contract()


def test_every_text_template_has_valid_placeholder_syntax():
    for path in (ROOT / 'templates').rglob('*.md'):
        if path.name == 'README.md':
            continue
        text = path.read_text(encoding='utf-8')
        variables = set(re.findall(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}', text))
        render_template_text(text, **{name: 'synthetic' for name in variables})


def test_template_references_are_present_and_no_orphan_text_templates():
    references = set()
    for directory in ('vector_lake', 'scripts'):
        for path in (ROOT / directory).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    name = node.value
                    if name.startswith(('wiki/', 'prompts/')) and name.endswith('.md'):
                        references.add(name)
    actual = {path.relative_to(ROOT / 'templates').as_posix() for path in (ROOT / 'templates').rglob('*.md') if path.name != 'README.md'}
    assert references == actual, {'missing': references - actual, 'unused': actual - references}


def test_migrated_callers_do_not_embed_page_or_task_instructions():
    callers = (
        'vector_lake/ingest_worker.py', 'vector_lake/ingest_model_contract.py',
        'vector_lake/ingest_output_contract.py', 'scripts/ingest_model_pi_subagents.py',
        'vector_lake/stub_creator.py', 'vector_lake/tool_memory.py',
        'vector_lake/tool_projection.py', 'vector_lake/skeleton_parser.py',
        'scripts/community_clustering_daemon.py', 'scripts/compile_domain_overviews.py',
        'vector_lake/tool_query.py', 'vector_lake/tool_research.py', 'vector_lake/tool_review.py',
        'vector_lake/purpose_contract.py', 'scripts/launch_janitor_swarm.py',
    )
    findings = []
    for name in callers:
        tree = ast.parse((ROOT / name).read_text(encoding='utf-8-sig'))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                    docstrings.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                if len(node.value) >= 120 and re.search(r'(^|\n)(?:---\n|## |# )|You (?:are|must)|SYSTEM DIRECTIVE|System Directive|Agent:', node.value):
                    findings.append((name, node.lineno))
    assert not findings, findings
