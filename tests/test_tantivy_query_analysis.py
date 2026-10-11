"""LEX-UPPERCASE-01: real native term queries, never query-parser syntax."""
import builtins

import pytest

from vector_lake import db_store as db
from vector_lake import tantivy_index as mirror
from vector_lake import tokenizer


@pytest.fixture
def native_mirror(isolated_memory, monkeypatch):
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'tantivy')
    db.init_db()
    mirror.reset()
    yield
    mirror.reset()


def keys(rows):
    return [key for key, _ in rows]


@pytest.mark.parametrize('query, text', [
    ('AI', 'AI'), ('ai', 'AI'), ('Ai', 'AI'),
    ('医疗AI', '医疗AI'), ('医疗ai', '医疗AI'),
    ('ΟΣ', 'ΟΣ'), ('οσ', 'ΟΣ'), ('STRAẞE', 'STRAẞE'), ('straße', 'STRAẞE'),
    ('İ', 'İ'), ('i\u0307', 'İ'), ('ＡI', 'ＡI'),
])
def test_native_lowercase_matches_index_without_python_unicode_assumptions(native_mirror, query, text):
    mirror.upsert('CaseSensitive_KEY', text, '', '')
    rows = mirror.search([query])
    assert keys(rows) == ['CaseSensitive_KEY']
    assert rows[0][1] < 0
    assert mirror.search_keys() == {'CaseSensitive_KEY'}


@pytest.mark.parametrize('query', ['strasse', 'ος', 'AI'])
def test_lowercase_is_not_casefold_contextual_lower_or_width_folding(native_mirror, query):
    mirror.upsert('sentinel', 'STRAẞE ΟΣ ＡI', '', '')
    assert mirror.search([query]) == []


@pytest.mark.parametrize('term', ['TITLE:AI', 'AI*', '+AI', '(AI)', '"AI"', 'AI\\BETA', 'AI:OR'])
def test_punctuation_is_literal_not_query_syntax(native_mirror, term, monkeypatch):
    import tantivy

    mirror.upsert('literal', term, '', '')
    mirror.upsert('decoy', 'AI BETA', '', '')
    monkeypatch.setattr(tantivy.Index, 'parse_query', lambda *a, **k: pytest.fail('literal query parsed'))
    assert keys(mirror.search([term])) == ['literal']


@pytest.mark.parametrize('term', ['AI BETA', 'AI\tBETA', 'AI\nBETA', ' AI ', ' ', ''])
def test_whitespace_and_empty_terms_remain_atomic(native_mirror, term):
    mirror.upsert('split-decoy', 'AI BETA', '', '')
    assert mirror.search([term]) == []
    assert mirror.search(['AI', term]) == []
    assert mirror.search([]) == []


@pytest.mark.parametrize('term', [None, 7, b'AI', '\ud800'])
def test_invalid_native_input_errors_remain_explicit(native_mirror, term):
    mirror.upsert('a', 'AI', '', '')
    with pytest.raises(ValueError):
        mirror.search([term])


def test_selected_fields_aliases_and_rank_limit_order(native_mirror):
    mirror.upsert_many([
        ('Case_KEY', 'ALPHA', '', '', '医疗AI'),
        ('title-only', 'ALPHA', '', '', ''),
        ('alias-only', '', '', '', '医疗AI'),
        ('weaker', 'ALPHA EXTRA EXTRA EXTRA', '', '', '医疗AI'),
    ])
    assert mirror.search(['ALPHA', '医疗AI']) == []
    assert mirror.search(['ALPHA', '医疗AI'], fields=('title',)) == []
    rows = mirror.search(['ALPHA', '医疗AI'], fields=('title', 'aliases'))
    assert keys(rows) == ['Case_KEY', 'weaker']
    assert rows == mirror.search(['alpha', '医疗ai'], fields=('title', 'aliases'))
    assert all(rank < 0 for _, rank in rows)
    assert [rank for _, rank in rows] == sorted(rank for _, rank in rows)
    assert mirror.search(['ALPHA', '医疗AI'], limit=1, fields=('title', 'aliases')) == rows[:1]
    # Tantivy's existing limit=0 contract is a native panic, not an empty result.
    with pytest.raises(BaseException, match='Limit must be greater than 0') as caught:
        mirror.search(['ALPHA', '医疗AI'], limit=0, fields=('title', 'aliases'))
    assert (caught.type.__module__, caught.type.__name__) == ('pyo3_runtime', 'PanicException')
    with pytest.raises(ValueError):
        mirror.search(['ALPHA'], fields=('not_a_field',))


