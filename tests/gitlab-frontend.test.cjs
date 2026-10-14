const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

// 1. Test canonical graph types logic
test('Canonical graph types combinations', () => {
  const app = require('../web/app.js');
  const { getCanonicalGraphTypes, parseGraphTypes } = app;

  assert.equal(typeof getCanonicalGraphTypes, 'function', 'getCanonicalGraphTypes should be exported');
  assert.equal(typeof parseGraphTypes, 'function', 'parseGraphTypes should be exported');

  // Verify all 7 canonical forms
  assert.equal(getCanonicalGraphTypes(true, true, true), 'all');
  assert.equal(getCanonicalGraphTypes(true, false, false), 'confluence');
  assert.equal(getCanonicalGraphTypes(false, true, false), 'jira');
  assert.equal(getCanonicalGraphTypes(false, false, true), 'gitlab');
  assert.equal(getCanonicalGraphTypes(false, true, true), 'jira,gitlab');
  assert.equal(getCanonicalGraphTypes(true, false, true), 'confluence,gitlab');
  assert.equal(getCanonicalGraphTypes(true, true, false), 'confluence,jira');

  // Fallback when all disabled
  assert.equal(getCanonicalGraphTypes(false, false, false), 'confluence');

  // Test parsing
  assert.deepEqual(parseGraphTypes('all'), { confluence: true, jira: true, gitlab: true });
  assert.deepEqual(parseGraphTypes('confluence'), { confluence: true, jira: false, gitlab: false });
  assert.deepEqual(parseGraphTypes('jira'), { confluence: false, jira: true, gitlab: false });
  assert.deepEqual(parseGraphTypes('gitlab'), { confluence: false, jira: false, gitlab: true });
  assert.deepEqual(parseGraphTypes('jira,gitlab'), { confluence: false, jira: true, gitlab: true });
  assert.deepEqual(parseGraphTypes('confluence,gitlab'), { confluence: true, jira: false, gitlab: true });
  assert.deepEqual(parseGraphTypes('confluence,jira'), { confluence: true, jira: true, gitlab: false });

  // Parsing handles whitespace and case
  assert.deepEqual(parseGraphTypes(' JIRA, gitlab '), { confluence: false, jira: true, gitlab: true });
  assert.deepEqual(parseGraphTypes(''), { confluence: true, jira: false, gitlab: false });
  assert.deepEqual(parseGraphTypes(null), { confluence: true, jira: false, gitlab: false });
});

// 2. Test 3D flight layout with GitLab datasets
test('KnowledgeGraphFlightLayout with pure GitLab dataset', () => {
  const KnowledgeGraphFlightLayout = require('../web/graph-flight-layout.js');

  const nodes = [
    {
      uid: 'gitlab:dw191299/bonbot:abc1234',
      type: 'gitlab',
      title: 'Fix issue API-1',
      committed_date: '2024-03-01T12:00:00Z'
    },
    {
      uid: 'gitlab:dw191299/bonbot:def5678',
      type: 'gitlab',
      title: 'Update config',
      source_updated_at: '2024-03-02T12:00:00Z'
    },
    {
      uid: 'gitlab:42:7890123',
      type: 'gitlab',
      title: 'Commit in numeric project',
      authored_date: '2024-02-15T10:00:00Z'
    }
  ];

  const layout = KnowledgeGraphFlightLayout.build({ nodes, edges: [] });

  assert.ok(layout, 'layout should build successfully');
  assert.equal(layout.positions.length, 3 * 3, 'should have 3 3D positions');

  // Verify non-NaN positions
  for (let i = 0; i < layout.positions.length; i++) {
    assert.ok(Number.isFinite(layout.positions[i]), `position[${i}] must be finite`);
  }

  // Verify clusters exist for both projects (namespaced path and numeric ID)
  const clusterIds = layout.clusters.map(c => c.id);
  assert.ok(clusterIds.includes('gitlab:dw191299/bonbot'), 'cluster for dw191299/bonbot should exist');
  assert.ok(clusterIds.includes('gitlab:42'), 'cluster for project 42 should exist');

  // Verify time mapping
  assert.ok(layout.minTime !== null && layout.maxTime !== null);
  assert.ok(layout.maxTime >= layout.minTime);
});

