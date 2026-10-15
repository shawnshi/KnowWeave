"""Schema/purpose/template alignment using only synthetic policies and temporary vaults."""
import copy

import pytest

from vector_lake.purpose_contract import PurposeContractError, validate_ingest_payload, validate_purpose_contract
from vector_lake.tool_memory import _new_memory_page, update_operational_memory
from vector_lake.tool_projection import _frontmatter_from_entity, _body_from_entity
from vector_lake.wiki_utils import split_frontmatter
from vector_lake.yaml_utils import dump_yaml


def strict_contract():
    return validate_purpose_contract({
        'purpose_version': '12.2', 'intent_keywords': ['synthetic'], 'intent_weight_boost': 0.0,
        'scope': {'core': ['synthetic'], 'edge': ['edge'], 'excluded': ['noise'], 'marketing_noise': ['marketing']},
        'evidence_tiers': {'policy-directive': 'A synthetic policy directive.', 'accreditation-record': 'A synthetic accreditation record.'},
        'sir_registry': [{'id': 'SIR_FIXTURE', 'status': 'active', 'review_after': '2099-01-01', 'signal_keywords': ['synthetic']}],
        'synthesis_policy': {'min_distinct_sources': 2, 'min_tension_intensity': 0.8},
    })


def write_strict_purpose(memory):
    contract = strict_contract()
    (memory / 'purpose.md').write_text('---\n' + dump_yaml(contract, sort_keys=False) + '---\nSynthetic policy only.\n', encoding='utf-8')
    return contract


def test_new_operational_memory_does_not_fabricate_a_business_tier(isolated_memory):
    contract = write_strict_purpose(isolated_memory)
    content = _new_memory_page('fact', 'Synthetic', '2026-10-07T00:00:00+00:00')
    fm, _ = split_frontmatter(content)
    assert 'evidence_tier' not in fm
    assert fm['memory_type'] == 'fact'
    assert fm['sources'] == ['Operational_Memory']
    validate_ingest_payload([{'filename': 'Concept_OperationalFacts.md', 'content': content}], contract)
    update_operational_memory('fact', 'Synthetic operating record, not graded business evidence.')
    update_operational_memory('fact', 'Another synthetic record.')
    path = isolated_memory / 'wiki/Concept_OperationalFacts.md'
    persisted, _ = split_frontmatter(path.read_text(encoding='utf-8'))
    assert 'evidence_tier' not in persisted


@pytest.mark.parametrize('grade', ['derived', 'primary', 0, False, None, '', ' \t '])
def test_existing_invalid_memory_tier_is_not_silently_erased(isolated_memory, grade):
    from vector_lake.defense_hook import DefenseHookException
    write_strict_purpose(isolated_memory)
    path = isolated_memory / 'wiki/Concept_OperationalFacts.md'
    content = _new_memory_page('fact', 'Synthetic', '2026-10-07T00:00:00+00:00')
    frontmatter, body = split_frontmatter(content)
    frontmatter['evidence_tier'] = grade
    content = '---\n' + dump_yaml(frontmatter, sort_keys=False) + '---\n' + body
    path.write_text(content, encoding='utf-8')
    with pytest.raises(DefenseHookException, match='evidence_tier'):
        update_operational_memory('fact', 'Synthetic update must not wash out the explicit old grade.')
    assert path.read_text(encoding='utf-8') == content


def test_recovery_does_not_invent_missing_grade_and_preserves_explicit_grade():
    entity = {'page_key': 'Concept_Synthetic', 'canonical_name': 'Synthetic', 'categories': ['System_Architecture'], 'updated': '2026-10-07'}
    fm = _frontmatter_from_entity(entity)
    assert 'evidence_tier' not in fm
    body = _body_from_entity(entity, fm)
    content = '---\n' + dump_yaml(fm, sort_keys=False) + '---\n' + body
    validate_ingest_payload([{'filename': 'Concept_Synthetic.md', 'content': content}], strict_contract())
    for grade in ('policy-directive', 'unsupported-explicit-grade', 0, False, None, '', ' \t '):
        fm = _frontmatter_from_entity({**entity, 'evidence_tier': grade})
        assert fm['evidence_tier'] == grade
        candidate = '---\n' + dump_yaml(fm, sort_keys=False) + '---\n' + body
        if grade == 'policy-directive':
            validate_ingest_payload([{'filename': 'Concept_Synthetic.md', 'content': candidate}], strict_contract())
        else:
            with pytest.raises(PurposeContractError, match='evidence_tier'):
                validate_ingest_payload([{'filename': 'Concept_Synthetic.md', 'content': candidate}], strict_contract())


