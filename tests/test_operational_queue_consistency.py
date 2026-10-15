"""Queue metadata must use one recognised work set; age is not status semantics."""
from datetime import datetime,timedelta,timezone
import sqlite3
import pytest
from vector_lake import operational_status as ops

@pytest.fixture
def queue_db(tmp_path):
    path=tmp_path/'queues.db'
    with sqlite3.connect(path) as conn:
        for table in ('jobs','mutation_outbox'):
            conn.execute(f'CREATE TABLE {table}(status TEXT, created_at TEXT, lease_until TEXT)')
    return path

def insert(path,*rows):
    with sqlite3.connect(path) as conn:
        conn.executemany('INSERT INTO jobs VALUES (?,?,?)',rows)

@pytest.mark.parametrize('other_time',[None,'bad-time','2020-01-01T00:00:00Z'])
def test_unrecognized_rows_cannot_supply_recognised_backlog_age(queue_db,other_time):
    now=datetime.now(timezone.utc)
    insert(queue_db,('queued',(now-timedelta(seconds=60)).isoformat(),None),('unknown-status',other_time,None))
    jobs=ops.collect_status(queue_db)['jobs']
    assert jobs['backlog']==1
    assert 58<=jobs['oldest_age_seconds']<=63
    assert jobs['unrecognized']['count']==jobs['counts']['other']==1
    assert jobs['unrecognized']['treated_as_legacy'] is False

@pytest.mark.parametrize('created',[None,'bad-time','2020-01-01T00:00:00Z'])
def test_null_status_is_not_lost_by_sql_not_in(queue_db,created):
    insert(queue_db,(None,created,None))
    jobs=ops.collect_status(queue_db)['jobs']
    assert jobs['backlog']==0 and jobs['oldest_age_seconds'] is None
    assert jobs['unrecognized']['count']==jobs['counts']['other']==1
    assert jobs['state']!='healthy'

def test_superseded_is_a_source_defined_terminal_not_age_inferred(queue_db):
    insert(queue_db,('superseded',None,None))
    jobs=ops.collect_status(queue_db)['jobs']
    assert jobs['state']=='healthy' and jobs['backlog']==0
    assert jobs['counts']['superseded']==1 and jobs['unrecognized']['count']==0
    assert jobs['oldest_age_seconds'] is None

def test_subagent_processing_is_recognised_leased_work(queue_db):
    now=datetime.now(timezone.utc)
    insert(queue_db,('subagent_processing',(now-timedelta(seconds=90)).isoformat(),(now+timedelta(minutes=5)).isoformat()))
    jobs=ops.collect_status(queue_db)['jobs']
    assert jobs['backlog']==1 and jobs['leases']['active']==1
    assert jobs['unrecognized']['count']==0

def test_oldest_uses_instant_not_iso_string_sorting(queue_db):
    now=datetime.now(timezone.utc)
    earlier=(now-timedelta(seconds=120)).astimezone(timezone(timedelta(hours=8))).isoformat()
    later=(now-timedelta(seconds=60)).isoformat()
    insert(queue_db,('queued',earlier,None),('queued',later,None))
    jobs=ops.collect_status(queue_db)['jobs']
    assert jobs['backlog']==2 and 118<=jobs['oldest_age_seconds']<=123

@pytest.mark.parametrize('seconds',[0.000001,1,4,5,6])
def test_near_future_queue_time_is_not_healthy(queue_db,monkeypatch,seconds):
    now=datetime(2026,10,15,tzinfo=timezone.utc)
    monkeypatch.setattr(ops,'_utc',lambda:now)
    insert(queue_db,('queued',(now+timedelta(seconds=seconds)).isoformat(),None))
    jobs=ops.collect_status(queue_db)['jobs']
    assert jobs['backlog']==1 and jobs['oldest_age_seconds'] is None
    assert jobs['state']=='unknown' and jobs['reason']=='backlog_age_unavailable'

def test_heartbeat_keeps_its_separate_clock_skew_tolerance(queue_db,monkeypatch):
    now=datetime(2026,10,15,tzinfo=timezone.utc)
    with monkeypatch.context() as future_clock:
        future_clock.setattr(ops,'_utc',lambda:now+timedelta(seconds=4))
        ops.write_public_heartbeat(queue_db.parent,'watchdog',state='idle',task_queue_size=0,index_queue_size=0)
    heartbeat=ops._heartbeat(queue_db.parent/'runtime/watchdog_ops.json',now)
    assert heartbeat['state']=='healthy' and heartbeat['age_seconds']==0
