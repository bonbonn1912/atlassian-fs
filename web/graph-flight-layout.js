(function (root, factory) {
  if (typeof module === 'object' && typeof module.exports === 'object') {
    module.exports = factory();
  } else {
    const layout = factory();
    if (typeof window !== 'undefined') {
      window.KnowledgeGraphFlightLayout = layout;
    }
    if (typeof self !== 'undefined') {
      self.KnowledgeGraphFlightLayout = layout;
    }
    if (typeof globalThis !== 'undefined') {
      globalThis.KnowledgeGraphFlightLayout = layout;
    }
  }
})(typeof globalThis !== 'undefined' ? globalThis : (typeof window !== 'undefined' ? window : this), function () {
  'use strict';

  const DEPTH = 12000;
  const UNKNOWN_Z = 600;
  const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
  const NODE_SPACING = 36;
  const CLUSTER_PADDING = 80;

  // 32-bit linear congruential generator for deterministic jitter without Math.random
  function lcg(seed) {
    return ((seed * 1664525 + 1013904223) >>> 0) / 4294967296;
  }

  // Parse ISO date string safely: avoids Date(null) = 1970 bug, validates calendar, rejects future dates
  function parseTimestamp(raw, maxTime) {
    if (raw === null || raw === undefined || raw === '' || raw === false || raw === true) {
      return NaN;
    }
    let time;
    if (typeof raw === 'number') {
      if (!Number.isFinite(raw) || raw <= 0) return NaN;
      time = raw;
    } else if (typeof raw === 'string') {
      const s = raw.trim();
      if (!s || s === 'null' || s === 'undefined') return NaN;
      // ISO-8601 validation pattern: YYYY-MM-DD[T| ]HH:MM[:SS[.mmm]][Z|+-HH[:MM]]
      const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?(Z|[+-]\d{2}(?::?\d{2})?)?)?$/i.exec(s);
      if (!match) return NaN;
      const year = parseInt(match[1], 10);
      const month = parseInt(match[2], 10);
      const day = parseInt(match[3], 10);
      if (month < 1 || month > 12) return NaN;
      const isLeap = (year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0));
      const daysInMonth = [31, isLeap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
      if (day < 1 || day > daysInMonth[month - 1]) return NaN;
      if (match[4] !== undefined) {
        const hour = parseInt(match[4], 10);
        const min = parseInt(match[5], 10);
        const sec = parseFloat(match[6] || '0');
        if (hour > 23 || min > 59 || sec >= 60) return NaN;
      }
      time = Date.parse(s);
      if (!Number.isFinite(time)) return NaN;
    } else if (raw instanceof Date) {
      time = raw.getTime();
      if (!Number.isFinite(time)) return NaN;
    } else {
      return NaN;
    }

    if (maxTime !== undefined && maxTime !== null) {
      if (time > maxTime) return NaN;
    }
    return time;
  }

  function extractSpaceFromUid(uid) {
    if (typeof uid === 'string' && uid.startsWith('confluence:unresolved:')) {
      const parts = uid.split(':');
      if (parts.length >= 3 && parts[2]) return parts[2];
    }
    return null;
  }

  function extractProjectFromJiraUid(uid) {
    if (typeof uid === 'string') {
      const match = /^jira:([A-Za-z0-9_]+)-\d+/i.exec(uid);
      if (match) return match[1].toUpperCase();
    }
    return null;
  }

  function extractProjectFromGitlabUid(uid) {
    if (typeof uid === 'string' && uid.startsWith('gitlab:')) {
      const parts = uid.split(':');
      parts.pop(); // remove commit sha
      return parts.slice(1).join(':') || null;
    }
    return null;
  }

  function build(data, options) {
    const rawNodes = (data && data.nodes) ? data.nodes : [];
    const rawEdges = (data && data.edges) ? data.edges : [];
    const n = rawNodes.length;

    const jiraTimeField = (options && options.jiraTimeField === 'created') ? 'created' : 'updated';
    const nowThreshold = (options && options.now !== undefined)
      ? (typeof options.now === 'number' ? options.now : (options.now instanceof Date ? options.now.getTime() : Date.parse(options.now)))
      : (Date.now() + 60000); // 60s tolerance for clock drift

    // 1. Classify nodes and parse timestamps
    const timestamps = new Float64Array(n);
    const isGitlab = new Uint8Array(n);
    const isJira = new Uint8Array(n);
    const isConf = new Uint8Array(n);
    const nodeSpaces = new Array(n);
    const uidToIndex = new Map();

    let minTime = null;
    let maxTime = null;
    let unknownCount = 0;

    for (let i = 0; i < n; i++) {
      const node = rawNodes[i];
      if (!node) {
        timestamps[i] = NaN;
        unknownCount++;
        continue;
      }

      if (node.uid !== undefined && node.uid !== null) {
        uidToIndex.set(String(node.uid), i);
      }

      const uidStr = typeof node.uid === 'string' ? node.uid : '';
      const typeStr = typeof node.type === 'string' ? node.type.toLowerCase() : '';

      const gitlab = typeStr === 'gitlab' || uidStr.startsWith('gitlab:');
      const jira = !gitlab && (typeStr === 'jira' || uidStr.startsWith('jira:'));
      const conf = !gitlab && !jira && (typeStr === 'confluence' || uidStr.startsWith('confluence:') || (!typeStr && !uidStr.startsWith('jira:') && !uidStr.startsWith('gitlab:')));

      isGitlab[i] = gitlab ? 1 : 0;
      isJira[i] = jira ? 1 : 0;
      isConf[i] = conf ? 1 : 0;

      // Determine space/project
      if (gitlab) {
        const p = (typeof node.project === 'string' && node.project.trim()) ||
                  (typeof node.project_id === 'string' && node.project_id.trim()) ||
                  (typeof node.space_or_project === 'string' && node.space_or_project.trim()) ||
                  extractProjectFromGitlabUid(node.uid) ||
                  'default';
        nodeSpaces[i] = p;
      } else if (conf) {
        const s = (typeof node.space === 'string' && node.space.trim()) ||
                  (typeof node.project === 'string' && node.project.trim()) ||
                  (typeof node.space_key === 'string' && node.space_key.trim()) ||
                  (typeof node.space_or_project === 'string' && node.space_or_project.trim()) ||
                  extractSpaceFromUid(node.uid) ||
                  'default';
        nodeSpaces[i] = s;
      } else if (jira) {
        const p = (typeof node.project === 'string' && node.project.trim()) ||
                  (typeof node.project_key === 'string' && node.project_key.trim()) ||
                  (typeof node.space_or_project === 'string' && node.space_or_project.trim()) ||
                  (typeof node.space === 'string' && node.space.trim()) ||
                  extractProjectFromJiraUid(node.uid) ||
                  'default';
        nodeSpaces[i] = p;
      } else {
        nodeSpaces[i] = (typeof node.project === 'string' && node.project.trim()) || 'default';
      }

      // Parse timestamp
      let rawTime;
      if (gitlab) {
        rawTime = node.source_updated_at !== undefined ? node.source_updated_at :
                 (node.source_created_at !== undefined ? node.source_created_at :
                 (node.committed_date !== undefined ? node.committed_date :
                 (node.committed_at !== undefined ? node.committed_at :
                 (node.authored_date !== undefined ? node.authored_date :
                 (node.updated_at !== undefined ? node.updated_at : node.created_at)))));
      } else if (jira) {
        if (jiraTimeField === 'created') {
          rawTime = node.source_created_at !== undefined ? node.source_created_at : node.created_at;
        } else {
          rawTime = node.source_updated_at !== undefined ? node.source_updated_at : node.updated_at;
        }
      } else {
        // Confluence uses source_updated_at exclusively
        rawTime = node.source_updated_at !== undefined ? node.source_updated_at : node.updated_at;
      }

      const t = parseTimestamp(rawTime, nowThreshold);
      timestamps[i] = t;

      if (Number.isNaN(t)) {
        unknownCount++;
      } else {
        if (minTime === null || t < minTime) minTime = t;
        if (maxTime === null || t > maxTime) maxTime = t;
      }
    }

    // 2. Scan edges to associate Jira tickets with dominant Confluence space (O(m))
    const jiraSpaceLinks = new Array(n);

    function recordJiraLink(jIdx, space) {
      if (!space) return;
      let m = jiraSpaceLinks[jIdx];
      if (!m) {
        m = new Map();
        jiraSpaceLinks[jIdx] = m;
      }
      m.set(space, (m.get(space) || 0) + 1);
    }

    function resolveNodeIdx(ref) {
      if (typeof ref === 'number') return (ref >= 0 && ref < n) ? ref : -1;
      if (typeof ref === 'string') {
        const idx = uidToIndex.get(ref);
        return idx !== undefined ? idx : -1;
      }
      if (ref && typeof ref === 'object') {
        if (typeof ref.idx === 'number' && ref.idx >= 0 && ref.idx < n) return ref.idx;
        if (ref.uid !== undefined && ref.uid !== null) {
          const idx = uidToIndex.get(String(ref.uid));
          return idx !== undefined ? idx : -1;
        }
      }
      return -1;
    }

    const mCount = rawEdges.length;
    for (let e = 0; e < mCount; e++) {
      const edge = rawEdges[e];
      if (!edge) continue;

      const uIdx = resolveNodeIdx(edge.source);
      const vIdx = resolveNodeIdx(edge.target);

      if (uIdx >= 0 && vIdx >= 0) {
        const uJira = isJira[uIdx], uConf = isConf[uIdx];
        const vJira = isJira[vIdx], vConf = isConf[vIdx];

        if (uJira && vConf) {
          recordJiraLink(uIdx, nodeSpaces[vIdx]);
        } else if (vJira && uConf) {
          recordJiraLink(vIdx, nodeSpaces[uIdx]);
        }
      } else if (uIdx >= 0 && isJira[uIdx]) {
        const targetRef = typeof edge.target === 'string' ? edge.target : (edge.target && edge.target.uid);
        const unresolvedSpace = extractSpaceFromUid(targetRef);
        if (unresolvedSpace) {
          recordJiraLink(uIdx, unresolvedSpace);
        }
      } else if (vIdx >= 0 && isJira[vIdx]) {
        const sourceRef = typeof edge.source === 'string' ? edge.source : (edge.source && edge.source.uid);
        const unresolvedSpace = extractSpaceFromUid(sourceRef);
        if (unresolvedSpace) {
          recordJiraLink(vIdx, unresolvedSpace);
        }
      }
    }

    // 3. Form clusters
    const clusterMap = new Map();

    for (let i = 0; i < n; i++) {
      let clusterId;
      let clusterLabel;

      if (isConf[i]) {
        const space = nodeSpaces[i];
        clusterId = 'space:' + space;
        clusterLabel = space;
      } else if (isJira[i]) {
        const spaceLinks = jiraSpaceLinks[i];
        if (spaceLinks && spaceLinks.size > 0) {
          // Find dominant space (highest count; alphabetical tie-break)
          let dominantSpace = null;
          let maxCount = -1;
          for (const [space, count] of spaceLinks.entries()) {
            if (count > maxCount || (count === maxCount && (dominantSpace === null || space < dominantSpace))) {
              dominantSpace = space;
              maxCount = count;
            }
          }
          clusterId = 'space:' + dominantSpace;
          clusterLabel = dominantSpace;
        } else {
          // No Confluence links -> cluster by Jira project
          const proj = nodeSpaces[i];
          clusterId = 'jira:' + proj;
          clusterLabel = proj;
        }
      } else if (isGitlab[i]) {
        const proj = nodeSpaces[i];
        clusterId = 'gitlab:' + proj;
        clusterLabel = proj;
      } else {
        const proj = nodeSpaces[i];
        clusterId = 'other:' + proj;
        clusterLabel = proj;
      }

      let cObj = clusterMap.get(clusterId);
      if (!cObj) {
        cObj = {
          id: clusterId,
          label: clusterLabel,
          members: []
        };
        clusterMap.set(clusterId, cObj);
      }
      cObj.members.push(i);
    }

    // Sort clusters deterministically: count descending, then ID ascending
    const clusters = Array.from(clusterMap.values()).map(c => {
      // Members sorted by original index for complete stability under time toggle
      c.members.sort((a, b) => a - b);
      const count = c.members.length;
      const radius = count > 0 ? (NODE_SPACING * Math.sqrt(count - 1) + 48) : 48;
      return {
        id: c.id,
        label: c.label,
        x: 0,
        y: 0,
        radius,
        count,
        members: c.members
      };
    });

    clusters.sort((a, b) => b.count - a.count || (a.id < b.id ? -1 : (a.id > b.id ? 1 : 0)));

    // Disambiguate identical labels (e.g. Confluence space "KAFKA" vs Jira project "KAFKA")
    const labelCounts = new Map();
    for (const c of clusters) {
      labelCounts.set(c.label, (labelCounts.get(c.label) || 0) + 1);
    }
    for (const c of clusters) {
      if (labelCounts.get(c.label) > 1) {
        if (c.id.startsWith('space:') || c.id.startsWith('conf:')) {
          c.label = `${c.label} (Space)`;
        } else if (c.id.startsWith('jira:')) {
          c.label = `${c.label} (Projekt)`;
        } else if (c.id.startsWith('gitlab:')) {
          c.label = `${c.label} (GitLab)`;
        } else if (c.id.startsWith('other:')) {
          c.label = `${c.label} (Sonstige)`;
        }
      }
    }

    // 4. Spread clusters in XY
    const C = clusters.length;
    if (C === 1) {
      clusters[0].x = 0;
      clusters[0].y = 0;
    } else if (C > 1) {
      let maxClusterRadius = 0;
      for (let c = 0; c < C; c++) {
        if (clusters[c].radius > maxClusterRadius) maxClusterRadius = clusters[c].radius;
      }

      if (C <= 500) {
        // Spiral packing with spatial hash grid
        const cell = 2 * maxClusterRadius + CLUSTER_PADDING;
        const occupied = new Map();
        for (let c = 0; c < C; c++) {
          const r_c = clusters[c].radius;
          for (let attempt = 0; ; attempt++) {
            const distance = cell * 0.6 * Math.sqrt(attempt);
            const x = Math.cos(attempt * GOLDEN_ANGLE) * distance;
            const y = Math.sin(attempt * GOLDEN_ANGLE) * distance;
            const gx = Math.floor(x / cell);
            const gy = Math.floor(y / cell);
            let overlap = false;
            for (let dx = -1; dx <= 1 && !overlap; dx++) {
              for (let dy = -1; dy <= 1 && !overlap; dy++) {
                const list = occupied.get(`${gx + dx},${gy + dy}`);
                if (list) {
                  for (let oIdx = 0; oIdx < list.length; oIdx++) {
                    const other = clusters[list[oIdx]];
                    if (Math.hypot(x - other.x, y - other.y) < r_c + other.radius + CLUSTER_PADDING) {
                      overlap = true;
                      break;
                    }
                  }
                }
              }
            }
            if (overlap) continue;
            clusters[c].x = x;
            clusters[c].y = y;
            const key = `${gx},${gy}`;
            let list = occupied.get(key);
            if (!list) {
              list = [];
              occupied.set(key, list);
            }
            list.push(c);
            break;
          }
        }
      } else {
        // Direct grid layout for very large cluster counts (> 500)
        const cols = Math.ceil(Math.sqrt(C));
        const cell = 2 * maxClusterRadius + CLUSTER_PADDING;
        const rows = Math.ceil(C / cols);
        for (let c = 0; c < C; c++) {
          const col = c % cols;
          const row = Math.floor(c / cols);
          clusters[c].x = (col - (cols - 1) / 2) * cell;
          clusters[c].y = (row - (rows - 1) / 2) * cell;
        }
      }
    }

    // 5. Generate outputs: positions Float32Array, clusterIndices Uint32Array
    const positions = new Float32Array(3 * n);
    const clusterIndices = new Uint32Array(n);
    const hasTimeSpan = minTime !== null && maxTime !== null && maxTime > minTime;

    for (let c = 0; c < C; c++) {
      const cluster = clusters[c];
      const members = cluster.members;
      const memCount = members.length;
      for (let m = 0; m < memCount; m++) {
        const nodeIdx = members[m];
        clusterIndices[nodeIdx] = c;

        // Phyllotaxis spiral for node packing inside cluster
        const dist = NODE_SPACING * Math.sqrt(m);
        const angle = m * GOLDEN_ANGLE;
        const localX = Math.cos(angle) * dist;
        const localY = Math.sin(angle) * dist;

        // Deterministic jitter
        const jx = (lcg(nodeIdx * 2 + 1) - 0.5) * 6;
        const jy = (lcg(nodeIdx * 2 + 2) - 0.5) * 6;

        positions[3 * nodeIdx] = cluster.x + localX + jx;
        positions[3 * nodeIdx + 1] = cluster.y + localY + jy;

        // Z coordinate: newest z=0, older more negative down to -DEPTH
        const t = timestamps[nodeIdx];
        if (Number.isNaN(t)) {
          positions[3 * nodeIdx + 2] = UNKNOWN_Z;
        } else if (!hasTimeSpan) {
          positions[3 * nodeIdx + 2] = 0; // equal-date stable z0
        } else {
          const ratio = (maxTime - t) / (maxTime - minTime);
          const z = -ratio * DEPTH;
          positions[3 * nodeIdx + 2] = (z === 0 ? 0 : z);
        }
      }
      delete cluster.members;
    }

    return {
      positions,
      clusters,
      clusterIndices,
      timestamps,
      minTime,
      maxTime,
      unknownCount,
      depth: DEPTH
    };
  }

  return {
    build,
    DEPTH,
    UNKNOWN_Z
  };
});
