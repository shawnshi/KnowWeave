"""Offline graph rendering: injection boundaries, loader compatibility and truthful status."""
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from vector_lake import tool_graph


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.ids = set()
        self.policy = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.scripts.append(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy":
            self.policy = attrs.get("content") or ""


def _payload():
    return {
        "pageGraph": {"nodes": [], "edges": [], "community_labels": {}},
        "claimGraph": {"nodes": [], "edges": [], "community_labels": {}},
        "meta": {"communities_stale": True, "read_consistency": "locked"},
    }


def _parse_data(html):
    match = re.search(r"const rawData = (.*);\nconst memoryBasePath", html)
    assert match is not None
    return json.loads(match.group(1))


def test_renderer_is_offline_and_self_contained(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_graph.webbrowser, "open", lambda *args: pytest.fail("renderer opened browser"))
    monkeypatch.setattr(tool_graph.governance_store, "ensure_canonical_store_populated", lambda: pytest.fail("renderer accessed lake state"))
    html = tool_graph._render_graph_html(_payload(), str(tmp_path))
    parsed = _HTML()
    parsed.feed(html)
    assert len(parsed.scripts) == 2
    assert all("src" not in attrs for attrs in parsed.scripts)
    assert "connect-src 'none'" in parsed.policy
    assert "font-src 'none'" in parsed.policy
    assert "%%GRAPH_SCRIPT%%" not in html
    assert "KnowWeaveGraph" in html
    assert _parse_data(html) == _payload()
    assert list(tmp_path.iterdir()) == []


def test_payload_cannot_close_a_script_or_reenter_template_substitution(tmp_path):
    payload = _payload()
    text = '</script><img src="https://invalid.example/x" onerror="alert(1)"> & %%GRAPH_SCRIPT%%'
    payload["pageGraph"]["nodes"] = [{"id": "Concept_Test", "summary": text}]
    html = tool_graph._render_graph_html(payload, str(tmp_path))
    parsed = _HTML()
    parsed.feed(html)
    assert len(parsed.scripts) == 2
    assert '<img src="https://invalid.example/x"' not in html
    assert _parse_data(html)["pageGraph"]["nodes"][0]["summary"] == text


def test_generated_html_javascript_preserves_optional_catalog_fields_and_untrusted_text(tmp_path, monkeypatch):
    from vector_lake import relation_catalog

    attack = '</script><img src=x onerror="throw 1"> & {{status}} %%GRAPH_DATA%%'
    relation = {
        "source": "Product_Synthetic", "target": "Concept_Synthetic", "predicate": "part-of",
        "layer": "declared", "declared": True, "weight": None, "verified": False,
        "subject_scope": "document", "resolution_status": "resolved",
        "context": {"claim_ids": ["synthetic-claim"], "note": attack},
        "origin": {"page_key": "Product_Synthetic"},
        "provenance_status": "unverified", "temporal": {"valid_from": "2026-01-01"},
    }
    monkeypatch.setattr(relation_catalog, "build_relation_catalog", lambda *args, **kwargs: {
        "relations": [relation], "diagnostics": [], "scope": {}, "truncated": False})
    payload = tool_graph._build_graph_payload({"nodes": {
        "Product_Synthetic": {"status": "seed", "epistemic_status": attack},
        "Concept_Synthetic": {},
    }}, max_page_edges=0)
    html = tool_graph._render_graph_html(payload, str(tmp_path))
    assert "<img src=x" not in html
    parsed = _HTML()
    parsed.feed(html)
    assert len(parsed.scripts) == 2
    assert _parse_data(html) == payload
    result = subprocess.run(["node", "-e", r'''
const fs = require('node:fs'), vm = require('node:vm');
const html = fs.readFileSync(0, 'utf8'), context = vm.createContext({});
for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInContext(match[1], context);
process.stdout.write(vm.runInContext(`JSON.stringify((() => {
  const graph = KnowWeaveGraph.buildGraph(rawData.pageGraph, 'page');
  return {nodes: graph.nodes, edges: graph.edges};
})())`, context));
'''], input=html, text=True, encoding="utf-8", capture_output=True, check=True, timeout=10)
    rendered = json.loads(result.stdout)
    product = next(node for node in rendered["nodes"] if node["id"] == "Product_Synthetic")
    assert product["status"] == "seed"
    assert product["epistemic_status"] == attack
    assert product["lifecycle_status"] is None
    assert len(rendered["edges"]) == 1
    assert rendered["edges"][0]["weight"] is None
    assert rendered["edges"][0]["declarations"] == [relation]


def test_memory_base_is_an_encoded_file_uri(tmp_path):
    root = tmp_path / 'memory # space & 中文'
    html = tool_graph._render_graph_html(_payload(), str(root))
    match = re.search(r"const memoryBasePath = (.*);", html)
    assert match is not None
    base = json.loads(match.group(1))
    assert base == root.resolve().as_uri() + "/"
    assert "%23" in base and "%20" in base


def test_missing_template_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_graph, "get_extension_root", lambda: tmp_path)
    with pytest.raises(FileNotFoundError, match="topology.html"):
        tool_graph._render_graph_html(_payload(), str(tmp_path))


