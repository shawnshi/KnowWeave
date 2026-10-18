"""Windows host adapter; no model-supplied roots or writer borrowing.

Construct only in trusted host code with its authorized root/config. This is not
an authentication layer or a model-callable root/connection factory. SQLite
mode=ro protects canonical data, not a claim of zero WAL/SHM coordination writes.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import stat
from threading import Event
import time

from vector_lake import query_evidence as evidence
from vector_lake import retrieval_runtime as runtime

_DB_PARTS = ('wiki', '.meta', 'vector_lake.db')


@dataclass(frozen=True, init=False)
class ReadOnlyEvidenceHost:
    """Explicit operator-owned capability, not a JSON/permission-flag constructor.

    Pilot canonical location is fixed under this root. VECTOR_LAKE_DB_PATH and
    ambient MEMORY/config changes never retarget a constructed host. Existing
    nonstandard DB locations are unsupported rather than silently borrowed.
    """
    _root: Path
    _exclude_paths: tuple[str, ...]
    _supported_extensions: tuple[str, ...]

    def __init__(self, memory_root, config):
        if os.name != 'nt':
            raise evidence.EvidenceRefused('safe_read_capability_unavailable')
        root, frozen = evidence._source_configuration(memory_root, config)
        object.__setattr__(self, '_root', root)
        object.__setattr__(self, '_exclude_paths', frozen['exclude_paths'])
        object.__setattr__(self, '_supported_extensions', frozen['supported_extensions'])

    def _context(self, cancellation, cleanup_errors=None, *, deadline=None):
        if cancellation is not None and not isinstance(cancellation, Event):
            raise ValueError('host_cancellation_event_required')
        end = time.monotonic() + runtime.remaining_seconds()
        if deadline is not None:
            end = min(end, deadline)  # Trusted facade may shorten, never extend it.
        return evidence._HostContext(self._root, self._exclude_paths, self._supported_extensions,
                                     end, cancellation, cleanup_errors)

    @staticmethod
    def _pin_existing_auxiliaries(context, stack, cleanup_errors):
        pinned = []
        for suffix in ('-wal', '-shm', '-journal'):
            runtime.checkpoint()
            context.checkpoint()
            parts = (*_DB_PARTS[:-1], _DB_PARTS[-1] + suffix)
            try:
                os.lstat(context.root.joinpath(*parts))
            except FileNotFoundError:
                continue
            fd = stack.enter_context(evidence._opened_source(context.root, parts, cleanup_errors=cleanup_errors))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise evidence.EvidenceRefused('regular_single_link_auxiliary_required')
            pinned.append((fd, info.st_dev, info.st_ino))
        return pinned

    @contextmanager
    def _snapshot(self, context, *, cleanup_errors=None):
        reported = cleanup_errors if cleanup_errors is not None else context.cleanup_errors
        has_receiver = reported is not None
        if reported is None:
            reported = []
        primary = None
        try:
            with self._snapshot_owned(context, reported) as conn:
                yield conn
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if primary is not None:
                if reported:
                    evidence._annotate_cleanup(primary, reported)
            elif not has_receiver and reported:
                raise evidence._ResourceCleanupError(reported)

    @contextmanager
    def _snapshot_owned(self, context, cleanup_errors):
        runtime.checkpoint()
        context.checkpoint()
        # One receiver covers SQLite, canonical/auxiliary descriptors and handles.
        with evidence._opened_source(context.root, _DB_PARTS, cleanup_errors=cleanup_errors) as fd, ExitStack() as auxiliaries:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise evidence.EvidenceRefused('regular_single_link_canonical_required')
            pinned_name = evidence._windows_path(fd)
            pinned_auxiliaries = self._pin_existing_auxiliaries(context, auxiliaries, cleanup_errors)
            runtime.checkpoint()
            context.checkpoint()
            filename = context.root.joinpath(*_DB_PARTS)
            conn = sqlite3.connect(filename.as_uri() + '?mode=ro&cache=private', uri=True,
                                   isolation_level=None, timeout=0, cached_statements=32)
            primary = None
            try:
                with runtime.timed_stage('evidence_snapshot_sql_setup'):
                    conn.execute('PRAGMA query_only=ON')
                    conn.execute('PRAGMA trusted_schema=OFF')
                    def interrupted():
                        return 1 if (runtime.expired() or time.monotonic() >= context.deadline
                                     or (context.cancellation is not None and context.cancellation.is_set())) else 0
                    conn.set_progress_handler(interrupted, 1000)
                    conn.execute('BEGIN DEFERRED')
                    # Reading sqlite_schema establishes the read generation. Transfer
                    # only tiny schema metadata, not full DDL or a whole DB backup.
                    schema = conn.execute(
                        "SELECT name,type,upper(substr(ltrim(sql),1,12)) FROM sqlite_schema "
                        "WHERE name IN ('entities','page_index_nodes') LIMIT 3").fetchall()
                    if ({row[0] for row in schema} != {'entities', 'page_index_nodes'}
                            or any(row[1:] != ('table', 'CREATE TABLE') for row in schema)):
                        raise evidence.EvidenceRefused('canonical_schema_unproven')
                    locations = conn.execute('PRAGMA database_list').fetchall()
                    if (len(locations) != 1 or locations[0][1] != 'main'
                            or os.path.normcase(os.path.normpath(locations[0][2])) != pinned_name):
                        raise evidence.EvidenceRefused('canonical_location_unproven')
                    runtime.checkpoint()
                    context.checkpoint()
                yield conn
            except BaseException as exc:
                primary = exc
                raise
            finally:
                actions = (('progress_handler_reset', lambda: conn.set_progress_handler(None, 0)),
                           ('rollback', lambda: conn.rollback()), ('connection_close', lambda: conn.close()))
                evidence._cleanup_resources(actions, primary, cleanup_errors)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, 1) != (after.st_dev, after.st_ino, after.st_nlink):
                raise evidence.EvidenceRefused('canonical_identity_changed')
            for auxiliary, device, inode in pinned_auxiliaries:
                current = os.fstat(auxiliary)
                if (device, inode, 1) != (current.st_dev, current.st_ino, current.st_nlink):
                    raise evidence.EvidenceRefused('canonical_auxiliary_identity_changed')

    def get_query_evidence(self, query, node_keys, *, domain, cluster=None, cancellation=None,
                           _deadline=None):
        """Dedicated snapshot per call; no writer/global connection is touched.

        Cancellation is an actual host Event, not a tool permission parameter.
        Safe partial results survive owned cleanup failures, which are explicit
        host_errors and prevent a complete result/terminal record.
        """
        domain, keys = evidence._validated_input(query, node_keys, domain, cluster)
        cleanup_errors = []
        context = self._context(cancellation, cleanup_errors, deadline=_deadline)
        result = None
        failure = None
        with runtime.timed_stage('evidence_host_total'):
            try:
                with self._snapshot(context, cleanup_errors=cleanup_errors) as conn:
                    result = evidence.get_query_evidence(query, keys, domain=domain, cluster=cluster,
                                                         canonical_connection=conn, _host_context=context)
            except evidence.EvidenceRefused as exc:
                failure = {'status': 'refused', 'code': str(exc)}
            except (OSError, sqlite3.Error, runtime.RetrievalBudgetExceeded) as exc:
                failure = evidence._error_record('', exc)
                failure.pop('node_key')
            if failure is not None:
                if result is None:
                    nodes = [{'node_key': key, **failure} for key in keys]
                    result = evidence._finish_batch(query, keys, nodes, evidence._Budget(context))
                else:
                    cleanup_errors.insert(0, failure)
            assert result is not None, 'Owned evidence request did not settle a result'
            if cleanup_errors:
                result = {**result,
                          'status': 'partial' if any(n['status'] == 'verified' for n in result['evidence']) else 'failed',
                          'host_errors': [*result.get('host_errors', []), *cleanup_errors]}
        # One final record only after DB/descriptor cleanup has settled. This does
        # not create a runtime scope or resolve/open an ambient ledger root.
        evidence._record_batch(query, keys, result)
        return result


def configured_query_evidence(query: str, node_keys: list[str], *, domain: str,
                              cluster: str | None = None) -> str:
    """Optional MCP facade; trusted configuration supplies private host resources.

    Missing canonical binding stays refused. This never fills metadata, opens a
    writer, constructs a ledger root, or silently substitutes ordinary reading.
    Existing original-reading workflows remain separate and explicitly scoped.
    """
    domain, keys = evidence._validated_input(query, node_keys, domain, cluster)
    budget = evidence._Budget()
    try:
        budget.check()
        if os.name != 'nt':
            raise evidence.EvidenceRefused('safe_read_capability_unavailable')
        root, config = evidence._configuration_snapshot()
        override = os.environ.get('VECTOR_LAKE_DB_PATH')
        if override and (os.path.normcase(os.path.normpath(str(Path(override).absolute())))
                         != os.path.normcase(os.path.normpath(str(root.joinpath(*_DB_PARTS))))):
            raise evidence.EvidenceRefused('nonstandard_canonical_location_unsupported')
        host = ReadOnlyEvidenceHost(root, config)
        budget.check()
    except evidence.EvidenceRefused as exc:
        result = evidence._finish_batch(query, keys,
            [{'node_key': k, 'status': 'refused', 'code': str(exc)} for k in keys], budget)
    except (OSError, RuntimeError, ValueError, runtime.RetrievalBudgetExceeded) as exc:
        result = evidence._finish_batch(query, keys,
            [evidence._error_record(k, exc) for k in keys], budget)
    else:
        result = host.get_query_evidence(query, keys, domain=domain, cluster=cluster,
                                         _deadline=budget.deadline)
    return json.dumps(result, ensure_ascii=False)
