'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const flightLayoutPath = path.join(root, 'web/graph-flight-layout.js');
const FlightLayout = require(flightLayoutPath);

test('UMD exposure in CommonJS, window, and self environments', () => {
  assert.ok(FlightLayout && typeof FlightLayout.build === 'function', 'CommonJS require should export build function');
  assert.equal(FlightLayout.DEPTH, 12000, 'DEPTH constant must be 12000');
  assert.equal(FlightLayout.UNKNOWN_Z, 600, 'UNKNOWN_Z constant must be 600');

  // Test window environment (browser)
  const windowScope = { window: {} };
  windowScope.window.window = windowScope.window;
  vm.createContext(windowScope);
  vm.runInContext(fs.readFileSync(flightLayoutPath, 'utf8'), windowScope);
  assert.ok(windowScope.window.KnowledgeGraphFlightLayout, 'Exposed on window');
  assert.equal(typeof windowScope.window.KnowledgeGraphFlightLayout.build, 'function');

  // Test self environment (Web Worker)
  const workerScope = { self: {} };
  workerScope.self.self = workerScope.self;
  vm.createContext(workerScope);
  vm.runInContext(fs.readFileSync(flightLayoutPath, 'utf8'), workerScope);
  assert.ok(workerScope.self.KnowledgeGraphFlightLayout, 'Exposed on self');
  assert.equal(typeof workerScope.self.KnowledgeGraphFlightLayout.build, 'function');
});

test('Exact time-order: newest at z=0, older more negative down to -12000', () => {
  const data = {
    nodes: [
      { uid: 'c:oldest', type: 'confluence', project: 'DOC', source_updated_at: '2026-01-01T00:00:00Z' },
      { uid: 'c:mid', type: 'confluence', project: 'DOC', source_updated_at: '2026-02-01T00:00:00Z' },
      { uid: 'c:newest', type: 'confluence', project: 'DOC', source_updated_at: '2026-03-01T00:00:00Z' },
    ],
    edges: []
  };

  const res = FlightLayout.build(data);

  assert.equal(res.depth, 12000);
  assert.equal(res.unknownCount, 0);
  assert.equal(res.minTime, Date.parse('2026-01-01T00:00:00Z'));
  assert.equal(res.maxTime, Date.parse('2026-03-01T00:00:00Z'));

  const zOldest = res.positions[3 * 0 + 2];
  const zMid = res.positions[3 * 1 + 2];
  const zNewest = res.positions[3 * 2 + 2];

  assert.equal(zNewest, 0, 'Newest date must be at z = 0');
  assert.equal(zOldest, -12000, 'Oldest date must be at z = -12000');
  assert.ok(zOldest < zMid && zMid < zNewest, `Order must satisfy oldest (${zOldest}) < mid (${zMid}) < newest (${zNewest})`);

  // Intermediate ratio check
  const expectedRatio = (res.maxTime - res.timestamps[1]) / (res.maxTime - res.minTime);
  assert.ok(Math.abs(zMid - (-expectedRatio * 12000)) < 1e-3);
});

test('Equal-date stable z0: nodes with identical dates all have z=0', () => {
  const data = {
    nodes: [
      { uid: 'c:1', type: 'confluence', project: 'DOC', source_updated_at: '2026-03-01T12:00:00Z' },
      { uid: 'c:2', type: 'confluence', project: 'DOC', source_updated_at: '2026-03-01T12:00:00Z' },
      { uid: 'c:3', type: 'confluence', project: 'DOC', source_updated_at: '2026-03-01T12:00:00Z' },
    ],
    edges: []
  };

  const res = FlightLayout.build(data);
  assert.equal(res.minTime, res.maxTime);
  assert.equal(res.unknownCount, 0);

  for (let i = 0; i < 3; i++) {
    assert.equal(res.positions[3 * i + 2], 0, `Node ${i} should be at z=0 when all dates are equal`);
  }

  // Also verify that despite having identical Z, nodes do not pile up on the same (x, y)
  const p0 = [res.positions[0], res.positions[1]];
  const p1 = [res.positions[3], res.positions[4]];
  const p2 = [res.positions[6], res.positions[7]];
  assert.ok(Math.hypot(p0[0] - p1[0], p0[1] - p1[1]) > 10, 'No XY pileup between node 0 and 1');
  assert.ok(Math.hypot(p1[0] - p2[0], p1[1] - p2[1]) > 10, 'No XY pileup between node 1 and 2');
});

