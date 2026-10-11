/**
 * Force-directed layout for the knowledge graph, executed in a Web Worker.
 *
 * - Barnes-Hut quadtree for repulsion (O(n log n)), springs along edges,
 *   weak gravity and a grid-based collision force.
 * - Deterministic: initial positions come from BFS rings (with center) or a
 *   BFS-ordered phyllotaxis spiral (overview); no Math.random().
 * - A final overlap-removal pass guarantees that no two nodes overlap.
 *
 * Message in:  {id, count, radii: Float32Array, links: Uint32Array (s0,t0,s1,t1,…), center: int}
 * Message out: {id, type: 'tick'|'done', progress: 0..1, positions: Float32Array (x0,y0,…)}
 */
'use strict';

const PADDING = 18;           // minimum free space between two node outlines (world units)
const LINK_DISTANCE = 70;     // spring rest length added to both radii
const GRAVITY = 0.012;
const VELOCITY_KEEP = 0.6;    // d3 velocityDecay 0.4

function jitter(i) {
  return ((((i + 1) * 9301 + 49297) % 233280) / 233280 - 0.5) * 1e-2;
}

// ------------------------------------------------------------------ initial positions

function buildAdjacency(n, links) {
  const deg = new Uint32Array(n);
  for (let k = 0; k < links.length; k += 2) { deg[links[k]]++; deg[links[k + 1]]++; }
  const start = new Uint32Array(n + 1);
  for (let i = 0; i < n; i++) start[i + 1] = start[i] + deg[i];
  const fill = start.slice(0, n);
  const adj = new Uint32Array(start[n]);
  for (let k = 0; k < links.length; k += 2) {
    const s = links[k], t = links[k + 1];
    adj[fill[s]++] = t;
    adj[fill[t]++] = s;
  }
  return { deg, start, adj };
}

function initialPositions(n, radii, graph, center, x, y, importanceMode, importance) {
  const { deg, start, adj } = graph;
  let avgR = 0;
  for (let i = 0; i < n; i++) avgR += radii[i];
  avgR = n ? avgR / n : 10;
  const slot = 2 * avgR + PADDING + 8;

  if (importanceMode) {
    // In importance mode, the most important (highest in-degree) nodes start at the center
    const byImp = Array.from({ length: n }, (_, i) => i).sort((a, b) => {
      const impDiff = (importance ? importance[b] : 0) - (importance ? importance[a] : 0);
      return impDiff || deg[b] - deg[a] || a - b;
    });
    const golden = Math.PI * (3 - Math.sqrt(5));
    byImp.forEach((v, idx) => {
      const r = slot * 0.75 * Math.sqrt(idx + 0.3);
      x[v] = Math.cos(idx * golden) * r + jitter(v);
      y[v] = Math.sin(idx * golden) * r + jitter(v + 7);
    });
    return;
  }

  // BFS order (from center, else component-wise from highest degree nodes).
  const order = [];
  const level = new Int32Array(n).fill(-1);
  const parent = new Int32Array(n).fill(-1);
  const byDegree = Array.from({ length: n }, (_, i) => i).sort((a, b) => deg[b] - deg[a] || a - b);
  const roots = center >= 0 ? [center, ...byDegree] : byDegree;
  for (const root of roots) {
    if (level[root] !== -1) continue;
    level[root] = 0;
    let head = order.length;
    order.push(root);
    while (head < order.length) {
      const v = order[head++];
      const nbs = [];
      for (let k = start[v]; k < start[v + 1]; k++) if (level[adj[k]] === -1) nbs.push(adj[k]);
      nbs.sort((a, b) => deg[b] - deg[a] || a - b);
      for (const w of nbs) {
        if (level[w] !== -1) continue;
        level[w] = level[v] + 1;
        parent[w] = v;
        order.push(w);
      }
    }
  }

  if (center >= 0) {
    // Concentric rings around the center; children stay near their parent's angle.
    const angle = new Float64Array(n);
    const rings = new Map();
    for (const v of order) {
      if (v === center) continue;
      const l = level[v] > 0 ? level[v] : 1;
      if (!rings.has(l)) rings.set(l, []);
      rings.get(l).push(v);
    }
    x[center] = 0; y[center] = 0;
    let radius = 0;
    const levels = [...rings.keys()].sort((a, b) => a - b);
    for (const l of levels) {
      const ring = rings.get(l);
      ring.sort((a, b) => (parent[a] >= 0 ? angle[parent[a]] : 0) - (parent[b] >= 0 ? angle[parent[b]] : 0) || a - b);
      radius = Math.max(radius + slot * 3, (ring.length * slot) / (2 * Math.PI));
      ring.forEach((v, idx) => {
        const a = (2 * Math.PI * idx) / ring.length;
        angle[v] = a;
        x[v] = Math.cos(a) * radius + jitter(v);
        y[v] = Math.sin(a) * radius + jitter(v + 7);
      });
    }
  } else {
    // Phyllotaxis spiral in BFS order: neighbours start close to each other.
    const golden = Math.PI * (3 - Math.sqrt(5));
    order.forEach((v, idx) => {
      const r = slot * 0.75 * Math.sqrt(idx + 0.5);
      x[v] = Math.cos(idx * golden) * r + jitter(v);
      y[v] = Math.sin(idx * golden) * r + jitter(v + 7);
    });
  }
}

