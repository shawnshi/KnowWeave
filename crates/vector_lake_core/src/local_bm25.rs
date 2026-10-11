use pyo3::prelude::*;
use std::collections::HashMap;

/// 纯内存极速 Okapi BM25 局部候选池重排引擎
/// 接收 query 分词与候选文档分词列表，直接输出混合重排后的 (原下标, 混合得分)
#[pyfunction]
#[pyo3(signature = (query_tokens, doc_tokens, upstream_scores, weight=0.4, k1=1.5, b=0.75))]
pub fn fast_bm25_rerank(
    query_tokens: Vec<String>,
    doc_tokens: Vec<Vec<String>>,
    upstream_scores: Vec<f64>,
    weight: Option<f64>,
    k1: Option<f64>,
    b: Option<f64>,
) -> Vec<(usize, f64)> {
    let n = doc_tokens.len();
    if n == 0 {
        return Vec::new();
    }
    if n != upstream_scores.len() {
        return (0..n)
            .map(|i| (i, upstream_scores.get(i).copied().unwrap_or(0.0)))
            .collect();
    }

    let blend_weight = weight.unwrap_or(0.4).clamp(0.0, 1.0);
    let bm25_k1 = k1.unwrap_or(1.5);
    let bm25_b = b.unwrap_or(0.75);

    // 1. 统计文档长度与 avgdl
    let mut doc_lens = Vec::with_capacity(n);
    let mut total_len = 0usize;
    let mut df: HashMap<String, usize> = HashMap::new();

    // 2. 统计各文档内部 TF 与全局 DF
    let mut doc_tfs: Vec<HashMap<String, usize>> = Vec::with_capacity(n);
    for doc in &doc_tokens {
        let len = doc.len();
        doc_lens.push(len);
        total_len += len;

        let mut tf_map: HashMap<String, usize> = HashMap::with_capacity(len);
        for tok in doc {
            *tf_map.entry(tok.clone()).or_insert(0) += 1;
        }
        for unique_term in tf_map.keys() {
            *df.entry(unique_term.clone()).or_insert(0) += 1;
        }
        doc_tfs.push(tf_map);
    }

    let avgdl = if n > 0 {
        (total_len as f64) / (n as f64)
    } else {
        1.0
    };

    // 3. 计算 BM25 得分
    let mut lexical_scores = vec![0.0f64; n];
    for q_term in &query_tokens {
        let doc_freq = match df.get(q_term) {
            Some(&c) if c > 0 => c,
            _ => continue,
        };

        // BM25 IDF: ln(1 + (N - df + 0.5) / (df + 0.5))
        let idf = (((n as f64) - (doc_freq as f64) + 0.5) / ((doc_freq as f64) + 0.5) + 1.0).ln();

        for i in 0..n {
            let tf = doc_tfs[i].get(q_term).copied().unwrap_or(0) as f64;
            if tf > 0.0 {
                let dl = doc_lens[i] as f64;
                let denominator = tf + bm25_k1 * (1.0 - bm25_b + bm25_b * (dl / avgdl.max(1e-6)));
                let score = idf * (tf * (bm25_k1 + 1.0)) / denominator.max(1e-6);
                lexical_scores[i] += score;
            }
        }
    }

    // 4. Min-max 归一化
    let normalise = |vals: &[f64]| -> Vec<f64> {
        if vals.is_empty() {
            return Vec::new();
        }
        let mut min_val = f64::INFINITY;
        let mut max_val = f64::NEG_INFINITY;
        for &v in vals {
            if v < min_val {
                min_val = v;
            }
            if v > max_val {
                max_val = v;
            }
        }
        if max_val - min_val <= 1e-12 {
            vec![0.0; vals.len()]
        } else {
            vals.iter()
                .map(|&v| (v - min_val) / (max_val - min_val))
                .collect()
        }
    };

    let upstream_norm = normalise(&upstream_scores);
    let lexical_norm = normalise(&lexical_scores);

    // 5. 混合打分
    let mut blended: Vec<(usize, f64)> = (0..n)
        .map(|i| {
            let score = (1.0 - blend_weight) * upstream_norm[i] + blend_weight * lexical_norm[i];
            let rounded = (score * 1_000_000.0).round() / 1_000_000.0;
            (i, rounded)
        })
        .collect();

    // 6. 稳定排序：得分降序，平局保持原下标升序
    blended.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });

    blended
}