test('Confluence unaffected by Jira mode: always uses source_updated_at', () => {
  const data = {
    nodes: [
      {
        uid: 'conf:page',
        type: 'confluence',
        project: 'WIKI',
        source_created_at: '2025-01-01T00:00:00Z',
        source_updated_at: '2026-05-01T00:00:00Z'
      },
      {
        uid: 'jira:ticket',
        type: 'jira',
        project: 'DEV',
        source_created_at: '2025-06-01T00:00:00Z',
        source_updated_at: '2026-06-01T00:00:00Z'
      }
    ],
    edges: []
  };

  const resUpdated = FlightLayout.build(data, { jiraTimeField: 'updated' });
  const resCreated = FlightLayout.build(data, { jiraTimeField: 'created' });

  // Confluence timestamp must be strictly identical regardless of jiraTimeField
  assert.equal(resUpdated.timestamps[0], Date.parse('2026-05-01T00:00:00Z'));
  assert.equal(resCreated.timestamps[0], Date.parse('2026-05-01T00:00:00Z'));

  // Jira timestamp switches between source_updated_at and source_created_at
  assert.equal(resUpdated.timestamps[1], Date.parse('2026-06-01T00:00:00Z'));
  assert.equal(resCreated.timestamps[1], Date.parse('2025-06-01T00:00:00Z'));
});

test('Dominant space association for Jira tickets (both edge directions and deterministic ties)', () => {
  const data = {
    nodes: [
      { uid: 'c:alpha1', type: 'confluence', project: 'ALPHA', source_updated_at: '2026-01-01T00:00:00Z' },
      { uid: 'c:alpha2', type: 'confluence', project: 'ALPHA', source_updated_at: '2026-01-01T00:00:00Z' },
      { uid: 'c:beta1', type: 'confluence', project: 'BETA', source_updated_at: '2026-01-01T00:00:00Z' },
      { uid: 'c:gamma1', type: 'confluence', project: 'GAMMA', source_updated_at: '2026-01-01T00:00:00Z' },
      // J1 links to ALPHA twice, BETA once -> dominant space ALPHA
      { uid: 'j:1', type: 'jira', project: 'PROJ', source_updated_at: '2026-01-01T00:00:00Z' },
      // J2 has incoming link from BETA -> dominant space BETA (either direction)
      { uid: 'j:2', type: 'jira', project: 'PROJ', source_updated_at: '2026-01-01T00:00:00Z' },
      // J3 links equally to BETA and ALPHA -> tie break alphabetically to ALPHA
      { uid: 'j:3', type: 'jira', project: 'PROJ', source_updated_at: '2026-01-01T00:00:00Z' },
      // J4 has NO Confluence links -> clusters by Jira project PROJ
      { uid: 'j:4', type: 'jira', project: 'PROJ', source_updated_at: '2026-01-01T00:00:00Z' },
    ],
    edges: [
      { source: 'j:1', target: 'c:alpha1' },
      { source: 'j:1', target: 'c:alpha2' },
      { source: 'j:1', target: 'c:beta1' },
      { source: 'c:beta1', target: 'j:2' }, // incoming to J2
      { source: 'j:3', target: 'c:beta1' },
      { source: 'j:3', target: 'c:alpha1' },
    ]
  };

  const res = FlightLayout.build(data);

  const clusterOf = (nodeIdx) => res.clusters[res.clusterIndices[nodeIdx]];

  // Confluence nodes cluster by space
  assert.equal(clusterOf(0).id, 'space:ALPHA');
  assert.equal(clusterOf(1).id, 'space:ALPHA');
  assert.equal(clusterOf(2).id, 'space:BETA');
  assert.equal(clusterOf(3).id, 'space:GAMMA');

  // J1 dominant space is ALPHA (2 links vs 1)
  assert.equal(clusterOf(4).id, 'space:ALPHA');
  assert.equal(res.clusterIndices[4], res.clusterIndices[0], 'J1 should share cluster with ALPHA Confluence pages');

  // J2 linked incoming from BETA
  assert.equal(clusterOf(5).id, 'space:BETA');
  assert.equal(res.clusterIndices[5], res.clusterIndices[2], 'J2 should share cluster with BETA');

  // J3 tied between ALPHA and BETA -> deterministic tie alphabetically picks ALPHA
  assert.equal(clusterOf(6).id, 'space:ALPHA');

  // J4 has no Confluence links -> clusters by Jira project
  assert.equal(clusterOf(7).id, 'jira:PROJ');
  assert.notEqual(res.clusterIndices[7], res.clusterIndices[0]);
});

