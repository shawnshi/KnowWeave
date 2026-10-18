"""Synthetic parser/index/scope tests; NOT proof of a live admission operation."""
import hashlib
import json
import sqlite3

import pytest

from vector_lake import source_binding_record as records


class Budget:
    def __init__(self, maximum=4 * 1024 * 1024):
        self.maximum = maximum
        self.used = 0

    def check(self):
        pass

    def allowance(self):
        return self.maximum - self.used

    def charge(self, size):
        self.used += size
        assert self.used <= self.maximum


SOURCE = {'type': 'source', 'domain': 'Artificial_Intelligence', 'status': 'Active',
          'raw_text': '\ufeff# Synthetic\r\nOnly an explicitly approved test source.\r\n'}
KEY = 'Source_synthetic'
DOMAIN = 'Artificial_Intelligence'


def fixture_record():
    fp = records.record_fingerprint(SOURCE)
    request = {'schema_version': 1, 'kind': records.KIND, 'node_key': KEY, 'domain': DOMAIN,
               'source_record_sha256': fp, 'raw_reference': 'raw/人工智能/test.md',
               'raw_sha256': 'a' * 64, 'owner_scope_sha256': 'b' * 64}
    result = dict(request, receipt_job_id=records.receipt_job_id(KEY, DOMAIN, fp),
                  admitted_at='2026-01-01T00:00:00+00:00', raw_size=42,
                  binding_scope=records.BINDING_SCOPE, recovery_sha256='c' * 64)
    return request, result


def make_db(request=None, result=None, **changes):
    request0, result0 = fixture_record()
    request = request0 if request is None else request
    result = result0 if result is None else result
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_type TEXT, payload TEXT, status TEXT, '
                 'lease_until TEXT, lease_owner TEXT, lease_token TEXT, lease_generation INTEGER, '
                 'completed_at TEXT,result_json TEXT)')
    values = {'job_id': result0['receipt_job_id'], 'task_type': records.KIND,
              'payload': json.dumps(request, ensure_ascii=False), 'status': 'completed',
              'lease_until': None, 'lease_owner': None, 'lease_token': None,
              'lease_generation': 1, 'completed_at': result0['admitted_at'],
              'result_json': json.dumps(result, ensure_ascii=False)}
    values.update(changes)
    conn.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(values.values()))
    return conn


def test_valid_exact_scoped_receipt_and_indexed_identity():
    request, expected = fixture_record()
    conn = make_db()
    budget = Budget()
    assert records.read_receipt(conn, KEY, DOMAIN, SOURCE, budget) == expected
    assert 0 < budget.used < records.MAX_RECEIPT_BYTES * 2
    plan = conn.execute('EXPLAIN QUERY PLAN SELECT result_json FROM jobs WHERE job_id=?',
                        (expected['receipt_job_id'],)).fetchall()
    assert any('INDEX' in row[3] for row in plan)
    assert records.record_fingerprint(SOURCE) == hashlib.sha256(
        json.dumps(SOURCE, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def test_absent_schema_and_absent_version_are_not_source_absence():
    assert records.read_receipt(sqlite3.connect(':memory:'), KEY, DOMAIN, SOURCE, Budget()) is None
    changed = dict(SOURCE, raw_text=SOURCE['raw_text'] + 'A changed canonical version.')
    assert records.read_receipt(make_db(), KEY, DOMAIN, changed, Budget()) is None
    assert records.read_receipt(make_db(), 'Source_other', DOMAIN, SOURCE, Budget()) is None


@pytest.mark.parametrize('field,value', [('task_type', 'ingest'), ('status', 'cancelled'),
    ('lease_token', 'not-a-completed-lease'), ('lease_owner', 'other'),
    ('lease_until', '2099-01-01T00:00:00+00:00'), ('lease_generation', 0)])
def test_generic_completion_or_live_lease_is_never_a_proof(field, value):
    with pytest.raises(records.BindingReceiptRefused, match='admission_receipt_inactive'):
        records.read_receipt(make_db(**{field: value}), KEY, DOMAIN, SOURCE, Budget())


@pytest.mark.parametrize('field,value', [('domain', 'Medical_IT'), ('node_key', 'Source_other'),
                                       ('source_record_sha256', 'd' * 64)])
def test_scope_filter_precedes_payload_transfer(field, value):
    request, result = fixture_record()
    request[field] = value
    with pytest.raises(records.BindingReceiptRefused, match='admission_receipt_invalid'):
        records.read_receipt(make_db(request, result), KEY, DOMAIN, SOURCE, Budget())


@pytest.mark.parametrize('field,value', [('schema_version', True), ('raw_size', True),
    ('raw_size', 512 * 1024 + 1), ('raw_sha256', 'not-a-digest'),
    ('owner_scope_sha256', ''), ('binding_scope', 'full_publication_verified')])
def test_receipt_fields_and_boolean_integers_are_not_authority(field, value):
    request, result = fixture_record()
    result[field] = value
    if field in request:
        request[field] = value
    with pytest.raises(records.BindingReceiptRefused, match='admission_receipt_invalid'):
        records.read_receipt(make_db(request, result), KEY, DOMAIN, SOURCE, Budget())


@pytest.mark.parametrize('malformed_result_version', [True, 1.0])
def test_result_only_schema_version_is_an_exact_integer(malformed_result_version):
    request, result = fixture_record()
    assert type(request['schema_version']) is int
    result['schema_version'] = malformed_result_version
    with pytest.raises(records.BindingReceiptRefused, match='admission_receipt_invalid'):
        records.read_receipt(make_db(request, result), KEY, DOMAIN, SOURCE, Budget())


def test_extra_authority_flags_duplicate_json_and_oversized_payload_refuse():
    request, result = fixture_record()
    result['verified'] = True
    with pytest.raises(records.BindingReceiptRefused, match='admission_receipt_invalid'):
        records.read_receipt(make_db(request, result), KEY, DOMAIN, SOURCE, Budget())
    _, result = fixture_record()
    raw = json.dumps(result)[:-1] + ',"kind":"source_binding_admission"}'
    with pytest.raises(records.BindingReceiptRefused, match='admission_receipt_invalid'):
        records.read_receipt(make_db(result_json=raw), KEY, DOMAIN, SOURCE, Budget())
    with pytest.raises(records.BindingReceiptRefused, match='admission_receipt_budget'):
        records.read_receipt(make_db(payload='x' * (records.MAX_RECEIPT_BYTES + 1)), KEY, DOMAIN, SOURCE, Budget())


def test_control_identity_cannot_be_a_composite_or_unindexed_key():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE jobs (job_id TEXT,task_type TEXT,payload TEXT,status TEXT,'
                 'lease_until TEXT,lease_owner TEXT,lease_token TEXT,lease_generation INTEGER,'
                 'completed_at TEXT,result_json TEXT,PRIMARY KEY(job_id,task_type))')
    assert records.control_schema_available(conn) is False
    assert records.read_receipt(conn, KEY, DOMAIN, SOURCE, Budget()) is None


def test_aggregate_budget_and_invalid_canonical_json_refuse_without_truncation():
    with pytest.raises(records.BindingReceiptRefused, match='aggregate_budget'):
        records.read_receipt(make_db(), KEY, DOMAIN, SOURCE, Budget(10))
    with pytest.raises(records.BindingReceiptRefused, match='binding_record_invalid'):
        records.record_fingerprint(dict(SOURCE, invalid=float('nan')))
