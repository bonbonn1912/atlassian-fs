'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { KnowledgeGraphFlight, defaultFlightLayout, formatClusterBadgeText, computeTimelineTicks, MAX_YEAR_LABEL_POOL } = require(path.join(__dirname, '..', 'web', 'graph-flight.js'));

test('defaultFlightLayout handles empty and minimal node datasets correctly', () => {
  const emptyRes = defaultFlightLayout({ nodes: [], edges: [] });
  assert.equal(emptyRes.positions.length, 0);
  assert.equal(emptyRes.clusters.length, 0);
  assert.equal(emptyRes.unknownCount, 0);
  assert.equal(emptyRes.minTime, null);
  assert.equal(emptyRes.maxTime, null);
  assert.equal(emptyRes.depth, 12000);

  const sampleNodes = [
    { uid: 'confluence:100', type: 'confluence', space_key: 'ENG', updated: '2025-01-01T12:00:00Z', title: 'Arch Doc' },
    { uid: 'jira:ENG-1', type: 'jira', project_key: 'ENG', created: '2024-01-01T00:00:00Z', updated: '2025-06-01T00:00:00Z', title: 'Refactor' },
    { uid: 'confluence:200', type: 'confluence', space_key: 'HR', updated: null, title: 'No Date Doc' },
  ];

  const resUpdated = defaultFlightLayout({ nodes: sampleNodes, edges: [] }, { jiraTimeField: 'updated' });
  assert.equal(resUpdated.positions.length, 9); // 3 nodes * 3
  assert.equal(resUpdated.clusters.length, 3); // conf:ENG, jira:ENG, conf:HR
  assert.equal(resUpdated.unknownCount, 1); // confluence:200 has no date

  // Check Z coordinates:
  // confluence:200 (unknown date) must have z = +600
  const z200 = resUpdated.positions[2 * 3 + 2];
  assert.equal(z200, 600);

  // Jira:ENG-1 is newest (2025-06-01), so z = 0
  const zJira = resUpdated.positions[1 * 3 + 2];
  assert.equal(zJira, 0);

  // Confluence:100 is oldest (2025-01-01), so z = -12000
  const zConf = resUpdated.positions[0 * 3 + 2];
  assert.equal(zConf, -12000);

  // Test jiraTimeField: 'created'
  const resCreated = defaultFlightLayout({ nodes: sampleNodes, edges: [] }, { jiraTimeField: 'created' });
  // Now Jira:ENG-1 created 2024-01-01 is oldest (z = -12000), Confluence:100 is newest (z = 0)
  const zJiraCreated = resCreated.positions[1 * 3 + 2];
  const zConfCreated = resCreated.positions[0 * 3 + 2];
  assert.equal(zJiraCreated, -12000);
  assert.equal(zConfCreated, 0);
});

test('100k nodes layout benchmark and coordinate limits', () => {
  const n = 100000;
  const nodes = [];
  const baseTime = Date.parse('2020-01-01T00:00:00Z');
  const span = 5 * 365 * 24 * 3600 * 1000;

  for (let i = 0; i < n; i++) {
    const isJira = i % 2 === 0;
    const spaceOrProj = `SPACE_${i % 25}`;
    const hasDate = i % 10 !== 0; // 10% without date
    const timeIso = hasDate ? new Date(baseTime + (i / n) * span).toISOString() : null;

    nodes.push({
      uid: isJira ? `jira:${spaceOrProj}-${i}` : `confluence:${i}`,
      type: isJira ? 'jira' : 'confluence',
      space_key: !isJira ? spaceOrProj : undefined,
      project_key: isJira ? spaceOrProj : undefined,
      updated: timeIso,
      created: timeIso,
      in_degree: i % 50,
      degree: i % 100,
    });
  }

  const start = performance.now();
  const res = defaultFlightLayout({ nodes, edges: [] }, { jiraTimeField: 'updated' });
  const elapsed = performance.now() - start;

  assert.equal(res.positions.length, 300000);
  assert.equal(res.clusterIndices.length, 100000);
  assert.equal(res.unknownCount, 10000); // 10% of 100k

  // Coordinates check
  let minZ = Infinity, maxZ = -Infinity;
  for (let i = 0; i < n; i++) {
    const z = res.positions[i * 3 + 2];
    assert.ok(Number.isFinite(z));
    if (z < minZ) minZ = z;
    if (z > maxZ) maxZ = z;
  }

  assert.equal(minZ, -12000);
  assert.equal(maxZ, 600);
  assert.ok(elapsed < 2000, `Layout should finish well within 2s, took ${elapsed}ms`);
});

test('KnowledgeGraphFlight API gracefully reports supported=false when WebGL is absent', async () => {
  let statusReport = null;
  const mockCanvas = {
    getContext: () => null, // WebGL unavailable
    addEventListener: () => {},
    removeEventListener: () => {},
  };

  const flight = new KnowledgeGraphFlight({
    canvasEl: mockCanvas,
    onStatus: (st) => { statusReport = st; },
  });

  assert.equal(flight.supported, false);
  assert.equal(flight.active, false);
  assert.ok(statusReport);
  assert.equal(statusReport.message, 'WebGL nicht verfügbar');

  // Methods should not throw even if unsupported
  flight.setActive(true);
  assert.equal(flight.active, false);
  flight.setImportanceMode(true);
  flight.setJiraTimeField('created');
  flight.fitToView();
  flight.zoomIn();
  flight.zoomOut();
  flight.selectNode('test');
  flight.selectEdge('edge1');
  flight.clearSelection();
  flight.cancelPending();
  flight.destroy();
});