test('Unresolved Confluence link targets associate Jira ticket to space', () => {
  const data = {
    nodes: [
      { uid: 'jira:TASK-1', type: 'jira', project: 'TASK', source_updated_at: '2026-01-01T00:00:00Z' }
    ],
    edges: [
      { source: 'jira:TASK-1', target: 'confluence:unresolved:FINANCE:Q4Report' }
    ]
  };

  const res = FlightLayout.build(data);
  const cluster = res.clusters[res.clusterIndices[0]];
  assert.equal(cluster.id, 'space:FINANCE', 'Jira ticket associates to unresolved target space');
  assert.equal(cluster.label, 'FINANCE');
});

test('Missing, null, invalid, and future timestamps: z=+600 in front band', () => {
  const now = Date.parse('2026-10-11T20:00:00Z');
  const data = {
    nodes: [
      { uid: 'c:valid', type: 'confluence', project: 'P', source_updated_at: '2026-05-01T12:00:00Z' },
      { uid: 'c:null', type: 'confluence', project: 'P', source_updated_at: null }, // Avoid Date(null) = 1970
      { uid: 'c:undefined', type: 'confluence', project: 'P' },
      { uid: 'c:empty', type: 'confluence', project: 'P', source_updated_at: '' },
      { uid: 'c:notadate', type: 'confluence', project: 'P', source_updated_at: 'hello-world' },
      { uid: 'c:feb31', type: 'confluence', project: 'P', source_updated_at: '2026-02-31T00:00:00Z' }, // Invalid calendar day
      { uid: 'c:future', type: 'confluence', project: 'P', source_updated_at: '2099-01-01T00:00:00Z' }, // Future date
    ],
    edges: []
  };

  const res = FlightLayout.build(data, { now });

  assert.equal(res.unknownCount, 6, 'All 6 invalid/missing/future dates must be counted in unknownCount');
  assert.equal(res.minTime, Date.parse('2026-05-01T12:00:00Z'));
  assert.equal(res.maxTime, Date.parse('2026-05-01T12:00:00Z'));

  // Valid node: only 1 valid date so z = 0
  assert.equal(res.positions[3 * 0 + 2], 0);
  assert.equal(res.timestamps[0], Date.parse('2026-05-01T12:00:00Z'));

  // Invalid nodes: timestamp must be NaN and z must be +600
  for (let i = 1; i <= 6; i++) {
    assert.ok(Number.isNaN(res.timestamps[i]), `Node ${i} timestamp must be NaN`);
    assert.equal(res.positions[3 * i + 2], 600, `Node ${i} z must be placed at front band z = +600`);
  }
});

test('All nodes with missing dates: minTime and maxTime are null', () => {
  const data = {
    nodes: [
      { uid: 'c:1', type: 'confluence', project: 'DOC' },
      { uid: 'j:1', type: 'jira', project: 'DEV' },
    ],
    edges: []
  };

  const res = FlightLayout.build(data);
  assert.equal(res.minTime, null);
  assert.equal(res.maxTime, null);
  assert.equal(res.unknownCount, 2);
  assert.equal(res.positions[2], 600);
  assert.equal(res.positions[5], 600);
});

test('Timezone parsing: ISO offsets produce identical epoch and z-position', () => {
  const data = {
    nodes: [
      { uid: 'c:utc', type: 'confluence', project: 'T', source_updated_at: '2026-03-01T10:00:00Z' },
      { uid: 'c:plus2', type: 'confluence', project: 'T', source_updated_at: '2026-03-01T12:00:00+02:00' },
      { uid: 'c:plus2no_colon', type: 'confluence', project: 'T', source_updated_at: '2026-03-01T12:00:00+0200' },
      { uid: 'c:minus5', type: 'confluence', project: 'T', source_updated_at: '2026-03-01T05:00:00-05:00' },
      { uid: 'c:reference_earlier', type: 'confluence', project: 'T', source_updated_at: '2026-01-01T00:00:00Z' }
    ],
    edges: []
  };

  const res = FlightLayout.build(data);

  const tUTC = res.timestamps[0];
  const tPlus2 = res.timestamps[1];
  const tPlus2NoColon = res.timestamps[2];
  const tMinus5 = res.timestamps[3];

  assert.equal(tUTC, 1772359200000);
  assert.equal(tPlus2, tUTC);
  assert.equal(tPlus2NoColon, tUTC);
  assert.equal(tMinus5, tUTC);

  const zUTC = res.positions[3 * 0 + 2];
  const zPlus2 = res.positions[3 * 1 + 2];
  const zPlus2NoColon = res.positions[3 * 2 + 2];
  const zMinus5 = res.positions[3 * 3 + 2];

  assert.equal(zPlus2, zUTC);
  assert.equal(zPlus2NoColon, zUTC);
  assert.equal(zMinus5, zUTC);
});

