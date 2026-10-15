import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from vector_lake import db_store, operational_status as ops, watchdog_status


def test_missing_database_does_not_create_directories(tmp_path):
    path = tmp_path / 'absent' / 'db.sqlite'
    before = set(tmp_path.rglob('*'))
    result = ops.collect_status(path)
    assert result['database']['state'] == 'unknown'
    assert set(tmp_path.rglob('*')) == before
    assert all(p['state'] == 'unknown' for p in result['projections'].values())


def test_old_schema_is_unknown_not_empty_or_healthy(tmp_path):
    path = tmp_path / 'old.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE untouched (id INTEGER)')
    conn.commit()
    conn.close()
    before = path.read_bytes()
    result = ops.collect_status(path)
    assert result['jobs']['state'] == result['outbox']['state'] == 'unknown'
    assert all(item['state'] == 'unknown' for item in result['projections'].values())
    assert path.read_bytes() == before


def test_no_init_repair_payload_or_private_status_reads(isolated_memory, monkeypatch):
    db_store.init_db()
    path = db_store.get_db_path()
    private = path.parent / 'runtime' / 'runner_status.json'
    private.parent.mkdir(exist_ok=True)
    private.write_text('secret-payload-command', encoding='utf-8')
    original = Path.open

    def guarded(self, *a, **kw):
        assert self.name not in {'runner_status.json', '.watchdog_status.json', 'index.json', 'config.json', '.env'}
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, 'open', guarded)
    monkeypatch.setattr(db_store, 'init_db', lambda: pytest.fail('ops must not initialize database'))
    result = ops.collect_status(path)
    serialized = json.dumps(result)
    assert 'secret-payload-command' not in serialized
    assert result['database']['state'] == 'healthy'
    assert set(result['projections']) == set(ops.PROJECTIONS)


