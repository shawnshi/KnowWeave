//! CJK segmentation through `jieba-rs`, owned by this core.
//!
//! `rjieba` is the only published Python binding for jieba-rs and it links 0.9.x; its newest
//! release predates the crate's 0.10/0.11 line.  This module makes the crate version a decision in
//! *this* repository (`Cargo.toml`) instead of a property of a wheel, and exposes rjieba's call
//! shape so the two can be diffed token-for-token:
//!
//! * `cut(text)` mirrors `rjieba.cut(text)` -- HMM enabled, which is that binding's default and
//!   therefore what every existing index was built with;
//! * `cut_joined(text)` is the pre-tokenized form the FTS projection stores.
//!
//! Nothing in the runtime selects this yet: `vector_lake/tokenizer.py` still resolves `rjieba`, and
//! switching a tokenizer invalidates every lexical index (the backend is part of the FTS cache key
//! because different backends produce different token streams).  Parity has to be measured first.

use jieba_rs::Jieba;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes, PyString};
use std::sync::OnceLock;

const MAX_TERMS: usize = 256;
const MAX_TERM_CHARS: usize = 64;
const MAX_TOTAL_BYTES: usize = 16 * 1024;

fn push_term(terms: &mut Vec<String>, total: &mut usize, term: &str) -> Result<(), &'static str> {
    if terms.len() >= MAX_TERMS {
        return Err("terms exceed 256 input items");
    }
    if term.is_empty() || term.chars().any(char::is_whitespace) {
        return Err("terms must be nonempty and contain no whitespace");
    }
    if term.chars().count() > MAX_TERM_CHARS {
        return Err("term exceeds 64 Unicode characters");
    }
    let bytes = total.checked_add(term.len()).ok_or("term byte overflow")?;
    if bytes > MAX_TOTAL_BYTES {
        return Err("terms exceed 16384 UTF-8 bytes");
    }
    terms.push(term.to_owned());
    *total = bytes;
    Ok(())
}

fn extract_terms(input: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    if input.is_instance_of::<PyString>()
        || input.is_instance_of::<PyBytes>()
        || input.is_instance_of::<PyByteArray>()
    {
        return Err(PyTypeError::new_err(
            "terms must be an iterable of strings, not a string or bytes",
        ));
    }
    let mut terms = Vec::new();
    let mut total = 0;
    // Do not use Vec extraction or iterator length hints: both can allocate before the caps.
    for item in input.try_iter()? {
        let item = item?;
        if terms.len() >= MAX_TERMS {
            return Err(PyValueError::new_err("terms exceed 256 input items"));
        }
        let term = item
            .cast::<PyString>()
            .map_err(|_| PyTypeError::new_err("every term must be a string"))?;
        // The bound is a live PyString under the GIL. The C API bypasses subclass __len__
        // overrides and bounds UTF-8 materialization even on the abi3 Python 3.8 path.
        let chars = unsafe { pyo3::ffi::PyUnicode_GetLength(term.as_ptr()) };
        if chars < 0 {
            return Err(PyErr::fetch(input.py()));
        }
        if chars as usize > MAX_TERM_CHARS {
            return Err(PyValueError::new_err("term exceeds 64 Unicode characters"));
        }
        let text = term.to_cow()?;
        push_term(&mut terms, &mut total, &text).map_err(PyValueError::new_err)?;
    }
    terms.sort_unstable();
    terms.dedup();
    Ok(terms)
}

/// An independent, immutable dictionary snapshot; never replaces the legacy singleton.
#[pyclass(frozen, module = "vector_lake_core")]
pub struct ProfileTokenizer {
    jieba: Jieba,
}

impl ProfileTokenizer {
    fn from_terms(terms: &[String]) -> Self {
        let mut jieba = Jieba::new();
        // jieba-rs 0.11.0 lib.rs:535: None uses suggest_freq against the evolving dictionary.
        // Sorted/deduplicated input therefore fixes the construction order and default weights.
        for term in terms {
            jieba.add_word(term, None, None);
        }
        Self { jieba }
    }
}

#[pymethods]
impl ProfileTokenizer {
    #[new]
    fn new(terms: &Bound<'_, PyAny>) -> PyResult<Self> {
        let terms = extract_terms(terms)?;
        Ok(Self::from_terms(&terms))
    }

    fn cut(&self, text: &str) -> Vec<String> {
        self.jieba
            .cut(text, true)
            .into_iter()
            .map(|token| token.word.to_owned())
            .collect()
    }

    #[pyo3(signature = (text, separator=" "))]
    fn cut_joined(&self, text: &str, separator: &str) -> String {
        self.cut(text).join(separator)
    }

