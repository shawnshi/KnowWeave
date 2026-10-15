"""Golden synthetic outputs recorded before template extraction, never real vault content."""
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vector_lake.template_loader import render_template


ROOT = Path(__file__).resolve().parents[1]
FIXED_ISO = '2026-10-07T12:00:00+00:00'


def fingerprint(text, *, normalized=False):
    if normalized:
        text = ' '.join(text.split())
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sample_packet():
    return {
        'prompt': 'SYNTHETIC SOURCE BRIEF',
        'metadata': {
            'output_contract': 'SYNTHETIC OUTPUT CONTRACT',
            'processed_data': {
                'filepath': 'fixture.md', 'hash': hashlib.md5(b'synthetic raw source').hexdigest(), 'canonical_name': 'Source_fixture.md',
                'source_hash': '', 'source_projection_hash': '', 'ingest_contract_version': 5,
                'integration_candidates': [],
            },
        },
    }


def sample_contract():
    return {
        'purpose_version': '12.2', 'intent_keywords': ['synthetic'], 'intent_weight_boost': 0.0,
        'synthesis_policy': {'min_distinct_sources': 2, 'min_tension_intensity': 0.8},
        'scope': {'core': ['core'], 'edge': ['edge'], 'excluded': ['excluded'], 'marketing_noise': ['noise']},
        'evidence_tiers': {'primary': 'Synthetic primary evidence.', 'derived': 'Synthetic derived evidence.'},
        'sir_registry': [
            {'id': 'sir1', 'status': 'active', 'signal_keywords': ['signal1', 'signal2'], 'review_after': '2026-11-01'},
            {'id': 'sir2', 'status': 'deprecated', 'signal_keywords': ['signal3'], 'review_after': '2026-11-01'},
        ],
    }


def overview_nodes(count):
    nodes = {}
    for i in range(count):
        nodes[f'Concept_fixture{i}'] = {
            'domain': 'Alpha', 'node_score': i / 3, 'type': 'Product' if i % 2 else 'Concept',
            'updated': '2026-10-06' if i % 3 else '2025-01-01',
            'summary': f'summary {i}' if i % 2 else '', 'links': ['Concept_beta'],
        }
    nodes['Concept_beta'] = {'domain': 'Beta', 'node_score': 1, 'updated': '2025-01-01'}
    return nodes


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 7)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 7, 12, tzinfo=timezone.utc)


