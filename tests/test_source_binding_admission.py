"""Actual dedicated API -> typed receipt -> strict owned reader, synthetic Windows only."""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vector_lake import query_evidence as evidence
from vector_lake.query_evidence_host import ReadOnlyEvidenceHost
from vector_lake import source_binding_admission as admission
from vector_lake import source_binding_record as records
from vector_lake.governance_store import _canonical_entity_rows_version

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows-only owned admission/reader contract')
CONFIG = {'exclude_paths': [], 'supported_extensions': ['.md', '.txt']}
DOMAIN = 'Artificial_Intelligence'
KEY = 'Source_legacy-owned-name'
REFERENCE = 'raw/人工智能/provided.md'
RAW = '\ufeff# Synthetic document\r\n\r\nWritten goals must be shared.\r\nHumans choose the work.\r\n'.encode('utf-8')


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'MEMORY'
    (root / 'wiki' / '.meta').mkdir(parents=True)
    raw = root.joinpath(*REFERENCE.split('/'))
    raw.parent.mkdir(parents=True)
    raw.write_bytes(RAW)
    source = {'entity_id': 'synthetic-source', 'id': 'stable-owner', 'page_key': KEY,
              'canonical_name': KEY + '.md', 'type': 'source', 'status': 'Active', 'domain': DOMAIN,
              'topic_cluster': 'Synthetic', 'sources': [REFERENCE], 'source_hash': None,
              'source_frontmatter': None, 'raw_text': '\ufeff---\r\ntype: source\r\n---\r\n# Legacy canonical body\r\n'}
    wiki = root / 'wiki' / (KEY + '.md')
    wiki.write_bytes(source['raw_text'].encode())
    db = root / 'wiki' / '.meta' / 'vector_lake.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('''
        CREATE TABLE entities(entity_id TEXT PRIMARY KEY,data_json TEXT,
          f_page_key TEXT GENERATED ALWAYS AS (json_extract(data_json,'$.page_key')) STORED);
        CREATE TABLE page_index_nodes(node_key TEXT PRIMARY KEY,domain TEXT,topic_cluster TEXT);
        CREATE TABLE jobs(job_id TEXT PRIMARY KEY,task_type TEXT,payload TEXT,status TEXT,retries INTEGER DEFAULT0,
          error_msg TEXT,created_at TEXT,updated_at TEXT,available_at TEXT,lease_until TEXT,lease_owner TEXT,
          lease_token TEXT,lease_generation INTEGER DEFAULT0,idempotency_key TEXT,task_packet_path TEXT,
          completed_at TEXT,result_json TEXT);
        '''.replace('DEFAULT0', 'DEFAULT 0'))
        conn.execute('INSERT INTO entities(entity_id,data_json) VALUES (?,?)',
                     (source['entity_id'], json.dumps(source, ensure_ascii=False)))
        conn.execute('INSERT INTO page_index_nodes VALUES (?,?,?)', (KEY, DOMAIN, 'Synthetic'))
        for table in ['claims', 'evidence', 'sources', 'claim_graph_edges', 'processed_files', 'operational_memory']:
            conn.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY,data_json TEXT)')
            conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('preserve-' + table, '{"fixture":"unchanged"}'))
    target = admission.BindingTarget(KEY, DOMAIN, REFERENCE, hashlib.sha256(RAW).hexdigest())
    host = admission.SourceBindingAdmissionHost(root, CONFIG, (target,), owner_scope_sha256='b' * 64)
    return {'root': root, 'db': db, 'raw': raw, 'wiki': wiki, 'source': source, 'target': target, 'host': host}


def snapshot(case):
    with sqlite3.connect(case['db']) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        result: dict[str, list[tuple] | str | bytes] = {
            t: conn.execute('SELECT * FROM "' + t + '" ORDER BY rowid').fetchall() for t in tables}
        result['schema'] = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall()
        result['canonical_version'] = _canonical_entity_rows_version(
            conn.execute('SELECT entity_id,data_json FROM entities ORDER BY entity_id').fetchall())
    result['wiki_bytes'] = case['wiki'].read_bytes()
    result['raw_bytes'] = case['raw'].read_bytes()
    return result


def query(case):
    return ReadOnlyEvidenceHost(case['root'], CONFIG).get_query_evidence('written goals humans', [KEY], domain=DOMAIN)


def assert_protected_equal(before, after):
    assert {k: v for k, v in before.items() if k != 'jobs'} == {k: v for k, v in after.items() if k != 'jobs'}


def test_real_current_admission_preserves_every_knowledge_table_version_and_byte(case):
    before = snapshot(case)
    assert query(case)['evidence'][0]['code'] == 'binding_unproven'
    prepared = case['host'].prepare(KEY, DOMAIN)
    assert snapshot(case) == before
    result = case['host'].admit(prepared)
    assert result['status'] == 'admitted'
    assert len(snapshot(case)['jobs']) == 1
    assert_protected_equal(before, snapshot(case))
    recovery = Path(result['recovery_file'])
    assert recovery.is_relative_to(case['root'] / 'wiki' / '.meta')
    assert hashlib.sha256(recovery.read_bytes()).hexdigest() == result['receipt']['recovery_sha256']
    assert json.loads(recovery.read_bytes())['before'] is None
    evidence_result = query(case)
    assert evidence_result['status'] == 'complete'
    item = evidence_result['evidence'][0]
    assert item['status'] == 'verified'
    assert item['source_sha256'] == hashlib.sha256(RAW).hexdigest()
    assert item['canonical_source_record_sha256'] == records.record_fingerprint(case['source'])
    assert item['binding_admission']['receipt_job_id'] == result['receipt']['receipt_job_id']
    assert item['binding_level'] == records.BINDING_SCOPE
    assert item['author_and_publication_truth'] == 'not_adjudicated'
    # No model retyping/normalization: selected Raw byte slices really decode to quote text.
    for passage in item['passages']:
        assert RAW[passage['byte_start']:passage['byte_end']].decode() == passage['text']


def test_replay_is_read_only_and_no_second_receipt_or_backup(case):
    prepared = case['host'].prepare(KEY, DOMAIN)
    first = case['host'].admit(prepared)
    before = snapshot(case)
    files = list((case['root'] / 'wiki' / '.meta').glob('source-binding-before-*.json'))
    second = case['host'].admit(prepared)
    assert second['status'] == 'already_admitted'
    assert second['receipt'] == first['receipt']
    assert second['recovery_file'] is None
    assert snapshot(case) == before
    assert list((case['root'] / 'wiki' / '.meta').glob('source-binding-before-*.json')) == files


def test_legacy_name_and_missing_old_refs_require_explicit_current_mapping(case):
    assert KEY + '.md' != Path(REFERENCE).name
    source = dict(case['source'], sources=[])
    with sqlite3.connect(case['db']) as conn:
        conn.execute('UPDATE entities SET data_json=?', (json.dumps(source, ensure_ascii=False),))
    before = snapshot(case)
    with pytest.raises(evidence.EvidenceRefused, match='target_not_in_approved_scope'):
        case['host'].prepare('Source_similarly_named_unapproved', DOMAIN)
    prepared = case['host'].prepare(KEY, DOMAIN)
    case['host'].admit(prepared)
    assert query(case)['evidence'][0]['status'] == 'verified'
    assert_protected_equal(before, snapshot(case))


def test_partial_legacy_stamp_is_never_overridden(case):
    source = dict(case['source'], source_hash='not-a-supported-hash')
    with sqlite3.connect(case['db']) as conn:
        conn.execute('UPDATE entities SET data_json=?', (json.dumps(source),))
    before = snapshot(case)
    with pytest.raises(evidence.EvidenceRefused, match='existing_legacy_binding_not_admissible'):
        case['host'].prepare(KEY, DOMAIN)
    assert snapshot(case) == before


def test_source_version_drift_refuses_before_control_mutation(case):
    prepared = case['host'].prepare(KEY, DOMAIN)
    with sqlite3.connect(case['db']) as conn:
        conn.execute('UPDATE entities SET data_json=?', (json.dumps(dict(case['source'], raw_text='Other current version')),))
    before = snapshot(case)
    with pytest.raises(evidence.EvidenceRefused, match='canonical_source_version_changed'):
        case['host'].admit(prepared)
    assert snapshot(case) == before


def test_raw_drift_refuses_and_never_guesses_a_different_digest(case):
    prepared = case['host'].prepare(KEY, DOMAIN)
    case['raw'].write_bytes(RAW + b'changed')
    before = snapshot(case)
    with pytest.raises(evidence.EvidenceRefused, match='raw_snapshot_changed'):
        case['host'].admit(prepared)
    assert snapshot(case) == before


def test_recovery_export_failure_leaves_database_unchanged(case, monkeypatch):
    prepared = case['host'].prepare(KEY, DOMAIN)
    before = snapshot(case)
    def fail(*args):
        raise OSError('synthetic recovery failure')
    monkeypatch.setattr(case['host'], '_recovery', fail)
    with pytest.raises(OSError, match='synthetic recovery failure'):
        case['host'].admit(prepared)
    assert snapshot(case) == before


def test_expired_real_lease_rolls_back_insert(case, monkeypatch):
    prepared = case['host'].prepare(KEY, DOMAIN)
    before = snapshot(case)
    ticks = iter([datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)])
    monkeypatch.setattr(admission, '_now', lambda: next(ticks))
    with pytest.raises(evidence.EvidenceRefused, match='admission_lease_lost'):
        case['host'].admit(prepared)
    assert snapshot(case) == before


def test_jobs_trigger_cannot_turn_admission_into_knowledge_mutation(case):
    prepared = case['host'].prepare(KEY, DOMAIN)
    with sqlite3.connect(case['db']) as conn:
        conn.execute('CREATE TRIGGER unsafe AFTER INSERT ON jobs BEGIN DELETE FROM claims; END')
    before = snapshot(case)
    with pytest.raises(evidence.EvidenceRefused, match='admission_job_triggers_unsupported'):
        case['host'].admit(prepared)
    assert snapshot(case) == before


def test_explicit_revoke_is_cas_guarded_preserves_history_and_does_not_reopen(case):
    prepared = case['host'].prepare(KEY, DOMAIN)
    result = case['host'].admit(prepared)
    before = snapshot(case)
    with pytest.raises(evidence.EvidenceRefused, match='admission_receipt_cas_changed'):
        case['host'].revoke(prepared, expected_receipt_sha256='d' * 64)
    assert snapshot(case) == before
    digest = hashlib.sha256(records.canonical_bytes(result['receipt'])).hexdigest()
    revoked = case['host'].revoke(prepared, expected_receipt_sha256=digest)
    assert revoked['status'] == 'revoked'
    after = snapshot(case)
    assert_protected_equal(before, after)
    assert len(after['jobs']) == 1
    assert query(case)['evidence'][0]['code'] == 'admission_receipt_inactive'
    with pytest.raises(records.BindingReceiptRefused, match='admission_receipt_inactive'):
        case['host'].admit(prepared)


def test_committed_close_failure_is_reported_without_duplicate_admission(case, monkeypatch):
    prepared = case['host'].prepare(KEY, DOMAIN)
    before = snapshot(case)
    real = sqlite3.connect
    class CloseFault:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def close(self):
            self.connection.close()  # actual handle closed; test reporting, not OS-close recovery
            raise OSError('synthetic close after actual close')
    with monkeypatch.context() as patcher:
        patcher.setattr(admission.sqlite3, 'connect', lambda *a, **k: CloseFault(real(*a, **k)))
        with pytest.raises(evidence._ResourceCleanupError):
            case['host'].admit(prepared)
    assert_protected_equal(before, snapshot(case))
    assert len(snapshot(case)['jobs']) == 1
    assert query(case)['evidence'][0]['status'] == 'verified'
    assert case['host'].admit(prepared)['status'] == 'already_admitted'


def test_scope_and_index_currentness_are_checked_before_raw_access(case, monkeypatch):
    with pytest.raises(evidence.EvidenceRefused, match='target_not_in_approved_scope'):
        case['host'].prepare(KEY, 'Medical_IT')
    with sqlite3.connect(case['db']) as conn:
        conn.execute('UPDATE page_index_nodes SET domain=?', ('Medical_IT',))
    before = snapshot(case)
    def forbidden(*args):
        raise AssertionError('Raw access must not precede scope/index validation')
    monkeypatch.setattr(case['host'], '_raw', forbidden)
    with pytest.raises(evidence.EvidenceRefused, match='not_available'):
        case['host'].prepare(KEY, DOMAIN)
    assert snapshot(case) == before


def test_prepared_object_cannot_expand_the_frozen_target_scope(case):
    from dataclasses import replace
    prepared = case['host'].prepare(KEY, DOMAIN)
    forged = replace(prepared, target=replace(case['target'], raw_reference='raw/人工智能/not-approved.md'))
    before = snapshot(case)
    with pytest.raises(evidence.EvidenceRefused, match='prepared_admission_scope_mismatch'):
        case['host'].admit(forged)
    assert snapshot(case) == before


def test_actual_native_schema_and_canonical_pipeline_also_keep_version_unchanged(tmp_path, monkeypatch):
    from vector_lake import db_store, wiki_utils, mutation_coordinator, page_index_projection, tool_ingest
    root = tmp_path / 'native' / 'MEMORY'
    (root / 'wiki' / '.meta').mkdir(parents=True)
    raw = root.joinpath(*REFERENCE.split('/'))
    raw.parent.mkdir(parents=True)
    raw.write_bytes(RAW)
    db_store.close_connection()
    wiki_utils.reset_memory_dir_cache()
    monkeypatch.setenv('VECTOR_LAKE_MEMORY_DIR', str(root))
    monkeypatch.delenv('VECTOR_LAKE_DB_PATH', raising=False)
    try:
        db_store.init_db()  # TEST FIXTURE ONLY, never called by the admission/reader.
        content = '''---