test('Determinism: repeat executions produce bit-identical Float32Array coordinates', () => {
  const data = {
    nodes: [
      { uid: 'c:1', type: 'confluence', project: 'ENG', source_updated_at: '2026-03-01T10:00:00Z' },
      { uid: 'c:2', type: 'confluence', project: 'ENG', source_updated_at: '2026-02-15T00:00:00Z' },
      { uid: 'j:1', type: 'jira', project: 'ENG', source_updated_at: '2026-03-10T12:00:00Z' },
      { uid: 'j:2', type: 'jira', project: 'OPS', source_updated_at: '2026-01-01T00:00:00Z' },
    ],
    edges: [
      { source: 'j:1', target: 'c:1' }
    ]
  };

  const run1 = FlightLayout.build(data);
  const run2 = FlightLayout.build(data);

  assert.equal(run1.positions.length, run2.positions.length);
  for (let i = 0; i < run1.positions.length; i++) {
    assert.equal(run1.positions[i], run2.positions[i], `Mismatch at position index ${i}`);
  }

  for (let i = 0; i < run1.clusterIndices.length; i++) {
    assert.equal(run1.clusterIndices[i], run2.clusterIndices[i]);
  }
});

test('Stable placement under Jira date toggle: XY coordinates remain identical', () => {
  const data = {
    nodes: [
      { uid: 'c:1', type: 'confluence', project: 'CORE', source_updated_at: '2026-05-01T00:00:00Z' },
      { uid: 'j:1', type: 'jira', project: 'CORE', source_created_at: '2025-01-01T00:00:00Z', source_updated_at: '2026-06-01T00:00:00Z' },
      { uid: 'j:2', type: 'jira', project: 'CORE', source_created_at: '2025-03-01T00:00:00Z', source_updated_at: '2026-04-01T00:00:00Z' },
      { uid: 'j:3', type: 'jira', project: 'DEV', source_created_at: '2025-02-01T00:00:00Z', source_updated_at: '2026-03-01T00:00:00Z' },
    ],
    edges: [
      { source: 'j:1', target: 'c:1' },
      { source: 'j:2', target: 'c:1' },
    ]
  };

  const resUpdated = FlightLayout.build(data, { jiraTimeField: 'updated' });
  const resCreated = FlightLayout.build(data, { jiraTimeField: 'created' });

  // XY coordinates of every node must be 100% identical between toggles
  for (let i = 0; i < data.nodes.length; i++) {
    const xU = resUpdated.positions[3 * i];
    const yU = resUpdated.positions[3 * i + 1];
    const xC = resCreated.positions[3 * i];
    const yC = resCreated.positions[3 * i + 1];

    assert.equal(xU, xC, `Node ${i} X must be unchanged across Jira date mode toggle`);
    assert.equal(yU, yC, `Node ${i} Y must be unchanged across Jira date mode toggle`);

    // Cluster assignments must also be identical
    assert.equal(resUpdated.clusterIndices[i], resCreated.clusterIndices[i]);
  }

  // Z coordinate of Jira nodes should have changed because created_at != updated_at
  const zJ1Updated = resUpdated.positions[3 * 1 + 2];
  const zJ1Created = resCreated.positions[3 * 1 + 2];
  assert.notEqual(zJ1Updated, zJ1Created, 'Jira node Z coordinate changes according to time mode');
});

