"""The incremental update must derive the same edges a full rebuild does.

These two paths used different resolution rules, and the difference was what the read-back in
``update_index_items`` papered over: it copied a node's existing projected edge rows forward,
because the incremental path could not re-derive the edges alias resolution had produced.  Both paths
now use one rule (``vector_lake.link_resolution``) for links *and* triple targets, and score a pair in
the same orientation -- so the read-back is gone and the database holds one projection of the edge
set (``page_index_edges``, derived from the published file) instead of a second table to compare it to.

The scope these tests can speak for is stated rather than implied: they compare the *touched* node's
incident edges against a rebuild.  Edges between two untouched nodes are never revisited by an
incremental pass (their weight depends on the touched node's raw link count through the common-neighbour
term, and the per-node cap re-ranks), so "equivalent to a rebuild" is a claim about the touched node.
"""

import json

import pytest

from vector_lake import db_store, governance_store, indexer


def _entity(entity_id: str, page_key: str, *, title=None, aliases=None, links=None):
    return {
        "entity_id": entity_id,
        "canonical_name": title or page_key,
        "page_key": page_key,
        "type": "concept",
        "status": "Active",
        "title": title or page_key,
        "aliases": list(aliases or []),
        "categories": ["Concept"],
        "links": list(links or []),
        "triples": [],
    }


def _save(items):
    governance_store.save_entities({"items": {item["entity_id"]: item for item in items}})


def _published(memory_dir):
    return json.loads((memory_dir / "wiki" / "index.json").read_text(encoding="utf-8"))


def _edges_touching(data, node):
    """Every published edge with ``node`` at either end.

    The pair is stored ``(min, max)`` -- ``dedupe_and_prune_edges`` normalises it -- so a helper
    that matched one column would silently return nothing for half the fixtures and make the
    comparison vacuous.
    """
    return sorted(
        (edge["source"], edge["target"], round(float(edge["weight"]), 6))
        for edge in data["weighted_edges"]
        if node in (edge["source"], edge["target"])
    )


def _incremental_matches_rebuild(isolated_memory, items, victim: str):
    """Rebuild, touch ``victim`` incrementally, then compare the two edge sets for it."""
    db_store.init_db()
    _save(items)
    indexer.generate_index()
    after_full_build = _edges_touching(_published(isolated_memory), victim)

    # A second full build with the same inputs is the oracle: the incremental path must land on it.
    indexer.generate_index()
    oracle = _edges_touching(_published(isolated_memory), victim)

    indexer.update_index_items([f"{victim}.md"])
    incrementally = _edges_touching(_published(isolated_memory), victim)

    assert after_full_build == oracle, "two full builds disagree, so this comparison is meaningless"
    return oracle, incrementally


@pytest.mark.parametrize("native", [False, pytest.param(True, marks=pytest.mark.requires_native_core)], ids=["python", "native"])
@pytest.mark.parametrize("linked", [False, True], ids=["source-candidate", "linked"])
def test_relation_maxima_match_incremental_rebuild_without_fact_loss(isolated_memory, monkeypatch, native, linked):
    if native:
        assert indexer.vector_lake_core is not None
    monkeypatch.setattr(indexer, "HAVE_CORE", native)
    items = [
        _entity("e-a", "Concept_A", links=["Alias-B"] if linked else []),
        _entity("e-b", "Concept_B", aliases=["Alias-B"], links=["Concept_A"] if linked else []),
    ]
    triples = [
        {"predicate": "has_part", "target": "Alias-B"},
        {"predicate": "related_to", "target": "Alias-B"},
        {"predicate": "has_part", "target": "Alias-B"},
        {"predicate": "mentions", "target": "Alias-B"},
    ]
    items[0]["triples"] = triples
    items[1]["triples"] = [{"predicate": "part-of", "target": "Concept_A"}]
    for item in items:
        item["sources"] = ["synthetic-source"]
    oracle, incrementally = _incremental_matches_rebuild(isolated_memory, items, "Concept_A")
    assert oracle == incrementally == [("Concept_A", "Concept_B", 13.8)]
    projected = _published(isolated_memory)
    assert projected["nodes"]["Concept_A"]["triples"] == triples
    stored = db_store.get_connection().execute(
        "SELECT data_json FROM entities WHERE entity_id = ?", ("e-a",)
    ).fetchone()
    assert json.loads(stored["data_json"])["triples"] == triples


