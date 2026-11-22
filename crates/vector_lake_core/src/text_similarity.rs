use pyo3::prelude::*;

/// 寻找两段切片中的最长连续公共子串 (Longest Contiguous Matching Block)
/// 返回 (a_start, b_start, length)
fn find_longest_match(
    a: &[char],
    b: &[char],
    a_low: usize,
    a_high: usize,
    b_low: usize,
    b_high: usize,
    j2len: &mut Vec<usize>,
) -> (usize, usize, usize) {
    let mut best_i = a_low;
    let mut best_j = b_low;
    let mut best_size = 0;

    let b_len = b_high.saturating_sub(b_low);
    if b_len == 0 || a_high <= a_low {
        return (best_i, best_j, 0);
    }

    if j2len.len() < b_len + 1 {
        j2len.resize(b_len + 1, 0);
    } else {
        j2len[..=b_len].fill(0);
    }

    for (idx_a, &ca) in a[a_low..a_high].iter().enumerate() {
        let i = a_low + idx_a;
        let mut prev = 0;
        for (idx_b, &cb) in b[b_low..b_high].iter().enumerate() {
            let j = b_low + idx_b;
            let current = j2len[idx_b + 1];
            if ca == cb {
                let k = prev + 1;
                j2len[idx_b + 1] = k;
                if k > best_size {
                    best_i = i + 1 - k;
                    best_j = j + 1 - k;
                    best_size = k;
                }
            } else {
                j2len[idx_b + 1] = 0;
            }
            prev = current;
        }
    }

    (best_i, best_j, best_size)
}

/// 迭代统计 Ratcliff-Obershelp 算法中的所有匹配字符总数
fn count_matches(
    a: &[char],
    b: &[char],
    j2len: &mut Vec<usize>,
) -> usize {
    let mut total = 0usize;
    let mut stack = vec![(0usize, a.len(), 0usize, b.len())];
    while let Some((a_low, a_high, b_low, b_high)) = stack.pop() {
        let (i, j, k) = find_longest_match(a, b, a_low, a_high, b_low, b_high, j2len);
        if k == 0 {
            continue;
        }
        total += k;
        if a_low < i && b_low < j {
            stack.push((a_low, i, b_low, j));
        }
        if i + k < a_high && j + k < b_high {
            stack.push((i + k, a_high, j + k, b_high));
        }
    }
    total
}

fn sequence_matcher_ratio_inner(a_str: &str, b_str: &str, j2len: &mut Vec<usize>) -> f64 {
    let a: Vec<char> = a_str.chars().collect();
    let b: Vec<char> = b_str.chars().collect();

    let total = a.len() + b.len();
    if total == 0 {
        return 1.0;
    }

    let matches = count_matches(&a, &b, j2len);
    (2.0 * matches as f64) / (total as f64)
}

/// 与 Python difflib.SequenceMatcher.ratio() 严格等价的高性能实现
#[pyfunction]
pub fn fast_sequence_matcher_ratio(a_str: &str, b_str: &str) -> f64 {
    let mut j2len = Vec::new();
    sequence_matcher_ratio_inner(a_str, b_str, &mut j2len)
}

/// 批量快速比对成对字符串的相似度比率
#[pyfunction]
pub fn fast_batch_sequence_matcher_ratios(py: Python<'_>, pairs: Vec<(String, String)>) -> Vec<f64> {
    py.detach(move || {
        let mut j2len = Vec::new();
        pairs
            .iter()
            .map(|(a, b)| sequence_matcher_ratio_inner(a, b, &mut j2len))
            .collect()
    })
}
