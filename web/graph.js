/**
 * Knowledge-Graph-Renderer auf Canvas.
 *
 * - Layout im Worker: Barnes-Hut für kleine, mehrstufiges Layout für große Graphen.
 * - Rendering per Canvas mit Viewport-Culling und gebündelten Pfaden pro Stil,
 *   sowie OffscreenCanvas-Worker und Bild-Cache für bis zu 100.000 Knoten.
 * - Beschriftungen werden nach Priorität platziert und nie übereinander gezeichnet;
 *   beim Hineinzoomen erscheinen automatisch mehr.
 * Vanilla JS, keine externen Abhängigkeiten, kein innerHTML.
 */
(function () {
  'use strict';

  const MIN_ZOOM = 0.0001;
  const MAX_ZOOM = 8;
  const LABEL_FONT_PX = 12;
  const MAX_LABELS = 700;
  const MIN_IMPORTANCE_RADIUS = 7;
  const MAX_IMPORTANCE_RADIUS = 35;
  const ERROR_STATES = new Set(['missing', 'forbidden', 'error']);

  const EDGE_STYLES = {
    links_to: { color: 'muted', width: 1, dash: [] },
    parent_of: { color: 'main', width: 2, dash: [] },
    mentions_jira: { color: 'jira', width: 1.2, dash: [5, 3] },
    jira_relates_to: { color: 'jira', width: 1.2, dash: [1.5, 3] },
    jira_parent_of: { color: 'jira', width: 2.2, dash: [] },
    links_to_confluence: { color: 'confluence', width: 1.2, dash: [6, 2, 2, 2] },
  };

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function truncate(text, max) {
    if (!text) return '';
    return text.length > max ? text.slice(0, max - 1) + '…' : text;
  }

  // Bounding-volume hierarchy: query nodes AND long crossing edges without
  // scanning the entire graph or duplicating edges across thousands of grid cells.
  class SpatialIndex {
    constructor(items, bounds, deferred = false) {
      this.items = items;
      this.order = new Uint32Array(items.length);
      this.boxes = new Float64Array(items.length * 4);
      this.steps = this._initialize(bounds);
      if (!deferred) while (!this.steps.next().done) { /* synchronous worker path */ }
    }

    *_initialize(bounds) {
      const items = this.items;
      for (let i = 0; i < items.length; i++) {
        if (i % 4096 === 0) yield;
        this.order[i] = i;
        this.boxes.set(bounds(items[i]), i * 4);
      }
      this.root = yield* this._build(0, items.length);
    }

    *_build(start, end) {
      if (start === end) return null;
      const { order, boxes } = this;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (let i = start; i < end; i++) {
        if ((i - start) % 8192 === 0 && i > start) yield;
        const b = order[i] * 4;
        x0 = Math.min(x0, boxes[b]); y0 = Math.min(y0, boxes[b + 1]);
        x1 = Math.max(x1, boxes[b + 2]); y1 = Math.max(y1, boxes[b + 3]);
      }
      const node = { x0, y0, x1, y1, start, end };
      if (end - start <= 32) return node;
      const axis = x1 - x0 > y1 - y0 ? 0 : 1;
      const middle = axis ? (y0 + y1) / 2 : (x0 + x1) / 2;
      let split = start;
      for (let i = start; i < end; i++) {
        if ((i - start) % 8192 === 0 && i > start) yield;
        const b = order[i] * 4 + axis;
        if ((boxes[b] + boxes[b + 2]) / 2 < middle) {
          const tmp = order[split]; order[split++] = order[i]; order[i] = tmp;
        }
      }
      // Bound recursion even for coincident nodes or highly skewed edge bounds.
      if (split - start < (end - start) / 16 || end - split < (end - start) / 16) split = (start + end) >>> 1;
      if (end - start >= 1024) yield;
      node.left = yield* this._build(start, split);
      node.right = yield* this._build(split, end);
      return node;
    }

    query(x0, y0, x1, y1) {
      const result = [];
      const { items, order, boxes } = this;
      const visit = (node) => {
        if (!node || node.x1 < x0 || node.x0 > x1 || node.y1 < y0 || node.y0 > y1) return;
        if (x0 <= node.x0 && y0 <= node.y0 && x1 >= node.x1 && y1 >= node.y1) {
          for (let i = node.start; i < node.end; i++) result.push(items[order[i]]);
        } else if (node.left) { visit(node.left); visit(node.right); }
        else for (let i = node.start; i < node.end; i++) {
          const index = order[i], b = index * 4;
          if (boxes[b + 2] >= x0 && boxes[b] <= x1 && boxes[b + 3] >= y0 && boxes[b + 1] <= y1) result.push(items[index]);
        }
      };
      visit(this.root);
      return result;
    }
  }

  class KnowledgeGraph {
    /**
     * @param {Object} options
     * @param {HTMLCanvasElement} options.canvasEl
     * @param {HTMLElement} options.containerEl
     * @param {Function} [options.onNodeSelect]   (node) => void
     * @param {Function} [options.onNodeActivate] (node) => void  (Doppelklick / Enter)
     * @param {Function} [options.onEdgeSelect]   (edge) => void
     * @param {Function} [options.onLayoutProgress] (progress 0..1, done:boolean) => void
     * @param {Function} [options.onRenderReady] first completed frame for a layout
     * @param {string} [options.workerUrl]
     */
    constructor(options) {
      this.canvas = options.canvasEl;
      this.container = options.containerEl;
      this.ctx = this.canvas.getContext('2d');
      this.onNodeSelect = options.onNodeSelect || (() => {});
      this.onNodeActivate = options.onNodeActivate || (() => {});
      this.onEdgeSelect = options.onEdgeSelect || (() => {});
      this.onLayoutProgress = options.onLayoutProgress || (() => {});
      this.onRenderReady = options.onRenderReady || (() => {});
      this.workerUrl = options.workerUrl || 'graph-layout-worker.js';

      this.nodes = [];
      this.edges = [];
      this.nodeMap = new Map();
      this.edgeMap = new Map();
      this.centerUid = null;
      this.selectedUid = null;
      this.selectedEdgeId = null;
      this.highlight = null;          // Set of uids highlighted by selection
      this.hoverNode = null;
      this.importanceMode = false;

      this.k = 1; this.tx = 0; this.ty = 0;
      this.width = 0; this.height = 0; this.dpr = 1;
      this.userMovedView = false;
      this.layoutDone = true;
      this.worker = null;
      this.layoutId = 0;
      this.dataId = 0;
      this.sceneVersion = 0;
      this.frameCache = null;
      this.drawPending = false;
      this.grid = null;               // spatial hash for hit testing
      this.keyboardIndex = -1;

      this.tooltip = document.createElement('div');
      this.tooltip.className = 'graph-tooltip';
      this.tooltip.hidden = true;
      this.container.appendChild(this.tooltip);

      this._readColors();
      this._bindEvents();
      this._resize();
      if (window.ResizeObserver) {
        new ResizeObserver(() => this._resize()).observe(this.container);
      } else {
        window.addEventListener('resize', () => this._resize());
      }
      if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
          this._readColors();
          this.requestDraw();
        });
      }
    }

    // ---------------------------------------------------------------- data

    _readColors() {
      this.sceneVersion++;
      this.fontFamily = cssVar('--font-sans', 'sans-serif');
      this.colors = {
        confluence: cssVar('--confluence-blue', '#1868db'),
        jira: cssVar('--jira-orange', '#e2740d'),
        danger: cssVar('--status-danger', '#de350b'),
        muted: cssVar('--text-muted', '#7a869a'),
        main: cssVar('--text-main', '#172b4d'),
        surface: cssVar('--bg-surface', '#ffffff'),
        app: cssVar('--bg-app', '#f4f5f7'),
        focus: cssVar('--border-focus', '#0052cc'),
      };
    }

    static radiusFor(node, isCenter) {
      const base = 7 + Math.min(13, Math.log2((node.degree || 0) + 1) * 2.2);
      return isCenter ? base + 5 : base;
    }

    _computeRadii() {
      if (!this.nodes.length) return;
      if (!this.importanceMode) {
        for (const node of this.nodes) {
          node.r = KnowledgeGraph.radiusFor(node, node.uid === this.centerUid);
        }
        return;
      }
      let minImp = Infinity, maxImp = -Infinity;
      for (const node of this.nodes) {
        const imp = node.in_degree ?? 0;
        if (imp < minImp) minImp = imp;
        if (imp > maxImp) maxImp = imp;
      }
      if (!isFinite(minImp)) { minImp = 0; maxImp = 0; }
      const span = maxImp - minImp;
      for (const node of this.nodes) {
        const imp = node.in_degree ?? 0;
        const ratio = span > 0 ? (imp - minImp) / span : 0.5;
        node.r = Math.round((MIN_IMPORTANCE_RADIUS + ratio * (MAX_IMPORTANCE_RADIUS - MIN_IMPORTANCE_RADIUS)) * 10) / 10;
      }
    }

    setImportanceMode(enabled) {
      this.importanceMode = Boolean(enabled);
      this._computeRadii();
      this._startLayout();
    }

    async setData(data, centerUid) {
      const dataId = ++this.dataId;
      this._stopLayout();
      // Prepare in small batches, keeping the previous graph valid until commit.
      const nodes = [], edges = [], nodeMap = new Map(), edgeMap = new Map();
      let deadline = performance.now() + 8;
      const breathe = async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
        deadline = performance.now() + 8;
      };
      for (const n of data.nodes || []) {
        const node = { ...n, idx: nodes.length, x: 0, y: 0, r: 0, label: truncate(n.title || n.uid, 34) };
        nodes.push(node);
        nodeMap.set(node.uid, node);
        if (nodes.length % 1024 === 0 && performance.now() >= deadline) {
          await breathe();
          if (dataId !== this.dataId) return;
        }
      }
      for (const e of data.edges || []) {
        const source = nodeMap.get(e.source), target = nodeMap.get(e.target);
        if (!source || !target) continue;
        const edge = { ...e, s: source, t: target, _id: String(e.id ?? `${e.source}->${e.target}:${e.kind}`) };
        edges.push(edge);
        edgeMap.set(edge._id, edge);
        if (edges.length % 1024 === 0 && performance.now() >= deadline) {
          await breathe();
          if (dataId !== this.dataId) return;
        }
      }
      if (dataId !== this.dataId) return;
      this.centerUid = centerUid || data.center || null;
      this.selectedUid = null;
      this.selectedEdgeId = null;
      this.highlight = null;
      this.hoverNode = null;
      this.tooltip.hidden = true;
      this.keyboardIndex = -1;
      this.userMovedView = false;

      this.nodes = nodes; this.edges = edges;
      this.nodeMap = nodeMap; this.edgeMap = edgeMap;
      this._computeRadii();
      // Neighbours are needed only for the selected node; avoid 100k Sets.
      this.keyboardOrder = [...this.nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0) || (a.uid < b.uid ? -1 : 1));
      this._startLayout();
    }

    cancelPending() {
      ++this.dataId;
      this._stopLayout();
    }

    _stopLayout() {
      ++this.layoutId;
      clearTimeout(this.layoutWatchdog);
      if (this.worker) this.worker.terminate();
      this.worker = null;
      if (this.renderWorker) this.renderWorker.terminate();
      this.renderWorker = null;
      this.renderBusy = false;
    }

    _startLayout() {
      this._stopLayout();
      this.grid = this.nodeIndex = this.edgeIndex = null;
      this.sceneVersion++;
      this.positionsReady = false;
      this._releaseFrame();
      const n = this.nodes.length;
      const center = this.centerUid && this.nodeMap.has(this.centerUid) ? this.nodeMap.get(this.centerUid).idx : -1;
      if (n === 0) {
        this.layoutDone = true;
        this.onLayoutProgress(1, true);
        this.requestDraw();
        return;
      }
      const radii = new Float32Array(n);
      const importance = new Float32Array(n);
      this.nodes.forEach((node) => {
        radii[node.idx] = node.r;
        importance[node.idx] = node.in_degree ?? 0;
      });
      const links = new Uint32Array(this.edges.length * 2);
      this.edges.forEach((e, k) => { links[2 * k] = e.s.idx; links[2 * k + 1] = e.t.idx; });

      const id = ++this.layoutId;
      this.layoutDone = false;
      this.onLayoutProgress(0, false);
      const msg = { id, count: n, radii, links, center, importanceMode: this.importanceMode, importance };
      const handle = (m) => {
        if (m.id !== id || id !== this.layoutId) return;
        if (m.type === 'ready') return;
        if (m.type === 'error') {
          console.error('Layout-Fehler:', m.message);
          this._gridLayout();
          return;
        }
        this._applyPositions(m.positions, m.type === 'done', m.progress);
      };

      let worker = null;
      try {
        worker = new Worker(this.workerUrl);
      } catch (err) {
        worker = null;
      }
      if (!worker) {
        this._mainThreadLayout(msg, handle);
        return;
      }
      this.worker = worker;
      let heardBack = false;
      // Some embedded browsers block workers silently: fall back after 2 s of silence.
      const fallback = () => {
        if (id !== this.layoutId) return;
        worker.terminate();
        this.worker = null;
        // Transferred arrays are detached; recreate only if fallback is needed.
        const radii = new Float32Array(n), importance = new Float32Array(n);
        const links = new Uint32Array(this.edges.length * 2);
        for (const nd of this.nodes) { radii[nd.idx] = nd.r; importance[nd.idx] = nd.in_degree ?? 0; }
        this.edges.forEach((e, i) => { links[2 * i] = e.s.idx; links[2 * i + 1] = e.t.idx; });
        this._mainThreadLayout({ ...msg, radii, importance, links }, handle);
      };
      this.layoutWatchdog = setTimeout(() => { if (!heardBack) fallback(); }, 2000);
      worker.onmessage = (ev) => {
        if (id !== this.layoutId) return;
        heardBack = true;
        clearTimeout(this.layoutWatchdog);
        if (ev.data.type === 'done' || ev.data.type === 'error') {
          worker.terminate();
          if (this.worker === worker) this.worker = null;
        }
        handle(ev.data);
      };
      worker.onerror = (ev) => {
        ev.preventDefault();
        clearTimeout(this.layoutWatchdog);
        fallback();
      };
      try { worker.postMessage(msg, [radii.buffer, links.buffer, importance.buffer]); }
      catch (err) { clearTimeout(this.layoutWatchdog); fallback(); }
    }

    async _applyPositions(p, done, progress) {
      if (this.nodes.length > 5000 && !done) { this.onLayoutProgress(progress, false); return; }
      const id = this.layoutId;
      this.positionsReady = true;
      this.sceneVersion++;
      for (const node of this.nodes) {
        if (node.pinned) continue;
        node.x = p[2 * node.idx];
        node.y = p[2 * node.idx + 1];
      }
      if (this.nodes.length > 5000) {
        await this._buildGridAsync();
        if (id !== this.layoutId) return;
      } else this._buildGrid();
      this.layoutDone = done;
      if (!this.userMovedView) this.fitToView(false);
      this.onLayoutProgress(progress, done);
      if (done && this.centerUid && this.nodeMap.has(this.centerUid) && !this.selectedUid) {
        this.selectNode(this.centerUid);
      }
      this.requestDraw();
    }

    /** Same algorithm as the worker, executed in ~12 ms slices so the UI stays responsive. */
    _mainThreadLayout(msg, handle) {
      const api = window.KnowledgeGraphLayout;
      if (!api) { this._gridLayout(); return; }
      const id = msg.id;
      const steps = api.layoutSteps(msg, handle);
      const slice = () => {
        if (id !== this.layoutId) return;   // superseded by a newer setData()
        const until = performance.now() + 12;
        while (performance.now() < until) {
          if (steps.next().done) return;
        }
        setTimeout(slice, 0);
      };
      slice();
    }

    /** Last-resort grid layout: non-overlapping, but without forces. */
    _gridLayout() {
      const n = this.nodes.length;
      const cols = Math.ceil(Math.sqrt(n));
      const slot = 2 * this.nodes.reduce((r, nd) => Math.max(r, nd.r), 0) + 12;
      this.keyboardOrder.forEach((node, i) => {
        node.x = (i % cols) * slot;
        node.y = Math.floor(i / cols) * slot;
      });
      this.positionsReady = true;
      this.sceneVersion++;
      this.layoutDone = true;
      this._buildGrid();
      this.fitToView(false);
      this.onLayoutProgress(1, true);
      this.requestDraw();
    }

    // ---------------------------------------------------------------- view

    _resize() {
      const rect = this.container.getBoundingClientRect();
      const w = Math.max(0, Math.floor(rect.width));
      const h = Math.max(0, Math.floor(rect.height));
      const dpr = window.devicePixelRatio || 1;
      if (w === this.width && h === this.height && dpr === this.dpr) return;
      const firstSize = (this.width === 0 || this.height === 0) && w > 0 && h > 0;
      this.width = w; this.height = h; this.dpr = dpr;
      this.canvas.width = Math.max(1, Math.round(w * dpr));
      this.canvas.height = Math.max(1, Math.round(h * dpr));
      this.canvas.style.width = `${w}px`;
      this.canvas.style.height = `${h}px`;
      if (firstSize && !this.userMovedView) this.fitToView(false);
      this.requestDraw();
    }

    fitToView(markUser = true) {
      if (!this.nodes.length || !this.width || !this.height) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of this.nodes) {
        minX = Math.min(minX, n.x - n.r); maxX = Math.max(maxX, n.x + n.r);
        minY = Math.min(minY, n.y - n.r); maxY = Math.max(maxY, n.y + n.r + LABEL_FONT_PX + 4);
      }
      const pad = 40;
      const k = Math.min((this.width - pad * 2) / Math.max(1, maxX - minX), (this.height - pad * 2) / Math.max(1, maxY - minY));
      this.k = Math.max(MIN_ZOOM, Math.min(2, k));
      this.tx = this.width / 2 - ((minX + maxX) / 2) * this.k;
      this.ty = this.height / 2 - ((minY + maxY) / 2) * this.k;
      if (markUser) this.userMovedView = false;
      this.requestDraw();
    }

    zoomBy(factor, sx = this.width / 2, sy = this.height / 2) {
      const k = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, this.k * factor));
      const wx = (sx - this.tx) / this.k;
      const wy = (sy - this.ty) / this.k;
      this._deferDetail();
      this.k = k;
      this.tx = sx - wx * k;
      this.ty = sy - wy * k;
      this.userMovedView = true;
      this.requestDraw();
    }

    zoomIn() { this.zoomBy(1.3); }
    zoomOut() { this.zoomBy(1 / 1.3); }
    resetZoom() { this.fitToView(); }

    centerOn(node) {
      this.tx = this.width / 2 - node.x * this.k;
      this.ty = this.height / 2 - node.y * this.k;
      this.requestDraw();
    }

    // ---------------------------------------------------------------- selection

    selectNode(uid) {
      const node = this.nodeMap.get(uid);
      if (!node) return;
      this.selectedUid = uid;
      this.selectedEdgeId = null;
      this.highlight = new Set([uid]);
      for (const e of this.edges) {
        if (e.source === uid) this.highlight.add(e.target);
        if (e.target === uid) this.highlight.add(e.source);
      }
      this.keyboardIndex = this.keyboardOrder.indexOf(node);
      this.requestDraw();
      this.onNodeSelect(node);
    }

    selectEdge(edgeId) {
      const edge = this.edgeMap.get(String(edgeId));
      if (!edge) return;
      this.selectedEdgeId = edge._id;
      this.selectedUid = null;
      this.highlight = new Set([edge.source, edge.target]);
      this.requestDraw();
      this.onEdgeSelect(edge);
    }

    clearSelection() {
      this.selectedUid = null;
      this.selectedEdgeId = null;
      this.highlight = null;
      this.requestDraw();
    }

    // ---------------------------------------------------------------- hit testing

    _buildGrid() {
      const steps = this._buildGridSteps();
      while (!steps.next().done) { /* synchronous fallback and rendering worker */ }
    }

    async _buildGridAsync() {
      const id = this.layoutId, version = this.sceneVersion;
      const steps = this._buildGridSteps();
      for (;;) {
        if (id !== this.layoutId || version !== this.sceneVersion) return;
        const until = performance.now() + 8;
        do { if (steps.next().done) return; } while (performance.now() < until);
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }

    *_buildGridSteps() {
      const maxR = this.nodes.reduce((m, n) => Math.max(m, n.r), 1);
      this.maxRadius = maxR;
      if (this.nodes.length > 5000) {
        const nodes = new SpatialIndex(this.nodes, (n) => [n.x - n.r, n.y - n.r, n.x + n.r, n.y + n.r], true);
        yield* nodes.steps;
        const edges = new SpatialIndex(this.edges, (e) => [Math.min(e.s.x, e.t.x), Math.min(e.s.y, e.t.y),
          Math.max(e.s.x, e.t.x), Math.max(e.s.y, e.t.y)], true);
        yield* edges.steps;
        this.nodeIndex = nodes; this.edgeIndex = edges; this.grid = null;
        return;
      }
      const cell = maxR * 2 + 8;
      const grid = new Map();
      for (const n of this.nodes) {
        const key = `${Math.floor(n.x / cell)},${Math.floor(n.y / cell)}`;
        if (!grid.has(key)) grid.set(key, []);
        grid.get(key).push(n);
      }
      this.grid = { cell, map: grid };
    }

    nodeAt(sx, sy) {
      const wx = (sx - this.tx) / this.k;
      const wy = (sy - this.ty) / this.k;
      const slack = 4 / this.k;
      let best = null, bestD = Infinity;
      const check = (n) => {
        const d = Math.hypot(n.x - wx, n.y - wy);
        if (d <= n.r + slack && d < bestD) { best = n; bestD = d; }
      };
      if (this.nodeIndex) {
        this.nodeIndex.query(wx - slack, wy - slack, wx + slack, wy + slack).forEach(check);
      } else if (this.grid && this.layoutDone && slack / this.grid.cell < 30) {
        const { cell, map } = this.grid;
        const gx = Math.floor(wx / cell), gy = Math.floor(wy / cell);
        const reach = Math.ceil(slack / cell) + 1;
        for (let ox = -reach; ox <= reach; ox++) {
          for (let oy = -reach; oy <= reach; oy++) {
            const bucket = map.get(`${gx + ox},${gy + oy}`);
            if (bucket) bucket.forEach(check);
          }
        }
      } else {
        this.nodes.forEach(check);
      }
      return best;
    }

    edgeAt(sx, sy) {
      const wx = (sx - this.tx) / this.k;
      const wy = (sy - this.ty) / this.k;
      const maxD = 6 / this.k;
      let best = null, bestD = maxD;
      const candidates = this.edgeIndex ? this.edgeIndex.query(wx - maxD, wy - maxD, wx + maxD, wy + maxD) : this.edges;
      for (const e of candidates) {
        const x1 = e.s.x, y1 = e.s.y, x2 = e.t.x, y2 = e.t.y;
        if (wx < Math.min(x1, x2) - maxD || wx > Math.max(x1, x2) + maxD ||
            wy < Math.min(y1, y2) - maxD || wy > Math.max(y1, y2) + maxD) continue;
        const dx = x2 - x1, dy = y2 - y1;
        const len2 = dx * dx + dy * dy || 1;
        const t = Math.max(0, Math.min(1, ((wx - x1) * dx + (wy - y1) * dy) / len2));
        const d = Math.hypot(x1 + t * dx - wx, y1 + t * dy - wy);
        if (d < bestD) { best = e; bestD = d; }
      }
      return best;
    }

    // ---------------------------------------------------------------- events

    _bindEvents() {
      const c = this.canvas;
      let drag = null;   // {mode:'pan'|'node', startX, startY, moved, node}

      const pos = (ev) => {
        const r = c.getBoundingClientRect();
        return [ev.clientX - r.left, ev.clientY - r.top];
      };

      c.addEventListener('pointerdown', (ev) => {
        if (ev.button !== 0) return;
        c.focus({ preventScroll: true });
        const [sx, sy] = pos(ev);
        const node = this.layoutDone ? this.nodeAt(sx, sy) : null;
        drag = { mode: node ? 'node' : 'pan', node, startX: sx, startY: sy, lastX: sx, lastY: sy, moved: false };
        c.setPointerCapture(ev.pointerId);
        c.classList.add(node ? 'is-dragging-node' : 'is-panning');
      });

      c.addEventListener('pointermove', (ev) => {
        const [sx, sy] = pos(ev);
        if (drag) {
          const dx = sx - drag.lastX, dy = sy - drag.lastY;
          if (Math.abs(sx - drag.startX) + Math.abs(sy - drag.startY) > 3) drag.moved = true;
          drag.lastX = sx; drag.lastY = sy;
          if (!drag.moved) return;
          if (drag.mode === 'pan') {
            this._deferDetail();
            this.tx += dx; this.ty += dy;
            this.userMovedView = true;
          } else {
            drag.node.x += dx / this.k;
            drag.node.y += dy / this.k;
            drag.node.pinned = true;
            this.sceneVersion++;
            this.nodeIndex = this.edgeIndex = null;
          }
          this.tooltip.hidden = true;
          this.requestDraw();
          return;
        }
        const node = this.layoutDone ? this.nodeAt(sx, sy) : null;
        if (node !== this.hoverNode) {
          this.hoverNode = node;
          c.classList.toggle('is-hovering-node', !!node);
          this.requestDraw();
        }
        this._updateTooltip(node, sx, sy);
      });

      const endDrag = (ev) => {
        if (!drag) return;
        const d = drag;
        drag = null;
        c.classList.remove('is-dragging-node', 'is-panning');
        if (c.hasPointerCapture(ev.pointerId)) c.releasePointerCapture(ev.pointerId);
        if (d.mode === 'node' && d.moved) { this._buildGridAsync(); return; }
        if (d.moved) return;
        const [sx, sy] = pos(ev);
        const node = d.node || (this.layoutDone ? this.nodeAt(sx, sy) : null);
        if (node) { this.selectNode(node.uid); return; }
        const edge = this.edgeAt(sx, sy);
        if (edge) { this.selectEdge(edge._id); return; }
        this.clearSelection();
      };
      c.addEventListener('pointerup', endDrag);
      c.addEventListener('pointercancel', endDrag);
      c.addEventListener('pointerleave', () => {
        if (!drag) { this.tooltip.hidden = true; this.hoverNode = null; this.requestDraw(); }
      });

      c.addEventListener('dblclick', (ev) => {
        const [sx, sy] = pos(ev);
        const node = this.nodeAt(sx, sy);
        if (node) this.onNodeActivate(node);
      });

      c.addEventListener('wheel', (ev) => {
        ev.preventDefault();
        const [sx, sy] = pos(ev);
        const delta = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
        this.zoomBy(Math.exp(-delta * 0.0015), sx, sy);
      }, { passive: false });

      c.addEventListener('keydown', (ev) => {
        const order = this.keyboardOrder || [];
        if (ev.key === '+' || ev.key === '=') { this.zoomIn(); ev.preventDefault(); }
        else if (ev.key === '-') { this.zoomOut(); ev.preventDefault(); }
        else if (ev.key === '0') { this.fitToView(); ev.preventDefault(); }
        else if (ev.key === 'Escape') { this.clearSelection(); ev.preventDefault(); }
        else if ((ev.key === 'ArrowRight' || ev.key === 'ArrowDown' || ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') && order.length) {
          const step = ev.key === 'ArrowRight' || ev.key === 'ArrowDown' ? 1 : -1;
          this.keyboardIndex = (this.keyboardIndex + step + order.length) % order.length;
          const node = order[this.keyboardIndex];
          this.selectNode(node.uid);
          if (!this._inView(node)) this.centerOn(node);
          ev.preventDefault();
        } else if (ev.key === 'Enter' && this.selectedUid) {
          this.onNodeActivate(this.nodeMap.get(this.selectedUid));
          ev.preventDefault();
        }
      });
    }

    _inView(node) {
      const sx = node.x * this.k + this.tx, sy = node.y * this.k + this.ty;
      return sx > 0 && sy > 0 && sx < this.width && sy < this.height;
    }

    _updateTooltip(node, sx, sy) {
      if (!node) { this.tooltip.hidden = true; return; }
      const tip = this.tooltip;
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      const title = document.createElement('div');
      title.className = 'graph-tooltip-title';
      title.textContent = node.title || node.uid;
      const meta = document.createElement('div');
      meta.className = 'graph-tooltip-meta';
      const typeLabel = node.type === 'jira' ? 'Jira' : 'Confluence';
      const inDeg = node.in_degree !== undefined ? String(node.in_degree) + " eingehende Links" : null;
      const parts = [node.uid, typeLabel, node.document_type, node.status, inDeg, node.state !== 'indexed' ? node.state : null];
      meta.textContent = parts.filter(Boolean).join(' · ');
      tip.appendChild(title);
      tip.appendChild(meta);
      tip.hidden = false;
      const left = Math.min(sx + 14, this.width - tip.offsetWidth - 8);
      const top = Math.min(sy + 14, this.height - tip.offsetHeight - 8);
      tip.style.left = `${Math.max(8, left)}px`;
      tip.style.top = `${Math.max(8, top)}px`;
    }

    // ---------------------------------------------------------------- drawing

    requestDraw() {
      if (this.drawPending) return;
      this.drawPending = true;
      requestAnimationFrame(() => {
        this.drawPending = false;
        this._draw();
      });
    }

    _releaseFrame() {
      if (this.frameCache?.canvas.close) this.frameCache.canvas.close();
      this.frameCache = null;
    }

    _requestWorkerFrame() {
      if (this.nodes.length <= 5000 || this.renderWorkerFailed || typeof OffscreenCanvas === 'undefined') return false;
      if (this.renderBusy) return true;
      if (!this.renderWorker) {
        try {
          this.renderWorker = new Worker(new URL('graph-render-worker.js', new URL(this.workerUrl, document.baseURI)));
          this.renderVersion = -1;
          const worker = this.renderWorker;
          this.renderWorker.onerror = (event) => {
            event.preventDefault();
            if (worker !== this.renderWorker) return;
            worker.terminate(); this.renderWorker = null;
            this.renderBusy = false; this.renderWorkerFailed = true;
            this.requestDraw();
          };
        } catch (err) { this.renderWorkerFailed = true; return false; }
      }
      const { width, height, dpr, k, tx, ty, sceneVersion: version, highlight } = this;
      const request = { width, height, dpr, k, tx, ty, colors: this.colors, fontFamily: this.fontFamily,
        selectedUid: this.selectedUid, selectedEdgeId: this.selectedEdgeId, highlight: highlight ? [...highlight] : null };
      if (this.renderVersion !== version) {
        request.nodes = this.nodes.map((n) => ({ idx: n.idx, uid: n.uid, x: n.x, y: n.y, r: n.r, type: n.type, state: n.state }));
        request.edges = this.edges.map((e) => ({ source: e.source, target: e.target, kind: e.kind, _id: e._id, s: e.s.idx, t: e.t.idx }));
        this.renderVersion = version;
      }
      this.renderBusy = true;
      const worker = this.renderWorker;
      this.renderWorker.onmessage = ({ data }) => {
        if (worker !== this.renderWorker) { data.bitmap?.close(); return; }
        this.renderBusy = false;
        if (data.error) {
          worker.terminate(); this.renderWorker = null; this.renderWorkerFailed = true;
        } else if (version === this.sceneVersion) {
          this._releaseFrame();
          const margin = 40 / k;
          const nodes = this.nodeIndex ? this.nodeIndex.query(-tx / k - margin, -ty / k - margin,
            (width - tx) / k + margin, (height - ty) / k + margin) : this.nodes;
          this.frameCache = { canvas: data.bitmap, version, width, height, dpr, k, tx, ty, highlight, nodes };
        } else data.bitmap.close();
        this.requestDraw();
      };
      try { worker.postMessage(request); }
      catch (err) {
        worker.terminate(); this.renderWorker = null;
        this.renderBusy = false; this.renderWorkerFailed = true;
        return false;
      }
      return true;
    }

    _notifyRenderReady() {
      if (this.renderReadyVersion === this.sceneVersion) return;
      this.renderReadyVersion = this.sceneVersion;
      this.onRenderReady();
    }

    _deferDetail() {
      if (this.nodes.length <= 5000) return;
      this.viewInteracting = true;
      clearTimeout(this.detailTimer);
      this.detailTimer = setTimeout(() => { this.viewInteracting = false; this.requestDraw(); }, 100);
    }

    _draw() {
      const { ctx, width, height, dpr, k, tx, ty } = this;
      if (!width || !height) return;
      if (this.nodes.length && this.positionsReady === false) return;
      // Large layouts settle quickly; do not queue an expensive rasterization of
      // the temporary cluster positions ahead of the final frame.
      if (this.nodes.length > 5000 && !this.layoutDone) return;
      const cache = this.frameCache;
      const valid = cache && cache.version === this.sceneVersion && cache.width === width &&
        cache.height === height && cache.dpr === dpr && cache.highlight === this.highlight;
      const sameView = valid && cache.k === k && cache.tx === tx && cache.ty === ty;
      if (valid && (sameView || this.viewInteracting)) {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        const scale = k / cache.k;
        ctx.setTransform(scale, 0, 0, scale, (tx - cache.tx * scale) * dpr, (ty - cache.ty * scale) * dpr);
        ctx.drawImage(cache.canvas, 0, 0);
        // Labels and focus rings retain their original screen size while zooming.
        this._drawDecorations(cache.nodes);
        this._notifyRenderReady();
        return;
      }
      if (this._requestWorkerFrame()) {
        // Keep the last completed frame responsive while the next one rasterizes.
        if (cache) {
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
          const scale = k / cache.k;
          ctx.setTransform(scale, 0, 0, scale, (tx - cache.tx * scale) * dpr, (ty - cache.ty * scale) * dpr);
          ctx.drawImage(cache.canvas, 0, 0);
          this._drawDecorations(cache.nodes);
        }
        return;
      }
      const visibleNodes = this._drawScene();
      if (this.nodes.length > 5000) {
        const canvas = cache && !cache.canvas.close ? cache.canvas : document.createElement('canvas');
        if (cache?.canvas.close) cache.canvas.close();
        canvas.width = this.canvas.width; canvas.height = this.canvas.height;
        canvas.getContext('2d').drawImage(this.canvas, 0, 0);
        this.frameCache = { canvas, version: this.sceneVersion, width, height, dpr, k, tx, ty,
          highlight: this.highlight, nodes: visibleNodes };
      }
      this._drawDecorations(visibleNodes);
      this._notifyRenderReady();
    }

    _drawScene() {
      const { width: w, height: h, dpr, k, tx, ty, colors } = this;
      let ctx = this.ctx;
      if (!w || !h) return [];
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (!this.nodes.length) return [];

      // visible world rectangle (+ margin)
      const margin = 40 / k;
      const vx0 = -tx / k - margin, vy0 = -ty / k - margin;
      const vx1 = (w - tx) / k + margin, vy1 = (h - ty) / k + margin;
      const visible = (n) => n.x + n.r >= vx0 && n.x - n.r <= vx1 && n.y + n.r >= vy0 && n.y - n.r <= vy1;

      ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * tx, dpr * ty);
      const hl = this.highlight;

      // Composite each style once at its original opacity. Bounded paths avoid
      // pathological Canvas tessellation without darkening overlapping edges.
      const layered = this.nodes.length > 5000;
      const beginStyle = (alpha) => {
        if (!layered) { ctx.globalAlpha = alpha; return; }
        const canvas = this.styleCanvas || (this.styleCanvas = typeof document === 'undefined' ?
          new OffscreenCanvas(this.canvas.width, this.canvas.height) : document.createElement('canvas'));
        if (canvas.width !== this.canvas.width || canvas.height !== this.canvas.height) {
          canvas.width = this.canvas.width; canvas.height = this.canvas.height;
        }
        ctx = canvas.getContext('2d');
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * tx, dpr * ty);
        ctx.globalAlpha = 1;
        this.ctx.globalAlpha = alpha;
      };
      const endStyle = () => {
        if (!layered) return;
        this.ctx.setTransform(1, 0, 0, 1, 0, 0);
        this.ctx.drawImage(this.styleCanvas, 0, 0);
        this.ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * tx, dpr * ty);
        this.ctx.globalAlpha = 1;
        ctx = this.ctx;
      };

      // ---- edges, batched per style (and per dimmed/highlighted)
      const groups = new Map();
      let visibleEdges = 0;
      const edgeCandidates = this.edgeIndex ? this.edgeIndex.query(vx0, vy0, vx1, vy1) : this.edges;
      for (const e of edgeCandidates) {
        const { s, t } = e;
        if (Math.max(s.x, t.x) < vx0 || Math.min(s.x, t.x) > vx1 || Math.max(s.y, t.y) < vy0 || Math.min(s.y, t.y) > vy1) continue;
        visibleEdges++;
        const active = !hl || (hl.has(e.source) && hl.has(e.target) &&
          (this.selectedEdgeId ? e._id === this.selectedEdgeId : (e.source === this.selectedUid || e.target === this.selectedUid)));
        const key = `${e.kind}|${active ? 1 : 0}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(e);
      }
      // Dash patterns and arrowheads are expensive for the canvas rasterizer; only use them
      // when few edges are visible and they are readable anyway.
      const detailed = k >= 0.5 && visibleEdges <= 2500;
      const drawArrows = detailed;
      const sortedKeys = [...groups.keys()].sort((a, b) => a.endsWith('|1') - b.endsWith('|1'));
      for (const key of sortedKeys) {
        const [kind, activeFlag] = key.split('|');
        const style = EDGE_STYLES[kind] || EDGE_STYLES.links_to;
        const color = colors[style.color] || colors.muted;
        const active = activeFlag === '1';
        beginStyle(hl ? (active ? 1 : 0.08) : 0.55);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = ((active && hl) ? style.width + 1 : style.width) / k;
        ctx.setLineDash(detailed ? style.dash.map((d) => d / k) : []);
        ctx.beginPath();
        const list = groups.get(key);
        let batch = 0;
        for (const e of list) {
          if (layered && ++batch % 256 === 0) { ctx.stroke(); ctx.beginPath(); }
          const { s, t } = e;
          const dx = t.x - s.x, dy = t.y - s.y;
          const len = Math.hypot(dx, dy) || 1;
          const ux = dx / len, uy = dy / len;
          ctx.moveTo(s.x + ux * s.r, s.y + uy * s.r);
          ctx.lineTo(t.x - ux * (t.r + (drawArrows ? 2 / k : 0)), t.y - uy * (t.r + (drawArrows ? 2 / k : 0)));
        }
        ctx.stroke();
        if (drawArrows && (!hl || active || list.length < 1500)) {
          ctx.setLineDash([]);
          ctx.beginPath();
          const a = 7 / k, b = 3.5 / k;
          for (const e of list) {
            const { s, t } = e;
            const dx = t.x - s.x, dy = t.y - s.y;
            const len = Math.hypot(dx, dy) || 1;
            const ux = dx / len, uy = dy / len;
            const px = t.x - ux * t.r, py = t.y - uy * t.r;
            ctx.moveTo(px, py);
            ctx.lineTo(px - ux * a - uy * b, py - uy * a + ux * b);
            ctx.lineTo(px - ux * a + uy * b, py - uy * a - ux * b);
            ctx.closePath();
          }
          ctx.fill();
        }
        endStyle();
      }
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      // ---- nodes, batched by visual style
      const nodeGroups = new Map();
      const visibleNodes = [];
      const nodeCandidates = this.nodeIndex ? this.nodeIndex.query(vx0, vy0, vx1, vy1) : this.nodes;
      for (const n of nodeCandidates) {
        if (!visible(n)) continue;
        visibleNodes.push(n);
        const dim = hl && !hl.has(n.uid);
        const status = ERROR_STATES.has(n.state) ? 'error' : n.state === 'indexed' ? 'ok' : 'ref';
        const key = `${n.type}|${status}|${dim ? 1 : 0}`;
        if (!nodeGroups.has(key)) nodeGroups.set(key, []);
        nodeGroups.get(key).push(n);
      }
      for (const [key, list] of nodeGroups) {
        const [type, status, dimFlag] = key.split('|');
        const base = type === 'jira' ? colors.jira : colors.confluence;
        beginStyle(dimFlag === '1' ? 0.15 : 1);
        ctx.beginPath();
        // Bounded paths avoid quadratic tessellation work in Canvas implementations.
        const paint = (method) => {
          ctx.beginPath();
          for (let i = 0; i < list.length; i++) {
            this._shapePath(list[i], type, ctx);
            if (layered && i % 256 === 255) { ctx[method](); ctx.beginPath(); }
          }
          ctx[method]();
        };
        if (status === 'ok') {
          ctx.fillStyle = base;
          paint('fill');
          ctx.lineWidth = 1.5 / k;
          ctx.strokeStyle = colors.surface;
          ctx.setLineDash([]);
          paint('stroke');
        } else {
          ctx.fillStyle = colors.surface;
          paint('fill');
          ctx.lineWidth = 1.6 / k;
          ctx.strokeStyle = status === 'error' ? colors.danger : colors.muted;
          ctx.setLineDash([3 / k, 2.5 / k]);
          paint('stroke');
          ctx.setLineDash([]);
        }
        endStyle();
      }
      ctx.globalAlpha = 1;

      // ---- glyphs (only when big enough to read)
      if (k * 7 >= 6) {
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.font = `600 ${Math.min(14, Math.max(9, k * 9))}px ${this.fontFamily}`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        for (const n of visibleNodes) {
          if (n.state !== 'indexed' || n.r * k < 8) continue;
          ctx.globalAlpha = hl && !hl.has(n.uid) ? 0.2 : 1;
          ctx.fillStyle = '#ffffff';
          ctx.fillText(n.type === 'jira' ? 'J' : 'C', n.x * k + tx, n.y * k + ty + 0.5);
        }
        ctx.globalAlpha = 1;
      }

      return visibleNodes;
    }

    _drawDecorations(visibleNodes) {
      const { ctx, colors, k, tx, ty, dpr } = this;
      const visible = (n) => n.x * k + tx + n.r * k >= 0 && n.x * k + tx - n.r * k <= this.width &&
        n.y * k + ty + n.r * k >= 0 && n.y * k + ty - n.r * k <= this.height;
      ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * tx, dpr * ty);
      // ---- rings for center / selected / hovered
      const ring = (n, color, width, gap) => {
        if (!n || !visible(n)) return;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r + gap / k, 0, Math.PI * 2);
        ctx.lineWidth = width / k;
        ctx.strokeStyle = color;
        ctx.stroke();
      };
      ring(this.nodeMap.get(this.centerUid), colors.main, 1.5, 4);
      ring(this.nodeMap.get(this.selectedUid), colors.focus, 3, 3);
      ring(this.hoverNode, colors.focus, 1.5, 3);

      this._drawLabels(visibleNodes);
    }

    _shapePath(n, type, ctx = this.ctx) {
      if (type === 'jira') {
        const r = n.r * 0.92, x = n.x - r, y = n.y - r, s = 2 * r, c = r * 0.35;
        ctx.moveTo(x + c, y);
        ctx.arcTo(x + s, y, x + s, y + s, c);
        ctx.arcTo(x + s, y + s, x, y + s, c);
        ctx.arcTo(x, y + s, x, y, c);
        ctx.arcTo(x, y, x + s, y, c);
        ctx.closePath();
      } else {
        ctx.moveTo(n.x + n.r, n.y);
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      }
    }

    /** Priority-based label placement: labels never overlap each other. */
    _drawLabels(visibleNodes) {
      const { ctx, k, tx, ty, dpr, colors } = this;
      const hl = this.highlight;
      const prio = (n) => {
        if (n.uid === this.selectedUid) return 0;
        if (n === this.hoverNode) return 1;
        if (n.uid === this.centerUid) return 2;
        if (hl && hl.has(n.uid)) return 3;
        return 4;
      };
      // Only nodes that are large enough on screen get labels, except important ones.
      const labelNodes = this.maxRadius * k < 2 ?
        [...new Set([this.nodeMap.get(this.selectedUid), this.hoverNode, this.nodeMap.get(this.centerUid),
          ...(hl ? [...hl].map((uid) => this.nodeMap.get(uid)) : [])])].filter(Boolean) : visibleNodes;
      const candidates = labelNodes.filter((n) => prio(n) < 4 || n.r * k >= 5);
      candidates.sort((a, b) => prio(a) - prio(b) || (b.degree || 0) - (a.degree || 0) || (a.uid < b.uid ? -1 : 1));

      if (!candidates.length) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.font = `${LABEL_FONT_PX}px ${this.fontFamily}`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.lineJoin = 'round';
      const cell = 80;
      const placed = new Map();
      const reserve = (rect) => {
        for (let gx = Math.floor(rect[0] / cell); gx <= Math.floor(rect[2] / cell); gx++) {
          for (let gy = Math.floor(rect[1] / cell); gy <= Math.floor(rect[3] / cell); gy++) {
            const key = `${gx},${gy}`;
            if (!placed.has(key)) placed.set(key, []);
            placed.get(key).push(rect);
          }
        }
      };
      // Node bodies are obstacles too, so labels never cover other nodes.
      for (const n of this.maxRadius * k < 2 ? [] : visibleNodes) {
        const sx = n.x * k + tx, sy = n.y * k + ty, r = n.r * k;
        if (r >= 2) reserve([sx - r, sy - r, sx + r, sy + r, n.uid]);
      }

      let count = 0;
      for (const n of candidates) {
        if (count >= MAX_LABELS) break;
        const sx = n.x * k + tx;
        const sy = n.y * k + ty + n.r * k + 3;
        const width = n.labelWidth ?? (n.labelWidth = ctx.measureText(n.label).width);
        const rect = [sx - width / 2 - 2, sy - 1, sx + width / 2 + 2, sy + LABEL_FONT_PX + 2];
        // ignore the node's own body when checking
        let hit = false;
        for (let gx = Math.floor(rect[0] / cell); gx <= Math.floor(rect[2] / cell) && !hit; gx++) {
          for (let gy = Math.floor(rect[1] / cell); gy <= Math.floor(rect[3] / cell) && !hit; gy++) {
            const bucket = placed.get(`${gx},${gy}`);
            if (!bucket) continue;
            for (const r of bucket) {
              if (r[4] === n.uid) continue;
              if (rect[0] < r[2] && rect[2] > r[0] && rect[1] < r[3] && rect[3] > r[1]) { hit = true; break; }
            }
          }
        }
        if (hit && prio(n) > 1) continue;   // selected/hovered labels are always shown
        reserve(rect);
        count++;
        ctx.globalAlpha = hl && !hl.has(n.uid) ? 0.35 : 1;
        ctx.strokeStyle = colors.app;
        ctx.lineWidth = 3;
        ctx.strokeText(n.label, sx, sy);
        ctx.fillStyle = colors.main;
        ctx.fillText(n.label, sx, sy);
      }
      ctx.globalAlpha = 1;
    }
  }

  window.KnowledgeGraph = KnowledgeGraph;
})();