test('KnowledgeGraphFlight mock WebGL lifecycle, selection, and interactions', async () => {
  const buffers = new Set();
  const mockGL = {
    isContextLost: () => false,
    createShader: () => ({}),
    shaderSource: () => {},
    compileShader: () => {},
    getShaderParameter: () => true,
    createProgram: () => ({}),
    attachShader: () => {},
    linkProgram: () => {},
    getProgramParameter: () => true,
    getUniformLocation: (p, name) => ({ name }),
    getAttribLocation: (p, name) => 1,
    createBuffer: () => {
      const b = { id: Math.random() };
      buffers.add(b);
      return b;
    },
    deleteBuffer: (b) => buffers.delete(b),
    deleteProgram: () => {},
    bindBuffer: () => {},
    bufferData: () => {},
    useProgram: () => {},
    uniformMatrix4fv: () => {},
    uniform2f: () => {},
    uniform1f: () => {},
    uniform3fv: () => {},
    enableVertexAttribArray: () => {},
    disableVertexAttribArray: () => {},
    vertexAttribPointer: () => {},
    viewport: () => {},
    clearColor: () => {},
    clear: () => {},
    enable: () => {},
    disable: () => {},
    depthFunc: () => {},
    blendFunc: () => {},
    drawArrays: () => {},
  };

  const canvasListeners = new Map();
  const mockCanvas = {
    getContext: () => mockGL,
    addEventListener: (type, fn) => {
      if (!canvasListeners.has(type)) canvasListeners.set(type, []);
      canvasListeners.get(type).push(fn);
    },
    removeEventListener: (type, fn) => {
      const arr = canvasListeners.get(type) || [];
      const idx = arr.indexOf(fn);
      if (idx >= 0) arr.splice(idx, 1);
    },
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    style: {},
    width: 800,
    height: 600,
  };

  let selectedNode = null;
  let activatedNode = null;
  let selectedEdge = null;
  let statusUpdates = [];

  const flight = new KnowledgeGraphFlight({
    canvasEl: mockCanvas,
    onNodeSelect: (n) => { selectedNode = n; },
    onNodeActivate: (n) => { activatedNode = n; },
    onEdgeSelect: (e) => { selectedEdge = e; },
    onStatus: (st) => statusUpdates.push(st),
  });

  assert.equal(flight.supported, true);
  assert.equal(flight.active, false);

  const testData = {
    nodes: [
      { uid: 'jira:PROJ-1', type: 'jira', project_key: 'PROJ', title: 'Task 1', in_degree: 5, state: 'indexed' },
      { uid: 'jira:PROJ-2', type: 'jira', project_key: 'PROJ', title: 'Task 2', in_degree: 1, state: 'indexed' },
      { uid: 'confluence:999', type: 'confluence', space_key: 'DEV', title: 'Design Doc', in_degree: 8, state: 'indexed' },
    ],
    edges: [
      { id: 'e1', source: 'jira:PROJ-1', target: 'jira:PROJ-2', kind: 'jira_relates_to' },
      { id: 'e2', source: 'jira:PROJ-1', target: 'confluence:999', kind: 'mentions_jira' },
    ],
    center: 'jira:PROJ-1',
  };

  await flight.setData(testData);

  assert.equal(flight.nodes.length, 3);
  assert.equal(flight.edges.length, 2);
  assert.equal(flight.nodeMap.size, 3);
  assert.equal(flight.edgeMap.size, 2);

  // Auto-selected center
  assert.equal(flight.selectedUid, 'jira:PROJ-1');
  assert.equal(selectedNode?.uid, 'jira:PROJ-1');
  assert.ok(flight.highlightSet.has('jira:PROJ-2'));
  assert.ok(flight.highlightSet.has('confluence:999'));

  // Test edge selection
  flight.selectEdge('e1');
  assert.equal(flight.selectedEdgeId, 'e1');
  assert.equal(flight.selectedUid, null);
  assert.equal(selectedEdge?.id, 'e1');

  // Test clear selection
  flight.clearSelection();
  assert.equal(flight.selectedUid, null);
  assert.equal(flight.selectedEdgeId, null);
  assert.equal(flight.highlightSet.size, 0);

  // Test setActive(true) and setActive(false)
  flight.setActive(true);
  assert.equal(flight.active, true);

  // Test importanceMode
  flight.setImportanceMode(true);
  assert.equal(flight.importanceMode, true);
  flight.setImportanceMode(false);
  assert.equal(flight.importanceMode, false);

  // Test zoom and fit
  flight.zoomIn();
  flight.zoomOut();
  flight.fitToView();

  flight.setActive(false);
  assert.equal(flight.active, false);

  // Test WebGL context loss handling
  const contextLostHandler = canvasListeners.get('webglcontextlost')?.[0];
  const contextRestoredHandler = canvasListeners.get('webglcontextrestored')?.[0];
  assert.ok(contextLostHandler);
  assert.ok(contextRestoredHandler);

  let prevented = false;
  contextLostHandler({ preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(flight.contextLost, true);

  contextRestoredHandler();
  assert.equal(flight.contextLost, false);

  // Test destroy
  flight.destroy();
  assert.equal(flight.active, false);
  assert.equal(flight.nodes.length, 0);
  assert.equal(flight.nodeMap.size, 0);
});

test('Flight physics update handles velocity damping and movement', () => {
  const mockCanvas = {
    getContext: () => ({
      isContextLost: () => false,
      createShader: () => ({}),
      shaderSource: () => {},
      compileShader: () => {},
      getShaderParameter: () => true,
      createProgram: () => ({}),
      attachShader: () => {},
      linkProgram: () => {},
      getProgramParameter: () => true,
      getUniformLocation: () => ({}),
      getAttribLocation: () => 1,
      createBuffer: () => ({}),
      deleteBuffer: () => {},
      deleteProgram: () => {},
      bindBuffer: () => {},
      bufferData: () => {},
      useProgram: () => {},
      uniformMatrix4fv: () => {},
      uniform2f: () => {},
      uniform1f: () => {},
      uniform3fv: () => {},
      enableVertexAttribArray: () => {},
      disableVertexAttribArray: () => {},
      vertexAttribPointer: () => {},
      viewport: () => {},
      clearColor: () => {},
      clear: () => {},
      enable: () => {},
      disable: () => {},
      depthFunc: () => {},
      blendFunc: () => {},
      drawArrays: () => {},
    }),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    style: {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  flight.setActive(true);

  const initialZ = flight.cameraPos[2];

  // Simulate pressing 'KeyW' (forward)
  flight.keys['KeyW'] = true;
  flight._updatePhysics(0.1);

  // Camera should have moved forward (-Z in camera space when yaw=0, pitch=0)
  assert.ok(flight.cameraPos[2] < initialZ, 'Camera should move deeper forward along -Z');

  // Release key and test damping
  delete flight.keys['KeyW'];
  const velBefore = flight.velocity[2];
  flight._updatePhysics(0.1);
  const velAfter = flight.velocity[2];
  assert.ok(Math.abs(velAfter) < Math.abs(velBefore), 'Velocity should dampen over time');

  flight.destroy();
});

test('External window.KnowledgeGraphFlightLayout integration is utilized if available', async () => {
  let customLayoutCalled = false;

  global.KnowledgeGraphFlightLayout = {
    build(data, opts) {
      customLayoutCalled = true;
      return {
        positions: new Float32Array(data.nodes.length * 3),
        clusters: [{ id: 'custom', label: 'Custom', x: 0, y: 0, radius: 100, count: data.nodes.length }],
        clusterIndices: new Uint32Array(data.nodes.length),
        timestamps: new Float64Array(data.nodes.length),
        minTime: null,
        maxTime: null,
        unknownCount: 0,
        depth: 12000,
      };
    },
  };

  const mockCanvas = {
    getContext: () => null,
    addEventListener: () => {},
    removeEventListener: () => {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  await flight.setData({
    nodes: [{ uid: 'node-1', type: 'jira', title: 'Test' }],
    edges: [],
  });

  assert.equal(customLayoutCalled, true, 'Should invoke external KnowledgeGraphFlightLayout.build');
  delete global.KnowledgeGraphFlightLayout;
});

test('_isInputFocused avoids hijacking keys when user is typing in forms', () => {
  const mockCanvas = {
    getContext: () => null,
    addEventListener: () => {},
    removeEventListener: () => {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });

  // In test environment without document, returns false
  assert.equal(flight._isInputFocused(), false);

  // Mock global document with active input element
  global.document = {
    activeElement: { tagName: 'INPUT' },
  };
  assert.equal(flight._isInputFocused(), true);

  global.document = {
    activeElement: { tagName: 'TEXTAREA' },
  };
  assert.equal(flight._isInputFocused(), true);

  global.document = {
    activeElement: { tagName: 'SELECT' },
  };
  assert.equal(flight._isInputFocused(), true);

  global.document = {
    activeElement: { tagName: 'DIV', isContentEditable: true },
  };
  assert.equal(flight._isInputFocused(), true);

  global.document = {
    activeElement: { tagName: 'DIV', isContentEditable: false },
  };
  assert.equal(flight._isInputFocused(), false);

  delete global.document;
});

test('Direct integration between KnowledgeGraphFlight and web/graph-flight-layout.js', async () => {
  let layoutModule;
  try {
    layoutModule = require('../web/graph-flight-layout.js');
  } catch (_) {
    return;
  }

  global.KnowledgeGraphFlightLayout = layoutModule;

  const mockCanvas = {
    getContext: () => null,
    addEventListener: () => {},
    removeEventListener: () => {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  await flight.setData({
    nodes: [
      { uid: 'jira:TEST-1', type: 'jira', updated: '2025-01-01T00:00:00Z', created: '2024-01-01T00:00:00Z', project_key: 'TEST' },
      { uid: 'confluence:1', type: 'confluence', updated: '2025-02-01T00:00:00Z', space_key: 'TEST' },
    ],
    edges: [
      { source: 'jira:TEST-1', target: 'confluence:1', kind: 'mentions_jira' },
    ],
  });

  assert.equal(flight.nodes.length, 2);
  assert.equal(flight.positions.length, 6);
  assert.ok(flight.positions[2] < 0 || flight.positions[2] === 0);

  delete global.KnowledgeGraphFlightLayout;
});

test("defaultFlightLayout disambiguates colliding cluster labels", () => {
  const data = {
    nodes: [
      { uid: "confluence:page1", type: "confluence", space_key: "KAFKA", updated: "2025-01-01T00:00:00Z" },
      { uid: "jira:KAFKA-1", type: "jira", project_key: "KAFKA", updated: "2025-01-01T00:00:00Z" },
      { uid: "confluence:page2", type: "confluence", space_key: "UNIQUE", updated: "2025-01-01T00:00:00Z" },
    ],
  };

  const res = defaultFlightLayout(data);
  const kafkaSpace = res.clusters.find(c => c.id === "conf:KAFKA");
  const kafkaProj = res.clusters.find(c => c.id === "jira:KAFKA");
  const uniqueSpace = res.clusters.find(c => c.id === "conf:UNIQUE");

  assert.ok(kafkaSpace);
  assert.ok(kafkaProj);
  assert.ok(uniqueSpace);
  assert.equal(kafkaSpace.label, "KAFKA (Space)");
  assert.equal(kafkaProj.label, "KAFKA (Projekt)");
  assert.equal(uniqueSpace.label, "UNIQUE");
});

test("setJiraTimeField refreshes in-scene timeline HUD label and requests redraw without camera input", async () => {
  const mockCanvas = {
    getContext: () => ({
      isContextLost: () => false,
      createShader: () => ({}),
      shaderSource: () => {},
      compileShader: () => {},
      getShaderParameter: () => true,
      createProgram: () => ({}),
      attachShader: () => {},
      linkProgram: () => {},
      getProgramParameter: () => true,
      getUniformLocation: () => ({}),
      getAttribLocation: () => 1,
      createBuffer: () => ({}),
      deleteBuffer: () => {},
      deleteProgram: () => {},
      bindBuffer: () => {},
      bufferData: () => {},
      useProgram: () => {},
      uniformMatrix4fv: () => {},
      uniform2f: () => {},
      uniform1f: () => {},
      uniform3fv: () => {},
      enableVertexAttribArray: () => {},
      viewport: () => {},
      clearColor: () => {},
      clear: () => {},
      enable: () => {},
      disable: () => {},
      depthFunc: () => {},
      blendFunc: () => {},
      drawArrays: () => {},
    }),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    style: {},
  };

  const hud = { textContent: "", style: {} };
  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  flight.timelineHudEl = hud;
  flight.overlayEl = {};

  await flight.setData({
    nodes: [
      { uid: "jira:T-1", type: "jira", created: "2021-05-15T12:00:00Z", updated: "2024-11-20T12:00:00Z", project_key: "T" },
      { uid: "jira:T-2", type: "jira", created: "2020-01-10T12:00:00Z", updated: "2023-08-01T12:00:00Z", project_key: "T" },
    ],
    edges: [],
  });

  flight.cameraPos = [0, 0, 0];
  flight._updateOverlay();
  const labelUpdated = hud.textContent;
  assert.ok(labelUpdated.includes("2024"), `Initial label should reflect updated time: ${labelUpdated}`);

  // Switch to created field: must refresh timeline HUD immediately without camera movement
  flight._needRedraw = false;
  flight.setJiraTimeField("created");

  assert.equal(flight.jiraTimeField, "created");
  assert.equal(flight._needRedraw, true, "requestDraw must have been invoked");
  const labelCreated = hud.textContent;
  assert.ok(labelCreated.includes("2021"), `Refreshed label should immediately reflect created time without camera input: ${labelCreated}`);
  assert.notEqual(labelCreated, labelUpdated);

  flight.destroy();
});

test("fitToView computes camera position and dynamic far-plane to fit large cluster arrangements without clipping", () => {
  const mockCanvas = {
    getContext: () => ({
      isContextLost: () => false,
      createShader: () => ({}),
      shaderSource: () => {},
      compileShader: () => {},
      getShaderParameter: () => true,
      createProgram: () => ({}),
      attachShader: () => {},
      linkProgram: () => {},
      getProgramParameter: () => true,
      getUniformLocation: () => ({}),
      getAttribLocation: () => 1,
      createBuffer: () => ({}),
      deleteBuffer: () => {},
      deleteProgram: () => {},
      bindBuffer: () => {},
      bufferData: () => {},
      useProgram: () => {},
      uniformMatrix4fv: () => {},
      uniform2f: () => {},
      uniform1f: () => {},
      uniform3fv: () => {},
      enableVertexAttribArray: () => {},
      viewport: () => {},
      clearColor: () => {},
      clear: () => {},
      enable: () => {},
      disable: () => {},
      depthFunc: () => {},
      blendFunc: () => {},
      drawArrays: () => {},
    }),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 1600, height: 900, left: 0, top: 0 }),
    style: {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  flight.width = 1600;
  flight.height = 900;

  // Synthetic clusters spread ±15,000 units in X/Y with depth range 12,000
  flight.clusters = [
    { id: "space:ALPHA", label: "ALPHA", x: -14000, y: -13000, radius: 1000, count: 5000 },
    { id: "jira:BETA", label: "BETA", x: 14000, y: 13000, radius: 1200, count: 6000 },
    { id: "space:GAMMA", label: "GAMMA", x: 0, y: 0, radius: 800, count: 2000 },
  ];
  flight.nodes = new Array(13000);
  flight.sceneDepth = 12000;
  flight.unknownCount = 50;

  flight.fitToView();

  const bounds = flight.sceneBounds;
  assert.ok(bounds, "sceneBounds should be calculated");
  assert.ok(bounds.minX <= -15000);
  assert.ok(bounds.maxX >= 15000);
  assert.ok(bounds.minY <= -14000);
  assert.ok(bounds.maxY >= 14000);
  assert.equal(bounds.minZ, -12000);
  assert.equal(bounds.maxZ, 600);

  // Camera should be positioned back far enough to view the entire span
  assert.ok(flight.cameraPos[2] > 20000, `Camera Z (${flight.cameraPos[2]}) must provide full overview`);

  // Far plane must expand to encompass the entire scene depth from the camera position
  const far = flight.getFarPlane();
  assert.ok(far > 40000, `Far plane (${far}) must expand to prevent clipping overview`);
  assert.ok(far >= 35000.0, "Far plane must never fall below baseline 35000");

  flight.destroy();
});

test("Adaptive forward/back travel: dolly step scales with distance to scene bounds and clamps", () => {
  const mockCanvas = {
    getContext: () => ({
      isContextLost: () => false,
      createShader: () => ({}),
      shaderSource: () => {},
      compileShader: () => {},
      getShaderParameter: () => true,
      createProgram: () => ({}),
      attachShader: () => {},
      linkProgram: () => {},
      getProgramParameter: () => true,
      getUniformLocation: () => ({}),
      getAttribLocation: () => 1,
      createBuffer: () => ({}),
      deleteBuffer: () => {},
      deleteProgram: () => {},
      bindBuffer: () => {},
      bufferData: () => {},
      useProgram: () => {},
      uniformMatrix4fv: () => {},
      uniform2f: () => {},
      uniform1f: () => {},
      uniform3fv: () => {},
      enableVertexAttribArray: () => {},
      viewport: () => {},
      clearColor: () => {},
      clear: () => {},
      enable: () => {},
      disable: () => {},
      depthFunc: () => {},
      blendFunc: () => {},
      drawArrays: () => {},
    }),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    style: {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  flight.sceneBounds = {
    minX: -5000, maxX: 5000,
    minY: -5000, maxY: 5000,
    minZ: -12000, maxZ: 600,
    cx: 0, cy: 0,
  };

  // 1. Inside scene bounds (dist = 0): step must be floorStep (Math.max(600, 12000 * 0.05) = 600)
  flight.cameraPos = [0, 0, 0];
  assert.equal(flight._distanceToSceneBounds(), 0);
  assert.equal(flight.getDollyStep(), 600);

  // 2. Intermediate distance (dist = 10000): dist * 0.28 = 2800
  flight.cameraPos = [0, 0, 10600];
  assert.equal(flight._distanceToSceneBounds(), 10000);
  assert.ok(Math.abs(flight.getDollyStep() - 2800) < 1e-4);

  // 3. Far from scene bounds (overview at Z = 30600 -> dist = 30000):
  flight.cameraPos = [0, 0, 30600];
  assert.equal(flight._distanceToSceneBounds(), 30000);
  // dist * 0.28 = 8400, clamped to 8000
  assert.equal(flight.getDollyStep(), 8000);

  // 4. Test zoomIn and zoomOut at overview: camera moves by adaptive step (8000)
  flight.cameraPos = [0, 0, 30600];
  flight.yaw = 0;
  flight.pitch = 0;
  flight.zoomIn();
  // Moving forward in camera space with yaw=0, pitch=0 decreases Z
  assert.equal(flight.cameraPos[2], 30600 - 8000);

  // zoomOut symmetric: moves back by step computed at current distance
  const currentStep = flight.getDollyStep();
  flight.zoomOut();
  assert.equal(flight.cameraPos[2], (30600 - 8000) + currentStep);

  flight.destroy();
});

test("Adaptive travel: target flight speed scales with distance to scene bounds and clamps", () => {
  const mockCanvas = {
    getContext: () => ({
      isContextLost: () => false,
      createShader: () => ({}),
      shaderSource: () => {},
      compileShader: () => {},
      getShaderParameter: () => true,
      createProgram: () => ({}),
      attachShader: () => {},
      linkProgram: () => {},
      getProgramParameter: () => true,
      getUniformLocation: () => ({}),
      getAttribLocation: () => 1,
      createBuffer: () => ({}),
      deleteBuffer: () => {},
      deleteProgram: () => {},
      bindBuffer: () => {},
      bufferData: () => {},
      useProgram: () => {},
      uniformMatrix4fv: () => {},
      uniform2f: () => {},
      uniform1f: () => {},
      uniform3fv: () => {},
      enableVertexAttribArray: () => {},
      viewport: () => {},
      clearColor: () => {},
      clear: () => {},
      enable: () => {},
      disable: () => {},
      depthFunc: () => {},
      blendFunc: () => {},
      drawArrays: () => {},
    }),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    style: {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  flight.sceneBounds = {
    minX: -5000, maxX: 5000,
    minY: -5000, maxY: 5000,
    minZ: -12000, maxZ: 600,
    cx: 0, cy: 0,
  };

  // 1. Inside scene bounds (dist = 0): speed must be exactly 1800 (close-up unchanged)
  flight.cameraPos = [0, 0, -1000];
  assert.equal(flight._distanceToSceneBounds(), 0);
  assert.equal(flight.getFlightSpeed(), 1800);

  // 2. Intermediate distance (dist = 4000): 1800 * (1 + 4000/4000) = 3600
  flight.cameraPos = [0, 0, 4600];
  assert.equal(flight._distanceToSceneBounds(), 4000);
  assert.equal(flight.getFlightSpeed(), 3600);

  // 3. Overview distance (dist = 31000): 1800 * (1 + 31000/4000) = 15750 -> clamped to 15000
  flight.cameraPos = [0, 0, 31600];
  assert.equal(flight._distanceToSceneBounds(), 31000);
  assert.equal(flight.getFlightSpeed(), 15000);

  // 4. Verify physics simulation uses adaptive speed when flying forward
  flight.setActive(true);
  flight.cameraPos = [0, 0, 31600];
  flight.keys["KeyW"] = true;
  flight._updatePhysics(0.1);
  // Velocity along Z should be accelerating toward -15000 (negative Z = forward)
  assert.ok(flight.velocity[2] < -1000, "Velocity should accelerate toward adaptive target speed 15000");

  flight.destroy();
});

test("Badge text polish: single-parenthesis format on collision, standard format without", () => {
  // Colliding cluster labels
  assert.equal(formatClusterBadgeText({ label: "KAFKA (Space)", count: 1426 }), "KAFKA (Space · 1426)");
  assert.equal(formatClusterBadgeText({ label: "KAFKA (Projekt)", count: 850 }), "KAFKA (Projekt · 850)");
  assert.equal(formatClusterBadgeText({ label: "SONSTIGES (Sonstige)", count: 12 }), "SONSTIGES (Sonstige · 12)");

  // Standard non-colliding cluster labels
  assert.equal(formatClusterBadgeText({ label: "HADOOP", count: 776 }), "HADOOP (776)");
  assert.equal(formatClusterBadgeText({ label: "DEV", count: 1 }), "DEV (1)");
  assert.equal(formatClusterBadgeText(null), "");
});

test("Modified keydown passes through without preventDefault, unmodified W is preventDefaulted", () => {
  const mockCanvas = {
    getContext: () => ({
      isContextLost: () => false,
      createShader: () => ({}),
      shaderSource: () => {},
      compileShader: () => {},
      getShaderParameter: () => true,
      createProgram: () => ({}),
      attachShader: () => {},
      linkProgram: () => {},
      getProgramParameter: () => true,
      getUniformLocation: () => ({}),
      getAttribLocation: () => 1,
      createBuffer: () => ({}),
      deleteBuffer: () => {},
      deleteProgram: () => {},
      bindBuffer: () => {},
      bufferData: () => {},
      useProgram: () => {},
      uniformMatrix4fv: () => {},
      uniform2f: () => {},
      uniform1f: () => {},
      uniform3fv: () => {},
      enableVertexAttribArray: () => {},
      viewport: () => {},
      clearColor: () => {},
      clear: () => {},
      enable: () => {},
      disable: () => {},
      depthFunc: () => {},
      blendFunc: () => {},
      drawArrays: () => {},
    }),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    style: {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  flight.setActive(true);

  // Unmodified KeyW -> preventDefault must be called, keys[KeyW] set
  let prevented = false;
  flight._handleKeyDown({
    code: "KeyW",
    key: "w",
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    preventDefault: () => { prevented = true; },
  });
  assert.equal(prevented, true, "Unmodified W must be preventDefaulted for flight navigation");
  assert.equal(flight.keys["KeyW"], true);

  // Modified shortcuts: Cmd+W, Ctrl+A, Alt+ArrowUp, Cmd+0, Ctrl+S
  const modifiedEvents = [
    { code: "KeyW", key: "w", metaKey: true },
    { code: "KeyA", key: "a", ctrlKey: true },
    { code: "ArrowUp", key: "ArrowUp", altKey: true },
    { code: "Digit0", key: "0", metaKey: true },
    { code: "KeyS", key: "s", ctrlKey: true },
  ];

  for (const ev of modifiedEvents) {
    let modPrevented = false;
    flight.keys = Object.create(null);
    flight._handleKeyDown({
      code: ev.code,
      key: ev.key,
      ctrlKey: Boolean(ev.ctrlKey),
      metaKey: Boolean(ev.metaKey),
      altKey: Boolean(ev.altKey),
      preventDefault: () => { modPrevented = true; },
    });
    assert.equal(modPrevented, false, `Modified key event (${ev.code}) must not be preventDefaulted`);
    assert.equal(flight.keys[ev.code], undefined, `Modified key event (${ev.code}) must not register flight movement`);
  }

  flight.destroy();
});

test("Mouse-wheel dolly scales with distance to scene bounds and clamping", () => {
  const mockCanvas = {
    getContext: () => ({
      isContextLost: () => false,
      createShader: () => ({}),
      shaderSource: () => {},
      compileShader: () => {},
      getShaderParameter: () => true,
      createProgram: () => ({}),
      attachShader: () => {},
      linkProgram: () => {},
      getProgramParameter: () => true,
      getUniformLocation: () => ({}),
      getAttribLocation: () => 1,
      createBuffer: () => ({}),
      deleteBuffer: () => {},
      deleteProgram: () => {},
      bindBuffer: () => {},
      bufferData: () => {},
      useProgram: () => {},
      uniformMatrix4fv: () => {},
      uniform2f: () => {},
      uniform1f: () => {},
      uniform3fv: () => {},
      enableVertexAttribArray: () => {},
      viewport: () => {},
      clearColor: () => {},
      clear: () => {},
      enable: () => {},
      disable: () => {},
      depthFunc: () => {},
      blendFunc: () => {},
      drawArrays: () => {},
    }),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    style: {},
  };

  const flight = new KnowledgeGraphFlight({ canvasEl: mockCanvas });
  flight.setActive(true);
  flight.sceneBounds = {
    minX: -5000, maxX: 5000,
    minY: -5000, maxY: 5000,
    minZ: -12000, maxZ: 600,
    cx: 0, cy: 0,
  };
  flight.yaw = 0;
  flight.pitch = 0;

  // 1. Inside scene bounds (dist = 0): step = 600
  // moveDist per notch (delta = 100): 600 * 0.3 * (100 / 100) = 180 units
  flight.cameraPos = [0, 0, 0];
  let prevented = false;
  flight._handleWheel({
    deltaY: 100,
    deltaMode: 0,
    preventDefault: () => { prevented = true; },
  });
  assert.equal(prevented, true, "Wheel event must be preventDefaulted");
  // Scrolling down (deltaY > 0) moves camera back along Z (increases Z)
  assert.equal(flight.cameraPos[2], 180);

  // 2. Far from scene bounds (overview at Z = 30600 -> dist = 30000):
  // step = clamp(30000 * 0.28, 600, 8000) = 8000
  // moveDist per notch (delta = 100): 8000 * 0.3 * (100 / 100) = 2400 units
  flight.cameraPos = [0, 0, 30600];
  prevented = false;
  flight._handleWheel({
    deltaY: -100, // Zooming forward / in
    deltaMode: 0,
    preventDefault: () => { prevented = true; },
  });
  assert.equal(prevented, true);
  assert.equal(flight.cameraPos[2], 30600 - 2400);

  flight.destroy();
});

test("Badge suppression: two clusters projecting to the same screen position yield one visible badge", () => {
  const overlayChildren = [];
  const mockOverlay = {
    appendChild: (el) => overlayChildren.push(el),
    querySelector: () => null,
  };
  const mockContainer = {
    querySelector: (sel) => (sel === ".graph-flight-overlay" ? mockOverlay : null),
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
  };
  const mockCanvas = {
    getContext: () => ({
      isContextLost: () => false,
      createShader: () => ({}),
      shaderSource: () => {},
      compileShader: () => {},
      getShaderParameter: () => true,
      createProgram: () => ({}),
      attachShader: () => {},
      linkProgram: () => {},
      getProgramParameter: () => true,
      getUniformLocation: () => ({}),
      getAttribLocation: () => 1,
      createBuffer: () => ({}),
      deleteBuffer: () => {},
      deleteProgram: () => {},
      bindBuffer: () => {},
      bufferData: () => {},
      useProgram: () => {},
      uniformMatrix4fv: () => {},
      uniform2f: () => {},
      uniform1f: () => {},
      uniform3fv: () => {},
      enableVertexAttribArray: () => {},
      viewport: () => {},
      clearColor: () => {},
      clear: () => {},
      enable: () => {},
      disable: () => {},
      depthFunc: () => {},
      blendFunc: () => {},
      drawArrays: () => {},
    }),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    width: 800,
    height: 600,
    style: {},
  };

  const flight = new KnowledgeGraphFlight({
    containerEl: mockContainer,
    canvasEl: mockCanvas,
  });
  flight.width = 800;
  flight.height = 600;
  flight.overlayEl = mockOverlay;

  // Create badge pool elements
  flight.clusterLabelPool = [];
  for (let i = 0; i < 5; i++) {
    flight.clusterLabelPool.push({
      style: {},
      offsetWidth: 110,
      offsetHeight: 28,
      textContent: "",
    });
  }

  // Two clusters that project to the identical spatial XY center (0, 0)
  flight.clusters = [
    { id: "space:ALPHA", label: "ALPHA", x: 0, y: 0, count: 100 },
    { id: "jira:BETA", label: "BETA", x: 0, y: 0, count: 200 },
  ];

  // Set camera and render to compute viewProjectionMatrix
  flight.cameraPos = [0, 0, 5000];
  flight.yaw = 0;
  flight.pitch = 0;
  flight._render();

  // Run overlay update
  flight._updateOverlay();

  // Exactly one badge should be visible (display === 'block'), the other suppressed ('none')
  const visibleBadges = flight.clusterLabelPool.filter((b) => b.style.display === "block");
  const hiddenBadges = flight.clusterLabelPool.filter((b) => b.style.display === "none");

  assert.equal(visibleBadges.length, 1, "Overlapping clusters must yield exactly 1 visible badge");
  assert.equal(visibleBadges[0].textContent, "ALPHA (100)");
  assert.equal(hiddenBadges.length, 4, "Remaining badges must be hidden");

  flight.destroy();
});


function createMockCanvas() {
  const gl = {
    isContextLost: () => false,
    createShader: () => ({}),
    shaderSource: () => {},
    compileShader: () => {},
    getShaderParameter: () => true,
    createProgram: () => ({}),
    attachShader: () => {},
    linkProgram: () => {},
    getProgramParameter: () => true,
    getUniformLocation: () => ({}),
    getAttribLocation: () => 1,
    createBuffer: () => ({}),
    deleteBuffer: () => {},
    deleteProgram: () => {},
    bindBuffer: () => {},
    bufferData: () => {},
    useProgram: () => {},
    uniformMatrix4fv: () => {},
    uniform2f: () => {},
    uniform1f: () => {},
    uniform3fv: () => {},
    enableVertexAttribArray: () => {},
    disableVertexAttribArray: () => {},
    vertexAttribPointer: () => {},
    viewport: () => {},
    clearColor: () => {},
    clear: () => {},
    enable: () => {},
    disable: () => {},
    depthFunc: () => {},
    blendFunc: () => {},
    drawArrays: () => {},
  };
  return {
    getContext: () => gl,
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    width: 800,
    height: 600,
    style: {},
  };
}

test("proximity node labels: computation returns at most MAX_NODE_LABELS entries, sorted nearest-first, within LABEL_RADIUS and in front of camera", async () => {
  const mockCanvas = createMockCanvas();
  const mockContainer = {
    querySelector: () => null,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
  };

  const flight = new KnowledgeGraphFlight({
    containerEl: mockContainer,
    canvasEl: mockCanvas,
  });
  flight.width = 800;
  flight.height = 600;
  flight.cameraPos = [0, 0, 0];
  flight.yaw = 0;
  flight.pitch = 0;

  // Create 15 nodes:
  // 12 nodes in front of camera within LABEL_RADIUS (350), at distances 50, 70, 90, ..., 270
  // 2 nodes farther than LABEL_RADIUS: distance 450 and 600
  // 1 node behind camera: distance 100 (z = +100 when camera looks towards -z)
  const nodes = [];
  const positions = [];

  for (let i = 0; i < 12; i++) {
    const dist = 50 + i * 20; // 50 to 270
    nodes.push({ uid: `node-near-${i}`, title: `Near Title ${i}`, type: i % 2 === 0 ? "confluence" : "jira", idx: i });
    positions.push(0, 0, -dist);
  }
  // Far nodes (dist > LABEL_RADIUS = 900)
  nodes.push({ uid: "node-far-1", title: "Far 1", type: "confluence", idx: 12 });
  positions.push(0, 0, -1050);
  nodes.push({ uid: "node-far-2", title: "Far 2", type: "jira", idx: 13 });
  positions.push(0, 0, -1200);
  // Behind camera (z = +100, camera at 0, facing -z => w <= 0.1)
  nodes.push({ uid: "node-behind", title: "Behind", type: "confluence", idx: 14 });
  positions.push(0, 0, 100);

  flight.nodes = nodes;
  flight.positions = new Float32Array(positions);
  flight.nodeMap = new Map(nodes.map(n => [n.uid, n]));
  flight._buildNodeSpatialGrid();

  const labels = flight._computeNodeLabels();

  // (1) At most MAX_NODE_LABELS (8) entries
  assert.ok(labels.length <= KnowledgeGraphFlight.MAX_NODE_LABELS, "At most MAX_NODE_LABELS entries");
  assert.equal(labels.length, KnowledgeGraphFlight.MAX_NODE_LABELS, "Exactly MAX_NODE_LABELS when > 8 candidates qualify");

  // (2) Sorted nearest-first
  for (let i = 0; i < labels.length - 1; i++) {
    assert.ok(labels[i].distance <= labels[i + 1].distance, `Nearest first: ${labels[i].distance} <= ${labels[i + 1].distance}`);
  }

  // (3) Only nodes within LABEL_RADIUS (350)
  for (const l of labels) {
    assert.ok(l.distance <= KnowledgeGraphFlight.LABEL_RADIUS, `Within radius: ${l.distance} <= ${KnowledgeGraphFlight.LABEL_RADIUS}`);
  }

  // (4) Only nodes in front of the camera (clipW > 0.1)
  for (const l of labels) {
    assert.ok(l.clipW > 0.1, "Clip w > 0.1 (in front of camera)");
  }

  // (5) Far and behind nodes are never included
  assert.ok(!labels.some(l => l.node.uid === "node-far-1"), "Far node 1 excluded");
  assert.ok(!labels.some(l => l.node.uid === "node-far-2"), "Far node 2 excluded");
  assert.ok(!labels.some(l => l.node.uid === "node-behind"), "Behind node excluded");

  // (6) Selected node is excluded from proximity labels (tooltip covers selection, no duplicate rendering)
  flight.selectedUid = "node-near-0";
  const labelsWithSel = flight._computeNodeLabels();
  assert.equal(labelsWithSel.length, KnowledgeGraphFlight.MAX_NODE_LABELS);
  assert.ok(!labelsWithSel.some(l => l.node.uid === "node-near-0"), "Selected node excluded to prevent duplicate rendering with tooltip");
  // Check sorting is preserved
  for (let i = 0; i < labelsWithSel.length - 1; i++) {
    assert.ok(labelsWithSel[i].distance <= labelsWithSel[i + 1].distance, "Sorted nearest-first");
  }

  flight.destroy();
});

test("proximity node labels: moving the camera close to a different node changes the selected label set", async () => {
  const mockCanvas = createMockCanvas();
  const mockContainer = {
    querySelector: () => null,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
  };

  const flight = new KnowledgeGraphFlight({
    containerEl: mockContainer,
    canvasEl: mockCanvas,
  });
  flight.width = 800;
  flight.height = 600;
  flight.yaw = 0;
  flight.pitch = 0;

  // Node Alpha at (0, 0, -100)
  // Node Beta at (2000, 0, -100)
  const nodes = [
    { uid: "node-alpha", title: "Alpha Space Home", type: "confluence", idx: 0 },
    { uid: "node-beta", title: "Beta Project Board", type: "jira", idx: 1 },
  ];
  const positions = [
    0, 0, -100,
    2000, 0, -100,
  ];

  flight.nodes = nodes;
  flight.positions = new Float32Array(positions);
  flight.nodeMap = new Map(nodes.map(n => [n.uid, n]));
  flight._buildNodeSpatialGrid();

  // Position 1: Camera at (0, 0, 0)
  flight.cameraPos = [0, 0, 0];
  const labelsAtAlpha = flight._computeNodeLabels();
  assert.equal(labelsAtAlpha.length, 1);
  assert.equal(labelsAtAlpha[0].node.uid, "node-alpha", "Alpha is in range at camera (0,0,0)");

  // Position 2: Move camera to (2000, 0, 0)
  flight.cameraPos = [2000, 0, 0];
  const labelsAtBeta = flight._computeNodeLabels();
  assert.equal(labelsAtBeta.length, 1);
  assert.equal(labelsAtBeta[0].node.uid, "node-beta", "Beta is in range at camera (2000,0,0)");
  assert.notEqual(labelsAtAlpha[0].node.uid, labelsAtBeta[0].node.uid, "Label set changed after moving camera");

  flight.destroy();
});

test("proximity node labels: overlay integration pool size, visibility cap, and lifecycle hiding on setActive(false)", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    children: overlayChildren,
    appendChild: (el) => overlayChildren.push(el),
    removeChild: (el) => {
      const idx = overlayChildren.indexOf(el);
      if (idx !== -1) overlayChildren.splice(idx, 1);
    },
    querySelector: () => null,
    querySelectorAll: (sel) => {
      if (sel === ".graph-flight-node-label") {
        return overlayChildren.filter(c => c.className === "graph-flight-node-label");
      }
      return [];
    },
  };
  const mockContainer = {
    querySelector: (sel) => (sel === ".graph-flight-overlay" ? mockOverlay : null),
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    appendChild: () => {},
  };

  // Mock global document with DOM creation methods
  global.document = {
    createElement: (tag) => ({
      tagName: tag.toUpperCase(),
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;

    // Verify bounded DOM pool size: exactly 10 node label elements created
    assert.equal(flight.nodeLabelPool.length, 10, "Node label pool must have exactly 10 elements");
    const domLabels = mockOverlay.querySelectorAll(".graph-flight-node-label");
    assert.equal(domLabels.length, 10, "At most 10 .graph-flight-node-label elements exist in the DOM");

    // All pool elements initially hidden
    assert.ok(flight.nodeLabelPool.every(el => el.style.display === "none"), "All pool elements initially hidden");

    flight.setActive(true);

    // Prepare 12 nodes within 50..270 units in front of the camera
    const testNodes = [];
    for (let i = 0; i < 12; i++) {
      testNodes.push({
        uid: `test-node-${i}`,
        title: `Document ${i}`,
        type: i % 2 === 0 ? "confluence" : "jira",
        updated_at: new Date(2025, 0, 1 + i).toISOString(),
      });
    }

    await flight.setData({ nodes: testNodes, edges: [] });

    // Set camera position to origin, looking towards -z
    flight.cameraPos = [0, 0, 0];
    flight.yaw = 0;
    flight.pitch = 0;

    // Set positions near camera with slight offsets to prevent screen-space collisions
    const positions = [];
    for (let i = 0; i < 12; i++) {
      positions.push((i - 6) * 40, (i % 3 - 1) * 30, -(50 + i * 20));
    }
    flight.positions = new Float32Array(positions);
    flight._buildNodeSpatialGrid();

    // Force a label pass
    flight._updateOverlay(true);

    // Check pool and visibility
    assert.equal(flight.nodeLabelPool.length, 10, "Pool size stays exactly 10 elements");
    const visibleLabels = flight.nodeLabelPool.filter(el => el.style.display === "block");
    const hiddenLabels = flight.nodeLabelPool.filter(el => el.style.display === "none");

    assert.ok(visibleLabels.length > 0, "Labels are visible near camera");
    assert.ok(visibleLabels.length <= 8, `At most 8 visible labels (got ${visibleLabels.length})`);
    assert.equal(visibleLabels.length + hiddenLabels.length, 10, "Total pool count remains 10");

    // textContent matches node titles
    for (const el of visibleLabels) {
      assert.ok(el.textContent.startsWith("Document "), `textContent matches title: ${el.textContent}`);
    }

    // After calling setActive(false), all node labels are hidden
    flight.setActive(false);
    const visibleAfterDeactivate = flight.nodeLabelPool.filter(el => el.style.display !== "none");
    assert.equal(visibleAfterDeactivate.length, 0, "All node labels must be hidden after setActive(false)");

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("proximity node labels: collision suppression prevents overlapping labels", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    children: overlayChildren,
    appendChild: (el) => overlayChildren.push(el),
    removeChild: () => {},
    querySelector: () => null,
  };
  const mockContainer = {
    querySelector: (sel) => (sel === ".graph-flight-overlay" ? mockOverlay : null),
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag.toUpperCase(),
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;
    flight.cameraPos = [0, 0, 0];
    flight.yaw = 0;
    flight.pitch = 0;
    flight.setActive(true);

    // Two nodes at the exact same screen position (0, 0, -100) and (0, 0, -105)
    flight.nodes = [
      { uid: "node-1", title: "Overlap 1", type: "confluence", idx: 0 },
      { uid: "node-2", title: "Overlap 2", type: "jira", idx: 1 },
    ];
    flight.positions = new Float32Array([
      0, 0, -100,
      0, 0, -105,
    ]);
    flight.nodeMap = new Map(flight.nodes.map(n => [n.uid, n]));
    flight._buildNodeSpatialGrid();

    // Run overlay update
    flight._updateOverlay(true);

    const visible = flight.nodeLabelPool.filter(el => el.style.display === "block");
    assert.equal(visible.length, 1, "Only 1 of 2 overlapping node labels is visible (the nearer one)");
    assert.equal(visible[0].textContent, "Overlap 1");

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("proximity node labels: dense cluster yields several labels (>= 4 within FOV with 900 radius) and cap of 8 holds", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    children: overlayChildren,
    appendChild: (el) => overlayChildren.push(el),
    removeChild: (el) => {
      const idx = overlayChildren.indexOf(el);
      if (idx !== -1) overlayChildren.splice(idx, 1);
    },
    querySelector: () => null,
    querySelectorAll: (sel) => {
      if (sel === ".graph-flight-node-label") {
        return overlayChildren.filter(c => c.className === "graph-flight-node-label");
      }
      return [];
    },
  };
  const mockContainer = {
    querySelector: (sel) => (sel === ".graph-flight-overlay" ? mockOverlay : null),
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag.toUpperCase(),
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 110,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;
    flight.setActive(true);

    // Build a synthetic dense cluster of 30 nodes around (0, 0, -2500)
    // Distributed within radius 100..700 in front of the camera with enough screen-space spacing
    const nodes = [];
    const positions = [];
    for (let i = 0; i < 30; i++) {
      nodes.push({
        uid: `cluster-node-${i}`,
        title: `Cluster Item ${i}`,
        type: i % 2 === 0 ? "confluence" : "jira",
        idx: i,
      });
      const angle = i * 0.8;
      const radius = 60 + i * 15;
      const x = Math.cos(angle) * radius;
      const y = Math.sin(angle) * radius;
      const z = -2500 - (100 + i * 20);
      positions.push(x, y, z);
    }

    flight.nodes = nodes;
    flight.positions = new Float32Array(positions);
    flight.nodeMap = new Map(nodes.map(n => [n.uid, n]));
    flight._buildNodeSpatialGrid();

    // Camera placed inside cluster at (0, 0, -2500)
    flight.cameraPos = [0, 0, -2500];
    flight.yaw = 0;
    flight.pitch = 0;

    // Run label computation
    const candidates = flight._computeNodeLabels();
    assert.equal(candidates.length, KnowledgeGraphFlight.MAX_NODE_LABELS, "Candidates capped at MAX_NODE_LABELS (8)");

    // Run overlay update with collision detection
    flight._updateOverlay(true);

    const visibleLabels = flight.nodeLabelPool.filter(el => el.style.display === "block");
    assert.ok(visibleLabels.length >= 4, `Inside dense cluster: expected >= 4 visible labels, got ${visibleLabels.length}`);
    assert.ok(visibleLabels.length <= KnowledgeGraphFlight.MAX_NODE_LABELS, `Cap of 8 holds (got ${visibleLabels.length})`);

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("proximity node labels: label colliding with timeline HUD rect is suppressed", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    children: overlayChildren,
    appendChild: (el) => overlayChildren.push(el),
    removeChild: () => {},
    querySelector: () => null,
  };
  const mockContainer = {
    querySelector: (sel) => (sel === ".graph-flight-overlay" ? mockOverlay : null),
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag.toUpperCase(),
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;
    flight.setActive(true);

    const nodes = [{ uid: "hud-overlap-node", title: "HUD Collision", type: "confluence", idx: 0 }];
    const positions = [0, 0, -200];
    flight.nodes = nodes;
    flight.positions = new Float32Array(positions);
    flight.nodeMap = new Map([["hud-overlap-node", nodes[0]]]);
    flight._buildNodeSpatialGrid();

    // Mock timeline HUD bounding rect to cover sx=400, sy=300
    flight.timelineHudEl.getBoundingClientRect = () => ({
      left: 350, top: 280, right: 550, bottom: 330, width: 200, height: 50
    });
    flight.timelineHudEl.style.display = "block";

    flight.cameraPos = [0, 0, 0];
    flight.yaw = 0;
    flight.pitch = 0;

    flight._updateOverlay(true);

    const visibleLabels = flight.nodeLabelPool.filter(el => el.style.display === "block");
    assert.equal(visibleLabels.length, 0, "Label hitting timeline HUD rect must be suppressed");

    // Move timeline HUD elsewhere, label should now appear
    flight.timelineHudEl.getBoundingClientRect = () => ({
      left: 350, top: 550, right: 450, bottom: 580, width: 100, height: 30
    });
    flight._updateOverlay(true);

    const visibleAfterMove = flight.nodeLabelPool.filter(el => el.style.display === "block");
    assert.equal(visibleAfterMove.length, 1, "Label appears when not colliding with timeline HUD");

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("proximity node labels: cancelPending hides labels and resets loading state", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    children: overlayChildren,
    appendChild: (el) => overlayChildren.push(el),
    removeChild: () => {},
    querySelector: () => null,
  };
  const mockContainer = {
    querySelector: (sel) => (sel === ".graph-flight-overlay" ? mockOverlay : null),
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag.toUpperCase(),
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;
    flight.setActive(true);

    const nodes = [{ uid: "node-1", title: "Node 1", type: "confluence", idx: 0 }];
    flight.nodes = nodes;
    flight.positions = new Float32Array([0, 0, -200]);
    flight.nodeMap = new Map([["node-1", nodes[0]]]);
    flight._buildNodeSpatialGrid();

    flight.cameraPos = [0, 0, 0];
    flight._updateOverlay(true);
    assert.equal(flight.nodeLabelPool.filter(el => el.style.display === "block").length, 1);

    // Simulate aborted/pending state
    flight._loading = true;
    flight.cancelPending();

    assert.equal(flight._loading, false, "cancelPending must clear _loading");
    assert.equal(flight.nodeLabelPool.filter(el => el.style.display === "block").length, 0, "cancelPending must hide all labels");

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("proximity node labels: empty dataset yields no labels and throws no errors", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    children: overlayChildren,
    appendChild: (el) => overlayChildren.push(el),
    removeChild: () => {},
    querySelector: () => null,
  };
  const mockContainer = {
    querySelector: (sel) => (sel === ".graph-flight-overlay" ? mockOverlay : null),
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag.toUpperCase(),
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;
    flight.setActive(true);

    await flight.setData({ nodes: [], edges: [] });
    flight._updateOverlay(true);

    const labels = flight._computeNodeLabels();
    assert.deepEqual(labels, [], "computeNodeLabels returns empty array for empty nodes");
    assert.equal(flight.nodeLabelPool.filter(el => el.style.display === "block").length, 0, "No labels visible on empty data");

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("proximity node labels: _computeNodeLabels(camPosOverride) restores matrices without side effects", async () => {
  const mockCanvas = createMockCanvas();
  const flight = new KnowledgeGraphFlight({
    containerEl: { querySelector: () => null, getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }) },
    canvasEl: mockCanvas,
  });
  flight.width = 800;
  flight.height = 600;
  flight.cameraPos = [100, 200, 300];
  flight._updateMatrices();

  const originalView = new Float32Array(flight.viewMatrix);
  const originalVP = new Float32Array(flight.viewProjectionMatrix);

  flight.nodes = [{ uid: "n1", title: "N1", type: "confluence", idx: 0 }];
  flight.positions = new Float32Array([500, 500, 500]);
  flight._buildNodeSpatialGrid();

  flight._computeNodeLabels([900, 800, 700]);

  assert.deepEqual(flight.viewMatrix, originalView, "viewMatrix must be restored after camPosOverride");
  assert.deepEqual(flight.viewProjectionMatrix, originalVP, "viewProjectionMatrix must be restored after camPosOverride");

  flight.destroy();
});


test("timeline axis: (a) axis off by default (no visible labels, no axis draw)", async () => {
  const drawCalls = [];
  const overlayChildren = [];
  const mockOverlay = {
    className: "graph-flight-overlay",
    style: {},
    children: overlayChildren,
    querySelector: (sel) => null,
    querySelectorAll: (sel) => {
      const cls = sel.replace(".", "");
      return overlayChildren.filter(c => c.className && c.className.split(" ").includes(cls));
    },
    appendChild: (child) => {
      overlayChildren.push(child);
      return child;
    },
  };

  const mockContainer = {
    querySelector: (sel) => sel.includes("graph-flight-overlay") ? mockOverlay : null,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag.toUpperCase(),
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const mockGL = mockCanvas.getContext("webgl");
    const origDrawArrays = mockGL.drawArrays;
    mockGL.drawArrays = (mode, first, count) => {
      drawCalls.push({ mode, first, count });
      origDrawArrays(mode, first, count);
    };

    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;

    // Default state: timelineAxis is false
    assert.equal(flight.timelineAxis, false, "timelineAxis must be false by default");
    assert.equal(flight.yearLabelPool.length, 20, "yearLabelPool has bounded size of 20");
    assert.ok(flight.yearLabelPool.every(el => el.style.display === "none"), "All year labels initially hidden");

    await flight.setData({
      nodes: [
        { uid: "jira:1", type: "jira", project_key: "PROJ", created: "2020-01-01T00:00:00Z", updated: "2024-01-01T00:00:00Z" },
        { uid: "jira:2", type: "jira", project_key: "PROJ", created: "2020-01-01T00:00:00Z", updated: "2020-06-01T00:00:00Z" },
      ],
      edges: [],
    });

    flight.setActive(true);
    drawCalls.length = 0;
    flight._render();

    // No year labels visible while timelineAxis is false
    assert.equal(flight.yearLabelPool.filter(el => el.style.display === "block").length, 0, "No year labels visible when axis is off");
    // No draw call for the timeline line buffer
    assert.ok(
      !drawCalls.some(c => c.count === flight.timelineLineCount),
      "No drawArrays call for timeline line buffer when axis is off"
    );

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("timeline axis: (b) setTimelineAxis(true) produces tick labels matching node z-mapping, count <= 16, format YYYY / MMM YYYY", async () => {
  // Test 1: computeTimelineTicks unit checks
  const minMultiYear = Date.parse("2020-01-01T00:00:00Z");
  const maxMultiYear = Date.parse("2024-06-01T00:00:00Z");
  const depth = 12000;
  const multiYearTicks = computeTimelineTicks(minMultiYear, maxMultiYear, depth);

  assert.ok(multiYearTicks.length > 0, "Produces ticks for multi-year range");
  assert.ok(multiYearTicks.length <= 16, `Tick count <= 16 (got ${multiYearTicks.length})`);
  for (const t of multiYearTicks) {
    // Multi-year step >= 12 months should format as YYYY
    assert.match(t.text, /^\d{4}$/, `Multi-year tick format must be YYYY: ${t.text}`);
    // Check z-mapping: z(t) = -((maxTime - t) / (maxTime - minTime)) * depth
    const expectedZ = -((maxMultiYear - t.time) / (maxMultiYear - minMultiYear)) * depth;
    assert.ok(Math.abs(t.z - expectedZ) < 0.001, `Tick z matches node z-mapping: ${t.z} vs ${expectedZ}`);
  }

  // Test 2: Sub-12-months range (< 12 months)
  const minSubYear = Date.parse("2025-01-15T00:00:00Z");
  const maxSubYear = Date.parse("2025-07-20T00:00:00Z");
  const subYearTicks = computeTimelineTicks(minSubYear, maxSubYear, depth);

  assert.ok(subYearTicks.length > 0, "Produces ticks for sub-year range");
  assert.ok(subYearTicks.length <= 16, `Tick count <= 16 (got ${subYearTicks.length})`);
  for (const t of subYearTicks) {
    // Sub-year step < 12 months should format as MMM YYYY (de-DE)
    assert.match(t.text, /^[A-ZÄÖÜ][a-zäöü]{2}\s\d{4}$/, `Sub-year tick format must be MMM YYYY: ${t.text}`);
    const expectedZ = -((maxSubYear - t.time) / (maxSubYear - minSubYear)) * depth;
    assert.ok(Math.abs(t.z - expectedZ) < 0.001, `Tick z matches node z-mapping: ${t.z} vs ${expectedZ}`);
  }

  // Test 3: KnowledgeGraphFlight integration with synthetic nodes
  const overlayChildren = [];
  const mockOverlay = {
    className: "graph-flight-overlay",
    style: {},
    children: overlayChildren,
    querySelector: (sel) => null,
    querySelectorAll: (sel) => {
      const cls = sel.replace(".", "");
      return overlayChildren.filter(c => c.className && c.className.split(" ").includes(cls));
    },
    appendChild: (child) => {
      overlayChildren.push(child);
      return child;
    },
  };

  const mockContainer = {
    querySelector: (sel) => sel.includes("graph-flight-overlay") ? mockOverlay : null,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag.toUpperCase(),
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const mockGL = mockCanvas.getContext("webgl");
    const drawCalls = [];
    const origDrawArrays = mockGL.drawArrays;
    mockGL.drawArrays = (mode, first, count) => {
      drawCalls.push({ mode, first, count });
      origDrawArrays(mode, first, count);
    };

    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;

    // Provide multi-year nodes
    await flight.setData({
      nodes: [
        { uid: "j1", type: "jira", project_key: "ENG", updated: "2020-01-01T00:00:00Z" },
        { uid: "j2", type: "jira", project_key: "ENG", updated: "2024-01-01T00:00:00Z" },
      ],
      edges: [],
    });

    flight.setActive(true);
    flight.setTimelineAxis(true);
    assert.equal(flight.timelineAxis, true);
    assert.ok(flight.timelineTicks.length > 0);
    assert.ok(flight.timelineTicks.length <= 16);

    // Verify axis is drawn in _render
    drawCalls.length = 0;
    flight._render();
    assert.ok(
      drawCalls.some(c => c.count === flight.timelineLineCount),
      "drawArrays called for timeline line buffer when axis is active"
    );

    // Verify synthetic node z equals tick z
    for (const tick of flight.timelineTicks) {
      const ratio = (flight.maxTime - tick.time) / (flight.maxTime - flight.minTime);
      const synthNodeZ = -ratio * flight.sceneDepth;
      assert.ok(Math.abs(tick.z - synthNodeZ) < 0.001);
    }

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("timeline axis: (c) toggling off hides all labels", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    className: "graph-flight-overlay",
    style: {},
    children: overlayChildren,
    querySelector: (sel) => null,
    querySelectorAll: () => [],
    appendChild: (c) => { overlayChildren.push(c); return c; },
  };
  const mockContainer = {
    querySelector: () => mockOverlay,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag ? tag.toUpperCase() : "DIV",
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;

    await flight.setData({
      nodes: [
        { uid: "j1", type: "jira", project_key: "ENG", updated: "2020-01-01T00:00:00Z" },
        { uid: "j2", type: "jira", project_key: "ENG", updated: "2024-01-01T00:00:00Z" },
      ],
      edges: [],
    });

    flight.setActive(true);
    flight.setTimelineAxis(true);
    // Camera looking at scene center
    flight.cameraPos = [0, 0, 1000];
    flight.yaw = 0;
    flight.pitch = 0;
    flight._updateOverlay(true);

    const visibleCountBefore = flight.yearLabelPool.filter(el => el.style.display === "block").length;
    assert.ok(visibleCountBefore > 0, "Some year labels are visible when timelineAxis is true");

    // Toggle off
    flight.setTimelineAxis(false);
    assert.equal(flight.timelineAxis, false);
    const visibleCountAfter = flight.yearLabelPool.filter(el => el.style.display !== "none").length;
    assert.equal(visibleCountAfter, 0, "All year labels must be hidden after setTimelineAxis(false)");

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("timeline axis: (d) setJiraTimeField rebuilds ticks", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    className: "graph-flight-overlay",
    style: {},
    children: overlayChildren,
    querySelector: (sel) => null,
    querySelectorAll: () => [],
    appendChild: (c) => { overlayChildren.push(c); return c; },
  };
  const mockContainer = {
    querySelector: () => mockOverlay,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag ? tag.toUpperCase() : "DIV",
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });

    // Node 1: created 2018, updated 2024
    // Node 2: created 2022, updated 2025
    await flight.setData({
      nodes: [
        { uid: "j1", type: "jira", project_key: "ENG", created: "2018-01-01T00:00:00Z", updated: "2024-01-01T00:00:00Z" },
        { uid: "j2", type: "jira", project_key: "ENG", created: "2022-01-01T00:00:00Z", updated: "2025-01-01T00:00:00Z" },
      ],
      edges: [],
    });

    flight.setTimelineAxis(true);
    const ticksUpdated = [...flight.timelineTicks];
    const minUpdated = flight.minTime;
    const maxUpdated = flight.maxTime;
    assert.equal(new Date(minUpdated).getUTCFullYear(), 2024);
    assert.equal(new Date(maxUpdated).getUTCFullYear(), 2025);

    // Switch to created: timespan is now 2018 to 2022
    flight.setJiraTimeField("created");
    assert.equal(flight.jiraTimeField, "created");
    const ticksCreated = flight.timelineTicks;
    const minCreated = flight.minTime;
    const maxCreated = flight.maxTime;
    assert.equal(new Date(minCreated).getUTCFullYear(), 2018);
    assert.equal(new Date(maxCreated).getUTCFullYear(), 2022);

    assert.notDeepEqual(
      ticksCreated.map(t => t.text),
      ticksUpdated.map(t => t.text),
      "Ticks must be rebuilt with new date boundaries after setJiraTimeField"
    );

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("timeline axis: (e) lifecycle: setActive(false) and cancelPending hide labels, empty timestamps produce no labels and no errors", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    className: "graph-flight-overlay",
    style: {},
    children: overlayChildren,
    querySelector: (sel) => null,
    querySelectorAll: () => [],
    appendChild: (c) => { overlayChildren.push(c); return c; },
  };
  const mockContainer = {
    querySelector: () => mockOverlay,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: (tag) => ({
      tagName: tag ? tag.toUpperCase() : "DIV",
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;

    await flight.setData({
      nodes: [
        { uid: "j1", type: "jira", project_key: "ENG", updated: "2020-01-01T00:00:00Z" },
        { uid: "j2", type: "jira", project_key: "ENG", updated: "2024-01-01T00:00:00Z" },
      ],
      edges: [],
    });

    flight.setActive(true);
    flight.setTimelineAxis(true);
    flight.cameraPos = [0, 0, 1000];
    flight.yaw = 0;
    flight.pitch = 0;
    flight._updateOverlay(true);

    assert.ok(flight.yearLabelPool.filter(el => el.style.display === "block").length > 0);

    // 1. setActive(false) hides all year labels
    flight.setActive(false);
    assert.equal(flight.yearLabelPool.filter(el => el.style.display !== "none").length, 0);

    // 2. cancelPending hides all year labels
    flight.setActive(true);
    flight._updateOverlay(true);
    assert.ok(flight.yearLabelPool.filter(el => el.style.display === "block").length > 0);

    flight.cancelPending();
    assert.equal(flight.yearLabelPool.filter(el => el.style.display !== "none").length, 0);

    // 3. Empty timestamps produce no labels and no errors
    await flight.setData({
      nodes: [
        { uid: "c1", type: "confluence", title: "No Date Doc" },
        { uid: "c2", type: "confluence", title: "Another No Date Doc" },
      ],
      edges: [],
    });

    flight.setActive(true);
    flight.setTimelineAxis(true);
    flight._updateOverlay(true);

    assert.equal(flight.timelineTicks.length, 0, "No timeline ticks when nodes lack timestamps");
    assert.equal(flight.yearLabelPool.filter(el => el.style.display !== "none").length, 0);
    // Line buffer contains central axis line only
    assert.equal(flight.timelineLineCount, 2, "Only central line vertices (2) present when no timestamps");

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("timeline axis: (f) edge-on camera (camera on the axis line, looking along -z, mid-depth): at least 4 year labels are visible and no two placed label rects overlap", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    className: "graph-flight-overlay",
    style: {},
    children: overlayChildren,
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: (c) => { overlayChildren.push(c); return c; },
  };
  const mockContainer = {
    querySelector: () => mockOverlay,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: () => ({
      tagName: "DIV",
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;

    const nodes = [];
    for (let y = 2016; y <= 2025; y++) {
      nodes.push({ uid: "j" + y, type: "jira", project_key: "ENG", updated: y + "-06-01T00:00:00Z" });
    }

    await flight.setData({ nodes, edges: [] });
    flight.setActive(true);
    flight.setTimelineAxis(true);

    // Camera on axis line, looking along -z, mid-depth
    flight.cameraPos = [flight.timelineAxisX, flight.timelineAxisY, -6000];
    flight.yaw = 0;
    flight.pitch = 0;
    flight._updateMatrices();
    flight._updateOverlay(true);

    const visible = flight.yearLabelPool.filter(el => el.style.display === "block");
    assert.ok(visible.length >= 4, "At least 4 year labels must be visible for edge-on camera (no culling to one)");

    // Verify no two placed label rects overlap
    const rects = visible.map(el => {
      const text = el.textContent;
      const lw = Math.max(50, text.length * 7 + 16);
      const lh = 22;
      const px = parseFloat(el.style.left);
      const py = parseFloat(el.style.top);
      return {
        left: px - lw * 0.5,
        right: px + lw * 0.5,
        top: py - lh * 0.5,
        bottom: py + lh * 0.5,
        text,
      };
    });

    for (let i = 0; i < rects.length; i++) {
      for (let j = i + 1; j < rects.length; j++) {
        const a = rects[i];
        const b = rects[j];
        const overlaps = !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom);
        assert.ok(!overlaps, "Labels " + a.text + " and " + b.text + " must not overlap");
      }
    }

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("timeline axis: (g) side view (camera offset laterally): labels are placed at their projected tick positions without nudging", async () => {
  const overlayChildren = [];
  const mockOverlay = {
    className: "graph-flight-overlay",
    style: {},
    children: overlayChildren,
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: (c) => { overlayChildren.push(c); return c; },
  };
  const mockContainer = {
    querySelector: () => mockOverlay,
    getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600 }),
    appendChild: () => {},
  };

  global.document = {
    createElement: () => ({
      tagName: "DIV",
      className: "",
      style: { display: "none" },
      setAttribute: () => {},
      getAttribute: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      appendChild: () => {},
      removeChild: () => {},
      childNodes: [],
      children: [],
      offsetWidth: 120,
      offsetHeight: 22,
      textContent: "",
    }),
    documentElement: { style: {} },
    hidden: false,
  };

  try {
    const mockCanvas = createMockCanvas();
    const flight = new KnowledgeGraphFlight({
      containerEl: mockContainer,
      canvasEl: mockCanvas,
    });
    flight.width = 800;
    flight.height = 600;

    const nodes = [
      { uid: "j1", type: "jira", project_key: "ENG", updated: "2020-01-01T00:00:00Z" },
      { uid: "j2", type: "jira", project_key: "ENG", updated: "2024-01-01T00:00:00Z" },
    ];

    await flight.setData({ nodes, edges: [] });
    flight.setActive(true);
    flight.setTimelineAxis(true);

    const axisX = flight.timelineAxisX;
    const axisY = flight.timelineAxisY;

    // Side view: camera offset laterally in X and Y
    flight.cameraPos = [axisX + 2500, axisY + 2500, 1000];
    flight.yaw = 0;
    flight.pitch = 0;
    flight._updateMatrices();
    flight._updateOverlay(true);

    const visible = flight.yearLabelPool.filter(el => el.style.display === "block");
    assert.ok(visible.length > 0, "Labels must be visible in side view");

    const vp = flight.viewProjectionMatrix;
    const hw = flight.width * 0.5;
    const hh = flight.height * 0.5;

    for (const el of visible) {
      const tick = el._tick;
      const tz = tick.z;
      const w = axisX * vp[3] + axisY * vp[7] + tz * vp[11] + vp[15];
      const invW = 1.0 / w;
      const ndcX = (axisX * vp[0] + axisY * vp[4] + tz * vp[8] + vp[12]) * invW;
      const ndcY = (axisX * vp[1] + axisY * vp[5] + tz * vp[9] + vp[13]) * invW;
      const sx = Math.round((ndcX + 1.0) * hw);
      const sy = Math.round((1.0 - ndcY) * hh);

      const expectedLeft = (sx + 16) + "px";
      const expectedTop = sy + "px";

      const diffX = Math.abs(parseFloat(el.style.left) - (sx + 16));
      const diffY = Math.abs(parseFloat(el.style.top) - sy);

      assert.ok(diffX < 2, "X position matches projected tick position (got " + el.style.left + ", expected " + expectedLeft + ")");
      assert.ok(diffY < 2, "Y position matches projected tick position without nudging (got " + el.style.top + ", expected " + expectedTop + ")");
    }

    flight.destroy();
  } finally {
    delete global.document;
  }
});

test("timeline axis: (h) sub-year span crossing New Year (Nov 2023 - Feb 2024) produces month ticks", async () => {
  const tNov = Date.UTC(2023, 10, 15);
  const tFeb = Date.UTC(2024, 1, 15);
  const ticks = computeTimelineTicks(tNov, tFeb, 12000);
  assert.ok(ticks.length >= 2, "Must produce multiple month ticks");
  const texts = ticks.map(t => t.text);
  assert.ok(texts.includes("Nov 2023"), "Should contain Nov 2023");
  assert.ok(texts.includes("Jan 2024"), "Should contain Jan 2024");
  assert.ok(!texts.includes("2023") && !texts.includes("2024"), "Should use month format, not year-only");
});

test("timeline axis: (i) sub-month span (5-25 March) produces exactly two endpoint ticks with day-precision labels", async () => {
  const tMar5 = Date.UTC(2024, 2, 5);
  const tMar25 = Date.UTC(2024, 2, 25);
  const ticks = computeTimelineTicks(tMar5, tMar25, 12000);
  assert.equal(ticks.length, 2, "Sub-month span must produce exactly two endpoint ticks");
  assert.equal(ticks[0].text, "25. Mär 2024");
  assert.equal(ticks[1].text, "5. Mär 2024");
  assert.equal(ticks[0].z, 0);
  assert.equal(ticks[1].z, -12000);
});

test("timeline axis: (j) >800 year span still contains the newest year in the clamped tick list", async () => {
  const t1000 = Date.UTC(1000, 0, 1);
  const t2024 = Date.UTC(2024, 0, 1);
  const ticks = computeTimelineTicks(t1000, t2024, 12000);
  assert.equal(ticks.length, 16, "Tick count must be clamped to 16");
  const texts = ticks.map(t => t.text);
  assert.ok(texts.includes("2024"), "Newest year (2024) must be in the clamped tick list");
  assert.ok(texts.includes("1000"), "Oldest year (1000) must be in the clamped tick list");
  assert.equal(ticks[0].text, "2024");
  assert.equal(ticks[ticks.length - 1].text, "1000");
});
