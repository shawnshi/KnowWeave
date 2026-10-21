"""Synthetic regression oracles for scoped maintenance and atomic stale refusal."""
import json
import os
import time

import pytest
import yaml

from vector_lake import db_store, governance_store
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.tool_gc import _topological_degrees, gc_vector_lake
from vector_lake.tool_rename import _replace_resolved_links, rename_vector_lake_entity


def seed(root, key, *, status="Archived", domain="General", cluster="One", scope="peripheral", body="A factual body.", explicit_identity=True):
    frontmatter = {"id": "id_" + key, "entity_id": "entity_" + key, "title": key.split("_", 1)[1],
                   "type": key.split("_", 1)[0].lower(), "domain": domain, "status": status,
                   "categories": ["Uncategorized"], "updated": "2026-09-17", "sources": [],
                   "strategic_scope": scope, "topic_cluster": cluster, "aliases": []}
    frontmatter["epistemic-status"] = "seed"
    if not explicit_identity:
        frontmatter.pop("entity_id")
    body = "## 1. 编译事实\n\n" + body + "\n\n## 2. 证据时间线\n\n"
    content = "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n" + body
    execute_mutation_batch([{"filename": key + ".md", "content": content}], validation_mode="schema")
    path = root / "wiki" / (key + ".md")
    old = time.time() - 90 * 86400
    os.utime(path, (old, old))
    return path


def preview(**kwargs):
    text = gc_vector_lake(**kwargs)
    return json.loads(text.split("Expected plan: ", 1)[1])


def counts():
    conn = db_store.get_connection()
    return (
        conn.execute("SELECT count(*) FROM entities").fetchone()[0],
        conn.execute("SELECT count(*) FROM claims").fetchone()[0],
        conn.execute("SELECT count(*) FROM mutation_outbox").fetchone()[0],
        conn.execute("SELECT count(*) FROM change_sets").fetchone()[0],
    )


def test_gc_aliases_and_ambiguous_names_use_shared_resolver():
    nodes = {"Vendor_A": {"links": ["Concept_Unique", "Concept_Shared"]},
             "Vendor_B": {"aliases": ["Unique"]},
             "Vendor_C": {"aliases": ["Shared"]},
             "Vendor_D": {"aliases": ["Shared"]}}
    assert _topological_degrees(nodes, {}, {}) == {"Vendor_A": 1, "Vendor_B": 1, "Vendor_C": 0, "Vendor_D": 0}


def test_gc_keeps_active_and_core_leaves(isolated_memory):
    seed(isolated_memory, "Vendor_Active", status="Active")
    seed(isolated_memory, "Vendor_Core", scope="core")
    text = gc_vector_lake()
    assert "No orphan" in text
    assert counts()[0] == 2


def test_gc_requires_preview_and_exact_scope(isolated_memory):
    a = seed(isolated_memory, "Vendor_Acme")
    b = seed(isolated_memory, "Vendor_Beta", domain="Other")
    c = seed(isolated_memory, "Vendor_Cedar", cluster="Two")
    plan = preview(domain="General", cluster="One")
    assert plan["pages"] == ["Vendor_Acme"]
    assert "expected_plan" in gc_vector_lake(dry_run=False, domain="General", cluster="One")
    result = gc_vector_lake(dry_run=False, domain="General", cluster="One", expected_plan=plan)
    assert "Deleted 1" in result
    assert not a.exists() and b.exists() and c.exists()
    assert list((isolated_memory / "backup" / "gc").rglob(a.name))


