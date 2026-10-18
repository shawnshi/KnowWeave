"""Read-only durable current-admission receipts; no writer, config or DB factory.

The authoritative row lives in the existing canonical database's control ledger.
A digest/JSON flag is not permission: only the operator-owned admission controller
may create this job kind. A receipt binds ONE exact canonical Source record to a
Raw snapshot now; it says nothing about historical ownership or full publication.
"""
from __future__ import annotations

import hashlib
import json
import re

KIND = 'source_binding_admission'
SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 16 * 1024
BINDING_SCOPE = 'canonical_source_record_and_exact_raw_bytes_not_full_publication'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_BASE = {'schema_version', 'kind', 'node_key', 'domain', 'source_record_sha256',
         'raw_reference', 'raw_sha256', 'owner_scope_sha256'}
_RESULT = _BASE | {'receipt_job_id', 'admitted_at', 'raw_size', 'binding_scope', 'recovery_sha256'}
_REQUIRED_COLUMNS = {'job_id', 'task_type', 'payload', 'status', 'lease_until', 'lease_owner',
                     'lease_token', 'lease_generation', 'completed_at', 'result_json'}


class BindingReceiptRefused(ValueError):
    pass


def is_sha256(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise BindingReceiptRefused('binding_record_invalid') from exc


def record_fingerprint(source):
    if type(source) is not dict:
        raise BindingReceiptRefused('binding_record_invalid')
    return hashlib.sha256(canonical_bytes(source)).hexdigest()


def receipt_job_id(key, domain, fingerprint):
    if (type(key) is not str or not key or len(key) > 512 or type(domain) is not str
            or not domain or len(domain) > 128 or not is_sha256(fingerprint)):
        raise BindingReceiptRefused('binding_identity_invalid')
    return 'sbind_' + hashlib.sha256(canonical_bytes([SCHEMA_VERSION, key, domain, fingerprint])).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise BindingReceiptRefused('admission_receipt_invalid')
        result[key] = value
    return result


def _load(value):
    try:
        result = json.loads(value, object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(BindingReceiptRefused('admission_receipt_invalid')))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, BindingReceiptRefused):
            raise
        raise BindingReceiptRefused('admission_receipt_invalid') from exc
    if type(result) is not dict:
        raise BindingReceiptRefused('admission_receipt_invalid')
    return result


def control_schema_available(conn):
    row = conn.execute("SELECT upper(substr(ltrim(sql),1,12)) FROM sqlite_schema "
                       "WHERE name='jobs' AND type='table' LIMIT 1").fetchone()
    if row is None:
        return False
    if row[0] != 'CREATE TABLE':
        raise BindingReceiptRefused('admission_control_schema_unproven')
    rows = conn.execute("SELECT name,pk FROM pragma_table_info('jobs') "
                        "WHERE name IN ('job_id','task_type','payload','status','lease_until','lease_owner',"
                        "'lease_token','lease_generation','completed_at','result_json') LIMIT 11").fetchall()
    columns = {r[0]: r[1] for r in rows}
    primary = conn.execute("SELECT name,pk FROM pragma_table_info('jobs') WHERE pk>0 LIMIT 2").fetchall()
    return (set(columns) == _REQUIRED_COLUMNS
            and [tuple(r) for r in primary] == [('job_id', 1)])


def read_receipt(conn, key, domain, source, budget):
    """One indexed primary-key lookup, same Source/read snapshot and input budget.

    Absent control schema/row is explicit absence of this proof, not of a source.
    Corruption, inactive receipts and SQL errors are NOT normalized to absence.
    No unrelated job payload is transferred: identity/scope filters precede it.
    """
    budget.check()
    fingerprint = record_fingerprint(source)
    job_id = receipt_job_id(key, domain, fingerprint)
    if not control_schema_available(conn):
        return None
    budget.check()
    meta = conn.execute(
        'SELECT task_type,status,lease_until IS NULL,lease_owner IS NULL,lease_token IS NULL,'
        'lease_generation,completed_at,length(CAST(payload AS BLOB)),length(CAST(result_json AS BLOB)) '
        'FROM jobs WHERE job_id=? LIMIT 1', (job_id,)).fetchone()
    if meta is None:
        return None
    if (meta[0] != KIND or meta[1] != 'completed' or tuple(meta[2:5]) != (1, 1, 1)
            or type(meta[5]) is not int or meta[5] != 1 or type(meta[6]) is not str or not meta[6]):
        raise BindingReceiptRefused('admission_receipt_inactive')
    if any(type(size) is not int or not 0 < size <= MAX_RECEIPT_BYTES for size in meta[7:9]):
        raise BindingReceiptRefused('admission_receipt_budget')
    size = meta[7] + meta[8]
    if size > budget.allowance():
        raise BindingReceiptRefused('aggregate_budget')
    budget.check()
    # CASE guards malformed JSON before inspecting fields. Payload bytes only cross
    # the boundary after BOTH exact source-version/domain/key scope checks pass.
    row = conn.execute(
        "SELECT payload,result_json FROM jobs WHERE job_id=? AND task_type=? AND status='completed' "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.domain') END=? "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.node_key') END=? "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.source_record_sha256') END=? "
        "AND CASE WHEN json_valid(result_json) THEN json_extract(result_json,'$.domain') END=? "
        "AND CASE WHEN json_valid(result_json) THEN json_extract(result_json,'$.node_key') END=? "
        "AND CASE WHEN json_valid(result_json) THEN json_extract(result_json,'$.source_record_sha256') END=? "
        "AND length(CAST(payload AS BLOB))=? AND length(CAST(result_json AS BLOB))=? LIMIT 1",
        (job_id, KIND, domain, key, fingerprint, domain, key, fingerprint, meta[7], meta[8])).fetchone()
    if row is None:
        raise BindingReceiptRefused('admission_receipt_invalid')
    if (type(row[0]) is not str or type(row[1]) is not str
            or len(row[0].encode('utf-8')) != meta[7] or len(row[1].encode('utf-8')) != meta[8]):
        raise BindingReceiptRefused('admission_receipt_budget')
    budget.charge(size)
    request, result = _load(row[0]), _load(row[1])
    if (set(request) != _BASE or set(result) != _RESULT
            or any(request[k] != result[k] for k in _BASE)
            or type(request['schema_version']) is not int or request['schema_version'] != SCHEMA_VERSION
            or type(result['schema_version']) is not int or result['schema_version'] != SCHEMA_VERSION
            or request['kind'] != KIND or request['domain'] != domain or request['node_key'] != key
            or request['source_record_sha256'] != fingerprint
            or not is_sha256(request['raw_sha256']) or not is_sha256(request['owner_scope_sha256'])
            or type(request['raw_reference']) is not str or not request['raw_reference']
            or len(request['raw_reference']) > 4096 or result['receipt_job_id'] != job_id
            or result['admitted_at'] != meta[6] or result['binding_scope'] != BINDING_SCOPE
            or type(result['raw_size']) is not int or not 0 < result['raw_size'] <= 512 * 1024
            or not is_sha256(result['recovery_sha256'])):
        raise BindingReceiptRefused('admission_receipt_invalid')
    budget.check()
    return result
