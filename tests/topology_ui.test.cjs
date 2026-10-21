'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../templates/topology.html'), 'utf8');
const script = [...template.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
vm.runInThisContext(script, { filename: 'topology-inline.js' });
const ui = global.KnowWeaveGraph;

function fixture() {
  return { pageGraph: { nodes: [
    { id: 'Concept_A', name: 'Alpha 概念', raw_type: 'Concept', group: 'Medical_IT', domain: 'Medical_IT', community: 'c1', backbone_degree: 2, degree: 7, summary: '医院 interface test', sources: ['raw/public/证据 一.md'], alignment_score: 100 },
    { id: 'Source_B', name: 'Beta 来源', raw_type: 'Source', group: 'Medical_IT', domain: 'Medical_IT', community: 'c2', backbone_degree: 2 },
    { id: 'Product_C', name: 'Gamma 产品', raw_type: 'Product', group: 'General', domain: 'General', community: 'c3', backbone_degree: 1, status: 'Contested' },
    { id: 'Concept_D', name: 'Delta 孤立', raw_type: 'Concept', group: 'General', domain: 'General', backbone_degree: 0 }
  ], edges: [{ source: 'Concept_A', target: 'Source_B', weight: 12 }, { source: 'Source_B', target: 'Product_C', weight: 4 }], community_labels: { c1: '社区 Alpha', c2: '社区 Beta', c3: '社区 Gamma' } },
  claimGraph: { nodes: [ { id: 'claim_1', name: '主张一', validity_state: 'unsupported', confidence: .8, source_pages: [], sources: ['Source_B'] }, { id: 'claim_2', name: '主张二', validity_state: 'active', confidence: null } ], edges: [{ source: 'claim_1', target: 'claim_2', weight: 1 }] },
  meta: { communities_stale: true, read_consistency: 'locked', total_page_edges: 17, total_claim_edges: 9, generated_at: '2026-10-21T01:26:30Z' } };
}
const options = graph => ({ types: new Set(graph.nodes.map(ui.typeOf)), query: '', attention: false, isolated: false });

test('empty type selection means zero results, never implicit all', () => {
  const graph = ui.buildGraph(fixture().pageGraph, 'page');
  assert.equal(ui.filterNodes(graph, { ...options(graph), types: new Set() }).length, 0);
});
test('malformed records are reported and invalid edges cannot become visible', () => {
  const raw = fixture().pageGraph;
  raw.nodes.push(null, {}, { id: 'Concept_A' });
  raw.edges.push(null, { source: 'Concept_A', target: 'missing' }, { source: 'Concept_A', target: 'Concept_A' });
  const graph = ui.buildGraph(raw, 'page');
  assert.equal(graph.nodes.length, 4); assert.equal(graph.edges.length, 2); assert.equal(graph.rejected, 6);
});
test('search normalizes IDs and summaries without losing Chinese', () => {
  const graph = ui.buildGraph(fixture().pageGraph, 'page');
  assert.deepEqual(ui.filterNodes(graph, { ...options(graph), query: '医院 interface' }).map(node => node.id), ['Concept_A']);
  assert.deepEqual(ui.filterNodes(graph, { ...options(graph), query: 'concept-a' }).map(node => node.id), ['Concept_A']);
});
test('attention and isolated filters intersect with types', () => {
  const graph = ui.buildGraph(fixture().pageGraph, 'page');
  assert.deepEqual(ui.filterNodes(graph, { ...options(graph), attention: true }).map(node => node.id), ['Product_C']);
  assert.deepEqual(ui.filterNodes(graph, { ...options(graph), isolated: true }).map(node => node.id), ['Concept_D']);
  assert.equal(ui.filterNodes(graph, { ...options(graph), attention: true, isolated: true }).length, 0);
});
test('all communities are searchable; overview remainder preserves membership', () => {
  const raw = { nodes: Array.from({ length: 100 }, (_, i) => ({ id: `Concept_${i}`, community: `cluster_${i}`, raw_type: 'Concept' })), edges: [] };
  const graph = ui.buildGraph(raw, 'page'), groups = ui.groupsFor(graph, graph.nodes, 'community');
  assert.equal(groups.length, 100);
  const overview = ui.overview(graph, groups);
  assert.equal(overview.nodes.length, 13); assert.equal(overview.total, 100);
  assert.equal(overview.nodes.reduce((sum, group) => sum + group.members.length, 0), 100);
  assert.equal(overview.nodes.at(-1).remainder, true);
});
test('local one/two hop views use true neighbors, remain bounded and keep root', () => {
  const graph = ui.buildGraph(fixture().pageGraph, 'page');
  assert.deepEqual(ui.localNodes(graph, graph.nodes, 'Concept_A', 1).map(node => node.id), ['Concept_A', 'Source_B']);
  assert.deepEqual(ui.localNodes(graph, graph.nodes, 'Concept_A', 2).map(node => node.id), ['Concept_A', 'Source_B', 'Product_C']);
  const bounded = ui.nodeView(graph, ui.localNodes(graph, graph.nodes, 'Concept_A', 2), 2);
  assert.equal(bounded.total, 3); assert.equal(bounded.nodes.length, 2); assert.equal(bounded.edges.length, 1);
  assert.deepEqual(ui.localNodes(graph, [], 'Concept_A', 1), []);
});
test('graph-specific raw metrics never fall back to the other graph', () => {
  const data = fixture();
  assert.equal(ui.totalEdges(data.meta, 'page'), 17); assert.equal(ui.totalEdges(data.meta, 'claim'), 9);
  assert.equal(ui.totalEdges({}, 'claim'), null);
});
test('missing confidence stays unknown; no invented 100 percent', () => {
  assert.equal(ui.confidenceLabel(null), '未提供'); assert.equal(ui.confidenceLabel(undefined), '未提供');
  assert.equal(ui.confidenceLabel(NaN), '未提供'); assert.equal(ui.confidenceLabel(100), '未提供');
  assert.match(ui.confidenceLabel(.8), /^80%/);
});
test('only bounded local Markdown references become clickable; paths are encoded', () => {
  const base = 'file:///C:/MEMORY/';
  assert.equal(ui.evidenceURL('Source_声明', base), 'file:///C:/MEMORY/wiki/Source_%E5%A3%B0%E6%98%8E.md');
  assert.equal(ui.evidenceURL('raw/public/a #.md', base), 'file:///C:/MEMORY/raw/public/a%20%23.md');
  for (const value of ['https://example.test/x.md', 'javascript:alert(1)', '../private.md', '/etc/private.md', 'raw/../secret.md', 'C:\\secret.md', '//host/a.md', 'raw/a.exe']) assert.equal(ui.evidenceURL(value, base), null);
});
test('layout is deterministic, finite and not a causal distance encoding', () => {
  const graph = ui.buildGraph(fixture().pageGraph, 'page');
  const view = ui.overview(graph, ui.groupsFor(graph, graph.nodes, 'domain'));
  assert.deepEqual(ui.layout(view), ui.layout(view));
  assert.ok(ui.layout(view).every(node => Number.isFinite(node.x) && Number.isFinite(node.y)));
});

class FakeElement {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.attributes = {}; this.style = {}; this.hidden = false; this.value = ''; this.textContent = ''; this.classes = new Set(); this.classList = { add: name => this.classes.add(name), remove: name => this.classes.delete(name), toggle: name => this.classes.has(name) ? (this.classes.delete(name), false) : (this.classes.add(name), true) }; }
  set innerHTML(_value) { throw new Error('HTML injection sink is not allowed in topology UI'); }
  append(...items) { for (const item of items) { if (item instanceof FakeElement) item.parent = this; this.children.push(item); } }
  contains(node) { return this === node || this.children.some(child => child instanceof FakeElement && child.contains(node)); }
  get isConnected() { return !!this.rootAttached || !!this.parent?.isConnected; }
  replaceChildren(...items) {
    for (const child of this.children) if (child instanceof FakeElement) { if (child.contains(global.document?.activeElement)) global.document.activeElement = global.document.body; child.parent = null; }
    this.children = []; this.textContent = ''; this.append(...items);
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, handler) { (this.listeners[key] ??= []).push(handler); }
  async emit(type, fields = {}) { if (type === 'click' && this.disabled) return; const event = { type, target: this, currentTarget: this, preventDefault() {}, ...fields }; for (const handler of this.listeners[type] || []) await handler(event); }
  focus() { if (this.isConnected && !this.hidden && !this.disabled) global.document.activeElement = this; }
  select() {}
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { const wasOpen = this.open; this.open = false; if (wasOpen) setTimeout(() => { for (const handler of this.listeners.close || []) handler({ type: 'close', target: this }); }, 0); }
  remove() { this.removed = true; }
  closest() { return null; }
  getBoundingClientRect() { return { x: 0, y: 0, left: 0, top: 0, width: 800, height: 500 }; }
  getContext() { return this.context ??= { labels: [], setTransform() {}, clearRect() { this.labels = []; }, beginPath() {}, moveTo() {}, lineTo() {}, arc() {}, rect() {}, closePath() {}, stroke() {}, fill() {}, fillRect() {}, fillText(text) { this.labels.push({ text, color: this.fillStyle }); }, measureText: text => ({ width: text.length * 8 }) }; }
  setPointerCapture() {}
}
function appFixture({ canvas = true, payload = fixture() } = {}) {
  const html = fs.readFileSync(path.join(__dirname, '../templates/topology.html'), 'utf8');
  const ids = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new FakeElement()]));
  for (const node of ids.values()) node.rootAttached = true;
  ids.get('search-input').tagName = 'INPUT';
  if (!canvas) ids.get('graph-canvas').getContext = () => null;
  const body = new FakeElement('body'); body.rootAttached = true;
  global.document = { body, activeElement: body, getElementById: id => { assert.ok(ids.has(id), `missing ${id}`); return ids.get(id); }, createElement: tag => new FakeElement(tag), addEventListener() {}, head: new FakeElement('head') };
  global.matchMedia = () => ({ matches: false, addEventListener() {} });
  global.ResizeObserver = class { observe() {} };
  const frames = []; global.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  global.getComputedStyle = () => ({ fontFamily: 'Microsoft YaHei UI' });
  global.innerWidth = 1440; global.devicePixelRatio = 1;
  const app = ui.createApp(payload, 'file:///C:/MEMORY/');
  return { app, ids, frames, data: fixture(), text: element => [element.textContent, ...element.children.map(child => typeof child === 'string' ? child : [child.textContent, ...(child.children || []).map(item => item.textContent)].join(' '))].join(' ') };
}