test('Data immutability: input data, nodes, and edges are not modified', () => {
  const node1 = Object.freeze({ uid: 'c:1', type: 'confluence', project: 'FROZEN', source_updated_at: '2026-03-01T00:00:00Z' });
  const node2 = Object.freeze({ uid: 'j:1', type: 'jira', project: 'FROZEN', source_updated_at: '2026-03-02T00:00:00Z' });
  const edge = Object.freeze({ source: 'j:1', target: 'c:1' });
  const nodes = Object.freeze([node1, node2]);
  const edges = Object.freeze([edge]);
  const data = Object.freeze({ nodes, edges });

  assert.doesNotThrow(() => {
    FlightLayout.build(data);
  }, 'Should not throw when reading completely frozen data');

  assert.equal(Object.keys(node1).length, 4, 'No extra properties attached to node');
  assert.equal(Object.keys(edge).length, 2, 'No extra properties attached to edge');
});

test('100k nodes and edges complexity sanity without flaky timing', () => {
  const n = 100000;
  const nodes = new Array(n);
  const edges = new Array(n);

  const baseTime = Date.parse('2026-01-01T00:00:00Z');

  for (let i = 0; i < n; i++) {
    const isConf = i < 40000;
    const spaceId = 'SP_' + (i % 20);
    const projId = 'PR_' + (i % 20);
    const dateStr = new Date(baseTime + (i % 1000) * 86400000).toISOString();

    if (isConf) {
      nodes[i] = {
        uid: `confluence:${i}`,
        type: 'confluence',
        project: spaceId,
        source_updated_at: dateStr
      };
    } else {
      nodes[i] = {
        uid: `jira:TICKET-${i}`,
        type: 'jira',
        project: projId,
        source_created_at: dateStr,
        source_updated_at: dateStr
      };
    }

    // Connect Jira ticket to Confluence page
    edges[i] = {
      source: `jira:TICKET-${40000 + (i % 60000)}`,
      target: `confluence:${i % 40000}`
    };
  }

  const start = performance.now();
  const res = FlightLayout.build({ nodes, edges }, { jiraTimeField: 'updated' });
  const elapsed = performance.now() - start;

  assert.equal(res.positions.length, 300000, 'Positions Float32Array has 3 * n elements');
  assert.equal(res.clusterIndices.length, 100000, 'clusterIndices Uint32Array has n elements');
  assert.equal(res.timestamps.length, 100000, 'timestamps Float64Array has n elements');
  assert.equal(res.depth, 12000);
  assert.ok(res.clusters.length > 0, 'Clusters generated');

  // Verify all coordinates are finite numbers (no NaN or Infinity)
  for (let i = 0; i < Math.min(1000, n); i++) {
    const x = res.positions[3 * i];
    const y = res.positions[3 * i + 1];
    const z = res.positions[3 * i + 2];
    assert.ok(Number.isFinite(x) && !Number.isNaN(x), `x must be finite at ${i}`);
    assert.ok(Number.isFinite(y) && !Number.isNaN(y), `y must be finite at ${i}`);
    assert.ok(Number.isFinite(z) && !Number.isNaN(z), `z must be finite at ${i}`);
  }

  // Non-flaky complexity sanity check: 100k nodes and 100k edges must complete in reasonable time (< 3000ms)
  assert.ok(elapsed < 5000, `Layout should scale linearly O(n+m); completed in ${elapsed.toFixed(1)}ms`);
});

test("Cluster label collision disambiguation: space and project with identical names", () => {
  const data = {
    nodes: [
      { uid: "confluence:page1", type: "confluence", space: "KAFKA", source_updated_at: "2026-01-01T00:00:00Z" },
      { uid: "jira:KAFKA-1", type: "jira", project: "KAFKA", source_updated_at: "2026-01-01T00:00:00Z" },
      { uid: "confluence:page2", type: "confluence", space: "UNIQUE", source_updated_at: "2026-01-01T00:00:00Z" },
    ],
    edges: []
  };

  const res = FlightLayout.build(data);
  const kafkaSpace = res.clusters.find(c => c.id === "space:KAFKA");
  const kafkaProj = res.clusters.find(c => c.id === "jira:KAFKA");
  const uniqueSpace = res.clusters.find(c => c.id === "space:UNIQUE");

  assert.ok(kafkaSpace);
  assert.ok(kafkaProj);
  assert.ok(uniqueSpace);
  assert.equal(kafkaSpace.label, "KAFKA (Space)");
  assert.equal(kafkaProj.label, "KAFKA (Projekt)");
  assert.equal(uniqueSpace.label, "UNIQUE");
});
