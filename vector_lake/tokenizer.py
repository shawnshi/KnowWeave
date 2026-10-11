"""Single tokenizer entry point for FTS pre-processing.

Backends
--------
``rjieba`` (the only backend)
    Rust implementation of jieba (``jieba-rs``) exposed through the official
    PyO3 binding by the same author (messense).  Ships ``cp38-abi3`` wheels for
    Windows, macOS, manylinux and musllinux, so it installs on CPython 3.8+
    without a compiler, and it measured ~7-15x faster than the pure-Python
    implementation this project used to fall back to.

The pure-Python ``jieba`` fallback was removed on 2026-09-18: the abi3 wheels
cover every platform this project supports, so the fallback was only reachable
outside that set, while carrying a second segmentation whose token stream differs
from ``rjieba``'s -- the one thing the search-index cache key exists to keep out of
a single FTS index.  On a platform with no ``rjieba`` wheel, tokenization is now
``unavailable``: CJK pre-tokenization is skipped and CJK queries match less,
which ``doctor`` and :func:`backend_name` report rather than hide.

Why the layering is observable
------------------------------
Every tokenization call goes through this module so that:

* the active backend is inspectable (:func:`backend_name`, :func:`backend_version`),
* the legacy search-index digest includes the backend NAME, not its version or
  dictionary identity (see ``indexer._node_content_digest``); candidate profiles
  are offline preparation only and cannot safely activate under that digest,
* ``VECTOR_LAKE_TOKENIZER`` is still accepted (the single valid value is
  ``rjieba``), so an existing environment file states its intent explicitly; an
  unavailable forced backend warns and tokenization goes ``unavailable``.

Known limitation
----------------
``rjieba`` does **not** expose ``add_word`` / ``load_userdict`` (neither at module
level nor on its ``Jieba`` class), and jieba-rs embeds its own dictionary, so
``QUERY_EXPANSION_DICT`` terms cannot be registered at all now that the pure-Python
backend is gone.  :func:`add_word` reports that instead of pretending success; see
:func:`supports_add_word`.  Indexing and querying use the same tokenizer, so recall
is unaffected apart from the exact phrase forms of those terms.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
from dataclasses import dataclass, field
from importlib import metadata
from types import ModuleType
from typing import Protocol

from .domain_dictionary import PUBLIC_QUERY_PROFILE, FrozenDomainProfile

log = logging.getLogger("vector-lake-tokenizer")

# The Rust implementation is the only backend; see the module docstring for why the
# pure-Python fallback was removed.  Kept as a tuple because the selection logic and
# ``VECTOR_LAKE_TOKENIZER`` validation read it as the set of valid names.
VALID_BACKENDS = ("rjieba",)

# Declared mapping ONLY for the tested rjieba 0.2.1 distribution; the crate is
# not runtime-discoverable. Other distribution versions must report unknown.
# Its Cargo.toml pins `jieba-rs = "0.9.0"` (verified from that release's sdist).
#
# 2026-09-25: the pin is no longer stuck on upstream's release schedule --
# `crates/vector_lake_core` now depends on `jieba-rs = "0.11"` itself and exposes
# `cut` / `cut_joined` with rjieba's call shape, so this constant still describes
# the backend that is in force (rjieba) but 0.11 is buildable in-tree.  Measured
# parity over 250 pages / 500 strings: the CJK token stream is identical
# (500/500, 0 differing positions), while punctuation/ASCII runs change
# (`Concept_1 - 0` -> `Concept_1-0`).  Switching the backend is therefore a
# punctuation-level change requiring a full lexical identity/rebuild gate.
# The existing production digest does not include this version.
JIEBA_RS_PINNED = "0.9.x"
RJIEBA_TESTED_VERSION = "0.2.1"

_MODULE_CACHE: dict[str, ModuleType | None] = {}
_LOCK = threading.Lock()
_RESOLVED: dict[str, str] = {}
_ADD_WORD_WARNED = False
_TEST_RESET_EPOCH = 0
_ACTIVE_IDENTITY: TokenizerIdentity | None = None


def _load(module_name: str, *, expected: bool = False):
    """Only exact top-level module absence is optional; broken imports propagate."""
    if module_name in _MODULE_CACHE:
        return _MODULE_CACHE[module_name]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        log_fn = log.warning if expected else log.debug
        log_fn("Tokenizer backend %r unavailable: %s: %s", module_name, type(exc).__name__, exc)
        _MODULE_CACHE[module_name] = None
        return None
    _MODULE_CACHE[module_name] = module
    return module


def _select():
    """Resolve the active backend once per process."""
    cached_name = _RESOLVED.get("name")
    if cached_name:
        return cached_name, _MODULE_CACHE.get(cached_name)
    with _LOCK:
        cached_name = _RESOLVED.get("name")
        if cached_name:
            return cached_name, _MODULE_CACHE.get(cached_name)
        forced = (os.environ.get("VECTOR_LAKE_TOKENIZER") or "").strip().lower()
        if forced and forced not in VALID_BACKENDS:
            log.warning(
                "VECTOR_LAKE_TOKENIZER=%r is not one of %s; falling back to auto-selection.",
                forced,
                list(VALID_BACKENDS),
            )
            forced = ""
        order = ([forced] if forced else []) + [name for name in VALID_BACKENDS if name != forced]
        for name in order:
            module = _load(name, expected=bool(forced) and name == forced)
            if module is not None:
                if name == "rjieba":
                    log.info("Tokenizer backend: rjieba (jieba-rs via Rust extension; see backend_version for provenance)")
                _RESOLVED["name"] = name
                return name, module
        log.warning(
            "No CJK tokenizer backend is importable (rjieba has no wheel for this platform?); "
            "FTS pre-tokenization is disabled, so CJK queries will match less. "
            "Install rjieba, or run without CJK full-text search."
        )
        _RESOLVED["name"] = "unavailable"
        return "unavailable", None


def reset_backend_cache() -> None:
    """Test-only reset; NOT a production switch or persistent index migration."""
    global _ADD_WORD_WARNED, _TEST_RESET_EPOCH, _ACTIVE_IDENTITY
    with _LOCK:
        _MODULE_CACHE.clear()
        _RESOLVED.clear()
        _ADD_WORD_WARNED = False
        _TEST_RESET_EPOCH += 1
        _ACTIVE_IDENTITY = None


def backend_name() -> str:
    """Active backend: ``rjieba``, or ``unavailable`` when it cannot be imported."""
    name, _ = _select()
    return name


def backend_version() -> str:
    """Actual binding distribution and declared crate mapping (or unknown)."""
    name, module = _select()
    if module is None:
        return "unavailable"
    identity = active_identity()
    return f"{name} {identity.binding_version} ({identity.engine} {identity.engine_version}; {identity.engine_version_source})"


def supports_add_word() -> bool:
    """Production and prepared profile dictionaries cannot be mutated."""
    return False


def cut(text: str) -> list[str]:
    """Segment ``text``; empty input or no backend yields an empty list."""
    if not text:
        return []
    _, module = _select()
    if module is None:
        return []
    return list(module.cut(text))


def tokenize_joined(text: str, separator: str = " ") -> str:
    """Segment and rejoin, the form the FTS5 pre-tokenization step needs."""
    return separator.join(cut(text))


def split(text: str) -> list[str]:
    """Like :func:`cut` but using the backend's ``lcut`` when available."""
    if not text:
        return []
    _, module = _select()
    if module is None:
        return []
    lcut = getattr(module, "lcut", None)
    if callable(lcut):
        return list(lcut(text))
    return list(module.cut(text))


