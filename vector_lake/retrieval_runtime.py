"""Request-local retrieval limits and terminal telemetry; not an authorization boundary."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect
import logging
import os
from pathlib import Path
import time

#: Process identity, so a ledger entry can say which process answered and how old it was.  Without
#: it a slow call cannot be told apart from a cold process: two restarts look like two slow queries.
PROCESS_STARTED = time.monotonic()
PROCESS_ID = os.getpid()
PROCESS_TOKEN = f"{PROCESS_ID:x}-{int(PROCESS_STARTED):x}"

#: Process-wide structural counters.  A request reports the delta it caused, which is what makes
#: "did this call rebuild the graph" answerable from the ledger instead of inferred from a latency.
_COUNTERS: dict[str, int] = {}


def counter(name: str, amount: int = 1) -> None:
    """Count one structural event (a rebuild, a parse, a cache hit)."""
    _COUNTERS[name] = _COUNTERS.get(name, 0) + amount


def counter_snapshot() -> dict[str, int]:
    return dict(_COUNTERS)


def counter_delta(before: dict[str, int]) -> dict[str, int]:
    """Counters that moved since ``before``; unchanged ones are omitted."""
    return {name: value - before.get(name, 0) for name, value in _COUNTERS.items()
            if value - before.get(name, 0)}


MAX_QUERY_CHARS = 4096
MAX_TOP_K = 50
MAX_CONTEXT_CHARS = 200000
MAX_SOURCE_BYTES = 512 * 1024
MAX_TOTAL_SOURCE_BYTES = 4 * 1024 * 1024
REQUEST_SECONDS = 15.0
_CURRENT: ContextVar[RequestState | None] = ContextVar('retrieval_request', default=None)
log = logging.getLogger(__name__)


class RetrievalBudgetExceeded(RuntimeError):
    pass


class SourceBudgetExceeded(RetrievalBudgetExceeded):
    pass


@dataclass
class RequestState:
    started: float
    deadline: float
    source_bytes: int = 0
    problems: list[str] = field(default_factory=list)
    backends: set[str] = field(default_factory=set)
    generations: dict = field(default_factory=dict)
    pending: list[dict] = field(default_factory=list)
    connections: dict = field(default_factory=dict)
    emitted_keys: set[str] | None = None
    fts_scope: dict | None = None
    failed: bool = False
    query: str = ''
    mode: str = 'page'
    top_k: int = 0
    completion: str = 'complete'
    timings: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    def problem(self, message: str):
        if message not in self.problems:
            self.problems.append(message)


def current() -> RequestState | None:
    return _CURRENT.get()


def safe_error(exc: BaseException) -> str:
    """Only public type and numeric native code; exception text may contain caller data."""
    value = type(exc).__name__
    code = getattr(exc, 'sqlite_errorcode', None)
    if type(code) is int:
        value += f' (sqlite_code={code})'
    return value


def validate(query: str, top_k=None, max_chars=None):
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError(f'query must contain 1..{MAX_QUERY_CHARS} characters')
    if top_k is not None and (type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K):
        raise ValueError(f'top_k must be an integer in 1..{MAX_TOP_K}')
    if max_chars is not None and (type(max_chars) is not int or not 1 <= max_chars <= MAX_CONTEXT_CHARS):
        raise ValueError(f'max_chars must be an integer in 1..{MAX_CONTEXT_CHARS}')


def remaining_seconds() -> float:
    state = current()
    return max(0.0, state.deadline - time.monotonic()) if state else REQUEST_SECONDS


def expired() -> bool:
    state = current()
    if state is not None and time.monotonic() >= state.deadline:
        state.problem('Request deadline budget exhausted')
        return True
    return False


def checkpoint():
    if expired():
        raise RetrievalBudgetExceeded('Request deadline budget exhausted')


def failed_stage(stage: str, exc: BaseException) -> str:
    message = f'{stage} failed: {safe_error(exc)}'
    state = current()
    if state is not None:
        state.problem(message)
    log.warning('%s', message)
    return message


def backend(name: str):
    state = current()
    if state is not None:
        state.backends.add(name)


def generation(name: str, revision):
    state = current()
    if state is not None:
        state.generations[name] = revision


@contextmanager
def timed_stage(name: str):
    """Accumulate coarse inclusive wall time; no request means no clock/ledger work.

    Nested stages overlap and must not be summed with their parent. Timing failures
    are not caught here: the original body exception and terminal status survive.
    """
    state = current()
    if state is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = max(0.0, (time.perf_counter() - started) * 1000)
        state.timings[name] = state.timings.get(name, 0.0) + elapsed


def track_connection(conn):
    state = current()
    if state is not None and id(conn) not in state.connections:
        # These are the retrieval engine's thread-local connections, not borrowed API handles.
        timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        conn.set_progress_handler(lambda: int(time.monotonic() >= state.deadline), 1000)
        state.connections[id(conn)] = (conn, timeout)
    if state is not None:
        _, timeout = state.connections[id(conn)]
        conn.execute(f'PRAGMA busy_timeout={min(timeout, max(1, int(remaining_seconds() * 1000)))}')
    return conn


def read_source(path: Path, *, root: Path | None = None) -> str:
    checkpoint()
    path = Path(path).resolve()
    if root is not None and not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Source path is outside Wiki root')
    state = current()
    allowance = min(MAX_SOURCE_BYTES, MAX_TOTAL_SOURCE_BYTES - state.source_bytes) if state else MAX_SOURCE_BYTES
    if allowance <= 0:
        raise SourceBudgetExceeded('Total source read budget exhausted')
    with Path(path).open('rb') as stream:
        data = stream.read(allowance + 1)
    if state is not None:
        state.source_bytes += min(len(data), allowance)
    if len(data) > allowance:
        raise SourceBudgetExceeded('Source read limit/budget exceeded')
    checkpoint()
    return data.decode('utf-8')


def defer_record(payload: dict) -> bool:
    state = current()
    if state is None:
        return False
    state.pending.append(payload)
    status = payload.get('status', 'complete')
    if status == 'failed' or state.completion == 'complete':
        state.completion = status
    return True


def _finish(state: RequestState):
    from vector_lake import search_ledger
    total = (time.monotonic() - state.started) * 1000
    events = counter_delta(state.counters)
    if not state.pending:
        state.pending.append({'query': state.query, 'mode': state.mode, 'top_k': state.top_k,
                              'returned': [], 'notes': [], 'status': 'complete'})
    for payload in state.pending:
        payload = dict(payload)
        notes = list(payload.get('notes') or [])
        notes.extend(n for n in state.problems if n not in notes)
        payload['notes'] = notes
        if state.emitted_keys is not None:
            payload['returned'] = [r for r in payload['returned'] if r.get('key') in state.emitted_keys]
        status = payload.pop('status', 'complete')
        if state.failed:
            status = 'failed'
        elif state.problems and status == 'complete':
            status = 'partial'
        payload['status'] = status
        if status == 'failed' and not payload.get('error'):
            payload['error'] = 'retrieval_failed'
        # The payload keeps the elapsed time of the stage that produced it; the whole request is
        # recorded beside it.  Overwriting ``elapsed_ms`` here is what made a page search inside a
        # context assembly report the assembly's duration, so a scoped memory packet and a page
        # search were indistinguishable in the ledger.
        payload.setdefault('elapsed_ms', total)
        payload['request_total_ms'] = total
        payload['entry_point'] = payload.get('entry_point') or state.mode
        payload['process'] = {'pid': PROCESS_ID, 'token': PROCESS_TOKEN,
                              'uptime_ms': round((time.monotonic() - PROCESS_STARTED) * 1000)}
        if events:
            payload['cache_events'] = events
        payload['timings'] = {**payload.get('timings', {}), **state.timings,
                              'endpoint_total': total}
        payload['index_generation'] = repr((payload.get('index_generation'), state.generations))
        payload['configuration'] = {**payload.get('configuration', {}),
                                    'actual_vector_backends': sorted(b for b in state.backends if b in {'ram', 'exact_sql', 'two_stage'}),
                                    'actual_backends': sorted(state.backends),
                                    'source_bytes': state.source_bytes}
        search_ledger.record(**payload)


@contextmanager
def request_scope(query, *, top_k=None, max_chars=None):
    validate(query, top_k, max_chars)
    existing = current()
    if existing is not None:
        yield existing
        return
    started = time.monotonic()
    state = RequestState(started, started + REQUEST_SECONDS, query=query, top_k=top_k or 0)
    state.counters = counter_snapshot()
    token = _CURRENT.set(state)
    try:
        yield state
    except BaseException as exc:
        state.failed = True
        state.problem('Request failed: ' + safe_error(exc))
        raise
    finally:
        for conn, timeout in state.connections.values():
            try:
                conn.set_progress_handler(None, 0)
                conn.execute(f'PRAGMA busy_timeout={int(timeout)}')
            except Exception as exc:
                log.warning('Retrieval SQL budget cleanup: %s', safe_error(exc))
        try:
            _finish(state)
        except Exception as exc:
            # Observation failure cannot replace the search result or its original exception.
            log.warning('Retrieval terminal observation: %s', safe_error(exc))
        finally:
            _CURRENT.reset(token)


def request_entry(fn):
    signature = inspect.signature(fn)
    @wraps(fn)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        query = values.get('query', values.get('query_str'))
        with request_scope(query, top_k=values.get('top_k'), max_chars=values.get('max_chars')) as state:
            state.mode = values.get('mode', 'context' if 'query_str' in values or 'max_chars' in values else 'page')
            result = fn(*args, **kwargs)
            if isinstance(result, dict):
                result['retrieval_status'] = state.completion if state.completion == 'failed' or not state.problems else 'partial'
                if state.problems:
                    result['retrieval_diagnostics'] = list(state.problems)
            return result
    return wrapped
