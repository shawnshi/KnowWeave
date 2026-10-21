"""Read-only, point-in-time process observations; never a deployment/health approval."""
from __future__ import annotations

import ast
import hashlib
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import types

PACKAGE = Path(__file__).resolve().parent
MODULE_PROBES = {
    'vector_lake.db_store': ('transaction', 'claim_mutation_outbox'),
    'vector_lake.mutation_coordinator': ('execute_mutation_batch', 'materialize_markdown_projection'),
    'vector_lake.indexer': ('update_index_items', '_calculate_weighted_edges'),
    'vector_lake.watchdog_app': ('process_mutation_outbox_batch',),
    'vector_lake.mcp_server': ('runtime_identity',),
    'vector_lake.runtime_identity': ('runtime_identity',),
    'vector_lake.wiki_utils': ('split_frontmatter', 'atomic_write_text', 'flush_durable', 'fsync_directory', 'entity_identity_key'),
    'vector_lake.yaml_utils': ('load_yaml', 'dump_yaml'),
    'vector_lake.link_resolution': ('build_link_map', 'resolve_link_target', 'core_name_maps', 'declaration_map', 'declared_names_from_nodes'),
    'vector_lake.node_vocabulary': ('strip_prefix',),
    'vector_lake.relation_catalog': ('build_relation_catalog', 'identity_resolver', 'in_scope', 'node_field', 'validate_limit'),
    'vector_lake.relation_policy': ('predicate_weight', 'pair_relation_weight'),
    'vector_lake.tool_relations': ('get_relations',),
    'vector_lake.tool_graph': ('visualize_vector_lake', 'audit_graph', '_build_page_graph', '_build_graph_payload', '_extract_backbone_edges'),
}
CONSTANT_PROBES = {
    'vector_lake.relation_policy': ('PRED_WEIGHT_TAXONOMY', 'PRED_WEIGHT_RELATION', 'PRED_WEIGHT_MENTION'),
    'vector_lake.relation_catalog': ('MAX_RELATIONS', 'MAX_DIAGNOSTICS'),
    'vector_lake.tool_graph': ('DEFAULT_MAX_PAGE_EDGES', 'DEFAULT_MAX_CLAIM_EDGES'),
    'vector_lake.tool_relations': ('MAX_DEPTH', 'MAX_RESULTS'),
    'vector_lake.node_vocabulary': ('NODE_TYPES', 'NON_NODE_WIKI_FILES', 'GENERATED_NODE_TYPES'),
}
MAIN_HOSTS = frozenset({'vector_lake.mcp_server', 'vector_lake.watchdog_app', 'vector_lake.indexer'})


def _constant(value):
    if isinstance(value, types.CodeType):
        return ['code', _code_shape(value)]
    if isinstance(value, tuple):
        return ['tuple', [_constant(v) for v in value]]
    if isinstance(value, frozenset):
        return ['frozenset', sorted((_constant(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))]
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, float):
        return ['float', struct.pack('!d', value).hex()]
    if isinstance(value, complex):
        return ['complex', struct.pack('!dd', value.real, value.imag).hex()]
    if value is Ellipsis:
        return ['ellipsis']
    if value is None or isinstance(value, (str, int, bool)):
        return [type(value).__name__, value]
    raise TypeError('Unsupported code constant')


def _code_shape(code):
    # Deliberately ignore file paths, line numbers and tracing tables, not opcode/exception semantics.
    return {'bytecode': code.co_code.hex(), 'exceptiontable': getattr(code, 'co_exceptiontable', b'').hex(),
            'args': [code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount],
            'flags': code.co_flags, 'nlocals': code.co_nlocals, 'stacksize': code.co_stacksize,
            'names': code.co_names, 'varnames': code.co_varnames, 'freevars': code.co_freevars,
            'cellvars': code.co_cellvars, 'qualname': getattr(code, 'co_qualname', code.co_name),
            'constants': [_constant(v) for v in code.co_consts]}


