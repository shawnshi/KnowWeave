"""Task-new-wheel mandatory contract; default selection is never replaced globally."""
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
from dataclasses import FrozenInstanceError

import pytest

from vector_lake import db_store as db
from vector_lake import tantivy_index as mirror
from vector_lake import tokenizer
from vector_lake.domain_dictionary import PUBLIC_QUERY_PROFILE, FrozenDomainProfile

try:
    core = importlib.import_module('vector_lake_core')
except ModuleNotFoundError as exc:
    if exc.name != 'vector_lake_core' or os.environ.get('VL_CI_CORE_BACKEND') == 'native':
        raise
    pytest.skip('profile integration requires the optional native module', allow_module_level=True)


def test_real_new_binding_identity_and_immutable_surface():
    before = tokenizer.cache_identity()
    profile = tokenizer.build_profile_tokenizer()
    # A deployment may deliberately run a build other than the working tree's version
    # (versioned installs and rollback both do), so this pins identity, not a literal:
    # the binding must report a real version, and when it is the installed build it must
    # agree with the installed distribution metadata.
    assert re.fullmatch(r'\d+\.\d+\.\d+', profile.identity.binding_version), profile.identity.binding_version
    implementation = Path(sys.modules['vector_lake_core'].__file__).resolve().parent
    if 'site-packages' in implementation.parts:
        assert profile.identity.binding_version == importlib.metadata.version('vector-lake-core')
    assert profile.identity.engine_version == '0.11.0'
    assert profile.identity.dictionary_hash == PUBLIC_QUERY_PROFILE.profile_hash
    assert profile.identity.profile_name == 'public-query-expansion-v1'
    for obj, name, value in [(profile, 'identity', None), (profile.identity, 'dictionary_hash', 'bad')]:
        with pytest.raises((FrozenInstanceError, AttributeError)):
            setattr(obj, name, value)
    native = core.ProfileTokenizer(PUBLIC_QUERY_PROFILE.terms)
    for name in ['engine', 'engine_version', 'contract_version', 'hmm', 'frequency_policy']:
        with pytest.raises(AttributeError):
            setattr(native, name, 'changed')
    assert not hasattr(native, 'add_word') and not hasattr(native, '__dict__')
    assert tokenizer.cache_identity() == before
    assert tokenizer.backend_name() == 'rjieba'


@pytest.mark.parametrize('text', ['', '电子病历', '医保DRG支付', 'Concept_1 - 0,ASCII12.3!?', '甲\t乙\n丙😀', '医疗AI 临床Agent'])
@pytest.mark.parametrize('separator', ['', ' ', '|', '分隔\t'])
def test_real_joined_all_streams(text, separator):
    handle = tokenizer.build_profile_tokenizer()
    independent = core.ProfileTokenizer(())
    assert handle.tokenize_joined(text, separator) == separator.join(handle.cut(text))
    assert independent.cut_joined(text, separator) == separator.join(independent.cut(text))
    assert core.cut_joined(text) == ' '.join(core.cut(text))
    with pytest.raises(TypeError):
        core.cut_joined(text, separator=separator)
    assert tokenizer.tokenize_joined(text, separator) == separator.join(tokenizer.cut(text))


def test_real_canonical_order_duplicates_hash_and_seed_effect():
    terms = list(PUBLIC_QUERY_PROFILE.terms)
    a = FrozenDomainProfile('same', '1', terms)
    b = FrozenDomainProfile('same', '1', list(reversed(terms)) + terms)
    assert a.profile_hash == b.profile_hash
    # Independent canonical JSON vector, not just two calls to the same implementation.
    payload = {'name': 'same', 'version': '1', 'terms': sorted(set(terms)), 'hmm': True,
               'frequency_policy': 'sorted-add-word-default', 'whitespace_policy': 'split-unicode-whitespace',
               'contract_version': 'profile-tokenizer-v1'}
    assert a.profile_hash == hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    native_a = core.ProfileTokenizer(terms)
    native_b = core.ProfileTokenizer(list(reversed(terms)) + terms)
    assert native_a.cut('电子病历 医疗AI 临床Agent') == native_b.cut('电子病历 医疗AI 临床Agent')
    assert native_a.cut('电子病历') == ['电子病历']
    assert core.cut('电子病历') == ['电子', '病历']
    handle = tokenizer.build_profile_tokenizer(a)
    assert handle.cut('电子病历') == ['电子病历']
    changed = FrozenDomainProfile('same', '1', terms + ['合成未知词项'])
    assert changed.profile_hash != a.profile_hash
    original = core.cut('电子病历')
    terms.clear()
    assert native_a.cut('电子病历') == ['电子病历'] and core.cut('电子病历') == original


@pytest.mark.parametrize('terms, error', [('bad', TypeError), ([1], TypeError), ([''], ValueError), (['甲 乙'], ValueError), (['x'] * 257, ValueError), (['甲' * 65], ValueError), (['\ud800'], UnicodeEncodeError)])
def test_real_native_bad_input_has_no_partial_object(terms, error):
    before = core.cut('电子病历')
    with pytest.raises(error):
        core.ProfileTokenizer(terms)
    assert core.cut('电子病历') == before