def add_word(term: str) -> bool:
    """Compatibility no-op, returning False without mutating any dictionary.

    Custom terms belong in an independent construction-time candidate profile,
    never in the active production tokenizer or an already constructed instance.
    """
    global _ADD_WORD_WARNED
    if not term:
        return False
    if not _ADD_WORD_WARNED:
        log.warning(
            "Active tokenizer backend %r has no mutable dictionary contract; custom terms are ignored.",
            backend_name(),
        )
        _ADD_WORD_WARNED = True
    return False


@dataclass(frozen=True, slots=True)
class TokenizerIdentity:
    """Runtime provenance and tokenization contract, not an index identity."""

    backend: str
    binding_version: str
    engine: str
    engine_version: str
    engine_version_source: str
    contract_version: str
    hmm: bool
    frequency_policy: str
    whitespace_policy: str
    profile_name: str
    profile_version: str
    dictionary_hash: str


def _distribution_version(name: str) -> str:
    # Query only the requested binding, never enumerate installed distributions.
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def active_identity() -> TokenizerIdentity:
    """Actual fixed active backend; never includes the test-reset epoch."""
    global _ACTIVE_IDENTITY
    if _ACTIVE_IDENTITY is None:
        name, module = _select()
        version = _distribution_version(name) if module is not None else "unknown"
        mapped = name == "rjieba" and version == RJIEBA_TESTED_VERSION
        _ACTIVE_IDENTITY = TokenizerIdentity(
            backend=name, binding_version=version,
            engine="jieba-rs" if module is not None else "unavailable",
            engine_version=JIEBA_RS_PINNED if mapped else "unknown",
            engine_version_source="tested-distribution-mapping" if mapped else "unknown",
            contract_version="legacy-rjieba-v1", hmm=True,
            frequency_policy="embedded-default", whitespace_policy="legacy-backend",
            profile_name="legacy-default", profile_version="unversioned",
            dictionary_hash="unknown",
        )
    return _ACTIVE_IDENTITY


