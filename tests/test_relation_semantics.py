"""Synthetic-only semantic diagnostics and real read-only SQLite context contracts."""
import copy
import json
import sqlite3

import pytest

from vector_lake import db_store, page_index_projection
from vector_lake.claim_extractor import _clean_claim_text
from vector_lake.relation_catalog import RelationInputError, build_relation_catalog
from vector_lake.relation_policy import predicate_weight
from vector_lake.tool_relations import get_relations


def node(kind="concept", **fields):
    return {"type": kind, "title": "", "aliases": [], "domain": "General",
            "topic_cluster": "C", "status": "Active", "triples": [], "links": [], **fields}


def nodes():
    return {"Source_A": node("source", triples=[{"predicate": "depends-on", "target": "Product_B"}],
                             links=["Product_B"]),
            "Product_B": node("product"),
            "Source_Hidden": node("source", domain="Private", title="hidden-name")}


def declared(result):
    return next(row for row in result["relations"] if row["layer"] == "declared")


@pytest.fixture
def projected(isolated_memory):
    db_store.init_db()
    projection = {"nodes": nodes(), "weighted_edges": []}
    (isolated_memory / "wiki" / "index.json").write_text(json.dumps(projection), encoding="utf-8")
    page_index_projection.refresh_page_index_projection(projection)
    return isolated_memory


def store_claim(claim_id="c1", *, text="[depends-on:: [[Product_B]]]", page="Source_A",
                evidence_ids=None, **extra):
    record = {"claim_id": claim_id, "claim_text": text, "evidence_ids": evidence_ids,
              "locator": {"page_key": page, "block_index": 2, "heading": "hidden-name"},
              "updated_at": "2026-10-21T12:00:00+00:00", "subject_entity_ids": ["hidden-name"],
              "source_ids": ["raw/hidden-name"], **extra}
    conn = db_store.get_connection()
    conn.execute("INSERT INTO claims(claim_id, claim_text, data_json) VALUES (?, ?, ?)",
                 (claim_id, text, json.dumps(record)))
    conn.commit()
    return record


def store_evidence(evidence_id="e1", *, page="Source_A", block=2, supports=None):
    record = {"evidence_id": evidence_id, "locator": {"page_key": page, "block_index": block},
              "supports_claim_ids": ["c1"] if supports is None else supports,
              "evidence_text": "hidden-name", "source_id": "raw/hidden-name"}
    conn = db_store.get_connection()
    conn.execute("INSERT INTO evidence(evidence_id, data_json) VALUES (?, ?)",
                 (evidence_id, json.dumps(record)))
    conn.commit()


def test_predicate_direction_inverse_and_types_do_not_rewrite_or_rescore():
    sample = {"Standard_A": node("standard", triples=[{"predicate": "complies-with", "target": "Product_B"}]),
              "Product_B": node("product", triples=[{"predicate": "part-of", "target": "Standard_A"},
                                                     {"predicate": "old-predicate", "target": "Standard_A"}])}
    before = copy.deepcopy(sample)
    result = build_relation_catalog(sample)
    assert sample == before
    rows = {row["predicate"]: row for row in result["relations"]}
    assert len(rows) == 3
    assert rows["complies-with"]["source"] == "Standard_A"
    assert rows["complies-with"]["predicate_semantics"]["diagnostics"] == ["wrong_direction"]
    assert rows["part-of"]["predicate_semantics"]["inverse_predicate"] == "has_part"
    assert rows["old-predicate"]["predicate_semantics"]["vocabulary_status"] == "unknown"
    assert "has_part" not in rows
    assert predicate_weight("part-of") == 9.0
    assert predicate_weight("related_to") == 4.5
    assert predicate_weight("mentions") == pytest.approx(1.2)


def test_document_declaration_is_not_factual_subject_even_with_nearby_prose():
    sample = nodes()
    sample["Source_A"].update(summary="Product_B depends on Standard_X", subject_entity_ids=["Product_B"])
    result = build_relation_catalog(sample)
    row = declared(result)
    assert row["subject_scope"] == "document" and row["semantic_kind"] == "document_declaration"
    assert row["verified"] is False and row["epistemic_status"] == "unverified"
    assert row["context"] == {"status": "binding_unknown", "evidence_count": None}
    assert row["temporal"] == {"status": "unknown"}
    assert next(r for r in result["relations"] if r["layer"] == "navigation")["semantic_kind"] == "navigation"