@pytest.mark.parametrize("change", ["canonical", "file", "claims", "new_page"])
def test_gc_stale_plan_has_zero_writes(isolated_memory, change):
    page = seed(isolated_memory, "Vendor_Acme")
    plan = preview()
    if change == "canonical":
        entity = governance_store.get_entity("entity_Vendor_Acme")
        assert entity is not None
        entity["status"] = "Active"
        governance_store.upsert_entity(entity["entity_id"], entity)
    elif change == "file":
        page.write_text(page.read_text(encoding="utf-8") + "\nChanged", encoding="utf-8")
    elif change == "claims":
        governance_store.save_claims({"items": {"claim_new": {"claim_id": "claim_new", "subject_entity_ids": ["entity_Vendor_Acme"]}}})
    else:
        seed(isolated_memory, "Vendor_New")
    before = counts()
    result = gc_vector_lake(dry_run=False, expected_plan=plan)
    assert "stale" in result.lower(), result
    assert counts() == before
    assert page.exists()
    assert not (isolated_memory / "backup" / "gc").exists()


def test_gc_race_after_scan_rolls_back_all(isolated_memory, monkeypatch):
    import vector_lake.mutation_coordinator as coordinator
    page = seed(isolated_memory, "Vendor_Acme")
    plan = preview()
    original = coordinator.execute_mutation_batch
    def race(mutations, **kwargs):
        page.write_text(page.read_text(encoding="utf-8") + "\nrace", encoding="utf-8")
        return original(mutations, **kwargs)
    monkeypatch.setattr(coordinator, "execute_mutation_batch", race)
    before = counts()
    result = gc_vector_lake(dry_run=False, expected_plan=plan)
    assert "stale" in result.lower()
    assert counts() == before and page.exists()
    assert not (isolated_memory / "backup" / "gc").exists()


def test_rename_only_link_tokens_and_preserves_identity_scope(isolated_memory):
    seed(isolated_memory, "Concept_Old", status="Active")
    body = ('Old prose; [[Concept_Old]] [mentions:: [[Old|label]]] [[Concept_Old#Part|caption]]\n'
            '`[[Concept_Old]]` "[[Concept_Old]]" \'[[Concept_Old]]\'\n'
            '> [[Concept_Old]]\n    [[Concept_Old]]\n'
            '~~~md\n[[Concept_Old]]\n~~~\n<!-- [[Concept_Old]] -->\n')
    ref = seed(isolated_memory, "Concept_Ref", body=body)
    hidden = seed(isolated_memory, "Concept_Hidden", domain="Other", body="[[Concept_Old]]")
    result = rename_vector_lake_entity("Old", "Concept_New", dry_run=False, domain="General", cluster="One")
    assert "Successfully renamed" in result, result
    entity = governance_store.get_entity("entity_Concept_Old")
    assert entity is not None
    assert entity["page_key"] == "Concept_New"
    assert entity["id"] == "id_Concept_Old"
    assert "[[Concept_Old]]" in hidden.read_text(encoding="utf-8")
    content = ref.read_text(encoding="utf-8")
    assert '[[Concept_New|Old]] [mentions:: [[Concept_New|label]]] [[Concept_New#Part|caption]]' in content
    assert '`[[Concept_Old]]` "[[Concept_Old]]" \'[[Concept_Old]]\'' in content
    assert '> [[Concept_Old]]\n    [[Concept_Old]]' in content
    assert '~~~md\n[[Concept_Old]]\n~~~' in content
    assert '<!-- [[Concept_Old]] -->' in content


def test_rename_ambiguity_is_not_resolved_by_scope(isolated_memory):
    seed(isolated_memory, "Concept_Old")
    seed(isolated_memory, "Product_Old", domain="Other")
    before = counts()
    result = rename_vector_lake_entity("Old", "Concept_New", dry_run=False, domain="General")
    assert "ambiguous" in result.lower(), result
    assert counts() == before


