/* Serve repository root: python3 -m http.server 8766 --bind 127.0.0.1
 * Open /tests/graph-benchmark.html?nodes=100000 (optional &fallback=1).
 * Timings exclude network/API latency; use browser CPU throttling separately.
 */
(async () => {
  const params = new URLSearchParams(location.search);
  const count = Math.max(1, Math.min(100000, Number(params.get('nodes')) || 100000));
  const output = document.getElementById('metrics');
  output.style.height = '180px';
  output.style.overflow = 'auto';
  const container = document.getElementById('container');
  container.style.height = '650px';
  const nodes = Array.from({ length: count }, (_, i) => ({
    uid: `doc:${i}`, title: `Document ${i}`, type: i % 2 ? 'jira' : 'confluence',
    state: i % 100 === 0 ? 'missing' : i % 30 === 0 ? 'discovered' : 'indexed', degree: 4, in_degree: i % 10,
  }));
  const edges = [];
  for (let i = 1; i < count; i++) {
    edges.push({ id: 2 * i, source: nodes[i].uid, target: nodes[Math.floor((i - 1) / 4)].uid, kind: 'parent_of' });
    edges.push({ id: 2 * i + 1, source: nodes[i].uid, target: nodes[i - 1].uid, kind: 'links_to' });
  }
  const metrics = window.graphMetrics = { nodes: count, edges: edges.length, frames: [], longTasks: [] };
  if (typeof PerformanceObserver !== 'undefined' && PerformanceObserver.supportedEntryTypes.includes('longtask')) {
    new PerformanceObserver((list) => metrics.longTasks.push(...list.getEntries().map((e) => Math.round(e.duration))))
      .observe({ type: 'longtask' });
  }
  const start = performance.now();
  const graph = window.benchmarkGraph = new KnowledgeGraph({
    canvasEl: document.getElementById('canvas'), containerEl: container,
    workerUrl: '../web/graph-layout-worker.js',
    onLayoutProgress: (progress, done) => {
      if (progress > 0 && !metrics.firstPositionsMs) metrics.firstPositionsMs = performance.now() - start;
      if (done) metrics.layoutMs = performance.now() - start;
    },
  });
  if (params.has('fallback')) graph.renderWorkerFailed = true;
  const draw = graph._draw.bind(graph);
  graph._draw = () => {
    const t = performance.now(); draw(); metrics.frames.push(performance.now() - t);
    if (graph.frameCache && !metrics.firstFrameMs) metrics.firstFrameMs = performance.now() - start;
    output.textContent = JSON.stringify({ ...metrics,
      frames: metrics.frames.slice(-5), longTasks: metrics.longTasks.slice(-10) }, null, 2);
  };
  document.getElementById('fit').onclick = () => graph.fitToView();
  document.getElementById('zoom').onclick = () => graph.zoomIn();
  await graph.setData({ nodes, edges });
  metrics.preparationMs = performance.now() - start;
})();