def test_optional_context_is_allowlisted_and_scoped():
    context = {("Source_A", "Product_B", "depends-on"): {
        "status": "matched", "evidence_count": 2,
        "locator": {"page_key": "Source_A", "block_index": 2, "heading": "hidden-name"},
        "valid_from": "2026-10-01", "updated_at": "2026-10-21",
        "valid_to": "hidden-name", "claim_text": "hidden-name", "source_ids": ["raw/hidden-name"],
        "subject_scope": "entity", "verified": True}}
    before = copy.deepcopy(context)
    row = declared(build_relation_catalog(nodes(), context=context, domain="General"))
    assert context == before
    assert "hidden-name" not in json.dumps(row) and "raw/" not in json.dumps(row)
    assert row["temporal"] == {"status": "explicit", "valid_from": "2026-10-01"}
    assert row["provenance_status"] == "linked_unverified" and not row["verified"]
    assert row["subject_scope"] == "document"
    context[("Source_A", "Product_B", "depends-on")]["locator"]["page_key"] = "Source_Hidden"
    row = declared(build_relation_catalog(nodes(), context=context, domain="General"))
    assert row["context"]["status"] == "binding_unknown" and row["context"]["evidence_count"] is None
    assert row["temporal"]["status"] == "unknown"
    with pytest.raises(RelationInputError):
        build_relation_catalog(nodes(), context=[])


def test_real_scoped_sqlite_context_multiple_evidence_and_source_protection(projected, monkeypatch):
    store_claim(evidence_ids=["e1", "e2"], temporal_anchor="2026-09-01", valid_to="2027-01-01")
    store_evidence("e1")
    store_evidence("e2")
    store_claim("hidden-claim", page="Source_Hidden", text="hidden-name", evidence_ids=[])
    conn = db_store.get_connection()
    conn.execute("INSERT INTO sources VALUES ('private-source', ?, 'now')", (json.dumps({"raw_ref": "raw/hidden-name"}),))
    conn.commit()
    db_store.close_connection()
    before_db = db_store.get_db_path().read_bytes()
    before_index = (projected / "wiki" / "index.json").read_bytes()
    statements = []
    connect = sqlite3.connect

    def observe(*args, **kwargs):
        assert "mode=ro" in args[0]
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr("vector_lake.tool_relations.sqlite3.connect", observe)
    response = get_relations("Source_A", "General", "C")
    row = declared(response)
    assert response["status"] == "ok"
    assert row["context"] == {"status": "matched", "evidence_count": 2,
                              "locator": {"page_key": "Source_A", "block_index": 2}}
    assert row["provenance_status"] == "linked_unverified" and row["verified"] is False
    assert row["temporal"] == {"status": "explicit", "valid_to": "2027-01-01", "temporal_anchor": "2026-09-01"}
    encoded = json.dumps(response)
    assert all(secret not in encoded for secret in ("hidden-name", "Source_Hidden", "raw/", "private-source", "subject_entity_ids"))
    assert db_store.get_db_path().read_bytes() == before_db
    assert (projected / "wiki" / "index.json").read_bytes() == before_index
    assert not any("FROM sources" in sql or "FROM entities" in sql for sql in statements)
    assert all("f_page_key = 'Source_A'" in sql for sql in statements if "FROM claims" in sql or "FROM evidence" in sql)
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements)


@pytest.mark.parametrize("evidence_ids,count,provenance", [(None, None, "unknown"), ([], 0, "absent"), (["missing"], None, "unknown")])
def test_absent_evidence_and_missing_pointer_are_distinct(projected, evidence_ids, count, provenance):
    store_claim(evidence_ids=evidence_ids)
    row = declared(get_relations("Source_A", "General"))
    assert row["context"]["status"] == "matched"
    assert row["context"]["evidence_count"] == count and row["provenance_status"] == provenance
    assert row["temporal"] == {"status": "unknown"}  # updated_at is not event time


@pytest.mark.parametrize("variation", ["other-page", "other-block", "wrong-claim", "duplicate-ids"])
def test_evidence_requires_same_page_block_and_explicit_claim_pointer(projected, variation):
    store_claim(evidence_ids=["e1", "e1"] if variation == "duplicate-ids" else ["e1"])
    store_evidence(page="Source_Hidden" if variation == "other-page" else "Source_A",
                   block=3 if variation == "other-block" else 2,
                   supports=["c2"] if variation == "wrong-claim" else ["c1"])
    row = declared(get_relations("Source_A", "General"))
    assert row["context"]["evidence_count"] is None and row["provenance_status"] == "unknown"


def test_multiple_matching_claims_are_ambiguous_not_first_match(projected):
    store_claim("c1", evidence_ids=[])
    store_claim("c2", evidence_ids=[])
    row = declared(get_relations("Source_A", "General"))
    assert row["context"]["status"] == "ambiguous"
    assert row["context"]["evidence_count"] is None and row["temporal"]["status"] == "unknown"


@pytest.mark.parametrize("text", ["Product_B", "Product_B depends on Source_A", "[Product_B:: [[depends-on]]]",
                                  "[depends-on:: [[Source_Hidden]]]", "[related_to:: [[Product_B]]]"])