// ------------------------------------------------------------------ Barnes-Hut quadtree

class QuadTree {
  constructor(capacity) {
    this.alloc(Math.max(64, capacity * 4));
  }

  alloc(size) {
    const grow = (old, Type, fillValue) => {
      const arr = new Type(size);
      if (fillValue !== undefined) arr.fill(fillValue);
      if (old) arr.set(old.subarray(0, Math.min(old.length, size)));
      return arr;
    };
    this.x0 = grow(this.x0, Float64Array);
    this.y0 = grow(this.y0, Float64Array);
    this.size = grow(this.size, Float64Array);
    this.child = grow(this.child, Int32Array, -1);   // index of first of 4 children
    this.body = grow(this.body, Int32Array, -1);     // first body in leaf (linked list)
    this.mass = grow(this.mass, Float64Array);
    this.cx = grow(this.cx, Float64Array);
    this.cy = grow(this.cy, Float64Array);
    this.cap = size;
  }

  newQuad(x0, y0, size) {
    if (this.count + 1 > this.cap) this.alloc(this.cap * 2);
    const q = this.count++;
    this.x0[q] = x0; this.y0[q] = y0; this.size[q] = size;
    this.child[q] = -1; this.body[q] = -1; this.mass[q] = 0; this.cx[q] = 0; this.cy[q] = 0;
    return q;
  }