def test_rename_native_cas_race_has_zero_batch_writes(isolated_memory, monkeypatch):
    import vector_lake.tool_rename as rename
    old = seed(isolated_memory, "Concept_Old")
    seed(isolated_memory, "Concept_Ref", body="[[Concept_Old]]")
    original = rename.execute_mutation_batch
    before_race = []
    def race(mutations, **kwargs):
        assert all("expected_version" in mutation for mutation in mutations)
        assert mutations[1]["expected_version"] == ""
        row = governance_store.get_entity("entity_Concept_Ref")
        assert row is not None
        row["summary"] = "Concurrent canonical update"
        governance_store.upsert_entity(row["entity_id"], row)
        before_race.append(counts())
        return original(mutations, **kwargs)
    monkeypatch.setattr(rename, "execute_mutation_batch", race)
    result = rename_vector_lake_entity("Concept_Old", "Concept_New", dry_run=False)
    assert "conflict" in result.lower() or "stale" in result.lower(), result
    assert counts() == before_race[0]
    assert old.exists() and not old.with_name("Concept_New.md").exists()


@pytest.mark.parametrize("literal", [
    '"quoted\n[[Concept_Old]]\nend"',
    '``code\n[[Concept_Old]]\nend``',
    '“quoted [[Concept_Old]]”',
    r'\[[Concept_Old]]',
    '```md\n[[Concept_Old]]\n```',
    '---\nsummary: "[[Concept_Old]]"\n---\n',
])
def test_rename_preserves_literal_syntax(literal):
    result = _replace_resolved_links(literal, "Concept_Old", "Concept_New", "Old", {"Concept_Old": "Concept_Old"}, {})
    assert result == literal


def test_rename_preserves_legacy_derived_entity_id(isolated_memory):
    seed(isolated_memory, "Concept_Old", explicit_identity=False)
    before = governance_store.load_entities()["items"]
    entity_id = next(iter(before))
    result = rename_vector_lake_entity("Concept_Old", "Concept_New", dry_run=False)
    assert "Successfully renamed" in result, result
    after = governance_store.load_entities()["items"]
    assert set(after) == {entity_id}
    assert after[entity_id]["page_key"] == "Concept_New"
    assert after[entity_id]["id"] == before[entity_id]["id"]


def test_rename_outside_scope_and_projection_drift_refused(isolated_memory):
    old = seed(isolated_memory, "Concept_Old", domain="Hidden")
    before = counts()
    assert "outside" in rename_vector_lake_entity("Concept_Old", "Concept_New", dry_run=False, domain="General")
    old.write_text(old.read_text(encoding="utf-8") + "\ndrift", encoding="utf-8")
    assert "Stale" in rename_vector_lake_entity("Concept_Old", "Concept_New", dry_run=False)
    assert counts() == before and old.exists()


def test_gc_plan_cannot_be_replayed_in_other_scope(isolated_memory):
    page = seed(isolated_memory, "Vendor_Acme")
    plan = preview(domain="General", cluster="One")
    before = counts()
    result = gc_vector_lake(dry_run=False, domain="General", cluster="Two", expected_plan=plan)
    assert "stale" in result.lower()
    assert counts() == before and page.exists()


@pytest.mark.parametrize("kwargs", [{"days": -1}, {"domain": None}, {"expected_plan": "{"}, {"expected_plan": []}])
def test_gc_invalid_inputs_are_errors_not_empty_results(kwargs):
    with pytest.raises(ValueError):
        gc_vector_lake(**kwargs)


def test_rename_commits_once_and_keeps_deferred_receipt(isolated_memory, monkeypatch):
    import vector_lake.tool_rename as rename
    seed(isolated_memory, "Concept_Old")
    seed(isolated_memory, "Concept_Ref", body="[[Concept_Old]]")
    calls = []
    def commit(mutations, *, validation_mode, canonical_precondition):
        canonical_precondition()
        calls.append(mutations)
        assert validation_mode == "schema"
        assert all("expected_version" in item for item in mutations)
        return True, "Canonical mutation batch committed; projections deferred for Concept_New.md."
    monkeypatch.setattr(rename, "execute_mutation_batch", commit)
    result = rename_vector_lake_entity("Concept_Old", "Concept_New", dry_run=False)
    assert len(calls) == 1 and len(calls[0]) == 3
    assert "projections deferred" in result and "Successfully renamed" not in result
