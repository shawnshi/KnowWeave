use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};

/// 快速 Personalized PageRank (PPR) 随机游走
/// 严格对齐 `tool_search.py` 的算法公式
#[pyfunction]
#[pyo3(signature = (adj, seed_keys, alpha=None, steps=None))]
pub fn fast_personalized_pagerank(
    adj: HashMap<String, Vec<(String, f64)>>,
    seed_keys: Vec<String>,
    alpha: Option<f64>,
    steps: Option<usize>,
) -> Vec<(String, f64)> {
    if seed_keys.is_empty() || adj.is_empty() {
        return Vec::new();
    }

    let alpha_val = alpha.unwrap_or(0.85);
    let step_count = steps.unwrap_or(2);
    let seed_set: HashSet<String> = seed_keys.iter().cloned().collect();
    let num_seeds = seed_set.len() as f64;
    let restart_mass = (1.0 - alpha_val) / num_seeds;

    let mut ppr_scores: HashMap<String, f64> = HashMap::with_capacity(seed_set.len());
    for seed in &seed_set {
        ppr_scores.insert(seed.clone(), 1.0 / num_seeds);
    }

    for _ in 0..step_count {
        let mut next_scores: HashMap<String, f64> = HashMap::with_capacity(adj.len());
        for k in adj.keys() {
            let initial = if seed_set.contains(k) {
                restart_mass
            } else {
                0.0
            };
            next_scores.insert(k.clone(), initial);
        }

        let mut sorted_nodes: Vec<String> = ppr_scores.keys().cloned().collect();
        sorted_nodes.sort();

        for node in &sorted_nodes {
            let current_score = ppr_scores[node];
            if let Some(neighbors) = adj.get(node) {
                let total_weight: f64 = neighbors.iter().map(|(_, w)| *w).sum();
                if total_weight <= 0.0 {
                    continue;
                }
                for (neighbor, w) in neighbors {
                    let mass = alpha_val * current_score * (w / total_weight);
                    *next_scores.entry(neighbor.clone()).or_insert(0.0) += mass;
                }
            }
        }
        ppr_scores = next_scores;
    }

    let mut result: Vec<(String, f64)> = ppr_scores.into_iter().collect();
    result.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    result
}

/// 预构建的 Personalized PageRank 索引。
///
/// 为什么存在：`fast_personalized_pagerank` 每次调用都要把整张 Python 邻接表搬进 Rust
/// （40k 条边，每条边两个 String）、每轮克隆 7.6k 个键并排序、每条边再 clone 一次邻居名。
/// 实测单次 **34 ms**，其中几乎全是堆分配而不是浮点计算。预构建把这些一次性做掉，并把
/// 节点名换成整数 id，热循环里不再出现任何字符串分配。
///
/// **输出与旧函数逐位相同**，因为浮点累加顺序被完整保留：外层仍是“节点名字典序”，
/// 内层仍是“邻接表里的原始邻居顺序”，行权和仍是按原顺序左折叠求和。
#[pyclass]
#[derive(Clone)]
pub struct PprIndex {
    /// 所有节点名，字典序。Python 的 str 排序与 UTF-8 字节序一致，故两处顺序相同。
    nodes: Vec<String>,
    index_of: HashMap<String, u32>,
    /// 该节点是否是输入邻接表的键：旧实现里只有键会在每轮被重置为 restart_mass，
    /// 也只会在结果里以 0.0 分的形态出现。
    in_adj: Vec<bool>,
    edges: Vec<Vec<(u32, f64)>>,
    row_sum: Vec<f64>,
}

#[pymethods]
impl PprIndex {
    #[new]
    pub fn new(adj: HashMap<String, Vec<(String, f64)>>) -> Self {
        // 节点集 = 邻接表键 ∪ 邻居目标。悬挂目标（只作为邻居出现）必须收进来：旧实现里
        // 它们能通过 entry 拿分并出现在结果中。
        let mut nodes: Vec<String> = adj.keys().cloned().collect();
        for (_source, neighbors) in adj.iter() {
            for (target, _weight) in neighbors {
                if !adj.contains_key(target) {
                    nodes.push(target.clone());
                }
            }
        }
        nodes.sort();
        nodes.dedup();

        let index_of: HashMap<String, u32> = nodes
            .iter()
            .enumerate()
            .map(|(i, key)| (key.clone(), i as u32))
            .collect();
        let in_adj: Vec<bool> = nodes.iter().map(|key| adj.contains_key(key)).collect();

        let mut edges: Vec<Vec<(u32, f64)>> = vec![Vec::new(); nodes.len()];
        let mut row_sum: Vec<f64> = vec![0.0; nodes.len()];
        for (source, neighbors) in adj.iter() {
            let i = index_of[source] as usize;
            let mut list: Vec<(u32, f64)> = Vec::with_capacity(neighbors.len());
            let mut total = 0.0f64;
            for (target, weight) in neighbors {
                list.push((index_of[target], *weight));
                total += *weight;
            }
            edges[i] = list;
            row_sum[i] = total;
        }

        PprIndex {
            nodes,
            index_of,
            in_adj,
            edges,
            row_sum,
        }
    }

