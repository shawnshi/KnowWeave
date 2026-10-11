use pulldown_cmark::{Event, Parser, Tag, TagEnd};
use pyo3::prelude::*;

#[pyclass]
#[derive(Clone, Debug)]
pub struct MarkdownBlock {
    #[pyo3(get)]
    pub kind: String,
    #[pyo3(get)]
    pub heading: Option<String>,
    #[pyo3(get)]
    pub raw_text: String,
    #[pyo3(get)]
    pub text: String,
}

#[pymethods]
impl MarkdownBlock {
    fn __repr__(&self) -> String {
        format!(
            "MarkdownBlock(kind={:?}, heading={:?}, text_len={})",
            self.kind,
            self.heading,
            self.text.len()
        )
    }
}

/// 清洗 claim 文本中的 Wikilink 与 Typed Link，使其变成纯文本
pub fn clean_claim_text(raw: &str, limit: usize) -> String {
    let mut out = String::with_capacity(raw.len());
    let mut chars = raw.chars().peekable();

    while let Some(c) = chars.next() {
        if c == '[' {
            // 检查是否为 typed link 或 wikilink
            let rest: String = chars.clone().collect();
            if let Some(end_idx) = rest.find(']') {
                let inner = &rest[..end_idx];
                // 如果是 [[Target|Alias]] 或 [pred:: [[Target|Alias]]]
                if inner.starts_with('[') && inner.ends_with(']') {
                    let link_content = &inner[1..inner.len() - 1];
                    let text_to_use = if let Some(pipe_idx) = link_content.rfind('|') {
                        &link_content[pipe_idx + 1..]
                    } else {
                        link_content
                    };
                    out.push_str(text_to_use);
                    for _ in 0..=end_idx {
                        chars.next();
                    }
                    continue;
                }
            }
        }
        out.push(c);
        if out.chars().count() >= limit {
            break;
        }
    }
    out
}

/// 快速分割 Frontmatter 与 Body
#[pyfunction]
pub fn fast_split_frontmatter(content: &str) -> (String, String) {
    if !content.starts_with("---\n") && !content.starts_with("---\r\n") {
        return (String::new(), content.to_string());
    }

    // Include the opening line's newline so an empty YAML section can close immediately.
    // Match complete closing lines, as the Python reference's \r?\n---(?:\r?\n|$) does.
    for (offset, _) in content[3..].match_indices("\n---") {
        let newline = 3 + offset;
        let delimiter_end = newline + 4;
        let suffix = &content[delimiter_end..];
        let body_start = if suffix.starts_with("\r\n") {
            delimiter_end + 2
        } else if suffix.starts_with('\n') {
            delimiter_end + 1
        } else if suffix.is_empty() {
            delimiter_end
        } else {
            continue;
        };
        let yaml_end = if content.as_bytes()[newline - 1] == b'\r' {
            newline - 1
        } else {
            newline
        };
        // Python slices from byte 4 even for CRLF; its slice is empty when end < start.
        let yaml_part = &content[4..yaml_end.max(4)];
        return (yaml_part.to_string(), content[body_start..].to_string());
    }
    (String::new(), content.to_string())
}

/// 快速统计指定章节标记下的列表项数量
#[pyfunction]
pub fn fast_count_list_items(body: &str, section_marker: &str) -> usize {
    let mut count = 0;
    let mut in_section = false;

    for line in body.lines() {
        let stripped = line.trim();
        if stripped.starts_with("## ") {
            in_section = stripped.contains(section_marker);
        } else if stripped.starts_with("# ") {
            in_section = false;
        }

        if in_section && (stripped.starts_with("- ") || stripped.starts_with("* ")) {
            count += 1;
        }
    }

    count
}

/// The block-extraction contract this build implements: `kind`/`heading`/`raw_text` byte-identical
/// to `claim_extractor._iter_blocks` (mistune) on well-formed bodies, with the carve-outs recorded
/// in that module.  Callers **must** check this before using `fast_extract_blocks`.
///
/// Why a marker instead of a bare `hasattr`: the pre-2026-09-25 build exported
/// `fast_extract_blocks` too, with different semantics (280-character cleaning, nested list items
/// emitted, headings inside lists moving `current_heading`, code blocks appended).  A presence check
/// therefore accepts a build that silently changes the claim corpus -- measured live on
/// 2026-09-25, when the installed wheel was still that older build while the Python side had already
/// been switched to prefer the Rust path.
#[pyfunction]
pub fn blocks_contract() -> &'static str {
    "claim-blocks-parity-2026-09-25"
}

