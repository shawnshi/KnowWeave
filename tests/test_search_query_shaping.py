"""Query shaping: the keyword guess, and the cached expansion that must stay pure.

Neither changes ranking, which is why they are here and not in the measured switches.  Both were
"works but for a reason nobody wrote down" before:

* ``_classify_intent`` matched the substring ``"202"``, so a query containing those digits for any
  reason -- an identifier, a version, a price -- was called temporal and its decayed pages escaped
  the relevance penalty.  It is also not a classifier, and the name said it was.
* ``_expand_query_locally`` was ``lru_cache``d *and* mutated global tokenizer state, so the mutation
  happened on a cache miss and not on a hit. Query token caches must now isolate actual tokenizer
  provenance, reset epochs and immutable profiles without ever registering words.
"""

from __future__ import annotations

import pytest

from vector_lake import tool_search


@pytest.mark.parametrize(
    "query,expected",
    [
        ("2026 年 的 政策", "temporal"),
        ("2024", "temporal"),
        ("上周 的 会议", "temporal"),
        ("last week's numbers", "temporal"),
        ("是谁 负责 这个", "entity"),
        ("where is it", "entity"),
        ("简单 的一个 问题", "general"),
    ],
)
def test_the_keyword_guess_still_answers_the_same_way(query, expected):
    assert tool_search._keyword_intent(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "SNOMED 2023",  # a year, and also a version -- temporal either way, kept for contrast
        "HIS2020X",  # digits inside an identifier
        "1.202 kg",  # digits inside a decimal
        "产品 编号 1202",  # contains "202" but is not a year
    ],
)
def test_only_a_year_makes_a_query_temporal(query):
    """``"202"`` was a substring test, so it matched identifiers and decimals too."""
    lowered = query.lower()
    looks_temporal = tool_search._YEAR_PATTERN.search(lowered) is not None
    assert tool_search._keyword_intent(query) == ("temporal" if looks_temporal else "general")


def test_a_year_inside_a_longer_number_is_not_a_year():
    assert tool_search._YEAR_PATTERN.search("1202") is None
    assert tool_search._YEAR_PATTERN.search("20260") is None
    assert tool_search._YEAR_PATTERN.search("2026") is not None


def test_the_cached_expansion_is_pure(isolated_memory):
    """Same input, same output -- no side effect depends on whether the cache was hit."""
    first = tool_search._local_expansions("医院数据平台 信创")
    second = tool_search._local_expansions("医院数据平台 信创")
    assert first == second
    assert isinstance(first, tuple)

    tool_search._local_expansions.cache_clear()
    third = tool_search._local_expansions("医院数据平台 信创")
    assert third == first


def test_the_public_wrapper_hands_out_a_copy(isolated_memory):
    """A caller mutating the result must not corrupt the cache for everyone else."""
    result = tool_search._expand_query_locally("大模型 落地")
    assert isinstance(result, list)
    baseline = list(result)

    result.append("mutated")

    assert tool_search._expand_query_locally("大模型 落地") == baseline


@pytest.mark.parametrize("reserved_query", ["NOT", "AND", "OR", "NEAR(a, b)", "test NOT", '"quoted"'])
def test_fts_query_escaping_reserved_words(isolated_memory, reserved_query):
    """FTS5 reserved words and punctuation do not crash with syntax errors."""
    from vector_lake import db_store
    db_store.init_db()
    hits = tool_search._get_fts_search_results(reserved_query)
    assert isinstance(hits, list)
    assert getattr(tool_search._LAST_FTS_ERROR, "msg", None) is None


@pytest.fixture
def fresh_token_caches():
    from vector_lake import tokenizer

    tokenizer.reset_backend_cache()
    tool_search._local_expansions.cache_clear()
    tool_search._cached_rerank_tokens.cache_clear()
    yield
    tokenizer.reset_backend_cache()
    tool_search._local_expansions.cache_clear()
    tool_search._cached_rerank_tokens.cache_clear()


