/**
 * Knowledge-Graph-Renderer im 3D-Flugmodus (WebGL).
 *
 * - Zeitachsen-Projektion: Neueste Dokumente vorn (z = 0), älteste hinten (z = -12000),
 *   Dokumente ohne Zeitstempel im vorderen Orbit (z = +600).
 * - Clusterung nach Confluence-Spaces und Jira-Projekten.
 * - GPU-Batching für bis zu 100.000 Knoten in einem einzigen Draw-Call.
 * - Confluence blaue Kreise, Jira orange abgerundete Rechtecke, Status-Ringe.
 * - 6-DOF Flugsteuerung (W/S/A/D/Q/E, Maus-Look, Wheel-Dolly).
 * - Bounded DOM/2D-Overlay für Cluster-Namen, Zeitleiste und Tooltips (kein innerHTML).
 * - Kein Endlos-RAF im Leerlauf, dt-Clamp, DPR-Deckelung für schwächere Hardware.
 * - Graceful WebGL-Context-Lost-Handling, wirft keine Fehler in die Host-App.
 * Vanilla JS, keine externen Abhängigkeiten, kein Build-Tool.
 */
(function (global) {
  'use strict';

  // ------------------------------------------------------------------ constants
  const LABEL_RADIUS = 900;
  const MAX_NODE_LABELS = 8;
  const MAX_NODE_LABEL_POOL = 10;
  const MAX_YEAR_LABEL_POOL = 20;
  const DE_MONTHS = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];

  // ------------------------------------------------------------------ helpers
  const safeRAF = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (cb) => setTimeout(() => cb(typeof performance !== 'undefined' ? performance.now() : Date.now()), 16);
  const safeCAF = typeof cancelAnimationFrame === 'function' ? cancelAnimationFrame : (id) => clearTimeout(id);

  function cssVar(name, fallback) {
    if (typeof document === 'undefined' || !document.documentElement) return fallback;
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (_) {
      return fallback;
    }
  }

  function parseColor(str, fallback) {
    if (!str || typeof str !== 'string') return fallback;
    const s = str.trim();
    if (s.startsWith('#')) {
      let hex = s.slice(1);
      if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
      const num = parseInt(hex, 16);
      if (isNaN(num)) return fallback;
      return [(num >> 16 & 255) / 255, (num >> 8 & 255) / 255, (num & 255) / 255];
    }
    const m = s.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (m) {
      return [parseInt(m[1], 10) / 255, parseInt(m[2], 10) / 255, parseInt(m[3], 10) / 255];
    }
    return fallback;
  }

  function clamp(val, min, max) {
    return Math.max(min, Math.min(max, val));
  }

  // ------------------------------------------------------------------ math 4x4

  function mat4Create() {
    return new Float32Array([
      1, 0, 0, 0,
      0, 1, 0, 0,
      0, 0, 1, 0,
      0, 0, 0, 1,
    ]);
  }

  function mat4Perspective(out, fovYRad, aspect, near, far) {
    const f = 1.0 / Math.tan(fovYRad / 2);
    const nf = 1.0 / (near - far);
    out[0] = f / aspect;
    out[1] = 0;
    out[2] = 0;
    out[3] = 0;
    out[4] = 0;
    out[5] = f;
    out[6] = 0;
    out[7] = 0;
    out[8] = 0;
    out[9] = 0;
    out[10] = (far + near) * nf;
    out[11] = -1;
    out[12] = 0;
    out[13] = 0;
    out[14] = (2 * far * near) * nf;
    out[15] = 0;
    return out;
  }

  function mat4CameraView(out, eyeX, eyeY, eyeZ, yaw, pitch) {
    const cy = Math.cos(yaw);
    const sy = Math.sin(yaw);
    const cp = Math.cos(pitch);
    const sp = Math.sin(pitch);

    // Forward vector (towards -Z at yaw=0, pitch=0)
    const fx = sy * cp;
    const fy = sp;
    const fz = -cy * cp;

    // Right vector (world up is Y)
    const rx = cy;
    const ry = 0;
    const rz = sy;

    // Up vector = cross(right, forward)
    const ux = -rz * fy;
    const uy = rz * fx - rx * fz;
    const uz = rx * fy;

    out[0] = rx;  out[1] = ux;  out[2] = -fx; out[3] = 0;
    out[4] = ry;  out[5] = uy;  out[6] = -fy; out[7] = 0;
    out[8] = rz;  out[9] = uz;  out[10] = -fz; out[11] = 0;
    out[12] = -(rx * eyeX + ry * eyeY + rz * eyeZ);
    out[13] = -(ux * eyeX + uy * eyeY + uz * eyeZ);
    out[14] = -(-fx * eyeX + -fy * eyeY + -fz * eyeZ);
    out[15] = 1;
    return out;
  }

  function mat4Multiply(out, a, b) {
    for (let i = 0; i < 4; i++) {
      const ai0 = a[i], ai1 = a[i + 4], ai2 = a[i + 8], ai3 = a[i + 12];
      out[i]      = ai0 * b[0]  + ai1 * b[1]  + ai2 * b[2]  + ai3 * b[3];
      out[i + 4]  = ai0 * b[4]  + ai1 * b[5]  + ai2 * b[6]  + ai3 * b[7];
      out[i + 8]  = ai0 * b[8]  + ai1 * b[9]  + ai2 * b[10] + ai3 * b[11];
      out[i + 12] = ai0 * b[12] + ai1 * b[13] + ai2 * b[14] + ai3 * b[15];
    }
    return out;
  }

  // ------------------------------------------------------------------ fallback layout

  /**
   * Integrierter Standard-Layout-Builder, falls window.KnowledgeGraphFlightLayout
   * noch nicht geladen ist oder unabhängig ausgeführt wird.
   */
  function defaultFlightLayout(data, { jiraTimeField = 'updated' } = {}) {
    const nodes = data?.nodes || [];
    const n = nodes.length;
    const positions = new Float32Array(n * 3);
    const clusterIndices = new Uint32Array(n);
    const timestamps = new Float64Array(n);

    // 1. Cluster nach Confluence Space / Jira Projekt
    const clusterMap = new Map();
    for (let i = 0; i < n; i++) {
      const node = nodes[i];
      let key = 'other';
      let label = 'Dokumente';
      if (node.type === 'confluence') {
        const space = node.space_key || node.space || (node.uid ? node.uid.split(':')[1] : '') || 'Confluence';
        key = 'conf:' + space;
        label = space;
      } else if (node.type === 'jira') {
        const proj = node.project_key || node.project || (node.uid ? node.uid.split(':')[1]?.split('-')[0] : '') || 'Jira';
        key = 'jira:' + proj;
        label = proj;
      }
      if (!clusterMap.has(key)) clusterMap.set(key, { id: key, label, members: [] });
      clusterMap.get(key).members.push(i);
    }

    // 2. Zeitstempel extrahieren
    let minTime = Infinity;
    let maxTime = -Infinity;
    let unknownCount = 0;

    for (let i = 0; i < n; i++) {
      const node = nodes[i];
      let timeStr = null;
      if (node.type === 'jira') {
        timeStr = jiraTimeField === 'created' ? (node.created || node.created_at) : (node.updated || node.updated_at || node.created);
      } else {
        timeStr = node.updated || node.last_modified || node.modified || node.created;
      }
      let t = NaN;
      if (timeStr) {
        const parsed = Date.parse(timeStr);
        if (!isNaN(parsed) && parsed > 0) {
          t = parsed;
          if (t < minTime) minTime = t;
          if (t > maxTime) maxTime = t;
        }
      }
      if (isNaN(t)) unknownCount++;
      timestamps[i] = t;
    }

    const validTime = isFinite(minTime) && isFinite(maxTime) && maxTime > minTime;
    const timeSpan = validTime ? maxTime - minTime : 1;

    // 3. Cluster zylindrisch anordnen
    const clusterList = [];
    let cIdx = 0;
    const totalClusters = clusterMap.size || 1;
    const ringRadius = Math.max(900, Math.sqrt(totalClusters) * 550);

    clusterMap.forEach((c) => {
      const angle = (cIdx / totalClusters) * Math.PI * 2;
      const cx = Math.cos(angle) * ringRadius;
      const cy = Math.sin(angle) * ringRadius;
      const count = c.members.length;
      const radius = Math.max(160, Math.sqrt(count) * 40);

      const clusterObj = {
        id: c.id,
        label: c.label,
        x: cx,
        y: cy,
        radius,
        count,
      };
      clusterList.push(clusterObj);

      for (let m = 0; m < count; m++) {
        const nodeIdx = c.members[m];
        clusterIndices[nodeIdx] = cIdx;

        // Spiralverteilung im Cluster-Querschnitt
        const r = Math.sqrt((m + 0.5) / count) * radius;
        const theta = m * 2.399963229728653; // Goldener Schnitt
        const x = cx + Math.cos(theta) * r;
        const y = cy + Math.sin(theta) * r;

        // Z-Koordinate: Neueste z=0, Älteste z=-12000, Unbekannt z=+600
        const t = timestamps[nodeIdx];
        let z = 0;
        if (isNaN(t)) {
          z = 600.0;
        } else if (validTime) {
          const ratio = (t - minTime) / timeSpan; // 0=alt .. 1=neu
          z = -12000.0 * (1.0 - ratio);
          if (z === 0) z = 0;
        }

        positions[nodeIdx * 3] = x;
        positions[nodeIdx * 3 + 1] = y;
        positions[nodeIdx * 3 + 2] = z;
      }
      cIdx++;
    });

    // Disambiguate colliding cluster labels (e.g. Confluence space vs Jira project)
    const labelCounts = new Map();
    for (const c of clusterList) {
      labelCounts.set(c.label, (labelCounts.get(c.label) || 0) + 1);
    }
    for (const c of clusterList) {
      if (labelCounts.get(c.label) > 1) {
        if (c.id.startsWith("conf:") || c.id.startsWith("space:")) {
          c.label = `${c.label} (Space)`;
        } else if (c.id.startsWith("jira:")) {
          c.label = `${c.label} (Projekt)`;
        } else if (c.id.startsWith("other")) {
          c.label = `${c.label} (Sonstige)`;
        }
      }
    }

    return {
      positions,
      clusters: clusterList,
      clusterIndices,
      timestamps,
      minTime: isFinite(minTime) ? minTime : null,
      maxTime: isFinite(maxTime) ? maxTime : null,
      unknownCount,
      depth: 12000,
    };
  }

  function formatClusterBadgeText(c) {
    if (!c) return "";
    const m = c.label ? c.label.match(/^(.+) \((Space|Projekt|Sonstige)\)$/) : null;
    return m ? `${m[1]} (${m[2]} · ${c.count})` : `${c.label} (${c.count})`;
  }

  function downsampleTicks(ticks, maxCount) {
    if (ticks.length <= maxCount) return ticks;
    const result = [];
    const n = ticks.length;
    for (let k = 0; k < maxCount; k++) {
      const idx = Math.round(k * (n - 1) / (maxCount - 1));
      result.push(ticks[idx]);
    }
    return result;
  }

  function computeTimelineTicks(minTime, maxTime, sceneDepth) {
    if (minTime == null || maxTime == null || !isFinite(minTime) || !isFinite(maxTime) || maxTime <= minTime) {
      return [];
    }
    const depth = sceneDepth || 12000;
    const timeSpan = maxTime - minTime;
    const isMultiYear = timeSpan >= 365 * 24 * 3600 * 1000;

    const steps = isMultiYear ? [12, 24, 36, 60, 120, 240, 600] : [1, 2, 3, 6];

    function buildCandidateTicks(step) {
      const res = [];
      const dMin = new Date(minTime);
      if (step < 12) {
        let y = dMin.getUTCFullYear();
        let m = Math.floor(dMin.getUTCMonth() / step) * step;
        while (true) {
          const t = Date.UTC(y, m, 1);
          if (t > maxTime) break;
          if (t >= minTime) {
            res.push({ time: t, text: `${DE_MONTHS[m]} ${y}` });
          }
          m += step;
          while (m >= 12) {
            m -= 12;
            y++;
          }
        }
      } else {
        const stepYears = step / 12;
        let y = Math.floor(dMin.getUTCFullYear() / stepYears) * stepYears;
        while (true) {
          const t = Date.UTC(y, 0, 1);
          if (t > maxTime) break;
          if (t >= minTime) {
            res.push({ time: t, text: String(y) });
          }
          y += stepYears;
        }
      }

      const dMax = new Date(maxTime);
      const newestLabel = step < 12
        ? `${DE_MONTHS[dMax.getUTCMonth()]} ${dMax.getUTCFullYear()}`
        : String(dMax.getUTCFullYear());
      const oldestLabel = step < 12
        ? `${DE_MONTHS[dMin.getUTCMonth()]} ${dMin.getUTCFullYear()}`
        : String(dMin.getUTCFullYear());

      if (!res.some(t => t.text === newestLabel)) {
        res.push({ time: maxTime, text: newestLabel });
      }
      if (!res.some(t => t.text === oldestLabel)) {
        res.unshift({ time: minTime, text: oldestLabel });
      }
      return res;
    }

    let chosenTicks = [];
    for (let i = 0; i < steps.length; i++) {
      const cand = buildCandidateTicks(steps[i]);
      if (cand.length <= 16) {
        chosenTicks = cand;
        break;
      }
    }
    if (!chosenTicks.length) {
      chosenTicks = buildCandidateTicks(steps[steps.length - 1]);
      if (chosenTicks.length > 16) {
        chosenTicks = downsampleTicks(chosenTicks, 16);
      }
    }

    if (chosenTicks.length < 2 && minTime !== maxTime) {
      const dMin = new Date(minTime);
      const dMax = new Date(maxTime);
      chosenTicks = [
        { time: minTime, text: `${dMin.getUTCDate()}. ${DE_MONTHS[dMin.getUTCMonth()]} ${dMin.getUTCFullYear()}` },
        { time: maxTime, text: `${dMax.getUTCDate()}. ${DE_MONTHS[dMax.getUTCMonth()]} ${dMax.getUTCFullYear()}` },
      ];
    }

    for (let i = 0; i < chosenTicks.length; i++) {
      const tick = chosenTicks[i];
      tick.z = (-((maxTime - tick.time) / timeSpan) * depth) || 0;
    }

    chosenTicks.sort((a, b) => b.z - a.z);
    return chosenTicks;
  }

  // ------------------------------------------------------------------ shaders

  const NODE_VS = `
    attribute vec3 a_position;
    attribute float a_type;       // 0: Confluence, 1: Jira
    attribute float a_state;      // 0: ok, 1: error, 2: ref
    attribute float a_radius;
    attribute float a_importance; // 0..1
    attribute float a_selected;   // -1: dimmed, 0: normal, 1: neighbor, 2: selected

    uniform mat4 u_viewProjection;
    uniform vec2 u_viewport;
    uniform float u_importanceMode;
    uniform float u_dpr;

    varying float v_type;
    varying float v_state;
    varying float v_selected;
    varying float v_size;

    void main() {
      vec4 clipPos = u_viewProjection * vec4(a_position, 1.0);
      gl_Position = clipPos;

      float r = a_radius;
      if (u_importanceMode > 0.5) {
        r = mix(7.0, 35.0, a_importance);
      }
      if (a_selected > 1.5) {
        r += 5.0;
      } else if (a_selected > 0.5) {
        r += 2.0;
      }

      if (clipPos.w > 0.1) {
        // Perspektivische Punktgröße: Durchmesser skaliert mit Distanz
        float sizePx = (r * 2.2 * u_viewport.y) / (clipPos.w * 1.732);
        sizePx = clamp(sizePx * u_dpr, 3.0 * u_dpr, 110.0 * u_dpr);
        gl_PointSize = sizePx;
        v_size = sizePx;
      } else {
        gl_PointSize = 0.0;
        v_size = 0.0;
      }

      v_type = a_type;
      v_state = a_state;
      v_selected = a_selected;
    }
  `;

  const NODE_FS = `
    precision mediump float;

    uniform vec3 u_colorConfluence;
    uniform vec3 u_colorJira;
    uniform vec3 u_colorDanger;
    uniform vec3 u_colorMuted;
    uniform vec3 u_colorSurface;
    uniform vec3 u_colorFocus;
    uniform float u_hasSelection;

    varying float v_type;
    varying float v_state;
    varying float v_selected;
    varying float v_size;

    void main() {
      if (v_size < 1.0) discard;

      vec2 p = gl_PointCoord * 2.0 - 1.0;
      float dist = 0.0;
      float aa = 2.0 / v_size;

      if (v_type < 0.5) {
        // Confluence: Kreis
        dist = length(p);
      } else {
        // Jira: Abgerundetes Quadrat
        float c = 0.35;
        vec2 q = abs(p) - (1.0 - c);
        dist = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - (c - 1.0);
      }

      if (dist > 1.0) discard;
      float shapeAlpha = 1.0 - smoothstep(1.0 - aa, 1.0, dist);

      // Randbreite
      float borderWidth = 4.0 / v_size;
      float borderInner = 1.0 - borderWidth;
      float isBorder = smoothstep(borderInner - aa, borderInner, dist);

      vec3 brandColor = (v_type < 0.5) ? u_colorConfluence : u_colorJira;
      vec3 fillColor = brandColor;
      vec3 strokeColor = u_colorSurface;

      if (v_state > 0.5 && v_state < 1.5) {
        fillColor = u_colorSurface;
        strokeColor = u_colorDanger;
      } else if (v_state >= 1.5) {
        fillColor = u_colorSurface;
        strokeColor = u_colorMuted;
      }

      vec3 rgb = mix(fillColor, strokeColor, isBorder);

      if (v_selected > 1.5) {
        if (dist > 1.0 - borderWidth * 1.6) {
          rgb = u_colorFocus;
        }
      }

      float alpha = shapeAlpha;
      if (u_hasSelection > 0.5) {
        if (v_selected < -0.5) {
          alpha *= 0.15;
        }
      }

      gl_FragColor = vec4(rgb, alpha);
    }
  `;

  const LINE_VS = `
    attribute vec3 a_position;
    attribute vec4 a_color;
    uniform mat4 u_viewProjection;
    varying vec4 v_color;

    void main() {
      gl_Position = u_viewProjection * vec4(a_position, 1.0);
      v_color = a_color;
    }
  `;

  const LINE_FS = `
    precision mediump float;
    varying vec4 v_color;

    void main() {
      gl_FragColor = v_color;
    }
  `;

  function createProgram(gl, vsSource, fsSource) {
    function compile(type, src) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        const info = gl.getShaderInfoLog(s);
        gl.deleteShader(s);
        throw new Error('Shader-Kompilierungsfehler: ' + info);
      }
      return s;
    }
    const vs = compile(gl.VERTEX_SHADER, vsSource);
    const fs = compile(gl.FRAGMENT_SHADER, fsSource);
    const p = gl.createProgram();
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      const info = gl.getProgramInfoLog(p);
      gl.deleteProgram(p);
      throw new Error('Programm-Link-Fehler: ' + info);
    }
    return p;
  }

  // ------------------------------------------------------------------ class

  class KnowledgeGraphFlight {
    /**
     * @param {Object} options
     * @param {HTMLCanvasElement} options.canvasEl
     * @param {HTMLElement} options.containerEl
     * @param {Function} [options.onNodeSelect]   (node) => void
     * @param {Function} [options.onNodeActivate] (node) => void
     * @param {Function} [options.onEdgeSelect]   (edge) => void
     * @param {Function} [options.onStatus]       ({ loading, message, timeLabel, unknownCount }) => void
     */
    constructor({ canvasEl, containerEl, onNodeSelect, onNodeActivate, onEdgeSelect, onStatus } = {}) {
      this.canvas = canvasEl;
      this.container = containerEl || canvasEl?.parentElement;
      this.onNodeSelect = typeof onNodeSelect === 'function' ? onNodeSelect : () => {};
      this.onNodeActivate = typeof onNodeActivate === 'function' ? onNodeActivate : () => {};
      this.onEdgeSelect = typeof onEdgeSelect === 'function' ? onEdgeSelect : () => {};
      this.onStatus = typeof onStatus === 'function' ? onStatus : () => {};

      this.supported = false;
      this.active = false;
      this.importanceMode = false;
      this.timelineAxis = false;
      this.jiraTimeField = 'updated';

      this.timelineTicks = [];
      this.timelineLineBuffer = null;
      this.timelineLineCount = 0;
      this.timelineAxisX = 0;
      this.timelineAxisY = 0;
      this.yearLabelPool = [];

      this.nodeMap = new Map();
      this.edgeMap = new Map();
      this.nodes = [];
      this.edges = [];
      this.clusters = [];
      this.positions = null;
      this.timestamps = null;
      this.clusterIndices = null;
      this.minTime = null;
      this.maxTime = null;
      this.unknownCount = 0;
      this.sceneDepth = 12000;

      this.dataId = 0;
      this.selectedUid = null;
      this.selectedEdgeId = null;
      this.highlightSet = new Set();
      this.hoverNode = null;

      // Kamera & Steuerung
      this.cameraPos = [0, 0, 1000];
      this.yaw = 0;
      this.pitch = 0;
      this.velocity = [0, 0, 0];
      this.keys = Object.create(null);
      this.pointerDragging = false;
      this.dragStartX = 0;
      this.dragStartY = 0;
      this.lastPointerX = 0;
      this.lastPointerY = 0;
      this.pointerMoved = false;

      // Bounds & Far-Plane
      this.sceneBounds = null;
      this.farPlane = 35000.0;
      this._cachedColors = null;

      // Matrizen
      this.projMatrix = mat4Create();
      this.viewMatrix = mat4Create();
      this.viewProjectionMatrix = mat4Create();

      // Render-Zustand
      this.width = 0;
      this.height = 0;
      this.dpr = 1;
      this._rafId = null;
      this._lastTime = 0;
      this._needRedraw = false;
      this.contextLost = false;

      // Proximity Node Label State
      this.nodeLabelPool = [];
      this._nodeSpatialGrid = null;
      this._lastLabelCamPos = [Infinity, Infinity, Infinity];
      this._lastLabelYaw = Infinity;
      this._lastLabelPitch = Infinity;
      this._lastLabelSelectedUid = undefined;
      this._lastLabelDataId = -1;
      this._loading = false;

      // WebGL Testen (ohne Event-Bindung vor setActive(true))
      if (this.canvas && typeof this.canvas.getContext === 'function') {
        try {
          const gl = this.canvas.getContext('webgl2') ||
                     this.canvas.getContext('webgl') ||
                     this.canvas.getContext('experimental-webgl');
          if (gl) {
            this.gl = gl;
            this.supported = true;
          }
        } catch (_) {
          this.supported = false;
        }
      }

      if (!this.supported) {
        this.onStatus({ loading: false, message: 'WebGL nicht verfügbar' });
        return;
      }

      // Bound Event-Handler für sauberes Add/Remove
      this._onKeyDown = this._handleKeyDown.bind(this);
      this._onKeyUp = this._handleKeyUp.bind(this);
      this._onPointerDown = this._handlePointerDown.bind(this);
      this._onPointerMove = this._handlePointerMove.bind(this);
      this._onPointerUp = this._handlePointerUp.bind(this);
      this._onPointerCancel = this._handlePointerCancel.bind(this);
      this._onWheel = this._handleWheel.bind(this);
      this._onDblClick = this._handleDblClick.bind(this);
      this._onResize = this._handleResize.bind(this);
      this._onBlur = this._stopMovement.bind(this);
      this._onVisibilityChange = () => { if (document.hidden) this._stopMovement(); };
      this._onContextLost = this._handleContextLost.bind(this);
      this._onContextRestored = this._handleContextRestored.bind(this);
      this._tick = this._step.bind(this);

      this.canvas.addEventListener('webglcontextlost', this._onContextLost);
      this.canvas.addEventListener('webglcontextrestored', this._onContextRestored);

      this._initGL();
      this._initOverlay();
    }

    // ---------------------------------------------------------------- WebGL Init

    _initGL() {
      const gl = this.gl;
      if (!gl) return;
      try {
        this.nodeProgram = createProgram(gl, NODE_VS, NODE_FS);
        this.lineProgram = createProgram(gl, LINE_VS, LINE_FS);

        // Uniform Locations
        this.nodeUniforms = {
          u_viewProjection: gl.getUniformLocation(this.nodeProgram, 'u_viewProjection'),
          u_viewport: gl.getUniformLocation(this.nodeProgram, 'u_viewport'),
          u_importanceMode: gl.getUniformLocation(this.nodeProgram, 'u_importanceMode'),
          u_dpr: gl.getUniformLocation(this.nodeProgram, 'u_dpr'),
          u_colorConfluence: gl.getUniformLocation(this.nodeProgram, 'u_colorConfluence'),
          u_colorJira: gl.getUniformLocation(this.nodeProgram, 'u_colorJira'),
          u_colorDanger: gl.getUniformLocation(this.nodeProgram, 'u_colorDanger'),
          u_colorMuted: gl.getUniformLocation(this.nodeProgram, 'u_colorMuted'),
          u_colorSurface: gl.getUniformLocation(this.nodeProgram, 'u_colorSurface'),
          u_colorFocus: gl.getUniformLocation(this.nodeProgram, 'u_colorFocus'),
          u_hasSelection: gl.getUniformLocation(this.nodeProgram, 'u_hasSelection'),
        };

        this.lineUniforms = {
          u_viewProjection: gl.getUniformLocation(this.lineProgram, 'u_viewProjection'),
        };

        // Buffer erstellen
        this.nodePosBuffer = gl.createBuffer();
        this.nodeAttrBuffer = gl.createBuffer();
        this.nodeSelBuffer = gl.createBuffer();

        this.sparseLineBuffer = gl.createBuffer();
        this.activeLineBuffer = gl.createBuffer();
        this.timelineLineBuffer = gl.createBuffer();
        this.activeLineCount = 0;
        this.sparseLineCount = 0;
        this.timelineLineCount = 0;
        this._updateCachedColors();
      } catch (err) {
        console.error('WebGL Initialisierung fehlgeschlagen:', err);
        this.supported = false;
        this.onStatus({ loading: false, message: 'WebGL-Shaderfehler' });
      }
    }

    _handleContextLost(ev) {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      this.contextLost = true;
      this._cancelRAF();
      this.onStatus({ loading: false, message: 'WebGL-Kontext verloren' });
    }

    _handleContextRestored() {
      this.contextLost = false;
      try {
        this._initGL();
        this._uploadNodeBuffers();
        this._uploadEdgeBuffers();
        this._buildTimelineAxis();
        this.requestDraw();
        this.onStatus({ loading: false, message: '' });
      } catch (err) {
        console.error('WebGL Restore fehlgeschlagen:', err);
      }
    }

    // ---------------------------------------------------------------- DOM Overlay

    _updateCachedColors() {
      this._cachedColors = {
        confCol: parseColor(cssVar('--confluence-blue', '#1868db'), [0.09, 0.41, 0.86]),
        jiraCol: parseColor(cssVar('--jira-orange', '#e2740d'), [0.89, 0.45, 0.05]),
        dangerCol: parseColor(cssVar('--status-danger', '#de350b'), [0.87, 0.21, 0.04]),
        mutedCol: parseColor(cssVar('--text-muted', '#7a869a'), [0.48, 0.53, 0.60]),
        surfCol: parseColor(cssVar('--bg-surface', '#ffffff'), [1.0, 1.0, 1.0]),
        focusCol: parseColor(cssVar('--border-focus', '#0052cc'), [0.0, 0.32, 0.80]),
        confStr: cssVar('--confluence-blue', '#1868db'),
        jiraStr: cssVar('--jira-orange', '#e2740d'),
      };
      if (this.nodeLabelPool) {
        const bg = cssVar('--bg-surface', 'rgba(255, 255, 255, 0.95)');
        const fg = cssVar('--text-main', '#172b4d');
        for (const el of this.nodeLabelPool) {
          el.style.background = bg;
          el.style.color = fg;
        }
      }
    }

    _initOverlay() {
      if (!this.container || typeof document === 'undefined') return;

      let overlay = this.container.querySelector('.graph-flight-overlay');
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.className = 'graph-flight-overlay';
        overlay.style.position = 'absolute';
        overlay.style.inset = '0';
        overlay.style.pointerEvents = 'none';
        overlay.style.overflow = 'hidden';
        overlay.style.zIndex = '5';
        this.container.appendChild(overlay);
      }
      this.overlayEl = overlay;

      // Timeline HUD
      let hud = overlay.querySelector('.graph-flight-timeline-hud');
      if (!hud) {
        hud = document.createElement('div');
        hud.className = 'graph-flight-timeline-hud';
        hud.style.position = 'absolute';
        hud.style.bottom = '12px';
        hud.style.left = '50%';
        hud.style.transform = 'translateX(-50%)';
        hud.style.background = 'rgba(23, 43, 77, 0.85)';
        hud.style.color = '#ffffff';
        hud.style.padding = '4px 14px';
        hud.style.borderRadius = '16px';
        hud.style.fontSize = '12px';
        hud.style.fontFamily = cssVar('--font-sans', 'sans-serif');
        hud.style.pointerEvents = 'none';
        hud.style.boxShadow = '0 2px 8px rgba(0,0,0,0.2)';
        hud.style.whiteSpace = 'nowrap';
        hud.style.display = 'none';
        overlay.appendChild(hud);
      }
      this.timelineHudEl = hud;

      // Tooltip
      let tip = overlay.querySelector('.graph-flight-tooltip');
      if (!tip) {
        tip = document.createElement('div');
        tip.className = 'graph-flight-tooltip';
        tip.style.position = 'absolute';
        tip.style.display = 'none';
        tip.style.pointerEvents = 'none';
        tip.style.background = cssVar('--bg-surface', '#ffffff');
        tip.style.color = cssVar('--text-main', '#172b4d');
        tip.style.border = '1px solid ' + cssVar('--border-muted', '#dfe1e6');
        tip.style.borderRadius = '4px';
        tip.style.padding = '6px 10px';
        tip.style.fontSize = '12px';
        tip.style.boxShadow = '0 4px 12px rgba(0,0,0,0.15)';
        tip.style.maxWidth = '300px';
        overlay.appendChild(tip);
      }
      this.tooltipEl = tip;

      // Cluster Label Pool (maximal 35 Elemente)
      this.clusterLabelPool = [];
      const MAX_CLUSTER_LABELS = 35;
      for (let i = 0; i < MAX_CLUSTER_LABELS; i++) {
        const el = document.createElement('div');
        el.className = 'graph-flight-cluster-badge';
        el.style.position = 'absolute';
        el.style.display = 'none';
        el.style.padding = '3px 8px';
        el.style.borderRadius = '12px';
        el.style.fontSize = '11px';
        el.style.fontWeight = '600';
        el.style.fontFamily = cssVar('--font-sans', 'sans-serif');
        el.style.background = 'rgba(255, 255, 255, 0.9)';
        el.style.color = cssVar('--text-main', '#172b4d');
        el.style.border = '1px solid ' + cssVar('--border-focus', '#0052cc');
        el.style.boxShadow = '0 2px 6px rgba(0,0,0,0.12)';
        el.style.cursor = 'pointer';
        el.style.pointerEvents = 'auto';
        el.style.transform = 'translate(-50%, -50%)';
        el.style.userSelect = 'none';
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          const c = el._cluster;
          if (c) {
            this.cameraPos[0] = c.x;
            this.cameraPos[1] = c.y;
            this.cameraPos[2] = 900;
            this.yaw = 0;
            this.pitch = 0;
            this.requestDraw();
          }
        });
        overlay.appendChild(el);
        this.clusterLabelPool.push(el);
      }

      // Node Proximity Label Pool (maximal 10 Elemente)
      this.nodeLabelPool = [];
      for (let i = 0; i < MAX_NODE_LABEL_POOL; i++) {
        const el = document.createElement('div');
        el.className = 'graph-flight-node-label';
        el.style.position = 'absolute';
        el.style.display = 'none';
        el.style.pointerEvents = 'none';
        if (typeof el.setAttribute === 'function') el.setAttribute('aria-hidden', 'true');
        el.style.padding = '2px 8px 2px 7px';
        el.style.borderRadius = '10px';
        el.style.fontSize = '11px';
        el.style.fontWeight = '500';
        el.style.fontFamily = cssVar('--font-sans', 'sans-serif');
        el.style.background = cssVar('--bg-surface', 'rgba(255, 255, 255, 0.95)');
        el.style.color = cssVar('--text-main', '#172b4d');
        el.style.border = '1px solid ' + cssVar('--border-muted', '#dfe1e6');
        el.style.borderLeft = '3px solid ' + cssVar('--border-focus', '#0052cc');
        el.style.boxShadow = '0 1px 4px rgba(0,0,0,0.12)';
        el.style.maxWidth = '220px';
        el.style.whiteSpace = 'nowrap';
        el.style.overflow = 'hidden';
        el.style.textOverflow = 'ellipsis';
        el.style.userSelect = 'none';
        el.style.boxSizing = 'border-box';
        el.style.transform = 'translateY(-50%)';
        el.style.zIndex = '6';
        overlay.appendChild(el);
        this.nodeLabelPool.push(el);
      }

      // Timeline Year Label Pool (maximal 20 Elemente)
      this.yearLabelPool = [];
      for (let i = 0; i < MAX_YEAR_LABEL_POOL; i++) {
        const el = document.createElement('div');
        el.className = 'graph-flight-year-label';
        el.style.position = 'absolute';
        el.style.display = 'none';
        el.style.pointerEvents = 'none';
        if (typeof el.setAttribute === 'function') el.setAttribute('aria-hidden', 'true');
        el.style.padding = '2px 8px';
        el.style.borderRadius = '10px';
        el.style.fontSize = '11px';
        el.style.fontWeight = '600';
        el.style.fontFamily = cssVar('--font-sans', 'sans-serif');
        el.style.background = 'rgba(23, 43, 77, 0.85)';
        el.style.color = '#ffffff';
        el.style.boxShadow = '0 2px 6px rgba(0,0,0,0.2)';
        el.style.whiteSpace = 'nowrap';
        el.style.userSelect = 'none';
        el.style.transform = 'translate(-50%, -50%)';
        el.style.zIndex = '7';
        overlay.appendChild(el);
        this.yearLabelPool.push(el);
      }
    }

    // ---------------------------------------------------------------- Lifecycle

    setActive(bool) {
      const next = Boolean(bool);
      if (this.active === next) return;
      if (next && !this.supported) return;
      this.active = next;

      if (this.active) {
        this._updateCachedColors();
        this._bindEvents();
        this._handleResize();
        if (this.nodes.length && this.cameraPos[2] === 1000 && this.cameraPos[0] === 0 && this.cameraPos[1] === 0) {
          this.fitToView();
        }
        this.requestDraw();
      } else {
        this._unbindEvents();
        this._stopMovement();
        this._cancelRAF();
        if (this.tooltipEl) this.tooltipEl.style.display = 'none';
        if (this.timelineHudEl) this.timelineHudEl.style.display = 'none';
        for (const badge of this.clusterLabelPool || []) badge.style.display = 'none';
        this._hideAllNodeLabels();
        this._hideAllYearLabels();
        this._lastLabelCamPos = [Infinity, Infinity, Infinity];
      }
    }

    setJiraTimeField(field) {
      const next = field === 'created' ? 'created' : 'updated';
      if (this.jiraTimeField === next) return;
      this.jiraTimeField = next;
      if (this.nodes.length) {
        this._runLayoutAndUpload();
        this._updateOverlay();
        this.requestDraw();
      }
    }

    setImportanceMode(bool) {
      this.importanceMode = Boolean(bool);
      this.requestDraw();
    }

    setTimelineAxis(enabled) {
      this.timelineAxis = Boolean(enabled);
      if (!this.timelineAxis) {
        this._hideAllYearLabels();
      } else {
        if (!this.timelineTicks || !this.timelineTicks.length) {
          this._buildTimelineAxis();
        }
      }
      this._updateOverlay(true);
      this.requestDraw();
    }

    _hideAllYearLabels() {
      if (!this.yearLabelPool) return;
      for (let i = 0; i < this.yearLabelPool.length; i++) {
        const el = this.yearLabelPool[i];
        if (el.style.display !== 'none') {
          el.style.display = 'none';
          el._tick = null;
        }
      }
    }

    cancelPending() {
      ++this.dataId;
      this._loading = false;
      this._hideAllNodeLabels();
      this._hideAllYearLabels();
      this._nodeSpatialGrid = null;
      this._lastLabelDataId = -1;
      this._lastLabelCamPos = [Infinity, Infinity, Infinity];
    }

    destroy() {
      this.setActive(false);
      this.cancelPending();
      this._hideAllNodeLabels();
      this._hideAllYearLabels();
      this.nodeLabelPool = [];
      this.yearLabelPool = [];
      this.timelineTicks = [];
      this._nodeSpatialGrid = null;

      if (this.canvas) {
        this.canvas.removeEventListener('webglcontextlost', this._onContextLost);
        this.canvas.removeEventListener('webglcontextrestored', this._onContextRestored);
      }

      if (this.overlayEl && this.overlayEl.parentElement) {
        this.overlayEl.parentElement.removeChild(this.overlayEl);
      }

      const gl = this.gl;
      if (gl && !gl.isContextLost()) {
        try {
          if (this.nodeProgram) gl.deleteProgram(this.nodeProgram);
          if (this.lineProgram) gl.deleteProgram(this.lineProgram);
          if (this.nodePosBuffer) gl.deleteBuffer(this.nodePosBuffer);
          if (this.nodeAttrBuffer) gl.deleteBuffer(this.nodeAttrBuffer);
          if (this.nodeSelBuffer) gl.deleteBuffer(this.nodeSelBuffer);
          if (this.sparseLineBuffer) gl.deleteBuffer(this.sparseLineBuffer);
          if (this.activeLineBuffer) gl.deleteBuffer(this.activeLineBuffer);
          if (this.timelineLineBuffer) gl.deleteBuffer(this.timelineLineBuffer);
        } catch (_) {}
      }

      this.timelineLineBuffer = null;
      this.timelineLineCount = 0;
      this.nodeMap.clear();
      this.edgeMap.clear();
      this.nodes = [];
      this.edges = [];
      this.clusters = [];
    }

    // ---------------------------------------------------------------- Data

    async setData(data, centerUid) {
      const dataId = ++this.dataId;
      this._loading = true;
      this._hideAllNodeLabels();
      this._hideAllYearLabels();
      this.onStatus({ loading: true, message: '3D-Fluglayout wird berechnet …' });

      // Daten normalisieren
      const nodes = [];
      const edges = [];
      const nodeMap = new Map();
      const edgeMap = new Map();

      for (const n of data?.nodes || []) {
        const node = { ...n, idx: nodes.length, source_updated_at: n.source_updated_at ?? n.updated_at ?? n.updated, source_created_at: n.source_created_at ?? n.created_at ?? n.created, updated_at: n.updated_at ?? n.source_updated_at ?? n.updated, created_at: n.created_at ?? n.source_created_at ?? n.created };
        nodes.push(node);
        nodeMap.set(node.uid, node);
      }

      for (const e of data?.edges || []) {
        const source = nodeMap.get(e.source);
        const target = nodeMap.get(e.target);
        if (!source || !target) continue;
        const edge = { ...e, s: source, t: target, _id: String(e.id ?? `${e.source}->${e.target}:${e.kind}`) };
        edges.push(edge);
        edgeMap.set(edge._id, edge);
      }

      if (dataId !== this.dataId) return;

      this.nodes = nodes;
      this.edges = edges;
      this.nodeMap = nodeMap;
      this.edgeMap = edgeMap;
      this.centerUid = centerUid || data?.center || null;

      this.selectedUid = null;
      this.selectedEdgeId = null;
      this.highlightSet.clear();

      this._runLayoutAndUpload();

      if (this.centerUid && this.nodeMap.has(this.centerUid)) {
        this.selectNode(this.centerUid);
      }

      this._loading = false;

      if (this.active) {
        this.fitToView();
        this.requestDraw();
      }
    }

    _runLayoutAndUpload() {
      this._nodeSpatialGrid = null;
      this._lastLabelDataId = -1;
      this._lastLabelCamPos = [Infinity, Infinity, Infinity];
      if (!this.nodes.length) {
        this.positions = new Float32Array(0);
        this.clusters = [];
        this._nodeSpatialGrid = new Map();
        this._hideAllNodeLabels();
        this.onStatus({ loading: false, message: '' });
        return;
      }

      let layoutResult = null;
      const buildOptions = { jiraTimeField: this.jiraTimeField };

      const flightLayout = (typeof window !== 'undefined' && window.KnowledgeGraphFlightLayout) || (typeof global !== 'undefined' && global.KnowledgeGraphFlightLayout);
      if (flightLayout && typeof flightLayout.build === 'function') {
        try {
          layoutResult = flightLayout.build({
            nodes: this.nodes,
            edges: this.edges,
            center: this.centerUid,
          }, buildOptions);
        } catch (err) {
          console.warn('Fehler im KnowledgeGraphFlightLayout, nutze Standard-Fallback:', err);
          layoutResult = defaultFlightLayout({ nodes: this.nodes, edges: this.edges }, buildOptions);
        }
      } else {
        layoutResult = defaultFlightLayout({ nodes: this.nodes, edges: this.edges }, buildOptions);
      }

      this.positions = layoutResult.positions;
      this.clusters = layoutResult.clusters || [];
      // Disambiguate colliding cluster labels if layout did not already do so
      const labelCounts = new Map();
      for (const c of this.clusters) {
        labelCounts.set(c.label, (labelCounts.get(c.label) || 0) + 1);
      }
      for (const c of this.clusters) {
        if (labelCounts.get(c.label) > 1) {
          if (c.id.startsWith("space:") || c.id.startsWith("conf:")) {
            c.label = `${c.label} (Space)`;
          } else if (c.id.startsWith("jira:")) {
            c.label = `${c.label} (Projekt)`;
          } else if (c.id.startsWith("other")) {
            c.label = `${c.label} (Sonstige)`;
          }
        }
      }
      this.clusterIndices = layoutResult.clusterIndices;
      this.timestamps = layoutResult.timestamps;
      this.minTime = layoutResult.minTime;
      this.maxTime = layoutResult.maxTime;
      this.unknownCount = layoutResult.unknownCount || 0;
      this.sceneDepth = layoutResult.depth || 12000;

      this._computeSceneBounds();

      this._uploadNodeBuffers();
      this._uploadEdgeBuffers();
      this._buildNodeSpatialGrid();
      this._buildTimelineAxis();

      this.onStatus({
        loading: false,
        message: '',
        unknownCount: this.unknownCount,
        timeLabel: this._formatTimeLabel(this.cameraPos[2]),
      });
    }

    _uploadNodeBuffers() {
      const gl = this.gl;
      if (!gl || this.contextLost || !this.positions) return;

      const n = this.nodes.length;
      if (n === 0) return;

      // 1. Positionen
      gl.bindBuffer(gl.ARRAY_BUFFER, this.nodePosBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, this.positions, gl.STATIC_DRAW);

      // 2. Attribute: [type, state, radius, importance] pro Knoten
      const attr = new Float32Array(n * 4);
      let minImp = Infinity, maxImp = -Infinity;
      for (let i = 0; i < n; i++) {
        const imp = this.nodes[i].in_degree ?? 0;
        if (imp < minImp) minImp = imp;
        if (imp > maxImp) maxImp = imp;
      }
      const impSpan = maxImp > minImp ? maxImp - minImp : 1;

      for (let i = 0; i < n; i++) {
        const node = this.nodes[i];
        const isJira = node.type === 'jira' ? 1.0 : 0.0;
        let st = 0.0;
        if (node.state === 'missing' || node.state === 'forbidden' || node.state === 'error') st = 1.0;
        else if (node.state !== 'indexed') st = 2.0;

        const baseR = 7.0 + Math.min(13.0, Math.log2((node.degree || 0) + 1) * 2.2);
        const impRatio = ((node.in_degree ?? 0) - minImp) / impSpan;

        const off = i * 4;
        attr[off] = isJira;
        attr[off + 1] = st;
        attr[off + 2] = baseR;
        attr[off + 3] = impRatio;
      }

      gl.bindBuffer(gl.ARRAY_BUFFER, this.nodeAttrBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, attr, gl.STATIC_DRAW);

      // 3. Selektions-Buffer
      this._updateSelectionBuffer();
    }

    _updateSelectionBuffer() {
      const gl = this.gl;
      if (!gl || this.contextLost || !this.nodes.length) return;

      const n = this.nodes.length;
      const sel = new Float32Array(n);
      const hasSel = this.selectedUid !== null;

      if (hasSel) {
        for (let i = 0; i < n; i++) {
          const uid = this.nodes[i].uid;
          if (uid === this.selectedUid) {
            sel[i] = 2.0;
          } else if (this.highlightSet.has(uid)) {
            sel[i] = 1.0;
          } else {
            sel[i] = -1.0; // gedimmt
          }
        }
      }

      gl.bindBuffer(gl.ARRAY_BUFFER, this.nodeSelBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, sel, gl.DYNAMIC_DRAW);
    }

    _buildTimelineAxis() {
      const b = this.sceneBounds || this._computeSceneBounds();
      const minX = b ? b.minX : -500;
      const maxX = b ? b.maxX : 500;
      const offsetX = clamp((maxX - minX) * 0.03, 180, 600);
      const axisX = (b ? b.cx : 0) + offsetX;
      const axisY = b ? b.cy : 0;
      this.timelineAxisX = axisX;
      this.timelineAxisY = axisY;
      const depth = this.sceneDepth || 12000;
      const zStart = 400;
      const zEnd = -(depth + 400);

      const hasValidTime = this.minTime !== null && this.maxTime !== null &&
                           isFinite(this.minTime) && isFinite(this.maxTime) &&
                           this.maxTime > this.minTime;

      this.timelineTicks = hasValidTime
        ? computeTimelineTicks(this.minTime, this.maxTime, depth)
        : [];

      const gl = this.gl;
      if (!gl || this.contextLost) return;

      const ticks = this.timelineTicks;
      const tickCount = ticks.length;
      const totalVertices = 2 + tickCount * 2;
      const buffer = new Float32Array(totalVertices * 7);

      const lineR = 23 / 255, lineG = 43 / 255, lineB = 77 / 255, lineA = 0.6;
      const tickR = 0 / 255, tickG = 82 / 255, tickB = 204 / 255, tickA = 0.85;

      let offset = 0;
      // Main axis line
      buffer[offset++] = axisX; buffer[offset++] = axisY; buffer[offset++] = zStart;
      buffer[offset++] = lineR; buffer[offset++] = lineG; buffer[offset++] = lineB; buffer[offset++] = lineA;

      buffer[offset++] = axisX; buffer[offset++] = axisY; buffer[offset++] = zEnd;
      buffer[offset++] = lineR; buffer[offset++] = lineG; buffer[offset++] = lineB; buffer[offset++] = lineA;

      // Tick marks perpendicular to axis (+/- 64 in X)
      for (let i = 0; i < tickCount; i++) {
        const tz = ticks[i].z;
        buffer[offset++] = axisX - 64; buffer[offset++] = axisY; buffer[offset++] = tz;
        buffer[offset++] = tickR; buffer[offset++] = tickG; buffer[offset++] = tickB; buffer[offset++] = tickA;

        buffer[offset++] = axisX + 64; buffer[offset++] = axisY; buffer[offset++] = tz;
        buffer[offset++] = tickR; buffer[offset++] = tickG; buffer[offset++] = tickB; buffer[offset++] = tickA;
      }

      if (!this.timelineLineBuffer && typeof gl.createBuffer === 'function') {
        this.timelineLineBuffer = gl.createBuffer();
      }
      if (this.timelineLineBuffer) {
        gl.bindBuffer(gl.ARRAY_BUFFER, this.timelineLineBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, buffer, gl.STATIC_DRAW);
      }
      this.timelineLineCount = totalVertices;
    }

    _renderTimelineAxis() {
      const gl = this.gl;
      if (!this.lineProgram || !this.timelineLineCount || !this.timelineLineBuffer) return;

      gl.useProgram(this.lineProgram);
      gl.uniformMatrix4fv(this.lineUniforms.u_viewProjection, false, this.viewProjectionMatrix);

      const aPos = gl.getAttribLocation(this.lineProgram, 'a_position');
      const aCol = gl.getAttribLocation(this.lineProgram, 'a_color');
      gl.enableVertexAttribArray(aPos);
      gl.enableVertexAttribArray(aCol);

      gl.bindBuffer(gl.ARRAY_BUFFER, this.timelineLineBuffer);
      gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 7 * 4, 0);
      gl.vertexAttribPointer(aCol, 4, gl.FLOAT, false, 7 * 4, 3 * 4);
      gl.drawArrays(gl.LINES, 0, this.timelineLineCount);

      gl.disableVertexAttribArray(aPos);
      gl.disableVertexAttribArray(aCol);
    }

    _uploadEdgeBuffers() {
      const gl = this.gl;
      if (!gl || this.contextLost || !this.positions) return;

      // Statische sparse Edges: Repräsentative Kanten (z.B. Parent-Links), max 4000
      const lines = [];
      let count = 0;
      const maxSparse = 4000;
      const pos = this.positions;

      for (let i = 0; i < this.edges.length && count < maxSparse; i++) {
        const e = this.edges[i];
        if (e.kind === 'parent_of' || e.kind === 'jira_parent_of' || i % 15 === 0) {
          const sIdx = e.s.idx * 3;
          const tIdx = e.t.idx * 3;
          // Vertex 1: x, y, z, r, g, b, a
          lines.push(pos[sIdx], pos[sIdx + 1], pos[sIdx + 2], 0.48, 0.53, 0.60, 0.22);
          // Vertex 2
          lines.push(pos[tIdx], pos[tIdx + 1], pos[tIdx + 2], 0.48, 0.53, 0.60, 0.22);
          count++;
        }
      }

      this.sparseLineCount = count * 2;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.sparseLineBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(lines), gl.STATIC_DRAW);

      this._uploadActiveEdges();
    }

    _uploadActiveEdges() {
      const gl = this.gl;
      if (!gl || this.contextLost || !this.positions) return;

      if (!this.selectedUid && !this.selectedEdgeId) {
        this.activeLineCount = 0;
        return;
      }

      const active = [];
      const pos = this.positions;
      const focusColor = parseColor(cssVar('--border-focus', '#0052cc'), [0, 0.32, 0.8]);

      for (const e of this.edges) {
        const matchNode = this.selectedUid && (e.source === this.selectedUid || e.target === this.selectedUid);
        const matchEdge = this.selectedEdgeId && e._id === this.selectedEdgeId;
        if (matchNode || matchEdge) {
          const sIdx = e.s.idx * 3;
          const tIdx = e.t.idx * 3;
          active.push(pos[sIdx], pos[sIdx + 1], pos[sIdx + 2], focusColor[0], focusColor[1], focusColor[2], 0.95);
          active.push(pos[tIdx], pos[tIdx + 1], pos[tIdx + 2], focusColor[0], focusColor[1], focusColor[2], 0.95);
        }
      }

      this.activeLineCount = (active.length / 7);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.activeLineBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(active), gl.DYNAMIC_DRAW);
    }

    // ---------------------------------------------------------------- Controls & Navigation

    _computeSceneBounds() {
      let minX = Infinity, maxX = -Infinity;
      let minY = Infinity, maxY = -Infinity;
      const minZ = -(this.sceneDepth || 12000);
      const maxZ = (this.unknownCount > 0) ? 600 : 0;

      if (this.clusters && this.clusters.length > 0) {
        for (const c of this.clusters) {
          const r = c.radius || 0;
          if (c.x - r < minX) minX = c.x - r;
          if (c.x + r > maxX) maxX = c.x + r;
          if (c.y - r < minY) minY = c.y - r;
          if (c.y + r > maxY) maxY = c.y + r;
        }
      } else if (this.positions && this.positions.length >= 3) {
        for (let i = 0; i < this.positions.length; i += 3) {
          const x = this.positions[i];
          const y = this.positions[i + 1];
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
      }

      if (!isFinite(minX) || !isFinite(maxX)) {
        minX = -500; maxX = 500;
        minY = -500; maxY = 500;
      }

      this.sceneBounds = {
        minX, maxX, minY, maxY, minZ, maxZ,
        cx: (minX + maxX) * 0.5,
        cy: (minY + maxY) * 0.5,
      };
      return this.sceneBounds;
    }

    _computeFarPlane() {
      const b = this.sceneBounds || this._computeSceneBounds();
      const dx = Math.max(Math.abs(this.cameraPos[0] - b.minX), Math.abs(this.cameraPos[0] - b.maxX));
      const dy = Math.max(Math.abs(this.cameraPos[1] - b.minY), Math.abs(this.cameraPos[1] - b.maxY));
      const dz = Math.max(Math.abs(this.cameraPos[2] - b.minZ), Math.abs(this.cameraPos[2] - b.maxZ));
      const maxSceneDist = Math.hypot(dx, dy, dz);
      return Math.max(35000.0, maxSceneDist * 1.25);
    }

    getFarPlane() {
      return this._computeFarPlane();
    }

    _distanceToSceneBounds() {
      const b = this.sceneBounds || ((this.nodes.length || this.clusters.length) ? this._computeSceneBounds() : null);
      if (!b) return 0;
      const cx = this.cameraPos[0];
      const cy = this.cameraPos[1];
      const cz = this.cameraPos[2];

      const dx = Math.max(0, b.minX - cx, cx - b.maxX);
      const dy = Math.max(0, b.minY - cy, cy - b.maxY);
      const dz = Math.max(0, b.minZ - cz, cz - b.maxZ);

      return Math.hypot(dx, dy, dz);
    }

    getDollyStep() {
      const dist = this._distanceToSceneBounds();
      const floorStep = Math.max(600, (this.sceneDepth || 12000) * 0.05);
      return clamp(Math.max(dist * 0.28, floorStep), floorStep, 8000);
    }

    getFlightSpeed() {
      const dist = this._distanceToSceneBounds();
      return clamp(1800 * (1 + dist / 4000), 1800, 15000);
    }

    fitToView() {
      if (!this.clusters.length && !this.nodes.length) {
        this.cameraPos = [0, 0, 1000];
        this.yaw = 0;
        this.pitch = 0;
        this.velocity = [0, 0, 0];
        this.farPlane = 35000.0;
        this.requestDraw();
        return;
      }

      const b = this.sceneBounds || this._computeSceneBounds();
      const halfW = Math.max((b.maxX - b.minX) * 0.5, 200);
      const halfH = Math.max((b.maxY - b.minY) * 0.5, 200);

      const fov = 60 * Math.PI / 180;
      const aspect = (this.width > 0 && this.height > 0) ? (this.width / this.height) : (16 / 9);
      const tanHalfFov = Math.tan(fov * 0.5);

      const distY = halfH / tanHalfFov;
      const distX = halfW / (aspect * tanHalfFov);
      const margin = 1.15;
      const fitDist = Math.max(distY, distX) * margin;

      const camZ = b.maxZ + fitDist;

      this.cameraPos = [b.cx, b.cy, camZ];
      this.yaw = 0;
      this.pitch = 0;
      this.velocity = [0, 0, 0];
      this.farPlane = this._computeFarPlane();
      this.requestDraw();
    }

    zoomIn() {
      const step = this.getDollyStep();
      const fz = -Math.cos(this.yaw) * Math.cos(this.pitch);
      const fx = Math.sin(this.yaw) * Math.cos(this.pitch);
      const fy = Math.sin(this.pitch);
      this.cameraPos[0] += fx * step;
      this.cameraPos[1] += fy * step;
      this.cameraPos[2] += fz * step;
      this.requestDraw();
    }

    zoomOut() {
      const step = this.getDollyStep();
      const fz = -Math.cos(this.yaw) * Math.cos(this.pitch);
      const fx = Math.sin(this.yaw) * Math.cos(this.pitch);
      const fy = Math.sin(this.pitch);
      this.cameraPos[0] -= fx * step;
      this.cameraPos[1] -= fy * step;
      this.cameraPos[2] -= fz * step;
      this.requestDraw();
    }

    selectNode(uid) {
      if (this.selectedUid === uid) return;
      this.selectedUid = uid || null;
      this.selectedEdgeId = null;
      this.highlightSet.clear();

      if (this.selectedUid) {
        this.highlightSet.add(this.selectedUid);
        for (const e of this.edges) {
          if (e.source === this.selectedUid) this.highlightSet.add(e.target);
          else if (e.target === this.selectedUid) this.highlightSet.add(e.source);
        }
      }

      this._updateSelectionBuffer();
      this._uploadActiveEdges();

      const node = this.selectedUid ? this.nodeMap.get(this.selectedUid) : null;
      if (node) this.onNodeSelect(node);

      this.requestDraw();
    }

    selectEdge(id) {
      if (this.selectedEdgeId === id) return;
      this.selectedEdgeId = id || null;
      this.selectedUid = null;
      this.highlightSet.clear();

      const edge = this.selectedEdgeId ? this.edgeMap.get(this.selectedEdgeId) : null;
      if (edge) {
        this.highlightSet.add(edge.source);
        this.highlightSet.add(edge.target);
        this.onEdgeSelect(edge);
      }

      this._updateSelectionBuffer();
      this._uploadActiveEdges();
      this.requestDraw();
    }

    clearSelection() {
      if (!this.selectedUid && !this.selectedEdgeId) return;
      this.selectedUid = null;
      this.selectedEdgeId = null;
      this.highlightSet.clear();
      this._updateSelectionBuffer();
      this._uploadActiveEdges();
      this.requestDraw();
    }

    // ---------------------------------------------------------------- Events & Input Handling

    _isInputFocused() {
      if (typeof document === 'undefined') return false;
      const el = document.activeElement;
      if (!el) return false;
      const tag = el.tagName;
      return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
    }

    _bindEvents() {
      if (!this.canvas || typeof window === 'undefined') return;

      this.canvas.addEventListener('pointerdown', this._onPointerDown);
      window.addEventListener('pointermove', this._onPointerMove);
      window.addEventListener('pointerup', this._onPointerUp);
      window.addEventListener('pointercancel', this._onPointerCancel);
      this.canvas.addEventListener('wheel', this._onWheel, { passive: false });
      this.canvas.addEventListener('dblclick', this._onDblClick);

      window.addEventListener('keydown', this._onKeyDown);
      window.addEventListener('keyup', this._onKeyUp);
      window.addEventListener('resize', this._onResize);
      window.addEventListener('blur', this._onBlur);
      document.addEventListener('visibilitychange', this._onVisibilityChange);
    }

    _unbindEvents() {
      if (!this.canvas || typeof window === 'undefined') return;

      this.canvas.removeEventListener('pointerdown', this._onPointerDown);
      window.removeEventListener('pointermove', this._onPointerMove);
      window.removeEventListener('pointerup', this._onPointerUp);
      window.removeEventListener('pointercancel', this._onPointerCancel);
      this.canvas.removeEventListener('wheel', this._onWheel);
      this.canvas.removeEventListener('dblclick', this._onDblClick);

      window.removeEventListener('keydown', this._onKeyDown);
      window.removeEventListener('keyup', this._onKeyUp);
      window.removeEventListener('resize', this._onResize);
      window.removeEventListener('blur', this._onBlur);
      document.removeEventListener('visibilitychange', this._onVisibilityChange);
    }

    _handleKeyDown(ev) {
      if (!this.active || this._isInputFocused()) return;
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

      const code = ev.code || ev.key;
      const k = ev.key;

      if (k === '+' || k === '=') { this.zoomIn(); ev.preventDefault(); return; }
      if (k === '-') { this.zoomOut(); ev.preventDefault(); return; }
      if (k === '0') { this.fitToView(); ev.preventDefault(); return; }
      if (k === 'Escape') { this.clearSelection(); ev.preventDefault(); return; }

      const flightKeys = ['KeyW', 'KeyS', 'KeyA', 'KeyD', 'KeyQ', 'KeyE', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];
      if (flightKeys.includes(code) || ['w', 's', 'a', 'd', 'q', 'e', 'W', 'S', 'A', 'D', 'Q', 'E'].includes(k)) {
        this.keys[code] = true;
        this.keys[k.toLowerCase()] = true;
        this._startRAF();
        ev.preventDefault();
      }
    }

    _handleKeyUp(ev) {
      const code = ev.code || ev.key;
      const k = ev.key;
      delete this.keys[code];
      delete this.keys[k?.toLowerCase()];
    }

    _stopMovement() {
      this.keys = Object.create(null);
      this.velocity = [0, 0, 0];
      this.pointerDragging = false;
      this.pointerMoved = false;
    }

    _handlePointerDown(ev) {
      if (!this.active || ev.button !== 0) return;
      this.pointerDragging = true;
      this.pointerMoved = false;
      this.dragStartX = ev.clientX;
      this.dragStartY = ev.clientY;
      this.lastPointerX = ev.clientX;
      this.lastPointerY = ev.clientY;

      if (this.canvas.setPointerCapture) {
        try { this.canvas.setPointerCapture(ev.pointerId); } catch (_) {}
      }
      this._startRAF();
    }

    _handlePointerMove(ev) {
      if (this.pointerDragging) {
        const dx = ev.clientX - this.lastPointerX;
        const dy = ev.clientY - this.lastPointerY;
        this.lastPointerX = ev.clientX;
        this.lastPointerY = ev.clientY;

        if (Math.abs(ev.clientX - this.dragStartX) + Math.abs(ev.clientY - this.dragStartY) > 4) {
          this.pointerMoved = true;
        }

        if (this.pointerMoved) {
          const sensitivity = 0.0035;
          this.yaw -= dx * sensitivity;
          this.pitch -= dy * sensitivity;
          this.pitch = clamp(this.pitch, -1.45, 1.45);
          this.requestDraw();
        }
      }
    }

    _handlePointerUp(ev) {
      if (!this.pointerDragging) return;
      this.pointerDragging = false;

      if (this.canvas.releasePointerCapture) {
        try { this.canvas.releasePointerCapture(ev.pointerId); } catch (_) {}
      }

      if (!this.pointerMoved) {
        // Klick auf Knoten hit-testen
        const rect = this.canvas.getBoundingClientRect();
        const clickX = ev.clientX - rect.left;
        const clickY = ev.clientY - rect.top;
        const hit = this._hitTestNode(clickX, clickY);

        if (hit) {
          this.selectNode(hit.uid);
        } else {
          this.clearSelection();
        }
      }
    }

    _handlePointerCancel() {
      this._stopMovement();
    }

    _handleDblClick(ev) {
      if (!this.active) return;
      const rect = this.canvas.getBoundingClientRect();
      const clickX = ev.clientX - rect.left;
      const clickY = ev.clientY - rect.top;
      const hit = this._hitTestNode(clickX, clickY);
      if (hit) {
        this.onNodeActivate(hit);
      }
    }

    _handleWheel(ev) {
      if (!this.active) return;
      ev.preventDefault();

      const delta = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
      const moveDist = this.getDollyStep() * 0.3 * (delta / 100);
      const fz = -Math.cos(this.yaw) * Math.cos(this.pitch);
      const fx = Math.sin(this.yaw) * Math.cos(this.pitch);
      const fy = Math.sin(this.pitch);

      this.cameraPos[0] -= fx * moveDist;
      this.cameraPos[1] -= fy * moveDist;
      this.cameraPos[2] -= fz * moveDist;

      this.requestDraw();
    }

    _handleResize() {
      if (!this.container || !this.canvas) return;
      const rect = this.container.getBoundingClientRect();
      const w = Math.max(1, Math.floor(rect.width));
      const h = Math.max(1, Math.floor(rect.height));
      const rawDpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
      const dpr = Math.min(rawDpr, 2.0); // DPR auf max 2 deckeln

      if (w === this.width && h === this.height && dpr === this.dpr) return;

      this.width = w;
      this.height = h;
      this.dpr = dpr;

      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this.canvas.style.width = w + 'px';
      this.canvas.style.height = h + 'px';

      if (this.gl) {
        this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      }

      this._lastLabelCamPos = [Infinity, Infinity, Infinity];
      this.requestDraw();
    }

    // ---------------------------------------------------------------- Hit Testing

    _hitTestNode(clickX, clickY) {
      if (!this.nodes.length || !this.positions) return null;

      const vp = this.viewProjectionMatrix;
      const hw = this.width * 0.5;
      const hh = this.height * 0.5;
      const pos = this.positions;
      const n = this.nodes.length;

      let bestNode = null;
      let bestDistSq = 22 * 22; // Trefferradius in Pixeln
      let bestDepth = Infinity;

      for (let i = 0; i < n; i++) {
        const i3 = i * 3;
        const x = pos[i3], y = pos[i3 + 1], z = pos[i3 + 2];

        // Clip-Koordinaten berechnen
        const w = x * vp[3] + y * vp[7] + z * vp[11] + vp[15];
        if (w <= 0.1) continue; // Hinter der Kamera

        const invW = 1.0 / w;
        const ndcX = (x * vp[0] + y * vp[4] + z * vp[8] + vp[12]) * invW;
        if (ndcX < -1.1 || ndcX > 1.1) continue;
        const ndcY = (x * vp[1] + y * vp[5] + z * vp[9] + vp[13]) * invW;
        if (ndcY < -1.1 || ndcY > 1.1) continue;

        const sx = (ndcX + 1.0) * hw;
        const sy = (1.0 - ndcY) * hh;

        const dx = sx - clickX;
        const dy = sy - clickY;
        const distSq = dx * dx + dy * dy;

        if (distSq < bestDistSq || (distSq <= bestDistSq + 36 && w < bestDepth)) {
          bestDistSq = distSq;
          bestDepth = w;
          bestNode = this.nodes[i];
        }
      }

      return bestNode;
    }

    // ---------------------------------------------------------------- Animation & Rendering

    requestDraw() {
      if (!this.supported) return;
      this._needRedraw = true;
      this._startRAF();
    }

    _startRAF() {
      if (this._rafId || !this.active || !this.supported) return;
      this._lastTime = (typeof performance !== 'undefined' ? performance.now() : Date.now());
      this._rafId = safeRAF(this._tick);
    }

    _cancelRAF() {
      if (this._rafId) {
        safeCAF(this._rafId);
        this._rafId = null;
      }
    }

    _isMoving() {
      const keysDown = Object.keys(this.keys).length > 0;
      const hasVel = Math.hypot(this.velocity[0], this.velocity[1], this.velocity[2]) > 0.05;
      return keysDown || hasVel || this.pointerDragging;
    }

    _updatePhysics(dt) {
      const prefersReducedMotion = typeof window !== 'undefined' &&
        window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      // Richtungsvektoren
      const cy = Math.cos(this.yaw);
      const sy = Math.sin(this.yaw);
      const cp = Math.cos(this.pitch);
      const sp = Math.sin(this.pitch);

      const fwd = [sy * cp, sp, -cy * cp];
      const right = [cy, 0, sy];
      const up = [0, 1, 0];

      let mx = 0, my = 0, mz = 0;
      const k = this.keys;

      if (k.KeyW || k.w || k.ArrowUp) { mz += 1; }
      if (k.KeyS || k.s || k.ArrowDown) { mz -= 1; }
      if (k.KeyA || k.a || k.ArrowLeft) { mx -= 1; }
      if (k.KeyD || k.d || k.ArrowRight) { mx += 1; }
      if (k.KeyQ || k.q) { my -= 1; }
      if (k.KeyE || k.e) { my += 1; }

      const len = Math.hypot(mx, my, mz);
      const speed = this.getFlightSpeed(); // Adaptive flight speed scaled with distance

      if (len > 0) {
        mx /= len; my /= len; mz /= len;
        const targetVx = (fwd[0] * mz + right[0] * mx + up[0] * my) * speed;
        const targetVy = (fwd[1] * mz + right[1] * mx + up[1] * my) * speed;
        const targetVz = (fwd[2] * mz + right[2] * mx + up[2] * my) * speed;

        if (prefersReducedMotion) {
          this.velocity[0] = targetVx;
          this.velocity[1] = targetVy;
          this.velocity[2] = targetVz;
        } else {
          const accel = 8.0;
          this.velocity[0] += (targetVx - this.velocity[0]) * Math.min(1, accel * dt);
          this.velocity[1] += (targetVy - this.velocity[1]) * Math.min(1, accel * dt);
          this.velocity[2] += (targetVz - this.velocity[2]) * Math.min(1, accel * dt);
        }
      } else {
        const friction = prefersReducedMotion ? 0 : 0.88;
        this.velocity[0] *= Math.pow(friction, dt * 60);
        this.velocity[1] *= Math.pow(friction, dt * 60);
        this.velocity[2] *= Math.pow(friction, dt * 60);
      }

      this.cameraPos[0] += this.velocity[0] * dt;
      this.cameraPos[1] += this.velocity[1] * dt;
      this.cameraPos[2] += this.velocity[2] * dt;

      return len > 0 || Math.hypot(this.velocity[0], this.velocity[1], this.velocity[2]) > 0.05;
    }

    _step(now) {
      this._rafId = null;
      if (!this.active || !this.gl || this.contextLost) return;

      const dt = clamp((now - this._lastTime) / 1000, 0.001, 0.1);
      this._lastTime = now;

      const moved = this._updatePhysics(dt);
      this._render();

      if (moved || this._needRedraw || this._isMoving()) {
        this._needRedraw = false;
        this._rafId = requestAnimationFrame(this._tick);
      }
    }

    _render() {
      const gl = this.gl;
      if (!gl || this.contextLost || !this.width || !this.height) return;

      // 1. Matrizen berechnen
      this._updateMatrices();

      // 2. Viewport & Clearing
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.clearColor(0.96, 0.96, 0.97, 1.0);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

      // 3. Kanten zeichnen
      this._renderEdges();

      if (this.timelineAxis) {
        this._renderTimelineAxis();
      }

      // 4. Knoten zeichnen
      this._renderNodes();

      // 5. DOM-Overlay aktualisieren
      this._updateOverlay();
    }

    _renderEdges() {
      const gl = this.gl;
      if (!this.lineProgram || (!this.sparseLineCount && !this.activeLineCount)) return;

      gl.useProgram(this.lineProgram);
      gl.uniformMatrix4fv(this.lineUniforms.u_viewProjection, false, this.viewProjectionMatrix);

      const aPos = gl.getAttribLocation(this.lineProgram, 'a_position');
      const aCol = gl.getAttribLocation(this.lineProgram, 'a_color');
      gl.enableVertexAttribArray(aPos);
      gl.enableVertexAttribArray(aCol);

      // Sparse Edges
      if (this.sparseLineCount > 0) {
        gl.bindBuffer(gl.ARRAY_BUFFER, this.sparseLineBuffer);
        gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 7 * 4, 0);
        gl.vertexAttribPointer(aCol, 4, gl.FLOAT, false, 7 * 4, 3 * 4);
        gl.drawArrays(gl.LINES, 0, this.sparseLineCount);
      }

      // Aktive Edges (hervorgehoben)
      if (this.activeLineCount > 0) {
        gl.bindBuffer(gl.ARRAY_BUFFER, this.activeLineBuffer);
        gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 7 * 4, 0);
        gl.vertexAttribPointer(aCol, 4, gl.FLOAT, false, 7 * 4, 3 * 4);
        gl.drawArrays(gl.LINES, 0, this.activeLineCount);
      }

      gl.disableVertexAttribArray(aPos);
      gl.disableVertexAttribArray(aCol);
    }

    _renderNodes() {
      const gl = this.gl;
      const n = this.nodes.length;
      if (!this.nodeProgram || n === 0) return;

      gl.useProgram(this.nodeProgram);

      // Gecachte Farben verwenden (aktualisiert nur bei setActive und Context Restore)
      const colors = this._cachedColors || {
        confCol: [0.09, 0.41, 0.86],
        jiraCol: [0.89, 0.45, 0.05],
        dangerCol: [0.87, 0.21, 0.04],
        mutedCol: [0.48, 0.53, 0.60],
        surfCol: [1.0, 1.0, 1.0],
        focusCol: [0.0, 0.32, 0.80],
      };
      const confCol = colors.confCol;
      const jiraCol = colors.jiraCol;
      const dangerCol = colors.dangerCol;
      const mutedCol = colors.mutedCol;
      const surfCol = colors.surfCol;
      const focusCol = colors.focusCol;

      // Uniforms setzen
      const u = this.nodeUniforms;
      gl.uniformMatrix4fv(u.u_viewProjection, false, this.viewProjectionMatrix);
      gl.uniform2f(u.u_viewport, this.width, this.height);
      gl.uniform1f(u.u_importanceMode, this.importanceMode ? 1.0 : 0.0);
      gl.uniform1f(u.u_dpr, this.dpr);
      gl.uniform3fv(u.u_colorConfluence, confCol);
      gl.uniform3fv(u.u_colorJira, jiraCol);
      gl.uniform3fv(u.u_colorDanger, dangerCol);
      gl.uniform3fv(u.u_colorMuted, mutedCol);
      gl.uniform3fv(u.u_colorSurface, surfCol);
      gl.uniform3fv(u.u_colorFocus, focusCol);
      gl.uniform1f(u.u_hasSelection, this.selectedUid ? 1.0 : 0.0);

      // Attribute binden
      const aPos = gl.getAttribLocation(this.nodeProgram, 'a_position');
      const aType = gl.getAttribLocation(this.nodeProgram, 'a_type');
      const aState = gl.getAttribLocation(this.nodeProgram, 'a_state');
      const aRad = gl.getAttribLocation(this.nodeProgram, 'a_radius');
      const aImp = gl.getAttribLocation(this.nodeProgram, 'a_importance');
      const aSel = gl.getAttribLocation(this.nodeProgram, 'a_selected');

      gl.enableVertexAttribArray(aPos);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.nodePosBuffer);
      gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, this.nodeAttrBuffer);
      if (aType >= 0) { gl.enableVertexAttribArray(aType); gl.vertexAttribPointer(aType, 1, gl.FLOAT, false, 4 * 4, 0); }
      if (aState >= 0) { gl.enableVertexAttribArray(aState); gl.vertexAttribPointer(aState, 1, gl.FLOAT, false, 4 * 4, 1 * 4); }
      if (aRad >= 0) { gl.enableVertexAttribArray(aRad); gl.vertexAttribPointer(aRad, 1, gl.FLOAT, false, 4 * 4, 2 * 4); }
      if (aImp >= 0) { gl.enableVertexAttribArray(aImp); gl.vertexAttribPointer(aImp, 1, gl.FLOAT, false, 4 * 4, 3 * 4); }

      gl.bindBuffer(gl.ARRAY_BUFFER, this.nodeSelBuffer);
      if (aSel >= 0) { gl.enableVertexAttribArray(aSel); gl.vertexAttribPointer(aSel, 1, gl.FLOAT, false, 0, 0); }

      // Einziger Draw-Call für alle 100k Punkte
      gl.drawArrays(gl.POINTS, 0, n);

      gl.disableVertexAttribArray(aPos);
      if (aType >= 0) gl.disableVertexAttribArray(aType);
      if (aState >= 0) gl.disableVertexAttribArray(aState);
      if (aRad >= 0) gl.disableVertexAttribArray(aRad);
      if (aImp >= 0) gl.disableVertexAttribArray(aImp);
      if (aSel >= 0) gl.disableVertexAttribArray(aSel);
    }

    // ---------------------------------------------------------------- Overlay Update

    _formatTimeLabel(camZ) {
      if (camZ > 300) {
        return 'Bereich: Ohne Datum';
      }
      if (this.minTime !== null && this.maxTime !== null && this.maxTime > this.minTime) {
        // z=0 ist maxTime (neueste), z=-12000 ist minTime (älteste)
        const ratio = clamp(-camZ / this.sceneDepth, 0, 1);
        const t = this.maxTime - ratio * (this.maxTime - this.minTime);
        try {
          const dateStr = new Date(t).toLocaleDateString('de-DE', { year: 'numeric', month: 'short', day: 'numeric' });
          return 'Zeitleiste: ~' + dateStr;
        } catch (_) {
          return 'Zeitleiste aktiv';
        }
      }
      return 'Tiefe: Z = ' + Math.round(camZ);
    }

    _updateOverlay(force = false) {
      if (!this.overlayEl) return;

      const vp = this.viewProjectionMatrix;
      const hw = this.width * 0.5;
      const hh = this.height * 0.5;

      // 1. Timeline HUD aktualisieren
      if (this.timelineHudEl) {
        const timeLabel = this._formatTimeLabel(this.cameraPos[2]);
        this.timelineHudEl.textContent = timeLabel;
        this.timelineHudEl.style.display = 'block';
      }

      // 2. Cluster Badges (mit Screen-Space-Kollisionsunterdrückung)
      const pool = this.clusterLabelPool || [];
      const placedRects = [];

      // Timeline-HUD Kollisionsbox registrieren, damit Labels es nicht überdecken
      if (this.timelineHudEl && this.timelineHudEl.style.display !== 'none') {
        const cr = (this.container && typeof this.container.getBoundingClientRect === 'function')
          ? this.container.getBoundingClientRect()
          : { left: 0, top: 0 };
        const hr = (typeof this.timelineHudEl.getBoundingClientRect === 'function')
          ? this.timelineHudEl.getBoundingClientRect()
          : null;
        if (hr && (hr.width > 0 || hr.height > 0 || hr.right > hr.left)) {
          placedRects.push({
            left: hr.left - cr.left,
            top: hr.top - cr.top,
            right: hr.right - cr.left,
            bottom: hr.bottom - cr.top,
          });
        } else {
          const hw = 120, hh = 26;
          const left = (this.width - hw) * 0.5;
          const top = this.height - 12 - hh;
          placedRects.push({ left, top, right: left + hw, bottom: top + hh });
        }
      }

      // 2. Year Labels (entlang der Zeitachse)
      this._updateYearLabels(placedRects, force);

      let labelIdx = 0;

      for (let i = 0; i < this.clusters.length && labelIdx < pool.length; i++) {
        const c = this.clusters[i];
        // Clusterzentrum im Raum
        const cx = c.x, cy = c.y, cz = -3000; // Repräsentative mittlere Tiefe

        const w = cx * vp[3] + cy * vp[7] + cz * vp[11] + vp[15];
        if (w <= 0.1) continue; // Hinter Kamera

        const invW = 1.0 / w;
        const ndcX = (cx * vp[0] + cy * vp[4] + cz * vp[8] + vp[12]) * invW;
        const ndcY = (cx * vp[1] + cy * vp[5] + cz * vp[9] + vp[13]) * invW;

        if (ndcX < -1.1 || ndcX > 1.1 || ndcY < -1.1 || ndcY > 1.1) continue;

        const sx = Math.round((ndcX + 1.0) * hw);
        const sy = Math.round((1.0 - ndcY) * hh);

        const badgeEl = pool[labelIdx];
        const bw = badgeEl.offsetWidth || 110;
        const bh = badgeEl.offsetHeight || 28;
        const bLeft = sx - bw * 0.5;
        const bTop = sy - bh * 0.5;
        const bRight = sx + bw * 0.5;
        const bBottom = sy + bh * 0.5;

        let collides = false;
        for (const p of placedRects) {
          if (!(bRight + 6 < p.left || bLeft > p.right + 6 || bBottom + 6 < p.top || bTop > p.bottom + 6)) {
            collides = true;
            break;
          }
        }
        if (collides) continue;

        placedRects.push({ left: bLeft, top: bTop, right: bRight, bottom: bBottom });

        const badge = pool[labelIdx++];
        badge._cluster = c;
        badge.textContent = formatClusterBadgeText(c);
        badge.style.left = sx + 'px';
        badge.style.top = sy + 'px';
        badge.style.display = 'block';
      }

      // Restliche Badges ausblenden
      for (; labelIdx < pool.length; labelIdx++) {
        pool[labelIdx].style.display = 'none';
      }

      // 3. Proximity Node Labels (mit Kollisionsunterdrückung gegen Badges und andere Labels)
      this._updateNodeLabels(placedRects, force);

      // 4. Tooltip für selektierten Knoten
      if (this.tooltipEl) {
        if (this.selectedUid && this.nodeMap.has(this.selectedUid) && this.positions) {
          const node = this.nodeMap.get(this.selectedUid);
          const i3 = node.idx * 3;
          const nx = this.positions[i3], ny = this.positions[i3 + 1], nz = this.positions[i3 + 2];

          const w = nx * vp[3] + ny * vp[7] + nz * vp[11] + vp[15];
          if (w > 0.1) {
            const invW = 1.0 / w;
            const ndcX = (nx * vp[0] + ny * vp[4] + nz * vp[8] + vp[12]) * invW;
            const ndcY = (nx * vp[1] + ny * vp[5] + nz * vp[9] + vp[13]) * invW;

            if (ndcX >= -1.0 && ndcX <= 1.0 && ndcY >= -1.0 && ndcY <= 1.0) {
              const sx = Math.round((ndcX + 1.0) * hw);
              const sy = Math.round((1.0 - ndcY) * hh);

              const tip = this.tooltipEl;
              while (tip.firstChild) tip.removeChild(tip.firstChild);

              const titleEl = document.createElement('div');
              titleEl.style.fontWeight = '600';
              titleEl.style.marginBottom = '2px';
              titleEl.textContent = node.title || node.uid;

              const metaEl = document.createElement('div');
              metaEl.style.color = cssVar('--text-muted', '#7a869a');
              metaEl.style.fontSize = '11px';
              const parts = [
                node.uid,
                node.type === 'jira' ? 'Jira' : 'Confluence',
                node.document_type,
                node.status,
                node.in_degree !== undefined ? `${node.in_degree} eingehend` : null,
              ];
              metaEl.textContent = parts.filter(Boolean).join(' · ');

              tip.appendChild(titleEl);
              tip.appendChild(metaEl);

              tip.style.left = Math.min(sx + 14, this.width - 240) + 'px';
              tip.style.top = Math.min(sy + 14, this.height - 70) + 'px';
              tip.style.display = 'block';
              return;
            }
          }
        }
        this.tooltipEl.style.display = 'none';
      }
    }

    _updateMatrices(camPos = this.cameraPos) {
      const w = this.width || 800;
      const h = this.height || 600;
      const aspect = w / h;
      const fov = 60 * Math.PI / 180;
      const near = 1.0;
      const far = this._computeFarPlane();
      this.farPlane = far;

      mat4Perspective(this.projMatrix, fov, aspect, near, far);
      mat4CameraView(this.viewMatrix, camPos[0], camPos[1], camPos[2], this.yaw, this.pitch);
      mat4Multiply(this.viewProjectionMatrix, this.projMatrix, this.viewMatrix);
    }

    _hideAllNodeLabels() {
      if (!this.nodeLabelPool) return;
      for (let i = 0; i < this.nodeLabelPool.length; i++) {
        this.nodeLabelPool[i].style.display = 'none';
        this.nodeLabelPool[i]._node = null;
      }
    }

    _buildNodeSpatialGrid() {
      const grid = new Map();
      if (!this.positions || !this.positions.length || !this.nodes.length) {
        this._nodeSpatialGrid = grid;
        return grid;
      }

      const invCell = 1.0 / LABEL_RADIUS;
      const count = Math.min(this.nodes.length, Math.floor(this.positions.length / 3));

      for (let i = 0; i < count; i++) {
        const i3 = i * 3;
        const x = this.positions[i3];
        const y = this.positions[i3 + 1];
        const z = this.positions[i3 + 2];

        const cx = Math.floor(x * invCell);
        const cy = Math.floor(y * invCell);
        const cz = Math.floor(z * invCell);
        const key = `${cx},${cy},${cz}`;

        let cell = grid.get(key);
        if (!cell) {
          cell = [];
          grid.set(key, cell);
        }
        cell.push(i);
      }

      this._nodeSpatialGrid = grid;
      return grid;
    }

    _computeNodeLabels(camPosOverride = null) {
      if (!this.positions || !this.positions.length || !this.nodes.length) {
        return [];
      }
      if (!this._nodeSpatialGrid) {
        this._buildNodeSpatialGrid();
      }

      const camPos = camPosOverride || this.cameraPos;
      const savedView = camPosOverride ? new Float32Array(this.viewMatrix) : null;
      const savedVP = camPosOverride ? new Float32Array(this.viewProjectionMatrix) : null;

      try {
        this._updateMatrices(camPos);

        const camX = camPos[0];
        const camY = camPos[1];
        const camZ = camPos[2];

        const vp = this.viewProjectionMatrix;
        const hw = (this.width || 800) * 0.5;
        const hh = (this.height || 600) * 0.5;

        const invCell = 1.0 / LABEL_RADIUS;
        const ccx = Math.floor(camX * invCell);
        const ccy = Math.floor(camY * invCell);
        const ccz = Math.floor(camZ * invCell);

        const maxRadiusSq = LABEL_RADIUS * LABEL_RADIUS;
        const candidates = [];
        const visited = new Set();

        for (let dx = -1; dx <= 1; dx++) {
          for (let dy = -1; dy <= 1; dy++) {
            for (let dz = -1; dz <= 1; dz++) {
              const key = `${ccx + dx},${ccy + dy},${ccz + dz}`;
              const cell = this._nodeSpatialGrid.get(key);
              if (!cell) continue;

              for (let c = 0; c < cell.length; c++) {
                const i = cell[c];
                if (visited.has(i)) continue;
                visited.add(i);

                const node = this.nodes[i];
                // Selektierten Knoten überspringen: wird bereits exklusiv durch den Tooltip angezeigt
                if (this.selectedUid && node && node.uid === this.selectedUid) continue;

                const i3 = i * 3;
                const nx = this.positions[i3];
                const ny = this.positions[i3 + 1];
                const nz = this.positions[i3 + 2];

                const diffX = nx - camX;
                const diffY = ny - camY;
                const diffZ = nz - camZ;
                const distSq = diffX * diffX + diffY * diffY + diffZ * diffZ;
                if (distSq > maxRadiusSq) continue;

                const w = nx * vp[3] + ny * vp[7] + nz * vp[11] + vp[15];
                if (w <= 0.1) continue; // Hinter Kamera

                const invW = 1.0 / w;
                const ndcX = (nx * vp[0] + ny * vp[4] + nz * vp[8] + vp[12]) * invW;
                const ndcY = (nx * vp[1] + ny * vp[5] + nz * vp[9] + vp[13]) * invW;

                if (ndcX < -1.05 || ndcX > 1.05 || ndcY < -1.05 || ndcY > 1.05) continue;

                const sx = Math.round((ndcX + 1.0) * hw);
                const sy = Math.round((1.0 - ndcY) * hh);
                const dist = Math.sqrt(distSq);

                candidates.push({
                  node,
                  index: i,
                  distance: dist,
                  dist: dist,
                  distSq,
                  sx,
                  sy,
                  clipW: w,
                  ndcX,
                  ndcY,
                });
              }
            }
          }
        }

        // Nach Distanz aufsteigend sortieren
        candidates.sort((a, b) => a.distance - b.distance);

        if (candidates.length > MAX_NODE_LABELS) {
          return candidates.slice(0, MAX_NODE_LABELS);
        }

        return candidates;
      } finally {
        if (savedView && savedVP) {
          this.viewMatrix.set(savedView);
          this.viewProjectionMatrix.set(savedVP);
        }
      }
    }

    _updateYearLabels(placedRects, force = false) {
      const pool = this.yearLabelPool;
      if (!pool || !pool.length) return;

      if (!this.active || !this.timelineAxis || this._loading || !this.timelineTicks || !this.timelineTicks.length) {
        this._hideAllYearLabels();
        return;
      }

      const vp = this.viewProjectionMatrix;
      const hw = (this.width || 800) * 0.5;
      const hh = (this.height || 600) * 0.5;
      const canvasW = this.width || 800;
      const canvasH = this.height || 600;

      const b = this.sceneBounds || this._computeSceneBounds();
      const minX = b ? b.minX : -500;
      const maxX = b ? b.maxX : 500;
      const offsetX = clamp((maxX - minX) * 0.03, 180, 600);
      const axisX = (this.timelineAxisX !== undefined) ? this.timelineAxisX : ((b ? b.cx : 0) + offsetX);
      const axisY = (this.timelineAxisY !== undefined) ? this.timelineAxisY : (b ? b.cy : 0);

      const camZ = (this.cameraPos && isFinite(this.cameraPos[2])) ? this.cameraPos[2] : 0;
      const sortedTicks = [...this.timelineTicks].sort((a, b) => {
        return Math.abs(a.z - camZ) - Math.abs(b.z - camZ);
      });

      const externalRects = placedRects ? placedRects.slice() : [];
      const placedYearRects = [];
      let placedCount = 0;

      for (let i = 0; i < sortedTicks.length && placedCount < pool.length; i++) {
        const tick = sortedTicks[i];
        const tz = tick.z;

        // Clip / frustum test
        const w = axisX * vp[3] + axisY * vp[7] + tz * vp[11] + vp[15];
        if (w <= 0.1) continue; // Behind camera

        const invW = 1.0 / w;
        const ndcX = (axisX * vp[0] + axisY * vp[4] + tz * vp[8] + vp[12]) * invW;
        const ndcY = (axisX * vp[1] + axisY * vp[5] + tz * vp[9] + vp[13]) * invW;

        if (ndcX < -1.05 || ndcX > 1.05 || ndcY < -1.05 || ndcY > 1.05) continue;

        const sx = Math.round((ndcX + 1.0) * hw);
        const sy = Math.round((1.0 - ndcY) * hh);

        const lw = Math.max(50, tick.text.length * 7 + 16);
        const lh = 22;
        const stepY = lh + 4; // 26px

        const posX = sx + 16;
        let posY = sy;
        let lLeft = posX - lw * 0.5;
        let lRight = posX + lw * 0.5;
        let lTop = posY - lh * 0.5;
        let lBottom = posY + lh * 0.5;

        // Canvas X bounds check (horizontal position does not change during vertical stacking)
        if (lLeft < 4 || lRight > canvasW - 4) {
          continue;
        }

        // Nudge downward until it fits without colliding with placed year labels or external obstacles (HUD)
        let fits = false;
        const maxBottom = canvasH - 4;

        while (lBottom <= maxBottom) {
          let collides = false;
          for (let j = 0; j < placedYearRects.length; j++) {
            const yr = placedYearRects[j];
            if (!(lRight + 4 <= yr.left || lLeft >= yr.right + 4 || lBottom + 4 <= yr.top || lTop >= yr.bottom + 4)) {
              collides = true;
              break;
            }
          }
          if (!collides && externalRects.length > 0) {
            for (let j = 0; j < externalRects.length; j++) {
              const er = externalRects[j];
              if (!(lRight + 4 <= er.left || lLeft >= er.right + 4 || lBottom + 4 <= er.top || lTop >= er.bottom + 4)) {
                collides = true;
                break;
              }
            }
          }
          if (!collides) {
            fits = true;
            break;
          }
          posY += stepY;
          lTop += stepY;
          lBottom += stepY;
        }

        if (!fits) continue;
        if (lTop < 4 || lBottom > maxBottom) continue;

        const rect = { left: lLeft, top: lTop, right: lRight, bottom: lBottom };
        placedYearRects.push(rect);
        if (placedRects) {
          placedRects.push(rect);
        }

        const el = pool[placedCount];
        if (el._tick !== tick) {
          el.textContent = tick.text;
          el._tick = tick;
        }
        el.style.left = posX + "px";
        el.style.top = posY + "px";
        el.style.display = "block";
        placedCount++;
      }

      for (let i = placedCount; i < pool.length; i++) {
        const el = pool[i];
        if (el.style.display !== "none") {
          el.style.display = "none";
          el._tick = null;
        }
      }
    }

    _updateNodeLabels(placedRects, force = false) {
      const pool = this.nodeLabelPool;
      if (!pool || !pool.length) return;

      if (!this.active && !force) {
        this._hideAllNodeLabels();
        return;
      }

      if (this._loading || !this.positions || !this.positions.length || !this.nodes.length) {
        this._hideAllNodeLabels();
        return;
      }

      const camDist = Math.hypot(
        this.cameraPos[0] - this._lastLabelCamPos[0],
        this.cameraPos[1] - this._lastLabelCamPos[1],
        this.cameraPos[2] - this._lastLabelCamPos[2]
      );
      const angleMoved = Math.abs(this.yaw - this._lastLabelYaw) > 0.005 || Math.abs(this.pitch - this._lastLabelPitch) > 0.005;
      const selectionChanged = this.selectedUid !== this._lastLabelSelectedUid;
      const dataChanged = this.dataId !== this._lastLabelDataId;

      if (!force && camDist <= 1.5 && !angleMoved && !selectionChanged && !dataChanged) {
        // Im Stillstand bestehende Labels unverändert lassen
        return;
      }

      const candidates = this._computeNodeLabels();
      let placedCount = 0;

      for (let i = 0; i < candidates.length && placedCount < MAX_NODE_LABELS && placedCount < pool.length; i++) {
        const cand = candidates[i];
        const el = pool[placedCount];
        const text = cand.node.title || cand.node.uid || '';
        // Reine Breiten-/Höhen-Heuristik ohne DOM-Reflow
        const lw = Math.min(220, Math.max(60, text.length * 7 + 20));
        const lh = 22;

        const lLeft = cand.sx + 8;
        const lRight = lLeft + lw;
        const lTop = cand.sy - lh * 0.5;
        const lBottom = cand.sy + lh * 0.5;

        let collides = false;
        if (placedRects) {
          for (let r = 0; r < placedRects.length; r++) {
            const p = placedRects[r];
            if (!(lRight + 4 < p.left || lLeft > p.right + 4 || lBottom + 4 < p.top || lTop > p.bottom + 4)) {
              collides = true;
              break;
            }
          }
        }
        if (collides) continue;

        if (placedRects) {
          placedRects.push({ left: lLeft, top: lTop, right: lRight, bottom: lBottom });
        }

        // Unnötige DOM-Schreibzugriffe vermeiden: nur bei Knotenwechsel Text und Farbe schreiben
        if (el._node !== cand.node) {
          el.textContent = text;
          if (this._cachedColors) {
            el.style.borderLeftColor = cand.node.type === 'jira'
              ? (this._cachedColors.jiraStr || '#e2740d')
              : (this._cachedColors.confStr || '#1868db');
          }
          el._node = cand.node;
        }
        el.style.left = (cand.sx + 8) + 'px';
        el.style.top = cand.sy + 'px';
        el.style.display = 'block';
        placedCount++;
      }

      for (let i = placedCount; i < pool.length; i++) {
        pool[i].style.display = 'none';
        pool[i]._node = null;
      }

      this._lastLabelCamPos[0] = this.cameraPos[0];
      this._lastLabelCamPos[1] = this.cameraPos[1];
      this._lastLabelCamPos[2] = this.cameraPos[2];
      this._lastLabelYaw = this.yaw;
      this._lastLabelPitch = this.pitch;
      this._lastLabelSelectedUid = this.selectedUid;
      this._lastLabelDataId = this.dataId;
    }
  }

  // ---------------------------------------------------------------- Export

  if (typeof window !== 'undefined') {
    window.KnowledgeGraphFlight = KnowledgeGraphFlight;
  }
  if (typeof global !== 'undefined') {
    global.KnowledgeGraphFlight = KnowledgeGraphFlight;
  }
  KnowledgeGraphFlight.formatClusterBadgeText = formatClusterBadgeText;
  KnowledgeGraphFlight.LABEL_RADIUS = LABEL_RADIUS;
  KnowledgeGraphFlight.MAX_NODE_LABELS = MAX_NODE_LABELS;
  KnowledgeGraphFlight.MAX_NODE_LABEL_POOL = MAX_NODE_LABEL_POOL;
  KnowledgeGraphFlight.MAX_YEAR_LABEL_POOL = MAX_YEAR_LABEL_POOL;
  KnowledgeGraphFlight.computeTimelineTicks = computeTimelineTicks;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      KnowledgeGraphFlight,
      defaultFlightLayout,
      formatClusterBadgeText,
      LABEL_RADIUS,
      MAX_NODE_LABELS,
      MAX_NODE_LABEL_POOL,
      MAX_YEAR_LABEL_POOL,
      computeTimelineTicks,
    };
  }

})(typeof window !== 'undefined' ? window : globalThis);
