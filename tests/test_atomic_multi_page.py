import json

import pytest

from vector_lake import db_store, governance_store, mcp_server
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.schema_validator import SchemaViolationException, validate_schema
from vector_lake.tool_rename import rename_vector_lake_entity
from vector_lake.wiki_utils import get_wiki_dir


def _seed_page(key, body):
    content = (
        f"---\nid: id_{key}\ntitle: {key.split('_', 1)[1]}\naliases: []\n"
        "type: concept\ndomain: General\ntopic_cluster: Unit\nstatus: Active\n"
        "epistemic-status: seed\ncategories: [Uncategorized]\nupdated: '2026-09-17'\nsources: []\n---\n"
        f"## 1. 编译事实\n\n{body}\n\n## 2. 证据时间线\n"
    )
    execute_mutation_batch([{"filename": key + ".md", "content": content}], validation_mode="schema")


def _canonical_counts():
    conn = db_store.get_connection()
    return (
        conn.execute("SELECT count(*) FROM entities").fetchone()[0],
        conn.execute("SELECT count(*) FROM mutation_outbox").fetchone()[0],
        conn.execute("SELECT count(*) FROM change_sets").fetchone()[0],
    )


def test_rename_builds_one_atomic_mutation_batch(isolated_memory, monkeypatch):
    wiki_dir = get_wiki_dir()
    _seed_page("Concept_Old", "Old body [[Concept_Old]].")
    _seed_page("Concept_Ref", "ref [[Concept_Old]]")
    old_entity_id = next(row["entity_id"] for row in governance_store.load_entities()["items"].values()
                         if row["page_key"] == "Concept_Old")
    captured = []
    modes = []

    def capture_batch(mutations, validation_mode="full", *, canonical_precondition=None):
        assert callable(canonical_precondition)
        captured.append(mutations)
        modes.append(validation_mode)
        return execute_mutation_batch(mutations, validation_mode=validation_mode,
                                      canonical_precondition=canonical_precondition)

    monkeypatch.setattr("vector_lake.tool_rename.execute_mutation_batch", capture_batch)
    result = rename_vector_lake_entity("Concept_Old", "Concept_New", dry_run=False,
                                       domain="General", cluster="Unit")

    assert result.startswith("Successfully renamed")
    assert len(captured) == 1
    # A rename moves an existing node; it is legacy maintenance, not authoring, so it must not
    # answer to the new-node classification rules.
    assert modes == ["schema"], modes
    assert [item["filename"] for item in captured[0]] == [
        "Concept_Old.md",
        "Concept_New.md",
        "Concept_Ref.md",
    ]
    assert captured[0][0]["is_delete"] is True
    assert all("expected_version" in item for item in captured[0])
    assert captured[0][0]["expected_version"] and captured[0][2]["expected_version"]
    assert captured[0][1]["expected_version"] == ""
    assert "[[Concept_New|Old]]" in captured[0][2]["content"]
    assert not (wiki_dir / "Concept_Old.md").exists()
    assert "[[Concept_New|Old]]" in (wiki_dir / "Concept_Ref.md").read_text(encoding="utf-8")
    moved = [row for row in governance_store.load_entities()["items"].values() if row["page_key"] == "Concept_New"]
    assert len(moved) == 1 and moved[0]["entity_id"] == old_entity_id


def test_batch_replace_links_commits_once(isolated_memory, monkeypatch):
    wiki_dir = get_wiki_dir()
    _seed_page("Concept_Alpha", "[[Old]]")
    _seed_page("Concept_Beta", "x [[Old]] y")
    captured = []

    def capture_batch(mutations, *, validation_mode="full", canonical_precondition=None):
        assert validation_mode == "schema" and callable(canonical_precondition)
        captured.append(mutations)
        return execute_mutation_batch(mutations, validation_mode=validation_mode,
                                      canonical_precondition=canonical_precondition)

    monkeypatch.setattr("vector_lake.mutation_coordinator.execute_mutation_batch", capture_batch)
    result = mcp_server.batch_replace_links("[[Old]]", "[[New]]", dry_run=False,
                                           domain="General", cluster="Unit")

    assert "Canonical mutation batch committed" in result
    assert "Index completion pending" in result
    assert len(captured) == 1
    assert len(captured[0]) == 2
    assert all("[[New]]" in item["content"] and item["expected_version"] for item in captured[0])
    assert all("[[New]]" in (wiki_dir / (key + ".md")).read_text(encoding="utf-8")
               for key in ("Concept_Alpha", "Concept_Beta"))


def test_unscoped_replacement_is_zero_write(isolated_memory, monkeypatch):
    _seed_page("Concept_Alpha", "[[Old]]")
    before = _canonical_counts()
    original = (get_wiki_dir() / "Concept_Alpha.md").read_bytes()
    monkeypatch.setattr("vector_lake.mutation_coordinator.execute_mutation_batch",
                        lambda *args, **kwargs: pytest.fail("unsafe call must not reach mutation batch"))
    assert "explicit domain" in mcp_server.batch_replace_links("[[Old]]", "[[New]]", dry_run=False)
    assert _canonical_counts() == before
    assert (get_wiki_dir() / "Concept_Alpha.md").read_bytes() == original


def test_unmanaged_projection_is_not_silently_replaced(isolated_memory, monkeypatch):
    _seed_page("Concept_Managed", "No link.")
    path = get_wiki_dir() / "Concept_Unmanaged.md"
    path.write_text("[[Old]]", encoding="utf-8")
    before = _canonical_counts()
    monkeypatch.setattr("vector_lake.mutation_coordinator.execute_mutation_batch",
                        lambda *args, **kwargs: pytest.fail("unmanaged page must not enter mutation batch"))
    result = mcp_server.batch_replace_links("[[Old]]", "[[New]]", dry_run=False, domain="General")
    assert result.startswith("No matching links")
    assert _canonical_counts() == before and path.read_text(encoding="utf-8") == "[[Old]]"


def test_unscoped_identity_is_not_guessed_for_rename(isolated_memory, monkeypatch):
    path = get_wiki_dir() / "Concept_Old.md"
    path.write_text("---\ntitle: Old\naliases: []\n---\nOld [[Concept_Old]].", encoding="utf-8")
    db_store.init_db()
    before = _canonical_counts()
    original = path.read_bytes()
    monkeypatch.setattr("vector_lake.tool_rename.execute_mutation_batch",
                        lambda *args, **kwargs: pytest.fail("unbound identity must not reach mutation batch"))
    assert "missing or ambiguous" in rename_vector_lake_entity("Concept_Old", "Concept_New", dry_run=False,
                                                               domain="General", cluster="Unit")
    assert _canonical_counts() == before and path.read_bytes() == original
    assert not (get_wiki_dir() / "Concept_New.md").exists()


def test_schema_tag_collision_is_not_swallowed(tmp_path):
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps({"nodes": {"Concept_Existing": {"title": "Existing", "aliases": []}}}),
        encoding="utf-8",
    )
    frontmatter = {
        "id": "source_test",
        "title": "Test",
        "type": "source",
        "domain": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["System_Architecture"],
        "updated": "2026-07-14T00:00:00+00:00",
        "sources": [],
        "tags": ["Existing"],
    }

    with pytest.raises(SchemaViolationException, match="Tag Collision"):
        validate_schema(frontmatter, "source body", "Source_Test.md", index_path=index_path)