def test_query_never_registers_words_even_when_backend_supports_it(monkeypatch, fresh_token_caches):
    from vector_lake import tokenizer

    mutations = []
    monkeypatch.setattr(tokenizer, "supports_add_word", lambda: True)
    monkeypatch.setattr(tokenizer, "add_word", lambda word: mutations.append(word))
    if hasattr(tool_search, "_QUERY_TERMS_REGISTERED"):
        monkeypatch.setattr(tool_search, "_QUERY_TERMS_REGISTERED", False)
    before = tokenizer.cache_identity()
    first = tool_search._expand_query_locally("医疗AI")
    assert first == tool_search._expand_query_locally("医疗AI")
    assert tokenizer.cache_identity() == before
    assert mutations == []
    assert not hasattr(tool_search, "_QUERY_TERMS_REGISTERED")
    assert not hasattr(tool_search, "_ensure_query_terms")


def test_public_expansion_is_the_exact_shared_object():
    from vector_lake.domain_dictionary import QUERY_EXPANSION_DICT

    assert tool_search.QUERY_EXPANSION_DICT is QUERY_EXPANSION_DICT
    assert list(QUERY_EXPANSION_DICT.items()) == [
        ("医疗信息化", ["HIT", "卫宁", "电子病历", "医疗IT"]),
        ("大模型", ["LLM", "大语言模型", "Agent", "智能体"]),
        ("医疗AI", ["临床Agent", "大模型医疗落地", "电子病历 智能化"]),
    ]


@pytest.mark.parametrize("query,expected", [
    ("", set()),
    ("THE and LLM", {"llm"}),
    ("医疗信息化", {"hit", "信息化", "医疗", "卫宁", "电子", "病历"}),
    ("大模型", {"agent", "llm", "体", "大", "智能", "模型", "语言"}),
    ("医疗AI", {"agent", "ai", "临床", "医疗", "大", "智能化", "模型", "电子", "病历", "落地"}),
    ("医疗AI 部署", {"agent", "ai", "临床", "医疗", "大", "智能化", "模型", "电子", "病历", "落地", "部署"}),
])
def test_exact_real_default_expansion_golden(query, expected, fresh_token_caches):
    """Golden token sets captured from the dirty baseline's real rjieba0.2.1."""
    from vector_lake import tokenizer

    assert tokenizer.backend_name() == "rjieba"
    assert tokenizer.active_identity().binding_version == "0.2.1"
    assert set(tool_search._local_expansions(query)) == expected
    assert set(tool_search._expand_query_locally(query)) == expected


def test_whitespace_expansion_value_is_passed_whole(monkeypatch, fresh_token_caches):
    from vector_lake import tokenizer

    texts = []
    monkeypatch.setattr(tokenizer, "split", lambda text: texts.append(text) or [text])
    tokens = tool_search._local_expansions("医疗AI")
    assert "电子病历 智能化" in texts
    assert "电子病历 智能化" in tokens
    assert "电子病历" not in texts and "智能化" not in texts


@pytest.mark.parametrize("cache_kind", ['expansion', 'rerank'])
@pytest.mark.parametrize("versions", [("unknown", "unknown"), ("0.2.1", "future-fixture"), ("unknown-a", "unknown-b")])
def test_cache_reset_cannot_reuse_tokens_with_same_backend_or_unknown_version(monkeypatch, fresh_token_caches, versions, cache_kind):
    from types import SimpleNamespace

    from vector_lake import tokenizer

    identities = []
    for version, token in zip(versions, ("alpha", "beta"), strict=True):
        tokenizer.reset_backend_cache()
        monkeypatch.setattr(tokenizer.metadata, "version", lambda name, v=version: v)
        module = SimpleNamespace(cut=lambda text, t=token: [t], lcut=lambda text, t=token: [t])
        monkeypatch.setitem(tokenizer._MODULE_CACHE, "rjieba", module)
        identities.append(tokenizer.cache_identity())
        result = (tool_search._local_expansions("甲乙") if cache_kind == 'expansion'
                  else tool_search._cached_rerank_tokens("甲乙", "rjieba"))
        assert result == (token,)
    assert identities[0] != identities[1]
    if versions[0] == versions[1]:
        assert identities[0][0] == identities[1][0], "only the real reset epoch differentiates unknown provenance"


