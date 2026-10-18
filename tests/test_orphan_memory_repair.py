import json
import sqlite3

import pytest

from scripts.repair_orphan_memory import candidates, freeze, maintain, readonly
from vector_lake import db_store, memory_gram_index


def test_cleanup_restores_read_and_write_after_authorizer(isolated_memory):
    from scripts.repair_orphan_memory import cleanup_connection
    conn = db_store.get_connection()
    timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
    conn.set_authorizer(lambda *_: sqlite3.SQLITE_DENY)
    assert cleanup_connection(conn, timeout) == []
    assert conn.execute('SELECT 1').fetchone()[0] == 1
    conn.execute('CREATE TEMP TABLE cleanup_probe(value)')
    conn.execute('DROP TABLE cleanup_probe')


def seed_memory(conn, memory_id, claim_id, page, *, live_claim=False):
    payload = {"memory_id": memory_id, "memory_type": "fact", "source_claim_id": claim_id,
               "source_page": page + ".md", "text": "Synthetic regression fact",
               "value": "Synthetic regression fact", "status": "Active", "validity_state": "active"}
    with db_store.transaction():
        if live_claim:
            conn.execute("INSERT INTO claims(claim_id, data_json, updated_at) VALUES (?, ?, ?)",
                         (claim_id, json.dumps({"claim_id": claim_id, "claim_text": "Synthetic fact",
                                              "source_page": page + ".md", "page_key": page}), "2026-10-02"))
        conn.execute("INSERT INTO operational_memory(memory_id, memory_type, score, data_json, updated_at, status, ttl) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (memory_id, "fact", 1.0, json.dumps(payload), "2026-10-02", "Active", 365))


def approved(archive):
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    return {"approved_count": manifest["count"], "approved_sha256": manifest["scope_sha256"]}


def seed_scope():
    db_store.init_db()
    conn = db_store.get_connection()
    seed_memory(conn, "mem_orphan", "claim_retired", "Deleted_Page")
    seed_memory(conn, "mem_keep", "claim_live", "Live_Page", live_claim=True)
    return conn


def test_delete_cascade_removes_claim_memory_and_retains_unrelated(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    seed_memory(conn, "mem_deleted", "claim_deleted", "Deleted_Page", live_claim=True)
    seed_memory(conn, "mem_keep", "claim_live", "Live_Page", live_claim=True)
    memory_gram_index.rebuild_memory_gram_index()
    old_doc_id = conn.execute("SELECT doc FROM operational_memory_gram_docs WHERE memory_id='mem_deleted'").fetchone()[0]

    db_store.delete_node_cascade("Deleted_Page")

    assert conn.execute("SELECT 1 FROM claims WHERE claim_id='claim_deleted'").fetchone() is None
    assert conn.execute("SELECT 1 FROM operational_memory WHERE memory_id='mem_deleted'").fetchone() is None
    assert conn.execute("SELECT 1 FROM operational_memory_index WHERE memory_id='mem_deleted'").fetchone() is None
    assert old_doc_id in memory_gram_index.skip_doc_set(conn)
    assert conn.execute("SELECT 1 FROM operational_memory WHERE memory_id='mem_keep'").fetchone() is not None


def test_delete_cascade_rolls_back_if_memory_cleanup_fails(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    seed_memory(conn, "mem_deleted", "claim_deleted", "Deleted_Page", live_claim=True)
    conn.execute("CREATE TEMP TRIGGER fail_memory_delete BEFORE DELETE ON operational_memory BEGIN SELECT RAISE(ABORT, 'synthetic delete failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic delete failure"):
        db_store.delete_node_cascade("Deleted_Page")
    assert conn.execute("SELECT 1 FROM claims WHERE claim_id='claim_deleted'").fetchone() is not None
    assert conn.execute("SELECT 1 FROM operational_memory WHERE memory_id='mem_deleted'").fetchone() is not None


def test_frozen_cleanup_dry_run_apply_restore_and_index_markers(isolated_memory, tmp_path):
    conn = seed_scope()
    memory_gram_index.rebuild_memory_gram_index()
    old_doc_id = conn.execute("SELECT doc FROM operational_memory_gram_docs WHERE memory_id='mem_orphan'").fetchone()[0]
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    with readonly(archive / "database.sqlite.bak") as backup:
        assert len(candidates(backup)) == 1
    assert maintain(database, archive)["would_delete"] == 1
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    assert maintain(database, archive, "apply", **approved(archive))["affected"] == 1
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 1
    assert old_doc_id in memory_gram_index.skip_doc_set(conn)
    assert conn.execute("SELECT 1 FROM operational_memory_index WHERE memory_id='mem_orphan'").fetchone() is None
    assert maintain(database, archive, "restore", **approved(archive))["affected"] == 1
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    with pytest.raises(RuntimeError, match="overwrite"):
        maintain(database, archive, "restore", **approved(archive))


def test_cleanup_rejects_changed_frozen_payload(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    with db_store.transaction():
        conn.execute("UPDATE operational_memory SET updated_at='later' WHERE memory_id='mem_orphan'")
    with pytest.raises(RuntimeError, match="scope changed"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


def test_cleanup_preserves_memory_if_canonical_page_reappears(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    with db_store.transaction():
        conn.execute("INSERT INTO entities(entity_id, canonical_name, data_json, updated_at) VALUES (?, ?, ?, ?)",
                     ("entity_revived", "Deleted_Page", json.dumps({"page_key": "Deleted_Page"}), "now"))
    with pytest.raises(RuntimeError, match="scope changed"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


def test_cleanup_rolls_back_when_native_index_delete_fails(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    conn.execute("CREATE TEMP TRIGGER fail_index_delete BEFORE DELETE ON operational_memory_index BEGIN SELECT RAISE(ABORT, 'synthetic index failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic index failure"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0] == 2


def test_cleanup_rejects_corrupt_recovery_archive(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    records = json.loads((archive / "records.json").read_text(encoding="utf-8"))
    records[0]["score"] = -100
    (archive / "records.json").write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Recovery records"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


def test_apply_requires_independently_supplied_scope_identity(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    with pytest.raises(RuntimeError, match="approved count/hash"):
        maintain(database, archive, "apply")
    with pytest.raises(RuntimeError, match="approved count/hash"):
        maintain(database, archive, "apply", approved_count=1, approved_sha256="0" * 64)
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


def test_pending_canonical_mutation_blocks_freeze_and_apply(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    db_store.enqueue_mutations_batch([{"filename": "Deleted_Page.md", "mutation_type": "update",
                                     "payload_text": "Synthetic pending page", "idempotency_key": "pending-ownership"}])
    with pytest.raises(RuntimeError, match="Canonical mutations are pending"):
        freeze(database, tmp_path / "pending-archive", 1)
    with pytest.raises(RuntimeError, match="Canonical mutations are pending"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


@pytest.mark.parametrize("ineffective", [False, True])
def test_missing_or_ineffective_gram_delete_trigger_rolls_back(isolated_memory, tmp_path, ineffective):
    conn = seed_scope()
    memory_gram_index.rebuild_memory_gram_index()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    conn.execute("DROP TRIGGER trg_om_gram_dirty_delete")
    if ineffective:
        conn.execute("CREATE TRIGGER trg_om_gram_dirty_delete AFTER DELETE ON operational_memory_index BEGIN SELECT 1; END")
    with pytest.raises(RuntimeError, match="gram retirement/dirty markers"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0] == 2


def test_restore_checks_new_gram_markers(isolated_memory, tmp_path):
    """A settled queue has to be re-marked by the restore, and the restore has to notice.

    A document's number survives both halves of this maintenance, so the retirement marker an
    ``apply`` queues is normally still there when its rows come back -- nothing new has to happen
    for the base to stay honest.  The check still has to exist, because a queue is settled by a
    rebuild: after one, the base holds those postings as current, and a restore that cannot queue
    the documents it resurrected leaves them credited as current too.  Clearing the queue first is
    that state; dropping the trigger is the failure the rollback is there for.
    """
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    maintain(database, archive, "apply", **approved(archive))
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_gram_dirty")
    conn.execute("DROP TRIGGER trg_om_gram_dirty_insert")
    with pytest.raises(RuntimeError, match="gram retirement/dirty markers"):
        maintain(database, archive, "restore", **approved(archive))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 1


def test_restore_checks_projection_content(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    maintain(database, archive, "apply", **approved(archive))
    conn.execute("CREATE TEMP TRIGGER corrupt_restored_projection AFTER INSERT ON operational_memory_index BEGIN UPDATE operational_memory_index SET text_blob='corrupt' WHERE memory_id=NEW.memory_id; END")
    with pytest.raises(RuntimeError, match="lookup projection differs"):
        maintain(database, archive, "restore", **approved(archive))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 1


def test_same_count_authority_write_is_prohibited(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    conn.execute("CREATE TEMP TRIGGER corrupt_other_authority AFTER DELETE ON operational_memory BEGIN UPDATE claims SET updated_at='corrupt' WHERE claim_id='claim_live'; END")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT updated_at FROM claims WHERE claim_id='claim_live'").fetchone()[0] == "2026-10-02"
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


def test_deadline_interrupts_sql_and_rolls_back(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    conn.execute("CREATE TEMP TRIGGER slow_delete AFTER DELETE ON operational_memory BEGIN SELECT sum(n) FROM (WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<10000000) SELECT n FROM numbers); END")
    with pytest.raises((sqlite3.OperationalError, TimeoutError), match="interrupted|deadline"):
        maintain(database, archive, "apply", **approved(archive), timeout_seconds=0.05)
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0] == 2
    assert not conn.in_transaction


@pytest.mark.parametrize("identity", ["entity_id", "canonical_name"])
def test_explicit_page_key_overrides_display_identity_in_cleanup(isolated_memory, tmp_path, identity):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    entity_id = "Deleted_Page" if identity == "entity_id" else "other_entity"
    title = "Deleted_Page" if identity == "canonical_name" else "Other Title"
    with db_store.transaction():
        conn.execute("INSERT INTO entities(entity_id, canonical_name, data_json, updated_at) VALUES (?, ?, ?, ?)",
                     (entity_id, title, json.dumps({"page_key": "Different_Page"}), "now"))
    assert maintain(database, archive, "apply", **approved(archive))["affected"] == 1
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 1
    assert conn.execute("SELECT f_page_key FROM entities WHERE entity_id=?", (entity_id,)).fetchone()[0] == "Different_Page"


def test_receipt_failure_reports_committed_state(isolated_memory, tmp_path, monkeypatch):
    from scripts import repair_orphan_memory as repair
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    actual_write = repair.write_json

    def fail_only_committed_receipt(path, data):
        if str(path).endswith(".committed.json"):
            raise OSError("synthetic receipt failure")
        return actual_write(path, data)

    monkeypatch.setattr(repair, "write_json", fail_only_committed_receipt)
    result = maintain(database, archive, "apply", **approved(archive))
    assert result["state"] == "committed"
    assert result["receipt_published"] is False
    assert result["receipt_error_type"] == "OSError"
    assert list(archive.glob("*.prepared.json"))
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 1


@pytest.mark.parametrize("outer_rollback", [False, True])
def test_nested_native_transaction_is_rejected_before_preparation(isolated_memory, tmp_path, outer_rollback):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    try:
        with db_store.transaction():
            with pytest.raises(RuntimeError, match="nested entry"):
                maintain(database, archive, "apply", **approved(archive))
            if outer_rollback:
                raise ValueError("outer rollback")
    except ValueError:
        assert outer_rollback
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    assert not list(archive.glob("*.prepared.json"))
    assert not list(archive.glob("*.committed.json"))


def test_raw_outer_transaction_is_also_rejected(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="nested entry"):
            maintain(database, archive, "apply", **approved(archive))
    finally:
        conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    assert not list(archive.glob("*.committed.json"))


def test_unrelated_gram_marker_deletion_is_prohibited(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    conn.execute("INSERT INTO operational_memory_gram_dirty(doc) VALUES (-99)")
    conn.commit()
    conn.execute("CREATE TEMP TRIGGER erase_other_marker AFTER DELETE ON operational_memory BEGIN DELETE FROM operational_memory_gram_dirty WHERE doc=-99; END")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT 1 FROM operational_memory_gram_dirty WHERE doc=-99").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


def test_unrelated_gram_marker_addition_rolls_back(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    conn.execute("CREATE TEMP TRIGGER add_other_marker AFTER DELETE ON operational_memory BEGIN INSERT INTO operational_memory_gram_dirty(doc) VALUES (-99); END")
    with pytest.raises(RuntimeError, match="Unrelated gram markers changed"):
        maintain(database, archive, "apply", **approved(archive))
    assert conn.execute("SELECT 1 FROM operational_memory_gram_dirty WHERE doc=-99").fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


def test_gram_state_write_is_prohibited(isolated_memory, tmp_path):
    conn = seed_scope()
    memory_gram_index.rebuild_memory_gram_index()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    original = tuple(conn.execute("SELECT * FROM operational_memory_gram_state").fetchone())
    conn.execute("CREATE TEMP TRIGGER corrupt_gram_state AFTER DELETE ON operational_memory BEGIN UPDATE operational_memory_gram_state SET doc_count=0; END")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        maintain(database, archive, "apply", **approved(archive))
    assert tuple(conn.execute("SELECT * FROM operational_memory_gram_state").fetchone()) == original
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


def test_restore_preserves_canonical_tie_break_order(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    before = [tuple(r) for r in conn.execute("SELECT memory_id,source_rowid FROM operational_memory_index ORDER BY memory_score DESC,source_rowid")]
    maintain(database, archive, "apply", **approved(archive))
    maintain(database, archive, "restore", **approved(archive))
    after = [tuple(r) for r in conn.execute("SELECT memory_id,source_rowid FROM operational_memory_index ORDER BY memory_score DESC,source_rowid")]
    assert after == before


def test_restore_respects_text_affinity_for_legacy_numeric_timestamp(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    with db_store.transaction():
        conn.execute("UPDATE operational_memory SET data_json=json_set(data_json,'$.updated_at',1728000) WHERE memory_id='mem_orphan'")
    freeze(database, archive, 1)
    maintain(database, archive, "apply", **approved(archive))
    maintain(database, archive, "restore", **approved(archive))
    row = conn.execute("SELECT source_updated_at,typeof(source_updated_at) FROM operational_memory_index WHERE memory_id='mem_orphan'").fetchone()
    assert tuple(row) == ("1728000", "text")


def test_restore_rejects_canonical_rowid_collision(isolated_memory, tmp_path):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    old_rowid = conn.execute("SELECT rowid FROM operational_memory WHERE memory_id='mem_orphan'").fetchone()[0]
    maintain(database, archive, "apply", **approved(archive))
    with db_store.transaction():
        conn.execute("INSERT INTO operational_memory(rowid,memory_id,memory_type,score,data_json,updated_at,status,ttl) VALUES (?, 'new_owner','fact',1,'{}','now','Active',365)", (old_rowid,))
    with pytest.raises(RuntimeError, match="canonical rowid"):
        maintain(database, archive, "restore", **approved(archive))
    assert conn.execute("SELECT 1 FROM operational_memory WHERE memory_id='new_owner'").fetchone()
    assert conn.execute("SELECT 1 FROM operational_memory WHERE memory_id='mem_orphan'").fetchone() is None


def test_commit_failure_rolls_back_without_committed_receipt(isolated_memory, tmp_path, monkeypatch):
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)

    class CommitFailureConnection(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("synthetic commit failure")

    conn.close()
    failing = sqlite3.connect(database, factory=CommitFailureConnection)
    failing.row_factory = sqlite3.Row
    monkeypatch.setattr(db_store._LOCAL, "conn", failing)
    with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
        maintain(database, archive, "apply", **approved(archive))
    assert not failing.in_transaction
    assert failing.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    assert failing.execute("SELECT COUNT(*) FROM operational_memory_index").fetchone()[0] == 2
    assert not list(archive.glob("*.committed.json"))


def test_lock_contention_stays_inside_native_budget(isolated_memory, tmp_path, monkeypatch):
    import time
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    monkeypatch.setattr(db_store, "BEGIN_LOCK_BUDGET_SECONDS", 0.05)
    blocker = sqlite3.connect(database)
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(db_store.DatabaseLockTimeout):
            maintain(database, archive, "apply", **approved(archive))
    finally:
        blocker.rollback()
        blocker.close()
    assert time.monotonic() - started < 2
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2
    assert not list(archive.glob("*.committed.json"))


def install_legacy_lookup_triggers(conn):
    columns = db_store._OM_INDEX_COLUMNS[:-1]
    values = db_store._om_index_value_expr("NEW").rsplit(",", 1)[0]
    upsert = ",".join(f"{name}=excluded.{name}" for name in columns if name != "memory_id")
    with db_store.transaction():
        for name, event in [("trg_om_index_insert", "AFTER INSERT"), ("trg_om_index_update", "AFTER UPDATE")]:
            conn.execute("DROP TRIGGER " + name)
            conn.execute(f"CREATE TRIGGER {name} {event} ON operational_memory BEGIN INSERT INTO operational_memory_index ({','.join(columns)}) VALUES (NEW.memory_id,{values}) ON CONFLICT(memory_id) DO UPDATE SET {upsert}; END")


def test_native_initialization_upgrades_legacy_lookup_triggers(isolated_memory):
    conn = seed_scope()
    install_legacy_lookup_triggers(conn)
    seed_memory(conn, "mem_legacy", "claim_legacy", "Deleted_Page")
    assert conn.execute("SELECT source_rowid FROM operational_memory_index WHERE memory_id='mem_legacy'").fetchone()[0] == 0
    with db_store.transaction():
        db_store._create_operational_memory_index(conn)
    for name in ["trg_om_index_insert", "trg_om_index_update"]:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
        assert "source_rowid" in sql and "NEW.rowid" in sql
    with db_store.transaction():
        conn.execute("UPDATE operational_memory SET updated_at='later' WHERE memory_id='mem_legacy'")
    row = conn.execute("SELECT m.rowid,i.source_rowid FROM operational_memory m JOIN operational_memory_index i ON i.memory_id=m.memory_id WHERE m.memory_id='mem_legacy'").fetchone()
    assert row[0] == row[1]


def test_bounded_trigger_refresh_preserves_rows_and_unrelated_schema(isolated_memory, tmp_path):
    from scripts.repair_orphan_memory import refresh_triggers, fingerprint
    conn = seed_scope()
    install_legacy_lookup_triggers(conn)
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    before = fingerprint(conn.execute("SELECT * FROM operational_memory ORDER BY memory_id"))
    lookup_before = fingerprint(conn.execute("SELECT * FROM operational_memory_index ORDER BY memory_id"))
    result = refresh_triggers(database, archive, **approved(archive))
    assert result["changed_triggers"] == ["trg_om_index_insert", "trg_om_index_update"]
    assert result["data_rows_unchanged"] and result["unrelated_schema_unchanged"]
    assert not result["whole_corpus_rebuild"]
    assert fingerprint(conn.execute("SELECT * FROM operational_memory ORDER BY memory_id")) == before
    assert fingerprint(conn.execute("SELECT * FROM operational_memory_index ORDER BY memory_id")) == lookup_before
    maintain(database, archive, "apply", **approved(archive))
    maintain(database, archive, "restore", **approved(archive))


def test_trigger_refresh_rolls_back_invalid_definition(isolated_memory, tmp_path, monkeypatch):
    from scripts.repair_orphan_memory import refresh_triggers
    conn = seed_scope()
    install_legacy_lookup_triggers(conn)
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    before = {r[0]: r[1] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('trg_om_index_insert','trg_om_index_update')")}
    monkeypatch.setattr(db_store, "_om_index_value_expr", lambda _source: ") INVALID_SQL (")
    with pytest.raises(sqlite3.OperationalError):
        refresh_triggers(database, archive, **approved(archive))
    after = {r[0]: r[1] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('trg_om_index_insert','trg_om_index_update')")}
    assert after == before
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 2


@pytest.mark.parametrize("mode", ["apply", "refresh-triggers"])
def test_post_commit_cleanup_failure_reports_committed_state(isolated_memory, tmp_path, monkeypatch, mode):
    from scripts.repair_orphan_memory import refresh_triggers
    conn = seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)

    class CleanupFailureConnection(sqlite3.Connection):
        just_committed = False

        def commit(self):
            result = super().commit()
            self.just_committed = True
            return result

        def execute(self, sql, *args, **kwargs):
            if self.just_committed and sql.startswith("PRAGMA busy_timeout="):
                raise sqlite3.OperationalError("synthetic post-commit cleanup failure")
            return super().execute(sql, *args, **kwargs)

    conn.close()
    failing = sqlite3.connect(database, factory=CleanupFailureConnection)
    failing.row_factory = sqlite3.Row
    monkeypatch.setattr(db_store._LOCAL, "conn", failing)
    if mode == "apply":
        result = maintain(database, archive, "apply", **approved(archive))
        expected_count = 1
    else:
        result = refresh_triggers(database, archive, **approved(archive))
        expected_count = 2
    assert result["state"] == "committed"
    assert result["connection_cleanup_errors"] == [{"action": "busy_timeout", "error_type": "OperationalError"}]
    assert result["receipt_published"]
    assert not failing.in_transaction
    assert failing.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == expected_count


def test_recovery_verification_honors_an_expired_deadline(isolated_memory, tmp_path):
    from scripts.repair_orphan_memory import recovery_rowids, load_archive, Deadline
    seed_scope()
    database = db_store.get_db_path()
    archive = tmp_path / "archive"
    freeze(database, archive, 1)
    manifest, rows = load_archive(archive, database)
    budget = Deadline(0.001)
    # A short sleep cannot establish expiry on a coarse Windows clock.
    budget.end -= 1.0
    with pytest.raises(TimeoutError, match="deadline"):
        recovery_rowids(archive, manifest, rows, budget)
    assert not (archive / "recovery-rowids.json").exists()