@pytest.mark.parametrize('scope', ['default', 'candidate'])
@pytest.mark.parametrize('terms', [('alpha', 'beta'), ('电子病历', '医疗信息化')])
def test_real_stream_to_sqlite_and_durable_tantivy(isolated_memory, monkeypatch, scope, terms):
    from tests.test_tantivy_index import _drain

    engine = tokenizer if scope == 'default' else tokenizer.build_profile_tokenizer()
    default = tokenizer.cache_identity()
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'tantivy')
    mirror.reset()
    try:
        db.init_db()
        a, b = terms
        for key, text in [('a-only', a), ('b-only', b), ('both', a + ' ' + b)]:
            db.upsert_search_index(key, engine.tokenize_joined(text), '', '')
        query = engine.tokenize_joined(a + ' ' + b)
        tokens = query.split()
        monkeypatch.setenv('VECTOR_LAKE_FTS', 'fts5')
        if scope == 'default':
            sql_rows = db.search_wiki(query)
        else:
            # Candidate identity is not active: the production entry re-tokenizes as rjieba.
            match = ' '.join('"' + token.replace('"', '""') + '"' for token in tokens)
            sql_rows = [dict(row) for row in db.get_connection().execute(
                'SELECT node_key, bm25(wiki_search_index) AS rank FROM wiki_search_index '
                'WHERE wiki_search_index MATCH ? ORDER BY rank', (match,))]
        assert [row['node_key'] for row in sql_rows] == ['both']
        assert sql_rows[0]['rank'] < 0
        monkeypatch.setenv('VECTOR_LAKE_FTS', 'tantivy')
        receipt = _drain()
        assert receipt['status'] == 'ready' and db.tantivy_mirror_state()['pending'] == 0

        def lookup():
            if scope == 'default':
                return db.search_wiki(query)
            ranked = mirror.search_if_current(tokens)
            assert ranked is not None
            return [{'node_key': key, 'rank': rank} for key, rank in ranked]

        rows = lookup()
        assert [row['node_key'] for row in rows] == ['both'] and rows[0]['rank'] < 0
        # Discard in-process handles and replay/reopen the durable task-local mirror.
        mirror.reset()
        assert lookup() == rows
        db.upsert_search_index('both', engine.tokenize_joined(a), '', '')
        _drain()
        assert lookup() == []
        db.upsert_search_index('both', engine.tokenize_joined(a + ' ' + b), '', '')
        _drain()
        assert [row['node_key'] for row in lookup()] == ['both']
        assert tokenizer.cache_identity() == default and tokenizer.backend_name() == 'rjieba'
    finally:
        mirror.reset()


@pytest.mark.parametrize('scope', ['default', 'candidate'])
def test_lex_uppercase_01_native_query_lowercase_matches_sqlite(isolated_memory, monkeypatch, scope, tmp_path):
    from tests.test_tantivy_index import _drain

    engine = tokenizer if scope == 'default' else tokenizer.build_profile_tokenizer()
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'tantivy')
    mirror.reset()
    try:
        db.init_db()
        for key, text in [('a-only', '电子病历'), ('b-only', '医疗AI'), ('both', '电子病历 医疗AI')]:
            db.upsert_search_index(key, engine.tokenize_joined(text), '', '')
        query = engine.tokenize_joined('电子病历 医疗AI')
        tokens = query.split()
        match = ' '.join('"' + token + '"' for token in tokens)
        sql_rows = [dict(row) for row in db.get_connection().execute(
            'SELECT node_key, bm25(wiki_search_index) AS rank FROM wiki_search_index '
            'WHERE wiki_search_index MATCH ? ORDER BY rank', (match,))]
        assert [row['node_key'] for row in sql_rows] == ['both'] and sql_rows[0]['rank'] < 0
        if scope == 'default':
            monkeypatch.setenv('VECTOR_LAKE_FTS', 'fts5')
            assert [row['node_key'] for row in db.search_wiki(query)] == ['both']
            monkeypatch.setenv('VECTOR_LAKE_FTS', 'tantivy')
        _drain()
        raw = mirror.search_if_current(tokens)
        lowered = mirror.search_if_current([token.lower() for token in tokens])
        assert raw is not None and [key for key, _ in raw] == ['both']
        assert lowered is not None and lowered == raw and raw[0][1] < 0
        if scope == 'default':
            rows = db.search_wiki(query)
            assert [row['node_key'] for row in rows] == ['both'] and rows[0]['rank'] < 0
        evidence = tmp_path / f'integration-LEX-UPPERCASE-01-{scope}.json'
        payload = {
            'issue': 'LEX-UPPERCASE-01', 'scope': scope, 'query_terms': tokens,
            'sql': sql_rows, 'tantivy_raw': [list(row) for row in raw],
            'tantivy_lowered': [list(row) for row in lowered],
            'oracle': 'native lowercase query terms match unchanged lowercase index analyzer; quoted SQLite AND agrees',
            'classification': 'LEX-UPPERCASE-01 query-only regression; no profile or production activation',
        }
        evidence.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding='utf-8')
        assert json.loads(evidence.read_text(encoding='utf-8')) == payload
    finally:
        mirror.reset()