function titleOf(ids) {
  const header = ids.get('detail-content').children.find(child => child.className === 'detail-heading');
  return header.children.find(child => child.tagName === 'H2');
}
test('Wiki title itself is a safe direct link to the corresponding page file', () => {
  const { app, ids } = appFixture(); app.chooseNode('Concept_A');
  const link = titleOf(ids).children.find(child => child.tagName === 'A');
  assert.ok(link, 'Page title must be an actual anchor, not a copy-path action');
  assert.equal(link.href, 'file:///C:/MEMORY/wiki/Concept_A.md');
  assert.equal(link.target, '_blank'); assert.equal(link.rel, 'noopener noreferrer');
  assert.match(link.getAttribute('aria-label'), /Alpha 概念/);
});
test('Wiki title links encode literal filename punctuation and Chinese text', () => {
  const payload = fixture(); payload.pageGraph.nodes[0].id = 'Concept_标题 #&';
  const { app, ids } = appFixture({ payload }); app.chooseNode('Concept_标题 #&');
  const link = titleOf(ids).children.find(child => child.tagName === 'A');
  assert.ok(link); assert.equal(link.href, 'file:///C:/MEMORY/wiki/Concept_%E6%A0%87%E9%A2%98%20%23%26.md');
});
test('Claim headings and invalid page paths do not invent a direct Wiki link', async () => {
  const { app, ids } = appFixture(); await ids.get('view-claim').emit('click'); app.chooseNode('claim_1');
  assert.ok(!titleOf(ids).children.some(child => child.tagName === 'A'));
  const payload = fixture(); payload.pageGraph.nodes[0].id = '../outside';
  const invalid = appFixture({ payload }); invalid.app.chooseNode('../outside');
  assert.ok(!titleOf(invalid.ids).children.some(child => child.tagName === 'A'));
});
test('panel pagination is bounded and preserves every item across pages', () => {
  const items = Array.from({ length: 23 }, (_, i) => i), seen = [];
  for (let page = 0; page < 3; page++) { const result = ui.slicePage(items, page, 8); assert.ok(result.items.length <= 8); seen.push(...result.items); }
  assert.deepEqual(seen, items); assert.equal(ui.slicePage(items, 999, 8).page, 2); assert.equal(ui.slicePage([], 0, 8).items.length, 0);
});
test('sparse labels reserve room but never hide hovered or selected nodes', () => {
  const nodes = Array.from({ length: 20 }, (_, i) => ({ id: String(i) }));
  const opts = { aggregated: true, density: 'key' };
  assert.equal(nodes.filter((node, i) => ui.shouldShowLabel(node, i, opts)).length, 4);
  assert.equal(nodes.filter((node, i) => ui.shouldShowLabel(node, i, { ...opts, hoverId: '19' })).length, 5);
  assert.equal(nodes.filter((node, i) => ui.shouldShowLabel(node, i, { ...opts, density: 'all' })).length, 20);
  assert.equal(nodes.filter((node, i) => ui.shouldShowLabel(node, i, { ...opts, density: 'hover', selectedId: '18', hoverId: '19' })).length, 2);
});
test('left panels switch independently and group paging does not grow the DOM', async () => {
  const payload = fixture(); payload.pageGraph.nodes = Array.from({ length: 25 }, (_, i) => ({ id: `Concept_${i}`, raw_type: 'Concept', domain: `领域${i}` })); payload.pageGraph.edges = [];
  const { ids } = appFixture({ payload });
  assert.equal(ids.get('browse-panel').hidden, false); assert.equal(ids.get('filter-panel').hidden, true);
  assert.equal(ids.get('group-list').children.length, 9);
  await ids.get('more-groups').emit('click'); assert.equal(ids.get('group-list').children.length, 9);
  await ids.get('more-groups').emit('click'); await ids.get('more-groups').emit('click');
  assert.equal(ids.get('group-list').children.length, 2); assert.equal(ids.get('more-groups').disabled, true);
  ids.get('group-search').value = '领域24'; await ids.get('group-search').emit('input'); assert.equal(ids.get('group-list').children.length, 2);
  await ids.get('nav-filters').emit('click'); assert.equal(ids.get('browse-panel').hidden, true); assert.equal(ids.get('filter-panel').hidden, false);
  await ids.get('nav-browse').emit('click'); assert.equal(ids.get('filter-panel').hidden, true);
});
function descendants(node) { return node.children.flatMap(child => child instanceof FakeElement ? [child, ...descendants(child)] : []); }
test('right panel sources are separately paged, searchable and do not accumulate', async () => {
  const payload = fixture(); payload.pageGraph.nodes[0].sources = Array.from({ length: 17 }, (_, i) => `raw/public/source${i}.md`);
  const { app, ids } = appFixture({ payload }); app.chooseNode('Concept_A');
  const all = () => descendants(ids.get('detail-content'));
  const sources = all().find(node => node.id === 'detail-pane-sources'), overview = all().find(node => node.id === 'detail-pane-overview');
  assert.equal(sources.hidden, true); assert.equal(overview.hidden, false);
  await all().find(node => node.id === 'detail-tab-sources').emit('click'); assert.equal(sources.hidden, false); assert.equal(overview.hidden, true);
  const list = all().find(node => node.className === 'evidence-list'); assert.equal(list.children.length, 5);
  await all().find(node => node.getAttribute('aria-label') === '下一页声明来源').emit('click'); assert.equal(list.children.length, 5);
  const input = all().find(node => node.getAttribute('aria-label') === '筛选声明来源'); input.value = 'source16'; await input.emit('input'); assert.equal(list.children.length, 1);
});
test('neighbor sections page independently and preserve focus across page and node changes', async () => {
  const payload = fixture(), root = payload.pageGraph.nodes[0]; root.backbone_degree = 12; root.degree = 12;
  payload.pageGraph.nodes = [root, ...Array.from({ length: 12 }, (_, i) => ({ id: `Product_${i}`, name: `邻居 ${i}`, raw_type: 'Product', domain: 'General', backbone_degree: 1 }))];
  payload.pageGraph.edges = payload.pageGraph.nodes.slice(1).map(node => ({ source: root.id, target: node.id, weight: 1 }));
  const { app, ids } = appFixture({ payload }); app.chooseNode(root.id);
  const all = () => descendants(ids.get('detail-content'));
  await all().find(node => node.id === 'detail-tab-relations').emit('click');
  const list = all().find(node => node.className === 'neighbor-list'), next = all().find(node => node.getAttribute('aria-label') === '下一页关联邻居');
  assert.equal(list.children.length, 5); await next.emit('click'); assert.equal(list.children.length, 5);
  await next.emit('click'); assert.equal(list.children.length, 2); assert.ok(global.document.activeElement.isConnected); assert.equal(global.document.activeElement.disabled, false);
  await list.children[0].children[0].emit('click'); assert.notEqual(app.state.selected, root.id); assert.equal(app.state.detailTab, 'relations'); assert.equal(global.document.activeElement.tagName, 'H2');
});
test('summary preview can expand and collapse without a long combined inspector', async () => {
  const payload = fixture(); payload.pageGraph.nodes[0].summary = '摘要段落'.repeat(500);
  const { app, ids } = appFixture({ payload }); app.chooseNode('Concept_A');
  const all = descendants(ids.get('detail-content')), text = all.find(node => node.className === 'summary-text'), toggle = all.find(node => node.textContent === '展开摘要');
  assert.ok(text.textContent.length <= 181); await toggle.emit('click'); assert.equal(text.textContent.length, 2000);
  await toggle.emit('click'); assert.ok(text.textContent.length <= 181); assert.equal(toggle.textContent, '展开摘要');
});
test('label control changes presentation only and graph background agrees with CSS', async () => {
  const { app, ids } = appFixture(), before = app.getModel().nodes.map(node => node.id);
  ids.get('label-density').value = 'hover'; await ids.get('label-density').emit('change');
  assert.equal(app.state.labelDensity, 'hover'); assert.deepEqual(app.getModel().nodes.map(node => node.id), before);
  assert.equal(ui.GRAPH_COLORS.background, template.match(/--graph-bg:\s*(#[a-f0-9]{6})/)[1]);
});
test('light graph text, nodes and normal blended links retain usable contrast', () => {
  const rgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
  const lum = values => values.map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4).reduce((sum, x, i) => sum + x * [.2126, .7152, .0722][i], 0);
  const ratio = (a, b) => { const [low, high] = [lum(a), lum(b)].toSorted((x, y) => x - y); return (high + .05) / (low + .05); };
  const background = rgb(ui.GRAPH_COLORS.background);
  assert.ok(ratio(rgb(ui.GRAPH_COLORS.text), background) >= 4.5);
  const blended = rgb(ui.GRAPH_COLORS.edge).map((value, i) => value * .8 + background[i] * .2);
  assert.ok(ratio(blended, background) >= 3);
  const palette = JSON.parse(template.match(/const COLORS = (\[[^\n]+\])/)[1].replaceAll("'", '"'));
  for (const color of palette) assert.ok(ratio(rgb(color), background) >= 3, color);
});
test('actual draw pass uses sparse dark labels and on-demand mode removes idle labels', async () => {
  const payload = fixture(); payload.pageGraph.nodes = Array.from({ length: 25 }, (_, i) => ({ id: `Concept_${i}`, name: `节点${i}`, raw_type: 'Concept', domain: `领域${i}` })); payload.pageGraph.edges = [];
  const { ids, frames } = appFixture({ payload }), context = ids.get('graph-canvas').getContext();
  while (frames.length) frames.shift()();
  assert.ok(context.labels.length > 0 && context.labels.length <= 4); assert.ok(context.labels.every(label => label.color === ui.GRAPH_COLORS.text));
  ids.get('label-density').value = 'hover'; await ids.get('label-density').emit('change'); while (frames.length) frames.shift()();
  assert.equal(context.labels.length, 0);
});
test('screenshot edits rename the UI and remove the red-circled description', () => {
  assert.match(template, /<title>KnowWeave · 知识经纬<\/title>/);
  assert.match(template, /<h1>KnowWeave · 知识经纬<\/h1>/);
  assert.ok(!template.includes('知识地图')); assert.ok(!template.includes('scope-description'));
});
test('legend lives inside the map overlay and is hidden outside the map view', async () => {
  const stage = template.match(/<section id="visualization"[\s\S]*?<\/section>/)[0];
  assert.match(stage, /id="graph-legend"/);
  const { app, ids } = appFixture(); assert.equal(ids.get('map-overlays').hidden, false);
  await ids.get('tab-list').emit('click'); assert.equal(ids.get('map-overlays').hidden, true);
  await ids.get('tab-map').emit('click'); assert.equal(ids.get('map-overlays').hidden, false);
});
test('actual drawing truncation is still disclosed without restoring the removed paragraph', () => {
  const payload = fixture(); payload.pageGraph.nodes = Array.from({ length: 205 }, (_, i) => ({ id: `Concept_${i}`, name: `条目 ${i}`, raw_type: 'Concept', domain: 'General' })); payload.pageGraph.edges = [];
  const { app, ids } = appFixture({ payload }); app.enterGroup('General');
  assert.match(ids.get('feedback').textContent, /绘图 200\/205/);
});
test('actual UI functions expose stale status and graph-specific counts', async () => {
  const { app, ids } = appFixture();
  assert.equal(ids.get('stale-warning').hidden, false);
  assert.match(ids.get('snapshot-total').textContent, /17/);
  await ids.get('view-claim').emit('click');
  assert.equal(app.state.key, 'claim'); assert.match(ids.get('snapshot-total').textContent, /9/);
  assert.doesNotMatch(ids.get('snapshot-total').textContent, /17/);
});
test('actual UI empty selection, reset, grouping, hops and bounded list transitions', async () => {
  const { app, ids } = appFixture();
  assert.equal(app.getModel().aggregated, true);
  await ids.get('toggle-types').emit('click');
  assert.equal(app.getCandidates().length, 0); assert.equal(ids.get('empty-state').hidden, false);
  await ids.get('empty-reset').emit('click'); assert.equal(app.getCandidates().length, 4);
  app.enterGroup('Medical_IT'); assert.equal(app.getCandidates().length, 2);
  app.chooseNode('Concept_A'); assert.equal(app.getCandidates().length, 2);
  app.state.hops = 2; app.refresh(); assert.equal(app.getCandidates().length, 3);
  assert.ok(app.getModel().nodes[0].id === 'Concept_A');
});
test('claim details use declared sources rather than invented claim Markdown', async () => {
  const { app, ids } = appFixture();
  await ids.get('view-claim').emit('click'); app.chooseNode('claim_1');
  const flatten = element => [element.textContent, ...element.children.map(flatten)].join(' ');
  const detail = flatten(ids.get('detail-content'));
  assert.match(detail, /80%/); assert.match(detail, /Source_B/); assert.doesNotMatch(detail, /查看本页 Markdown/); assert.doesNotMatch(detail, /100%/);
});
test('no optional script is requested by default; network requires explicit confirmation', async () => {
  const { ids } = appFixture();
  assert.equal(global.document.head.children.length, 0);
  await ids.get('toggle-3d').emit('click'); assert.equal(ids.get('network-dialog').open, true);
  assert.equal(global.document.head.children.length, 0);
  await ids.get('cancel-network').emit('click'); assert.equal(ids.get('network-dialog').open, false);
});
test('3D uses the same projected model, shared filtering and real pause/resume', async () => {
  const { app, ids } = appFixture();
  let shown, paused = false;
  const methods = new Proxy({}, { get(target, method) {
    if (method === 'graphData') return data => { if (data) { shown = data; return methods; } return shown; };
    if (method === 'pauseAnimation') return () => { paused = true; return methods; };
    if (method === 'resumeAnimation') return () => { paused = false; return methods; };
    return () => methods;
  } });
  global.ForceGraph3D = () => () => methods;
  try {
    await ids.get('toggle-3d').emit('click'); await ids.get('confirm-network').emit('click'); assert.equal(app.state.dimension, '3d'); assert.equal(shown.nodes.length, app.getModel().nodes.length);
    await ids.get('pause-3d').emit('click'); assert.equal(paused, true);
    app.state.types.clear(); app.refresh(); assert.equal(shown.nodes.length, 0); assert.equal(shown.links.length, 0);
    await ids.get('toggle-3d').emit('click'); assert.equal(app.state.dimension, '2d'); assert.equal(paused, true);
  } finally { delete global.ForceGraph3D; }
});
test('missing Canvas falls back to the equivalent list instead of a broken map', async () => {
  const { app, ids } = appFixture({ canvas: false });
  assert.equal(app.state.tab, 'list'); assert.match(ids.get('feedback').textContent, /Canvas 不可用/);
  await ids.get('tab-map').emit('click'); assert.equal(app.state.tab, 'list');
});
test('missing metadata stays unknown and malformed graph carriers show a warning', () => {
  const payload = fixture(); payload.meta = {}; payload.pageGraph = { nodes: 'bad', edges: [] };
  const { ids } = appFixture({ payload });
  assert.equal(ids.get('data-warning').hidden, false); assert.equal(ids.get('consistency-warning').hidden, false);
  assert.equal(ids.get('generated-at').textContent, '生成时间未知');
});
test('Enter flushes pending search rather than selecting a stale suggestion', async () => {
  const { app, ids } = appFixture(); const input = ids.get('search-input');
  input.value = 'Alpha'; await input.emit('input'); await new Promise(resolve => setTimeout(resolve, 150));
  input.value = 'Product_C'; await input.emit('input'); await input.emit('keydown', { key: 'Enter' });
  assert.equal(app.state.selected, 'Product_C'); assert.equal(input.getAttribute('aria-expanded'), 'false');
});
test('optional engine download failure retains 2D and exposes the real error', async () => {
  const { app, ids } = appFixture();
  global.document.head.append = script => { assert.equal(script.integrity, ui.ENGINE_INTEGRITY); queueMicrotask(() => script.onerror()); };
  await ids.get('toggle-3d').emit('click'); await ids.get('confirm-network').emit('click');
  assert.equal(app.state.dimension, '2d'); assert.match(ids.get('feedback').textContent, /下载或完整性校验失败/);
  assert.equal(ids.get('confirm-network').disabled, false);
});
test('WebGL construction failure is not disguised as an empty graph', async () => {
  const { app, ids } = appFixture(); global.ForceGraph3D = () => () => { throw new Error('WebGL unavailable'); };
  try { await ids.get('toggle-3d').emit('click'); await ids.get('confirm-network').emit('click'); assert.equal(app.state.dimension, '2d'); assert.match(ids.get('feedback').textContent, /WebGL unavailable/); assert.equal(app.getCandidates().length, 4); }
  finally { delete global.ForceGraph3D; }
});
for (const cancel of ['button', 'escape', 'close']) test(`KW-GRAPH-001: ${cancel} invalidates an in-flight 3D activation`, async () => {
  const { app, ids } = appFixture(); let loader, constructed = 0;
  global.document.head.append = script => { loader = script; };
  const chain = new Proxy({}, { get: () => () => chain });
  await ids.get('toggle-3d').emit('click');
  const pending = ids.get('confirm-network').emit('click');
  try {
    if (cancel === 'button') await ids.get('cancel-network').emit('click');
    else if (cancel === 'escape') { await ids.get('network-dialog').emit('cancel'); ids.get('network-dialog').close(); }
    else ids.get('network-dialog').close();
    await Promise.resolve();
    global.ForceGraph3D = () => { constructed++; return () => chain; };
    loader.onload(); await pending;
    assert.equal(app.state.dimension, '2d'); assert.equal(constructed, 0);
    assert.doesNotMatch(ids.get('feedback').textContent, /已进入可选三维/);
  } finally { delete global.ForceGraph3D; }
});
for (const native of [false, true]) test(`KW-GRAPH-001: closed dialog blocks activation before its delayed close event (${native ? 'prototype' : 'instance'})`, async () => {
  const { app, ids } = appFixture(); let loader, constructed = 0;
  global.document.head.append = script => { loader = script; };
  const chain = new Proxy({}, { get: () => () => chain });
  await ids.get('toggle-3d').emit('click'); const pending = ids.get('confirm-network').emit('click');
  try {
    if (native) FakeElement.prototype.close.call(ids.get('network-dialog')); else ids.get('network-dialog').close();
    global.ForceGraph3D = () => { constructed++; return () => chain; }; loader.onload(); await pending;
    assert.equal(app.state.dimension, '2d'); assert.equal(constructed, 0); assert.equal(ids.get('confirm-network').disabled, false);
  } finally { delete global.ForceGraph3D; }
});
test('KW-GRAPH-001: closing and reopening without reconfirming does not revive old approval', async () => {
  const { app, ids } = appFixture(); let loader, constructed = 0; global.document.head.append = script => { loader = script; };
  const chain = new Proxy({}, { get: () => () => chain });
  await ids.get('toggle-3d').emit('click'); const pending = ids.get('confirm-network').emit('click');
  try {
    ids.get('network-dialog').close(); ids.get('network-dialog').showModal();
    global.ForceGraph3D = () => { constructed++; return () => chain; }; loader.onload(); await pending;
    assert.equal(app.state.dimension, '2d'); assert.equal(constructed, 0);
  } finally { delete global.ForceGraph3D; }
});
test('KW-GRAPH-001: a new explicit activation can reuse a pending loader without reviving a cancelled request', async () => {
  const { app, ids } = appFixture(); let loader, constructions = 0;
  global.document.head.append = script => { loader = script; };
  const chain = new Proxy({}, { get: () => () => chain });
  await ids.get('toggle-3d').emit('click'); const oldRequest = ids.get('confirm-network').emit('click');
  await ids.get('cancel-network').emit('click'); await ids.get('toggle-3d').emit('click');
  const newRequest = ids.get('confirm-network').emit('click');
  try {
    global.ForceGraph3D = () => { constructions++; return () => chain; }; loader.onload(); await Promise.all([oldRequest, newRequest]);
    assert.equal(app.state.dimension, '3d'); assert.equal(constructions, 1); assert.equal(ids.get('confirm-network').disabled, false);
  } finally { delete global.ForceGraph3D; }
});
test('KW-GRAPH-001: a cancelled loader failure does not overwrite cancellation feedback', async () => {
  const { app, ids } = appFixture(); let loader; global.document.head.append = script => { loader = script; };
  await ids.get('toggle-3d').emit('click'); const pending = ids.get('confirm-network').emit('click');
  await ids.get('cancel-network').emit('click'); loader.onerror(); await pending;
  assert.equal(app.state.dimension, '2d'); assert.match(ids.get('feedback').textContent, /已取消进入三维/);
});
test('KW-GRAPH-002: paging replaces detached focus with the first visible result', async () => {
  const payload = fixture(); payload.pageGraph.nodes = Array.from({ length: 85 }, (_, i) => ({ id: `Concept_${i}`, name: `条目 ${i}`, raw_type: 'Concept', group: 'General' })); payload.pageGraph.edges = [];
  const { app, ids } = appFixture({ payload }); app.state.tab = 'list'; app.showTab();
  const controls = ids.get('list-results').children.at(-1), next = controls.children[1];
  next.focus(); assert.equal(global.document.activeElement, next); await next.emit('click');
  assert.equal(app.state.page, 1); assert.notEqual(global.document.activeElement, global.document.body);
  assert.ok(global.document.activeElement.isConnected); assert.equal(global.document.activeElement.tagName, 'BUTTON');
  assert.notEqual(global.document.activeElement, next);
  assert.equal(global.document.activeElement, ids.get('list-results').children[1].children[0].children[0]);
  const previous = ids.get('list-results').children.at(-1).children[0]; previous.focus(); await previous.emit('click');
  assert.equal(app.state.page, 0); assert.equal(global.document.activeElement, ids.get('list-results').children[1].children[0].children[0]);
});
test('KW-GRAPH-002: choosing a result transfers focus to its detail heading', async () => {
  const { app, ids } = appFixture(); app.state.tab = 'list'; app.showTab();
  const first = ids.get('list-results').children[1].children[0].children[0];
  first.focus(); await first.emit('click');
  assert.notEqual(global.document.activeElement, global.document.body); assert.ok(global.document.activeElement.isConnected);
  assert.equal(global.document.activeElement.tagName, 'H2'); assert.equal(global.document.activeElement, titleOf(ids));
  assert.match(global.document.activeElement.children[0].getAttribute('aria-label'), /Alpha 概念/);
});
test('KW-GRAPH-003: Escape cancels a pending search popup', async () => {
  const { ids } = appFixture(), input = ids.get('search-input');
  input.value = 'Alpha'; await input.emit('input'); await input.emit('keydown', { key: 'Escape' });
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(ids.get('search-results').hidden, true); assert.equal(input.getAttribute('aria-expanded'), 'false');
  input.value = 'Beta'; await input.emit('input'); await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(ids.get('search-results').hidden, false); assert.equal(input.getAttribute('aria-expanded'), 'true');
});
test('representative 7438-node snapshot has bounded overview/local output and finite layout', () => {
  const raw = { nodes: Array.from({ length: 7438 }, (_, i) => ({ id: `Concept_${i}`, name: `主题 ${i}`, raw_type: 'Concept', domain: `领域 ${i % 8}`, community: `c${i % 1485}`, summary: 'synthetic evidence '.repeat(20), backbone_degree: 2, declared_links: [`Concept_${(i + 1) % 7438}`] })), edges: Array.from({ length: 5000 }, (_, i) => ({ source: `Concept_${i}`, target: `Concept_${(i + 1) % 7438}`, weight: 1 })) };
  const timings = [];
  for (let repeat = 0; repeat < 5; repeat++) {
    const begin = performance.now(), graph = ui.buildGraph(raw, 'page'), eligible = ui.filterNodes(graph, options(graph)), groups = ui.groupsFor(graph, eligible, 'domain'), view = ui.overview(graph, groups), positions = ui.layout(view);
    timings.push(performance.now() - begin);
    assert.equal(view.total, 7438); assert.ok(positions.length <= 25);
    const local = ui.nodeView(graph, graph.nodes, 200); assert.equal(local.nodes.length, 200); assert.equal(local.total, 7438);
  }
  console.log('SYNTHETIC_CORE_BENCHMARK_NOT_BROWSER', JSON.stringify({ nodes: 7438, edges: 5000, repeats: 5, ms: timings, medianMs: timings.toSorted((a, b) => a - b)[2], node: process.version, platform: process.platform }));
});

test('declared ownership survives a full relevance degree budget and is traversable in reverse', () => {
  const raw = { nodes: [{ id: 'Vendor_A', declared_links: [] },
    { id: 'Product_B', declared_links: ['Vendor_A'], declared_relations: [{ target: 'Vendor_A', predicate: 'part-of' }, { target: 'Vendor_A', predicate: 'part-of' }] },
    ...Array.from({ length: 15 }, (_, i) => ({ id: `Source_${i}` }))],
    edges: Array.from({ length: 15 }, (_, i) => ({ source: 'Vendor_A', target: `Source_${i}`, weight: 10 })) };
  const graph = ui.buildGraph(raw, 'page');
  assert.equal(graph.adjacency.get('Vendor_A').size, 16);
  assert.ok(ui.localNodes(graph, graph.nodes, 'Vendor_A', 1).some(node => node.id === 'Product_B'));
  const edge = graph.edges.find(edge => edge.source === 'Product_B');
  assert.equal(edge.weight, null); assert.equal(edge.declarations.length, 1);
  assert.deepEqual(edge.declarations[0], { source: 'Product_B', target: 'Vendor_A', predicate: 'part-of' });
  assert.equal(edge.verified, undefined);
});
test('weighted pairs and declared pairs deduplicate without losing direction or inventing a score', () => {
  const graph = ui.buildGraph({ nodes: [{ id: 'Vendor_A', declared_links: ['Product_B'], declared_relations: [{ target: 'Product_B', predicate: 'has_part' }] },
    { id: 'Product_B', declared_links: ['Vendor_A'], declared_relations: [{ target: 'Vendor_A', predicate: 'part-of' }] }],
    edges: [{ source: 'Product_B', target: 'Vendor_A', weight: 4.5, relation: 'medium' }] }, 'page');
  assert.equal(graph.edges.length, 1); assert.equal(graph.edges[0].weight, 4.5);
  assert.equal(graph.edges[0].declarations.length, 2);
});
test('legacy semantic links recover only missing pairs and never relabel backbone neighbors as declarations', () => {
  const graph = ui.buildGraph({ nodes: [{ id: 'A', semantic_links: ['B', 'C'] }, { id: 'B' }, { id: 'C' }],
    edges: [{ source: 'A', target: 'B', weight: 5 }] }, 'page');
  assert.equal(graph.edges.length, 2);
  assert.equal(graph.edges.find(edge => edge.target === 'B').declarations.length, 0);
  const recovered = graph.edges.find(edge => edge.target === 'C');
  assert.equal(recovered.weight, null); assert.equal(recovered.declarations[0].predicate, null);
});
test('malformed declarations and claim-space semantic links cannot create phantom relationships', () => {
  const raw = { nodes: [{ id: 'A', declared_links: ['A', 'Missing', {}], semantic_links: ['B'],
    declared_relations: [null, { target: 'B', predicate: {} }] }, { id: 'B', declared_links: [] }], edges: [] };
  const graph = ui.buildGraph(raw, 'page'); assert.equal(graph.edges.length, 0); assert.ok(graph.rejected >= 4);
  assert.equal(ui.buildGraph({ nodes: [{ id: 'Claim_A', semantic_links: ['Claim_B'] }, { id: 'Claim_B' }], edges: [] }, 'claim').edges.length, 0);
});
test('relation inspector distinguishes an incoming declaration from relevance weights', () => {
  const payload = fixture(); payload.pageGraph.edges = [];
  payload.pageGraph.nodes[1].declared_links = ['Concept_A'];
  payload.pageGraph.nodes[1].declared_relations = [{ target: 'Concept_A', predicate: 'part-of' }];
  const { app, ids } = appFixture({ payload }); app.chooseNode('Concept_A');
  const strings = descendants(ids.get('detail-content')).map(node => node.textContent);
  assert.ok(strings.some(text => text.includes('页面声明：← part-of')));
  assert.ok(!strings.some(text => text.includes('检索权重')));
});
function inspectorElements(ids) {
  const walk = node => [node, ...node.children.flatMap(child => child instanceof FakeElement ? walk(child) : [])];
  return walk(ids.get('detail-content'));
}
test('record, epistemic and explicit product lifecycle remain separate; seed never means unreleased', () => {
  const payload = fixture();
  payload.pageGraph.nodes.push({ id: 'Product_Seed', name: 'Synthetic product', raw_type: 'Product', status: 'seed', epistemic_status: 'unverified' },
    { id: 'Product_Released', raw_type: 'Product', status: 'active', epistemic_status: 'supported', lifecycle_status: 'released' });
  const { app, ids } = appFixture({ payload });
  app.chooseNode('Product_Seed');
  let text = inspectorElements(ids).map(node => node.textContent).join('\n');
  assert.match(text, /记录状态（status）：seed/);
  assert.match(text, /认知状态（epistemic_status）：unverified/);
  assert.match(text, /产品生命周期（lifecycle_status）：unknown/);
  assert.doesNotMatch(text, /unreleased|未发布/);
  app.chooseNode('Product_Released');
  text = inspectorElements(ids).map(node => node.textContent).join('\n');
  assert.match(text, /记录状态（status）：active/);
  assert.match(text, /认知状态（epistemic_status）：supported/);
  assert.match(text, /产品生命周期（lifecycle_status）：released/);
  app.chooseNode('Concept_A');
  text = inspectorElements(ids).map(node => node.textContent).join('\n');
  assert.match(text, /记录状态（status）：unknown/);
  assert.match(text, /认知状态（epistemic_status）：unknown/);
});
test('catalog details preserve direction, unverified null declaration weight, context and independent numeric relatedness', () => {
  const payload = fixture();
  const relation = { source: 'Source_B', target: 'Concept_A', predicate: 'part-of', layer: 'declared', declared: true, weight: null, verified: false,
    subject_scope: 'document', resolution_status: 'resolved', origin: { page_key: 'Source_B', locator: 'synthetic heading' },
    context: { claim_ids: ['synthetic-claim'], evidence_ids: ['synthetic-evidence'] }, temporal: { valid_from: '2026-01-01', valid_until: null }, provenance_status: 'unverified' };
  payload.pageGraph.relations = [relation, { source: 'Person_D', target: 'Concept_A', layer: 'relatedness', weight: 999 }];
  payload.pageGraph.nodes[1].declared_relations = [{ target: 'Concept_A', predicate: 'part-of' }];
  payload.pageGraph.nodes[1].declared_links = ['Concept_A'];
  payload.pageGraph.edges = [{ source: 'Source_B', target: 'Concept_A', relation: 'medium', weight: 4.5 }];
  const graph = ui.buildGraph(payload.pageGraph, 'page');
  assert.equal(graph.edges.length, 1);
  assert.deepEqual(graph.edges[0].declarations, [relation]);
  const { app, ids } = appFixture({ payload }); app.chooseNode('Concept_A');
  let text = inspectorElements(ids).map(node => node.textContent).join('\n');
  assert.match(text, /← part-of/); assert.match(text, /声明权重：null/); assert.match(text, /核验（verified）：false/);
  assert.match(text, /检索权重 4\.5/); assert.match(text, /主体范围（subject_scope）：document/);
  assert.match(text, /synthetic-claim/); assert.match(text, /synthetic-evidence/); assert.match(text, /valid_from.*2026-01-01/);
  assert.match(text, /溯源状态（provenance_status）：unverified/);
  app.chooseNode('Source_B');
  text = inspectorElements(ids).map(node => node.textContent).join('\n'); assert.match(text, /→ part-of/);
});
test('malicious state and optional relation text remain literal text, never HTML or network actions', () => {
  const attack = '</script><img src="https://invalid.example/x" onerror="attack()"> & <svg onload=attack()>';
  const payload = fixture();
  payload.pageGraph.nodes[0].status = attack; payload.pageGraph.nodes[0].epistemic_status = attack; payload.pageGraph.nodes[0].lifecycle_status = attack;
  payload.pageGraph.relations = [{ source: 'Concept_A', target: 'Source_B', predicate: attack, layer: 'navigation', verified: false, weight: null,
    context: attack, origin: { locator: attack }, provenance_status: attack, temporal: { valid_from: attack } }];
  const { app, ids } = appFixture({ payload }); app.chooseNode('Concept_A');
  const elements = inspectorElements(ids), text = elements.map(node => node.textContent).join('\n');
  assert.ok(text.includes(`记录状态（status）：${attack}`));
  assert.ok(text.includes(`认知状态（epistemic_status）：${attack}`));
  assert.ok(text.includes(`产品生命周期（lifecycle_status）：${attack}`));
  assert.ok(text.includes(`导航声明：→ ${attack}`));
  assert.ok(text.includes(`声明上下文（context）：${attack}`));
  assert.ok(text.includes(`溯源状态（provenance_status）：${attack}`));
  assert.ok(!elements.some(node => ['IMG', 'SCRIPT', 'SVG'].includes(node.tagName)));
  assert.equal(global.document.head.children.length, 0);
});
test('catalog-only invalid endpoints cannot mint nodes and claim-space catalog remains uninterpreted', () => {
  const raw = { nodes: [{ id: 'A' }, { id: 'B' }], edges: [], relations: [
    { source: 'Missing', target: 'B', predicate: 'part-of', layer: 'declared' },
    { source: 'A', target: 'A', predicate: 'part-of', layer: 'declared' },
    { source: 'A', target: 'B', predicate: {}, layer: 'declared' },
  ] };
  const graph = ui.buildGraph(raw, 'page'); assert.equal(graph.edges.length, 0); assert.equal(graph.nodes.length, 2); assert.equal(graph.rejected, 3);
  raw.relations = [{ source: 'A', target: 'B', predicate: 'part-of', layer: 'declared', weight: null, verified: false }];
  assert.equal(ui.buildGraph(raw, 'claim').edges.length, 0);
  raw.relations = ['navigation', 'declared'].map(layer => ({ source: 'A', target: 'B', predicate: 'wikilink', layer, weight: null, verified: false }));
  assert.deepEqual(ui.buildGraph(raw, 'page').edges[0].declarations.map(item => item.layer), ['navigation', 'declared']);
});
test('scoped refresh reports its own time without claiming an all-library fresh snapshot', () => {
  const payload = fixture(); payload.meta.generated_at = '2026-10-21T01:26:30Z';
  payload.meta.read_consistency = 'scoped'; payload.meta.scoped_refresh = { label: '卫宁健康相关页', updated_at: '2026-10-21T04:00:00Z' };
  const { ids } = appFixture({ payload });
  assert.match(ids.get('generated-at').textContent, /卫宁健康相关页更新/);
  assert.match(ids.get('generated-at').textContent, /其余保留原快照/);
  assert.equal(ids.get('generated-at').dateTime, '2026-10-21T01:26:30.000Z');
  assert.equal(ids.get('consistency-warning').hidden, false);
  assert.equal(ids.get('consistency-warning').textContent, '定向范围更新，不是全库同一时点快照');
});
