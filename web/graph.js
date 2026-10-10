/**
 * Knowledge-Graph-Renderer auf Canvas.
 *
 * - Layout läuft in graph-layout-worker.js (Barnes-Hut, Kollision, keine Überlappungen).
 * - Rendering per Canvas mit Viewport-Culling und gebündelten Pfaden pro Stil,
 *   damit auch mehrere tausend Knoten flüssig bleiben.
 * - Beschriftungen werden nach Priorität platziert und nie übereinander gezeichnet;
 *   beim Hineinzoomen erscheinen automatisch mehr.
 * Vanilla JS, keine externen Abhängigkeiten, kein innerHTML.
 */
(function () {
  'use strict';

  const MIN_ZOOM = 0.02;
  const MAX_ZOOM = 8;
  const LABEL_FONT_PX = 12;
  const MAX_LABELS = 700;
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

  class KnowledgeGraph {
    /**
     * @param {Object} options
     * @param {HTMLCanvasElement} options.canvasEl
     * @param {HTMLElement} options.containerEl
     * @param {Function} [options.onNodeSelect]   (node) => void
     * @param {Function} [options.onNodeActivate] (node) => void  (Doppelklick / Enter)
     * @param {Function} [options.onEdgeSelect]   (edge) => void
     * @param {Function} [options.onLayoutProgress] (progress 0..1, done:boolean) => void
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

      this.k = 1; this.tx = 0; this.ty = 0;
      this.width = 0; this.height = 0; this.dpr = 1;
      this.userMovedView = false;
      this.layoutDone = true;
      this.worker = null;
      this.layoutId = 0;
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

    setData(data, centerUid) {
      this.centerUid = centerUid || data.center || null;
      this.selectedUid = null;
      this.selectedEdgeId = null;
      this.highlight = null;
      this.hoverNode = null;
      this.tooltip.hidden = true;
      this.keyboardIndex = -1;
      this.userMovedView = false;

      this.nodeMap.clear();
      this.nodes = (data.nodes || []).map((n, idx) => {
        const node = { ...n, idx, x: 0, y: 0 };
        node.r = KnowledgeGraph.radiusFor(node, node.uid === this.centerUid);
        node.label = truncate(node.title || node.uid, 34);
        this.nodeMap.set(node.uid, node);
        return node;
      });
      this.edgeMap.clear();
      this.edges = [];
      for (const e of data.edges || []) {
        const s = this.nodeMap.get(e.source);
        const t = this.nodeMap.get(e.target);
        if (!s || !t) continue;
        const edge = { ...e, s, t, _id: String(e.id ?? `${e.source}->${e.target}:${e.kind}`) };
        this.edges.push(edge);
        this.edgeMap.set(edge._id, edge);
      }
      this.adjacency = new Map();
      for (const e of this.edges) {
        if (!this.adjacency.has(e.source)) this.adjacency.set(e.source, new Set());
        if (!this.adjacency.has(e.target)) this.adjacency.set(e.target, new Set());
        this.adjacency.get(e.source).add(e.target);
        this.adjacency.get(e.target).add(e.source);
      }
      this.keyboardOrder = [...this.nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0) || (a.uid < b.uid ? -1 : 1));
      this._startLayout();
    }

    _startLayout() {
      if (this.worker) this.worker.terminate();
      this.worker = null;
      const n = this.nodes.length;
      const center = this.centerUid && this.nodeMap.has(this.centerUid) ? this.nodeMap.get(this.centerUid).idx : -1;
      if (n === 0) {
        this.layoutDone = true;
        this.onLayoutProgress(1, true);
        this.requestDraw();
        return;
      }
      const radii = new Float32Array(n);
      this.nodes.forEach((node) => { radii[node.idx] = node.r; });
      const links = new Uint32Array(this.edges.length * 2);
      this.edges.forEach((e, k) => { links[2 * k] = e.s.idx; links[2 * k + 1] = e.t.idx; });

      const id = ++this.layoutId;
      this.layoutDone = false;
      this.onLayoutProgress(0, false);
      const msg = { id, count: n, radii, links, center };
      const handle = (m) => {
        if (m.id !== id || id !== this.layoutId) return;
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
      const watchdog = setTimeout(() => {
        if (heardBack || id !== this.layoutId) return;
        worker.terminate();
        if (this.worker === worker) this.worker = null;
        this._mainThreadLayout({ ...msg, radii: Float32Array.from(this.nodes.map((nd) => nd.r)),
                                 links: Uint32Array.from(this.edges.flatMap((e) => [e.s.idx, e.t.idx])) }, handle);
      }, 2000);
      worker.onmessage = (ev) => {
        heardBack = true;
        clearTimeout(watchdog);
        if (ev.data.type === 'done') {
          worker.terminate();
          if (this.worker === worker) this.worker = null;
        }
        handle(ev.data);
      };
      worker.onerror = (ev) => {
        ev.preventDefault();
        clearTimeout(watchdog);
        if (heardBack) { this._gridLayout(); return; }
        heardBack = true;
        this._mainThreadLayout({ ...msg, radii: Float32Array.from(this.nodes.map((nd) => nd.r)),
                                 links: Uint32Array.from(this.edges.flatMap((e) => [e.s.idx, e.t.idx])) }, handle);
      };
      // Copies are sent so the main-thread fallback could still use the originals.
      worker.postMessage({ ...msg, radii: radii.slice(), links: links.slice() });
    }

    _applyPositions(p, done, progress) {
      for (const node of this.nodes) {
        if (node.pinned) continue;
        node.x = p[2 * node.idx];
        node.y = p[2 * node.idx + 1];
      }
      if (done) {
        this.layoutDone = true;
        this._buildGrid();
      }
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
      const slot = 2 * Math.max(...this.nodes.map((nd) => nd.r)) + 12;
      this.keyboardOrder.forEach((node, i) => {
        node.x = (i % cols) * slot;
        node.y = Math.floor(i / cols) * slot;
      });
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
      this.highlight = new Set([uid, ...(this.adjacency.get(uid) || [])]);
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
      const maxR = this.nodes.reduce((m, n) => Math.max(m, n.r), 1);
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
      if (this.grid && this.layoutDone) {
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
      for (const e of this.edges) {
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
            this.tx += dx; this.ty += dy;
            this.userMovedView = true;
          } else {
            drag.node.x += dx / this.k;
            drag.node.y += dy / this.k;
            drag.node.pinned = true;
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
        if (d.mode === 'node' && d.moved) { this._buildGrid(); return; }
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
      const parts = [node.uid, typeLabel, node.document_type, node.status, node.state !== 'indexed' ? node.state : null];
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

    _draw() {
      const { ctx, width: w, height: h, dpr, k, tx, ty, colors } = this;
      if (!w || !h) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (!this.nodes.length) return;

      // visible world rectangle (+ margin)
      const margin = 40 / k;
      const vx0 = -tx / k - margin, vy0 = -ty / k - margin;
      const vx1 = (w - tx) / k + margin, vy1 = (h - ty) / k + margin;
      const visible = (n) => n.x + n.r >= vx0 && n.x - n.r <= vx1 && n.y + n.r >= vy0 && n.y - n.r <= vy1;

      ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * tx, dpr * ty);
      const hl = this.highlight;

      // ---- edges, batched per style (and per dimmed/highlighted)
      const groups = new Map();
      let visibleEdges = 0;
      for (const e of this.edges) {
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
        ctx.globalAlpha = hl ? (active ? 1 : 0.08) : 0.55;
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = ((active && hl) ? style.width + 1 : style.width) / k;
        ctx.setLineDash(detailed ? style.dash.map((d) => d / k) : []);
        ctx.beginPath();
        const list = groups.get(key);
        for (const e of list) {
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
      }
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      // ---- nodes, batched by visual style
      const nodeGroups = new Map();
      const visibleNodes = [];
      for (const n of this.nodes) {
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
        ctx.globalAlpha = dimFlag === '1' ? 0.15 : 1;
        ctx.beginPath();
        for (const n of list) this._shapePath(n, type);
        if (status === 'ok') {
          ctx.fillStyle = base;
          ctx.fill();
          ctx.lineWidth = 1.5 / k;
          ctx.strokeStyle = colors.surface;
          ctx.setLineDash([]);
          ctx.stroke();
        } else {
          ctx.fillStyle = colors.surface;
          ctx.fill();
          ctx.lineWidth = 1.6 / k;
          ctx.strokeStyle = status === 'error' ? colors.danger : colors.muted;
          ctx.setLineDash([3 / k, 2.5 / k]);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
      ctx.globalAlpha = 1;

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

      // ---- glyphs (only when big enough to read)
      if (k * 7 >= 6) {
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.font = `600 ${Math.min(14, Math.max(9, k * 9))}px ${cssVar('--font-sans', 'sans-serif')}`;
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

      this._drawLabels(visibleNodes);
    }

    _shapePath(n, type) {
      const { ctx } = this;
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
      const candidates = visibleNodes.filter((n) => prio(n) < 4 || n.r * k >= 5);
      candidates.sort((a, b) => prio(a) - prio(b) || (b.degree || 0) - (a.degree || 0) || (a.uid < b.uid ? -1 : 1));

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.font = `${LABEL_FONT_PX}px ${cssVar('--font-sans', 'sans-serif')}`;
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
      for (const n of visibleNodes) {
        const sx = n.x * k + tx, sy = n.y * k + ty, r = n.r * k;
        if (r >= 2) reserve([sx - r, sy - r, sx + r, sy + r, n.uid]);
      }

      let count = 0;
      for (const n of candidates) {
        if (count >= MAX_LABELS) break;
        const sx = n.x * k + tx;
        const sy = n.y * k + ty + n.r * k + 3;
        const width = ctx.measureText(n.label).width;
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
