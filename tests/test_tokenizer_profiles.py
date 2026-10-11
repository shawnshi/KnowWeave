"""Preparation component contracts; fake native ABI, not real-wheel acceptance."""
import ast
import hashlib
import json
import operator
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from types import ModuleType

import pytest

from vector_lake import indexer, tokenizer
from vector_lake.domain_dictionary import (
    PUBLIC_QUERY_PROFILE,
    QUERY_EXPANSION_DICT,
    FrozenDomainProfile,
)


@dataclass(frozen=True)
class FakeNative:
    terms: tuple[str, ...]
    engine: str = "jieba-rs"
    engine_version: str = "0.11.0"
    contract_version: str = "profile-tokenizer-v1"
    hmm: bool = True
    frequency_policy: str = "sorted-add-word-default"

    def cut(self, text):
        return [text] if text in self.terms else [f"native:{word}" for word in text.split()]

    def cut_joined(self, text, separator=" "):
        return separator.join(self.cut(text))


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_TOKENIZER", raising=False)
    tokenizer.reset_backend_cache()
    yield
    tokenizer.reset_backend_cache()


def install_native(monkeypatch, constructor=FakeNative):
    module = ModuleType("vector_lake_core")
    module.__dict__["ProfileTokenizer"] = constructor
    monkeypatch.setitem(tokenizer._MODULE_CACHE, "vector_lake_core", module)
    return module


def test_public_expansion_exact_legacy_literal_and_seed_snapshot():
    tree = ast.parse((Path(__file__).parents[1] / "vector_lake/tool_search.py").read_text("utf-8"))
    # Before query owner extraction the literal is authoritative. Afterwards the
    # explicit original contract below still protects its compatibility export.
    literals = [node.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "QUERY_EXPANSION_DICT"
                        for target in node.targets) and isinstance(node.value, ast.Dict)]
    original = {
        "医疗信息化": ["HIT", "卫宁", "电子病历", "医疗IT"],
        "大模型": ["LLM", "大语言模型", "Agent", "智能体"],
        "医疗AI": ["临床Agent", "大模型医疗落地", "电子病历 智能化"],
    }
    if literals:
        assert ast.literal_eval(literals[0]) == original
    assert list(QUERY_EXPANSION_DICT.items()) == list(original.items())
    expected = sorted({word for key, values in original.items()
                       for text in [key, *values] for word in text.split()})
    assert PUBLIC_QUERY_PROFILE.terms == tuple(expected)
    assert "电子病历 智能化" not in PUBLIC_QUERY_PROFILE.terms
    assert "电子病历" in PUBLIC_QUERY_PROFILE.terms and "智能化" in PUBLIC_QUERY_PROFILE.terms
    assert PUBLIC_QUERY_PROFILE.name == "public-query-expansion-v1"
    old = QUERY_EXPANSION_DICT["医疗AI"][:]
    try:
        QUERY_EXPANSION_DICT["医疗AI"].append("not-a-seed")
        assert PUBLIC_QUERY_PROFILE.terms == tuple(expected)
    finally:
        QUERY_EXPANSION_DICT["医疗AI"][:] = old


def test_profile_canonical_hash_and_identity_changes():
    source = ["é", "Agent", "agent", "e\u0301", "Agent", "甲"]
    profile = FrozenDomainProfile("synthetic", "1", source)
    assert profile.terms == ("Agent", "agent", "e\u0301", "é", "甲")
    payload = ('{"contract_version":"profile-tokenizer-v1",'
               '"frequency_policy":"sorted-add-word-default","hmm":true,'
               '"name":"synthetic","terms":["Agent","agent","é","é","甲"],'
               '"version":"1","whitespace_policy":"split-unicode-whitespace"}')
    assert profile.profile_hash == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert FrozenDomainProfile("synthetic", "1", reversed(source)).profile_hash == profile.profile_hash
    assert len({profile.profile_hash, replace(profile, name="other").profile_hash,
                replace(profile, version="2").profile_hash,
                replace(profile, terms=("new",)).profile_hash}) == 4
    source.append("mutable")
    assert "mutable" not in profile.terms
    with pytest.raises(FrozenInstanceError):
        setattr(profile, "name", "changed")  # noqa: B010 - intentional frozen-write oracle
    with pytest.raises(TypeError):
        operator.setitem(profile.terms, 0, "changed")  # pyright: ignore[reportCallIssue]
    assert not hasattr(profile, "__dict__")
    assert hash(profile)