@pytest.mark.parametrize("cache_kind", ['expansion', 'rerank'])
def test_two_immutable_candidate_profiles_cannot_share_query_tokens(monkeypatch, fresh_token_caches, cache_kind):
    from types import SimpleNamespace

    from vector_lake import tokenizer
    from vector_lake.domain_dictionary import FrozenDomainProfile

    class NativeFixture:
        engine = "jieba-rs"
        engine_version = "0.11.0"
        contract_version = "profile-tokenizer-v1"
        hmm = True
        frequency_policy = "sorted-add-word-default"

        def __init__(self, terms):
            self.terms = terms

        def cut(self, text):
            return [text] if text in self.terms else list(text)

        def cut_joined(self, text, separator=" "):
            return separator.join(self.cut(text))

    monkeypatch.setitem(tokenizer._MODULE_CACHE, "vector_lake_core", SimpleNamespace(ProfileTokenizer=NativeFixture))
    monkeypatch.setattr(tokenizer.metadata, "version", lambda name: "unknown")
    active = tokenizer.cache_identity()
    for terms, expected in [(('甲乙',), ('甲乙',)), (('甲', '乙'), ('甲', '乙'))]:
        handle = tokenizer.build_profile_tokenizer(FrozenDomainProfile("query-fixture", "1", terms))
        # Test-only adapters: candidate creation itself never selects/activates it.
        assert tokenizer.cache_identity() == active
        with monkeypatch.context() as patch:
            patch.setattr(tokenizer, "cache_identity", lambda identity=handle.identity: (identity, active[1]))
            patch.setattr(tokenizer, "split", handle.split)
            patch.setattr(tokenizer, "tokenize_joined", handle.tokenize_joined)
            if cache_kind == 'expansion':
                assert set(tool_search._local_expansions("甲乙")) == set(expected)
            else:
                assert tool_search._cached_rerank_tokens("甲乙", "rjieba") == expected


def test_immutable_expansion_profile_invalidates_both_token_caches(monkeypatch, fresh_token_caches):
    from dataclasses import replace

    from vector_lake import tokenizer
    from vector_lake.domain_dictionary import PUBLIC_QUERY_PROFILE

    calls = []
    monkeypatch.setattr(tokenizer, "split", lambda text: calls.append(('expand', text)) or ['same'])
    monkeypatch.setattr(tokenizer, "tokenize_joined", lambda text: calls.append(('rerank', text)) or 'same')
    for profile in (PUBLIC_QUERY_PROFILE, replace(PUBLIC_QUERY_PROFILE, version="query-test-only-2")):
        monkeypatch.setattr(tool_search, "PUBLIC_QUERY_PROFILE", profile, raising=False)
        assert tool_search._local_expansions("甲乙") == ('same',)
        assert tool_search._cached_rerank_tokens("甲乙", "rjieba") == ('same',)
        assert tool_search._local_expansions("甲乙") == ('same',)
        assert tool_search._cached_rerank_tokens("甲乙", "rjieba") == ('same',)
    assert calls == [('expand', '甲乙'), ('rerank', '甲乙')] * 2


def test_changed_compatibility_expansion_keeps_exact_whitespace(monkeypatch, fresh_token_caches):
    from vector_lake import tokenizer

    monkeypatch.setattr(tokenizer, "split", lambda text: [text])
    for replacement in ('甲 乙', '甲\t乙'):
        monkeypatch.setattr(tool_search, "QUERY_EXPANSION_DICT", {'甲乙': [replacement]})
        assert set(tool_search._local_expansions('甲乙')) == {'甲乙', replacement}