def test_a_core_name_link_survives_an_incremental_update(isolated_memory):
    """The shape the read-back was added for: ``[[Concept_CoMET]]`` against ``Product_CoMET.md``."""
    items = [
        _entity("e-product", "Product_CoMET", title="CoMET"),
        _entity("e-user", "Concept_User", links=["Concept_CoMET"]),
    ]

    oracle, incrementally = _incremental_matches_rebuild(isolated_memory, items, "Concept_User")

    # 2.0 is the mention weight for a plain [[link]] (triples carry their own predicate weight).
    assert ("Concept_User", "Product_CoMET", 2.0) in oracle, oracle
    assert incrementally == oracle, "the incremental path lost the core-name edge"


def test_an_alias_link_survives_an_incremental_update(isolated_memory):
    items = [
        _entity("e-vendor", "Vendor_Epic-Systems", title="Epic"),
        _entity("e-user", "Concept_User", links=["Epic"]),
    ]

    oracle, incrementally = _incremental_matches_rebuild(isolated_memory, items, "Concept_User")

    assert ("Concept_User", "Vendor_Epic-Systems", 2.0) in oracle, oracle
    assert incrementally == oracle, "the incremental path lost an alias-resolved edge"


def test_a_contested_name_builds_no_edge_in_either_path(isolated_memory):
    """Two pages declaring one name is ambiguous, so neither path may invent an edge for it."""
    items = [
        _entity("e-a", "Vendor_Shared", title="Shared-Name"),
        _entity("e-b", "Product_Shared", title="Shared-Name"),
        _entity("e-user", "Concept_User", links=["Shared-Name"]),
    ]

    oracle, incrementally = _incremental_matches_rebuild(isolated_memory, items, "Concept_User")

    assert incrementally == oracle
    assert not [edge for edge in oracle if edge[1] in ("Vendor_Shared", "Product_Shared")], oracle


def test_an_id_is_not_a_link_target_in_either_path(isolated_memory):
    """A page's ``id`` is an identifier, not a name: it must not resolve an edge."""
    items = [
        _entity("20260101_abc123", "Concept_Target"),
        _entity("e-user", "Concept_User", links=["20260101_abc123"]),
    ]

    oracle, incrementally = _incremental_matches_rebuild(isolated_memory, items, "Concept_User")

    assert incrementally == oracle
    assert not [edge for edge in oracle if edge[1] == "Concept_Target"], oracle


def test_an_alias_edge_survives_updating_its_other_end(isolated_memory):
    """An alias edge must survive updating its other end -- the shape the read-back was for.

    The fixture links by *alias* (``[[Epic]]`` -> ``Vendor_Epic-Systems``), because a link spelling
    the filename is answered by the untouched page's own raw string and would survive regardless.
    Updating the vendor is what isolates it: the touched node re-derives the pair through the shared
    resolution rule, and nothing is copied from the projection any more.
    """
    items = [
        _entity("e-user", "Concept_User", links=["Epic"]),
        _entity("e-vendor", "Vendor_Epic-Systems", title="Epic"),
    ]
    db_store.init_db()
    _save(items)
    indexer.generate_index()
    assert ("Concept_User", "Vendor_Epic-Systems", 2.0) in _edges_touching(
        _published(isolated_memory), "Concept_User"
    )

    indexer.update_index_items(["Vendor_Epic-Systems.md"])

    assert _published(isolated_memory)["graph_state"]["dirty"] is True, (
        "the incremental path did not run: update_index_items fell back to a full rebuild"
    )
    assert ("Concept_User", "Vendor_Epic-Systems", 2.0) in _edges_touching(
        _published(isolated_memory), "Concept_User"
    ), "updating one end dropped the alias-resolved edge"