@pytest.mark.parametrize("terms,error", [
    ("bare", TypeError), (b"bare", TypeError), (bytearray(b"bare"), TypeError),
    (None, TypeError), ([1], TypeError), ([None], TypeError), ([""], ValueError),
    (["a b"], ValueError), (["a\u2003b"], ValueError), (["x" * 65], ValueError),
    (["x"] * 257, ValueError), (["甲" * 64] * 86, ValueError),
    (["\ud800"], UnicodeEncodeError),
])
def test_profile_rejects_raw_caps_and_malformed_terms(terms, error):
    with pytest.raises(error):
        FrozenDomainProfile("synthetic", "1", terms)


def test_profile_cap_boundaries_and_bounded_generator():
    assert FrozenDomainProfile("synthetic", "1", ()).terms == ()
    assert FrozenDomainProfile("synthetic", "1", ["x" * 64] * 256).terms == ("x" * 64,)
    assert FrozenDomainProfile("synthetic", "1", ["甲" * 64] * 85).terms == ("甲" * 64,)
    count = 0

    def endless():
        nonlocal count
        while True:
            count += 1
            yield "x"

    with pytest.raises(ValueError, match="256"):
        FrozenDomainProfile("synthetic", "1", endless())
    assert count == 257

    class LyingString(str):
        def __len__(self):
            return 1

    with pytest.raises(ValueError):
        FrozenDomainProfile("synthetic", "1", [LyingString("x" * 65)])

    class HostileIterable:
        def __length_hint__(self):
            raise AssertionError("unbounded allocation hint")

        def __iter__(self):
            yield "term"

    assert FrozenDomainProfile("synthetic", "1", HostileIterable()).terms == ("term",)


def test_profile_iterator_errors_are_not_empty_results():
    fault = RuntimeError("synthetic iterator failed")

    def broken():
        yield "valid"
        raise fault

    with pytest.raises(RuntimeError) as caught:
        FrozenDomainProfile("synthetic", "1", broken())
    assert caught.value is fault


@pytest.mark.parametrize("changes,error", [
    ({"name": ""}, ValueError), ({"version": " 1"}, ValueError),
    ({"name": 1}, TypeError), ({"version": None}, TypeError),
    ({"hmm": False}, ValueError), ({"hmm": 1}, ValueError),
    ({"frequency_policy": "weighted"}, ValueError),
    ({"whitespace_policy": "normalize"}, ValueError),
    ({"contract_version": "next"}, ValueError),
    ({"frequency_policy": []}, TypeError), ({"whitespace_policy": None}, TypeError),
    ({"contract_version": 1}, TypeError),
])
def test_profile_rejects_unsupported_contract(changes, error):
    with pytest.raises(error):
        replace(PUBLIC_QUERY_PROFILE, **changes)


