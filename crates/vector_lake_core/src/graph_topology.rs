use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::IntoPyObjectExt;
use std::collections::{HashMap, HashSet};

#[derive(Clone, Debug)]
pub struct FastNodeInput {
    pub key: String,
    pub node_type: String,
    pub links: Vec<String>,
    pub sources: Vec<String>,
    pub triples: HashMap<String, f64>,
    pub multiplier: f64,
    pub degree_weight: f64,
}

fn zero_evidence_below_threshold(
    max_affinity: Option<f64>,
    multiplier: f64,
    max_multiplier: Option<f64>,
    min_relevance: f64,
) -> bool {
    let (Some(affinity), Some(maximum)) = (max_affinity, max_multiplier) else {
        return false;
    };
    if ![affinity, multiplier, maximum]
        .iter()
        .all(|value| value.is_finite() && *value >= 0.0)
        || !min_relevance.is_finite()
    {
        return false;
    }
    // Match the existing scorer's grouping and Rust rounding (not Python's).
    let bound = affinity * (multiplier * maximum);
    bound.is_finite() && (bound * 1000.0).round() / 1000.0 < min_relevance
}

#[pyfunction]
#[pyo3(signature = (nodes_payload, type_affinity_table, overlap_weight=2.0, min_relevance=1.5, max_edges_per_node=50))]
pub fn fast_calculate_weighted_edges(
    nodes_payload: &Bound<'_, PyDict>,
    type_affinity_table: &Bound<'_, PyDict>,
    overlap_weight: Option<f64>,
    min_relevance: Option<f64>,
    max_edges_per_node: Option<usize>,
) -> PyResult<Vec<HashMap<String, Py<PyAny>>>> {
    let py = nodes_payload.py();
    let overlap_w = overlap_weight.unwrap_or(2.0);
    let min_rel = min_relevance.unwrap_or(1.5);
    let edge_cap = max_edges_per_node.unwrap_or(50);

    // 1. 解包类型亲和度表
    let mut affinity_cache: HashMap<(String, String), f64> = HashMap::new();
    let mut max_affinity: Option<f64> = Some(0.5); // Missing-type fallback remains conservative.
    for (type_a, inner_val) in type_affinity_table.iter() {
        let t_a: String = type_a.extract()?;
        if let Ok(inner_dict) = inner_val.cast::<PyDict>() {
            for (type_b, score_val) in inner_dict.iter() {
                let t_b: String = type_b.extract()?;
                let score: f64 = score_val.extract()?;
                max_affinity = max_affinity
                    .filter(|_| score.is_finite())
                    .map(|value| value.max(score));
                affinity_cache.insert((t_a.clone(), t_b), score);
            }
        }
    }

    // 2. 解包节点信息
    let mut node_keys: Vec<String> = Vec::with_capacity(nodes_payload.len());
    let mut nodes: HashMap<String, FastNodeInput> = HashMap::with_capacity(nodes_payload.len());

    let mut source_to_nodes: HashMap<String, Vec<String>> = HashMap::new();
    let mut reverse_links: HashMap<String, HashSet<String>> = HashMap::new();
    let mut max_multiplier: Option<f64> = Some(0.0);

    for (k, v) in nodes_payload.iter() {
        let key: String = k.extract()?;
        if let Ok(dict) = v.cast::<PyDict>() {
            let node_type: String = dict
                .get_item("type")?
                .and_then(|x| x.extract::<String>().ok())
                .unwrap_or_else(|| "concept".to_string())
                .to_lowercase();

            let links: Vec<String> = dict
                .get_item("links")?
                .and_then(|x| x.extract::<Vec<String>>().ok())
                .unwrap_or_default();

            let sources: Vec<String> = dict
                .get_item("sources")?
                .and_then(|x| x.extract::<Vec<String>>().ok())
                .unwrap_or_default();

            let mut triples: HashMap<String, f64> = HashMap::new();
            if let Some(t_item) = dict.get_item("triples")? {
                if let Ok(t_dict) = t_item.cast::<PyDict>() {
                    for (tgt, w_val) in t_dict.iter() {
                        if let (Ok(t_str), Ok(w_num)) =
                            (tgt.extract::<String>(), w_val.extract::<f64>())
                        {
                            triples.insert(t_str, w_num);
                        }
                    }
                }
            }

            let multiplier: f64 = dict
                .get_item("multiplier")?
                .and_then(|x| x.extract::<f64>().ok())
                .unwrap_or(1.0);

            max_multiplier = max_multiplier
                .filter(|_| multiplier.is_finite() && multiplier >= 0.0)
                .map(|value| value.max(multiplier));

            let degree_weight: f64 = dict
                .get_item("degree_weight")?
                .and_then(|x| x.extract::<f64>().ok())
                .unwrap_or(0.0);

            for s in &sources {
                source_to_nodes
                    .entry(s.clone())
                    .or_default()
                    .push(key.clone());
            }
            for l in &links {
                reverse_links
                    .entry(l.clone())
                    .or_default()
                    .insert(key.clone());
            }

            node_keys.push(key.clone());
            nodes.insert(
                key.clone(),
                FastNodeInput {
                    key,
                    node_type,
                    links,
                    sources,
                    triples,
                    multiplier,
                    degree_weight,
                },
            );
        }
    }

    // The established native scorer also accepts explicit triples not listed
    // as links, provided another posting made that pair a candidate. Preserve
    // that latent evidence by retaining zero postings at either endpoint.
    let mut triple_only_endpoints: HashSet<String> = HashSet::new();
    // No distinct pair can come from a singleton posting. Avoid even the
    // linear triple mask scan when there is no zero-contribution fanout.
    let has_zero_fanout = reverse_links.iter().any(|(neighbor, linked)| {
        linked.len() > 1
            && nodes
                .get(neighbor)
                .map(|node| node.degree_weight)
                .unwrap_or(0.0)
                == 0.0
    }) || (overlap_w == 0.0
        && source_to_nodes.values().any(|linked| linked.len() > 1));
    if has_zero_fanout {
        for (key, node) in &nodes {
            for target in node.triples.keys() {
                if nodes.contains_key(target)
                    && !reverse_links
                        .get(target)
                        .map_or(false, |linked| linked.contains(key))
                {
                    triple_only_endpoints.insert(key.clone());
                    triple_only_endpoints.insert(target.clone());
                }
            }
        }
    }

    // 3. 快速拓扑边权重计算
    let mut raw_edges: Vec<(String, String, f64)> = Vec::new();

    for key_a in &node_keys {
        let node_a = match nodes.get(key_a) {
            Some(n) => n,
            None => continue,
        };

        let mut candidate_source_overlaps: HashMap<&String, usize> = HashMap::new();
        let mut candidate_neighbor_scores: HashMap<&String, f64> = HashMap::new();

        let skip_zero_postings = !triple_only_endpoints.contains(key_a)
            && zero_evidence_below_threshold(
                max_affinity,
                node_a.multiplier,
                max_multiplier,
                min_rel,
            );
        if overlap_w != 0.0 || !skip_zero_postings {
            for source in &node_a.sources {
                if let Some(b_nodes) = source_to_nodes.get(source) {
                    for key_b in b_nodes {
                        if key_a < key_b {
                            *candidate_source_overlaps.entry(key_b).or_insert(0) += 1;
                        }
                    }
                }
            }
        }

        for neighbor in &node_a.links {
            if let Some(b_nodes) = reverse_links.get(neighbor) {
                let deg_w = nodes.get(neighbor).map(|n| n.degree_weight).unwrap_or(0.0);
                if deg_w == 0.0 && skip_zero_postings {
                    continue;
                }
                for key_b in b_nodes {
                    if key_a < key_b {
                        *candidate_neighbor_scores.entry(key_b).or_insert(0.0) += deg_w;
                    }
                }
            }
        }

        let mut candidates: HashSet<&String> = HashSet::new();
        for k in candidate_source_overlaps.keys() {
            candidates.insert(*k);
        }
        for k in candidate_neighbor_scores.keys() {
            candidates.insert(*k);
        }
        for l in &node_a.links {
            if key_a < l {
                candidates.insert(l);
            }
        }
        if let Some(rev) = reverse_links.get(key_a) {
            for b in rev {
                if key_a < b {
                    candidates.insert(b);
                }
            }
        }

        for key_b in candidates {
            let node_b = match nodes.get(key_b) {
                Some(n) => n,
                None => continue,
            };

            let mut score = 0.0f64;

            if let Some(&w) = node_a.triples.get(key_b) {
                score += w;
            }

            if let Some(&w) = node_b.triples.get(key_a) {
                score += w;
            }

            if let Some(&cnt) = candidate_source_overlaps.get(key_b) {
                score += (cnt as f64) * overlap_w;
            }

            if let Some(&n_score) = candidate_neighbor_scores.get(key_b) {
                score += n_score;
            }

            let affinity = affinity_cache
                .get(&(node_a.node_type.clone(), node_b.node_type.clone()))
                .copied()
                .unwrap_or(0.5);
            score += affinity;

            score *= node_a.multiplier * node_b.multiplier;
            let relevance = (score * 1000.0).round() / 1000.0;

            if relevance >= min_rel {
                raw_edges.push((key_a.clone(), key_b.clone(), relevance));
            }
        }
    }

    // 4. 去重并保留 Top-K 边 (dedupe_and_prune)
    raw_edges.sort_by(|a, b| {
        b.2.partial_cmp(&a.2)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
            .then_with(|| a.1.cmp(&b.1))
    });

    let mut incident_counts: HashMap<String, usize> = HashMap::new();
    let mut pruned_edges = Vec::with_capacity(raw_edges.len());

    for (src, tgt, w) in raw_edges {
        let sc = incident_counts.get(&src).copied().unwrap_or(0);
        let tc = incident_counts.get(&tgt).copied().unwrap_or(0);
        if sc >= edge_cap || tc >= edge_cap {
            continue;
        }
        *incident_counts.entry(src.clone()).or_insert(0) += 1;
        *incident_counts.entry(tgt.clone()).or_insert(0) += 1;

        let mut map = HashMap::new();
        map.insert("source".to_string(), src.into_py_any(py)?);
        map.insert("target".to_string(), tgt.into_py_any(py)?);
        map.insert("weight".to_string(), w.into_py_any(py)?);
        pruned_edges.push(map);
    }

    Ok(pruned_edges)
}