def test_backlog_and_lease_counts_are_aggregate_only(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    now = datetime.now(timezone.utc)
    for job, until in [('one', now - timedelta(seconds=10)), ('two', now + timedelta(minutes=5))]:
        conn.execute("INSERT INTO jobs(job_id,task_type,payload,status,created_at,lease_until) VALUES (?,?,?,?,?,?)",
                     (job, 'ingest', '{"secret":"private-source"}', 'dispatched', (now - timedelta(minutes=3)).isoformat(), until.isoformat()))
    conn.execute("INSERT INTO jobs(job_id,task_type,payload,status,created_at) VALUES (?,?,?,?,?)",
                 ('three', 'ingest', 'secret', 'private-custom-status', now.isoformat()))
    conn.commit()
    report = ops.collect_status(db_store.get_db_path())
    jobs = report['jobs']
    assert jobs['backlog'] == 2, 'unrecognised rows are reported separately, not as waiting work'
    assert jobs['leases'] == {'active': 1, 'expired_or_invalid': 1}
    assert jobs['oldest_age_seconds'] >= 180
    assert jobs['counts']['other'] == 1
    assert jobs['unrecognized']['count'] == 1
    assert not any(secret in json.dumps(report) for secret in ('private-source', 'private-custom-status', 'secret'))


def test_long_abandoned_unrecognized_status_remains_unknown_not_retired_by_age(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    old = (datetime.now(timezone.utc) - timedelta(days=92)).isoformat()
    conn.execute("INSERT INTO jobs(job_id,task_type,payload,status,created_at) VALUES ('legacy','ingest','private','retired-legacy-state',?)", (old,))
    conn.commit()
    result = ops.collect_status(db_store.get_db_path())
    assert result['jobs']['unrecognized']['count'] == 1
    assert result['jobs']['unrecognized']['treated_as_legacy'] is False
    assert result['jobs']['state'] == 'unknown'
    # a recent unrecognised status is still a live fault signal
    conn.execute("INSERT INTO jobs(job_id,task_type,payload,status,created_at) VALUES ('new','ingest','private','brand-new-state',?)", (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    assert ops.collect_status(db_store.get_db_path())['jobs']['state'] == 'degraded'


def test_public_heartbeat_is_sanitized(tmp_path):
    ops.write_public_heartbeat(tmp_path, 'watchdog', state='private-secret-status', task_queue_size=4, index_queue_size=2)
    raw = (tmp_path / 'runtime/watchdog_ops.json').read_text()
    assert 'private-secret' not in raw
    assert ops._heartbeat(tmp_path / 'runtime/watchdog_ops.json', datetime.now(timezone.utc))['status'] == 'unknown'


@pytest.mark.parametrize('body', ['{"schema_version":1,"status":"idle","updated_at":"invalid","model_command":"secret"}', 'bad', 'x' * 9000])
def test_bad_public_heartbeat_is_unknown(tmp_path, body):
    path = tmp_path / 'heartbeat.json'
    path.write_text(body)
    result = ops._heartbeat(path, datetime.now(timezone.utc))
    assert result['state'] == 'unknown'
    assert 'secret' not in json.dumps(result)


def test_watchdog_publishes_public_sidecar_without_legacy_text(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog_status, 'get_status_file', lambda: tmp_path / '.watchdog_status.json')
    watchdog_status.write_status('processing', 1, 2, current_action='private-source', last_error='secret-token')
    public = (tmp_path / 'runtime/watchdog_ops.json').read_text()
    assert 'private-source' not in public and 'secret-token' not in public
    assert json.loads(public)['task_queue_size'] == 1


def test_cli_ops_does_not_load_tools_or_env(tmp_path):
    path = tmp_path / 'missing.db'
    code = f"""
import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname == 'vector_lake.tools': raise AssertionError('private tool/config imports')
sys.meta_path.insert(0,Deny())
from vector_lake import cli_app
cli_app._load_env=lambda: (_ for _ in ()).throw(AssertionError('dotenv read'))
sys.argv=['cli.py','ops-status','--db',{str(path)!r},'--json']
assert cli_app.main() == 1
"""
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'unknown' in result.stdout


@pytest.mark.parametrize('status', [[], {}])
def test_unhashable_public_heartbeat_is_unknown(tmp_path, status):
    path = tmp_path / 'runtime/watchdog_ops.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'schema_version': 1, 'status': status, 'updated_at': datetime.now(timezone.utc).isoformat()}))
    result = ops.collect_status(tmp_path / 'missing.db')
    assert result['runtime']['watchdog']['state'] == 'unknown'


@pytest.mark.parametrize('created', [None, 'not-a-date', '2099-01-01T00:00:00Z'])
def test_backlog_with_bad_time_cannot_be_healthy(isolated_memory, created):
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("INSERT INTO jobs(job_id,task_type,payload,status,created_at) VALUES ('invalid','ingest','private','queued',?)", (created,))
    conn.commit()
    result = ops.collect_status(db_store.get_db_path())
    assert result['jobs']['state'] != 'healthy'


def test_fake_jobs_view_cannot_read_private_underlying_table(tmp_path):
    path = tmp_path / 'view.db'
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE private_records (body TEXT); INSERT INTO private_records VALUES ('sensitive'); CREATE VIEW jobs AS SELECT body AS status,'2026-10-14' AS created_at,NULL AS lease_until FROM private_records;")
    conn.close()
    original = sqlite3.connect
    forbidden = []

    class Audited(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if 'FROM jobs GROUP BY' in sql:
                forbidden.append(sql)
            return super().execute(sql, parameters)

    from unittest.mock import patch
    with patch.object(ops.sqlite3, 'connect', lambda *a, **kw: original(*a, **kw, factory=Audited)):
        result = ops.collect_status(path)
    assert result['jobs']['state'] == 'unknown'
    assert not forbidden, 'view must be rejected before executing its private definition'


@pytest.mark.parametrize('link_kind', ['meta', 'runtime'])
def test_reader_and_writer_reject_directory_links(tmp_path, link_kind):
    outside = tmp_path / 'outside'
    outside.mkdir()
    metadata = tmp_path / 'meta'
    if link_kind == 'meta':
        metadata.symlink_to(outside, target_is_directory=True)
        (outside / 'runtime').mkdir()
    else:
        metadata.mkdir()
        (metadata / 'runtime').symlink_to(outside, target_is_directory=True)
    before = set(outside.rglob('*'))
    result = ops.collect_status(tmp_path / 'missing.db', meta_dir=metadata)
    assert result['runtime']['watchdog']['state'] == 'unknown'
    with pytest.raises((OSError, ValueError)):
        ops.write_public_heartbeat(metadata, 'watchdog', state='idle')
    assert set(outside.rglob('*')) == before


def test_held_windows_directory_rejects_ancestor_replacement(tmp_path):
    import os
    if os.name != 'nt':
        pytest.skip('Windows directory-handle oracle')
    from vector_lake import ops_files
    metadata = tmp_path / 'meta'
    runtime = metadata / 'runtime'
    runtime.mkdir(parents=True)
    with ops_files.directory(runtime):
        with pytest.raises(OSError):
            metadata.rename(tmp_path / 'changed')
        with pytest.raises(OSError):
            runtime.rename(metadata / 'changed')
    metadata.rename(tmp_path / 'changed')


def test_column_authorizer_rejects_payload_as_generated_status(tmp_path):
    path = tmp_path / 'generated.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE jobs(payload TEXT, status TEXT GENERATED ALWAYS AS (payload), created_at TEXT, lease_until TEXT)')
    conn.execute("INSERT INTO jobs(payload,created_at) VALUES ('private body','2026-10-14')")
    conn.commit()
    conn.close()
    result = ops.collect_status(path)
    assert result['jobs']['state'] == 'unknown'
    assert 'private body' not in json.dumps(result)


def test_authorizer_precedes_virtual_schema_configuration_reads(tmp_path):
    from unittest.mock import patch
    path = tmp_path / 'external.db'
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE private_records(id INTEGER PRIMARY KEY,status TEXT,created_at TEXT,lease_until TEXT); INSERT INTO private_records VALUES(1,'synthetic-private','2026-10-14',NULL); CREATE VIRTUAL TABLE jobs USING fts5(status,created_at,lease_until,content='private_records',content_rowid='id'); INSERT INTO jobs(jobs) VALUES('rebuild');")
    original = sqlite3.connect
    protected = [False]
    unsafe = []
    denied = []

    class Audited(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.set_trace_callback(lambda sql: unsafe.append(sql) if not protected[0] and 'jobs_config' in sql else None)

        def set_authorizer(self, authorizer_callback):
            protected[0] = authorizer_callback is not None
            if authorizer_callback is None:
                return super().set_authorizer(None)
            def wrapped(*args):
                decision = authorizer_callback(*args)
                if args[0] == sqlite3.SQLITE_READ and args[1] == 'jobs_config':
                    denied.append(decision)
                return decision
            return super().set_authorizer(wrapped)

    with patch.object(ops.sqlite3, 'connect', lambda *a, **kw: original(*a, **kw, factory=Audited)):
        result = ops.collect_status(path)
    assert not unsafe, 'virtual schema inspection must not read shadow config before authorization'
    assert denied and all(value == sqlite3.SQLITE_DENY for value in denied)
    assert result['jobs']['state'] == 'unknown'
    assert 'synthetic-private' not in json.dumps(result)


def test_locked_database_respects_wait_budget(tmp_path):
    path = tmp_path / 'locked.db'
    writer = sqlite3.connect(path)
    writer.execute('CREATE TABLE marker (id INTEGER)')
    writer.commit()
    writer.execute('BEGIN EXCLUSIVE')
    try:
        result = ops.collect_status(path, budget_seconds=.1)
        assert result['database']['state'] == 'unknown'
        assert result['elapsed_seconds'] < .6
    finally:
        writer.rollback()
        writer.close()


def test_sql_deadline_and_independent_sections(isolated_memory):
    db_store.init_db()
    result = ops.collect_status(db_store.get_db_path(), budget_seconds=.00001)
    assert result['budget_exhausted']
    assert result['state'] != 'healthy'


def test_current_wal_commit_is_observed_not_immutable_snapshot(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    conn.execute("INSERT INTO jobs(job_id,task_type,payload,status,created_at) VALUES ('wal','ingest','private','queued','2026-10-14T00:00:00Z')")
    conn.commit()
    result = ops.collect_status(db_store.get_db_path())
    assert result['jobs']['counts']['queued'] == 1
    assert 'private' not in json.dumps(result)


def test_canonical_schema_and_data_unchanged(isolated_memory):
    db_store.init_db()
    conn = db_store.get_connection()
    before = list(conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
    version = conn.execute('PRAGMA data_version').fetchone()[0]
    ops.collect_status(db_store.get_db_path())
    assert [tuple(r) for r in conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name')] == [tuple(r) for r in before]
    assert conn.execute('PRAGMA data_version').fetchone()[0] == version