test('KnowledgeGraphFlightLayout with Jira + GitLab without Confluence', () => {
  const KnowledgeGraphFlightLayout = require('../web/graph-flight-layout.js');

  const nodes = [
    {
      uid: 'jira:API-2323',
      type: 'jira',
      title: 'API Gateway Bug',
      project: 'API',
      updated_at: '2024-03-02T10:00:00Z'
    },
    {
      uid: 'gitlab:dw191299/bonbot:fa394a1',
      type: 'gitlab',
      title: 'API-2323: resolve connection reset',
      project: 'dw191299/bonbot',
      committed_date: '2024-03-02T11:00:00Z'
    }
  ];

  const edges = [
    {
      source: 'gitlab:dw191299/bonbot:fa394a1',
      target: 'jira:API-2323',
      kind: 'mentions_jira'
    }
  ];

  const layout = KnowledgeGraphFlightLayout.build({ nodes, edges });
  assert.ok(layout, 'layout should build without throwing');
  assert.equal(layout.positions.length, 6, '2 nodes -> 6 position floats');

  for (let i = 0; i < layout.positions.length; i++) {
    assert.ok(Number.isFinite(layout.positions[i]), `position[${i}] must be finite`);
  }

  const clusterIds = layout.clusters.map(c => c.id);
  assert.ok(clusterIds.includes('jira:API'), 'Jira cluster should exist');
  assert.ok(clusterIds.includes('gitlab:dw191299/bonbot'), 'GitLab cluster should exist');
});

test('KnowledgeGraphFlight defaultFlightLayout handles GitLab', () => {
  const KnowledgeGraphFlight = require('../web/graph-flight.js');

  const data = {
    nodes: [
      { uid: 'gitlab:dw191299/bonbot:123', type: 'gitlab', committed_date: '2024-01-01T00:00:00Z' },
      { uid: 'jira:API-1', type: 'jira', updated: '2024-01-02T00:00:00Z' }
    ],
    edges: []
  };

  const layout = KnowledgeGraphFlight.defaultFlightLayout(data);
  assert.ok(layout);
  assert.ok(layout.clusters.some(c => c.id === 'gitlab:dw191299/bonbot'));
  assert.ok(layout.clusters.some(c => c.id === 'jira:API'));
});

test('KnowledgeGraph 2D shape and glyph for GitLab', () => {
  const KnowledgeGraph = require('../web/graph.js');

  const canvas = {
    getContext: () => ({
      beginPath: () => {},
      moveTo: () => {},
      lineTo: () => {},
      closePath: () => {},
      fill: () => {},
      stroke: () => {},
      fillText: () => {},
      measureText: () => ({ width: 10 })
    }),
    width: 800,
    height: 600,
    style: {},
    addEventListener: () => {}
  };

  const container = {
    appendChild: () => {},
    clientWidth: 800,
    getBoundingClientRect: () => ({ width: 800, height: 600 }),
    clientHeight: 600,
    addEventListener: () => {}
  };

  global.window = {
    devicePixelRatio: 1,
    addEventListener: () => {},
    removeEventListener: () => {},
    requestAnimationFrame: () => 1
  };
  global.requestAnimationFrame = global.window.requestAnimationFrame;
  // Mock global document if needed
  global.document = {
    createElement: () => ({
      className: '',
      style: {},
      appendChild: () => {},
      setAttribute: () => {}
    }),
    documentElement: {
      style: {}
    }
  };

  const graph = new KnowledgeGraph({
    canvasEl: canvas,
    containerEl: container
  });

  assert.equal(graph.colors.gitlab, '#fc6d26', 'GitLab color should fallback to #fc6d26');

  // Verify _shapePath executes diamond for gitlab
  let movedTo = null;
  let lineToCalls = 0;
  const mockCtx = {
    moveTo: (x, y) => { movedTo = { x, y }; },
    lineTo: (x, y) => { lineToCalls++; },
    closePath: () => {}
  };

  const node = { x: 50, y: 50, r: 10 };
  graph._shapePath(node, 'gitlab', mockCtx);

  assert.ok(movedTo !== null, 'moveTo should be called');
  assert.equal(lineToCalls, 3, 'diamond has 3 lineTo calls after moveTo');
});
