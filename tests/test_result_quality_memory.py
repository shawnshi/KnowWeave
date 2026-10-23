"""Synthetic entity relevance and display-only aggregation contracts."""
import copy
import json

import pytest

from vector_lake import db_store, governance_store as store, page_index_projection as projection
from vector_lake import retrieval_runtime as runtime, scoped_memory_search as scoped, tool_search as search
from vector_lake.wiki_utils import get_index_path

NAMES = ["Atlas-X", "Beacon-Y", "Cedar-Z", "Delta-Q", "Elm-R", "Flint-S", "Grove-T",
         "Harbor-U", "Iris-V", "Juniper-W"]


def memory(mid, **fields) -> dict:
    return dict(memory_id=mid, memory_type="fact", memory_key="release date", text="release date unknown",
                source_page="Concept_Unrelated.md", domain="General", topic_cluster="specs",
                memory_score=.9, validity_state="active", updated_at="2026-01-01", **fields)


@pytest.fixture
def entity_lake(isolated_memory):
    db_store.init_db()
    nodes = {f"Concept_{i}": dict(id=f"entity_{i}", title=name, aliases=[f"规格{i}规范"],
             type="concept", domain="General", topic_cluster="specs", status="Active")
             for i, name in enumerate(NAMES)}
    data = dict(nodes=nodes, weighted_edges=[])
    get_index_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    projection.reset_catalog_cache()
    projection.refresh_page_index_projection(data)
    records = [memory(f"noise_{i:03}") for i in range(120)]
    for i in range(10):
        item = memory(f"target_{i}")
        item.update(text=f"release date 2024-03-{i+1:02}", source_page=f"Concept_{i}.md", memory_score=.1)
        records.append(item)
    store.save_memory_objects({"items": {m["memory_id"]: m for m in records}})
    return records, data


@pytest.mark.parametrize("backend", ["legacy", "index", "gram"])
@pytest.mark.parametrize("domain", [None, "General"])
def test_ten_entity_questions_align_before_limit_not_after_top24(entity_lake, monkeypatch, backend, domain):
    monkeypatch.setenv(store.MEMORY_SEARCH_BACKEND_ENV, backend)
    for i, name in enumerate(NAMES):
        rows = store.search_operational_memory(f"{name} release date", top_k=2, domain=domain)
        assert [row["memory_id"] for row in rows] == [f"target_{i}"]
        assert rows[0]["text"] == f"release date 2024-03-{i+1:02}"


def test_subject_id_and_explicit_complete_name_also_align(entity_lake):
    records, _ = entity_lake
    subject = memory("subject")
    subject["subject_entity_ids"] = ["entity_0"]
    named = memory("named")
    named["text"] = "Atlas-X release date documented"
    fragment = memory("fragment")
    fragment["text"] = "Atlas release date unknown"
    store.save_memory_objects({"items": {m["memory_id"]: m for m in [subject, named, fragment]}})
    rows = store.search_operational_memory("Atlas-X release date", top_k=5, domain="General")
    assert {m["memory_id"] for m in rows} == {"subject", "named"}


@pytest.mark.parametrize("query", ["Atlas-X versus Beacon-Y release date", "release date", "规格 release date"])
def test_multi_entity_and_topic_queries_do_not_apply_single_entity_gate(entity_lake, query):
    rows = store.search_operational_memory(query, top_k=50, domain="General")
    assert any(m["memory_id"].startswith("noise") for m in rows)


def test_ambiguous_alias_does_not_select_first_page(entity_lake):
    _, data = entity_lake
    for node in data["nodes"].values():
        node["aliases"] = ["Shared Spec"]
    get_index_path().write_text(json.dumps(data), encoding="utf-8")
    projection.refresh_page_index_projection(data)
    rows = store.search_operational_memory("Shared Spec release date", top_k=5, domain="General")
    assert any(m["memory_id"].startswith("noise") for m in rows)