/// Extract the blocks claim extraction consumes: top-level paragraphs and the items of top-level
/// lists, each with the text mistune's `extract_text` would have produced.
///
/// This is a **parity port**, not an approximation.  `vector_lake/claim_extractor.py::_iter_blocks`
/// walks mistune's AST and emits a block per top-level `paragraph` and per item of a top-level
/// `list`; the extraction rule it applies (`extract_text`) is: a code block contributes a single
/// space and nothing else, a soft/hard break contributes a space, and a node contributes its own
/// `raw` followed by its children's.  Three things therefore differ from the naive pulldown walk
/// this function used to do, and each was a measured divergence:
///
/// * **code blocks are skipped** (they used to be appended to whatever buffer was open);
/// * **only items of a top-level list emit a block** -- a nested list's items are not separate
///   claims, their text accumulates into the enclosing item, which is what mistune's recursion
///   does when it walks the outer `list_item`;
/// * **paragraphs inside a list item do not emit** (the Python walk never visits them).
///
/// `raw_text` is the extracted text; `text` repeats it uncleaned on purpose.  Porting
/// `_clean_claim_text` here would mean re-implementing a five-step regex chain (whitespace
/// collapse, non-greedy `(Source: ...)` stripping, typed links, legacy links, truncation) without
/// the regex crate, which is exactly where a silent divergence would come from -- so the cleaner
/// stays in Python and the parity test compares the text this function returns.
#[pyfunction]
pub fn fast_extract_blocks(body: &str) -> Vec<MarkdownBlock> {
    let parser = Parser::new(body);
    let mut blocks = Vec::new();

    let mut current_heading: Option<String> = None;
    // What is accumulating now: a heading, a top-level paragraph, or a top-level list item.
    #[derive(PartialEq)]
    enum Buffer {
        Heading,
        Paragraph,
        Item,
    }
    let mut buffer: Option<Buffer> = None;
    let mut text = String::new();
    // Nesting of `Item` starts.  Only depth 1 emits; deeper items share the enclosing buffer.
    let mut item_depth: usize = 0;
    // The Python walk visits only the *top level* of mistune's AST: `block_quote`, `table` and
    // footnote definitions are never entered, so nothing inside them is a claim.  Measured -- a
    // templated blockquote (`> **Chunking Rule...**`, shipped in the page template) was being
    // emitted as a paragraph here and shifted every later block on those pages by one.
    let mut list_depth: usize = 0;
    let mut other_container_depth: usize = 0;
    let mut in_code_block = false;

    for event in parser {
        match event {
            Event::Start(Tag::Heading { .. }) => {
                // Nested headings contribute text to an enclosing item, not a new buffer.
                if list_depth == 0 && other_container_depth == 0 && item_depth == 0 {
                    buffer = Some(Buffer::Heading);
                    text.clear();
                }
            }
            Event::End(TagEnd::Heading(_)) => {
                // Only the buffer opened by a top-level heading may be consumed here.
                if buffer == Some(Buffer::Heading) {
                    current_heading = Some(text.trim().to_string());
                    text.clear();
                    buffer = None;
                }
            }
            Event::Start(Tag::Paragraph) => {
                // A paragraph inside a list item, a blockquote or a table is not its own claim.
                if item_depth == 0 && list_depth == 0 && other_container_depth == 0 {
                    buffer = Some(Buffer::Paragraph);
                    text.clear();
                }
            }
            Event::End(TagEnd::Paragraph) => {
                if item_depth == 0
                    && list_depth == 0
                    && other_container_depth == 0
                    && buffer == Some(Buffer::Paragraph)
                {
                    let raw = text.trim().to_string();
                    if !raw.is_empty() {
                        blocks.push(MarkdownBlock {
                            kind: "paragraph".to_string(),
                            heading: current_heading.clone(),
                            raw_text: raw.clone(),
                            text: raw,
                        });
                    }
                    text.clear();
                    buffer = None;
                }
            }
            Event::Start(Tag::List(_)) => {
                list_depth += 1;
            }
            Event::End(TagEnd::List(_)) => {
                list_depth = list_depth.saturating_sub(1);
            }
            Event::Start(Tag::BlockQuote(_))
            | Event::Start(Tag::Table(_))
            | Event::Start(Tag::TableHead)
            | Event::Start(Tag::TableRow)
            | Event::Start(Tag::TableCell)
            | Event::Start(Tag::FootnoteDefinition(_)) => {
                other_container_depth += 1;
            }
            Event::End(TagEnd::BlockQuote(_))
            | Event::End(TagEnd::Table)
            | Event::End(TagEnd::TableHead)
            | Event::End(TagEnd::TableRow)
            | Event::End(TagEnd::TableCell)
            | Event::End(TagEnd::FootnoteDefinition) => {
                other_container_depth = other_container_depth.saturating_sub(1);
            }
            Event::Start(Tag::Item) => {
                item_depth += 1;
                if item_depth == 1 && list_depth == 1 && other_container_depth == 0 {
                    buffer = Some(Buffer::Item);
                    text.clear();
                }
                // Deeper: keep accumulating into the enclosing item's buffer.
            }
            Event::End(TagEnd::Item) => {
                if item_depth == 1
                    && list_depth == 1
                    && other_container_depth == 0
                    && buffer == Some(Buffer::Item)
                {
                    let raw = text.trim().to_string();
                    if !raw.is_empty() {
                        blocks.push(MarkdownBlock {
                            kind: "bullet".to_string(),
                            heading: current_heading.clone(),
                            raw_text: raw.clone(),
                            text: raw,
                        });
                    }
                    text.clear();
                    buffer = None;
                }
                item_depth = item_depth.saturating_sub(1);
            }
            Event::Start(Tag::CodeBlock(_)) => {
                in_code_block = true;
                // ``block_code`` contributes one space, and only where a buffer is open (a
                // top-level code block belongs to no emitted block at all).
                if buffer.is_some() {
                    text.push(' ');
                }
            }
            Event::End(TagEnd::CodeBlock) => {
                in_code_block = false;
            }
            Event::Text(t) => {
                if buffer.is_some() && !in_code_block {
                    text.push_str(&t);
                }
            }
            // Inline code and inline HTML are text in mistune's ``raw`` walk.
            Event::Code(c) => {
                if buffer.is_some() && !in_code_block {
                    text.push_str(&c);
                }
            }
            Event::Html(h) | Event::InlineHtml(h) => {
                if buffer.is_some() && !in_code_block {
                    text.push_str(&h);
                }
            }
            // A soft break becomes a space; a hard break becomes nothing.  That asymmetry is
            // mistune's, not a choice: mistune 3 emits the token ``linebreak`` for a hard break,
            // and `claim_extractor.extract_text` only special-cases ``("softbreak",
            // "hardbreak")`` -- so ``linebreak`` falls through to ``node.get("raw", "")`` and
            // contributes nothing.  The obvious port (space for both) is what the first parity run
            // measured: 7 pages differing by exactly the number of hard breaks.
            Event::SoftBreak => {
                if buffer.is_some() && !in_code_block {
                    text.push(' ');
                }
            }
            Event::HardBreak => {}
            _ => {}
        }
    }

    blocks
}

/// 快速提取正文中的 Wikilinks: `[[Target]]` or `[[Target|Alias]]`
#[pyfunction]
pub fn fast_extract_wikilinks(content: &str) -> Vec<(String, Option<String>)> {
    let mut links = Vec::new();
    let mut chars = content.char_indices().peekable();

    while let Some((i, c)) = chars.next() {
        if c == '[' && content[i..].starts_with("[[") {
            let start = i + 2;
            if let Some(end_offset) = content[start..].find("]]") {
                let inner = &content[start..start + end_offset];
                if !inner.contains('\n') && !inner.is_empty() {
                    if let Some(pipe_pos) = inner.find('|') {
                        let target = inner[..pipe_pos].trim().to_string();
                        let alias = inner[pipe_pos + 1..].trim().to_string();
                        links.push((target, Some(alias)));
                    } else {
                        links.push((inner.trim().to_string(), None));
                    }
                }
            }
        }
    }

    links
}