    fn __len__(&self) -> usize {
        self.nodes.len()
    }

    /// 已解析出边的节点数，供 Python 侧的缓存键与自检使用。
    fn edge_count(&self) -> usize {
        self.edges.iter().map(|row| row.len()).sum()
    }
}

/// 在预构建索引上做 Personalized PageRank；与 `fast_personalized_pagerank` 逐位相同。
#[pyfunction]
#[pyo3(signature = (index, seed_keys, alpha=None, steps=None))]
pub fn prepared_personalized_pagerank(
    index: &PprIndex,
    seed_keys: Vec<String>,
    alpha: Option<f64>,
    steps: Option<usize>,
) -> Vec<(String, f64)> {
    let node_count = index.nodes.len();
    if seed_keys.is_empty() || node_count == 0 {
        return Vec::new();
    }

    let alpha_val = alpha.unwrap_or(0.85);
    let step_count = steps.unwrap_or(2);
    let seed_set: HashSet<&String> = seed_keys.iter().collect();
    let num_seeds = seed_set.len() as f64;
    if step_count == 0 {
        // No propagation: legacy retains every initial seed, including unknown keys.
        let mut result: Vec<(String, f64)> = seed_set
            .iter()
            .map(|seed| (seed.to_string(), 1.0 / num_seeds))
            .collect();
        result.sort_by(|a, b| a.0.cmp(&b.0));
        return result;
    }
    let restart_mass = (1.0 - alpha_val) / num_seeds;

    // 未知种子（不在图里）仍然计入分母：旧实现用整个 seed_set 的长度算 restart_mass，
    // 而它没有出边，所以只需略过传播。
    //
    // ``present`` 必须显式跟踪：旧实现的键集不是“分数非零”的集合，而是一个 HashMap 的
    // 键集——每轮初始化为全部 adj 键，累积阶段又会把邻居（哪怕质量是 0.0）插进去。
    // 只按分数判存在会丢掉“零分但存在”的项，边界用例已抓到这一点（种子完全不在图里时，
    // 旧函数仍会把零分节点的邻居以 0.0 分返回）。
    let mut seed_mask = vec![false; node_count];
    let mut cur = vec![0.0f64; node_count];
    let mut cur_present = vec![false; node_count];
    for seed in &seed_set {
        if let Some(&i) = index.index_of.get(seed.as_str()) {
            seed_mask[i as usize] = true;
            cur[i as usize] = 1.0 / num_seeds;
            cur_present[i as usize] = true;
        }
    }

    let mut next = vec![0.0f64; node_count];
    let mut next_present = vec![false; node_count];
    for _ in 0..step_count {
        for i in 0..node_count {
            next[i] = if index.in_adj[i] && seed_mask[i] {
                restart_mass
            } else {
                0.0
            };
            next_present[i] = index.in_adj[i];
        }
        for i in 0..node_count {
            // 旧实现只遍历上一轮键集里的节点；分数为零仍会处理，因为邻居会被插入。
            if !cur_present[i] {
                continue;
            }
            let score = cur[i];
            let total = index.row_sum[i];
            if total <= 0.0 {
                continue;
            }
            for (target, weight) in &index.edges[i] {
                let j = *target as usize;
                next[j] += alpha_val * score * (weight / total);
                next_present[j] = true;
            }
        }
        std::mem::swap(&mut cur, &mut next);
        std::mem::swap(&mut cur_present, &mut next_present);
    }

    let mut result: Vec<(String, f64)> = Vec::with_capacity(node_count);
    for i in 0..node_count {
        if cur_present[i] {
            result.push((index.nodes[i].clone(), cur[i]));
        }
    }
    result.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    result
}

/// 快速 Reciprocal Rank Fusion (RRF) 融合
#[pyfunction]
#[pyo3(signature = (ranked_lists, k=None))]
pub fn fast_reciprocal_rank_fusion(
    ranked_lists: Vec<Vec<String>>,
    k: Option<f64>,
) -> Vec<(String, f64)> {
    let rrf_k = k.unwrap_or(60.0);
    let mut scores: HashMap<String, f64> = HashMap::new();

    for list in ranked_lists {
        for (rank, key) in list.into_iter().enumerate() {
            let contribution = 1.0 / (rrf_k + rank as f64 + 1.0);
            *scores.entry(key).or_insert(0.0) += contribution;
        }
    }

    let mut out: Vec<(String, f64)> = scores.into_iter().collect();
    out.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    out
}