def test_alignment_only_gates_facts_and_preserves_history(entity_lake):
    records, _ = entity_lake
    for kind in ("preference", "decision", "task_state"):
        item = memory(kind)
        item["memory_type"] = kind
        records.append(item)
    old = memory("old")
    old.update(source_page="Concept_0.md", validity_state="expired")
    records.append(old)
    store.save_memory_objects({"items": {m["memory_id"]: m for m in records}})
    current, history = store.search_memory_packet_views("Atlas-X release date", domain="General")
    assert {m["memory_type"] for m in current} == {"fact", "preference", "decision", "task_state"}
    assert {m["memory_id"] for m in current if m["memory_type"] == "fact"} == {"target_0"}
    assert "old" in {m["memory_id"] for m in history}
    assert "old" not in {m["memory_id"] for m in current}


def test_fact_policy_is_not_active_equals_verified_and_runtime_policy_survives():
    packet = search.build_memory_packet("q", _views=([], []))["packet"]
    assert "prefer active non-conflicted memory items" not in packet
    assert "active is not verified" in packet
    assert "preferences, decisions and task state" in packet
    assert "source" in packet.lower() and "conflict" in packet.lower()


def test_display_grouping_preserves_every_member_and_never_mutates_input():
    a = memory("a")
    a.update(source_claim_id="claim_a", evidence_ids=["ev_a"], source_ids=["src"],
             subject_entity_ids=["subject"], locator={"occurrence": 1})
    b = copy.deepcopy(a)
    b.update(memory_id="b", source_claim_id="claim_b", evidence_ids=["ev_b"], locator={"occurrence": 2},
             subject_entity_ids=["other-subject"])
    before = copy.deepcopy([a, b])
    groups = store._aggregate_memory_display([a, b], 5)
    assert len(groups) == 1
    assert groups[0]["_display_members"] == before
    assert [a, b] == before
    text = search._format_memory_result(groups[0])
    for token in ("a", "b", "claim_a", "claim_b", "ev_a", "ev_b", "occurrence", "other-subject"):
        assert token in text


@pytest.mark.parametrize("changes", [dict(source_page="Elsewhere.md"), dict(source_ids=["other"]),
    dict(validity_state="conflicted"), dict(superseded_by="winner"), dict(valid_to="2020-01-01")])
def test_cross_source_or_incompatible_status_records_are_not_merged(changes):
    a = memory("a")
    b = memory("b")
    b.update(changes)
    assert len(store._aggregate_memory_display([a, b], 5)) == 2


@pytest.mark.parametrize("domain", [None, "General"])
def test_display_refills_only_within_existing_window_and_preserves_raw_ids(entity_lake, domain):
    items = [memory(f"dup{i:03}") for i in range(9)]
    extra = memory("unique")
    extra.update(text="release date different", memory_score=.1)
    items.append(extra)
    store.save_memory_objects({"items": {m["memory_id"]: m for m in items}})
    rows = store.search_operational_memory("release date", top_k=2, domain=domain, _display_groups=True)
    assert len(rows) == 2
    assert {m["memory_id"] for m in rows[0]["_display_members"]} == {m["memory_id"] for m in items[:9]}
    assert rows[1]["memory_id"] == "unique"
    assert len(store.search_operational_memory("release date", top_k=2, domain=domain)) == 2


def test_display_does_not_search_past_candidate_window_to_fill_slots(entity_lake):
    items = [memory(f'dup{i:03}') for i in range(30)]
    unique = memory('unique')
    unique.update(text='release date other', memory_score=.1)
    store.save_memory_objects({'items': {m['memory_id']: m for m in [*items, unique]}})
    with runtime.request_scope('release date') as state:
        rows = store.search_operational_memory('release date', top_k=2, domain='General', _display_groups=True)
    assert len(rows) == 1
    assert len(rows[0]['_display_members']) == max(2 * store.MEMORY_SEARCH_RESULT_WINDOW, 2 + 16)
    assert state.generations['memory_scope']['retained_rows'] == 18


def test_comparison_preserves_both_entities_and_does_not_drop_runtime_constraints(entity_lake):
    records = [memory('left'), memory('right')]
    records[0].update(source_page='Concept_0.md', text='Atlas-X release date documented')
    records[1].update(source_page='Concept_1.md', text='Beacon-Y release date documented')
    store.save_memory_objects({'items': {m['memory_id']: m for m in records}})
    rows = store.search_operational_memory('Atlas-X versus Beacon-Y release date', domain='General')
    assert {m['memory_id'] for m in rows} == {'left', 'right'}


