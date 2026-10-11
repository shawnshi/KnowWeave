"""Tests for the tokenizer backend.

``rjieba`` (jieba-rs via PyO3) is the only backend since 2026-09-18; the pure-Python
``jieba`` fallback was removed because the abi3 wheels cover every supported platform
and a second segmentation would differ from rjieba's inside one FTS index.  The
contract that matters now:

* the Rust backend is selected when importable, and ``lcut`` degrades to ``cut``
  because the binding exposes no ``lcut``,
* **a missing backend leaves tokenization ``unavailable``** -- there is no fallback
  to degrade to, so the warning has to say what stops working,
* ``VECTOR_LAKE_TOKENIZER`` still validates against the single valid name: an
  unknown value warns and auto-selection proceeds, and forcing the backend when it
  is unavailable warns and leaves tokenization unavailable rather than pretending,
* the backend identity is part of the search-index cache key (otherwise two
  segmentations would be silently mixed inside one FTS index),
* ``add_word`` reports the lack of a dictionary API instead of pretending success.
"""
import importlib
import importlib.abc
import importlib.machinery
import sys
import types

import pytest

from vector_lake import indexer, tokenizer


class _RustBackend(types.ModuleType):
    """Mirrors the real binding: cut/cut_all/cut_for_search/tag/tokenize only."""

    def cut(self, text, hmm=True):
        return [f"rs:{chunk}" for chunk in text.split()]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_TOKENIZER", raising=False)
    tokenizer.reset_backend_cache()
    yield
    tokenizer.reset_backend_cache()


def _install(monkeypatch, name, module):
    monkeypatch.setitem(sys.modules, name, module)
    tokenizer.reset_backend_cache()
    return module


def _hide(monkeypatch, name):
    """Simulate exact top-level absence, not a broken installed wheel."""

    class _RaisingLoader(importlib.abc.Loader):
        def create_module(self, spec):
            raise ModuleNotFoundError(f"no {name} for this platform", name=name)

        def exec_module(self, module):
            raise ModuleNotFoundError(f"no {name} for this platform", name=name)

    class _RaisingFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == name:
                return importlib.machinery.ModuleSpec(fullname, _RaisingLoader())
            return None

    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_RaisingFinder(), *sys.meta_path])
    tokenizer.reset_backend_cache()


# --- backend selection -----------------------------------------------------


def test_prefers_rjieba_when_importable(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    assert tokenizer.backend_name() == "rjieba"
    assert not tokenizer.cut("医疗")[0].startswith("py:")
    assert tokenizer.split("医疗") == tokenizer.cut("医疗")


def test_rjieba_version_reports_the_crate_in_force(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    monkeypatch.setattr(tokenizer.metadata, "version", lambda name: "0.2.1")
    assert tokenizer.JIEBA_RS_PINNED in tokenizer.backend_version()
    assert "tested-distribution-mapping" in tokenizer.backend_version()
    assert "rjieba" in tokenizer.backend_version()


def test_split_degrades_to_cut_when_lcut_is_missing(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    assert tokenizer.split("医疗") == ["rs:医疗"]
    assert not hasattr(sys.modules["rjieba"], "lcut")


def test_a_missing_backend_is_unavailable_and_says_what_stops(monkeypatch, caplog):
    """There is no fallback any more, so the warning has to state the consequence."""
    _hide(monkeypatch, "rjieba")

    with caplog.at_level("WARNING", logger="vector-lake-tokenizer"):
        assert tokenizer.backend_name() == "unavailable"

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("pre-tokenization is disabled" in m for m in warnings), warnings
    assert tokenizer.cut("医疗") == []
    assert tokenizer.tokenize_joined("医疗") == ""


def test_a_removed_backend_name_is_rejected_as_an_override(monkeypatch, caplog):
    """``jieba`` used to be valid; naming it now warns and auto-selection proceeds."""
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))
    monkeypatch.setenv("VECTOR_LAKE_TOKENIZER", "jieba")
    tokenizer.reset_backend_cache()

    with caplog.at_level("WARNING", logger="vector-lake-tokenizer"):
        assert tokenizer.backend_name() == "rjieba"

    assert any("is not one of" in r.getMessage() for r in caplog.records)


def test_env_override_can_force_rjieba(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))
    monkeypatch.setenv("VECTOR_LAKE_TOKENIZER", "rjieba")
    tokenizer.reset_backend_cache()

    assert tokenizer.backend_name() == "rjieba"