#[cfg(test)]
mod zero_evidence_tests {
    use super::zero_evidence_below_threshold as skip;

    #[test]
    fn low_affinity_is_safe_but_rounding_and_large_multipliers_retain_postings() {
        assert!(skip(Some(0.5), 1.0, Some(1.0), 1.5));
        assert!(skip(Some(1.49949), 1.0, Some(1.0), 1.5));
        assert!(!skip(Some(1.4995), 1.0, Some(1.0), 1.5));
        assert!(!skip(Some(0.5), 2.0, Some(2.0), 1.5));
        assert!(!skip(Some(0.5), 1.0, Some(1.0), 0.0));
    }

    #[test]
    fn nonfinite_negative_or_overflow_inputs_disable_the_optimization() {
        for bad in [f64::NAN, f64::INFINITY, -1.0] {
            assert!(!skip(Some(bad), 1.0, Some(1.0), 1.5));
            assert!(!skip(Some(0.5), bad, Some(1.0), 1.5));
            assert!(!skip(Some(0.5), 1.0, Some(bad), 1.5));
        }
        assert!(!skip(None, 1.0, Some(1.0), 1.5));
        assert!(!skip(Some(0.5), 1.0, None, 1.5));
        assert!(!skip(Some(0.5), f64::MAX, Some(f64::MAX), 1.5));
    }
}