def test_display_packet_is_bounded_and_keeps_ids_of_emitted_groups(entity_lake):
    items = [memory('dup-a'), memory('dup-b')]
    store.save_memory_objects({'items': {m['memory_id']: m for m in items}})
    roomy = search.build_memory_packet('release date', domain='General')['packet']
    assert 'dup-a' in roomy and 'dup-b' in roomy
    assert roomy.count('release date unknown') == 1
    tight = search.build_memory_packet('release date', domain='General', max_chars=220)
    assert len(tight['packet']) <= 220
    assert tight['omitted_count'] == 1
    assert ('dup-a' in tight['packet']) == ('dup-b' in tight['packet'])


def test_directory_generator_is_closed_when_checkpoint_fails(entity_lake, monkeypatch):
    catalog = projection._FileCatalog(entity_lake[1])
    closed = []
    def rows():
        try:
            yield 'Concept_0', entity_lake[1]['nodes']['Concept_0']
        finally:
            closed.append(True)
    monkeypatch.setattr(catalog, 'iter_name_fields', rows)
    def fail():
        raise runtime.RetrievalBudgetExceeded('synthetic checkpoint')
    monkeypatch.setattr(runtime, 'checkpoint', fail)
    with pytest.raises(runtime.RetrievalBudgetExceeded):
        search._name_entry_candidates('Atlas-X', catalog, None, None, False, None)
    assert closed == [True]


def test_alignment_budget_failure_is_explicit_and_read_snapshot_is_released(entity_lake, monkeypatch):
    monkeypatch.setattr(scoped, "MAX_SCANNED_ROWS", 1)
    conn = db_store.get_connection()
    with pytest.raises(runtime.RetrievalBudgetExceeded):
        store.search_operational_memory("Atlas-X release date", domain="General")
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0


def test_alignment_deadline_closes_directory_cursor(entity_lake, monkeypatch):
    conn = db_store.get_connection()
    with runtime.request_scope("Atlas-X release date") as state:
        state.deadline = 0
        with pytest.raises(runtime.RetrievalBudgetExceeded):
            store.search_operational_memory("Atlas-X release date", domain="General")
    assert runtime.current() is None
    conn.execute("SELECT 1").fetchone()


@pytest.mark.parametrize('query', ['医疗健康的发布情况', '元数据发布情况'])
def test_chinese_topic_substrings_do_not_become_confident_entities(entity_lake, query):
    _, data = entity_lake
    data['nodes'] = {'Concept_Topic': dict(title='医疗' if query.startswith('医疗') else '数据',
        type='concept', domain='General', status='Active')}
    get_index_path().write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    projection.refresh_page_index_projection(data)
    assert search._fact_entity_context(query, 'General') is None


def test_unicode_name_fragments_are_not_strong_mentions(entity_lake):
    _, data = entity_lake
    data['nodes'] = {'Concept_Name': dict(title='André', aliases=[], type='concept',
                                       domain='General', status='Active')}
    catalog = projection._FileCatalog(data)
    assert search._name_entry_candidates('Andréa release date', catalog, 'General', None, True, None) == {}


@pytest.mark.parametrize('source', ['Concept_0.md', 'folder/Concept_0.md', r'folder\Concept_0.md'])
def test_source_page_identity_supports_catalog_paths(entity_lake, source):
    item = memory('path')
    item['source_page'] = source
    entity = ('Concept_0', entity_lake[1]['nodes']['Concept_0'])
    assert search._memory_matches_entity(item, entity)


def test_unicode_lower_expansion_preserves_name_span_offsets():
    node = {'title': 'İris', 'aliases': []}
    assert search._confident_name_mentions('İris', 'Concept_Garden', node)


def test_malformed_subject_string_cannot_match_id_substring(entity_lake):
    item = memory('bad-subject')
    item['subject_entity_ids'] = 'prefix_entity_0_suffix'
    entity = ('Concept_0', entity_lake[1]['nodes']['Concept_0'])
    assert not search._memory_matches_entity(item, entity)


@pytest.mark.parametrize("query", [None, 42, ""])
def test_bad_public_inputs_fail_without_reading_memories(entity_lake, query):
    with pytest.raises(ValueError, match="query must contain"):
        search.search_vector_lake(query, mode="memory")
