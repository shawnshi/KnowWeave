import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_canonical_change_sets import _concept
from tests.test_mutation_coordinator import _write_purpose_contract
from vector_lake import db_store, governance_store
from vector_lake.claim_migration import apply_claim_migration, plan_claim_migration


@pytest.fixture
def migration_root(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE", "1")
    _write_purpose_contract(isolated_memory)
    db_store.init_db()
    return isolated_memory


def _seed(root, name="Concept_Migrate", text=None):
    content = _concept(name)
    if text is not None:
        content = content.replace(f"A fact about {name}.", text)
    (root / "wiki" / f"{name}.md").write_text(content, encoding="utf-8")
    change = governance_store.create_change_set_from_content(f"{name}.md", content, "test", auto_approve=True)
    return content, change


def _prepared(name, content):
    return governance_store.prepare_change_set_from_content(f"{name}.md", content, "test", auto_approve=True)


def _claim_rows():
    return [tuple(row) for row in db_store.get_connection().execute("SELECT claim_id, data_json FROM claims ORDER BY claim_id")]


def test_missing_relation_aborts_without_deleting_old_claims(migration_root):
    content, _ = _seed(migration_root)
    before = _claim_rows()
    delta = _prepared("Concept_Migrate", content)
    delta["proposed_claims"][0]["contradicts"] = ["claim_missing"]
    with pytest.raises(ValueError, match="Missing or ambiguous"):
        governance_store.apply_change_set(delta)
    assert _claim_rows() == before


def test_cross_page_identity_cannot_be_stolen(migration_root):
    content, _ = _seed(migration_root)
    _, peer = _seed(migration_root, "Concept_Peer")
    before = _claim_rows()
    delta = _prepared("Concept_Migrate", content)
    delta["proposed_claims"][0]["claim_id"] = peer["proposed_claims"][0]["claim_id"]
    with pytest.raises(ValueError, match="another page"):
        governance_store.apply_change_set(delta)
    assert _claim_rows() == before


def test_self_relation_is_rejected(migration_root):
    content, _ = _seed(migration_root)
    delta = _prepared("Concept_Migrate", content)
    claim = delta["proposed_claims"][0]
    claim["contradicts"] = [claim["claim_id"]]
    with pytest.raises(ValueError, match="Self-referential"):
        governance_store.apply_change_set(delta)


def test_unique_legacy_relation_target_resolves(migration_root):
    content, _ = _seed(migration_root)
    _, peer = _seed(migration_root, "Concept_Peer")
    target = copy.deepcopy(peer["proposed_claims"][0])
    target["legacy_claim_ids"] = ["claim_legacy_target"]
    with db_store.transaction():
        db_store.get_connection().execute("UPDATE claims SET data_json = ? WHERE claim_id = ?", (json.dumps(target), target["claim_id"]))
    delta = _prepared("Concept_Migrate", content)
    delta["proposed_claims"][0]["contradicts"] = ["claim_legacy_target"]
    governance_store.apply_change_set(delta)
    claim_id = delta["proposed_claims"][0]["claim_id"]
    record = json.loads(db_store.get_connection().execute("SELECT data_json FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()[0])
    assert record["contradicts"] == [target["claim_id"]]


def test_inbound_relation_requires_explicit_peer_scope(migration_root):
    content, original = _seed(migration_root)
    _, peer = _seed(migration_root, "Concept_Peer")
    target = original["proposed_claims"][0]["claim_id"]
    peer_claim = copy.deepcopy(peer["proposed_claims"][0])
    peer_claim["contradicts"] = [target]
    with db_store.transaction():
        db_store.get_connection().execute("UPDATE claims SET data_json = ? WHERE claim_id = ?", (json.dumps(peer_claim), peer_claim["claim_id"]))
    before = _claim_rows()
    delta = _prepared("Concept_Migrate", content)
    delta["proposed_claims"] = [item for item in delta["proposed_claims"] if item["claim_id"] != target]
    with pytest.raises(ValueError, match="include page Concept_Peer"):
        governance_store.apply_change_set(delta)
    assert _claim_rows() == before


def _legacy_long_claim(root):
    text = "x" * 400 + " NOT approved."
    _, change = _seed(root, text=text)
    record = copy.deepcopy(next(item for item in change["proposed_claims"] if item["claim_scope"] == "block" and item["claim_text"].startswith("x")))
    legacy_id = "claim_" + hashlib.blake2b(f"Concept_Migrate:{text[:360]}".encode(), digest_size=12).hexdigest()
    old = copy.deepcopy(record)
    old["claim_id"] = legacy_id
    old["claim_text"] = text[:360]
    old.pop("legacy_claim_ids", None)
    with db_store.transaction():
        db_store.get_connection().execute("UPDATE claims SET claim_id = ?, claim_text = ?, data_json = ? WHERE claim_id = ?", (legacy_id, old["claim_text"], json.dumps(old), record["claim_id"]))
    return legacy_id, record["claim_id"]


def test_preview_is_read_only_and_maps_legacy_identity(migration_root):
    old, new = _legacy_long_claim(migration_root)
    before = _claim_rows()
    files = sorted(path.relative_to(migration_root).as_posix() for path in migration_root.rglob("*") if path.is_file())
    plan = plan_claim_migration(migration_root, ["Concept_Migrate.md"])
    assert not plan["blockers"]
    assert plan["identity_map"][old] == [new]
    assert _claim_rows() == before
    assert files == sorted(path.relative_to(migration_root).as_posix() for path in migration_root.rglob("*") if path.is_file())


def test_apply_has_backup_cas_and_recoverable_original(migration_root):
    old, new = _legacy_long_claim(migration_root)
    before = _claim_rows()
    plan = plan_claim_migration(migration_root, ["Concept_Migrate.md"])
    receipt = apply_claim_migration(plan)
    assert receipt["canonical_committed"]
    backup = Path(receipt["backup_path"])
    assert backup.is_file()
    ids = {row[0] for row in _claim_rows()}
    assert new in ids and old not in ids
    recovered = sqlite3.connect(":memory:")
    with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as original:
        original.backup(recovered)
    assert recovered.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert recovered.execute("SELECT claim_id, data_json FROM claims ORDER BY claim_id").fetchall() == before
    recovered.close()
    with pytest.raises(ValueError, match="stale"):
        apply_claim_migration(plan)


def test_changed_page_blocks_stale_plan(migration_root):
    _legacy_long_claim(migration_root)
    plan = plan_claim_migration(migration_root, ["Concept_Migrate.md"])
    path = migration_root / "wiki" / "Concept_Migrate.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nNew assertion.\n", encoding="utf-8")
    before = _claim_rows()
    with pytest.raises(ValueError, match="stale"):
        apply_claim_migration(plan)
    assert _claim_rows() == before
    assert not list((migration_root / "wiki" / ".meta" / "backups").glob("claim-migration-*"))


def test_ambiguous_identity_is_not_guessed(migration_root):
    old, _ = _legacy_long_claim(migration_root)
    path = migration_root / "wiki" / "Concept_Migrate.md"
    content = path.read_text(encoding="utf-8").replace(" NOT approved.", " NOT approved.\n\n" + "x" * 400 + " approved.")
    path.write_text(content, encoding="utf-8")
    with db_store.transaction():
        row = db_store.get_connection().execute("SELECT entity_id, data_json FROM entities WHERE f_page_key = 'Concept_Migrate'").fetchone()
        entity = json.loads(row[1])
        from vector_lake.wiki_utils import split_frontmatter
        entity["raw_text"] = split_frontmatter(content)[1]
        db_store.get_connection().execute("UPDATE entities SET data_json = ? WHERE entity_id = ?", (json.dumps(entity), row[0]))
    plan = plan_claim_migration(migration_root, ["Concept_Migrate.md"])
    assert len(plan["identity_map"][old]) == 2
    assert any("ambiguous truncated identity" in item for item in plan["blockers"])
    with pytest.raises(ValueError, match="blockers"):
        apply_claim_migration(plan)


def test_cli_preview_apply_and_root_boundary(migration_root, tmp_path):
    old, new = _legacy_long_claim(migration_root)
    repo = Path(__file__).resolve().parents[1]
    output = tmp_path / "preview.json"
    command = [sys.executable, "-B", str(repo / "scripts" / "migrate_claims.py"), "--memory-root", str(migration_root)]
    preview = subprocess.run([*command, "--page", "Concept_Migrate.md", "--plan-out", str(output)], capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    assert old in {row[0] for row in _claim_rows()}
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["identity_map"][old] == [new]
    mismatch = subprocess.run([sys.executable, "-B", str(repo / "scripts" / "migrate_claims.py"), "--memory-root", str(tmp_path / "wrong"), "--apply", "--plan", str(output)], capture_output=True, text=True)
    assert mismatch.returncode != 0
    assert old in {row[0] for row in _claim_rows()}
    applied = subprocess.run([*command, "--apply", "--plan", str(output)], capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["canonical_committed"]
    assert new in {row[0] for row in _claim_rows()}


@pytest.mark.parametrize("entry", ["content", "file"])
def test_invalid_recompile_does_not_clear_canonical(migration_root, entry):
    from vector_lake.schema_validator import SchemaViolationException
    content, _ = _seed(migration_root)
    before = _claim_rows()
    invalid = content.replace("type: concept", "type: invalid-node-type")
    with pytest.raises(SchemaViolationException):
        if entry == "content":
            governance_store.create_change_set_from_content("Concept_Migrate.md", invalid, "test", auto_approve=True)
        else:
            path = migration_root / "wiki" / "Concept_Migrate.md"
            path.write_text(invalid, encoding="utf-8")
            governance_store.create_change_set([str(path)], "test", auto_approve=True, force=True)
    assert _claim_rows() == before


@pytest.mark.parametrize("field", ["supersedes", "revision_of"])
def test_canonical_cycle_is_rejected_atomically(migration_root, field):
    content, _ = _seed(migration_root)
    delta = _prepared("Concept_Migrate", content)
    blocks = [item for item in delta["proposed_claims"] if item["claim_scope"] == "block"]
    first, second = blocks[:2]
    first[field] = second["claim_id"] if field == "revision_of" else [second["claim_id"]]
    second[field] = first["claim_id"] if field == "revision_of" else [first["claim_id"]]
    before = _claim_rows()
    with pytest.raises(ValueError, match="Cyclic"):
        governance_store.apply_change_set(delta)
    assert _claim_rows() == before


def test_preview_does_not_hide_one_claims_db_only_relation_behind_another(migration_root):
    _, peer = _seed(migration_root, "Concept_Peer")
    target = peer["proposed_claims"][0]["claim_id"]
    content = _concept("Concept_Migrate").replace("---\n##", f"claim_relations:\n  - block_index: 2\n    contradicts: [{target}]\n---\n##")
    path = migration_root / "wiki" / "Concept_Migrate.md"
    path.write_text(content, encoding="utf-8")
    change = governance_store.create_change_set_from_content(path.name, content, "test", auto_approve=True)
    first = copy.deepcopy(change["proposed_claims"][0])
    assert not first.get("contradicts")
    first["contradicts"] = [target]
    with db_store.transaction():
        db_store.get_connection().execute("UPDATE claims SET data_json = ? WHERE claim_id = ?", (json.dumps(first), first["claim_id"]))
    before = _claim_rows()
    plan = plan_claim_migration(migration_root, [path.name])
    assert any(first["claim_id"] in blocker and "contradicts" in blocker for blocker in plan["blockers"])
    with pytest.raises(ValueError, match="blockers"):
        apply_claim_migration(plan)
    assert _claim_rows() == before


@pytest.mark.parametrize("change", ["archived", "no_evidence"])
def test_reverse_contradiction_neighbor_refreshes_when_winner_loses_validity(migration_root, change):
    peer_content, peer = _seed(migration_root, "Concept_Peer")
    winner_delta = _prepared("Concept_Peer", peer_content)
    winner = winner_delta["proposed_claims"][0]
    winner.update(evidence_support="verified", verified_evidence_ids=list(winner["evidence_ids"]), authority_score=0.99)
    governance_store.apply_change_set(winner_delta)
    content, _ = _seed(migration_root)
    delta = _prepared("Concept_Migrate", content)
    challenger = delta["proposed_claims"][0]
    challenger.update(evidence_support="verified", verified_evidence_ids=list(challenger["evidence_ids"]), contradicts=[winner["claim_id"]], authority_score=0.2)
    governance_store.apply_change_set(delta)
    prior = next(item for item in governance_store.load_memory_objects()["items"].values() if item["source_claim_id"] == challenger["claim_id"])
    assert prior["validity_state"] == "superseded"
    invalid = _prepared("Concept_Peer", peer_content)
    if change == "archived":
        invalid["proposed_claims"][0]["status"] = "Archived"
    else:
        invalid["proposed_claims"][0].update(evidence_ids=[], verified_evidence_ids=[])
    governance_store.apply_change_set(invalid)
    restored = next(item for item in governance_store.load_memory_objects()["items"].values() if item["source_claim_id"] == challenger["claim_id"])
    assert restored["validity_state"] == "conflicted"
    assert "superseded_by" not in restored


def _seed_explicit_supersession(root):
    content, _ = _seed(root)
    old = _prepared("Concept_Migrate", content)
    target = old["proposed_claims"][0]
    target.update(evidence_support="verified", verified_evidence_ids=list(target["evidence_ids"]))
    governance_store.apply_change_set(old)
    peer_content, _ = _seed(root, "Concept_Peer")
    delta = _prepared("Concept_Peer", peer_content)
    replacement = delta["proposed_claims"][0]
    replacement.update(evidence_support="verified", verified_evidence_ids=list(replacement["evidence_ids"]), supersedes=[target["claim_id"]])
    governance_store.apply_change_set(delta)
    return target["claim_id"], replacement["claim_id"]


def test_real_delete_cascade_restores_explicit_predecessor(migration_root):
    old, new = _seed_explicit_supersession(migration_root)
    prior = [item for item in governance_store.load_memory_objects()["items"].values() if item["source_claim_id"] == old][0]
    assert prior["validity_state"] == "superseded"
    db_store.delete_node_cascade("Concept_Peer")
    remaining = list(governance_store.load_memory_objects()["items"].values())
    predecessor = next(item for item in remaining if item["source_claim_id"] == old)
    assert predecessor["validity_state"] == "active"
    assert "superseded_by" not in predecessor
    assert not any(item["source_claim_id"] == new for item in remaining)


def test_real_delete_cascade_rejects_dangling_inbound_relation(migration_root):
    _seed_explicit_supersession(migration_root)
    before = _claim_rows()
    with pytest.raises(ValueError, match="orphan supersedes"):
        db_store.delete_node_cascade("Concept_Migrate")
    assert _claim_rows() == before


@pytest.mark.parametrize("peer_action", ["delete", "clear_reference"])
def test_canonical_batch_can_remove_inbound_reference_explicitly(migration_root, peer_action):
    from vector_lake.mutation_coordinator import execute_mutation_batch
    _seed_explicit_supersession(migration_root)
    peer = {"filename": "Concept_Peer.md", "is_delete": True}
    if peer_action == "clear_reference":
        peer = {"filename": "Concept_Peer.md", "content": (migration_root / "wiki" / "Concept_Peer.md").read_text(encoding="utf-8")}
    ok, _ = execute_mutation_batch([
        {"filename": "Concept_Migrate.md", "is_delete": True}, peer,
    ], validation_mode="schema")
    assert ok
    records = [json.loads(row[1]) for row in _claim_rows()]
    assert all(record["source_page"] != "Concept_Migrate.md" for record in records)
    assert all(not record.get("supersedes") for record in records)


@pytest.mark.parametrize("name", ["../Concept_Escape.md", "sub/Concept_Escape.md", "Concept_Escape.txt"])
def test_preview_rejects_out_of_scope_paths(migration_root, name):
    with pytest.raises(ValueError, match="basenames"):
        plan_claim_migration(migration_root, [name])
