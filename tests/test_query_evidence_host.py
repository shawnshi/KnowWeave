"""Synthetic owned-host SQLite/Windows filesystem cases; never the actual vault."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
from threading import Event
import time
import types

import pytest

from tests.test_query_evidence import published
from vector_lake import db_store
from vector_lake import query_evidence as evidence
import vector_lake.query_evidence_host as adapter
from vector_lake import retrieval_runtime as runtime
from vector_lake import wiki_utils


CONFIG = {'exclude_paths': [], 'supported_extensions': ['.md', '.txt']}


def host(root, config=None):
    return adapter.ReadOnlyEvidenceHost(root, CONFIG if config is None else config)


def run(owner, keys=('Source_test',), **kw):
    return owner.get_query_evidence('agent harness', keys, domain='Artificial_Intelligence', **kw)


def test_dedicated_readonly_snapshot_never_borrows_or_mutates_writer(published, monkeypatch):
    root, writer, add = published
    path, _ = add()
    before = list(writer.iterdump())
    raw = path.read_bytes()
    opened = []
    connect = sqlite3.connect
    def capture(*args, **kwargs):
        assert args[0].endswith('?mode=ro&cache=private')
        conn = connect(*args, **kwargs)
        opened.append(conn)
        return conn
    monkeypatch.setattr(adapter.sqlite3, 'connect', capture)
    monkeypatch.setattr(db_store, 'get_connection', lambda: pytest.fail('borrowed writer'))
    writer.execute('BEGIN')
    result = run(host(root))
    assert result['status'] == 'complete'
    assert writer.in_transaction and writer.execute('PRAGMA query_only').fetchone()[0] == 0
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute('SELECT 1')
    writer.rollback()
    assert list(writer.iterdump()) == before and path.read_bytes() == raw


def test_host_root_and_exclusion_snapshot_ignore_env_and_config_drift(published, monkeypatch):
    root, _, add = published
    add(text='Agent harness authorized host root.')
    config = {'exclude_paths': [], 'supported_extensions': ['.md', '.txt']}
    owner = host(root, config)
    config['exclude_paths'].append('raw')
    config['supported_extensions'].clear()
    monkeypatch.setenv('VECTOR_LAKE_MEMORY_DIR', str(root.parent / 'different-vault'))
    monkeypatch.setenv('VECTOR_LAKE_DB_PATH', str(root.parent / 'different.db'))
    monkeypatch.setattr(wiki_utils, 'load_config', lambda: pytest.fail('ambient config'))
    monkeypatch.setattr(wiki_utils, 'get_memory_dir', lambda: pytest.fail('ambient root'))
    monkeypatch.setattr(runtime, 'request_scope', lambda *a: pytest.fail('implicit request scope'))
    result = run(owner)
    assert result['status'] == 'complete'
    assert any('authorized host root' in p['text'] for p in result['evidence'][0]['passages'])
    assert not (root.parent / 'different-vault').exists()


@pytest.mark.parametrize('kind', ['leaf', 'meta'])
def test_canonical_reparse_boundary_fails_before_sqlite_open(published, monkeypatch, kind):
    root, writer, add = published
    add()
    writer.close()
    meta = root / 'wiki' / '.meta'
    target = meta / 'vector_lake.db' if kind == 'leaf' else meta
    moved = root / ('moved.db' if kind == 'leaf' else 'moved-meta')
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=kind == 'meta')
    monkeypatch.setattr(adapter.sqlite3, 'connect', lambda *a, **k: pytest.fail('reparse DB open'))
    result = run(host(root))
    assert result['status'] == 'failed'
    assert result['evidence'][0]['code'] == 'source_reparse_or_path_change'


def test_canonical_hardlink_is_refused_before_sqlite_open(published, monkeypatch):
    root, writer, add = published
    add()
    writer.close()
    os.link(root / 'wiki' / '.meta' / 'vector_lake.db', root / 'db-alias')
    monkeypatch.setattr(adapter.sqlite3, 'connect', lambda *a, **k: pytest.fail('hardlink DB open'))
    result = run(host(root))
    assert result['evidence'][0]['code'] == 'regular_single_link_canonical_required'


def test_directory_and_leaf_cannot_swap_during_sqlite_open(published, monkeypatch):
    root, writer, add = published
    add()
    writer.close()
    meta = root / 'wiki' / '.meta'
    db = meta / 'vector_lake.db'
    decoy = root / 'decoy.db'
    decoy.write_bytes(b'not a database')
    connect = sqlite3.connect
    attempts = []
    def racing(*args, **kwargs):
        for change in (lambda: meta.rename(root / 'moved-meta'), lambda: os.replace(decoy, db)):
            with pytest.raises(OSError):
                change()
            attempts.append(1)
        return connect(*args, **kwargs)
    monkeypatch.setattr(adapter.sqlite3, 'connect', racing)
    assert run(host(root))['status'] == 'complete'
    assert len(attempts) == 2 and decoy.read_bytes() == b'not a database'


def test_missing_canonical_is_not_created(isolated_memory):
    root = isolated_memory
    result = run(host(root))
    assert result['status'] == 'failed'
    assert not (root / 'wiki' / '.meta').exists()
    assert result['evidence'][0]['status'] == 'error'


def test_empty_database_is_refused_without_schema_initialization(isolated_memory):
    meta = isolated_memory / 'wiki' / '.meta'
    meta.mkdir()
    db = meta / 'vector_lake.db'
    sqlite3.connect(db).close()
    before = db.read_bytes()
    result = run(host(isolated_memory))
    assert result['evidence'][0]['code'] == 'canonical_schema_unproven'
    assert db.read_bytes() == before


def test_canonical_view_is_not_treated_as_an_owned_table(published):
    root, writer, add = published
    add()
    writer.execute('DROP TABLE page_index_nodes')
    writer.execute("CREATE VIEW page_index_nodes AS SELECT 'Source_test' AS node_key")
    writer.commit()
    assert run(host(root))['evidence'][0]['code'] == 'canonical_schema_unproven'


def test_cancellation_before_setup_has_no_database_open(published, monkeypatch):
    root, _, add = published
    add()
    cancel = Event()
    cancel.set()
    monkeypatch.setattr(adapter.sqlite3, 'connect', lambda *a, **k: pytest.fail('cancelled setup'))
    result = run(host(root), cancellation=cancel)
    assert result['evidence'][0]['code'] == 'EvidenceCancelled'


def test_inherited_expired_deadline_has_no_database_open(published, monkeypatch):
    root, _, add = published
    add()
    monkeypatch.setattr(adapter.sqlite3, 'connect', lambda *a, **k: pytest.fail('expired setup'))
    with runtime.request_scope('agent harness') as state:
        state.deadline = time.monotonic() - 1
        result = run(host(root))
    assert result['evidence'][0]['code'] == 'RetrievalBudgetExceeded'


def test_mid_batch_cancellation_keeps_completed_evidence_and_closes_snapshot(published, monkeypatch):
    root, _, add = published
    add('Source_first')
    add('Source_second')
    add('Source_third')
    signal = Event()
    original = evidence._one
    def cancel_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        signal.set()
        return result
    monkeypatch.setattr(evidence, '_one', cancel_after_first)
    result = run(host(root), ('Source_first', 'Source_second', 'Source_third'), cancellation=signal)
    assert result['status'] == 'partial'
    assert result['evidence'][0]['status'] == 'verified'
    assert [n['code'] for n in result['evidence'][1:]] == ['EvidenceCancelled', 'EvidenceCancelled']


def test_sql_progress_cancellation_does_not_prevent_owned_cleanup(published):
    root, writer, add = published
    add()
    owner = host(root)
    signal = Event()
    with owner._snapshot(owner._context(signal)) as conn:
        signal.set()
        with pytest.raises(sqlite3.OperationalError) as failure:
            conn.execute('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<10000000) SELECT sum(n) FROM x').fetchone()
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    assert writer.execute('PRAGMA query_only').fetchone()[0] == 0


def test_wal_snapshot_is_pinned_before_source_reads_and_new_call_sees_new_generation(published):
    root, writer, add = published
    _, record = add()
    assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    owner = host(root)
    context = owner._context(None)
    with owner._snapshot(context) as conn:
        changed = {**record, 'status': 'Archived'}
        writer.execute('UPDATE entities SET data_json=?', (json.dumps(changed),))
        writer.commit()
        result = evidence.get_query_evidence('agent harness', ['Source_test'], domain='Artificial_Intelligence',
                                            canonical_connection=conn, _host_context=context)
        assert result['status'] == 'complete'
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('UPDATE entities SET data_json=data_json')
    assert run(owner)['evidence'][0]['code'] == 'not_available'


def test_inherited_byte_budget_is_not_reset_by_snapshot_bootstrap(published, monkeypatch):
    root, _, add = published
    add()
    monkeypatch.setattr(evidence, '_read_raw', lambda *a: pytest.fail('over-budget Raw'))
    with runtime.request_scope('agent harness') as state:
        state.source_bytes = runtime.MAX_TOTAL_SOURCE_BYTES - 20
        result = run(host(root))
        assert state.source_bytes == runtime.MAX_TOTAL_SOURCE_BYTES - 20
    assert result['evidence'][0]['code'] == 'canonical_read_budget'


def test_bootstrap_native_error_is_safe_and_pins_are_released(published, monkeypatch):
    root, writer, add = published
    add()
    writer.close()
    marker = 'SYNTHETIC_DB_EXCEPTION_CANARY'
    def failed_connect(*a, **kw):
        raise sqlite3.OperationalError(marker)
    monkeypatch.setattr(adapter.sqlite3, 'connect', failed_connect)
    result = run(host(root))
    assert result['evidence'][0]['code'] == 'OperationalError'
    assert marker not in json.dumps(result)
    meta = root / 'wiki' / '.meta'
    moved = root / 'released-meta'
    meta.rename(moved)
    moved.rename(meta)


def test_cleanup_error_preserves_evidence_and_emits_one_partial_terminal_record(published, monkeypatch):
    root, _, add = published
    add()
    original = adapter.ReadOnlyEvidenceHost._snapshot
    @contextmanager
    def failed_cleanup(self, context, *, cleanup_errors=None):
        with original(self, context, cleanup_errors=cleanup_errors) as conn:
            yield conn
        raise OSError(5, 'SYNTHETIC_CLEANUP_CANARY')
    monkeypatch.setattr(adapter.ReadOnlyEvidenceHost, '_snapshot', failed_cleanup)
    records = []
    monkeypatch.setattr(runtime, 'defer_record', lambda record: records.append(record))
    result = run(host(root))
    assert result['status'] == 'partial' and result['evidence'][0]['status'] == 'verified'
    assert result['host_errors'][0]['native_codes']['errno'] == 5
    assert 'SYNTHETIC_CLEANUP_CANARY' not in json.dumps(result)
    assert len(records) == 1 and records[0]['status'] == 'partial'


@pytest.mark.parametrize('cleanup', ['rollback', 'progress'])
def test_connection_close_is_attempted_even_when_prior_cleanup_fails(published, monkeypatch, cleanup):
    root, _, add = published
    add()
    connect = sqlite3.connect
    closed = []
    class Fault(sqlite3.Connection):
        def set_progress_handler(self, progress_handler, n=0):
            if cleanup == 'progress' and progress_handler is None:
                raise OSError(5, 'synthetic progress cleanup')
            return super().set_progress_handler(progress_handler, n)
        def rollback(self):
            if cleanup == 'rollback':
                raise OSError(5, 'synthetic rollback cleanup')
            return super().rollback()
        def close(self):
            closed.append(1)
            return super().close()
    monkeypatch.setattr(adapter.sqlite3, 'connect', lambda *a, **kw: connect(*a, factory=Fault, **kw))
    result = run(host(root))
    assert result['status'] == 'partial' and closed == [1]
    assert result['host_errors'][0]['native_codes']['errno'] == 5


def test_parallel_calls_use_independent_connections(published):
    root, _, add = published
    add()
    owner = host(root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(owner), range(2)))
    assert all(r['status'] == 'complete' for r in results)


def test_unsupported_platform_fails_before_source_configuration(monkeypatch):
    monkeypatch.setattr(adapter, 'os', types.SimpleNamespace(name='posix'))
    monkeypatch.setattr(evidence, '_source_configuration', lambda *a: pytest.fail('unsupported config'))
    with pytest.raises(evidence.EvidenceRefused, match='safe_read_capability_unavailable'):
        host(Path('C:/unused'))


def test_sensitive_node_and_invalid_cancellation_are_rejected_before_setup(published, monkeypatch):
    root, _, add = published
    add()
    owner = host(root)
    monkeypatch.setattr(adapter.sqlite3, 'connect', lambda *a, **k: pytest.fail('invalid-input setup'))
    with pytest.raises(ValueError, match='sensitive_source_identifier'):
        run(owner, ('Source_access_token=SYNTHETIC',))
    with pytest.raises(ValueError, match='host_cancellation_event_required'):
        run(owner, cancellation=True)


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_existing_auxiliary_reparse_is_refused_before_sqlite_open(published, monkeypatch, suffix):
    root, writer, add = published
    add()
    writer.close()
    outside = root.parent / 'outside-auxiliary'
    outside.write_bytes(b'synthetic auxiliary boundary canary')
    auxiliary = root / 'wiki' / '.meta' / ('vector_lake.db' + suffix)
    auxiliary.symlink_to(outside)
    monkeypatch.setattr(adapter.sqlite3, 'connect', lambda *a, **k: pytest.fail('unverified auxiliary SQLite open'))
    result = run(host(root))
    assert result['status'] == 'failed'
    assert result['evidence'][0]['code'] == 'source_reparse_or_path_change'
    assert outside.read_bytes() == b'synthetic auxiliary boundary canary'


@pytest.fixture
def compound_cleanup(monkeypatch):
    connect = sqlite3.connect
    attempts, opened = [], []
    def install(stages, unexpected=None):
        faults = {stage: OSError(index + 5, 'SYNTHETIC_CLEANUP_FAULT_CANARY')
                  for index, stage in enumerate(stages)}
        if unexpected is not None:
            faults[unexpected[0]] = unexpected[1]
        class Fault(sqlite3.Connection):
            def set_progress_handler(self, progress_handler, n=0):
                if progress_handler is None:
                    attempts.append('progress_handler_reset')
                    if 'progress_handler_reset' in faults:
                        raise faults['progress_handler_reset']
                return super().set_progress_handler(progress_handler, n)
            def rollback(self):
                attempts.append('rollback')
                if 'rollback' in faults:
                    raise faults['rollback']
                return super().rollback()
            def close(self):
                attempts.append('connection_close')
                super().close()
                if 'connection_close' in faults:
                    raise faults['connection_close']
        def factory(*args, **kwargs):
            conn = connect(*args, factory=Fault, **kwargs)
            opened.append(conn)
            return conn
        monkeypatch.setattr(adapter.sqlite3, 'connect', factory)
        return attempts, opened
    return install


@pytest.mark.parametrize('primary_type', [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize('stages', [('rollback',),
                                  ('progress_handler_reset', 'rollback', 'connection_close')])
def test_primary_exception_survives_compound_cleanup_faults(
        published, monkeypatch, compound_cleanup, primary_type, stages):
    root, _, add = published
    add()
    attempts, opened = compound_cleanup(stages)
    primary = primary_type('SYNTHETIC_PRIMARY_CANARY')
    def failed_reader(*args, **kwargs):
        raise primary
    monkeypatch.setattr(evidence, 'get_query_evidence', failed_reader)
    records = []
    monkeypatch.setattr(runtime, 'defer_record', lambda record: records.append(record))
    with pytest.raises(primary_type) as captured:
        run(host(root))
    assert captured.value is primary
    assert attempts == ['progress_handler_reset', 'rollback', 'connection_close']
    faults = getattr(primary, '_evidence_cleanup_errors')
    assert tuple(f['stage'] for f in faults) == stages
    assert 'SYNTHETIC_CLEANUP_FAULT_CANARY' not in json.dumps(faults)
    assert records == []
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute('SELECT 1')


def test_setup_refusal_is_not_replaced_by_cleanup_errors(published, compound_cleanup):
    root, writer, add = published
    add()
    writer.execute('DROP TABLE page_index_nodes')
    writer.commit()
    attempts, _ = compound_cleanup(('progress_handler_reset', 'rollback', 'connection_close'))
    result = run(host(root))
    assert result['status'] == 'failed'
    assert result['evidence'][0]['status'] == 'refused'
    assert result['evidence'][0]['code'] == 'canonical_schema_unproven'
    assert [f['stage'] for f in result['host_errors']] == attempts
    assert len(attempts) == 3 and 'SYNTHETIC_CLEANUP_FAULT_CANARY' not in json.dumps(result)


def test_all_cleanup_faults_are_retained_after_success(published, compound_cleanup, monkeypatch):
    root, _, add = published
    add()
    attempts, opened = compound_cleanup(('progress_handler_reset', 'rollback', 'connection_close'))
    records = []
    monkeypatch.setattr(runtime, 'defer_record', lambda record: records.append(record))
    result = run(host(root))
    assert result['status'] == 'partial' and result['evidence'][0]['status'] == 'verified'
    assert [f['stage'] for f in result['host_errors']] == attempts
    assert len(attempts) == 3 and len(records) == 1 and records[0]['status'] == 'partial'
    assert 'SYNTHETIC_CLEANUP_FAULT_CANARY' not in json.dumps(result)
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute('SELECT 1')


@pytest.mark.parametrize('primary_type', [sqlite3.OperationalError, evidence.EvidenceCancelled])
def test_handled_primary_classification_and_cleanup_faults_both_survive(
        published, compound_cleanup, monkeypatch, primary_type):
    root, _, add = published
    add()
    compound_cleanup(('rollback',))
    def failed_reader(*args, **kwargs):
        raise primary_type('SYNTHETIC_PRIMARY_CANARY')
    monkeypatch.setattr(evidence, 'get_query_evidence', failed_reader)
    result = run(host(root))
    assert result['evidence'][0]['code'] == primary_type.__name__
    assert result['host_errors'][0]['stage'] == 'rollback'
    assert 'SYNTHETIC_PRIMARY_CANARY' not in json.dumps(result)


@pytest.mark.parametrize('cleanup_type', [RuntimeError, KeyboardInterrupt])
def test_unexpected_cleanup_exception_keeps_propagating_after_all_attempts(
        published, compound_cleanup, monkeypatch, cleanup_type):
    root, _, add = published
    add()
    unexpected = cleanup_type('SYNTHETIC_UNEXPECTED_CLEANUP_CANARY')
    attempts, opened = compound_cleanup(('rollback',), ('progress_handler_reset', unexpected))
    records = []
    monkeypatch.setattr(runtime, 'defer_record', lambda record: records.append(record))
    with pytest.raises(cleanup_type) as captured:
        run(host(root))
    assert captured.value is unexpected
    assert attempts == ['progress_handler_reset', 'rollback', 'connection_close']
    assert [f['stage'] for f in getattr(unexpected, '_evidence_cleanup_errors')] == ['progress_handler_reset', 'rollback']
    assert records == []
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute('SELECT 1')


@pytest.fixture
def native_close_faults(monkeypatch):
    win_dll = ctypes.WinDLL
    close_fd = os.close
    final_path = evidence._windows_path
    created, native_closed, fd_closed = [], [], []
    source_fds = set()
    leaf_handles = set()
    target = {'suffix': None, 'fd': None, 'fd_fired': False, 'handle_fail': False, 'handle_fired': False}
    class Proxy:
        def __init__(self, kernel):
            self.kernel = kernel
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            def creating(*args):
                value = create(*args)
                if value is not None and value != ctypes.c_void_p(-1).value:
                    created.append(value)
                return value
            def closing(handle):
                value = close(handle)
                native_closed.append(handle)
                if target['handle_fail'] and not target['handle_fired']:
                    target['handle_fired'] = True
                    ctypes.set_last_error(5)
                    return 0
                return value
            self.CreateFileW = creating
            self.CloseHandle = closing
        def __getattr__(self, name):
            return getattr(self.kernel, name)
    def factory(*args, **kwargs):
        return Proxy(win_dll(*args, **kwargs))
    def locate(fd):
        import msvcrt
        value = final_path(fd)
        source_fds.add(fd)
        leaf_handles.add(msvcrt.get_osfhandle(fd))
        if target['suffix'] and value.endswith(target['suffix']):
            target['fd'] = fd
        return value
    def closing_fd(fd):
        if fd in source_fds:
            fd_closed.append(fd)
        close_fd(fd)
        if fd == target['fd'] and not target['fd_fired']:
            target['fd_fired'] = True
            raise OSError(5, 'SYNTHETIC_DESCRIPTOR_CLOSE_CANARY')
    monkeypatch.setattr(ctypes, 'WinDLL', factory)
    monkeypatch.setattr(evidence, '_windows_path', locate)
    monkeypatch.setattr(os, 'close', closing_fd)
    try:
        yield target, created, native_closed, fd_closed
    finally:
        # The red oracle intentionally exposes the old product's skipped closes.
        # Harness-only recovery runs after assertions and is not product evidence.
        kernel = win_dll('kernel32', use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        for handle in set(created) - set(native_closed) - leaf_handles:
            assert kernel.CloseHandle(handle), 'Synthetic harness could not release leaked ancestor'


@pytest.mark.parametrize('primary_type', [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize('resource', ['canonical', 'auxiliary'])
def test_descriptor_fault_preserves_primary_and_attempts_every_native_handle(
        published, monkeypatch, native_close_faults, primary_type, resource):
    root, _, add = published
    add()
    target, created, native_closed, fd_closed = native_close_faults
    if resource == 'auxiliary':
        (root / 'wiki' / '.meta' / 'vector_lake.db-journal').write_bytes(b'')
        target['suffix'] = 'vector_lake.db-journal'
    else:
        target['suffix'] = 'vector_lake.db'
    primary = primary_type('SYNTHETIC_PRIMARY_CANARY')
    def fail_reader(*a, **kw):
        raise primary
    monkeypatch.setattr(evidence, 'get_query_evidence', fail_reader)
    records = []
    monkeypatch.setattr(runtime, 'defer_record', lambda record: records.append(record))
    with pytest.raises(primary_type) as captured:
        run(host(root))
    assert captured.value is primary and target['fd_fired']
    assert len(native_closed) + len(fd_closed) == len(created)
    faults = getattr(primary, '_evidence_cleanup_errors')
    assert any(f['stage'] == 'source_fd_close' for f in faults)
    assert 'SYNTHETIC_DESCRIPTOR_CLOSE_CANARY' not in json.dumps(faults)
    assert records == []


def test_false_native_close_result_is_reported_and_cannot_remain_complete(
        published, monkeypatch, native_close_faults):
    root, _, add = published
    add()
    target, created, native_closed, fd_closed = native_close_faults
    target['handle_fail'] = True
    records = []
    monkeypatch.setattr(runtime, 'defer_record', lambda record: records.append(record))
    result = run(host(root))
    assert target['handle_fired'] and result['status'] == 'partial'
    assert len(native_closed) + len(fd_closed) == len(created)
    assert any(f['native_codes'].get('winerror') == 5 for f in result['host_errors'])
    assert len(records) == 1 and records[0]['status'] == 'partial'


@pytest.mark.parametrize('kind', ['os_error', 'sqlite_error'])
@pytest.mark.parametrize('primary_present', [False, True])
def test_diagnostic_properties_cannot_interrupt_cleanup_or_replace_primary(
        published, monkeypatch, compound_cleanup, kind, primary_present):
    root, _, add = published
    add()
    def forbidden_metadata(_self):
        raise RuntimeError('SYNTHETIC_DIAGNOSTIC_PROPERTY_CANARY')
    # Dynamic fault injection intentionally replaces native data descriptors;
    # do not declare a statically incompatible subclass field or suppress it.
    if kind == 'os_error':
        poison_type = type('PoisonOsError', (OSError,), {'errno': property(forbidden_metadata)})
        poison = poison_type(5, 'SYNTHETIC_CLEANUP_CANARY')
    else:
        poison_type = type('PoisonSqliteError', (sqlite3.OperationalError,),
                           {'sqlite_errorcode': property(forbidden_metadata)})
        poison = poison_type('SYNTHETIC_CLEANUP_CANARY')
        BaseException.__dict__['__dict__'].__get__(poison, BaseException)['sqlite_errorcode'] = 9
    attempts, opened = compound_cleanup(('rollback',), ('progress_handler_reset', poison))
    primary = RuntimeError('SYNTHETIC_PRIMARY_CANARY')
    if primary_present:
        def fail_reader(*a, **kw):
            raise primary
        monkeypatch.setattr(evidence, 'get_query_evidence', fail_reader)
        with pytest.raises(RuntimeError) as captured:
            run(host(root))
        assert captured.value is primary
        faults = getattr(primary, '_evidence_cleanup_errors')
    else:
        result = run(host(root))
        assert result['status'] == 'partial'
        faults = result['host_errors']
    assert attempts == ['progress_handler_reset', 'rollback', 'connection_close']
    assert len(faults) == 2 and 'SYNTHETIC_DIAGNOSTIC_PROPERTY_CANARY' not in json.dumps(faults)
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute('SELECT 1')


def test_readonly_diagnostic_descriptor_cannot_replace_primary(published, compound_cleanup, monkeypatch):
    root, _, add = published
    add()
    class LockedPrimary(RuntimeError):
        @property
        def _evidence_cleanup_errors(self):
            return 'read-only diagnostic property'
    primary = LockedPrimary('SYNTHETIC_PRIMARY_CANARY')
    attempts, opened = compound_cleanup(('rollback',))
    def fail_reader(*a, **kw):
        raise primary
    monkeypatch.setattr(evidence, 'get_query_evidence', fail_reader)
    with pytest.raises(LockedPrimary) as captured:
        run(host(root))
    assert captured.value is primary
    details = BaseException.__dict__['__dict__'].__get__(primary, BaseException)
    assert [f['stage'] for f in details['_evidence_cleanup_errors']] == ['rollback']
    assert attempts == ['progress_handler_reset', 'rollback', 'connection_close']
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute('SELECT 1')