def test_duplicate_marker_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "topology.html").write_text("%%GRAPH_DATA%% %%GRAPH_DATA%% %%MEMORY_BASE_PATH%%", encoding="utf-8")
    monkeypatch.setattr(tool_graph, "get_extension_root", lambda: tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        tool_graph._render_graph_html(_payload(), str(tmp_path))


def test_template_controls_have_named_equivalents_and_no_fake_time_or_score(tmp_path):
    html = tool_graph._render_graph_html(_payload(), str(tmp_path))
    parsed = _HTML()
    parsed.feed(html)
    assert {"stale-warning", "tab-list", "search-input", "type-filters", "network-dialog"} <= parsed.ids
    assert "timeline-slider" not in parsed.ids
    assert "inspect-score" not in parsed.ids
    assert 'aria-label="匹配节点"' in html
    assert 'prefers-reduced-motion: reduce' in html
    assert 'pointerdown' in html
    assert '"Microsoft YaHei UI"' in html and '"DengXian"' in html
    assert 'getComputedStyle(canvas).fontFamily' in html


def _contrast(first, second):
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in channels]
        return sum(x * w for x, w in zip(linear, [.2126, .7152, .0722]))
    low, high = sorted((luminance(first), luminance(second)))
    return (high + .05) / (low + .05)


@pytest.mark.parametrize("background", ["--bg", "--panel", "--panel-raised"])
@pytest.mark.parametrize("foreground", ["--ink", "--secondary", "--muted", "--accent"])
def test_actual_ui_text_tokens_meet_normal_text_contrast(foreground, background):
    html = (Path(__file__).parents[1] / "templates" / "topology.html").read_text(encoding="utf-8")
    tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", html))
    assert _contrast(tokens[foreground], tokens[background]) >= 4.5


def test_template_remains_compatible_with_the_resident_renderer():
    """A cached old MCP module can still render the new single-file template."""
    html = (Path(__file__).parents[1] / "templates" / "topology.html").read_text(encoding="utf-8")
    html = html.replace("%%GRAPH_DATA%%", json.dumps(_payload()))
    html = html.replace("%%MEMORY_BASE_PATH%%", "file:///C:/MEMORY/")
    assert "%%" not in html
    assert _parse_data(html) == _payload()
    assert "KnowWeaveGraph" in html
    assert "const memoryBasePath = \"file:///C:/MEMORY/\";" in html


def test_graph_brand_inlines_the_knowledge_graph_asset(tmp_path):
    root = Path(__file__).parents[1]
    html = tool_graph._render_graph_html(_payload(), str(tmp_path))
    match = re.search(r'<span class="brand-mark" aria-hidden="true">(<svg.*?</svg>)</span>', html, re.S)
    assert match is not None
    expected = ET.parse(root / "knowledge-graph.svg").getroot()
    actual = ET.fromstring(match.group(1))
    assert ET.tostring(actual) == ET.tostring(expected)
    assert actual.attrib["width"] == actual.attrib["height"] == "24"
    assert 'stroke="currentColor"' in match.group(1)
    assert '<img src="knowledge-graph.svg"' not in html


def test_readme_uses_the_project_app_icon():
    root = Path(__file__).parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert '<img src="knowweave-app.svg" alt="KnowWeave 项目图标"' in readme
    assert (root / "knowweave-app.svg").is_file()
