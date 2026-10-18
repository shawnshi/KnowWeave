"""Operator-owned current Source-version/Raw binding admission; never a Wiki writer.

No MCP constructor/permission flags. A trusted host must supply the explicit
approved targets and owner-request digest. Those Python objects are not an OS
security boundary. Only typed control job rows and new private recovery artifacts
are written. Source records, raw_text, Wiki, publication/processed ledgers and all
knowledge tables remain untouched; no schema initialization or outbox publishing.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

from vector_lake import query_evidence as evidence
from vector_lake.query_evidence_host import ReadOnlyEvidenceHost, _DB_PARTS
from vector_lake import source_binding_record as records


@dataclass(frozen=True)
class BindingTarget:
    node_key: str
    domain: str
    raw_reference: str
    raw_sha256: str
    cluster: str | None = None


@dataclass(frozen=True)
class PreparedAdmission:
    target: BindingTarget
    source_record_sha256: str
    owner_scope_sha256: str


def _now():
    return datetime.now(timezone.utc)


class SourceBindingAdmissionHost:
    """Synchronous, bounded capability constructed only by the authorized operator.

    Actual scope approval stays upstream; a digest alone does not authorize a
    write. Deterministic control identities make one admission per exact Source
    record version immutable. A different Raw mapping or a revoked slot refuses;
    there is no implicit rebind, legacy overwrite or automatic model dispatch.
    """

    def __init__(self, memory_root, config, targets, *, owner_scope_sha256):
        if not records.is_sha256(owner_scope_sha256):
            raise evidence.EvidenceRefused('owner_scope_digest_required')
        if type(targets) is not tuple or not 1 <= len(targets) <= 8:
            raise evidence.EvidenceRefused('bounded_approved_targets_required')
        self._reader = ReadOnlyEvidenceHost(memory_root, config)
        self._owner_scope_sha256 = owner_scope_sha256
        approved = {}
        for target in targets:
            if type(target) is not BindingTarget or not records.is_sha256(target.raw_sha256):
                raise evidence.EvidenceRefused('approved_target_invalid')
            domain, keys = evidence._validated_input('binding admission', [target.node_key],
                                                     target.domain, target.cluster)
            if domain != target.domain or keys != (target.node_key,):
                raise evidence.EvidenceRefused('canonical_target_required')
            evidence._raw_reference(target.raw_reference, domain, self._reader._root,
                                    self._reader._context(None).configuration())
            identity = (domain, target.node_key)
            if identity in approved:
                raise evidence.EvidenceRefused('duplicate_approved_target')
            approved[identity] = target
        self._targets = approved

    def _target(self, key, domain):
        target = self._targets.get((domain, key))
        if target is None:
            raise evidence.EvidenceRefused('target_not_in_approved_scope')
        return target

    @staticmethod
    def _source(conn, context, target, budget):
        source = evidence._eligible_source_record(conn, target.node_key, target.domain,
                                                   target.cluster, budget)
        if source.get('source_hash') is not None or source.get('source_frontmatter') is not None:
            raise evidence.EvidenceRefused('existing_legacy_binding_not_admissible')
        refs = source.get('sources')
        if refs is not None and (type(refs) is not list or len(refs) > 1):
            raise evidence.EvidenceRefused('source_reference_ambiguous')
        parts, relative = evidence._raw_reference(target.raw_reference, target.domain,
                                                context.root, context.configuration())
        if refs:
            _, existing = evidence._raw_reference(refs[0], target.domain, context.root,
                                                 context.configuration())
            if existing != relative:
                raise evidence.EvidenceRefused('approved_mapping_conflicts_with_canonical_reference')
        return source, parts, relative

    @staticmethod
    def _raw(context, parts, target, budget):
        raw = evidence._read_raw(context.root, parts, budget)
        if not raw:
            raise evidence.EvidenceRefused('raw_snapshot_empty')
        raw.decode('utf-8')  # This pilot cannot admit an unreadable text/PDF representation.
        if hashlib.sha256(raw).hexdigest() != target.raw_sha256:
            raise evidence.EvidenceRefused('raw_snapshot_changed')
        return raw

    def prepare(self, node_key, domain):
        """Read-only current record CAS and approved Raw check; creates no job/backup."""
        target = self._target(node_key, domain)
        errors = []
        context = self._reader._context(None, errors)
        budget = evidence._Budget(context)
        with self._reader._snapshot(context, cleanup_errors=errors) as conn:
            source, parts, _ = self._source(conn, context, target, budget)
            self._raw(context, parts, target, budget)
            prepared = PreparedAdmission(target, records.record_fingerprint(source), self._owner_scope_sha256)
        if errors:
            raise evidence._ResourceCleanupError(errors)
        return prepared

    def _prepared(self, prepared):
        if (type(prepared) is not PreparedAdmission
                or self._target(prepared.target.node_key, prepared.target.domain) != prepared.target
                or prepared.owner_scope_sha256 != self._owner_scope_sha256
                or not records.is_sha256(prepared.source_record_sha256)):
            raise evidence.EvidenceRefused('prepared_admission_scope_mismatch')
        return prepared.target

    @contextmanager
    def _transaction(self, context, errors):
        """Owned rw-existing connection, fixed/pinned location, zero init/extensions/retry."""
        with evidence._opened_source(context.root, _DB_PARTS, cleanup_errors=errors) as fd, ExitStack() as stack:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise evidence.EvidenceRefused('regular_single_link_canonical_required')
            pinned_name = evidence._windows_path(fd)
            auxiliaries = self._reader._pin_existing_auxiliaries(context, stack, errors)
            conn = sqlite3.connect(context.root.joinpath(*_DB_PARTS).as_uri() + '?mode=rw&cache=private',
                                   uri=True, timeout=0, isolation_level=None, cached_statements=32)
            primary = None
            try:
                conn.execute('PRAGMA trusted_schema=OFF')
                conn.execute('PRAGMA foreign_keys=ON')
                conn.set_progress_handler(lambda: 1 if time.monotonic() >= context.deadline else 0, 1000)
                locations = conn.execute('PRAGMA database_list').fetchall()
                if (len(locations) != 1 or locations[0][1] != 'main'
                        or os.path.normcase(os.path.normpath(locations[0][2])) != pinned_name):
                    raise evidence.EvidenceRefused('canonical_location_unproven')
                conn.execute('BEGIN IMMEDIATE')
                if not records.control_schema_available(conn):
                    raise evidence.EvidenceRefused('admission_control_schema_unavailable')
                schema = conn.execute("SELECT name,type,upper(substr(ltrim(sql),1,12)) FROM sqlite_schema "
                    "WHERE name IN ('entities','page_index_nodes') LIMIT 3").fetchall()
                if ({r[0] for r in schema} != {'entities', 'page_index_nodes'}
                        or any(r[1:] != ('table', 'CREATE TABLE') for r in schema)):
                    raise evidence.EvidenceRefused('canonical_schema_unproven')
                columns = {r[0] for r in conn.execute("SELECT name FROM pragma_table_xinfo('jobs') WHERE hidden=0 LIMIT 32")}
                if not {'created_at', 'updated_at', 'available_at', 'retries', 'error_msg'} <= columns:
                    raise evidence.EvidenceRefused('admission_control_schema_unavailable')
                if conn.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name='jobs' LIMIT 1").fetchone():
                    raise evidence.EvidenceRefused('admission_job_triggers_unsupported')
                if conn.execute("PRAGMA foreign_key_list('jobs')").fetchone():
                    raise evidence.EvidenceRefused('admission_job_foreign_keys_unsupported')
                context.checkpoint()
                yield conn
                context.checkpoint()
                conn.commit()
            except BaseException as exc:
                primary = exc
                raise
            finally:
                evidence._cleanup_resources((('progress_handler_reset', lambda: conn.set_progress_handler(None, 0)),
                    ('rollback', lambda: conn.rollback()), ('connection_close', lambda: conn.close())), primary, errors)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, 1) != (after.st_dev, after.st_ino, after.st_nlink):
                raise evidence.EvidenceRefused('canonical_identity_changed')
            for auxiliary, device, inode in auxiliaries:
                current = os.fstat(auxiliary)
                if (device, inode, 1) != (current.st_dev, current.st_ino, current.st_nlink):
                    raise evidence.EvidenceRefused('canonical_auxiliary_identity_changed')

    @staticmethod
    def _recovery(context, job_id, operation, before, errors):
        """Exclusive new leaf under the ALREADY PINNED .meta parent; no data dump.

        Contains only this typed control row (or its absence), never Source bodies
        or a whole DB. Hash checking/explicit compensation replaces blind restore.
        """
        context.checkpoint()
        raw = records.canonical_bytes({'schema_version': 1, 'kind': records.KIND, 'job_id': job_id,
                                       'operation': operation, 'before': before})
        if len(raw) > 64 * 1024:
            raise evidence.EvidenceRefused('recovery_record_budget')
        path = context.root / 'wiki' / '.meta' / ('source-binding-before-' + secrets.token_hex(16) + '.json')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
                     | getattr(os, 'O_NOINHERIT', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        primary = None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise evidence.EvidenceRefused('regular_single_link_recovery_required')
            if evidence._windows_path(fd) != os.path.normcase(os.path.normpath(str(path))):
                raise evidence.EvidenceRefused('recovery_location_unproven')
            pending = memoryview(raw)
            while pending:
                context.checkpoint()
                count = os.write(fd, pending)
                if count <= 0:
                    raise OSError('Short admission recovery write')
                pending = pending[count:]
            os.fsync(fd)
            if os.fstat(fd).st_nlink != 1:
                raise evidence.EvidenceRefused('recovery_identity_changed')
        except BaseException as exc:
            primary = exc
            raise
        finally:
            evidence._cleanup_resources((('recovery_fd_close', lambda: os.close(fd)),), primary, errors)
        if errors:
            raise evidence._ResourceCleanupError(errors)
        return str(path), hashlib.sha256(raw).hexdigest()

    def admit(self, prepared):
        """One real lease + immutable receipt, same transaction as current-version CAS.

        Cleanup errors after commit are surfaced, not claimed to roll back a commit.
        Inspect deterministic job id/readback before any recovery; do not blindly retry.
        """
        target = self._prepared(prepared)
        errors = []
        context = self._reader._context(None, errors)
        budget = evidence._Budget(context)
        recovery = None
        with self._transaction(context, errors) as conn:
            source, parts, relative = self._source(conn, context, target, budget)
            fingerprint = records.record_fingerprint(source)
            if fingerprint != prepared.source_record_sha256:
                raise evidence.EvidenceRefused('canonical_source_version_changed')
            raw = self._raw(context, parts, target, budget)
            job_id = records.receipt_job_id(target.node_key, target.domain, fingerprint)
            existing = records.read_receipt(conn, target.node_key, target.domain, source, budget)
            if existing is not None:
                if existing['raw_reference'] != relative or existing['raw_sha256'] != target.raw_sha256:
                    raise evidence.EvidenceRefused('immutable_admission_conflict')
                result = existing
                status = 'already_admitted'
            else:
                # Re-read, do not mistake a pinned READ-sharing descriptor for exclusion.
                self._raw(context, parts, target, budget)
                recovery, recovery_sha = self._recovery(context, job_id, 'admit', None, errors)
                now = _now()
                stamp = now.isoformat()
                request = {'schema_version': 1, 'kind': records.KIND, 'node_key': target.node_key,
                           'domain': target.domain, 'source_record_sha256': fingerprint,
                           'raw_reference': relative, 'raw_sha256': target.raw_sha256,
                           'owner_scope_sha256': self._owner_scope_sha256}
                token = secrets.token_hex(32)
                conn.execute('INSERT INTO jobs (job_id,task_type,payload,status,retries,error_msg,created_at,'
                    'updated_at,available_at,lease_until,lease_owner,lease_token,lease_generation) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (job_id, records.KIND,
                    records.canonical_bytes(request).decode(), 'running', 0, '', stamp, stamp, stamp,
                    (now + timedelta(seconds=15)).isoformat(), 'source-binding-admission-controller', token, 1))
                completed = _now().isoformat()
                result = dict(request, receipt_job_id=job_id, admitted_at=completed, raw_size=len(raw),
                              binding_scope=records.BINDING_SCOPE, recovery_sha256=recovery_sha)
                changed = conn.execute("UPDATE jobs SET status='completed',result_json=?,completed_at=?,updated_at=?,"
                    'lease_until=NULL,lease_owner=NULL,lease_token=NULL WHERE job_id=? AND task_type=? '
                    "AND status='running' AND lease_token=? AND lease_generation=1 AND lease_until>?",
                    (records.canonical_bytes(result).decode(), completed, completed,
                     job_id, records.KIND, token, completed)).rowcount
                if changed != 1:
                    raise evidence.EvidenceRefused('admission_lease_lost')
                status = 'admitted'
        if errors:
            raise evidence._ResourceCleanupError(errors)
        return {'status': status, 'receipt': result, 'recovery_file': recovery,
                'knowledge_tables_changed': False, 'full_publication_proven': False}

    def revoke(self, prepared, *, expected_receipt_sha256):
        """Explicit scoped compensation; preserve history, never restore whole DB/files.

        A revoked version cannot silently re-admit. Correcting/reopening that slot
        requires a separately specified operator action/new canonical version.
        """
        target = self._prepared(prepared)
        if not records.is_sha256(expected_receipt_sha256):
            raise evidence.EvidenceRefused('receipt_cas_required')
        errors = []
        context = self._reader._context(None, errors)
        budget = evidence._Budget(context)
        with self._transaction(context, errors) as conn:
            source, _, _ = self._source(conn, context, target, budget)
            if records.record_fingerprint(source) != prepared.source_record_sha256:
                raise evidence.EvidenceRefused('canonical_source_version_changed')
            receipt = records.read_receipt(conn, target.node_key, target.domain, source, budget)
            if receipt is None or hashlib.sha256(records.canonical_bytes(receipt)).hexdigest() != expected_receipt_sha256:
                raise evidence.EvidenceRefused('admission_receipt_cas_changed')
            job_id = receipt['receipt_job_id']
            cursor = conn.execute('SELECT job_id,task_type,status,error_msg,updated_at,result_json '
                                  'FROM jobs WHERE job_id=?', (job_id,))
            row = cursor.fetchone()
            before = dict(zip((c[0] for c in cursor.description), row, strict=True))
            recovery, _ = self._recovery(context, job_id, 'revoke', before, errors)
            changed = conn.execute("UPDATE jobs SET status='cancelled',error_msg='source_binding_revoked',updated_at=? "
                "WHERE job_id=? AND task_type=? AND status='completed' AND result_json=?",
                (_now().isoformat(), job_id, records.KIND, before['result_json'])).rowcount
            if changed != 1:
                raise evidence.EvidenceRefused('admission_receipt_cas_changed')
        if errors:
            raise evidence._ResourceCleanupError(errors)
        return {'status': 'revoked', 'receipt_job_id': job_id, 'recovery_file': recovery,
                'knowledge_tables_changed': False}
