use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};

const FIELD_WEIGHTS: [u32; 16] = [0, 4, 3, 7, 1, 5, 4, 8, 0, 0, 0, 0, 0, 0, 0, 0];

/// 打包 `[(doc, mask), ...]` -> 小端 uint32 紧凑字节流
#[pyfunction]
pub fn pack_postings(postings: Vec<(u32, u8)>) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(postings.len() * 4);
    let mut previous = 0u32;
    for (doc, mask) in postings {
        let delta = doc.saturating_sub(previous);
        let value = (delta << 4) | (mask as u32 & 0x0F);
        bytes.extend_from_slice(&value.to_le_bytes());
        previous = doc;
    }
    bytes
}

/// 解包小端 uint32 紧凑字节流 -> `[(doc, mask), ...]`
#[pyfunction]
pub fn unpack_postings(blob: &[u8]) -> Vec<(u32, u8)> {
    if blob.len() % 4 != 0 {
        return Vec::new();
    }
    let count = blob.len() / 4;
    let mut out = Vec::with_capacity(count);
    let mut doc = 0u32;
    for chunk in blob.chunks_exact(4) {
        let value = u32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
        doc = doc.wrapping_add(value >> 4);
        out.push((doc, (value & 0x0F) as u8));
    }
    out
}

/// 快速单 posting blob 累加
#[pyfunction]
#[pyo3(signature = (blob, weights=None, skip_docs=None))]
pub fn accumulate_postings(
    blob: &[u8],
    weights: Option<Vec<u32>>,
    skip_docs: Option<HashSet<u32>>,
) -> HashMap<u32, u32> {
    let w = match &weights {
        Some(custom) if custom.len() >= 16 => {
            let mut arr = [0u32; 16];
            arr.copy_from_slice(&custom[..16]);
            arr
        }
        _ => FIELD_WEIGHTS,
    };

    let mut accumulator = HashMap::new();
    if blob.len() % 4 != 0 {
        return accumulator;
    }

    let mut doc = 0u32;
    for chunk in blob.chunks_exact(4) {
        let value = u32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
        doc = doc.wrapping_add(value >> 4);

        if let Some(ref skip) = skip_docs {
            if skip.contains(&doc) {
                continue;
            }
        }

        let mask = (value & 0x0F) as usize;
        if mask > 0 {
            *accumulator.entry(doc).or_insert(0) += w[mask];
        }
    }
    accumulator
}

/// 极速单文档 Gram 提取 (单字、双字、长字母数字串)
#[pyfunction]
pub fn extract_grams(key_blob: &str, text_blob: &str, page_blob: &str) -> HashMap<String, u8> {
    let mut grams: HashMap<String, u8> = HashMap::new();

    let fields = [(1u8, key_blob), (2u8, text_blob), (4u8, page_blob)];

    for (bit, blob) in fields {
        if blob.is_empty() {
            continue;
        }

        let chars: Vec<char> = blob.chars().collect();
        let len = chars.len();

        // 1. 单字
        for &c in &chars {
            let mut s = String::with_capacity(4);
            s.push(c);
            *grams.entry(s).or_insert(0) |= bit;
        }

        // 2. 双字 (bigrams)
        for i in 0..len.saturating_sub(1) {
            let mut s = String::with_capacity(8);
            s.push(chars[i]);
            s.push(chars[i + 1]);
            *grams.entry(s).or_insert(0) |= bit;
        }

        // 3. 连续字母数字 >= 3 (ASCII alphanum run)
        let mut start: Option<usize> = None;
        for (i, &c) in chars.iter().enumerate() {
            if (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') {
                if start.is_none() {
                    start = Some(i);
                }
            } else if let Some(s_idx) = start {
                if i - s_idx >= 3 {
                    let run: String = chars[s_idx..i]
                        .iter()
                        .map(|ch| ch.to_ascii_lowercase())
                        .collect();
                    *grams.entry(run).or_insert(0) |= bit;
                }
                start = None;
            }
        }
        if let Some(s_idx) = start {
            if len - s_idx >= 3 {
                let run: String = chars[s_idx..len]
                    .iter()
                    .map(|ch| ch.to_ascii_lowercase())
                    .collect();
                *grams.entry(run).or_insert(0) |= bit;
            }
        }
    }

    grams
}