def test_a_pair_score_does_not_depend_on_which_end_was_updated(isolated_memory):
    """The orientation the parity fixtures would otherwise miss.

    ``TYPE_AFFINITY`` is asymmetric -- ``["event"]["vendor"]`` is 1.0 while ``["vendor"]`` has no
    ``"event"`` key and falls back to 0.5 -- and the full build reads the row from the
    lexicographically smaller key.
    Every other fixture here uses symmetric pairs, so a derivation that took the score from the
    updated node instead would pass them all and still drift on this one.
    """
    items = [
        _entity("e-event", "Event_Launch", links=["Acme"]),
        _entity("e-vendor", "Vendor_Acme", title="Acme"),
    ]
    items[1]["type"] = "vendor"
    items[0]["type"] = "event"
    db_store.init_db()
    _save(items)
    indexer.generate_index()
    oracle = _edges_touching(_published(isolated_memory), "Event_Launch")
    assert oracle, "the fixture produced no edge, so it cannot compare anything"

    # Update the end that is *not* the lexicographic minimum ("Event_Launch" < "Vendor_Acme").
    indexer.update_index_items(["Vendor_Acme.md"])

    assert _edges_touching(_published(isolated_memory), "Event_Launch") == oracle



def test_removing_a_link_removes_its_edge(isolated_memory):
    """The fix that let the read-back go, and with it the second edge table.

    This was a strict xfail: the incremental path carried the updated node's published edges forward
    from ``page_graph_edges`` instead of deriving them, so an edge whose link had been deleted was
    re-added.  It passes now that the derivation resolves links and triple targets through the shared
    rule and scores each pair in the same orientation as a full build -- which is what the read-back
    had been compensating for.
    """
    items = [
        _entity("e-user", "Concept_User", links=["Concept_Target"]),
        _entity("e-target", "Concept_Target"),
    ]
    db_store.init_db()
    _save(items)
    indexer.generate_index()
    assert _edges_touching(_published(isolated_memory), "Concept_Target")

    # The page stops linking: the canonical projection drops the link.
    _save([_entity("e-user", "Concept_User", links=[]), _entity("e-target", "Concept_Target")])
    indexer.update_index_items(["Concept_User.md"])

    assert _edges_touching(_published(isolated_memory), "Concept_Target") == [], (
        "the edge survived the link that produced it being deleted"
    )


def test_a_typed_link_written_by_name_keeps_its_predicate_weight(isolated_memory):
    """The witness end's triples must be resolved too, not only the touched node's.

    ``calculate_relevance`` reads the *other* node's predicate weight from
    ``all_nodes_triples``, which was built from raw targets: a typed link written as a name (title,
    alias or core name) missed that map, so the pair scored as a plain mention (1.2) instead of the
    predicate's weight.  Because the difference is large, a pair whose decay/alignment multipliers
    are low enough also fell under the 1.5 gate and disappeared -- which is what the read-back used
    to hide, and why it could only go after this was fixed.
    """
    items = [
        _entity("e-user", "Concept_User", title="User"),
        _entity("e-target", "Concept_Target", links=["User"]),
    ]
    items[1]["triples"] = [{"predicate": "created", "target": "User"}]

    oracle, incrementally = _incremental_matches_rebuild(isolated_memory, items, "Concept_User")

    assert oracle, "the fixture produced no edge"
    assert incrementally == oracle, "the name-written typed link lost its predicate weight"


def test_a_name_the_batch_itself_introduces_resolves_in_the_same_pass(isolated_memory):
    """The maps must include the pages this batch is about, not only the published ones.

    ``Concept_User`` links to the *alias of a page the batch introduces*, so the resolution map has
    to know that alias before the pass runs.  Built from the pre-batch set instead, the pair is
    dropped here and appears only a batch later -- or on the next rebuild, which is what the parity
    comparison would then show as a difference.
    """
    existing = _entity("e-user", "Concept_User", links=["Fresh Alias"])
    fresh = _entity("e-fresh", "Concept_Fresh", title="Fresh Alias")
    db_store.init_db()
    _save([existing])
    indexer.generate_index()

    _save([existing, fresh])
    indexer.update_index_items(["Concept_User.md", "Concept_Fresh.md"])

    edges = _edges_touching(_published(isolated_memory), "Concept_User")
    assert ("Concept_Fresh", "Concept_User") in [(a, b) for a, b, _w in edges], edges