@pytest.mark.parametrize("cache_name,capacity", [('_local_expansions', 128), ('_cached_rerank_tokens', 256)])
def test_identity_aware_cache_bounds_and_clear_compatibility(cache_name, capacity, fresh_token_caches):
    cached = getattr(tool_search, cache_name)
    for index in range(capacity + 3):
        args = (f'fixture {index}',) if cache_name == '_local_expansions' else (f'fixture {index}', 'rjieba')
        cached(*args)
    assert cached.cache_info().maxsize == capacity
    assert cached.cache_info().currsize == capacity
    cached.cache_clear()
    assert cached.cache_info().currsize == 0


@pytest.mark.parametrize("error", [
    ImportError('broken ABI fixture'),
    ModuleNotFoundError('transitive dependency fixture', name='dependency_fixture'),
    ModuleNotFoundError('unattributed missing fixture'),
    OSError('DLL fixture'),
    RuntimeError('native initialization fixture'),
])
def test_query_core_broken_import_is_visible(monkeypatch, error):
    import builtins
    import runpy

    original = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == 'vector_lake_core':
            raise error
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', broken)
    with pytest.raises(type(error), match=str(error)):
        runpy.run_path(tool_search.__file__)


def test_query_core_only_exact_top_level_absence_is_optional(monkeypatch):
    import builtins
    import runpy

    original = builtins.__import__

    def absent(name, *args, **kwargs):
        if name == 'vector_lake_core':
            raise ModuleNotFoundError('absent fixture', name='vector_lake_core')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', absent)
    namespace = runpy.run_path(tool_search.__file__)
    assert namespace['HAVE_CORE'] is False
    assert namespace['vector_lake_core'] is None


@pytest.mark.parametrize("cache_kind", ['expansion', 'rerank'])
def test_tokenizer_fault_is_not_an_empty_result_or_cached(monkeypatch, fresh_token_caches, cache_kind):
    from types import SimpleNamespace

    from vector_lake import tokenizer

    def fail(text):
        raise RuntimeError('tokenizer fixture fault')

    module = SimpleNamespace(cut=fail, lcut=fail)
    monkeypatch.setitem(tokenizer._MODULE_CACHE, 'rjieba', module)
    cached = tool_search._local_expansions if cache_kind == 'expansion' else tool_search._cached_rerank_tokens
    args = ('甲乙',) if cache_kind == 'expansion' else ('甲乙', 'rjieba')
    with pytest.raises(RuntimeError, match='tokenizer fixture fault'):
        cached(*args)
    assert cached.cache_info().currsize == 0
    module.cut = module.lcut = lambda text: ['recovered']
    assert cached(*args) == ('recovered',)


@pytest.mark.parametrize("backend", ['fts5', 'tantivy'])
def test_real_lexical_and_negative_rank_and_same_query_after_reset(isolated_memory, monkeypatch, fresh_token_caches, backend):
    from tests.test_tantivy_index import _drain
    from vector_lake import db_store, tantivy_index, tokenizer

    monkeypatch.setenv('VECTOR_LAKE_FTS', backend)
    tantivy_index.reset()
    try:
        db_store.init_db()
        for key, text in [('only-alpha', 'alpha'), ('only-beta', 'beta'), ('both', 'alpha beta')]:
            db_store.upsert_search_index(key, tokenizer.tokenize_joined(text), '', '')
        if backend == 'tantivy':
            _drain()
        original = tool_search._get_fts_recall_results('alpha beta')
        assert [row['node_key'] for row in original] == ['both']
        assert original[0]['rank'] < 0
        assert tool_search._fts_backend_name() == backend
        identity = tokenizer.active_identity()
        tokenizer.reset_backend_cache()
        assert tokenizer.active_identity() == identity
        assert tool_search._get_fts_recall_results('alpha beta') == original
        assert tool_search._fts_backend_name() == backend
        assert getattr(tool_search._LAST_FTS_ERROR, 'msg', None) is None
    finally:
        tantivy_index.reset()

