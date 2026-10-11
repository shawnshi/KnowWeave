use pyo3::prelude::*;

mod gram_index;
mod graph_backbone;
mod graph_fusion;
mod graph_topology;
mod local_bm25;
mod markdown;
mod name_pairs;
mod text_similarity;
mod tokenizer;

#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn vector_lake_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;

    // 模块 1: 内存倒排索引加速
    m.add_function(wrap_pyfunction!(gram_index::pack_postings, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::unpack_postings, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::accumulate_postings, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::fast_accumulate_terms, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::extract_grams, m)?)?;
    m.add_function(wrap_pyfunction!(gram_index::intersect_postings, m)?)?;

    // 模块 2: Markdown 极速解析
    m.add_class::<markdown::MarkdownBlock>()?;
    m.add_function(wrap_pyfunction!(markdown::fast_split_frontmatter, m)?)?;
    m.add_function(wrap_pyfunction!(markdown::fast_count_list_items, m)?)?;
    m.add_function(wrap_pyfunction!(markdown::blocks_contract, m)?)?;
    m.add_function(wrap_pyfunction!(markdown::fast_extract_blocks, m)?)?;
    m.add_function(wrap_pyfunction!(markdown::fast_extract_wikilinks, m)?)?;

    // 模块 3: 图遍历与召回融合
    m.add_function(wrap_pyfunction!(
        graph_fusion::fast_personalized_pagerank,
        m
    )?)?;
    m.add_class::<graph_fusion::PprIndex>()?;
    m.add_function(wrap_pyfunction!(
        graph_fusion::prepared_personalized_pagerank,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        graph_fusion::fast_reciprocal_rank_fusion,
        m
    )?)?;

    // 模块 4: 图拓扑加权边计算
    m.add_function(wrap_pyfunction!(
        graph_topology::fast_calculate_weighted_edges,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(graph_backbone::fast_backbone_indices, m)?)?;

    // 模块 5: 实体相似度与名称碰撞审查
    m.add_function(wrap_pyfunction!(
        text_similarity::fast_sequence_matcher_ratio,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        text_similarity::fast_batch_sequence_matcher_ratios,
        m
    )?)?;
    m.add_class::<name_pairs::NamePairIndex>()?;

    // 模块 6: 候选池局部 BM25 内存重排
    m.add_function(wrap_pyfunction!(local_bm25::fast_bm25_rerank, m)?)?;

    // 模块 7: CJK 分词（jieba-rs 由本 crate 自身钉定版本，不再由 rjieba wheel 决定）
    m.add_function(wrap_pyfunction!(tokenizer::cut, m)?)?;
    m.add_function(wrap_pyfunction!(tokenizer::cut_joined, m)?)?;
    m.add_class::<tokenizer::ProfileTokenizer>()?;

    Ok(())
}