def _code_hash(code):
    return hashlib.sha256(json.dumps(_code_shape(code), sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _literal_constant(node):
    # Only public allowlisted literal assignments, never eval or source execution.
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'frozenset' and len(node.args) == 1 and not node.keywords):
        return frozenset(ast.literal_eval(node.args[0]))
    return ast.literal_eval(node)


def _constant_records(module, tree, names):
    assignments = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    assignments.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in names:
            assignments.setdefault(node.target.id, []).append(node.value)
    records = {}
    for name in names:
        values = assignments.get(name, [])
        if name not in module.__dict__ or not values:
            records[name] = {'status': 'missing'}
            continue
        try:
            if len(values) != 1:
                raise ValueError('Ambiguous constant assignment')
            loaded = _constant(module.__dict__[name])
            current = _constant(_literal_constant(values[0]))
            def digest(value):
                return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            loaded_hash, current_hash = digest(loaded), digest(current)
            records[name] = {'loaded_value_sha256': loaded_hash, 'current_source_value_sha256': current_hash,
                             'matches': loaded_hash == current_hash}
        except (ValueError, TypeError) as exc:
            records[name] = {'status': 'unverifiable', 'error_type': type(exc).__name__}
    return records


def _module_record(name: str, expected_path: Path, functions: tuple[str, ...], constants: tuple[str, ...] = ()) -> dict:
    module = sys.modules.get(name)
    loaded_as = name
    if name in MAIN_HOSTS:
        host = sys.modules.get('__main__')
        origin = host.__dict__.get('__file__') if isinstance(host, types.ModuleType) else None
        if isinstance(origin, str):
            try:
                if Path(origin).resolve() == expected_path.resolve():
                    module, loaded_as = host, '__main__'
            except (OSError, ValueError):
                return {'status': 'source_unverifiable'}
    if module is None:
        return {'status': 'not_loaded'}
    if not isinstance(module, types.ModuleType):
        return {'status': 'source_unverifiable', 'reason': 'Loaded entry is not a module'}
    record = {'loaded_path': str(module.__dict__.get('__file__', '')), 'loaded_as': loaded_as, 'functions': {}}
    try:
        if Path(record['loaded_path']).resolve() != expected_path.resolve():
            return {**record, 'status': 'module_origin_mismatch'}
        with expected_path.open('rb') as handle:
            data = handle.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ValueError('Source exceeds probe budget')
        record['source_sha256'] = hashlib.sha256(data).hexdigest()
        # Diagnostic only: preserve BOM, lone CR, whitespace and every other byte.
        record['source_logical_sha256'] = hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()
        compiled = compile(data, str(expected_path), 'exec', dont_inherit=True, optimize=sys.flags.optimize)
        # Only the top-level functions named in the explicit probe allowlist. No source execution.
        codes = {value.co_name: value for value in compiled.co_consts if isinstance(value, types.CodeType)}
        mismatch = False
        for function in functions:
            candidate = module.__dict__.get(function)
            actual = inspect.unwrap(candidate) if callable(candidate) else None
            if not isinstance(actual, types.FunctionType) or function not in codes:
                record['functions'][function] = {'status': 'missing'}
                mismatch = True
                continue
            loaded = _code_hash(actual.__code__)
            current = _code_hash(codes[function])
            record['functions'][function] = {'loaded_code_sha256': loaded, 'current_source_code_sha256': current, 'matches': loaded == current}
            mismatch |= loaded != current
        if constants:
            record['constants'] = _constant_records(module, ast.parse(data, filename=str(expected_path)), constants)
        constant_records = record.get('constants', {}).values()
        if mismatch:
            status = 'function_code_mismatch'
        elif any(value.get('status') in {'missing', 'unverifiable'} for value in constant_records):
            status = 'constant_unverifiable'
        elif any(not value['matches'] for value in constant_records):
            status = 'constant_mismatch'
        else:
            status = 'matches_function_code'
        return {**record, 'status': status}
    except (OSError, SyntaxError, ValueError, TypeError) as exc:
        return {**record, 'status': 'source_unverifiable', 'error_type': type(exc).__name__}


def _native_record() -> dict:
    core = sys.modules.get('vector_lake_core')
    if core is None:
        return {'status': 'not_loaded'}
    binaries = {}
    for name, module in list(sys.modules.items()):
        if name == 'vector_lake_core' or name.startswith('vector_lake_core.'):
            filename = getattr(module, '__file__', '') or ''
            if any(filename.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES):
                binaries[Path(filename).resolve()] = module
    if len(binaries) != 1:
        return {'status': 'unverifiable', 'reason': 'Expected exactly one loaded core extension origin'}
    path, = binaries
    try:
        # This hashes disk bytes, NOT a claim to hash the already-mapped native image.
        digest = hashlib.sha256()
        total = 0
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                total += len(chunk)
                if total > 256 * 1024 * 1024:
                    raise ValueError('Native binary exceeds probe budget')
                digest.update(chunk)
        return {'status': 'loaded', 'binary_path': str(path), 'version': core.version(), 'on_disk_binary_sha256': digest.hexdigest()}
    except (OSError, AttributeError, TypeError, ValueError, RuntimeError) as exc:
        return {'status': 'unverifiable', 'error_type': type(exc).__name__}


def _checkout_record() -> dict:
    try:
        commit = subprocess.check_output(['git', 'rev-parse', '--verify', 'HEAD'], cwd=PACKAGE.parent, stderr=subprocess.DEVNULL, timeout=1).decode().strip()
        dirty = subprocess.check_output(['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=no'], cwd=PACKAGE.parent, stderr=subprocess.DEVNULL, timeout=1)
        return {'commit': commit, 'tracked_worktree_dirty': bool(dirty), 'scope': 'disk checkout; not a loaded-code identifier; excludes untracked files'}
    except (OSError, subprocess.SubprocessError) as exc:
        return {'status': 'unavailable', 'error_type': type(exc).__name__}


def runtime_identity() -> dict:
    """Observe this caller process without importing unloaded core/application modules or opening a DB."""
    return {'schema_version': 1, 'pid': os.getpid(), 'python': sys.version.split()[0],
            'checkout': _checkout_record(),
            'modules': {name: _module_record(name, PACKAGE / (name.rsplit('.', 1)[1]+'.py'), functions, CONSTANT_PROBES.get(name, ())) for name, functions in MODULE_PROBES.items()},
            'native': _native_record(), 'production_acceptance': 'not_established',
            'scope_limitations': 'Selected unwrapped function code and explicit public literal constants only; excludes decorator wrappers, defaults, other globals, import-time state, rebound dependency references and whole-module equivalence. Known __main__ hosts are recognized by exact source path without importing them. Source logical SHA replaces CRLF with LF only; diagnostic, not canonical/CAS identity. Disk native SHA is not mapped-image SHA. No release binding, health approval or production acceptance. Not one atomic process/filesystem snapshot.'}