def cache_identity() -> tuple[TokenizerIdentity, int]:
    """Hashable process-local cache key; must not be used as a durable digest."""
    return active_identity(), _TEST_RESET_EPOCH


class ProfileTokenizerUnavailable(RuntimeError):
    """Optional candidate is missing or lacks the required immutable capability."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Profile tokenizer unavailable: {reason}")


class _ProfileNative(Protocol):
    def cut(self, text: str) -> list[str]: ...
    def cut_joined(self, text: str, separator: str = " ") -> str: ...


@dataclass(frozen=True, slots=True)
class ProfileTokenizerHandle:
    """An independent immutable candidate; constructing it never changes selection."""

    identity: TokenizerIdentity
    _native: _ProfileNative = field(repr=False, compare=False)

    def cut(self, text: str) -> list[str]:
        return list(self._native.cut(text))

    def split(self, text: str) -> list[str]:
        return self.cut(text)

    def tokenize_joined(self, text: str, separator: str = " ") -> str:
        return self._native.cut_joined(text, separator)


def build_profile_tokenizer(profile: FrozenDomainProfile | None = None) -> ProfileTokenizerHandle:
    """Explicit offline candidate; no activation and no no-domain fallback."""
    if profile is None:
        profile = PUBLIC_QUERY_PROFILE
    if not isinstance(profile, FrozenDomainProfile):
        raise TypeError("profile must be a FrozenDomainProfile")
    module = _load("vector_lake_core")
    if module is None:
        raise ProfileTokenizerUnavailable("missing_module")
    constructor = getattr(module, "ProfileTokenizer", None)
    if constructor is None:
        raise ProfileTokenizerUnavailable("unsupported_capability")
    native = constructor(profile.terms)
    for name, expected in (
        ("engine", "jieba-rs"), ("contract_version", profile.contract_version),
        ("hmm", profile.hmm), ("frequency_policy", profile.frequency_policy),
    ):
        actual = getattr(native, name)
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError(f"native profile contract mismatch: {name}")
    engine_version = native.engine_version
    if not isinstance(engine_version, str) or not engine_version:
        raise ValueError("native engine version must be a nonempty string")
    if not callable(native.cut) or not callable(native.cut_joined):
        raise TypeError("native profile must implement cut and cut_joined")
    identity = TokenizerIdentity(
        backend="vector_lake_core", binding_version=_distribution_version("vector-lake-core"),
        engine=native.engine, engine_version=engine_version,
        engine_version_source="native-readonly-metadata", contract_version=profile.contract_version,
        hmm=profile.hmm, frequency_policy=profile.frequency_policy,
        whitespace_policy=profile.whitespace_policy, profile_name=profile.name,
        profile_version=profile.version, dictionary_hash=profile.profile_hash,
    )
    return ProfileTokenizerHandle(identity=identity, _native=native)
