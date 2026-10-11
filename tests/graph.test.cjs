'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const scope = { self: {} };
vm.createContext(scope);
vm.runInContext(fs.readFileSync(path.join(root, 'web/graph-layout-worker.js'), 'utf8'), scope);
const { runLayout } = scope.self.KnowledgeGraphLayout;

function layout(n, mode = 'tree', center = -1, importanceMode = false) {
  const links = [];
  for (let i = 1; i < n && mode !== 'isolates'; i++) links.push(i, mode === 'star' ? 0 : Math.floor((i - 1) / 4));
  const radii = Float32Array.from({ length: n }, (_, i) => 7 + i % 29);
  const importance = Float32Array.from({ length: n }, (_, i) => i % 100);
  const messages = [];
  const start = performance.now();
  runLayout({ id: 42, count: n, radii, links: Uint32Array.from(links), importance, center, importanceMode }, (m) => messages.push(m));
  return { radii, messages, positions: messages.at(-1).positions, ms: performance.now() - start };
}

function assertNoOverlaps(positions, radii) {
  const cells = new Map(), cell = 90;
  for (let i = 0; i < radii.length; i++) {
    const x = positions[2 * i], y = positions[2 * i + 1];
    assert.ok(Number.isFinite(x) && Number.isFinite(y));
    const gx = Math.floor(x / cell), gy = Math.floor(y / cell);
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      for (const j of cells.get(`${gx + dx},${gy + dy}`) || []) {
        assert.ok(Math.hypot(x - positions[2 * j], y - positions[2 * j + 1]) >= radii[i] + radii[j], `overlap: ${i}, ${j}`);
      }
    }
    const key = `${gx},${gy}`;
    if (!cells.has(key)) cells.set(key, []);
    cells.get(key).push(i);
  }
}

test('100k nodes retain finite, non-overlapping positions', () => {
  const result = layout(100000);
  assert.equal(result.positions.length, 200000);
  assert.equal(result.messages.at(-1).type, 'done');
  assert.equal(result.messages.at(-1).progress, 1);
  assert.ok(result.messages.length <= 3, 'bounded full-position transfers');
  assertNoOverlaps(result.positions, result.radii);
  console.log(`100k layout: ${Math.round(result.ms)} ms (no timing assertion)`);
});

test('large layouts are deterministic with stars, isolates, focus and importance', () => {
  for (const mode of ['star', 'isolates']) {
    const a = layout(6001, mode, 321, true), b = layout(6001, mode, 321, true);
    assert.deepEqual(a.positions, b.positions);
    assert.equal(a.positions[642], 0); assert.equal(a.positions[643], 0);
    assertNoOverlaps(a.positions, a.radii);
  }
});

test('empty and single-node layout finish', () => {
  assert.equal(layout(0).positions.length, 0);
  assert.equal(layout(1).positions.length, 2);
});

function renderer() {
  const frames = [], workers = [];
  const ctx = new Proxy({ measureText: () => ({ width: 50 }) }, { get: (o, k) => k in o ? o[k] : () => {} });
  const element = () => ({
    style: {}, classList: { add() {}, remove() {}, toggle() {} }, appendChild() {}, addEventListener() {},
    getBoundingClientRect: () => ({ width: 1200, height: 800 }), getContext: () => ctx,
  });
  class Worker {
    constructor() { workers.push(this); }
    terminate() { this.terminated = true; }
    postMessage(message) { this.message = message; }
  }
  const context = { window: { devicePixelRatio: 1, addEventListener() {} }, document: { documentElement: {}, createElement: element },
    getComputedStyle: () => ({ getPropertyValue: () => '' }), Worker, requestAnimationFrame: (f) => frames.push(f),
    setTimeout, clearTimeout, performance, console };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'web/graph.js'), 'utf8'), context);
  const graph = new context.window.KnowledgeGraph({ canvasEl: element(), containerEl: element() });
  return { graph, workers, frames };
}

const fixture = (n) => ({ nodes: Array.from({ length: n }, (_, i) => ({ uid: `n${i}`, title: `Node ${i}`, type: 'jira', state: 'indexed' })),
  edges: [{ id: 1, source: 'n0', target: 'n1', kind: 'links_to' }] });

test('superseding data cancels sliced preparation and old worker messages', async () => {
  const { graph, workers } = renderer();
  const pending = graph.setData(fixture(100000));
  await graph.setData(fixture(2));
  await pending;
  assert.equal(graph.nodes.length, 2);
  const previous = workers.at(-1), message = previous.message;
  await graph.setData({ nodes: [], edges: [] });
  assert.ok(previous.terminated);
  previous.onmessage({ data: { id: message.id, type: 'done', positions: new Float32Array([1, 2, 3, 4]), progress: 1 } });
  assert.equal(graph.nodes.length, 0);
  assert.ok(graph.layoutDone);
  graph.cancelPending();
});

test('spatial queries retain crossing edges and agree with brute-force hit testing', async () => {
  const { graph } = renderer();
  await graph.setData(fixture(10000)); graph.cancelPending();
  graph.nodes.forEach((n, i) => { n.x = (i % 100) * 60; n.y = Math.floor(i / 100) * 60; });
  graph.nodes[0].x = -1000; graph.nodes[0].y = 15;
  graph.nodes[1].x = 1000; graph.nodes[1].y = 15;
  graph._buildGrid(); graph.layoutDone = true;
  assert.equal(graph.edgeIndex.query(-10, 0, 10, 30).length, 1);
  graph.k = 1; graph.tx = graph.ty = 0;
  assert.equal(graph.edgeAt(0, 15)._id, '1');
  for (let i = 200; i < 10000; i += 379) {
    const node = graph.nodes[i];
    assert.equal(graph.nodeAt(node.x, node.y), node);
  }
  const index = graph.nodeIndex;
  for (const rect of [[-1, -1, 100, 100], [2800, 2000, 3400, 3000], [-10000, -10000, 10000, 10000]]) {
    const actual = index.query(...rect).map((n) => n.uid).sort();
    const expected = graph.nodes.filter((n) => n.x + n.r >= rect[0] && n.y + n.r >= rect[1] && n.x - n.r <= rect[2] && n.y - n.r <= rect[3]).map((n) => n.uid).sort();
    assert.deepEqual(Array.from(actual), Array.from(expected));
  }
  graph.fitToView();
  assert.ok(graph.nodes.every((n) => n.x * graph.k + graph.tx >= 0 && n.x * graph.k + graph.tx <= graph.width));
});

test('100k last-resort grid avoids argument overflow and selection retains neighbours', async () => {
  const { graph } = renderer();
  await graph.setData(fixture(100000)); graph.cancelPending(); graph._gridLayout();
  assert.ok(graph.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y)));
  graph.selectNode('n0');
  assert.ok(graph.highlight.has('n1'));
  graph.selectEdge('1'); assert.equal(graph.selectedEdgeId, '1');
  graph.clearSelection(); assert.equal(graph.highlight, null);
});

test('worker failure completes the fallback and stale errors cannot replace new data', async () => {
  const { graph, workers } = renderer();
  await graph.setData(fixture(2));
  const failed = workers.at(-1);
  failed.onerror({ preventDefault() {} });
  assert.ok(failed.terminated);
  assert.ok(graph.layoutDone);
  assert.notEqual(graph.nodes[0].x, graph.nodes[1].x);
  await graph.setData(fixture(1));
  failed.onerror({ preventDefault() {} });
  assert.equal(graph.nodes.length, 1);
  graph.cancelPending();
});
