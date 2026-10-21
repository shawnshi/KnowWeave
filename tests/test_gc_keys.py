import json
import os
import time

from vector_lake import db_store, governance_store
from vector_lake.tool_gc import gc_vector_lake


def _seed(memory_dir, entities, aged=True):
    """Create canonical rows and aged Markdown pages in one canonical write."""
    db_store.init_db()
    governance_store.save_entities({"items": {node["entity_id"]: node for node in entities}})
    old = time.time() - 90 * 86400
    pages = []
    for node in entities:
        page = memory_dir / "wiki" / f"{node['page_key']}.md"
        page.write_text("legacy vendor", encoding="utf-8")
        if aged:
            os.utime(page, (old, old))
        pages.append(page)
    return pages


def _vendor(page_key, entity_id, **extra):
    node = {
        "entity_id": entity_id,
        "canonical_name": page_key.split("_", 1)[-1],
        "page_key": page_key,
        "type": "vendor",
        "status": "Archived",
        "links": [],
        "sources": [],
    }
    node.update(extra)
    return node


def test_gc_reports_isolated_page(isolated_memory):
    _seed(isolated_memory, [_vendor("Vendor_Acme", "entity-acme")])

    result = gc_vector_lake(days=30, dry_run=True)

    assert "Vendor_Acme.md" in result
    assert "entity-acme" in result
    assert "degree: 0" in result


def test_gc_keeps_page_connected_by_two_links(isolated_memory):
    _seed(isolated_memory, [
        _vendor("Vendor_Acme", "entity-acme", links=["Vendor_Beta", "Concept_Alpha"]),
        _vendor("Vendor_Beta", "entity-beta"),
        {"entity_id": "entity-alpha", "canonical_name": "Alpha", "page_key": "Concept_Alpha",
         "type": "concept", "status": "Active", "links": [], "sources": []},
    ])

    result = gc_vector_lake(days=30, dry_run=True)

    # Degree 2 from two explicit links: not an orphan.
    assert "Vendor_Acme" not in result, result
    # A retired degree-1 page remains eligible; active/core leaves are protected.
    assert "Vendor_Beta.md" in result, result


def test_gc_keeps_page_connected_by_shared_source(isolated_memory):
    _seed(isolated_memory, [
        _vendor("Vendor_Acme", "entity-acme", sources=["raw/industry-report.md"]),
        _vendor("Vendor_Beta", "entity-beta", sources=["raw/industry-report.md"]),
        _vendor("Vendor_Gamma", "entity-gamma", sources=["raw/industry-report.md"]),
    ])

    result = gc_vector_lake(days=30, dry_run=True)

    assert "Vendor_Acme" not in result, result


def test_gc_keeps_page_connected_by_claim_co_occurrence(isolated_memory):
    _seed(isolated_memory, [
        _vendor("Vendor_Acme", "entity-acme"),
        {"entity_id": "entity-alpha", "canonical_name": "Alpha", "page_key": "Concept_Alpha",
         "type": "concept", "status": "Active", "links": [], "sources": []},
        {"entity_id": "entity-delta", "canonical_name": "Delta", "page_key": "Concept_Delta",
         "type": "concept", "status": "Active", "links": [], "sources": []},
    ])
    governance_store.save_claims({
        "items": {
            "claim-1": {
                "claim_id": "claim-1",
                "claim_text": "Acme, Alpha and Delta",
                "subject_entity_ids": ["entity-acme", "entity-alpha", "entity-delta"],
                "source_ids": [],
            }
        }
    })

    result = gc_vector_lake(days=30, dry_run=True)

    assert "Vendor_Acme" not in result, result


def test_gc_ignores_claim_space_edges(isolated_memory):
    """Regression: claim ids in claim_graph_edges must never count as page degree."""
    _seed(isolated_memory, [_vendor("Vendor_Acme", "entity-acme")])
    conn = db_store.get_connection()
    with db_store.transaction():
        for index in range(6):
            conn.execute(
                "INSERT INTO claim_graph_edges (source_id, target_id, relation, weight, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Vendor_Acme", f"claim_{index}", "supports", 1.0, "2026-01-01"),
            )

    result = gc_vector_lake(days=30, dry_run=True)

    assert "Vendor_Acme.md" in result
    assert "degree: 0" in result


def test_gc_safety_valve_blocks_mass_deletion(isolated_memory):
    _seed(isolated_memory, [_vendor(f"Vendor_Page{index}", f"entity-{index}") for index in range(10)])

    plan = json.loads(gc_vector_lake(days=30).split("Expected plan: ", 1)[1])
    result = gc_vector_lake(days=30, dry_run=False, expected_plan=plan)

    assert "GC aborted" in result
    for index in range(10):
        assert (isolated_memory / "wiki" / f"Vendor_Page{index}.md").exists()


def test_gc_force_bypasses_safety_valve(isolated_memory):
    _seed(isolated_memory, [_vendor(f"Vendor_Page{index}", f"entity-{index}") for index in range(10)])

    plan = json.loads(gc_vector_lake(days=30).split("Expected plan: ", 1)[1])
    gc_vector_lake(days=30, dry_run=False, force=True, expected_plan=plan)

    assert not (isolated_memory / "wiki" / "Vendor_Page0.md").exists()
    backups = list((isolated_memory / "backup" / "gc").rglob("Vendor_Page0.md"))
    assert backups, "GC must keep a recoverable copy of every deleted page"


def test_claim_edges_stay_in_the_claim_key_space():
    """``save_graph_edges`` writes claim-space edges, and nothing mirrors them into page space.

    The page-space projection is ``page_index_edges``, owned by the indexer and derived from the
    published file; a second table that held a collapsed copy was removed on 2026-09-18 because
    nothing read it.  What this pins is the key-space separation: claim ids must not appear among
    page keys.
    """
    db_store.init_db()
    governance_store.save_graph_edges([{"source_id": "claim-a", "target_id": "claim-b", "relation": "supports"}])
    conn = db_store.get_connection()

    assert conn.execute("SELECT COUNT(*) FROM claim_graph_edges").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0] == 0
