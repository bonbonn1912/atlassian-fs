/** Same Canvas styles as the main-thread renderer, rasterized off the UI thread. */
'use strict';
self.window = self;
importScripts('graph.js');
const graph = Object.create(self.KnowledgeGraph.prototype);
graph.canvas = new OffscreenCanvas(1, 1);
graph.ctx = graph.canvas.getContext('2d');
self.onmessage = ({ data }) => {
  try {
    if (data.nodes) {
      graph.nodes = data.nodes;
      graph.edges = data.edges.map((e) => ({ ...e, s: graph.nodes[e.s], t: graph.nodes[e.t] }));
      graph._buildGrid();
    }
    for (const key of ['width', 'height', 'dpr', 'k', 'tx', 'ty', 'colors', 'fontFamily', 'selectedUid', 'selectedEdgeId']) graph[key] = data[key];
    graph.highlight = data.highlight ? new Set(data.highlight) : null;
    const width = Math.max(1, Math.round(data.width * data.dpr));
    const height = Math.max(1, Math.round(data.height * data.dpr));
    if (graph.canvas.width !== width || graph.canvas.height !== height) {
      graph.canvas.width = width; graph.canvas.height = height;
    }
    graph._drawScene();
    const bitmap = graph.canvas.transferToImageBitmap();
    self.postMessage({ bitmap }, [bitmap]);
  } catch (err) { self.postMessage({ error: String(err) }); }
};