def test_missing_exact_binding_refuses_prose_subject_or_target_inference(projected, text):
    store_claim(text=text, evidence_ids=[])
    row = declared(get_relations("Source_A", "General"))
    assert row["context"]["status"] == "binding_unknown" and row["subject_scope"] == "document"
    assert row["context"]["evidence_count"] is None


def test_existing_cleaned_extraction_cannot_be_reverse_engineered(projected):
    store_claim(text=_clean_claim_text("[depends-on:: [[Product_B]]]"), evidence_ids=[])
    assert declared(get_relations("Source_A", "General"))["context"]["status"] == "binding_unknown"


@pytest.mark.parametrize("table", ["claims", "evidence"])
def test_missing_context_schema_is_not_fake_zero_evidence(projected, table):
    store_claim(evidence_ids=["e1"])
    conn = db_store.get_connection()
    conn.execute(f"DROP TABLE {table}")
    conn.commit()
    row = declared(get_relations("Source_A", "General"))
    assert row["context"]["evidence_count"] is None and row["provenance_status"] == "unknown"
    if table == "claims":
        assert row["context"]["status"] == "schema_unavailable"


def test_absent_locator_is_binding_unknown(projected):
    conn = db_store.get_connection()
    conn.execute("INSERT INTO claims(claim_id, claim_text, data_json) VALUES ('bad', ?, ?)",
                 ("[depends-on:: [[Product_B]]]", json.dumps({"locator": {"page_key": "Source_A"}})))
    conn.execute("UPDATE claims SET claim_text = NULL WHERE claim_id='bad'")
    conn.commit()
    # An absent locator is an unavailable binding, not fabricated evidence.
    row = declared(get_relations("Source_A", "General"))
    assert row["context"]["status"] == "binding_unknown"


def test_scoped_context_type_error_is_read_error_not_empty(projected):
    store_claim(evidence_ids=[])
    conn = db_store.get_connection()
    conn.execute("UPDATE claims SET claim_text=? WHERE claim_id='c1'", (sqlite3.Binary(b"bad"),))
    conn.commit()
    result = get_relations("Source_A", "General")
    assert result["status"] == "error" and result["error"]["code"] == "read_error"


def test_unreachable_scoped_context_and_depth_zero_are_not_hydrated(projected, monkeypatch):
    sample = nodes()
    sample["Source_Unrelated"] = node("source", triples=[{"predicate": "depends-on", "target": "Product_Other"}])
    sample["Product_Other"] = node("product")
    projection = {"nodes": sample, "weighted_edges": []}
    (projected / "wiki" / "index.json").write_text(json.dumps(projection), encoding="utf-8")
    page_index_projection.refresh_page_index_projection(projection)
    store_claim("unrelated", page="Source_Unrelated", evidence_ids=[])
    conn = db_store.get_connection()
    conn.execute("UPDATE claims SET claim_text=? WHERE claim_id='unrelated'", (sqlite3.Binary(b"bad"),))
    conn.commit()
    statements = []
    connect = sqlite3.connect

    def observe(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr("vector_lake.tool_relations.sqlite3.connect", observe)
    assert get_relations("Source_A", "General")["status"] == "ok"
    assert not any("Source_Unrelated" in sql for sql in statements if "FROM claims" in sql)
    statements.clear()
    assert get_relations("Source_A", "General", depth=0)["relations"] == []
    assert not any("FROM claims" in sql or "FROM evidence" in sql for sql in statements)
    statements.clear()
    assert get_relations("Source_A", "General", limit=0)["relations"] == []
    assert not any("FROM claims" in sql or "FROM evidence" in sql for sql in statements)


@pytest.mark.parametrize("anchor", ["2026", "2026-09", "2026-Q3", "2026-H2"])
def test_explicit_partial_temporal_anchor_retains_precision(projected, anchor):
    store_claim(evidence_ids=[], temporal_anchor=anchor)
    row = declared(get_relations("Source_A", "General"))
    assert row["temporal"] == {"status": "explicit", "temporal_anchor": anchor}


@pytest.mark.parametrize("source_type,target_type,predicate,code", [
    ("source", "concept", "part-of", "document_taxonomy_declaration"),
    ("product", "concept", "deployed-at", "predicate_type_mismatch"),
    ([], "concept", "depends-on", "endpoint_type_unknown"),
])
def test_advisory_type_failures_preserve_old_assertions(source_type, target_type, predicate, code):
    sample = {"A": node(source_type, triples=[{"predicate": predicate, "target": "B"}]),
              "B": node(target_type)}
    row = declared(build_relation_catalog(sample))
    assert row["predicate"] == predicate and code in row["predicate_semantics"]["diagnostics"]
    assert row["subject_scope"] == "document" and row["verified"] is False
