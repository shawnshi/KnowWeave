"""Capacity transitions must preserve exact-mode semantics and matrix bounds."""
from array import array
import math
import sqlite3

import pytest
import sqlite_vec
from vector_lake import two_stage_index as stage, ram_candidate_index as ram


def connection():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    return c


def unit(values):
    norm = math.hypot(*values)
    return [v / norm for v in values]


def test_busy_auto_keeps_exact_neighbor(monkeypatch):
    ram.clear()
    c = connection()
    c.execute('CREATE TABLE vec_embedding_projection(id INTEGER PRIMARY KEY,model TEXT,dimension INTEGER)')
    c.execute("INSERT INTO vec_embedding_projection VALUES (1,'synthetic',32)")
    c.execute('CREATE VIRTUAL TABLE vec_embeddings USING vec0(page_key TEXT PRIMARY KEY,embedding FLOAT[32])')
    query = unit([1.0] + [.0001] * 31)
    decoy = sqlite_vec.serialize_float32(unit([.0001] + [1.0] * 31))
    blob = sqlite_vec.serialize_float32(query)
    c.executemany('INSERT INTO vec_embeddings VALUES (?,?)', [(f'Source_{i:04d}', decoy) for i in range(600)])
    stage.build(c)
    c.commit()
    monkeypatch.setenv('VECTOR_LAKE_VECTOR_INDEX', 'two_stage')
    with stage.query_view(c):
        shortlist = set(stage.shortlist_keys(c, blob, 256))
    target = next(f'Source_{i:04d}' for i in range(600) if f'Source_{i:04d}' not in shortlist)
    c.execute('UPDATE vec_embeddings SET embedding=? WHERE page_key=?', (blob, target))
    c.execute('UPDATE vec_emb_float SET embedding=? WHERE page_key=?', (blob, target))
    c.commit()
    monkeypatch.setenv('VECTOR_LAKE_VECTOR_INDEX', 'auto')
    try:
        with stage.query_view(c) as view:
            normal = stage.search(c, blob, 5, state=view.state)
        for _ in range(ram.MAX_ACTIVE_VIEWS):
            assert ram._SLOTS.acquire(False)
        try:
            with stage.query_view(c) as view:
                assert view.backend == 'legacy'
                busy = stage.search(c, blob, 5, state=view.state)
        finally:
            for _ in range(ram.MAX_ACTIVE_VIEWS):
                ram._SLOTS.release()
        assert normal[0][0] == target and busy[0][0] == target
        assert normal[0][1] == pytest.approx(busy[0][1])
    finally:
        ram.clear()
        c.close()


def test_new_generation_never_allocates_while_old_lease_is_retained(monkeypatch):
    ram.clear()
    connections, leases = [], []
    loads = []
    original = ram._load
    def counted(*args):
        loads.append(1)
        return original(*args)
    monkeypatch.setattr(ram, '_load', counted)
    try:
        for i in range(2):
            c = sqlite3.connect(':memory:')
            c.execute('CREATE TABLE vec_emb_float(page_key TEXT,embedding BLOB)')
            c.execute('INSERT INTO vec_emb_float VALUES (?,?)', ('Source_Test', array('f', [1.0] + [0.0] * 31).tobytes()))
            c.commit()
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            connections.append(c)
            leases.append(ram.acquire(c, {'model': 'synthetic', 'dimension': 32, 'row_count': 1, 'written_at': str(i)}))
        assert leases[0] is not None and leases[1] is None
        assert len(loads) == 1
        assert leases[0].shortlist(array('f', [1.0] + [0.0] * 31).tobytes(), 1) == ['Source_Test']
    finally:
        for lease in leases:
            if lease is not None:
                lease.close()
        ram.clear()
        for c in connections:
            c.close()
