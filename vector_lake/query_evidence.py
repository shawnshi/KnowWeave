"""Offline evidence-reader candidate; deliberately not registered in MCP.

Verification here means canonical binding and exact bounded source bytes, not
factual truth, authorization, full publication/projection, or whole-document review.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import heapq
import html
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
from threading import Event
from urllib.parse import unquote, urlsplit

from vector_lake import governance_store as store
from vector_lake import retrieval_runtime as runtime
from vector_lake import wiki_utils
from vector_lake.schema_validator import canonical_domain

MAX_NODES = 8
MAX_CANONICAL_ROWS = 64
MAX_PASSAGES = 3
MAX_EXCERPT_CHARS = 2500
# Pilot capability covers the already-authorized public/professional replay, not private domains.
PILOT_PREFIXES = {
    'Artificial_Intelligence': ('raw/人工智能/',),
    'Medical_IT': ('raw/article/', 'raw/数字化转型/', 'raw/医疗信息化/'),
}
_PRIVATE_PARTS = {'privacy', 'private', 'health', '健康', '投资', 'holdings', '.meta', '.git',
                  'credentials', 'credential', 'secrets', 'secret', 'auth',
                  'api_key', 'apikey', 'token', 'password'}
_URL = re.compile(r'https?://[^\s<>]+', re.I)
_CREDENTIAL = re.compile(
    r'(?im)\b(?:api[_ -]?key|access[_ -]?token|pass[_ -]?ticket|exportkey|'
    r'password|secret|sessionid|authorization|token)\b\s*[:=]\s*[^\s<>]+')
_IMAGE = re.compile(r'!\[[^\]\r\n]*\]\([^\)\r\n]+\)')
_NODE = re.compile(r'Source_[^/\\:?\x00-\x1f]{1,220}')
_IDENTIFIER_CREDENTIAL = re.compile(
    r'(?i)(?:^|[^a-z0-9])(?:api[_ -]?key|access[_ -]?token|pass[_ -]?ticket|'
    r'exportkey|password|secret|sessionid|authorization|auth|token|key)[ \t]*[:=]')


class EvidenceRefused(ValueError):
    """A fixed public code, never a source value or native exception message."""


class EvidenceCancelled(runtime.RetrievalBudgetExceeded):
    """Host cancellation, not a source absence or an exception-message channel."""


class _ResourceCleanupError(OSError):
    def __init__(self, faults):
        super().__init__('Owned resource cleanup failed')
        self.errors = tuple(faults)


def _exception_fields(exc):
    try:
        value = BaseException.__dict__['__dict__'].__get__(exc, BaseException)
        return value if issubclass(type(value), dict) else None
    except BaseException:
        return None


def _annotate_cleanup(primary, faults):
    # Access actual exception storage, not subclass setters or data descriptors.
    # Optional diagnostics must never replace the selected exception.
    try:
        values = _exception_fields(primary)
        if values is None:
            return False
        dict.__setitem__(values, '_evidence_cleanup_errors', tuple(dict(f) for f in faults))
        return True
    except BaseException:
        return False


def _cleanup_resources(actions, primary, reported):
    faults = []
    unexpected = None
    for stage, action in actions:
        try:
            action()
        except BaseException as exc:
            # This catch is only for attempting every owned close. Exception
            # priority is decided separately from best-effort diagnostics.
            try:
                fault = _error_record('', exc)
                dict.pop(fault, 'node_key', None)
                fault['stage'] = stage
            except BaseException:
                fault = {'status': 'error', 'code': 'CleanupDiagnosticUnavailable',
                         'native_codes': {}, 'stage': stage}
            faults.append(fault)
            if unexpected is None and not issubclass(type(exc), (OSError, sqlite3.Error)):
                unexpected = exc
    combined = faults
    if reported is not None:
        try:
            list.extend(reported, faults)
            combined = reported
        except BaseException:
            pass  # A diagnostic receiver cannot replace a primary or skip closes.
    if primary is not None:
        if combined:
            _annotate_cleanup(primary, combined)
        return
    if unexpected is not None:
        _annotate_cleanup(unexpected, combined)
        raise unexpected
    if faults and reported is None:
        raise _ResourceCleanupError(faults)


@dataclass(frozen=True)
class _HostContext:
    """Private host capability binding; never an MCP/JSON argument or permission flag."""
    root: Path
    exclude_paths: tuple[str, ...]
    supported_extensions: tuple[str, ...]
    deadline: float
    cancellation: Event | None = None
    cleanup_errors: list[dict] | None = None

    def checkpoint(self):
        if self.cancellation is not None and self.cancellation.is_set():
            raise EvidenceCancelled('Evidence request cancelled')
        if time.monotonic() >= self.deadline:
            raise runtime.RetrievalBudgetExceeded('Evidence deadline budget exhausted')

    def configuration(self):
        return {'exclude_paths': self.exclude_paths, 'supported_extensions': self.supported_extensions}


class _Budget:
    def __init__(self, context=None):
        self.context = context
        state = runtime.current()
        self.used = state.source_bytes if state is not None else 0
        self.deadline = time.monotonic() + runtime.remaining_seconds()
        if context is not None:
            self.deadline = min(self.deadline, context.deadline)

    def check(self):
        runtime.checkpoint()
        if self.context is not None:
            self.context.checkpoint()
        if time.monotonic() >= self.deadline:
            raise runtime.RetrievalBudgetExceeded('Evidence deadline budget exhausted')

    def charge(self, size):
        # Bytes already transferred stay charged even if the next guard refuses them.
        if type(size) is not int or size < 0:
            raise EvidenceRefused('invalid_input_byte_count')
        self.used += size
        state = runtime.current()
        if state is not None:
            state.source_bytes += size
        self.check()
        if self.used > runtime.MAX_TOTAL_SOURCE_BYTES:
            raise EvidenceRefused('aggregate_budget')

    def allowance(self):
        self.check()
        return min(runtime.MAX_SOURCE_BYTES, runtime.MAX_TOTAL_SOURCE_BYTES - self.used)


def _sensitive_identifier(value):
    decoded = unquote(html.unescape(value)).replace('\\_', '_')
    return bool(_IDENTIFIER_CREDENTIAL.search(decoded))


def _node_key(value):
    if isinstance(value, str) and _sensitive_identifier(value):
        raise ValueError('sensitive_source_identifier')
    if not isinstance(value, str) or not _NODE.fullmatch(value):
        raise ValueError('source_node_keys_required')
    key = value[:-3] if value.endswith('.md') else value
    if key.endswith('.') or '..' in key:
        raise ValueError('invalid_source_node_key')
    return key


def _domain(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('explicit_domain_required')
    domain = canonical_domain(value)
    if domain not in PILOT_PREFIXES:
        raise ValueError('pilot_domain_unsupported')
    return domain


def _source_configuration(configured, config):
    if not isinstance(config, dict):
        raise EvidenceRefused('configuration_invalid')
    if not isinstance(configured, (str, Path)) or (isinstance(configured, str) and not configured.strip()):
        raise EvidenceRefused('explicit_memory_root_required')
    root = (configured if isinstance(configured, Path) else Path(configured.strip())).expanduser()
    if not root.is_absolute() or '..' in root.parts:
        raise EvidenceRefused('trusted_absolute_root_required')
    _require_local_windows_root(root)
    exclusions = config.get('exclude_paths', [])
    extensions = config.get('supported_extensions', ['.md', '.txt'])
    if (not isinstance(exclusions, (list, tuple)) or not all(isinstance(x, str) for x in exclusions)
            or not isinstance(extensions, (list, tuple)) or not all(isinstance(x, str) for x in extensions)):
        raise EvidenceRefused('configuration_invalid')
    return root, {'exclude_paths': tuple(exclusions), 'supported_extensions': tuple(extensions)}


def _configuration_snapshot():
    # Standalone helper only; bound host requests never consult global/env roots.
    config = wiki_utils.load_config()
    if not isinstance(config, dict):
        raise EvidenceRefused('configuration_invalid')
    configured = os.environ.get('VECTOR_LAKE_MEMORY_DIR') or config.get('memory_dir')
    return _source_configuration(configured, config)


def _raw_reference(value, domain, root, config):
    if isinstance(value, str) and _sensitive_identifier(value):
        raise EvidenceRefused('sensitive_source_identifier')
    if not isinstance(value, str) or '\x00' in value or ':' in value:
        raise EvidenceRefused('binding_unproven')
    text = value.replace('\\', '/')
    parts = text.split('/')
    if not parts or parts[0] != 'raw' or any(p in {'', '.', '..'} for p in parts):
        raise EvidenceRefused('source_path_refused')
    if any(p.casefold() in _PRIVATE_PARTS or p.startswith('.') for p in parts[1:]):
        raise EvidenceRefused('private_source_refused')
    if PurePosixPath(parts[-1]).stem.casefold() in _PRIVATE_PARTS:
        raise EvidenceRefused('private_source_refused')
    if wiki_utils.is_private_raw_source(text) or not text.startswith(PILOT_PREFIXES[domain]):
        raise EvidenceRefused('private_or_unsupported_pilot_source')
    if PurePosixPath(text).suffix.casefold() not in {'.md', '.txt'}:
        raise EvidenceRefused('source_format_refused')
    if os.name == 'nt' and any(os.path.isreserved(p) for p in parts):
        raise EvidenceRefused('source_path_refused')
    # Reuse the configured exclusion/format rules, not the ingest pathname resolver:
    # resolving an attacker-swapped reparse point may itself follow a network target.
    lexical = root.joinpath(*parts).as_posix().casefold()
    if (any(str(p).casefold() in lexical for p in config.get('exclude_paths', []))
            or PurePosixPath(text).suffix.casefold() not in config.get('supported_extensions', ['.md', '.txt'])):
        raise EvidenceRefused('configured_source_refused')
    return parts, text


def _windows_path(fd):
    import ctypes
    from ctypes import wintypes
    import msvcrt
    fn = ctypes.WinDLL('kernel32', use_last_error=True).GetFinalPathNameByHandleW
    fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(32768)
    size = fn(msvcrt.get_osfhandle(fd), buf, len(buf), 0)
    if not size or size >= len(buf):
        raise EvidenceRefused('opened_path_unverifiable')
    path = buf.value
    if path.startswith('\\\\?\\UNC\\'):
        raise EvidenceRefused('network_source_unsupported')
    if path.startswith('\\\\?\\'):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _require_local_windows_root(root):
    import ctypes
    from ctypes import wintypes
    fn = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
    fn.argtypes = [wintypes.LPCWSTR]
    fn.restype = wintypes.UINT
    if not root.drive or root.anchor.startswith('\\\\') or fn(root.anchor) != 3:
        raise EvidenceRefused('fixed_local_drive_required')


@contextmanager
def _windows_source(root, parts, cleanup_errors=None):
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT

    class Information(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME),
                    ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
                    ('volume', wintypes.DWORD), ('size_hi', wintypes.DWORD),
                    ('size_lo', wintypes.DWORD), ('links', wintypes.DWORD),
                    ('index_hi', wintypes.DWORD), ('index_lo', wintypes.DWORD)]

    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    if not root.drive or root.anchor.startswith('\\\\') or kernel.GetDriveTypeW(root.anchor) != 3:
        raise EvidenceRefused('fixed_local_drive_required')
    invalid = ctypes.c_void_p(-1).value
    handles = []
    fd = None

    def opened(path, directory):
        # Parent READ sharing only prevents a concurrent reparse-point write as well
        # as rename/delete. Leaf WRITE sharing permits normal edits, caught by fstat/hash.
        handle = kernel.CreateFileW(str(path), 0x81 if directory else 0x80000080,
                                    1 if directory else 3, None, 3, 0x02200000, None)
        if handle is None or handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(handle)
        info = Information()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            raise EvidenceRefused('source_reparse_or_path_change')
        return handle

    primary = None
    try:
        current = Path(root.anchor)
        opened(current, True)
        for part in [*root.parts[1:], *parts[:-1]]:
            current = current / part
            opened(current, True)
        leaf = opened(current / parts[-1], False)
        fd = msvcrt.open_osfhandle(leaf, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        handles.pop()  # CRT now owns this one handle.
        expected = os.path.normcase(os.path.normpath(str(current / parts[-1])))
        if _windows_path(fd) != expected:
            raise EvidenceRefused('source_reparse_or_path_change')
        yield fd
        if _windows_path(fd) != expected:
            raise EvidenceRefused('source_changed_during_read')
    except BaseException as exc:
        primary = exc
        raise
    finally:
        def close_handle(handle):
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        actions = []
        if fd is not None:
            actions.append(('source_fd_close', lambda: os.close(fd)))
        actions.extend((f'source_handle_close_{i}', lambda h=handle: close_handle(h))
                       for i, handle in enumerate(reversed(handles)))
        _cleanup_resources(actions, primary, cleanup_errors)


@contextmanager
def _opened_source(root, parts, cleanup_errors=None):
    """Windows-only verified capability; unsupported platforms fail before path access."""
    if os.name != 'nt':
        raise EvidenceRefused('safe_read_capability_unavailable')
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise EvidenceRefused('trusted_absolute_root_required')
    with _windows_source(root, parts, cleanup_errors=cleanup_errors) as fd:
        yield fd


def _read_raw(root, parts, budget):
    allowance = budget.allowance()
    if allowance <= 0:
        raise EvidenceRefused('aggregate_budget')
    cleanup_errors = budget.context.cleanup_errors if budget.context is not None else None
    with _opened_source(root, parts, cleanup_errors=cleanup_errors) as fd:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EvidenceRefused('regular_single_link_source_required')
        if before.st_size > allowance:
            raise EvidenceRefused('source_read_budget')
        chunks = []
        size = 0
        while size < before.st_size:
            budget.check()
            chunk = os.read(fd, min(65536, before.st_size - size))
            if not chunk:
                raise EvidenceRefused('source_changed_during_read')
            budget.charge(len(chunk))
            size += len(chunk)
            chunks.append(chunk)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
        if any(getattr(before, f) != getattr(after, f) for f in fields):
            raise EvidenceRefused('source_changed_during_read')
    return b''.join(chunks)


def _sql_checkpoint(conn, budget):
    budget.check()
    timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
    if type(timeout) is not int or timeout > max(0.0, (budget.deadline - time.monotonic()) * 1000):
        raise EvidenceRefused('canonical_statement_budget_required')


def _require_snapshot(conn, budget):
    # Actual host-owned connection is the authority/capability; flags alone are not.
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction:
        raise EvidenceRefused('canonical_snapshot_required')
    _sql_checkpoint(conn, budget)
    if conn.execute('PRAGMA query_only').fetchone()[0] != 1:
        raise EvidenceRefused('readonly_canonical_snapshot_required')


def _eligible_source_record(conn, key, domain, cluster, budget):
    budget.check()
    if budget.allowance() <= 0:
        raise EvidenceRefused('aggregate_budget')
    _sql_checkpoint(conn, budget)
    selected = conn.execute('SELECT domain,topic_cluster FROM page_index_nodes WHERE node_key=?', (key,)).fetchone()
    if selected is None or not isinstance(selected[0], str) or canonical_domain(selected[0]) != domain:
        raise EvidenceRefused('not_available')
    if cluster and str(selected[1] or '').casefold() != cluster.casefold():
        raise EvidenceRefused('not_available')
    _sql_checkpoint(conn, budget)
    rows = conn.execute(
        'SELECT rowid,length(CAST(data_json AS BLOB)) FROM entities WHERE f_page_key=? LIMIT ?',
        (key, MAX_CANONICAL_ROWS + 1))
    sources = []
    try:
        for count, (rowid, size) in enumerate(rows, 1):
            budget.check()
            if count > MAX_CANONICAL_ROWS or type(size) is not int or size > budget.allowance():
                raise EvidenceRefused('canonical_read_budget')
            _sql_checkpoint(conn, budget)
            body = conn.execute(
                'SELECT CASE WHEN length(CAST(data_json AS BLOB))<=? THEN data_json END AS payload,'
                'length(CAST(data_json AS BLOB)) FROM entities WHERE rowid=? AND f_page_key=?',
                (budget.allowance(), rowid, key)).fetchone()
            if body is None or body[0] is None or body[1] != size:
                raise EvidenceRefused('canonical_read_budget')
            raw = body[0]
            budget.charge(size)
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise EvidenceRefused('canonical_record_invalid')
            if str(obj.get('type', '')).casefold() == 'source':
                sources.append(obj)
    finally:
        rows.close()
    if len(sources) != 1:
        raise EvidenceRefused('binding_unproven')
    source = sources[0]
    if str(source.get('status', '')).casefold() not in {'active', 'decayed'}:
        raise EvidenceRefused('not_available')
    if canonical_domain(source.get('domain', '')) != domain:
        raise EvidenceRefused('not_available')
    if cluster and str(source.get('topic_cluster', '')).casefold() != cluster.casefold():
        raise EvidenceRefused('not_available')
    return source


def _canonical_source(conn, key, domain, cluster, budget):
    source = _eligible_source_record(conn, key, domain, cluster, budget)
    # This is a distinct durable CURRENT admission, not an ordinary-reading or
    # malformed-legacy fallback. Partial/conflicting legacy stamps still refuse.
    if source.get('source_hash') is None and source.get('source_frontmatter') is None:
        from vector_lake.source_binding_record import BindingReceiptRefused, read_receipt
        try:
            receipt = read_receipt(conn, key, domain, source, budget)
        except BindingReceiptRefused as exc:
            raise EvidenceRefused(str(exc)) from exc
        if receipt is None:
            raise EvidenceRefused('binding_unproven')
        return source, receipt['raw_reference'], 'sha256', receipt['raw_sha256'], receipt
    refs = source.get('sources')
    digest = source.get('source_hash')
    if not isinstance(digest, str):
        raise EvidenceRefused('binding_unproven')
    digest = digest.casefold()
    algorithm = 'sha256' if len(digest) == 64 else 'md5'
    if digest.startswith('sha256:'):
        algorithm, digest = 'sha256', digest[7:]
        if len(digest) != 64:
            raise EvidenceRefused('binding_unproven')
    if (not isinstance(source.get('source_frontmatter'), dict) or not isinstance(refs, list)
            or len(refs) != 1 or not re.fullmatch(r'(?:[0-9a-f]{32}|[0-9a-f]{64})', digest)):
        raise EvidenceRefused('binding_unproven')
    return source, refs[0], algorithm, digest, None


def _sensitive_regions(content):
    regions = [(m.start(), m.end()) for m in _CREDENTIAL.finditer(content)]
    for match in _URL.finditer(content):
        candidate = html.unescape(match.group().replace('\\_', '_'))
        try:
            url = urlsplit(candidate)
            sensitive = bool(url.query or url.fragment or url.username or url.password)
        except ValueError:
            sensitive = True
        if sensitive:
            regions.append((match.start(), match.end()))
    return regions


def _span(content, start, end):
    """Exclusive char/UTF-8 offsets refer to the decoded original, including BOM/CRLF."""
    text = content[start:end]
    return {'text': text, 'char_start': start, 'char_end': end,
            'byte_start': len(content[:start].encode('utf-8')),
            'byte_end': len(content[:end].encode('utf-8')),
            'line_start': content.count('\n', 0, start) + 1,
            'line_end': content.count('\n', 0, max(start, end - 1)) + 1,
            'exact_source_slice': True}


def _paragraph_spans(content):
    start = 0
    for match in itertools.chain(re.finditer(r'\r?\n[ \t]*\r?\n', content), (None,)):
        end = match.start() if match else len(content)
        yield start, end
        start = match.end() if match else end


def _blocked_paragraphs(content, budget):
    regions = sorted(_sensitive_regions(content))
    blocked = []
    index = 0
    for start, end in _paragraph_spans(content):
        budget.check()
        while index < len(regions) and regions[index][1] <= start:
            index += 1
        if index < len(regions) and regions[index][0] < end:
            blocked.append((start, end))
    return blocked


def _passages(content, query, budget, blocked):
    terms = store._query_terms(query)
    def candidates():
        for start, end in _paragraph_spans(content):
            budget.check()
            raw = content[start:end]
            left = len(raw) - len(raw.lstrip())
            right = len(raw.rstrip())
            a, b = start + left, start + right
            if b <= a or _IMAGE.sub('', content[a:b]).strip('\ufeff \r\n\t') == '':
                continue
            if any(x < b and y > a for x, y in blocked):
                continue
            lower = content[a:b].lower()
            score = 0
            for i, term in enumerate(terms):
                if i % 128 == 0:
                    budget.check()
                score += term in lower
            yield -score, a, b
    chosen = []
    remaining = MAX_EXCERPT_CHARS
    for neg_score, start, end in heapq.nsmallest(MAX_PASSAGES, candidates()):
        if not remaining or len(chosen) >= MAX_PASSAGES:
            break
        if end - start > remaining and neg_score:
            segment = content[start:end]
            hits = []
            for i, term in enumerate(terms):
                if i % 128 == 0:
                    budget.check()
                for match in re.finditer(re.escape(term), segment, re.I):
                    if term in match.group().lower():
                        hits.append(match.start())
                        break
            if hits:
                start += min(max(0, min(hits) - remaining // 3), end - start - remaining)
        end = min(end, start + remaining)
        visible = content[start:end].lower()
        chosen.append((start, end, any(term in visible for term in terms)))
        remaining -= end - start
    passages = [{**_span(content, a, b), 'has_query_term_match': matched,
                 'semantic_support': 'not_adjudicated'} for a, b, matched in sorted(chosen)]
    coverage = 'text_only_partial' if _IMAGE.search(content) else 'selected_fragments_only'
    return passages, coverage, bool(blocked)


def _attribution(content, blocked):
    candidates = []
    pattern = re.compile(r'(?im)^\ufeff?(author|作者|published_at|发布日期|发表日期|date|created|updated)[ \t]*[:：][ \t]*([^\r\n]{1,160})\r?$')
    for match in pattern.finditer(content):
        if len(candidates) >= 6:
            break
        if any(a < match.end() and b > match.start() for a, b in blocked):
            continue
        name, value = match[1].casefold(), match[2].strip()
        if not value:
            continue
        candidates.append({'field': name, 'value': value,
                           'role': 'record_metadata_only' if name in {'created', 'updated'} else 'unspecified',
                           'status': 'explicit_text_not_adjudicated',
                           'locator': {'line': content.count('\n', 0, match.start()) + 1,
                                       'char_start': match.start(), 'char_end': match.end()}})
    return candidates


def _one(conn, root, config, key, domain, cluster, query, budget):
    with runtime.timed_stage('evidence_binding'):
        source, reference, algorithm, expected_hash, receipt = _canonical_source(conn, key, domain, cluster, budget)
        parts, relative = _raw_reference(reference, domain, root, config)
    with runtime.timed_stage('evidence_source_read'):
        raw = _read_raw(root, parts, budget)
    actual = (hashlib.md5(raw, usedforsecurity=False).hexdigest() if algorithm == 'md5'
              else hashlib.sha256(raw).hexdigest())
    if actual != expected_hash or (receipt is not None and len(raw) != receipt['raw_size']):
        raise EvidenceRefused('stale_source_binding')
    content = raw.decode('utf-8')
    with runtime.timed_stage('evidence_extract'):
        blocked = _blocked_paragraphs(content, budget)
        passages, coverage, sensitive = _passages(content, query, budget, blocked)
        attribution = _attribution(content, blocked)
    if not passages:
        raise EvidenceRefused('no_safe_text_passage')
    result = {'node_key': key, 'status': 'verified', 'domain': domain,
            'source': relative, 'canonical_status': source['status'],
            'source_sha256': hashlib.sha256(raw).hexdigest(),
            'canonical_source_record_sha256': hashlib.sha256(json.dumps(source, sort_keys=True,
                separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
            'binding_level': 'canonical_source_record_and_exact_raw_bytes_not_full_publication',
            'passages': passages, 'coverage': coverage, 'sensitive_spans_withheld': sensitive,
            'attribution_candidates': attribution,
            'author_and_publication_truth': 'not_adjudicated'}
    if receipt is not None:
        result['binding_admission'] = {
            'receipt_job_id': receipt['receipt_job_id'], 'admitted_at': receipt['admitted_at'],
            'scope': 'current_binding_not_historical_ownership_or_full_publication'}
    return result


def _record_batch(query, keys, result):
    # Existing authorized host scope only; defer_record never opens a ledger itself.
    runtime.defer_record({'query': query, 'mode': 'context', 'top_k': len(keys),
                          'returned': [{'key': e['node_key']} for e in result['evidence'] if e['status'] == 'verified'],
                          'notes': [] if result['status'] == 'complete' else ['Evidence batch incomplete; inspect explicit per-node status'],
                          'status': result['status']})


def _finish_batch(query, keys, evidence, budget):
    complete = all(e['status'] == 'verified' and e['coverage'] != 'text_only_partial'
                   and not e['sensitive_spans_withheld'] for e in evidence)
    status = 'complete' if complete else 'partial' if any(e['status'] == 'verified' for e in evidence) else 'failed'
    result = {'schema_version': 2, 'status': status, 'evidence': evidence,
              'verification_scope': 'binding_and_exact_selected_bytes_only',
              'not_claimed': ['factual truth', 'full document/image review', 'author/date adjudication',
                              'full current publication/projection', 'independent security approval']}
    # The owned adapter settles cleanup before emitting its one terminal record.
    if budget.context is None:
        _record_batch(query, keys, result)
    runtime.generation('query_evidence', {'nodes': len(keys), 'bounded_input_bytes': budget.used})
    return result


def _error_record(key, exc):
    # Built-in storage/descriptors avoid executing exception-subclass properties.
    codes = {}
    values = _exception_fields(exc)
    for name in ('errno', 'winerror', 'sqlite_errorcode'):
        try:
            descriptor = OSError.__dict__.get(name) if issubclass(type(exc), OSError) else None
            value = (descriptor.__get__(exc, OSError) if descriptor is not None
                     else dict.get(values, name) if values is not None else None)
            if type(value) is int:
                codes[name] = value
        except BaseException:
            continue  # Optional numeric metadata is not the underlying failure.
    try:
        name = type.__dict__['__name__'].__get__(type(exc), type)
    except BaseException:
        name = 'BaseException'
    record = {'node_key': key, 'status': 'error', 'code': name, 'native_codes': codes}
    if type(exc) is _ResourceCleanupError and values is not None:
        record['cleanup_errors'] = dict.get(values, 'errors', ())
    return record


def _validated_input(query, node_keys, domain, cluster):
    runtime.validate(query)
    domain = _domain(domain)
    if not isinstance(node_keys, (list, tuple)) or not 1 <= len(node_keys) <= MAX_NODES:
        raise ValueError('node_count_outside_budget')
    keys = [_node_key(k) for k in node_keys]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate_source_node_keys')
    if cluster is not None and (not isinstance(cluster, str) or not cluster.strip() or len(cluster) > 200):
        raise ValueError('invalid_cluster')
    return domain, tuple(keys)


def get_query_evidence(query, node_keys, *, domain, cluster=None, canonical_connection=None,
                       _host_context=None):
    """Windows-only reader; connection/context are actual host resources, not MCP inputs.

    The reader does not open DB filenames or take over connection lifecycle. A
    bound host context pins root/config/deadline/cancellation and defers terminal
    telemetry until the owned adapter has settled cleanup. Standalone borrowed
    snapshot behavior remains explicit and creates no generic runtime scope.
    """
    domain, keys = _validated_input(query, node_keys, domain, cluster)
    if _host_context is not None and not isinstance(_host_context, _HostContext):
        raise ValueError('private_host_context_required')
    budget = _Budget(_host_context)
    try:
        budget.check()
        if os.name != 'nt':
            raise EvidenceRefused('safe_read_capability_unavailable')
        root, config = ((_host_context.root, _host_context.configuration())
                        if _host_context is not None else _configuration_snapshot())
    except EvidenceRefused as exc:
        return _finish_batch(query, keys, [{'node_key': k, 'status': 'refused', 'code': str(exc)} for k in keys], budget)
    except (OSError, RuntimeError, ValueError) as exc:
        return _finish_batch(query, keys, [_error_record(k, exc) for k in keys], budget)
    try:
        _require_snapshot(canonical_connection, budget)
    except EvidenceRefused as exc:
        return _finish_batch(query, keys, [{'node_key': k, 'status': 'refused', 'code': str(exc)} for k in keys], budget)
    except (sqlite3.Error, runtime.RetrievalBudgetExceeded) as exc:
        return _finish_batch(query, keys, [_error_record(k, exc) for k in keys], budget)
    runtime.backend('canonical_raw_evidence')
    evidence = []
    for key in keys:
        try:
            evidence.append(_one(canonical_connection, root, config, key, domain, cluster, query, budget))
        except EvidenceRefused as exc:
            evidence.append({'node_key': key, 'status': 'refused', 'code': str(exc)})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, sqlite3.Error,
                runtime.RetrievalBudgetExceeded) as exc:
            evidence.append(_error_record(key, exc))
    return _finish_batch(query, keys, evidence, budget)