  build(n, x, y, strength, next) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (let i = 0; i < n; i++) {
      if (x[i] < minX) minX = x[i];
      if (x[i] > maxX) maxX = x[i];
      if (y[i] < minY) minY = y[i];
      if (y[i] > maxY) maxY = y[i];
    }
    const size = Math.max(maxX - minX, maxY - minY, 1) * 1.0001 + 1;
    this.count = 0;
    this.next = next;
    this.newQuad(minX, minY, size);
    for (let i = 0; i < n; i++) this.insert(i, x, y);
    this.accumulate(0, x, y, strength);
  }

  insert(i, x, y) {
    let q = 0;
    let depth = 0;
    for (;;) {
      if (this.child[q] === -1) {
        if (this.body[q] === -1) { this.body[q] = i; this.next[i] = -1; return; }
        if (depth > 40) { this.next[i] = this.body[q]; this.body[q] = i; return; }
        // split leaf, push existing bodies down
        const half = this.size[q] / 2;
        const first = this.newQuad(this.x0[q], this.y0[q], half);
        this.newQuad(this.x0[q] + half, this.y0[q], half);
        this.newQuad(this.x0[q], this.y0[q] + half, half);
        this.newQuad(this.x0[q] + half, this.y0[q] + half, half);
        this.child[q] = first;
        let b = this.body[q];
        this.body[q] = -1;
        while (b !== -1) {
          const nb = this.next[b];
          const c = this.childFor(q, x[b], y[b]);
          this.next[b] = this.body[c];
          this.body[c] = b;
          b = nb;
        }
      }
      q = this.childFor(q, x[i], y[i]);
      depth++;
    }
  }

  childFor(q, px, py) {
    const half = this.size[q] / 2;
    const right = px >= this.x0[q] + half ? 1 : 0;
    const bottom = py >= this.y0[q] + half ? 2 : 0;
    return this.child[q] + right + bottom;
  }

  accumulate(root, x, y, strength) {
    // iterative post-order
    const stack = [root];
    const post = [];
    while (stack.length) {
      const q = stack.pop();
      post.push(q);
      if (this.child[q] !== -1) for (let c = 0; c < 4; c++) stack.push(this.child[q] + c);
    }
    for (let k = post.length - 1; k >= 0; k--) {
      const q = post[k];
      let m = 0, sx = 0, sy = 0, w = 0;
      if (this.child[q] === -1) {
        for (let b = this.body[q]; b !== -1; b = this.next[b]) {
          const s = strength[b];
          const a = Math.abs(s);
          m += s; sx += a * x[b]; sy += a * y[b]; w += a;
        }
      } else {
        for (let c = 0; c < 4; c++) {
          const cq = this.child[q] + c;
          const a = Math.abs(this.mass[cq]);
          m += this.mass[cq]; sx += a * this.cx[cq]; sy += a * this.cy[cq]; w += a;
        }
      }
      this.mass[q] = m;
      this.cx[q] = w ? sx / w : this.x0[q] + this.size[q] / 2;
      this.cy[q] = w ? sy / w : this.y0[q] + this.size[q] / 2;
    }
  }

  applyCharge(i, x, y, vx, vy, strength, alpha, stack, theta2) {
    let top = 0;
    stack[top++] = 0;
    const xi = x[i], yi = y[i];
    while (top) {
      const q = stack[--top];
      if (this.mass[q] === 0) continue;
      let dx = this.cx[q] - xi;
      let dy = this.cy[q] - yi;
      let l = dx * dx + dy * dy;
      const w = this.size[q];
      if (this.child[q] !== -1) {
        if ((w * w) / theta2 < l) {
          if (l < 1) l = Math.sqrt(l);
          const f = (this.mass[q] * alpha) / l;
          vx[i] += dx * f; vy[i] += dy * f;
        } else {
          if (top + 4 > stack.length) stack.length = top + 64;
          for (let c = 0; c < 4; c++) stack[top++] = this.child[q] + c;
        }
        continue;
      }
      for (let b = this.body[q]; b !== -1; b = this.next[b]) {
        if (b === i) continue;
        dx = x[b] - xi; dy = y[b] - yi; l = dx * dx + dy * dy;
        if (l === 0) { dx = jitter(i + b); dy = jitter(i * 3 + b); l = dx * dx + dy * dy; }
        if (l < 1) l = Math.sqrt(l);
        const f = (strength[b] * alpha) / l;
        vx[i] += dx * f; vy[i] += dy * f;
      }
    }
  }
}

// ------------------------------------------------------------------ collision grid

/**
 * Spatial hash grid in typed arrays (no Map/Array allocations per tick).
 * cellX/cellY are kept so that hash collisions never cause a pair to be handled twice.
 */
class Grid {
  constructor(n) {
    let size = 1;
    while (size < n * 2) size <<= 1;
    this.mask = size - 1;
    this.head = new Int32Array(size);
    this.next = new Int32Array(n);
    this.cellX = new Int32Array(n);
    this.cellY = new Int32Array(n);
  }

  static hash(cx, cy) {
    return (Math.imul(cx, 73856093) ^ Math.imul(cy, 19349663)) >>> 0;
  }

