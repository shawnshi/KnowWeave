"""Native dispatch must preserve the substring scorer and gram-format contract."""
from __future__ import annotations

import math
import random
import re
import sqlite3

import pytest

from vector_lake import memory_gram_index as gram


def _reference_grams(*blobs: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for bit, blob in zip((1, 2, 4), blobs):
        if not blob:
            continue
        distinct = set(blob)
        distinct.update(blob[i:i + 2] for i in range(len(blob) - 1))
        distinct.update(re.findall(r"[0-9a-z]{3,}", blob))
        for token in distinct:
            result[token] = result.get(token, 0) | bit
    return result


@pytest.fixture
def postings_db(monkeypatch):
    docs = {
        1: ("研究a", "研究数据ab", "source_a"),
        2: ("数据b", "研究方法abc", "source_b"),
        3: ("索引c", "数据结构abc", "source_c"),
        4: ("研究d", "ab研究数据", "source_d"),
    }
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE operational_memory_gram(gram TEXT PRIMARY KEY, postings BLOB)")
    conn.execute("CREATE TABLE operational_memory_index(memory_id TEXT, key_blob TEXT, text_blob TEXT, page_blob TEXT)")
    # A posting names a document number, and this table is what that number means; the projection's
    # row id is a physical address and is deliberately not used as identity.
    conn.execute("CREATE TABLE operational_memory_gram_docs(doc INTEGER PRIMARY KEY, memory_id TEXT NOT NULL UNIQUE)")
    postings: dict[str, list[tuple[int, int]]] = {}
    for doc, blobs in docs.items():
        conn.execute("INSERT INTO operational_memory_index(rowid, memory_id, key_blob, text_blob, page_blob) VALUES (?, ?, ?, ?, ?)", (doc, f"mem_{doc}", *blobs))
        conn.execute("INSERT INTO operational_memory_gram_docs(doc, memory_id) VALUES (?, ?)", (doc, f"mem_{doc}"))
        for token, mask in _reference_grams(*blobs).items():
            postings.setdefault(token, []).append((doc, mask))
    conn.executemany("INSERT INTO operational_memory_gram VALUES (?, ?)",
                     [(token, gram._pack(entries)) for token, entries in postings.items()])
    monkeypatch.setattr(gram, "get_connection", lambda: conn)
    monkeypatch.setattr(gram, "gram_index_state", lambda: {"base_docs": len(docs)})
    yield docs
    conn.close()


def _oracle(docs, terms, skip=None, df=None):
    # This oracle scores substrings directly; it never decodes the index under test.
    live = {doc: fields for doc, fields in docs.items() if doc not in (skip or set())}
    frequencies = df if df is not None else {
        term: sum(any(term in field for field in fields) for fields in live.values())
        for term in terms if term
    }
    scores = {}
    for term in terms:
        if not term:
            continue
        count = frequencies.get(term, 0)
        factor = math.log(1 + len(docs) / count) if count > 0 else 0.0
        for doc, fields in live.items():
            weight = sum(w for w, field in zip((4, 3, 1), fields) if term in field)
            if weight:
                scores[doc] = scores.get(doc, 0.0) + factor * weight
    return scores


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("terms,skip", [
    ([], set()),
    ([None, "", "研究"], set()),
    (["", "不存在"], set()),
    (["研", "研究", "数据", "研", "ab", "z"], {2}),
    (["a", "ab", "数据"], {1, 2, 3, 4}),
    (["abc", "数据", "研"], set()),
    (["ab", "数据", "abc", "研"], set()),
])
def test_scores_match_direct_substring_oracle(postings_db, monkeypatch, native, terms, skip):
    if native and not gram.HAVE_CORE:
        pytest.skip("Native backend is tested in the separate required native lane")
    monkeypatch.setattr(gram, "HAVE_CORE", native)
    actual = gram.accumulate_relevance(terms, skip_docs=skip)
    assert actual == pytest.approx(_oracle(postings_db, terms, skip), abs=1e-12)


def test_native_batch_scoring_is_used(postings_db, monkeypatch):
    if not gram.HAVE_CORE:
        pytest.skip("Requires the native lane")
    original = gram.vector_lake_core.fast_accumulate_terms
    calls = []

    def batch(*args):
        calls.append(args)
        return original(*args)

    def no_scalar(*args, **kwargs):
        pytest.fail("A short-term batch must not return to the scalar Python fold")

    monkeypatch.setattr(gram.vector_lake_core, "fast_accumulate_terms", batch)
    monkeypatch.setattr(gram, "_accumulate", no_scalar)
    terms = ["研", "研究", "研", "数据", "ab", "z", ""]
    frequencies = {"研": 3, "研究": 3, "数据": 3, "ab": 4}
    scores = gram.accumulate_relevance(terms, skip_docs={2}, df=frequencies)
    assert scores == pytest.approx(_oracle(postings_db, terms, {2}, frequencies), abs=1e-12)
    assert len(calls) == 1
    assert [term for term, _ in calls[0][0]] == ["研", "研究", "研", "数据", "ab"]


def test_zero_df_keeps_existing_zero_score_documents(postings_db):
    scores = gram.accumulate_relevance(["研究"], df={"研究": 0})
    assert scores == {1: 0.0, 2: 0.0, 4: 0.0}


def test_mixed_terms_keep_scalar_path(postings_db, monkeypatch):
    if gram.HAVE_CORE:
        def no_batch(*args, **kwargs):
            pytest.fail("Mixed-term reduction keeps its existing ordering contract")
        monkeypatch.setattr(gram.vector_lake_core, "fast_accumulate_terms", no_batch)
    terms = ["数据", "abc", "研究"]
    assert gram.accumulate_relevance(terms) == pytest.approx(_oracle(postings_db, terms), abs=1e-12)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("blobs", [
    ("", "", ""),
    (None, "数据", ""),
    ("研究abc123", "数据abc123_研究", "source_synthetic"),
    ("ABCdef", "data_42", "Source_SYNTHETIC"),
    ("éßσ", "中𐐨é\n123", "数据"),
    ("ab\ud800c", "\udfff数据", "abc"),
])
def test_extraction_preserves_gram_contract(monkeypatch, native, blobs):
    if native and not gram.HAVE_CORE:
        pytest.skip("Requires the native lane")
    monkeypatch.setattr(gram, "HAVE_CORE", native)
    actual = gram.extract_grams(*blobs)
    expected = _reference_grams(*blobs)
    assert actual == expected
    # Persisted postings do not depend on dictionary insertion order.
    assert {token: gram._pack([(17, mask)]) for token, mask in actual.items()} == {
        token: gram._pack([(17, mask)]) for token, mask in expected.items()
    }


def test_native_extraction_is_used(monkeypatch):
    if not gram.HAVE_CORE:
        pytest.skip("Requires the native lane")
    original = gram.vector_lake_core.extract_grams
    calls = []

    def extract(*blobs):
        calls.append(blobs)
        return original(*blobs)

    monkeypatch.setattr(gram.vector_lake_core, "extract_grams", extract)
    blobs = ("研究abc123", "研究数据_abc", "source_synthetic")
    assert gram.extract_grams(*blobs) == _reference_grams(*blobs)
    assert calls == [blobs]
    assert gram.extract_grams("ABCdef", "", "") == _reference_grams("ABCdef", "", "")
    assert gram.extract_grams("\ud800", "", "") == _reference_grams("\ud800", "", "")
    assert calls == [blobs]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("safe_text", [False, True])
def test_randomized_extraction_contract(monkeypatch, native, safe_text):
    if native and not gram.HAVE_CORE:
        pytest.skip("Requires the native lane")
    monkeypatch.setattr(gram, "HAVE_CORE", native)
    rng = random.Random(17)
    alphabet = "研究 abc0123_!?\nßσé中𐐨" if safe_text else "研究 ABCabc0123_!?\nßẞΣé中𐐀\ud800"
    for _ in range(200):
        blobs = tuple("".join(rng.choice(alphabet) for _ in range(rng.randrange(100))) for _ in range(3))
        assert gram.extract_grams(*blobs) == _reference_grams(*blobs)


@pytest.mark.parametrize("native", [False, True])
def test_randomized_scores_match_substring_oracle(postings_db, monkeypatch, native):
    if native and not gram.HAVE_CORE:
        pytest.skip("Requires the native lane")
    monkeypatch.setattr(gram, "HAVE_CORE", native)
    rng = random.Random(29)
    vocabulary = ["研", "研究", "数据", "ab", "a", "b", "z", ""]
    for _ in range(100):
        terms = rng.choices(vocabulary, k=rng.randrange(20))
        skip = {doc for doc in postings_db if rng.randrange(3) == 0}
        assert gram.accumulate_relevance(terms, skip_docs=skip) == pytest.approx(
            _oracle(postings_db, terms, skip), abs=1e-12
        )


def test_native_errors_are_not_silenced(postings_db, monkeypatch):
    if not gram.HAVE_CORE:
        pytest.skip("Requires the native lane")

    def fail(*args, **kwargs):
        raise ValueError("injected native failure")

    monkeypatch.setattr(gram.vector_lake_core, "fast_accumulate_terms", fail)
    monkeypatch.setattr(gram.vector_lake_core, "extract_grams", fail)
    with pytest.raises(ValueError, match="injected native failure"):
        gram.accumulate_relevance(["研究"], df={"研究": 3})
    with pytest.raises(ValueError, match="injected native failure"):
        gram.extract_grams("研究", "数据", "")


def test_native_fast_fused_frequencies_and_relevance_matches_oracle(postings_db):
    """The fused Rust operator must calculate exact DF and exact relevance matching oracle."""
    if not gram.HAVE_CORE or not hasattr(gram.vector_lake_core, "fast_fused_frequencies_and_relevance"):
        pytest.skip("Requires fast_fused_frequencies_and_relevance in native core")

    conn = gram.get_connection()
    terms = ["研", "研究", "数据", "ab", "z", "研"]
    term_blobs = [(t, gram._term_base(t, conn)) for t in terms if gram._term_base(t, conn) is not None]
    total = len(postings_db)

    # 1. Unscoped test
    df_rust, rel_rust = gram.vector_lake_core.fast_fused_frequencies_and_relevance(
        term_blobs, total, None, None, list(gram.FIELD_WEIGHTS)
    )
    df_expected = gram.term_document_frequencies(terms)
    rel_expected = _oracle(postings_db, terms)

    assert df_rust == {t: df_expected[t] for t in df_rust}
    assert rel_rust == pytest.approx(rel_expected, abs=1e-12)

    # 2. Scoped + Skip test
    skip = {2}
    scope = {1, 3, 4}
    df_scoped, rel_scoped = gram.vector_lake_core.fast_fused_frequencies_and_relevance(
        term_blobs, len(scope), skip, scope, list(gram.FIELD_WEIGHTS)
    )
    df_scope_expected = gram.term_document_frequencies(terms, skip_docs=skip, docs=scope)
    rel_scope_expected = gram.accumulate_relevance(
        terms, skip_docs=skip, total_docs=len(scope), df=df_scope_expected, docs=scope
    )

    assert df_scoped == {t: df_scope_expected[t] for t in df_scoped}
    assert rel_scoped == pytest.approx(rel_scope_expected, abs=1e-12)

