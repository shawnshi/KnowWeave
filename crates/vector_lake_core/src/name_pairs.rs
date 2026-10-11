//! Ordered, bounded name-pair prefilter. Scoring and governance remain in Python.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::{BTreeMap, HashMap};

const MAX_NAMES: usize = 100_000;
const MAX_CHARS: usize = 16_000_000;

struct Features {
    length: usize,
    mask: [u64; 4],
    counts: HashMap<char, usize>,
}

impl Features {
    fn new(name: String) -> Self {
        let mut result = Self {
            length: 0,
            mask: [0; 4],
            counts: HashMap::new(),
        };
        for ch in name.chars() {
            result.length += 1;
            let bit = (ch as usize) % 256;
            result.mask[bit / 64] |= 1u64 << (bit % 64);
            *result.counts.entry(ch).or_default() += 1;
        }
        result
    }

    fn can_match(&self, other: &Self, threshold: f64) -> bool {
        let total = self.length + other.length;
        if total == 0 {
            return true;
        }
        let unmatched: usize = self
            .mask
            .iter()
            .zip(other.mask.iter())
            .map(|(a, b)| (a & !b).count_ones() as usize)
            .sum();
        if 2.0 * (self.length - unmatched) as f64 / total as f64 <= threshold {
            return false;
        }
        let (small, large) = if self.counts.len() <= other.counts.len() {
            (&self.counts, &other.counts)
        } else {
            (&other.counts, &self.counts)
        };
        let common: usize = small
            .iter()
            .map(|(ch, n)| (*n).min(*large.get(ch).unwrap_or(&0)))
            .sum();
        2.0 * common as f64 / total as f64 > threshold
    }
}

#[pyclass]
pub struct NamePairIndex {
    features: Vec<Features>,
    groups: Vec<(usize, Vec<usize>)>,
    threshold: f64,
    left_group: usize,
    right_group: usize,
    left_position: usize,
    right_position: usize,
}

#[pymethods]
impl NamePairIndex {
    #[new]
    fn new(py: Python<'_>, names: Vec<String>, threshold: f64) -> PyResult<Self> {
        if !threshold.is_finite() || !(0.0..1.0).contains(&threshold) {
            return Err(PyValueError::new_err(
                "threshold must be finite and in [0, 1)",
            ));
        }
        if names.len() > MAX_NAMES
            || names.iter().map(|s| s.chars().count()).sum::<usize>() > MAX_CHARS
        {
            return Err(PyValueError::new_err("name index capacity exceeded"));
        }
        Ok(py.allow_threads(move || {
            let features: Vec<Features> = names.into_iter().map(Features::new).collect();
            let mut groups: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
            for (index, name) in features.iter().enumerate() {
                groups.entry(name.length).or_default().push(index);
            }
            Self {
                features,
                groups: groups.into_iter().collect(),
                threshold,
                left_group: 0,
                right_group: 0,
                left_position: 0,
                right_position: 0,
            }
        }))
    }

    #[pyo3(signature = (max_pairs=4096, max_checks=200_000))]
    fn next_pairs(
        &mut self,
        py: Python<'_>,
        max_pairs: usize,
        max_checks: usize,
    ) -> PyResult<(Vec<(usize, usize)>, bool)> {
        if max_pairs == 0 || max_pairs > 65_536 || max_checks == 0 || max_checks > 1_000_000 {
            return Err(PyValueError::new_err(
                "pair/check budgets are outside supported bounds",
            ));
        }
        Ok(py.allow_threads(|| self.chunk(max_pairs, max_checks)))
    }
}

impl NamePairIndex {
    fn chunk(&mut self, max_pairs: usize, max_checks: usize) -> (Vec<(usize, usize)>, bool) {
        let mut pairs = Vec::new();
        let mut checks = 0;
        while self.left_group < self.groups.len() {
            let length = self.groups[self.left_group].0;
            if self.right_group >= self.groups.len()
                || !length_ceiling(length, self.groups[self.right_group].0, self.threshold)
            {
                self.left_group += 1;
                self.right_group = self.left_group;
                self.left_position = 0;
                self.right_position = 0;
                continue;
            }
            let left = &self.groups[self.left_group].1;
            let right = &self.groups[self.right_group].1;
            if self.left_position >= left.len() {
                self.right_group += 1;
                self.left_position = 0;
                self.right_position = 0;
                continue;
            }
            let lower = if self.left_group == self.right_group {
                self.left_position + 1
            } else {
                0
            };
            self.right_position = self.right_position.max(lower);
            if self.right_position >= right.len() {
                self.left_position += 1;
                self.right_position = 0;
                continue;
            }
            let a = left[self.left_position];
            let b = right[self.right_position];
            self.right_position += 1;
            checks += 1;
            if self.features[a].can_match(&self.features[b], self.threshold) {
                pairs.push((a, b));
            }
            if pairs.len() >= max_pairs || checks >= max_checks {
                return (pairs, false);
            }
        }
        (pairs, true)
    }
}

fn length_ceiling(a: usize, b: usize, threshold: f64) -> bool {
    a + b == 0 || 2.0 * a.min(b) as f64 / (a + b) as f64 > threshold
}