  build(n, x, y, cell) {
    this.head.fill(-1);
    for (let i = 0; i < n; i++) {
      const cx = Math.floor(x[i] / cell);
      const cy = Math.floor(y[i] / cell);
      this.cellX[i] = cx;
      this.cellY[i] = cy;
      const h = Grid.hash(cx, cy) & this.mask;
      this.next[i] = this.head[h];
      this.head[h] = i;
    }
  }
}

/** Push overlapping pairs apart, via velocities (simulation) or positions (final pass). */
function resolveCollisions(grid, n, x, y, vx, vy, radii, maxR, strength, usePosition) {
  const cell = 2 * maxR + PADDING;
  grid.build(n, x, y, cell);
  const { head, next, cellX, cellY, mask } = grid;
  let overlaps = 0;
  for (let i = 0; i < n; i++) {
    const gx = cellX[i];
    const gy = cellY[i];
    for (let ox = -1; ox <= 1; ox++) {
      for (let oy = -1; oy <= 1; oy++) {
        const tx = gx + ox, ty = gy + oy;
        for (let j = head[Grid.hash(tx, ty) & mask]; j !== -1; j = next[j]) {
          if (j <= i || cellX[j] !== tx || cellY[j] !== ty) continue;
          let dx = x[j] - x[i];
          let dy = y[j] - y[i];
          const rr = radii[i] + radii[j] + PADDING;
          let l2 = dx * dx + dy * dy;
          if (l2 >= rr * rr) continue;
          overlaps++;
          if (l2 === 0) { dx = jitter(i + j * 13) + 0.01; dy = jitter(j + i * 17); l2 = dx * dx + dy * dy; }
          const l = Math.sqrt(l2);
          const push = ((rr - l) / l) * strength;
          // bigger nodes move less
          const wi = (radii[j] * radii[j]) / (radii[i] * radii[i] + radii[j] * radii[j]);
          const wj = 1 - wi;
          if (usePosition) {
            x[i] -= dx * push * wi; y[i] -= dy * push * wi;
            x[j] += dx * push * wj; y[j] += dy * push * wj;
          } else {
            vx[i] -= dx * push * wi; vy[i] -= dy * push * wi;
            vx[j] += dx * push * wj; vy[j] += dy * push * wj;
          }
        }
      }
    }
  }
  return overlaps;
}

// ------------------------------------------------------------------ simulation