def test_one_runtime_snapshot_drives_main_handoff_output_and_relay(monkeypatch):
    import json
    from datetime import datetime, timezone
    from scripts import ingest_model_pi_subagents as relay
    from scripts.ingest_model_pi_subagents import _runtime_schema_contract

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            assert tz is timezone.utc
            return cls(2030, 1, 2, tzinfo=timezone.utc)

    monkeypatch.setattr(relay, 'datetime', FixedDatetime)
    from vector_lake import tool_ingest, schema_validator as schema
    from vector_lake.ingest_schema_contract import schema_snapshot, render_schema_contract
    from vector_lake.ingest_worker import _subagent_ingest_prompt
    from vector_lake.ingest_output_contract import build_output_contract
    monkeypatch.setattr(tool_ingest, '_read_purpose', lambda: '')
    monkeypatch.setattr(tool_ingest, 'parse_static_skeleton', lambda _: '')
    monkeypatch.setattr(schema, 'DOMAIN_VERTICALS', schema.DOMAIN_VERTICALS | {'Synthetic_Vertical'})
    monkeypatch.setattr(schema, 'MAX_TAGS', 4)
    monkeypatch.setattr(schema, 'DOMAIN_ALIASES', {**schema.DOMAIN_ALIASES, 'Synthetic_Alias': 'Medical_IT'})
    monkeypatch.setattr(schema, 'INGEST_EVENT_TAGS', ('SyntheticEvent',))
    snapshot = schema_snapshot()
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    main = tool_ingest._build_ingest_instructions('synthetic.md', 'hash', 'Source_synthetic.md', index_context='')
    assert encoded in main
    assert _runtime_schema_contract() == render_schema_contract(snapshot) + (
        '\n\nHOST NODE UPDATE DATE (UTC): 2030-01-02\n'
        'A Source page must use literal type: source, not Source; updated must not be null. '
        'Use the host node update date for updated. This is the compilation/edit date, '
        "not the source's publication or event date; never invent an unknown event date."
    )
    assert '[SyntheticEvent]' in _subagent_ingest_prompt(main)
    assert 'tags: at most 4' in _subagent_ingest_prompt(main)
    assert '**Tags (标签)**: At most 4' in main
    assert 'SyntheticEvent' in build_output_contract()
    assert 'domain_aliases' in main and 'Synthetic_Vertical' in main
    assert 'pick one of the 9 values' not in main
    assert 'from the macro-domains' not in main


def test_strategy_passes_version_definitions_thresholds_without_mutating_input():
    from vector_lake.purpose_contract import render_strategy_directive
    contract = strict_contract()
    original = copy.deepcopy(contract)
    text = render_strategy_directive(contract)
    assert contract == original
    assert 'Purpose version: 12.2' in text
    assert 'at least 2 distinct sources' in text
    assert 'intensity >= 0.8' in text
    for name, definition in contract['evidence_tiers'].items():
        assert f'{name}: {definition}' in text
    assert 'SIR_FIXTURE' in text
    changed = copy.deepcopy(contract)
    changed['purpose_version'] = '12.7'
    changed['synthesis_policy'] = {'min_distinct_sources': 3, 'min_tension_intensity': 0.95}
    changed['evidence_tiers']['policy-directive'] = 'Changed synthetic definition.'
    updated = render_strategy_directive(changed)
    assert 'Purpose version: 12.7' in updated
    assert 'at least 3 distinct sources' in updated and 'intensity >= 0.95' in updated
    assert 'Changed synthetic definition.' in updated
    with pytest.raises(PurposeContractError):
        render_strategy_directive({'scope': {}})


def test_purpose_body_is_not_policy_and_model_data_is_not_recursively_rendered(isolated_memory, monkeypatch):
    from vector_lake import tool_ingest
    contract = write_strict_purpose(isolated_memory)
    contract['evidence_tiers']['policy-directive'] = 'Synthetic definition with {{opaque_data}}.'
    path = isolated_memory / 'purpose.md'
    path.write_text('---\n' + dump_yaml(contract, sort_keys=False) + '---\nBODY IS NOT EXECUTABLE POLICY\n', encoding='utf-8')
    monkeypatch.setattr(tool_ingest, 'parse_static_skeleton', lambda _: '')
    text = tool_ingest._build_ingest_instructions('synthetic.md', 'hash', 'Source_synthetic.md', index_context='')
    assert '{{opaque_data}}' in text
    assert 'BODY IS NOT EXECUTABLE POLICY' not in text
    assert 'accreditation-record' in text
    assert 'Purpose version: 12.2' in text


