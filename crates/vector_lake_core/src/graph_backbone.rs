//! Stable backbone selection over dense node ids; normalization stays in Python.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashSet;

type Edge = (u32, u32, f64, usize);

#[pyfunction]
pub fn fast_backbone_indices(
    py: Python<'_>,
    node_count: usize,
    edges: Vec<Edge>,
    max_edges: usize,
) -> PyResult<Vec<usize>> {
    if node_count > 100_000 || edges.len() > 1_000_000 {
        return Err(PyValueError::new_err("backbone capacity exceeded"));
    }
    if edges.iter().any(|&(a, b, w, _)| {
        a as usize >= node_count || b as usize >= node_count || a == b || !w.is_finite()
    }) {
        return Err(PyValueError::new_err(
            "backbone requires valid distinct endpoints and finite weights",
        ));
    }
    Ok(py.detach(move || select(node_count, edges, max_edges)))
}

fn select(node_count: usize, mut edges: Vec<Edge>, max_edges: usize) -> Vec<usize> {
    if max_edges == 0 {
        return Vec::new();
    }
    // Stable sorting preserves the first raw row when endpoint and weight keys tie.
    edges.sort_by(|a, b| {
        b.2.partial_cmp(&a.2)
            .unwrap()
            .then_with(|| a.0.cmp(&b.0))
            .then_with(|| a.1.cmp(&b.1))
    });
    let mut seen = HashSet::new();
    edges.retain(|&(a, b, _, _)| seen.insert((a.min(b), a.max(b))));
    if edges.len() <= max_edges {
        return edges.into_iter().map(|e| e.3).collect();
    }
    let mut chosen = vec![false; edges.len()];
    let mut covered = vec![false; node_count];
    let mut count = 0;
    for (index, &(a, b, _, _)) in edges.iter().enumerate() {
        if covered[a as usize] || covered[b as usize] {
            continue;
        }
        chosen[index] = true;
        covered[a as usize] = true;
        covered[b as usize] = true;
        count += 1;
        if count >= max_edges {
            break;
        }
    }
    if count < max_edges {
        for (index, &(a, b, _, _)) in edges.iter().enumerate() {
            if chosen[index] || (covered[a as usize] && covered[b as usize]) {
                continue;
            }
            chosen[index] = true;
            covered[a as usize] = true;
            covered[b as usize] = true;
            count += 1;
            if count >= max_edges {
                break;
            }
        }
    }
    if count < max_edges {
        for item in &mut chosen {
            if !*item {
                *item = true;
                count += 1;
                if count >= max_edges {
                    break;
                }
            }
        }
    }
    edges
        .into_iter()
        .zip(chosen)
        .filter_map(|(edge, keep)| keep.then_some(edge.3))
        .collect()
}
