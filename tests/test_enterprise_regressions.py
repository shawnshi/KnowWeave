"""Enterprise audit regressions; only synthetic files, providers and task definitions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from scripts import install_core
from vector_lake import db_store, periodic_catch_up, projection_registry, tool_search, tool_sync, watchdog_app


@pytest.fixture
def install_root(tmp_path, monkeypatch):
    parent = tmp_path / 'site-packages'
    shim = parent / 'vector_lake_core'
    shim.mkdir(parents=True)
    (shim / '__init__.py').write_text('original loader\n', encoding='utf-8')
    (shim / install_core.POINTER).write_text('0.1.0\n', encoding='utf-8')
    for version in ('0.1.0', '0.2.0'):
        directory = parent / f"vector_lake_core_{version.replace('.', '_')}"
        directory.mkdir()
        (directory / install_core.NATIVE_FILENAME).write_bytes(b'synthetic extension, not executable')
    monkeypatch.setattr(install_core, 'site_packages', lambda: parent)
    return parent


def test_failed_activation_preserves_pointer_and_loader(install_root, monkeypatch):
    shim = install_root / 'vector_lake_core'
    before = {name: (shim / name).read_bytes() for name in ('__init__.py', install_core.POINTER)}
    monkeypatch.setattr(install_core.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 1, '', 'synthetic ABI failure'))
    with pytest.raises(SystemExit, match='activation probe failed'):
        install_core.activate('0.2.0')
    assert {name: (shim / name).read_bytes() for name in before} == before


def test_activation_probes_candidate_not_environment_override(install_root, monkeypatch):
    calls = []
    monkeypatch.setenv('VECTOR_LAKE_CORE_VERSION', '0.1.0')

    def probe(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, 'synthetic candidate validated', '')

    monkeypatch.setattr(install_core.subprocess, 'run', probe)
    install_core.activate('0.2.0')
    assert calls[0][1]['env']['VECTOR_LAKE_CORE_VERSION'] == '0.2.0'
    assert 'vector_lake_core_0_2_0.vector_lake_core' in calls[0][0][-1]
    assert (install_root / 'vector_lake_core' / install_core.POINTER).read_text() == '0.2.0\n'


def test_corrupt_candidate_is_rejected_by_a_fresh_process(install_root):
    pointer = install_root / 'vector_lake_core' / install_core.POINTER
    with pytest.raises(SystemExit, match='activation probe failed'):
        install_core.activate('0.2.0')
    assert pointer.read_text() == '0.1.0\n'


@pytest.mark.parametrize('mismatch', ['version', 'path'])
def test_optimized_probe_still_rejects_wrong_candidate(install_root, monkeypatch, mismatch):
    candidate = install_root / 'vector_lake_core_0_2_0' / install_core.NATIVE_FILENAME
    module_path = candidate if mismatch == 'version' else install_root / 'wrong.pyd'
    reported_version = '0.1.0' if mismatch == 'version' else '0.2.0'
    (install_root / 'sitecustomize.py').write_text(
        "import sys, types\n"
        "c = types.ModuleType('vector_lake_core_0_2_0.vector_lake_core')\n"
        f"c.__file__ = {str(module_path)!r}\n"
        f"c.version = lambda: {reported_version!r}\n"
        "sys.modules[c.__name__] = c\n", encoding='utf-8')
    monkeypatch.setenv('PYTHONPATH', str(install_root))
    monkeypatch.setenv('PYTHONOPTIMIZE', '1')
    pointer = install_root / 'vector_lake_core' / install_core.POINTER
    with pytest.raises(SystemExit, match='activation probe failed') as failure:
        install_core.activate('0.2.0')
    assert ('candidate version mismatch' if mismatch == 'version' else 'wrong candidate path') in str(failure.value)
    assert pointer.read_text() == '0.1.0\n'


def test_same_version_different_build_cannot_overwrite(install_root, tmp_path):
    original = install_root / 'vector_lake_core_0_2_0' / install_core.NATIVE_FILENAME
    before = original.read_bytes()
    candidate = tmp_path / 'candidate.dll'
    candidate.write_bytes(b'different build')
    with pytest.raises(SystemExit, match='different bytes'):
        install_core.install(candidate, '0.2.0')
    assert original.read_bytes() == before


def test_identical_build_is_idempotent_without_copy(install_root, tmp_path, monkeypatch):
    target = install_root / 'vector_lake_core_0_2_0' / install_core.NATIVE_FILENAME
    candidate = tmp_path / 'candidate.dll'
    candidate.write_bytes(target.read_bytes())
    monkeypatch.setattr(install_core.shutil, 'copy2', lambda *a, **kw:
                        pytest.fail('an existing immutable build must not be copied over'))
    assert install_core.install(candidate, '0.2.0') == target


def test_interrupted_install_does_not_publish_partial_build(install_root, tmp_path, monkeypatch):
    candidate = tmp_path / 'candidate.dll'
    candidate.write_bytes(b'candidate')

    def interrupted(source, destination):
        Path(destination).write_bytes(b'partial')
        raise OSError('synthetic copy interruption')

    monkeypatch.setattr(install_core.shutil, 'copy2', interrupted)
    with pytest.raises(OSError, match='copy interruption'):
        install_core.install(candidate, '0.3.0')
    assert not (install_root / 'vector_lake_core_0_3_0').exists()
    assert not list(install_root.glob('.install-core-*'))


def test_pointer_publish_failure_restores_loader(install_root, monkeypatch):
    shim = install_root / 'vector_lake_core'
    pointer = shim / install_core.POINTER
    original_loader = (shim / '__init__.py').read_bytes()
    monkeypatch.setattr(install_core.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 0, 'validated', ''))
    replace = os.replace

    def deny_pointer(source, target):
        if Path(target) == pointer:
            raise PermissionError('synthetic pointer publish failure')
        return replace(source, target)

    monkeypatch.setattr(os, 'replace', deny_pointer)
    with pytest.raises(PermissionError, match='pointer publish failure'):
        install_core.activate('0.2.0')
    assert pointer.read_text() == '0.1.0\n'
    assert (shim / '__init__.py').read_bytes() == original_loader


def test_vector_repair_passes_the_configured_batch(monkeypatch):
    calls = []
    monkeypatch.setenv('VECTOR_LAKE_CATCHUP_EMBEDDING_BATCH', '7')

    def catch_up(batch_size):
        calls.append(batch_size)
        return {'embedded': 0, 'candidates': 0, 'skipped': 'synthetic no provider'}

    monkeypatch.setattr(periodic_catch_up, 'embedding_catch_up', catch_up)
    assert 'skipped' in projection_registry._vector_repair()
    assert calls == [7]


def _node(conn, key):
    conn.execute("INSERT INTO page_index_nodes (node_key,node_id,title,type,status,domain,topic_cluster,node_json) "
                 "VALUES (?, ?, ?, 'concept', 'Active', 'General', 'x', '{}')", (key, key, key))


def _vector(conn, key):
    conn.execute('INSERT INTO vec_embeddings (page_key,embedding) VALUES (?,?)',
                 (key, struct.pack('3072f', *([0.0] * 3072))))
    conn.execute("INSERT INTO vec_embedding_inputs (node_key,content_digest,updated_at) "
                 "VALUES (?, 'digest', '2030-01-01T00:00:00+00:00')", (key,))


@pytest.mark.parametrize('with_nodes', [True, False])
def test_equal_counts_or_empty_store_do_not_hide_orphan_vectors(isolated_memory, with_nodes):
    db_store.init_db()
    conn = db_store.get_connection()
    with db_store.transaction():
        if with_nodes:
            _node(conn, 'Concept_One')
            _node(conn, 'Concept_Two')
            _vector(conn, 'Concept_One')
        _vector(conn, 'Concept_Orphan')
    state, detail = projection_registry._vector_health()
    assert state == projection_registry.DEGRADED
    assert '1 orphaned' in detail
    if with_nodes:
        assert '1 missing' in detail


def test_reconcile_does_not_report_an_unhealthy_repair_completed(monkeypatch):
    projection = projection_registry.Projection('synthetic', 'a', 'b',
        lambda: (projection_registry.DEGRADED, 'still missing'), lambda: 'skipped', 'repair')
    monkeypatch.setattr(projection_registry, 'PILOTS', (projection,))
    result = projection_registry.reconcile()['synthetic']
    assert result['action'] == 'failed'
    assert result['post_state'] == projection_registry.DEGRADED
    assert 'still missing' in result['error']


@pytest.mark.parametrize('as_xml', [False, True])
def test_bad_utf8_is_not_returned_as_original_evidence(isolated_memory, monkeypatch, as_xml):
    node = {'_key': 'Concept_Bad', 'title': 'Synthetic', 'summary': 'safe index summary'}
    monkeypatch.setattr(tool_search, '_search_scored_pages', lambda *a, **kw: ([(1.0, node)], [], None))
    path = isolated_memory / 'wiki' / 'Concept_Bad.md'
    original = b'# Synthetic\n\ncorrupt evidence: \xff\n'
    path.write_bytes(original)
    result = tool_search.search_vector_lake('evidence', as_xml=as_xml)
    assert '[DEGRADED]' in result and 'UnicodeDecodeError' in result
    assert 'index-summary' in result and 'safe index summary' in result
    assert '\ufffd' not in result
    assert path.read_bytes() == original


def test_bad_utf8_is_marked_in_query_context(isolated_memory, monkeypatch):
    from vector_lake import author_facet, page_index_projection, purpose_contract

    node = {'_key': 'Concept_Bad', 'title': 'Synthetic', 'summary': 'safe index summary'}
    monkeypatch.setattr(tool_search, '_search_scored_pages', lambda *a, **kw: ([(1.0, node)], [], None))
    monkeypatch.setattr(tool_search, 'build_memory_packet', lambda *a, **kw:
                        {'packet': '', 'warning_count': 0, 'memory_count': 0, 'omitted_count': 0})
    monkeypatch.setattr(page_index_projection, 'read_catalog', lambda: (None, None))
    monkeypatch.setattr(purpose_contract, 'render_strategy_directive', lambda: '')
    monkeypatch.setattr(author_facet, 'annotate_enabled', lambda: False)
    path = isolated_memory / 'wiki' / 'Concept_Bad.md'
    original = b'corrupt evidence: \xff\n'
    path.write_bytes(original)
    result = tool_search.assemble_context('evidence', max_chars=3000)
    assert 'index-summary' in result['wiki_context'] and '\ufffd' not in result['wiki_context']
    assert any('UnicodeDecodeError' in note for note in result['retrieval_notes'])
    assert result['budget_used'] <= result['budget_max']
    assert path.read_bytes() == original


def test_raw_event_burst_is_bounded_and_scans_the_last_batch(isolated_memory, monkeypatch):
    entered, release, caught_up = threading.Event(), threading.Event(), threading.Event()
    emitted = [1]
    snapshots = []

    def sync(**kwargs):
        snapshots.append(emitted[0])
        if not release.is_set():
            entered.set()
            assert release.wait(5)
        if len(snapshots) > 1:
            caught_up.set()

    monkeypatch.setattr(tool_sync, 'sync_vector_lake', sync)
    handler = watchdog_app.RawWatchdogHandler()
    handler.handle_event(SimpleNamespace(src_path=str(isolated_memory / 'raw' / 'source_0.md'), is_directory=False))
    try:
        assert entered.wait(3)
        for i in range(1, 2001):
            emitted[0] = i + 1
            handler.handle_event(SimpleNamespace(src_path=str(isolated_memory / 'raw' / f'source_{i}.md'), is_directory=False))
        assert handler.executor._work_queue.qsize() <= 1
        assert len(handler.last_triggered) <= 1024
        release.set()
        assert caught_up.wait(3)
    finally:
        release.set()
        handler.shutdown(wait=True)
    assert snapshots == [1, 2001]


def test_waiting_shutdown_drains_an_already_accepted_scan(isolated_memory, monkeypatch):
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def sync(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(tool_sync, 'sync_vector_lake', sync)
    handler = watchdog_app.RawWatchdogHandler()
    handler.handle_event(SimpleNamespace(src_path=str(isolated_memory / 'raw' / 'one.md'), is_directory=False))
    assert entered.wait(3)
    handler.handle_event(SimpleNamespace(src_path=str(isolated_memory / 'raw' / 'two.md'), is_directory=False))

    def stop():
        closing.set()
        handler.shutdown(wait=True)

    thread = threading.Thread(target=stop, daemon=True)
    thread.start()
    assert closing.wait(3)
    # Wait for admission to close, not for arbitrary wall-clock scheduling.
    import time
    deadline = time.monotonic() + 3
    while not handler._closing and time.monotonic() < deadline:
        time.sleep(.001)
    assert handler._closing
    release.set()
    thread.join(3)
    assert not thread.is_alive(), 'shutdown must not join while holding the worker lock'
    assert len(calls) == 2, 'the accepted follow-up scan must finish before waiting shutdown returns'


def test_raw_sync_failure_is_visible_and_next_event_can_recover(isolated_memory, monkeypatch, caplog):
    failed, recovered = threading.Event(), threading.Event()
    calls = []

    def sync(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            failed.set()
            raise RuntimeError('synthetic sync failure')
        recovered.set()

    monkeypatch.setattr(tool_sync, 'sync_vector_lake', sync)
    handler = watchdog_app.RawWatchdogHandler()
    try:
        handler.handle_event(SimpleNamespace(src_path=str(isolated_memory / 'raw' / 'one.md'), is_directory=False))
        assert failed.wait(3)
        handler.handle_event(SimpleNamespace(src_path=str(isolated_memory / 'raw' / 'two.md'), is_directory=False))
        assert recovered.wait(3)
    finally:
        handler.shutdown(wait=True)
    assert 'synthetic sync failure' in caplog.text


def test_raw_coalescing_drains_more_than_one_ingest_batch(isolated_memory, monkeypatch):
    from vector_lake import tool_ingest

    monkeypatch.setattr(tool_ingest, '_build_ingest_instructions', lambda *a, **kw: 'synthetic instructions')
    for i in range(51):
        (isolated_memory / 'raw' / f'source_{i}.md').write_text(f'synthetic source {i}', encoding='utf-8')
    db_store.init_db()
    original = tool_sync.sync_vector_lake
    drained = threading.Event()
    batches = []

    def sync(**kwargs):
        result = original(**kwargs)
        progress = kwargs['_progress']
        batches.append(dict(progress))
        if not progress['remaining']:
            drained.set()
        return result

    monkeypatch.setattr(tool_sync, 'sync_vector_lake', sync)
    handler = watchdog_app.RawWatchdogHandler()
    try:
        handler.handle_event(SimpleNamespace(src_path=str(isolated_memory / 'raw' / 'source_0.md'), is_directory=False))
        assert drained.wait(10), 'one event must also prepare sources beyond the 50-file batch'
    finally:
        handler.shutdown(wait=True)
    assert [batch['enqueued'] for batch in batches] == [50, 1]
    assert db_store.get_connection().execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 51


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Task Scheduler wrapper')
@pytest.mark.parametrize('reject_all', [True, False])
def test_task_update_never_unregisters_old_definition(tmp_path, reject_all):
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'register_watchdog_task.ps1'
    wrapper = tmp_path / 'watchdog_service.ps1'
    wrapper.write_text('# synthetic wrapper', encoding='utf-8')
    harness = tmp_path / 'task_harness.ps1'
    harness.write_text(r'''
param($Script, $Wrapper, $Root, [int]$RejectAll)
$ErrorActionPreference = 'Stop'
$env:USERDOMAIN='SYNTHETIC'; $env:USERNAME='Operator'
$global:enterprise_removed=0; $global:enterprise_forced=0; $global:enterprise_old=$true; $global:enterprise_reject=$RejectAll
function Get-ScheduledTask { param($TaskName,$ErrorAction) if($global:enterprise_old){
  [pscustomobject]@{TaskName=$TaskName; State='Ready'; Principal=[pscustomobject]@{LogonType='Interactive';RunLevel='Limited'};Triggers=@();Settings=[pscustomobject]@{RunOnlyIfIdle=$false;StopOnIdleEnd=$false;MultipleInstances='IgnoreNew';ExecutionTimeLimit='PT0S'}}
} }
function Unregister-ScheduledTask { param($TaskName,$Confirm) $global:enterprise_removed++; $global:enterprise_old=$false }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) [pscustomobject]@{Execute=$Execute} }
function New-ScheduledTaskTrigger { param([switch]$AtLogOn,$User,[switch]$AtStartup,[switch]$Once,$At,$RepetitionInterval,$RepetitionDuration) [pscustomobject]@{Repetition=[pscustomobject]@{StopAtDurationEnd=$true};CimClass=[pscustomobject]@{CimClassName='SyntheticTrigger'}} }
function New-ScheduledTaskSettingsSet { param($MultipleInstances,$ExecutionTimeLimit,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,[switch]$StartWhenAvailable,$RestartCount,$RestartInterval,$RunOnlyIfIdle,[switch]$DontStopOnIdleEnd) [pscustomobject]@{} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) [pscustomobject]@{LogonType=$LogonType;RunLevel=$RunLevel} }
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Settings,$Principal,$Description,[switch]$Force)
 if($Force){$global:enterprise_forced++}; if($global:enterprise_reject -or $Principal.LogonType -eq 'S4U'){throw 'synthetic registration denied'}
 $global:enterprise_old=$true
}
function Export-ScheduledTask { param($TaskName) '<SyntheticTask />' }
$failed=$false
try { & $Script -TaskName 'SyntheticTask' -Script $Wrapper -WorkDir $Root | Out-Null } catch { $failed=$true }
[pscustomobject]@{failed=$failed;oldExists=$global:enterprise_old;removed=$global:enterprise_removed;forced=$global:enterprise_forced} | ConvertTo-Json -Compress
''', encoding='utf-8')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(harness),
                             str(script), str(wrapper), str(tmp_path), str(int(reject_all))],
                            capture_output=True, text=True, encoding='utf-8', timeout=15, check=True)
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt['oldExists'] and receipt['removed'] == 0
    assert receipt['failed'] == reject_all
    assert receipt['forced'] == 2