def test_uppercase_ready_dirty_fallback_update_reopen(native_mirror, monkeypatch):
    from tests.test_tantivy_index import _drain

    query = 'ALPHA 医疗AI'
    stream = tokenizer.tokenize_joined(query)
    terms = stream.split()
    db.upsert_search_index('Case_KEY', stream, '', '')
    assert mirror.search_if_current(terms) is None
    assert [row['node_key'] for row in db.search_wiki(query)] == ['Case_KEY']
    _drain()
    identity, token = mirror._disk_identity(), mirror.current_token()
    rows = mirror.search_if_current(terms)
    assert rows is not None and keys(rows) == ['Case_KEY'] and rows[0][1] < 0
    assert mirror._disk_identity() == identity and mirror.current_token() == token
    mirror.reset()
    assert mirror.search_if_current(terms) == rows
    assert mirror._disk_identity() == identity
    statements = []
    db.get_connection().set_trace_callback(statements.append)
    try:
        assert [row['node_key'] for row in db.search_wiki(query)] == ['Case_KEY']
        assert not any(' MATCH ' in sql for sql in statements)
    finally:
        db.get_connection().set_trace_callback(None)
    db.upsert_search_index('Case_KEY', tokenizer.tokenize_joined('ALPHA'), '', '')
    assert mirror.search_if_current(terms) is None
    assert db.search_wiki(query) == []
    _drain()
    assert mirror.search_if_current(terms) == []
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'fts5')
    assert db.search_wiki(query) == []
    monkeypatch.setenv('VECTOR_LAKE_FTS', 'tantivy')
    db.upsert_search_index('Case_KEY', stream, '', '')
    _drain()
    mirror.reset()
    assert keys(mirror.search_if_current(terms)) == ['Case_KEY']


@pytest.mark.parametrize('fault', ['missing', 'transitive', 'dll', 'native'])
def test_query_dependency_faults_are_not_verified_empty(monkeypatch, fault):
    original = builtins.__import__
    errors = {
        'missing': ModuleNotFoundError("No module named 'tantivy'", name='tantivy'),
        'transitive': ModuleNotFoundError("No module named 'native_dependency'", name='native_dependency'),
        'dll': ImportError('DLL load failed: fixture'),
        'native': RuntimeError('native initialization failed: fixture'),
    }
    error = errors[fault]

    def guarded_import(name, *args, **kwargs):
        if name == 'tantivy':
            raise error
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    assert mirror.search([]) == []
    with pytest.raises(type(error)) as caught:
        mirror._search_index(None, ['AI'], 50, mirror.SEARCH_FIELDS)
    assert caught.value is error
    if isinstance(error, ModuleNotFoundError):
        assert caught.value.name == error.name


def test_native_analysis_call_failure_is_not_empty(native_mirror, monkeypatch):
    import tantivy

    mirror.upsert('a', 'AI', '', '')

    def broken(*args, **kwargs):
        raise RuntimeError('native analysis fault: fixture')

    monkeypatch.setattr(tantivy.TextAnalyzer, 'analyze', broken)
    with pytest.raises(RuntimeError, match='native analysis fault'):
        mirror.search(['AI'])


def test_raw_node_key_query_remains_case_sensitive(native_mirror):
    mirror.upsert_many([
        ('Case_KEY', 'ALPHA', '', '', ''),
        ('case_key', 'BETA', '', '', ''),
    ])
    assert keys(mirror.search(['Case_KEY'], fields=('node_key',))) == ['Case_KEY']
    assert keys(mirror.search(['case_key'], fields=('node_key',))) == ['case_key']
    assert mirror.search(['CASE_KEY'], fields=('node_key',)) == []


@pytest.mark.parametrize('fields', [('node_key', 'title'), ('title', 'node_key')])
def test_mixed_raw_key_and_text_fields_keep_each_terms_or_and(native_mirror, fields):
    mirror.upsert_many([
        ('Case_KEY', 'ALPHA', '', '', ''),
        ('case_key', 'BETA', '', '', ''),
        ('decoy', 'CASE_KEY ALPHA', '', '', ''),
    ])
    assert set(keys(mirror.search(['Case_KEY', 'ALPHA'], fields=fields))) == {'Case_KEY', 'decoy'}
    assert keys(mirror.search(['CASE_KEY', 'ALPHA'], fields=fields)) == ['decoy']
    assert mirror.search(['Case_KEY', 'ABSENT'], fields=fields) == []


@pytest.mark.parametrize('failure_site', ['builder', 'analyze'])
def test_raw_key_does_not_acquire_text_analyzer_dependency(native_mirror, monkeypatch, failure_site):
    import tantivy

    mirror.upsert('Case_KEY', 'ALPHA', '', '')

    def broken(*args, **kwargs):
        raise RuntimeError('lowercase dependency must not affect raw key')

    if failure_site == 'builder':
        monkeypatch.setattr(tantivy, 'TextAnalyzerBuilder', broken)
    else:
        monkeypatch.setattr(tantivy.TextAnalyzer, 'analyze', broken)
    assert keys(mirror.search(['Case_KEY'], fields=('node_key',))) == ['Case_KEY']
    with pytest.raises(ValueError):
        mirror.search(['Case_KEY'], fields=('not_a_field',))