    #[getter]
    fn engine(&self) -> &'static str {
        "jieba-rs"
    }

    #[getter]
    fn engine_version(&self) -> &'static str {
        "0.11.0"
    }

    #[getter]
    fn contract_version(&self) -> &'static str {
        "profile-tokenizer-v1"
    }

    #[getter]
    fn hmm(&self) -> bool {
        true
    }

    #[getter]
    fn frequency_policy(&self) -> &'static str {
        "sorted-add-word-default"
    }
}

fn tokenizer() -> &'static Jieba {
    // `Jieba::new()` loads the dictionary (~several MB) and is not cheap; one process-wide
    // instance is what the Python binding does too.
    static JIEBA: OnceLock<Jieba> = OnceLock::new();
    JIEBA.get_or_init(Jieba::new)
}

/// Segment `text`, HMM enabled -- `rjieba.cut(text)` in Python.
///
/// jieba-rs 0.11 returns `Token { word, start, end, .. }` rather than the `&str` the 0.9 line
/// produced, which is one reason the pin is spelled out here: the binding's call shape and the
/// crate's return type have to be checked against each other, not assumed equal.
#[pyfunction]
pub fn cut(text: &str) -> Vec<String> {
    tokenizer()
        .cut(text, true)
        .into_iter()
        .map(|token| token.word.to_string())
        .collect()
}

