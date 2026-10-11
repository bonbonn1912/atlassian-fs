/* Serve repository root; open /tests/graph-flight-benchmark.html?nodes=100000. */
(async () => {
  const count = Math.min(100000, Math.max(1, Number(new URLSearchParams(location.search).get('nodes')) || 100000));
  const container = document.getElementById('container');
  container.style.height = '650px';
  const output = document.getElementById('metrics');
  output.style.height = '150px'; output.style.overflow = 'auto';
  const now = Date.UTC(2026, 9, 11), day = 86400000;
  const nodes = Array.from({ length: count }, (_, i) => ({
    uid: `doc:${i}`, title: `Document ${i}`, type: i % 3 ? 'jira' : 'confluence',
    state: i % 31 ? 'indexed' : 'discovered', degree: 3, in_degree: i % 50,
    project: i % 3 ? `PROJ-${i % 10}` : `SPACE-${Math.floor(i / 300) % 12}`,
    source_updated_at: i % 97 ? new Date(now - (i % 5000) * day).toISOString() : null,
    source_created_at: i % 97 ? new Date(now - (5000 + i % 2000) * day).toISOString() : null,
  }));
  const edges = [];
  for (let i = 1; i < count; i++) edges.push({ id: i, source: nodes[i].uid, target: nodes[i - i % 3].uid, kind: 'mentions_jira' });
  const metrics = window.flightMetrics = { nodes: count, edges: edges.length, longTasks: [] };
  if (typeof PerformanceObserver !== 'undefined' && PerformanceObserver.supportedEntryTypes.includes('longtask')) {
    new PerformanceObserver((list) => metrics.longTasks.push(...list.getEntries().map((e) => Math.round(e.duration))))
      .observe({ type: 'longtask' });
  }
  const start = performance.now();
  const graph = window.flightBenchmark = new KnowledgeGraphFlight({
    canvasEl: document.getElementById('canvas'), containerEl: container,
    onNodeSelect: (node) => { metrics.selected = node.uid; },
    onStatus: (status) => { metrics.status = status; output.textContent = JSON.stringify(metrics, null, 2); },
  });
  graph.setActive(true);
  await graph.setData({ nodes, edges });
  metrics.preparedMs = performance.now() - start;
  metrics.supported = graph.supported;
  output.textContent = JSON.stringify(metrics, null, 2);
  document.getElementById('reset').onclick = () => graph.fitToView();
  document.getElementById('time').onchange = (event) => graph.setJiraTimeField(event.target.value);
})();