def test_candidate_missing_and_old_native_are_distinct(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError("synthetic exact absence", name=name)

    monkeypatch.setattr(tokenizer.importlib, "import_module", missing)
    with pytest.raises(tokenizer.ProfileTokenizerUnavailable) as caught:
        tokenizer.build_profile_tokenizer()
    assert caught.value.reason == "missing_module"
    tokenizer.reset_backend_cache()
    old = ModuleType("vector_lake_core")
    old.__dict__["cut"] = lambda text: ["legacy-core"]
    monkeypatch.setattr(tokenizer.importlib, "import_module", lambda name: old)
    with pytest.raises(tokenizer.ProfileTokenizerUnavailable) as caught:
        tokenizer.build_profile_tokenizer()
    assert caught.value.reason == "unsupported_capability"


@pytest.mark.parametrize("target", ["rjieba", "vector_lake_core"])
@pytest.mark.parametrize("fault", [ImportError("synthetic import failure"),
    ModuleNotFoundError("synthetic dependency absent", name="transitive_dependency"),
    ModuleNotFoundError("synthetic unattributed absence"),
    OSError("synthetic DLL failure"), RuntimeError("synthetic initialization failure")])
def test_broken_imports_propagate_and_are_not_cached(monkeypatch, target, fault):
    def broken(name):
        assert name == target
        raise fault

    monkeypatch.setattr(tokenizer.importlib, "import_module", broken)
    invoke = tokenizer.backend_name if target == "rjieba" else tokenizer.build_profile_tokenizer
    with pytest.raises(type(fault)) as caught:
        invoke()
    assert caught.value is fault
    assert target not in tokenizer._MODULE_CACHE


def test_candidate_constructs_exact_terms_identity_and_readonly_handle(monkeypatch):
    received = []

    def constructor(terms):
        received.append(terms)
        return FakeNative(terms)

    install_native(monkeypatch, constructor)
    monkeypatch.setattr(tokenizer.metadata, "version", lambda name: "synthetic-0.2.3")
    profile = FrozenDomainProfile("synthetic", "1", ["领域词", "a", "领域词"])
    handle = tokenizer.build_profile_tokenizer(profile)
    assert received == [("a", "领域词")]
    assert handle.cut("领域词") == ["领域词"]
    assert handle.split("a b") == ["native:a", "native:b"]
    assert handle.identity.dictionary_hash == profile.profile_hash
    assert handle.identity.profile_name == "synthetic"
    assert handle.identity.profile_version == "1"
    assert handle.identity.binding_version == "synthetic-0.2.3"
    assert handle.identity.engine_version == "0.11.0"
    assert handle.identity.engine_version_source == "native-readonly-metadata"
    assert hash(handle.identity)
    assert not hasattr(handle, "add_word") and not hasattr(handle, "__dict__")
    with pytest.raises(FrozenInstanceError):
        setattr(handle, "identity", None)  # noqa: B010 - intentional frozen-write oracle
    with pytest.raises(FrozenInstanceError):
        setattr(handle.identity, "dictionary_hash", "changed")  # noqa: B010 - intentional frozen-write oracle
    assert tokenizer.build_profile_tokenizer().identity.dictionary_hash == PUBLIC_QUERY_PROFILE.profile_hash
    assert tokenizer.build_profile_tokenizer(replace(profile, name="different")).identity != handle.identity


@pytest.mark.parametrize("profile", [{"terms": []}, ["x"], "name-only"])
def test_factory_rejects_non_profile(profile):
    with pytest.raises(TypeError):
        tokenizer.build_profile_tokenizer(profile)


@pytest.mark.parametrize("text", ["", "a b", "领域词", "甲\u2003乙", "emoji🙂"])
@pytest.mark.parametrize("separator", [" ", "", "|", "\u2003", "界"])
def test_native_joined_separator_forwarding(monkeypatch, text, separator):
    calls = []

    class RecordingNative(FakeNative):
        def cut_joined(self, text, separator=" "):
            calls.append((text, separator))
            return super().cut_joined(text, separator)

    install_native(monkeypatch, RecordingNative)
    handle = tokenizer.build_profile_tokenizer()
    assert handle.tokenize_joined(text, separator) == separator.join(handle.cut(text))
    assert calls == [(text, separator)]


@pytest.mark.parametrize("operation", ["construct", "cut", "split", "tokenize_joined"])
def test_native_faults_visible_without_fallback(monkeypatch, operation):
    fault = OSError("synthetic native fault")

    class BrokenNative(FakeNative):
        def cut(self, text):
            raise fault

        def cut_joined(self, text, separator=" "):
            raise fault

    def broken_constructor(terms):
        raise fault

    install_native(monkeypatch, broken_constructor if operation == "construct" else BrokenNative)
    with pytest.raises(OSError) as caught:
        if operation == "construct":
            tokenizer.build_profile_tokenizer()
        else:
            getattr(tokenizer.build_profile_tokenizer(), operation)("")
    assert caught.value is fault


@pytest.mark.parametrize("field,value", [("engine", "wrong"), ("hmm", False), ("hmm", 1),
    ("frequency_policy", "weighted"), ("contract_version", "old"), ("engine_version", "")])
def test_native_metadata_mismatch_is_visible(monkeypatch, field, value):
    install_native(monkeypatch, lambda terms: replace(FakeNative(terms), **{field: value}))
    with pytest.raises(ValueError):
        tokenizer.build_profile_tokenizer()


@pytest.mark.parametrize("version,expected,source", [
    ("0.2.1", "0.9.x", "tested-distribution-mapping"),
    ("0.9.9", "unknown", "unknown"), (None, "unknown", "unknown"),
])
def test_actual_distribution_provenance_and_test_epoch(monkeypatch, version, expected, source):
    module = ModuleType("rjieba")
    # An arbitrary module version is not distribution provenance.
    module.__dict__["__version__"] = "0.2.1"
    module.__dict__["cut"] = lambda text: [text]
    monkeypatch.setitem(tokenizer._MODULE_CACHE, "rjieba", module)
    calls = []

    def distribution(name):
        calls.append(name)
        if version is None:
            raise tokenizer.metadata.PackageNotFoundError(name)
        return version

    monkeypatch.setattr(tokenizer.metadata, "version", distribution)
    before = tokenizer.cache_identity()
    assert before[0].binding_version == (version or "unknown")
    assert before[0].engine_version == expected
    assert before[0].engine_version_source == source
    assert before == tokenizer.cache_identity() and hash(before)
    assert calls == ["rjieba"]
    tokenizer.reset_backend_cache()
    monkeypatch.setitem(tokenizer._MODULE_CACHE, "rjieba", module)
    after = tokenizer.cache_identity()
    assert before[0] == after[0] and before[1] != after[1]
    assert tokenizer.active_identity() == after[0]


def test_candidate_presence_cannot_select_or_mutate_default_and_digest(monkeypatch):
    legacy = ModuleType("rjieba")
    mutations = []
    legacy.__dict__["cut"] = lambda text: ["old:" + text] if text else []
    legacy.__dict__["add_word"] = mutations.append
    monkeypatch.setitem(tokenizer._MODULE_CACHE, "rjieba", legacy)
    install_native(monkeypatch)
    monkeypatch.setattr(tokenizer.metadata, "version", lambda name: "0.2.1")
    node = {"title": "synthetic", "summary": "public", "aliases": []}
    digest = indexer._node_content_digest(node, "body")
    cache = tokenizer.cache_identity()
    handle = tokenizer.build_profile_tokenizer()
    assert tokenizer.VALID_BACKENDS == ("rjieba",)
    assert tokenizer.backend_name() == "rjieba"
    assert tokenizer.cut("医疗AI") == tokenizer.split("医疗AI") == ["old:医疗AI"]
    assert tokenizer.tokenize_joined("医疗AI", "|") == "old:医疗AI"
    assert handle.cut("医疗AI") == ["医疗AI"]
    assert tokenizer.cache_identity() == cache
    assert indexer._node_content_digest(node, "body") == digest
    assert not tokenizer.supports_add_word() and not tokenizer.add_word("any")
    assert not mutations
    tokenizer.reset_backend_cache()
    monkeypatch.setitem(tokenizer._MODULE_CACHE, "rjieba", legacy)
    monkeypatch.setattr(tokenizer.metadata, "version", lambda name: "9.9")
    assert indexer._node_content_digest(node, "body") == digest
    assert tokenizer.cache_identity()[0] != cache[0]


def test_native_distribution_unknown_and_metadata_failure_visible(monkeypatch):
    install_native(monkeypatch)

    def absent(name):
        raise tokenizer.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(tokenizer.metadata, "version", absent)
    assert tokenizer.build_profile_tokenizer().identity.binding_version == "unknown"
    fault = OSError("synthetic broken distribution metadata")

    def broken(name):
        raise fault

    monkeypatch.setattr(tokenizer.metadata, "version", broken)
    with pytest.raises(OSError) as caught:
        tokenizer.build_profile_tokenizer()
    assert caught.value is fault


def test_missing_active_cache_identity_has_no_distribution_lookup(monkeypatch):
    def absent(name):
        raise ModuleNotFoundError("synthetic absence", name=name)

    def forbidden(name):
        raise AssertionError("unavailable is not a distribution")

    monkeypatch.setattr(tokenizer.importlib, "import_module", absent)
    monkeypatch.setattr(tokenizer.metadata, "version", forbidden)
    identity, epoch = tokenizer.cache_identity()
    assert identity.backend == "unavailable" and identity.binding_version == "unknown"
    assert identity.engine_version == "unknown"
    assert tokenizer.cache_identity() == (identity, epoch)


def test_fresh_default_never_imports_native_and_matches_real_provenance():
    code = '''
import importlib.abc, json, sys
from importlib import metadata
class RejectNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "vector_lake_core":
            raise AssertionError("default must not import optional native candidate")
sys.meta_path.insert(0, RejectNative())
from vector_lake import tokenizer
import rjieba
identity = tokenizer.active_identity()
assert identity.binding_version == metadata.version("rjieba")
for text in ("", "医疗数据平台与电子病历系统", "Concept_1 - 0", "甲\\u2003乙", "emoji🙂"):
    assert tokenizer.cut(text) == list(rjieba.cut(text))
    assert tokenizer.split(text) == list(rjieba.cut(text))
    assert tokenizer.tokenize_joined(text, "|") == "|".join(rjieba.cut(text))
assert tokenizer.backend_name() == "rjieba"
assert "vector_lake_core" not in sys.modules
print(json.dumps({"backend":identity.backend,"distribution":identity.binding_version}))
'''
    child = subprocess.run([sys.executable, "-B", "-s", "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="strict", check=True, timeout=20,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    output = json.loads(child.stdout)
    assert output["backend"] == "rjieba"
    assert output["distribution"]