def current_outputs(tmp_path, monkeypatch):
    from scripts import compile_domain_overviews as overview, ingest_model_pi_subagents as relay
    from vector_lake import ingest_model_contract, ingest_worker, ingest_output_contract, purpose_contract
    from vector_lake import skeleton_parser, stub_creator, tool_memory, tool_projection, wiki_utils
    source = tmp_path / 'fixture-cli-source.md'
    source.write_bytes(b'synthetic raw source')
    monkeypatch.setattr(wiki_utils, 'resolve_ingest_source_path', lambda _: source)
    monkeypatch.setattr(relay, 'datetime', FixedDatetime)
    outputs = {}
    outputs['handoff'] = fingerprint(ingest_worker._subagent_ingest_prompt('BRIEF'), normalized=True)
    outputs['output_contract'] = fingerprint(ingest_output_contract.build_output_contract())
    outputs['cli'] = fingerprint(ingest_model_contract.build_cli_prompt(sample_packet()))
    outputs['relay'] = fingerprint(relay._brief(sample_packet()))
    outputs['repair_empty'] = fingerprint(relay._repair_block({'validation_error': 'synthetic error'}))
    outputs['repair_previous'] = fingerprint(relay._repair_block({'validation_error': 'synthetic error', 'previous_output': {'x': 1}}))
    outputs['strategy'] = fingerprint(purpose_contract.render_strategy_directive(sample_contract()))
    for node_type in ('concept', 'vendor', 'institution', 'product', 'person', 'event', 'policy', 'standard'):
        outputs[f'stub_{node_type}'] = fingerprint(stub_creator.stub_body(f'{node_type.capitalize()}_fixture', node_type, '2026-10-07'))
    for memory_type in ('fact', 'decision', 'preference', 'task_state'):
        fm, body = wiki_utils.split_frontmatter(tool_memory._new_memory_page(memory_type, 'Fixture', FIXED_ISO))
        outputs[f'memory_{memory_type}'] = fingerprint(json.dumps(fm, sort_keys=True, default=str, ensure_ascii=False) + body)
    monkeypatch.setattr(tool_projection, 'datetime', FixedDatetime)
    for node_type in ('source', 'concept', 'synthesis'):
        outputs[f'restored_{node_type}'] = fingerprint(tool_projection._body_from_entity({'canonical_name': 'Fixture'}, {'title': 'Fixture', 'type': node_type}))
    source_cases = {
        'object.json': '{"a": 1, "b": "x"}', 'array.json': '[1, 2]', 'empty.json': '[]',
        'scalar.json': 'true', 'fixture.py': 'import os\nclass Example: pass\ndef run(): pass\n',
        'fixture.yaml': 'a: 1\nb: two\n', 'array.yaml': '- one\n', 'plain.md': 'plain', 'broken.json': '{',
    }
    for name, text in source_cases.items():
        path = tmp_path / name
        path.write_text(text, encoding='utf-8')
        outputs[f'skeleton_{name}'] = fingerprint(skeleton_parser.parse_static_skeleton(str(path)))
    captured = {}
    index = tmp_path / 'index.json'
    monkeypatch.setattr(overview, 'get_index_path', lambda: index)
    monkeypatch.setattr(overview, 'get_wiki_dir', lambda: tmp_path)
    monkeypatch.setattr(overview, 'datetime', SimpleNamespace(date=FixedDate, datetime=datetime))
    monkeypatch.setattr(wiki_utils, 'safe_write_markdown', lambda path, content: captured.__setitem__(Path(path).name, content))
    for count in (1, 55):
        index.write_text(json.dumps({'nodes': overview_nodes(count)}), encoding='utf-8')
        captured.clear()
        overview.compile_overviews()
        for name, text in captured.items():
            outputs[f'overview_{count}_{name}'] = fingerprint(text)
    outputs['community'] = fingerprint(render_template(
        'wiki/community.md', id='System_Community_L0_fixture', label_yaml='"Fixture"',
        updated=FIXED_ISO, community_id='fixture', level='L0', label='Fixture', zoom='Global',
        hubs='- [[Concept_A]]', members='- [[Concept_B]]', summary='summary', delta='',
    ))
    return outputs


def test_outputs_match_pre_refactor_golden(tmp_path, monkeypatch):
    expected = json.loads((ROOT / 'tests/fixtures/template_outputs.json').read_text(encoding='utf-8'))
    actual = current_outputs(tmp_path, monkeypatch)
    assert set(actual) == set(expected)
    mismatched = {key: (expected[key], value) for key, value in actual.items() if expected[key] != value}
    assert not mismatched, mismatched


def test_skeleton_template_failure_is_not_reported_as_parse_error(tmp_path, monkeypatch):
    from vector_lake import skeleton_parser, template_loader
    path = tmp_path / 'fixture.json'
    path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(template_loader, 'get_extension_root', lambda: tmp_path)
    with pytest.raises(FileNotFoundError):
        skeleton_parser.parse_static_skeleton(str(path))


@pytest.mark.parametrize('script', [
    'community_clustering_daemon.py', 'compile_domain_overviews.py', 'launch_janitor_swarm.py',
])
def test_scripts_bootstrap_in_a_fresh_process_without_running_main(script, tmp_path):
    memory = tmp_path / 'MEMORY'
    memory.mkdir()
    env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'PATH', 'TEMP', 'TMP') if key in os.environ}
    env['VECTOR_LAKE_MEMORY_DIR'] = str(memory)
    probe = "import runpy,sys; runpy.run_path(sys.argv[1], run_name='review_probe'); print('BOOTSTRAP_OK')"
    result = subprocess.run(
        [sys.executable, '-I', '-B', '-c', probe, str(ROOT / 'scripts' / script)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert 'BOOTSTRAP_OK' in result.stdout
    assert not list(memory.rglob('*')), 'import must not start a production or synthetic mutation'