def test_forced_but_missing_backend_warns_and_stays_unavailable(monkeypatch, caplog):
    """Forcing the backend cannot conjure it: the state is unavailable, not a fallback."""
    _hide(monkeypatch, "rjieba")
    monkeypatch.setenv("VECTOR_LAKE_TOKENIZER", "rjieba")
    tokenizer.reset_backend_cache()

    with caplog.at_level("WARNING", logger="vector-lake-tokenizer"):
        assert tokenizer.backend_name() == "unavailable"

    assert [r for r in caplog.records if r.levelname == "WARNING"]
    assert tokenizer.cut("医疗") == []


def test_missing_optional_backend_is_logged_at_debug(monkeypatch, caplog):
    """Auto-selection stays quiet: a fresh install without the wheel is not a warning."""
    _hide(monkeypatch, "rjieba")

    with caplog.at_level("WARNING", logger="vector-lake-tokenizer"):
        assert tokenizer.backend_name() == "unavailable"

    assert not [
        r for r in caplog.records if r.levelname == "WARNING" and "pre-tokenization" not in r.getMessage()
    ]


def test_backend_is_selected_only_once_per_process(monkeypatch, caplog):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    with caplog.at_level("INFO", logger="vector-lake-tokenizer"):
        for _ in range(5):
            tokenizer.backend_name()
            tokenizer.backend_version()
            tokenizer.supports_add_word()
            tokenizer.cut("医疗")

    selections = [r for r in caplog.records if "jieba-rs" in r.getMessage()]
    assert len(selections) == 1, f"backend selection was logged {len(selections)} times"


def test_unknown_env_value_falls_back_to_auto_selection(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))
    monkeypatch.setenv("VECTOR_LAKE_TOKENIZER", "not-a-backend")
    tokenizer.reset_backend_cache()

    assert tokenizer.backend_name() == "rjieba"


# --- index integration -----------------------------------------------------


def test_tokenize_joined_matches_indexer_contract(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    assert indexer._tokenize_for_fts("医疗 数据") == "rs:医疗 rs:数据"
    assert indexer._tokenize_for_fts("") == ""


def test_the_backend_identity_is_part_of_the_index_cache_key(monkeypatch):
    """The reason the key exists: two segmentations must never share one FTS index.

    The backend list has one entry now, so the digest cannot differ between backends
    within a process -- but it must still be *in* the key, because a platform without
    the wheel digests under the ``unavailable`` identity and a later install must force
    re-tokenization rather than reuse an index built by a different tokenizer.
    """
    node = {"title": "T", "summary": "S", "aliases": ["a"]}

    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))
    with_backend = indexer._node_content_digest(node, "body")

    _hide(monkeypatch, "rjieba")
    tokenizer.reset_backend_cache()
    without_backend = indexer._node_content_digest(node, "body")

    assert with_backend != without_backend, "the tokenizer identity must be in the key"


# --- add_word capability reporting ----------------------------------------


def test_add_word_is_unsupported_on_the_rust_backend(monkeypatch, caplog):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    with caplog.at_level("WARNING", logger="vector-lake-tokenizer"):
        assert tokenizer.supports_add_word() is False
        assert tokenizer.add_word("电子病历") is False

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "the limitation must be reported once, not per term"


def test_add_word_is_unsupported_by_every_backend_this_tree_has(monkeypatch):
    """The pure-Python backend that supported it is gone, so nothing supports it."""
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    assert tokenizer.supports_add_word() is False
    assert tokenizer.add_word("电子病历") is False
    assert tokenizer.add_word("") is False


# --- source-level guard ----------------------------------------------------


def test_no_module_imports_a_tokenizer_directly():
    """All tokenization must go through vector_lake.tokenizer."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "vector_lake"
    forbidden = ("jieba", "jieba_fast", "rjieba")
    offenders = []
    for path in root.glob("*.py"):
        if path.name == "tokenizer.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            for name in forbidden:
                if stripped.startswith((f"import {name}", f"from {name} ")):
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert not offenders, f"import the tokenizer module instead: {offenders}"


# --- real backend smoke (only when installed) -----------------------------


def test_real_rjieba_backend_is_usable():
    import os
    import subprocess

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import rjieba; from vector_lake import tokenizer;"
            "text='医疗数据平台与电子病历系统';"
            "assert tokenizer.cut(text)==list(rjieba.cut(text));"
            "assert tokenizer.split(text)==list(rjieba.cut(text));"
            "assert tokenizer.tokenize_joined(text)== ' '.join(rjieba.cut(text));"
            "print(tokenizer.backend_name());"
            "print(' '.join(tokenizer.cut(text)))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=env,
        check=True,
        timeout=20,
    )
    out = result.stdout.splitlines()
    assert out[0] == "rjieba"
    assert "医疗" in out[1] and "电子" in out[1]