entity_id: entity_native_fixture
id: native-fixture
title: Explicit native fixture
type: source
domain: Artificial_Intelligence
status: Active
epistemic-status: seed
categories: [System_Architecture]
created: '2026-01-01T00:00:00+00:00'
updated: '2026-01-01T00:00:00+00:00'
sources: [raw/人工智能/provided.md]
strategic_scope: core
evidence_tier: primary
---
# Native fixture
A sufficiently long synthetic claim built by the real canonical parser, not hand-stamped metadata.
'''
        ok, _ = mutation_coordinator.execute_mutation_batch(
            [{'filename': KEY + '.md', 'content': content}], validation_mode='schema')
        assert ok
        conn = db_store.get_connection()
        # Canonical commit intentionally does not bootstrap a reader's derived index.
        # Finish the one-document TEST FIXTURE using the real writer-side projector;
        # never remove the admission reader's current scope/index prerequisite.
        source_rows = conn.execute("SELECT data_json FROM entities WHERE f_page_key=? "
                                   "AND json_extract(data_json,'$.type')='source'", (KEY,)).fetchall()
        assert len(source_rows) == 1
        source_node = json.loads(source_rows[0][0])
        page_index_projection.refresh_page_index_projection({'nodes': {KEY: source_node}}, node_keys=[KEY])
        assert conn.execute('SELECT domain FROM page_index_nodes WHERE node_key=?', (KEY,)).fetchone()[0] == DOMAIN
        protected = ['entities', 'claims', 'evidence', 'sources', 'claim_graph_edges', 'processed_files', 'operational_memory', 'page_index_nodes']
        before = {name: conn.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall() for name in protected}
        version = _canonical_entity_rows_version(conn.execute('SELECT entity_id,data_json FROM entities').fetchall())
        schema = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall()
        wiki = (root / 'wiki' / (KEY + '.md')).read_bytes()
        publication_index = {'declared_pages': {tool_ingest._raw_key(str(raw)): {'page': KEY + '.md'}}}
        raw_md5 = hashlib.md5(RAW, usedforsecurity=False).hexdigest()
        assert tool_ingest.current_source_publication_proven(str(raw), raw_md5, publication_index) is False
        target = admission.BindingTarget(KEY, DOMAIN, REFERENCE, hashlib.sha256(RAW).hexdigest())
        host = admission.SourceBindingAdmissionHost(root, CONFIG, (target,), owner_scope_sha256='b' * 64)
        prepared = host.prepare(KEY, DOMAIN)
        result = host.admit(prepared)
        assert tool_ingest.current_source_publication_proven(str(raw), raw_md5, publication_index) is False
        assert result['status'] == 'admitted'
        for name in protected:
            assert conn.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall() == before[name]
        assert _canonical_entity_rows_version(conn.execute('SELECT entity_id,data_json FROM entities').fetchall()) == version
        assert conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall() == schema
        assert (root / 'wiki' / (KEY + '.md')).read_bytes() == wiki
        verified = ReadOnlyEvidenceHost(root, CONFIG).get_query_evidence('written goals', [KEY], domain=DOMAIN)
        assert verified['evidence'][0]['status'] == 'verified'
        assert verified['evidence'][0]['binding_admission']['receipt_job_id'] == result['receipt']['receipt_job_id']
    finally:
        db_store.close_connection()
        wiki_utils.reset_memory_dir_cache()


def test_same_source_version_cannot_silently_rebind_different_raw(case):
    from dataclasses import replace
    prepared = case['host'].prepare(KEY, DOMAIN)
    case['host'].admit(prepared)
    case['raw'].write_bytes(RAW + b'different current bytes')
    target = replace(case['target'], raw_sha256=hashlib.sha256(case['raw'].read_bytes()).hexdigest())
    another = admission.SourceBindingAdmissionHost(case['root'], CONFIG, (target,), owner_scope_sha256='c' * 64)
    new_prepared = another.prepare(KEY, DOMAIN)
    before = snapshot(case)
    with pytest.raises(evidence.EvidenceRefused, match='immutable_admission_conflict'):
        another.admit(new_prepared)
    assert snapshot(case) == before
    assert query(case)['evidence'][0]['code'] == 'stale_source_binding'


def test_busy_existing_writer_fails_without_retry_or_control_mutation(case):
    prepared = case['host'].prepare(KEY, DOMAIN)
    before = snapshot(case)
    held = sqlite3.connect(case['db'], timeout=0, isolation_level=None)
    try:
        held.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            case['host'].admit(prepared)
    finally:
        held.rollback()
        held.close()
    assert snapshot(case) == before
    assert not list((case['root'] / 'wiki' / '.meta').glob('source-binding-before-*.json'))


def test_post_commit_raw_or_source_change_never_uses_stale_receipt(case):
    prepared = case['host'].prepare(KEY, DOMAIN)
    case['host'].admit(prepared)
    case['raw'].write_bytes(RAW + b'new bytes')
    assert query(case)['evidence'][0]['code'] == 'stale_source_binding'
    case['raw'].write_bytes(RAW)
    with sqlite3.connect(case['db']) as conn:
        conn.execute('UPDATE entities SET data_json=?', (json.dumps(dict(case['source'], title='New Source version')),))
    assert query(case)['evidence'][0]['code'] == 'binding_unproven'