/// Segment and rejoin with single spaces: what `indexer` stores as pre-tokenized text.
#[pyfunction]
pub fn cut_joined(text: &str) -> String {
    tokenizer()
        .cut(text, true)
        .into_iter()
        .map(|token| token.word)
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyDict;
    use std::ffi::CString;

    fn python_test(source: &str) {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let locals = PyDict::new(py);
            locals
                .set_item("ProfileTokenizer", py.get_type::<ProfileTokenizer>())
                .unwrap();
            let source = CString::new(source).unwrap();
            py.run(&source, Some(&locals), None).unwrap();
        });
    }

    #[test]
    fn canonical_order_and_dedup() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let input =
                pyo3::types::PyList::new(py, ["量子医疗验证桥", "医疗验证", "量子医疗验证桥"])
                    .unwrap();
            let canonical = extract_terms(input.as_any()).unwrap();
            assert_eq!(canonical, ["医疗验证", "量子医疗验证桥"]);
            let a = ProfileTokenizer::from_terms(&canonical);
            let reversed = pyo3::types::PyList::new(py, ["医疗验证", "量子医疗验证桥"]).unwrap();
            let b = ProfileTokenizer::new(reversed.as_any()).unwrap();
            for text in ["量子医疗验证桥", "医疗验证：量子医疗验证桥 NLP", ""] {
                assert_eq!(a.cut(text), b.cut(text));
            }
            assert_eq!(a.cut("量子医疗验证桥"), ["量子医疗验证桥"]);
        });
    }

    #[test]
    fn hmm_and_legacy_default_stability() {
        let text = "他来到了网易杭研大厦";
        let default = Jieba::new();
        let hmm: Vec<_> = default
            .cut(text, true)
            .into_iter()
            .map(|t| t.word.to_owned())
            .collect();
        let no_hmm: Vec<_> = default
            .cut(text, false)
            .into_iter()
            .map(|t| t.word.to_owned())
            .collect();
        assert_ne!(hmm, no_hmm, "fixture must distinguish HMM on/off");
        let before = cut("量子医疗验证桥");
        let profile = ProfileTokenizer::from_terms(&["量子医疗验证桥".to_owned()]);
        assert_ne!(before, profile.cut("量子医疗验证桥"));
        assert_eq!(cut("量子医疗验证桥"), before);
        assert_eq!(ProfileTokenizer::from_terms(&[]).cut(text), hmm);
        assert_eq!(cut(text), hmm);
        assert_eq!(cut_joined(text), cut(text).join(" "));
        assert!(profile.hmm());
    }

    #[test]
    fn joined_unicode_empty_and_separators() {
        let profile = ProfileTokenizer::from_terms(&[]);
        for text in ["", " ", "中文，ASCII NLP!\nβ𠮷", "他来到了网易杭研大厦"] {
            for separator in [" ", "", "::", "\n", "𠮷"] {
                assert_eq!(
                    profile.cut_joined(text, separator),
                    profile.cut(text).join(separator)
                );
            }
            assert_eq!(cut_joined(text), cut(text).join(" "));
        }
        python_test(
            r#"
p = ProfileTokenizer([])
assert p.cut('') == []
assert p.cut_joined('') == ''
for sep in [' ', '', '::', '\n', '𠮷']:
    text = '中文，ASCII NLP!\nβ𠮷'
    assert p.cut_joined(text, separator=sep) == sep.join(p.cut(text))
assert p.cut_joined('中文 test') == ' '.join(p.cut('中文 test'))
"#,
        );
    }

    #[test]
    fn invalid_containers_and_items() {
        python_test(
            r#"
for terms in ['abc', b'abc', bytearray(b'abc'), None, 42, ['ok', 42], [b'abc'], [None]]:
    try:
        ProfileTokenizer(terms)
    except TypeError:
        pass
    else:
        raise AssertionError(repr(terms))
for term in ['', ' ', '\t', 'a b', ' a', 'a\n', '\u00a0', 'a\u3000b']:
    try:
        ProfileTokenizer([term])
    except ValueError:
        pass
    else:
        raise AssertionError(repr(term))
"#,
        );
    }

    #[test]
    fn invalid_unicode_is_visible() {
        python_test(
            r#"
try:
    ProfileTokenizer(['\ud800'])
except UnicodeEncodeError:
    pass
else:
    raise AssertionError('unpaired surrogate must not be replaced')
p = ProfileTokenizer([])
for method, args in [(p.cut, ('\ud800',)), (p.cut_joined, ('x', '\ud800'))]:
    try:
        method(*args)
    except UnicodeEncodeError:
        pass
    else:
        raise AssertionError('invalid method Unicode must propagate')
"#,
        );
    }

    #[test]
    fn term_count_is_on_raw_input_before_dedup() {
        python_test(
            r#"
ProfileTokenizer(['a'] * 256)
try:
    ProfileTokenizer(['a'] * 257)
except ValueError as error:
    assert '256' in str(error)
else:
    raise AssertionError('duplicate input still counts toward cap')
"#,
        );
    }

    #[test]
    fn unicode_character_and_utf8_byte_caps() {
        python_test(
            r#"
ProfileTokenizer(['😀' * 64] * 64)  # exactly 16 KiB, each item exactly 64 scalars
ProfileTokenizer(['a' * 64] * 256)  # also exactly 16 KiB
for terms in [['😀' * 65], ['a' * 65], ['😀' * 64] * 65]:
    try:
        ProfileTokenizer(terms)
    except ValueError:
        pass
    else:
        raise AssertionError('character/byte overflow was accepted')
class LyingLength(str):
    def __len__(self):
        return 1
try:
    ProfileTokenizer([LyingLength('a' * 65)])
except ValueError:
    pass
else:
    raise AssertionError('str subclass cannot bypass character cap')
"#,
        );
    }

    #[test]
    fn iterator_is_bounded_and_does_not_use_size_hint() {
        python_test(
            r#"
class Endless:
    calls = 0
    def __iter__(self):
        return self
    def __next__(self):
        self.calls += 1
        if self.calls > 257:
            raise AssertionError('unbounded extraction')
        return 'a'
    def __length_hint__(self):
        raise AssertionError('must not allocate using length hint')
terms = Endless()
try:
    ProfileTokenizer(terms)
except ValueError:
    assert terms.calls == 257
else:
    raise AssertionError('endless iterator accepted')
"#,
        );
    }

    #[test]
    fn iterator_failure_is_visible() {
        python_test(
            r#"
def broken():
    yield 'valid'
    raise RuntimeError('synthetic dependency failure')
try:
    ProfileTokenizer(broken())
except RuntimeError as error:
    assert str(error) == 'synthetic dependency failure'
else:
    raise AssertionError('iterator exception swallowed')
"#,
        );
    }

    #[test]
    fn immutable_python_surface_and_owned_snapshot() {
        python_test(
            r#"
terms = ['量子医疗验证桥']
p = ProfileTokenizer(terms)
terms.clear()
assert p.cut('量子医疗验证桥') == ['量子医疗验证桥']
assert p.engine == 'jieba-rs'
assert p.engine_version == '0.11.0'
assert p.contract_version == 'profile-tokenizer-v1'
assert p.hmm is True
assert p.frequency_policy == 'sorted-add-word-default'
assert not hasattr(p, 'add_word') and not hasattr(p, 'reload')
for name in ['engine', 'engine_version', 'contract_version', 'hmm', 'frequency_policy', 'jieba', 'cut', 'new_field']:
    try:
        setattr(p, name, 'changed')
    except AttributeError:
        pass
    else:
        raise AssertionError('mutable attribute: ' + name)
try:
    del p.engine
except AttributeError:
    pass
else:
    raise AssertionError('metadata can be deleted')
assert p.cut('量子医疗验证桥') == ['量子医疗验证桥']
"#,
        );
    }

    #[test]
    fn checked_byte_overflow_has_no_partial_write() {
        let mut terms = Vec::new();
        let mut total = usize::MAX;
        assert_eq!(
            push_term(&mut terms, &mut total, "a"),
            Err("term byte overflow")
        );
        assert!(terms.is_empty());
        assert_eq!(total, usize::MAX);
    }
}