def test_hydrated_cli_and_relay_keep_common_schema_and_complete_policy(isolated_memory):
    import json
    from vector_lake import tool_ingest
    from vector_lake.ingest_model_contract import build_cli_prompt
    from vector_lake.ingest_schema_contract import schema_snapshot
    from scripts.ingest_model_pi_subagents import _brief
    write_strict_purpose(isolated_memory)
    source = isolated_memory / 'raw/synthetic.md'
    source.write_text('Synthetic source, no private records.', encoding='utf-8')
    prompt = tool_ingest._build_ingest_instructions(str(source), tool_ingest.calculate_hash(str(source)), 'Source_synthetic.md', index_context='')
    packet = {'prompt': prompt, 'metadata': {
        'output_contract': 'Synthetic output contract',
        'processed_data': {'filepath': str(source), 'hash': tool_ingest.calculate_hash(str(source)),
                           'canonical_name': 'Source_synthetic.md', 'integration_candidates': []},
    }}
    encoded = json.dumps(schema_snapshot(), ensure_ascii=False, sort_keys=True)
    for text in (build_cli_prompt(packet), _brief(packet)):
        assert encoded in text
        assert 'Purpose version: 12.2' in text
        assert 'A synthetic accreditation record.' in text
        assert 'at least 2 distinct sources' in text and 'intensity >= 0.8' in text


def test_taxonomy_document_matches_code_but_is_not_the_runtime_owner():
    import re
    from pathlib import Path
    from vector_lake import schema_validator as schema
    root = Path(__file__).resolve().parents[1]
    text = (root / 'SCHEMA_CATEGORIES.md').read_text(encoding='utf-8')
    categories = text.split('## Allowed Categories', 1)[1].split('## Enforcement', 1)[0]
    domains = text.split('### 第一层', 1)[1].split('### 第二层', 1)[0]
    verticals = text.split('### 第二层', 1)[1].split('#### 别名登记', 1)[0]
    category_names = set(re.findall(r'^- `([^`]+)`:', categories, re.M))
    macro_names = set(re.findall(r'^- `([^`]+)`', domains, re.M))
    vertical_names = {name for line in verticals.splitlines() if line.startswith('| `')
                      for name in re.findall(r'`([^`]+)`', line.split('|')[1])}
    aliases = text.split('#### 别名登记', 1)[1]
    alias_map = {}
    for line in aliases.splitlines():
        if line.startswith('| `'):
            values = re.findall(r'`([^`]+)`', '|'.join(line.split('|')[1:3]))
            alias_map[values[0]] = values[1]
    assert category_names == set(schema.VALID_CATEGORIES)
    assert macro_names == set(schema.VALID_DOMAINS)
    assert vertical_names == set(schema.DOMAIN_VERTICALS)
    assert alias_map == schema.DOMAIN_ALIASES
    assert 'Editing this Markdown alone never changes' in text


def test_schema_document_links_skeletons_without_pinning_user_strategy():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / 'schema.md').read_text(encoding='utf-8')
    assert 'templates/wiki/entity.md' in text and 'templates/wiki/source.md' in text
    assert '# [[Title]]' not in text
    assert 'Strategic Contract V12.1' not in text
    assert 'evidence_tier: "code-availability |' not in text
    assert 'Operational memory uses' in text


def test_schema_snapshot_is_a_copy_of_real_rule_owners():
    from vector_lake import schema_validator as schema
    from vector_lake.ingest_schema_contract import schema_snapshot
    from vector_lake.node_vocabulary import GENERATED_NODE_TYPES, NODE_PREFIXES, type_for_prefix
    snapshot = schema_snapshot()
    assert snapshot['required_frontmatter'] == list(schema.REQUIRED_FIELDS)
    assert snapshot['categories'] == sorted(schema.VALID_CATEGORIES)
    assert snapshot['domain'] == sorted(schema.VALID_DOMAINS | schema.DOMAIN_VERTICALS)
    assert snapshot['macro_domains'] == sorted(schema.VALID_DOMAINS)
    assert snapshot['domain_verticals'] == sorted(schema.DOMAIN_VERTICALS)
    assert snapshot['domain_aliases'] == schema.DOMAIN_ALIASES
    assert snapshot['h3_slots'] == {key: list(value) for key, value in schema.VALID_H3_SLOTS.items()}
    assert snapshot['page_predicates'] == sorted(schema.VALID_PREDICATES)
    assert snapshot['integration_predicates'] == sorted(schema.INGEST_INTEGRATION_PREDICATES)
    assert snapshot['event_tags'] == list(schema.INGEST_EVENT_TAGS)
    assert snapshot['controlled_metrics'] == sorted(schema.CONTROLLED_METRICS)
    assert snapshot['synthesis_headings'] == list(schema.SYNTHESIS_SKELETON_HEADINGS)
    assert snapshot['knowledge_prefixes'] == [prefix for prefix in NODE_PREFIXES if type_for_prefix(prefix) not in GENERATED_NODE_TYPES]
    assert snapshot['max_tags'] == schema.MAX_TAGS
    snapshot['domain_aliases']['SyntheticAlias'] = 'General'
    snapshot['h3_slots']['concept'].clear()
    assert 'SyntheticAlias' not in schema.DOMAIN_ALIASES
    assert schema.VALID_H3_SLOTS['concept']