function* layoutSteps(msg, post) {
  const n = msg.count;
  const radii = msg.radii;
  const links = msg.links;
  const center = msg.center;
  const importanceMode = Boolean(msg.importanceMode);
  const importance = msg.importance;
  const x = new Float64Array(n), y = new Float64Array(n);
  const vx = new Float64Array(n), vy = new Float64Array(n);
  const positions = new Float32Array(2 * n);
  const emit = (type, progress) => {
    for (let i = 0; i < n; i++) { positions[2 * i] = x[i]; positions[2 * i + 1] = y[i]; }
    post({ id: msg.id, type, progress, positions: positions.slice() });
  };
  if (n === 0) { emit('done', 1); return; }

  const graph = buildAdjacency(n, links);
  initialPositions(n, radii, graph, center, x, y, importanceMode, importance);

  let minImp = Infinity, maxImp = -Infinity;
  if (importanceMode && importance) {
    for (let i = 0; i < n; i++) {
      if (importance[i] < minImp) minImp = importance[i];
      if (importance[i] > maxImp) maxImp = importance[i];
    }
  }
  const impSpan = maxImp > minImp ? maxImp - minImp : 0;
  if (n === 1) { emit('done', 1); return; }

  let maxR = 0;
  const strength = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    if (radii[i] > maxR) maxR = radii[i];
    strength[i] = -(28 + radii[i] * 6);     // bigger nodes repel more
  }
  const m = links.length / 2;
  const bias = new Float64Array(m);
  const linkStrength = new Float64Array(m);
  const linkDist = new Float64Array(m);
  for (let k = 0; k < m; k++) {
    const s = links[2 * k], t = links[2 * k + 1];
    const ds = graph.deg[s], dt = graph.deg[t];
    bias[k] = ds / (ds + dt);
    linkStrength[k] = 1 / Math.min(ds, dt);
    linkDist[k] = radii[s] + radii[t] + LINK_DISTANCE;
  }

  // Larger graphs trade a little accuracy for speed (theta 0.9 -> 1.2).
  const iterations = n < 300 ? 300 : n < 2000 ? 330 : 300;
  const theta2 = n < 1500 ? 0.81 : 1.44;
  const alphaMin = 0.001;
  const alphaDecay = 1 - Math.pow(alphaMin, 1 / iterations);
  let alpha = 1;
  const tree = new QuadTree(n);
  const grid = new Grid(n);
  const next = new Int32Array(n);
  const stack = new Array(256);
  const emitEvery = n > 3000 ? 40 : 20;

  for (let iter = 0; iter < iterations; iter++) {
    alpha += (0 - alpha) * alphaDecay;

    // springs
    for (let k = 0; k < m; k++) {
      const s = links[2 * k], t = links[2 * k + 1];
      if (s === t) continue;
      let dx = x[t] + vx[t] - x[s] - vx[s];
      let dy = y[t] + vy[t] - y[s] - vy[s];
      let l = Math.sqrt(dx * dx + dy * dy) || 1e-3;
      l = ((l - linkDist[k]) / l) * alpha * linkStrength[k];
      dx *= l; dy *= l;
      vx[t] -= dx * bias[k]; vy[t] -= dy * bias[k];
      vx[s] += dx * (1 - bias[k]); vy[s] += dy * (1 - bias[k]);
    }

    // repulsion
    tree.build(n, x, y, strength, next);
    for (let i = 0; i < n; i++) tree.applyCharge(i, x, y, vx, vy, strength, alpha, stack, theta2);

    // gravity: in importance mode, important nodes have stronger central pull
    for (let i = 0; i < n; i++) {
      let g = GRAVITY;
      if (importanceMode && impSpan > 0) {
        const ratio = (importance[i] - minImp) / impSpan;
        g = GRAVITY * (0.6 + 2.8 * ratio);
      }
      vx[i] -= x[i] * g * alpha;
      vy[i] -= y[i] * g * alpha;
    }

    // collision
    resolveCollisions(grid, n, x, y, vx, vy, radii, maxR, 0.7, false);

    for (let i = 0; i < n; i++) {
      if (i === center) { vx[i] = 0; vy[i] = 0; continue; }   // keep the focus node fixed
      vx[i] *= VELOCITY_KEEP; vy[i] *= VELOCITY_KEEP;
      x[i] += vx[i]; y[i] += vy[i];
    }
    if (iter % emitEvery === 0) emit('tick', iter / iterations);
    yield;
  }

  // Final pass: positional overlap removal until no two nodes touch.
  for (let pass = 0; pass < 400; pass++) {
    if (resolveCollisions(grid, n, x, y, vx, vy, radii, maxR, 0.5, true) === 0) break;
    if (pass % 10 === 9) yield;
  }
  emit('done', 1);
}

/** Run the whole layout synchronously (worker) – the generator yields between ticks. */
function runLayout(msg, post) {
  const steps = layoutSteps(msg, post);
  while (!steps.next().done) { /* run to completion */ }
}

const isWorker = typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope;
if (isWorker) {
  self.onmessage = (event) => {
    try {
      runLayout(event.data, (m) => self.postMessage(m, [m.positions.buffer]));
    } catch (err) {
      self.postMessage({ id: event.data && event.data.id, type: 'error', message: String((err && err.message) || err) });
    }
  };
} else {
  // Loaded via <script>: expose the stepwise layout for the main-thread fallback.
  self.KnowledgeGraphLayout = { layoutSteps, runLayout };
}