/// 批量多词项倒排解码 + IDF 缩放 + 复合累加打分
/// 严格对齐 Python 原版算法的整型求和与浮点数单次相乘精度
#[pyfunction]
#[pyo3(signature = (term_blobs, idf_factors, skip_docs=None, weights=None))]
pub fn fast_accumulate_terms(
    term_blobs: Vec<(String, Vec<u8>)>,
    idf_factors: HashMap<String, f64>,
    skip_docs: Option<HashSet<u32>>,
    weights: Option<Vec<u32>>,
) -> HashMap<u32, f64> {
    let w = match &weights {
        Some(custom) if custom.len() >= 16 => {
            let mut arr = [0u32; 16];
            arr.copy_from_slice(&custom[..16]);
            arr
        }
        _ => FIELD_WEIGHTS,
    };

    let mut accumulator: HashMap<u32, f64> = HashMap::new();

    for (term, blob) in term_blobs {
        if blob.len() % 4 != 0 {
            continue;
        }
        let factor = match idf_factors.get(&term) {
            Some(&f) if f > 0.0 => f,
            _ => continue,
        };

        // 先计算该 term 的 folded 整型权重 (doc -> u32)
        let mut folded: HashMap<u32, u32> = HashMap::new();
        let mut doc = 0u32;

        for chunk in blob.chunks_exact(4) {
            let value = u32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
            doc = doc.wrapping_add(value >> 4);

            if let Some(ref skip) = skip_docs {
                if skip.contains(&doc) {
                    continue;
                }
            }

            let mask = (value & 0x0F) as usize;
            if mask > 0 {
                *folded.entry(doc).or_insert(0) += w[mask];
            }
        }

        // 与 Python 的 _fold 完全一致：factor * (weight as f64)
        for (doc_id, weight) in folded {
            let score = factor * (weight as f64);
            *accumulator.entry(doc_id).or_insert(0.0) += score;
        }
    }

    accumulator
}

/// Intersect document ids; exact substring verification still belongs to Python.
#[pyfunction]
pub fn intersect_postings(py: Python<'_>, mut blobs: Vec<Vec<u8>>) -> PyResult<Vec<u32>> {
    if blobs.iter().map(Vec::len).sum::<usize>() > 64 * 1024 * 1024 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "postings capacity exceeded",
        ));
    }
    py.allow_threads(move || {
        blobs.sort_by_key(Vec::len);
        let mut result: Option<Vec<u32>> = None;
        for blob in blobs {
            if blob.len() % 4 != 0 {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "postings must contain complete uint32 entries",
                ));
            }
            let mut doc = 0u32;
            let mut docs = Vec::with_capacity(blob.len() / 4);
            for bytes in blob.chunks_exact(4) {
                let packed = u32::from_le_bytes(bytes.try_into().unwrap());
                doc = doc.checked_add(packed >> 4).ok_or_else(|| {
                    pyo3::exceptions::PyValueError::new_err("postings document id overflow")
                })?;
                if docs.last() != Some(&doc) {
                    docs.push(doc);
                }
            }
            result = Some(match result {
                None => docs,
                Some(previous) => {
                    let mut common = Vec::with_capacity(previous.len().min(docs.len()));
                    let (mut a, mut b) = (0, 0);
                    while a < previous.len() && b < docs.len() {
                        match previous[a].cmp(&docs[b]) {
                            std::cmp::Ordering::Less => a += 1,
                            std::cmp::Ordering::Greater => b += 1,
                            std::cmp::Ordering::Equal => {
                                common.push(previous[a]);
                                a += 1;
                                b += 1;
                            }
                        }
                    }
                    common
                }
            });
        }
        Ok(result.unwrap_or_default())
    })
}
