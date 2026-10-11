/**
 * Lokaler Knowledge Graph – Frontend-Logik.
 * Vanilla JS, keine externen Abhängigkeiten. Daten werden ausschließlich per
 * createElement/textContent gerendert (CSP: default-src 'self').
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------ helpers

  const $ = (id) => document.getElementById(id);

  function el(tag, opts = {}, children = []) {
    const node = document.createElement(tag);
    if (opts.className) node.className = opts.className;
    if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
    if (opts.attrs) {
      for (const [k, v] of Object.entries(opts.attrs)) {
        if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? '' : String(v));
      }
    }
    if (opts.on) {
      for (const [evt, fn] of Object.entries(opts.on)) node.addEventListener(evt, fn);
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  class ApiError extends Error {
    constructor(message, status, code) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  async function api(method, path, body, signal) {
    const opts = { method, headers: { Accept: 'application/json' }, signal };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      throw new ApiError('Server nicht erreichbar. Läuft „python app.py serve“?', 0, 'network');
    }
    let data = null;
    try {
      data = await res.json();
    } catch (err) {
      data = null;
    }
    if (!res.ok) {
      const e = (data && data.error) || {};
      throw new ApiError(e.message || `HTTP ${res.status}`, res.status, e.code || 'error');
    }
    return data;
  }

  const dateFmt = new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' });
  function formatDate(value) {
    if (!value) return '';
    const d = new Date(value);
    return isNaN(d.getTime()) ? String(value) : dateFmt.format(d);
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '—';
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h) return `${h} h ${m} min`;
    if (m) return `${m} min ${sec} s`;
    return `${sec} s`;
  }

  const fmtNum = (n) => (typeof n === 'number' ? n.toLocaleString('de-DE') : (n ?? '—'));

  function showFeedback(container, message, kind) {
    clear(container);
    if (!message) return;
    container.appendChild(el('div', {
      className: `feedback-msg ${kind === 'error' ? 'feedback-error' : 'feedback-success'}`,
      text: message,
      attrs: { role: kind === 'error' ? 'alert' : 'status' },
    }));
  }

  const TYPE_LABEL = { confluence: 'Confluence', jira: 'Jira' };
  const STATE_LABEL = {
    discovered: 'Referenz (nicht indexiert)', indexed: 'Indexiert', skipped: 'Übersprungen',
    missing: 'Nicht gefunden', forbidden: 'Kein Zugriff', error: 'Fehler',
  };
  const JOB_STATE_LABEL = {
    running: 'Läuft', stopping: 'Wird gestoppt …', stopped: 'Gestoppt', completed: 'Abgeschlossen',
    failed: 'Fehlgeschlagen', interrupted: 'Unterbrochen',
  };
  const EDGE_LABEL = {
    links_to: 'Seitenlink', parent_of: 'Kindseite', mentions_jira: 'Erwähnt Jira-Ticket',
    jira_relates_to: 'Jira-Verknüpfung', jira_parent_of: 'Jira-Hierarchie',
    links_to_confluence: 'Jira → Confluence',
  };
  const FIELD_LABEL = {
    body: 'Seiteninhalt', 'ac:link': 'Confluence-Link (ac:link)', 'jira-macro': 'Jira-Makro',
    children: 'Kindseiten-Liste', plain_text: 'Fließtext-Erkennung', issuelinks: 'Jira-Issue-Link',
    parent: 'Jira-Parent', subtasks: 'Jira-Subtasks', remotelink: 'Jira-Remote-Link',
    'title-resolved': 'Titel-Link (aufgelöst)',
  };

  // ------------------------------------------------------------------ state

  const state = {
    config: null,       // /api/config
    stats: null,        // /api/stats
    currentTab: 'search',
    job: null,
    pollTimer: null,
    graph: null,
    graph2d: null,
    flightGraph: null,
    graphData: null,
    graphFlight: false,
    timelineAxis: false,
    graphImportance: false,
    graphViewGeneration: 0,
    graphLoadedKey: null,
    graphRequest: null,
    settings: null,
    spaces: null,        // /api/spaces
    spacesFetchedAt: null,
  };

  // ------------------------------------------------------------------ routing

  const TABS = ['search', 'index', 'graph', 'top', 'settings'];

  function parseHash() {
    const raw = window.location.hash.replace(/^#/, '');
    const [name, query] = raw.split('?');
    const params = new URLSearchParams(query || '');
    return { tab: TABS.includes(name) ? name : 'search', params };
  }

  function setHash(tab, params) {
    const qs = params ? new URLSearchParams(params).toString() : '';
    const next = `#${tab}${qs ? '?' + qs : ''}`;
    if (window.location.hash !== next) window.location.hash = next;
    else onRoute();
  }

  /** Size the graph view to the remaining window height (banners vary in height). */
  function fitGraphViewport() {
    const container = document.querySelector('.graph-layout-container');
    if (!container || state.currentTab !== 'graph') return;
    const top = container.getBoundingClientRect().top + window.scrollY;
    container.style.height = `${Math.max(420, window.innerHeight - top)}px`;
  }

  function activateTab(tab) {
    state.currentTab = tab;
    for (const t of TABS) {
      const btn = $(`tab-${t}`);
      const panel = $(`panel-${t}`);
      const active = t === tab;
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
      btn.tabIndex = active ? 0 : -1;
      panel.hidden = !active;
      panel.classList.toggle('active', active);
    }
    fitGraphViewport();
    if (state.flightGraph) state.flightGraph.setActive(tab === 'graph' && state.graphFlight);
  }

  function onRoute() {
    const { tab, params } = parseHash();
    activateTab(tab);
    if (tab === 'search') routeSearch(params);
    else if (tab === 'index') refreshIndexTab();
    else if (tab === 'graph') routeGraph(params);
    else if (tab === 'top') routeTop(params);
    else if (tab === 'settings') loadSettings();
  }

  function initTabs() {
    const list = document.querySelector('.nav-tablist');
    TABS.forEach((t, idx) => {
      const btn = $(`tab-${t}`);
      btn.addEventListener('click', () => {
        if (t === 'search') {
          const q = $('search-input').value.trim();
          setHash('search', q ? { q } : null);
        } else if (t === 'graph' && state.graphLoadedKey) {
          activateTab('graph');
        } else {
          setHash(t);
        }
      });
      btn.addEventListener('keydown', (e) => {
        let next = null;
        if (e.key === 'ArrowRight') next = TABS[(idx + 1) % TABS.length];
        else if (e.key === 'ArrowLeft') next = TABS[(idx - 1 + TABS.length) % TABS.length];
        else if (e.key === 'Home') next = TABS[0];
        else if (e.key === 'End') next = TABS[TABS.length - 1];
        if (next) {
          e.preventDefault();
          $(`tab-${next}`).focus();
          $(`tab-${next}`).click();
        }
      });
    });
    list.setAttribute('aria-orientation', 'horizontal');
  }

  // ------------------------------------------------------------------ global data

  async function loadConfig() {
    state.config = await api('GET', '/api/config');
    renderGlobalBanners();
    return state.config;
  }

  async function loadStats() {
    try {
      state.stats = await api('GET', '/api/stats');
    } catch (err) {
      state.stats = null;
      return null;
    }
    const s = state.stats;
    const indexed = (s.indexed?.confluence || 0) + (s.indexed?.jira || 0);
    $('header-doc-count').textContent = `${fmtNum(indexed)} Dokumente indexiert`;
    $('stat-docs-total').textContent = fmtNum(indexed);
    $('stat-docs-breakdown').textContent =
      `Confluence: ${fmtNum(s.indexed?.confluence || 0)} · Jira: ${fmtNum(s.indexed?.jira || 0)} · ` +
      `Knoten gesamt: ${fmtNum(s.documents_total || 0)}`;
    $('stat-edges-total').textContent = fmtNum(s.edges_total || 0);
    const kinds = Object.entries(s.edges_by_kind || {})
      .map(([k, v]) => `${EDGE_LABEL[k] || k}: ${fmtNum(v)}`).join(' · ');
    $('stat-edges-sub').textContent = kinds || 'Noch keine Verknüpfungen';
    $('stat-chunks-total').textContent = fmtNum(s.chunks_total || 0);
    return s;
  }

  function renderGlobalBanners() {
    const box = $('global-banners');
    clear(box);
    requestAnimationFrame(fitGraphViewport);
    const cfg = state.config;
    if (!cfg) return;
    const missing = [];
    if (!cfg.configured?.confluence) missing.push('Confluence');
    if (!cfg.configured?.jira) missing.push('Jira');
    if (!missing.length) return;
    const link = el('a', { className: 'banner-action-link', text: 'Zu den Einstellungen', attrs: { href: '#settings' } });
    box.appendChild(el('div', { className: `banner ${cfg.configured?.confluence ? 'banner-info' : 'banner-warning'}`, attrs: { role: 'status' } }, [
      `${missing.join(' und ')} ${missing.length > 1 ? 'sind' : 'ist'} noch nicht konfiguriert. `,
      link,
    ]));
  }

  // ------------------------------------------------------------------ search

  let searchSeq = 0;

  function initSearch() {
    const form = $('search-form');
    const input = $('search-input');
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      submitSearch();
    });
    input.addEventListener('input', () => {
      $('search-clear-btn').hidden = !input.value;
    });
    $('search-clear-btn').addEventListener('click', () => {
      input.value = '';
      $('search-clear-btn').hidden = true;
      setHash('search');
      input.focus();
    });
    document.querySelectorAll('input[name="search-type"]').forEach((r) => {
      r.addEventListener('change', () => { if (input.value.trim()) submitSearch(); });
    });
    $('search-project-select').addEventListener('change', () => { if (input.value.trim()) submitSearch(); });
    const max = state.config?.search?.max_query_chars;
    if (max) input.maxLength = max;
  }

  function currentSearchType() {
    const checked = document.querySelector('input[name="search-type"]:checked');
    return checked ? checked.value : 'all';
  }

  function submitSearch() {
    const q = $('search-input').value.trim();
    const params = {};
    if (q) params.q = q;
    const type = currentSearchType();
    if (type !== 'all') params.type = type;
    const project = $('search-project-select').value;
    if (project) params.project = project;
    setHash('search', Object.keys(params).length ? params : null);
  }

  async function loadProjects() {
    const searchSelect = $('search-project-select');
    const topSelect = $('top-project-select');
    try {
      const data = await api('GET', '/api/projects');
      const curSearch = searchSelect ? searchSelect.value : '';
      const curTop = topSelect ? topSelect.value : '';
      if (searchSelect) {
        while (searchSelect.options.length > 1) searchSelect.remove(1);
      }
      if (topSelect) {
        while (topSelect.options.length > 1) topSelect.remove(1);
      }
      for (const p of data.projects || []) {
        if (!p.project) continue;
        const label = `${p.project} (${TYPE_LABEL[p.type] || p.type}, ${p.count})`;
        if (searchSelect) searchSelect.appendChild(el('option', { text: label, attrs: { value: p.project } }));
        if (topSelect) topSelect.appendChild(el('option', { text: label, attrs: { value: p.project } }));
      }
      if (searchSelect) searchSelect.value = curSearch;
      if (topSelect) topSelect.value = curTop;
    } catch (err) {
      /* projects are optional */
    }
  }

  async function routeSearch(params) {
    const q = params.get('q') || '';
    const type = params.get('type') || 'all';
    const project = params.get('project') || '';
    const input = $('search-input');
    if (input.value.trim() !== q) input.value = q;
    $('search-clear-btn').hidden = !q;
    const radio = $(`search-type-${type}`);
    if (radio) radio.checked = true;
    if (project && ![...$('search-project-select').options].some((o) => o.value === project)) {
      $('search-project-select').appendChild(el('option', { text: project, attrs: { value: project } }));
    }
    $('search-project-select').value = project;

    const container = $('search-view-container');
    const list = $('results-list');
    const status = $('search-status-area');
    const meta = $('search-meta-bar');

    if (!q) {
      container.classList.add('hero-mode');
      container.classList.remove('compact-mode');
      clear(list);
      meta.hidden = true;
      clear(status);
      await loadStats();
      if (state.stats && (state.stats.indexed?.confluence || 0) + (state.stats.indexed?.jira || 0) === 0) {
        status.appendChild(el('div', { className: 'empty-state-card' }, [
          el('div', { className: 'empty-state-title', text: 'Der lokale Index ist noch leer.' }),
          el('p', {}, ['Starte eine ', el('a', { text: 'Indexierung', attrs: { href: '#index' } }),
            ', um Confluence-Seiten und Jira-Tickets lokal durchsuchbar zu machen.']),
        ]));
      }
      input.focus();
      return;
    }

    container.classList.remove('hero-mode');
    container.classList.add('compact-mode');
    const seq = ++searchSeq;
    const qs = new URLSearchParams({ q, type });
    if (project) qs.set('project', project);
    let data;
    try {
      data = await api('GET', `/api/search?${qs}`);
    } catch (err) {
      if (seq !== searchSeq) return;
      clear(list);
      clear(status);
      meta.hidden = true;
      status.appendChild(el('div', { className: 'banner banner-danger', text: `Suche fehlgeschlagen: ${err.message}` }));
      return;
    }
    if (seq !== searchSeq) return;
    renderResults(data);
  }

  function renderResults(data) {
    const list = $('results-list');
    const status = $('search-status-area');
    const meta = $('search-meta-bar');
    clear(list);
    clear(status);
    const n = data.total_returned || 0;
    meta.hidden = false;
    const ms = typeof data.duration_ms === 'number' ? data.duration_ms.toLocaleString('de-DE', { maximumFractionDigits: 1 }) : '?';
    $('search-meta-count').textContent = `${n} ${n === 1 ? 'Treffer' : 'Treffer'} · ${ms} ms (lokal)`;
    if (!n) {
      status.appendChild(el('div', { className: 'empty-state-card' }, [
        el('div', { className: 'empty-state-title', text: 'Noch keine lokalen Treffer' }),
        el('p', { text: 'Andere Begriffe probieren oder weitere Seiten indexieren.' }),
      ]));
      return;
    }
    for (const r of data.results || []) list.appendChild(renderResult(r));
  }

  function renderResult(r) {
    const titleLink = el('a', {
      className: 'result-title-link',
      text: r.title || r.uid,
      attrs: { href: r.url || '#', target: '_blank', rel: 'noopener noreferrer' },
    });
    const header = el('div', { className: 'result-header-row' }, [
      titleLink,
      el('span', { className: `result-badge badge-${r.type}`, text: TYPE_LABEL[r.type] || r.type }),
    ]);
    const urlLine = el('div', { className: 'result-url-line', text: r.url || r.uid });
    const snippet = el('p', { className: 'result-snippet' });
    const parts = Array.isArray(r.snippet_parts) && r.snippet_parts.length ? r.snippet_parts : [{ text: r.snippet || '', hit: false }];
    for (const part of parts) {
      snippet.appendChild(part.hit ? el('mark', { text: part.text }) : document.createTextNode(part.text));
    }
    const tags = el('div', { className: 'result-meta-tags' });
    if (r.project) tags.appendChild(el('span', { className: 'meta-chip', text: r.project }));
    if (r.document_type) tags.appendChild(el('span', { className: 'meta-chip', text: r.document_type }));
    if (r.status) tags.appendChild(el('span', { className: 'meta-chip meta-chip-status', text: r.status }));
    if (r.updated_at) tags.appendChild(el('span', { className: 'meta-chip', text: `Aktualisiert ${formatDate(r.updated_at)}` }));
    const graphLink = el('a', {
      className: 'result-graph-link',
      text: 'Im Graph anzeigen →',
      attrs: { href: `#graph?${new URLSearchParams({ focus: r.uid })}` },
    });
    return el('li', { className: 'result-item' }, [header, urlLine, snippet,
      el('div', { className: 'result-footer-row' }, [tags, graphLink])]);
  }

  // ------------------------------------------------------------------ indexing

  const JOB_FORM_FIELDS = [
    'start_page', 'space_key', 'space_keys', 'modified_since',
    'max_confluence_pages', 'max_jira_issues', 'max_total_documents', 'max_links_per_document',
    'max_api_requests', 'max_duration_minutes', 'max_confluence_depth', 'max_jira_link_hops',
    'confluence_sleep_seconds', 'jira_sleep_seconds', 'request_timeout_seconds', 'max_retries',
    'batch_fetch_size',
    'include_child_pages', 'follow_confluence_links', 'index_linked_jira_issues', 'follow_jira_issue_links',
    'follow_jira_to_confluence', 'scan_plain_text_issue_keys', 'include_remote_links', 'resolve_page_titles',
    'refresh_existing',
  ];
  const fieldEl = (name) => $(`job-${name.replace(/_/g, '-')}`);
  let jobFormFilled = false;

  function fillJobForm(force) {
    if (jobFormFilled && !force) return;
    const defaults = state.config?.defaults || {};
    const limits = state.config?.limits || {};
    for (const name of JOB_FORM_FIELDS) {
      const input = fieldEl(name);
      if (!input) continue;
      const lim = limits[name];
      if (input.type === 'checkbox') {
        input.checked = !!defaults[name];
        // Ohne Jira-Basis-URL kann der Link-Follow nicht funktionieren und der
        // Job-Start würde sonst an der Validierung scheitern.
        if (name === 'index_linked_jira_issues' && !(state.config?.configured?.jira)) {
          input.checked = false;
        }
      } else {
        if (name === 'space_keys') {
          const value = Array.isArray(defaults[name]) ? defaults[name].join(', ') : (defaults[name] || '');
          if (!input.value) input.value = value;
          continue;
        }
        if (name === 'modified_since') {
          if (!input.value) input.value = defaults[name] || '';
          continue;
        }
        if (lim && input.type === 'number') {
          if (lim.min !== undefined && lim.min !== null) input.min = lim.min;
          if (lim.max !== undefined && lim.max !== null) input.max = lim.max;
          input.step = lim.type === 'float' ? (input.step && input.step !== 'any' ? input.step : '0.1') : '1';
          input.required = true;
        }
        if (defaults[name] !== undefined && defaults[name] !== null && ((name !== 'start_page' && name !== 'space_key') || !input.value)) {
          input.value = defaults[name];
        }
      }
    }
    jobFormFilled = true;
  }

  function collectJobForm({ allowMissingStartPage = false } = {}) {
    const limits = state.config?.limits || {};
    const payload = {};
    const errors = [];
    for (const name of JOB_FORM_FIELDS) {
      const input = fieldEl(name);
      if (!input) continue;
      if (input.type === 'checkbox') {
        payload[name] = input.checked;
        continue;
      }
      const raw = input.value.trim();
      const lim = limits[name] || {};
      const label = input.labels && input.labels[0] ? input.labels[0].textContent.replace('*', '').trim() : name;
      if (name === 'start_page') {
        payload[name] = raw;
        continue;
      }
      if (name === 'space_key') {
        payload[name] = raw.toUpperCase();
        continue;
      }
      if (name === 'space_keys') {
        // Schlüssel werden unverändert übernommen: Personal Spaces nutzen
        // case-sensitive Keys wie "~jsmith".
        payload[name] = raw.split(/[\s,;]+/).map((v) => v.trim()).filter(Boolean);
        continue;
      }
      if (name === 'modified_since') {
        payload[name] = raw;
        continue;
      }
      if (raw === '') { errors.push(`${label}: Pflichtfeld`); continue; }
      const num = Number(raw.replace(',', '.'));
      if (!Number.isFinite(num)) { errors.push(`${label}: Zahl erwartet`); continue; }
      if (lim.type === 'int' && !Number.isInteger(num)) { errors.push(`${label}: Ganzzahl erwartet`); continue; }
      if ((lim.min !== undefined && num < lim.min) || (lim.max !== undefined && num > lim.max)) {
        errors.push(`${label}: erlaubt ${lim.min}–${lim.max}`);
        continue;
      }
      payload[name] = num;
    }
    const hasSpaceSelection = Array.isArray(payload.space_keys) && payload.space_keys.length > 0;
    if (!allowMissingStartPage && !payload.start_page && !payload.space_key
        && !payload.modified_since && !hasSpaceSelection) {
      errors.push('Bitte eine Confluence-Start-URL / Page-ID, einen Space-Key, '
        + 'eine Space-Auswahl oder ein Änderungsdatum angeben.');
    }
    return { payload, errors };
  }

  function initIndexing() {
    const form = $('indexing-form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fb = $('indexing-feedback');
      const { payload, errors } = collectJobForm();
      if (errors.length) {
        showFeedback(fb, errors.join(' · '), 'error');
        return;
      }
      // Eine explizit eingetragene Startseite/Space-Key gewinnt gegen die
      // Modus-Felder aus den Standardwerten.
      if (payload.start_page) {
        payload.modified_since = '';
        payload.space_keys = [];
      } else if (payload.modified_since && payload.space_key && !payload.space_keys.length) {
        // Ein einzelner Space-Key dient dem Änderungs-Modus als Filter.
        payload.space_keys = [payload.space_key];
        payload.space_key = '';
      }
      $('job-start-btn').disabled = true;
      try {
        const res = await api('POST', '/api/jobs', payload);
        showFeedback(fb, `Job #${res.job_id} gestartet.`, 'success');
        await pollJob();
      } catch (err) {
        showFeedback(fb, err.message, 'error');
        updateJobButtons();
      }
    });

    async function triggerRefreshExisting() {
      const fb = $('indexing-feedback');
      const { payload, errors } = collectJobForm({ allowMissingStartPage: true });
      if (errors.length) {
        showFeedback(fb, errors.join(' · '), 'error');
        return;
      }
      payload.refresh_only = true;
      payload.refresh_existing = true;
      const startBtn = $('job-start-btn');
      const refHeaderBtn = $('job-refresh-existing-btn');
      const refFormBtn = $('job-refresh-existing-form-btn');
      if (startBtn) startBtn.disabled = true;
      if (refHeaderBtn) refHeaderBtn.disabled = true;
      if (refFormBtn) refFormBtn.disabled = true;
      try {
        const res = await api('POST', '/api/jobs', payload);
        showFeedback(fb, `Aktualisierungs-Job #${res.job_id} gestartet.`, 'success');
        await pollJob();
      } catch (err) {
        showFeedback(fb, err.message, 'error');
        updateJobButtons();
      }
    }

    const refHeaderBtn = $('job-refresh-existing-btn');
    if (refHeaderBtn) {
      refHeaderBtn.addEventListener('click', triggerRefreshExisting);
    }
    const refFormBtn = $('job-refresh-existing-form-btn');
    if (refFormBtn) {
      refFormBtn.addEventListener('click', triggerRefreshExisting);
    }

    // "Save as defaults" persists the current form as crawl_defaults.
    const actions = form.querySelector('.form-actions');
    const saveDefaults = el('button', {
      className: 'btn btn-secondary', text: 'Als Standard speichern',
      attrs: { type: 'button', title: 'Aktuelle Werte als Standard für künftige Jobs speichern' },
    });
    actions.insertBefore(saveDefaults, actions.firstChild);
    saveDefaults.addEventListener('click', async () => {
      const fb = $('indexing-feedback');
      const { payload, errors } = collectJobForm();
      const realErrors = errors.filter((m) => !m.startsWith('Bitte eine Confluence'));
      if (realErrors.length) { showFeedback(fb, realErrors.join(' · '), 'error'); return; }
      const crawl = { ...payload };
      const confSleep = crawl.confluence_sleep_seconds;
      const jiraSleep = crawl.jira_sleep_seconds;
      delete crawl.confluence_sleep_seconds;
      delete crawl.jira_sleep_seconds;
      // Modus-Felder sind Einmal-Eingaben und dürfen nicht als Standard
      // gespeichert werden – sonst verdrängen sie später die Startseite.
      crawl.modified_since = '';
      crawl.space_keys = [];
      try {
        await api('PUT', '/api/settings', {
          crawl_defaults: crawl,
          confluence: { sleep_seconds: confSleep },
          jira: { sleep_seconds: jiraSleep },
        });
        await loadConfig();
        showFeedback(fb, 'Standardwerte gespeichert.', 'success');
      } catch (err) {
        showFeedback(fb, err.message, 'error');
      }
    });

    const resetBtn = el('button', { className: 'btn btn-ghost', text: 'Standardwerte laden', attrs: { type: 'button' } });
    actions.insertBefore(resetBtn, actions.firstChild);
    resetBtn.addEventListener('click', () => { fillJobForm(true); showFeedback($('indexing-feedback'), '', ''); });

    $('job-stop-btn').addEventListener('click', async () => {
      if (!state.job) return;
      $('job-stop-btn').disabled = true;
      $('job-stop-btn').textContent = 'Wird gestoppt …';
      try {
        const res = await api('POST', `/api/jobs/${state.job.id}/stop`);
        renderJob(res.job);
        schedulePoll();
      } catch (err) {
        showFeedback($('indexing-feedback'), err.message, 'error');
        updateJobButtons();
      }
    });
  }

  async function refreshIndexTab() {
    fillJobForm(false);
    await Promise.all([loadStats(), pollJob()]);
  }

  const isActive = (job) => job && (job.state === 'running' || job.state === 'stopping');

  async function pollJob() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    try {
      const data = await api('GET', '/api/jobs/current');
      const wasActive = isActive(state.job);
      renderJob(data.job);
      if (wasActive && !isActive(data.job)) {
        loadStats();
        loadProjects();
        state.graphLoadedKey = null; // graph may have changed
      } else if (isActive(data.job) && state.currentTab === 'index') {
        loadStats();
      }
    } catch (err) {
      /* keep polling; server may restart */
    }
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (isActive(state.job)) state.pollTimer = setTimeout(pollJob, 1500);
  }

  function updateJobButtons() {
    const active = isActive(state.job);
    const startBtn = $('job-start-btn');
    if (startBtn) {
      startBtn.disabled = active;
      startBtn.textContent = active ? 'Indexierung läuft …' : 'Indexierung starten';
    }
    const refHeaderBtn = $('job-refresh-existing-btn');
    if (refHeaderBtn) {
      refHeaderBtn.disabled = active;
    }
    const refFormBtn = $('job-refresh-existing-form-btn');
    if (refFormBtn) {
      refFormBtn.disabled = active;
    }
    const stop = $('job-stop-btn');
    if (stop) {
      stop.disabled = !active || state.job.state === 'stopping';
      stop.textContent = state.job && state.job.state === 'stopping' ? 'Wird gestoppt …' : 'Indexierung stoppen';
    }
    const indicator = $('job-active-indicator');
    if (indicator) indicator.hidden = !active;
  }

  function renderJob(job) {
    state.job = job || null;
    updateJobButtons();
    const card = $('job-status-card');
    if (!job) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const badge = $('job-status-badge');
    badge.className = `job-status-badge badge-${job.state}`;
    badge.textContent = JOB_STATE_LABEL[job.state] || job.state;
    $('job-id-title').textContent = `Job #${job.id}${job.start_page_uid ? ' · ' + job.start_page_uid : ''}`;
    const elapsed = job.elapsed_seconds;
    $('job-elapsed-time').textContent = `Dauer: ${formatDuration(elapsed)}` +
      (job.finished_at ? ` · beendet ${formatDate(job.finished_at)}` : '');
    $('job-pages-indexed').textContent = fmtNum(job.pages_indexed || 0);
    $('job-jira-indexed').textContent = fmtNum(job.jira_indexed || 0);
    $('job-api-requests').textContent = fmtNum(job.api_requests || 0);
    $('job-skipped').textContent = fmtNum(job.skipped || 0);
    $('job-errors').textContent = fmtNum(job.errors || 0);
    $('job-queue-size').textContent = fmtNum(job.queue_size ?? 0);
    $('job-reused').textContent = fmtNum(job.reused_documents || 0);
    $('job-current-uid').textContent = job.current_uid || '—';
    $('job-last-message').textContent = job.last_message || '—';

    const waitBanner = $('job-wait-banner');
    const waiting = isActive(job) && job.phase === 'rate_limit_wait' && job.retry_wait_seconds;
    waitBanner.hidden = !waiting;
    if (waiting) {
      const reason = { sleep: 'Sleep zwischen Requests', '429': 'HTTP 429 – Rate Limit', retry: 'Wiederholung nach Fehler' }[job.wait_reason] || 'Wartezeit';
      const src = TYPE_LABEL[job.wait_source] || job.wait_source || '';
      $('job-wait-title').textContent = job.wait_reason === '429' ? 'Rate-Limit erreicht' : 'Wartephase';
      $('job-wait-desc').textContent = `Warte ${Number(job.retry_wait_seconds).toLocaleString('de-DE', { maximumFractionDigits: 1 })} s (${src}${src ? ', ' : ''}${reason}). Stoppen ist jederzeit möglich.`;
    }

    renderBudgets(job.budget || {});
    renderJobLog(job.log || []);
  }

  const BUDGET_LABEL = {
    api_requests: 'API-Requests', confluence_pages: 'Confluence-Seiten', jira_issues: 'Jira-Tickets',
    total_documents: 'Dokumente gesamt', duration_seconds: 'Zeitbudget',
  };

  function renderBudgets(budget) {
    const list = $('job-budgets-list');
    clear(list);
    for (const [key, label] of Object.entries(BUDGET_LABEL)) {
      const b = budget[key];
      if (!b || !b.max) continue;
      const ratio = Math.min(1, (b.used || 0) / b.max);
      const usedText = key === 'duration_seconds'
        ? `${formatDuration(b.used)} / ${formatDuration(b.max)}`
        : `${fmtNum(b.used || 0)} / ${fmtNum(b.max)}`;
      const fill = el('div', { className: `budget-bar-fill${ratio >= 1 ? ' danger' : ratio >= 0.8 ? ' warning' : ''}` });
      fill.style.width = `${(ratio * 100).toFixed(1)}%`;
      list.appendChild(el('div', { className: 'budget-bar-item' }, [
        el('div', { className: 'budget-bar-labels' }, [el('span', { text: label }), el('span', { text: usedText })]),
        el('div', {
          className: 'budget-bar-track',
          attrs: { role: 'meter', 'aria-label': label, 'aria-valuemin': 0, 'aria-valuemax': b.max, 'aria-valuenow': b.used || 0 },
        }, [fill]),
      ]));
    }
    if (budget.reused_documents) {
      list.appendChild(el('div', { className: 'field-hint', text: `${fmtNum(budget.reused_documents)} bereits indexierte Dokumente wiederverwendet (ohne API-Abruf).` }));
    }
  }

  function renderJobLog(entries) {
    const tbody = $('job-log-tbody');
    clear(tbody);
    $('job-log-count').textContent = String(entries.length);
    for (const entry of entries) {
      const level = (entry.level || 'info').toLowerCase();
      tbody.appendChild(el('tr', {}, [
        el('td', { text: formatDate(entry.created_at) }),
        el('td', {}, [el('span', { className: `log-level-badge log-level-${level === 'warn' ? 'warning' : level}`, text: level })]),
        el('td', { text: entry.document_uid || '' }),
        el('td', { text: entry.message || '' }),
      ]));
    }
  }

  // ------------------------------------------------------------------ graph

  const EDGE_KINDS = ['links_to', 'parent_of', 'mentions_jira', 'jira_relates_to', 'jira_parent_of', 'links_to_confluence'];

  function initGraph() {
    if (!window.KnowledgeGraph) return;
    state.graph = new window.KnowledgeGraph({
      canvasEl: $('graph-canvas'),
      containerEl: $('graph-canvas-container'),
      onNodeSelect: (node) => inspectNode(node.uid),
      onNodeActivate: (node) => setHash('graph', { focus: node.uid }),
      onEdgeSelect: (edge) => inspectEdge(edge),
      onLayoutProgress: (progress, done) => {
        if (state.graph !== state.graph2d) return;
        const overlay = $('graph-loading-overlay');
        overlay.hidden = done || state.graph.nodes.length < 400 || progress > 0;
        $('graph-loading-text').textContent = `Layout wird berechnet … ${Math.round(progress * 100)} %`;
        const banner = $('graph-layout-progress');
        if (banner) {
          banner.hidden = done && state.graph.nodes.length <= 5000;
          banner.textContent = done && state.graph.nodes.length > 5000 ? `Graph wird gezeichnet … ${fmtNum(state.graph.nodes.length)} Knoten` : `Layout ${Math.round(progress * 100)} % – ${fmtNum(state.graph.nodes.length)} Knoten`;
        }
      },
      onRenderReady: () => { if (state.graph === state.graph2d && state.graph.layoutDone) $('graph-layout-progress').hidden = true; },
    });
    state.graph2d = state.graph;
    $('graph-canvas-container').appendChild(el('div', {
      className: 'graph-layout-progress', attrs: { id: 'graph-layout-progress', 'aria-live': 'polite', hidden: true },
    }));
    const graphCfg = state.config?.graph || {};
    const hopsSel = $('graph-hops-select');
    const maxHops = graphCfg.max_hops ?? 3;
    [...hopsSel.options].forEach((o) => { o.hidden = Number(o.value) > maxHops; o.disabled = Number(o.value) > maxHops; });
    hopsSel.value = String(Math.min(graphCfg.default_hops ?? 1, maxHops));
    const maxNodes = $('graph-max-nodes-input');
    maxNodes.max = graphCfg.max_visible_nodes ?? 5000;
    maxNodes.value = graphCfg.max_visible_nodes ?? 5000;

    $('graph-center-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const value = $('graph-center-input').value.trim();
      if (!value) { setHash('graph'); return; }
      if (/^(confluence|jira):/i.test(value)) {
        const [type, ...rest] = value.split(':');
        const id = rest.join(':');
        setHash('graph', { focus: `${type.toLowerCase()}:${type.toLowerCase() === 'jira' ? id.toUpperCase() : id}` });
        return;
      }
      try {
        const res = await api('GET', `/api/search?${new URLSearchParams({ q: value, type: 'all' })}`);
        if (res.results && res.results.length) setHash('graph', { focus: res.results[0].uid });
        else showGraphBanner(`Kein lokaler Treffer für „${value}“.`);
      } catch (err) {
        showGraphBanner(err.message);
      }
    });
    $('graph-overview-btn').addEventListener('click', () => { $('graph-center-input').value = ''; setHash('graph'); });
    const reload = () => { state.graphLoadedKey = null; routeGraph(parseHash().params); };
    hopsSel.addEventListener('change', reload);
    maxNodes.addEventListener('change', reload);
    $('graph-type-filter').addEventListener('change', reload);
    EDGE_KINDS.forEach((k) => $(`edge-filter-${k}`).addEventListener('change', () => {
      $('edge-filters-active-count').textContent = String(selectedEdgeKinds().length);
      reload();
    }));
    const impBtn = $('graph-importance-btn');
    if (impBtn) {
      impBtn.addEventListener('click', () => {
        const next = !state.graphImportance;
        state.graphImportance = next;
        state.graph.setImportanceMode(next);
        impBtn.setAttribute('aria-pressed', next ? 'true' : 'false');
        impBtn.classList.toggle('active', next);
        impBtn.title = next
          ? 'Wichtigkeits-Modus aktiv (zentriert wichtigste Knoten mit proportionaler Größe)'
          : 'Knoten nach Wichtigkeit (eingehende Links) skalieren und zentral anordnen';
      });
    }

    $('graph-flight-btn').addEventListener('click', () => setGraphFlight(!state.graphFlight));
    const timelineBtn = $('graph-timeline-btn');
    if (timelineBtn) {
      timelineBtn.addEventListener('click', () => {
        state.timelineAxis = !state.timelineAxis;
        timelineBtn.setAttribute('aria-pressed', String(state.timelineAxis));
        timelineBtn.classList.toggle('active', state.timelineAxis);
        if (state.flightGraph) state.flightGraph.setTimelineAxis(state.timelineAxis);
      });
    }
    $('graph-flight-jira-time').addEventListener('change', () => {
      if (state.flightGraph) state.flightGraph.setJiraTimeField($('graph-flight-jira-time').value);
    });

    $('graph-zoom-in').addEventListener('click', () => state.graph.zoomIn());
    $('graph-zoom-out').addEventListener('click', () => state.graph.zoomOut());
    $('graph-fit-btn').addEventListener('click', () => state.graph.fitToView());
    $('inspector-close-btn').addEventListener('click', () => {
      state.graph.clearSelection();
      $('inspector-details').hidden = true;
      $('inspector-empty-state').hidden = false;
      $('inspector-title').textContent = 'Inspektor';
    });
  }

  async function loadActiveGraphData() {
    const graph = state.graph;
    if (!state.graphData || graph.loadedData === state.graphData) return;
    const data = state.graphData;
    graph.loadedData = data;
    try { await graph.setData(data, data.center || null); }
    catch (err) { if (graph.loadedData === data) graph.loadedData = null; throw err; }
  }

  async function setGraphFlight(enabled) {
    const generation = ++state.graphViewGeneration;
    if (enabled && !state.flightGraph) {
      try {
        state.flightGraph = new window.KnowledgeGraphFlight({
          canvasEl: $('graph-flight-canvas'), containerEl: $('graph-flight-container'),
          onNodeSelect: (node) => { if (state.graphFlight) inspectNode(node.uid); },
          onNodeActivate: (node) => { if (state.graphFlight) setHash('graph', { focus: node.uid }); },
          onEdgeSelect: (edge) => { if (state.graphFlight) inspectEdge(edge); },
          onStatus: (status) => {
            if (!state.graphFlight) return;
            $('graph-flight-status').textContent = status.message || (status.loading ? '3D-Fluglayout wird berechnet …' : 'Bereit zum Fliegen');
            if (status.unknownCount !== undefined) {
              const unknown = $('graph-flight-unknown');
              unknown.hidden = !status.unknownCount;
              unknown.textContent = `${fmtNum(status.unknownCount)} Einträge ohne Datum – separate Ebene vor der Zeitachse. Bereits indexierte Tickets erhalten ihr Erstellungsdatum beim nächsten Aktualisieren.`;
            }
            if (state.flightGraph?.supported === false) {
              setGraphFlight(false);
              showGraphBanner(status.message || '3D ist hier nicht verfügbar. Die 2D-Ansicht bleibt nutzbar.');
            }
          },
        });
      } catch (err) {
        showGraphBanner(`3D konnte nicht gestartet werden: ${err.message}. Die 2D-Ansicht bleibt nutzbar.`);
        return;
      }
    }
    if (enabled && !state.flightGraph.supported) {
      showGraphBanner('Dieser Browser unterstützt die 3D-Ansicht nicht. Die 2D-Ansicht bleibt nutzbar.');
      return;
    }
    state.graphFlight = enabled;
    state.graph = enabled ? state.flightGraph : state.graph2d;
    $('graph-canvas').hidden = enabled;
    $('graph-flight-container').hidden = !enabled;
    $('graph-legend').hidden = enabled;
    $('graph-flight-hud').hidden = !enabled;
    $('graph-flight-help').hidden = !enabled;
    if ($('graph-timeline-btn')) $('graph-timeline-btn').hidden = !enabled;
    $('graph-flight-time-control').hidden = !enabled;
    $('graph-layout-progress').hidden = true;
    const button = $('graph-flight-btn');
    button.setAttribute('aria-pressed', String(enabled));
    button.classList.toggle('active', enabled);
    const tBtn = $('graph-timeline-btn');
    if (tBtn) {
      tBtn.setAttribute('aria-pressed', String(state.timelineAxis));
      tBtn.classList.toggle('active', state.timelineAxis);
    }
    $('graph-zoom-in').title = enabled ? 'Vorwärts in den Raum' : 'Vergrößern (+)';
    $('graph-zoom-in').setAttribute('aria-label', enabled ? 'Vorwärts fliegen' : 'Vergrößern');
    $('graph-zoom-out').title = enabled ? 'Zurück zum Eingang' : 'Verkleinern (−)';
    $('graph-zoom-out').setAttribute('aria-label', enabled ? 'Rückwärts fliegen' : 'Verkleinern');
    $('graph-fit-btn').textContent = enabled ? 'Startposition' : 'Zentrieren';
    if (state.flightGraph) {
      state.flightGraph.setActive(enabled && state.currentTab === 'graph');
      if (enabled) {
        state.flightGraph.setJiraTimeField($('graph-flight-jira-time').value);
        state.flightGraph.setTimelineAxis(state.timelineAxis);
      }
    }
    if (state.graph.importanceMode !== state.graphImportance) state.graph.setImportanceMode(state.graphImportance);
    fitGraphViewport();
    try {
      await loadActiveGraphData();
      if (generation !== state.graphViewGeneration) return;
      if (!enabled) { state.graph2d._resize(); state.graph2d.requestDraw(); }
      (enabled ? $('graph-flight-canvas') : $('graph-canvas')).focus({ preventScroll: true });
    } catch (err) {
      if (generation !== state.graphViewGeneration) return;
      if (enabled) await setGraphFlight(false);
      showGraphBanner(`Graph konnte nicht dargestellt werden: ${err.message}`);
    }
  }

  function selectedEdgeKinds() {
    return EDGE_KINDS.filter((k) => $(`edge-filter-${k}`).checked);
  }

  function showGraphBanner(message) {
    const banner = $('graph-banner');
    if (!message) { banner.hidden = true; return; }
    $('graph-banner-text').textContent = message;
    banner.hidden = false;
  }

  async function routeGraph(params) {
    if (!state.graph) return;
    const focus = params.get('focus') || '';
    if (focus) $('graph-center-input').value = focus;
    const hops = $('graph-hops-select').value;
    const maxNodes = $('graph-max-nodes-input').value || '5000';
    const types = $('graph-type-filter').value;
    const kinds = selectedEdgeKinds();
    const qs = new URLSearchParams({ hops, max_nodes: maxNodes, types });
    if (focus) qs.set('center', focus);
    if (kinds.length !== EDGE_KINDS.length) qs.set('edge_kinds', kinds.join(','));
    const key = qs.toString();
    if (state.graphLoadedKey === key) return;
    state.graphLoadedKey = key;
    if (state.graphRequest) state.graphRequest.abort();
    const request = new AbortController();
    state.graphRequest = request;
    // Cancel both old layout work and a sliced data preparation still in flight.
    state.graph.cancelPending();
    state.graph.loadedData = null;
    $('graph-loading-overlay').hidden = false;
    showGraphBanner('');
    try {
      const data = await api('GET', `/api/graph?${qs}`, undefined, request.signal);
      // Wait one frame so the now-visible canvas has its real size for layout/fit.
      await new Promise((r) => requestAnimationFrame(r));
      if (request !== state.graphRequest) return;
      state.graphData = data;
      await loadActiveGraphData();
      if (request !== state.graphRequest) return;
      const impBtn = $('graph-importance-btn');
      if (impBtn) {
        impBtn.setAttribute('aria-pressed', state.graph.importanceMode ? 'true' : 'false');
        impBtn.classList.toggle('active', state.graph.importanceMode);
      }
      if (!data.nodes || !data.nodes.length) {
        showGraphBanner(focus ? 'Keine Knoten für diese Filter.' : 'Der Graph ist noch leer – zuerst eine Indexierung starten.');
      } else if (data.truncated) {
        showGraphBanner(`Weitere Knoten ausgeblendet (${fmtNum(data.hidden_nodes || 0)}). „Max. Knoten“ erhöhen oder Hops verringern.`);
      }
      if (!data.center) {
        $('inspector-details').hidden = true;
        $('inspector-empty-state').hidden = false;
      }
    } catch (err) {
      if (request !== state.graphRequest || err.name === 'AbortError') return;
      state.graphLoadedKey = null;
      state.graphData = { nodes: [], edges: [] };
      await loadActiveGraphData();
      showGraphBanner(err.status === 404 ? `Knoten „${focus}“ ist lokal nicht bekannt.` : `Graph konnte nicht geladen werden: ${err.message}`);
    } finally {
      if (request === state.graphRequest) $('graph-loading-overlay').hidden = true;
    }
  }

  function prop(label, value) {
    if (value === null || value === undefined || value === '') return null;
    return el('div', { className: 'inspector-prop-item' }, [
      el('span', { className: 'inspector-prop-label', text: label }),
      el('span', { className: 'inspector-prop-value', text: value }),
    ]);
  }

  function linkList(title, items, pick) {
    const section = el('div', { className: 'inspector-links-section' }, [
      el('div', { className: 'inspector-section-heading', text: `${title} (${items.length})` }),
    ]);
    if (!items.length) {
      section.appendChild(el('div', { className: 'field-hint', text: 'Keine' }));
      return section;
    }
    const list = el('ul', { className: 'inspector-link-list' });
    for (const item of items) {
      const node = pick(item) || {};
      const relation = item.relation_label || EDGE_LABEL[item.kind] || item.kind;
      const li = el('li', {
        className: 'inspector-link-item',
        attrs: { tabindex: 0, role: 'button', title: `${node.uid}${item.anchor_text ? ' – ' + item.anchor_text : ''}` },
      }, [
        el('span', { className: 'link-item-title', text: node.title || node.uid }),
        el('span', { className: 'link-item-relation', text: relation }),
      ]);
      const go = () => setHash('graph', { focus: node.uid });
      li.addEventListener('click', go);
      li.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
      list.appendChild(li);
    }
    section.appendChild(list);
    return section;
  }

  let inspectSeq = 0;

  async function inspectNode(uid) {
    const details = $('inspector-details');
    const seq = ++inspectSeq;
    $('inspector-empty-state').hidden = true;
    details.hidden = false;
    clear(details);
    details.appendChild(el('div', { className: 'field-hint', text: 'Lade Details …' }));
    let doc;
    try {
      doc = (await api('GET', `/api/documents/${encodeURIComponent(uid)}`)).document;
    } catch (err) {
      if (seq !== inspectSeq) return;
      clear(details);
      details.appendChild(el('div', { className: 'banner banner-danger', text: err.message }));
      return;
    }
    if (seq !== inspectSeq) return;
    clear(details);
    const docType = doc.type || doc.source_type || (doc.uid?.startsWith('jira:') ? 'jira' : 'confluence');
    const projectOrSpace = doc.project || doc.space_or_project || doc.space;
    const statusLabel = doc.status || doc.status_label;
    $('inspector-title').textContent = TYPE_LABEL[docType] || 'Dokument';
    details.appendChild(el('span', { className: `result-badge badge-${docType} inspector-type-badge`, text: TYPE_LABEL[docType] }));
    details.appendChild(el('div', { className: 'inspector-doc-title', text: doc.title || doc.uid }));
    const actions = el('div', { className: 'inspector-actions-row' });
    if (doc.url) {
      actions.appendChild(el('a', { className: 'btn btn-primary btn-sm', text: 'Original öffnen ↗', attrs: { href: doc.url, target: '_blank', rel: 'noopener noreferrer' } }));
    }
    actions.appendChild(el('button', {
      className: 'btn btn-secondary btn-sm', text: 'Als Zentrum laden', attrs: { type: 'button' },
      on: { click: () => setHash('graph', { focus: doc.uid }) },
    }));
    details.appendChild(actions);
    const props = el('div', { className: 'inspector-props-list' }, [
      prop('UID', doc.uid),
      prop('Zustand', STATE_LABEL[doc.state] || doc.state),
      prop(docType === 'jira' ? 'Projekt' : 'Space', projectOrSpace),
      prop('Typ', doc.document_type),
      prop('Status', statusLabel),
      prop('Labels', (doc.labels || []).join(', ')),
      prop('Erstellt', doc.source_created_at ? formatDate(doc.source_created_at) : null),
      prop('Aktualisiert', formatDate(doc.source_updated_at || doc.updated_at)),
      prop('Abgerufen', formatDate(doc.fetched_at)),
      prop('Letzter Fehler', doc.last_error),
    ].filter(Boolean));
    details.appendChild(props);
    if (doc.text) {
      details.appendChild(el('div', { className: 'inspector-section-heading', text: 'Kurztext' }));
      details.appendChild(el('div', { className: 'inspector-text-block', text: doc.text + (doc.truncated ? ' …' : '') }));
    } else if (doc.state !== 'indexed') {
      details.appendChild(el('div', { className: 'field-hint', text: 'Nur als Referenz bekannt – Inhalt wurde noch nicht abgerufen.' }));
    }
    details.appendChild(linkList('Ausgehende Links', doc.outgoing || [], (e) => e.target));
    details.appendChild(linkList('Eingehende Links', doc.incoming || [], (e) => e.source));
  }

  function inspectEdge(edge) {
    const details = $('inspector-details');
    ++inspectSeq;
    $('inspector-empty-state').hidden = true;
    details.hidden = false;
    clear(details);
    $('inspector-title').textContent = 'Beziehung';
    const nodeTitle = (uid) => {
      const n = state.graph.nodeMap.get(uid);
      return n && n.title ? `${n.title} (${uid})` : uid;
    };
    details.appendChild(el('div', { className: 'inspector-doc-title', text: EDGE_LABEL[edge.kind] || edge.kind }));
    details.appendChild(el('div', { className: 'inspector-props-list' }, [
      prop('Kantenart', edge.kind),
      prop('Relation', edge.relation_label),
      prop('Von', nodeTitle(edge.source)),
      prop('Nach', nodeTitle(edge.target)),
      prop('Herkunft', FIELD_LABEL[edge.source_field] || edge.source_field),
      prop('Ankertext', edge.anchor_text),
    ].filter(Boolean)));
    if (edge.evidence_url) {
      details.appendChild(el('a', {
        className: 'btn btn-ghost btn-sm', text: 'Beleg-URL öffnen ↗',
        attrs: { href: edge.evidence_url, target: '_blank', rel: 'noopener noreferrer', title: edge.evidence_url },
      }));
    }
    const actions = el('div', { className: 'inspector-actions-row' }, [
      el('button', { className: 'btn btn-secondary btn-sm', text: 'Quelle inspizieren', attrs: { type: 'button' }, on: { click: () => state.graph.selectNode(edge.source) } }),
      el('button', { className: 'btn btn-secondary btn-sm', text: 'Ziel inspizieren', attrs: { type: 'button' }, on: { click: () => state.graph.selectNode(edge.target) } }),
    ]);
    details.appendChild(actions);
  }


  // ------------------------------------------------------------------ tab: top-dokumente

  let topTabInitialized = false;

  function initTopTabOnce() {
    if (topTabInitialized) return;
    topTabInitialized = true;

    $('top-refresh-btn').addEventListener('click', () => loadTopDocuments());

    ['top-type-all', 'top-type-confluence', 'top-type-jira'].forEach((id) => {
      const elRadio = $(id);
      if (elRadio) elRadio.addEventListener('change', () => onTopFiltersChanged());
    });

    $('top-project-select').addEventListener('change', () => onTopFiltersChanged());
    $('top-limit-select').addEventListener('change', () => onTopFiltersChanged());

    let searchDebounce = null;
    $('top-search-input').addEventListener('input', () => {
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => onTopFiltersChanged(), 250);
    });
  }

  function getTopFilters() {
    let types = 'all';
    if ($('top-type-confluence').checked) types = 'confluence';
    else if ($('top-type-jira').checked) types = 'jira';

    const project = $('top-project-select').value.trim();
    const limit = parseInt($('top-limit-select').value, 10) || 50;
    const q = $('top-search-input').value.trim();
    return { types, project, limit, q };
  }

  function onTopFiltersChanged() {
    const f = getTopFilters();
    const p = {};
    if (f.types !== 'all') p.types = f.types;
    if (f.project) p.project = f.project;
    if (f.limit !== 50) p.limit = String(f.limit);
    if (f.q) p.q = f.q;
    setHash('top', Object.keys(p).length ? p : null);
  }

  async function routeTop(params) {
    initTopTabOnce();
    const types = params.get('types') || 'all';
    const project = params.get('project') || '';
    const limit = params.get('limit') || '50';
    const q = params.get('q') || '';

    const r = $(`top-type-${types}`);
    if (r) r.checked = true;

    if (project && ![...$('top-project-select').options].some((o) => o.value === project)) {
      $('top-project-select').appendChild(el('option', { text: project, attrs: { value: project } }));
    }
    $('top-project-select').value = project;
    $('top-limit-select').value = limit;
    $('top-search-input').value = q;

    await loadTopDocuments();
  }

  async function loadTopDocuments() {
    const { types, project, limit, q } = getTopFilters();
    const qs = new URLSearchParams({ types, limit: String(limit), offset: '0' });
    if (project) qs.set('project', project);
    if (q) qs.set('q', q);

    $('top-meta-text').textContent = 'Lade Top-Dokumente …';
    const tbody = $('top-docs-tbody');
    const emptyState = $('top-empty-state');
    const table = $('top-docs-table');

    try {
      const res = await api('GET', `/api/top-documents?${qs}`);
      const docs = res.documents || [];
      const total = res.total || 0;

      while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

      if (!docs.length) {
        table.hidden = true;
        emptyState.hidden = false;
        $('top-meta-text').textContent = 'Keine Dokumente für diese Filterkriterien gefunden.';
        return;
      }

      table.hidden = false;
      emptyState.hidden = true;
      $('top-meta-text').textContent = `${fmtNum(docs.length)} von ${fmtNum(total)} Dokumenten angezeigt (sortiert nach Anzahl eingehender Verlinkungen)`;

      docs.forEach((doc, idx) => {
        const rank = idx + 1;
        const tr = document.createElement('tr');
        tr.className = `top-doc-row top-rank-${rank <= 3 ? rank : 'other'}`;

        // Rang
        const tdRank = el('td', { className: 'td-rank' }, [
          el('span', { className: `rank-badge rank-badge-${rank <= 3 ? rank : 'default'}`, text: `#${rank}` })
        ]);

        // Eingehende Links (Wichtigkeit)
        const inBadge = el('span', {
          className: 'incoming-count-badge',
          attrs: { title: `${doc.incoming_count} Dokument(e) verlinken auf dieses Dokument` },
          text: `${fmtNum(doc.incoming_count)} Links`
        });
        const tdInc = el('td', { className: 'td-incoming' }, [inBadge]);

        // Titel & UID
        const titleLink = el('a', {
          className: 'top-doc-title-link',
          text: doc.title || doc.uid,
          attrs: { href: `#graph?focus=${encodeURIComponent(doc.uid)}`, title: 'Im Graph zentrieren' }
        });
        const uidSub = el('div', { className: 'top-doc-uid-sub', text: doc.uid });
        const tdTitle = el('td', { className: 'td-title' }, [titleLink, uidSub]);

        // Typ
        const typeBadge = el('span', {
          className: `type-pill type-pill-${doc.source_type}`,
          text: doc.source_type === 'jira' ? 'Jira' : 'Confluence'
        });
        const tdType = el('td', { className: 'td-type' }, [typeBadge]);

        // Space / Projekt
        const tdSpace = el('td', { className: 'td-space', text: doc.space_or_project || '—' });

        // Ausgehende Links
        const tdOut = el('td', { className: 'td-outgoing', text: String(doc.outgoing_count || 0) });

        // Status
        const statusBadge = el('span', {
          className: `status-tag status-${doc.state === 'indexed' ? 'indexed' : 'ref'}`,
          text: doc.status_label || (doc.state === 'indexed' ? 'Indexiert' : doc.state)
        });
        const tdStatus = el('td', { className: 'td-status' }, [statusBadge]);

        // Aktionen
        const btnGraph = el('button', {
          className: 'btn btn-secondary btn-xs action-graph-btn',
          text: 'Graph',
          attrs: { type: 'button', title: 'Dieses Dokument im Graph zentrieren' }
        });
        btnGraph.addEventListener('click', (e) => {
          e.stopPropagation();
          setHash('graph', { focus: doc.uid });
        });

        const actions = [btnGraph];
        if (doc.url) {
          const btnExt = el('a', {
            className: 'btn btn-ghost btn-xs action-ext-link',
            text: 'Öffnen ↗',
            attrs: {
              href: doc.url,
              target: '_blank',
              rel: 'noopener noreferrer',
              title: 'Im Browser öffnen'
            }
          });
          actions.push(btnExt);
        }
        const tdActions = el('td', { className: 'td-actions' }, actions);

        tr.appendChild(tdRank);
        tr.appendChild(tdInc);
        tr.appendChild(tdTitle);
        tr.appendChild(tdType);
        tr.appendChild(tdSpace);
        tr.appendChild(tdOut);
        tr.appendChild(tdStatus);
        tr.appendChild(tdActions);

        tbody.appendChild(tr);
      });
    } catch (err) {
      table.hidden = true;
      emptyState.hidden = false;
      $('top-meta-text').textContent = `Fehler beim Laden: ${err.message}`;
    }
  }

  // ------------------------------------------------------------------ settings

  // [elementId, section, key, kind]
  const SETTINGS_FIELDS = [
    ['setting-confluence-base-url', 'confluence', 'base_url', 'text'],
    ['setting-confluence-api-mode', 'confluence', 'api_mode', 'text'],
    ['setting-confluence-aliases', 'confluence', 'aliases', 'lines'],
    ['setting-confluence-sleep-seconds', 'confluence', 'sleep_seconds', 'float'],
    ['setting-jira-base-url', 'jira', 'base_url', 'text'],
    ['setting-jira-api-mode', 'jira', 'api_mode', 'text'],
    ['setting-jira-aliases', 'jira', 'aliases', 'lines'],
    ['setting-jira-project-keys', 'jira', 'project_keys', 'csv'],
    ['setting-jira-sleep-seconds', 'jira', 'sleep_seconds', 'float'],
    ['setting-http-verify-ssl', 'http', 'verify_ssl', 'bool'],
    ['setting-http-ca-bundle', 'http', 'ca_bundle', 'text'],
    ['setting-http-use-system-proxy', 'http', 'use_system_proxy', 'bool'],
    ['setting-http-max-response-mb', 'http', 'max_response_mb', 'int'],
    ['setting-http-user-agent', 'http', 'user_agent', 'text'],
    ['setting-crawl-batch-fetch-size', 'crawl_defaults', 'batch_fetch_size', 'int'],
    ['setting-index-chunk-target-chars', 'index', 'chunk_target_chars', 'int'],
    ['setting-index-chunk-overlap-chars', 'index', 'chunk_overlap_chars', 'int'],
    ['setting-index-min-chunk-chars', 'index', 'min_chunk_chars', 'int'],
    ['setting-index-max-document-chars', 'index', 'max_document_chars', 'int'],
    ['setting-mcp-default-max-chars', 'mcp', 'default_max_chars', 'int'],
    ['setting-mcp-max-output-chars', 'mcp', 'max_output_chars', 'int'],
    ['setting-search-max-results', 'search', 'max_results', 'int'],
    ['setting-search-max-query-chars', 'search', 'max_query_chars', 'int'],
    ['setting-graph-max-visible-nodes', 'graph', 'max_visible_nodes', 'int'],
    ['setting-graph-default-hops', 'graph', 'default_hops', 'int'],
    ['setting-graph-max-hops', 'graph', 'max_hops', 'int'],
    ['setting-server-host', 'server', 'host', 'text'],
    ['setting-server-port', 'server', 'port', 'int'],
    ['setting-db-path', 'database', 'path', 'text'],
  ];
  const SOURCES = ['confluence', 'jira'];
  const pendingTokenClear = { confluence: false, jira: false };

  // Selected Confluence spaces (checkbox list in the settings tab)
  let selectedSpaces = new Set();
  let spacesAutoLoaded = false;

  function isoDaysAgo(days) {
    const d = new Date();
    d.setDate(d.getDate() - Math.max(0, days));
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function setSpacesStatus(text, isError) {
    const node = $('spaces-status');
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('is-error', !!isError);
  }

  function updateSpacesSelectionHint() {
    const hint = $('spaces-selection-hint');
    if (!hint) return;
    const keys = [...selectedSpaces];
    hint.textContent = keys.length
      ? `${keys.length} Space(s) ausgewählt: ${keys.join(', ')}`
      : 'Keine Spaces ausgewählt.';
  }

  function spacesFilterValue() {
    const input = $('spaces-filter');
    return input ? input.value.trim().toLowerCase() : '';
  }

  function spaceRow(space) {
    // autocomplete=off: verhindert, dass der Browser beim Neuladen einen
    // Checkbox-Zustand wiederherstellt, der nicht zur Auswahl im State passt.
    const checkbox = el('input', { attrs: { type: 'checkbox', autocomplete: 'off' } });
    checkbox.checked = selectedSpaces.has(space.key);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selectedSpaces.add(space.key);
      else selectedSpaces.delete(space.key);
      updateSpacesSelectionHint();
    });
    const badges = [];
    if (space.key) badges.push(el('span', { className: 'space-key', text: space.key }));
    if (space.type) badges.push(el('span', { className: 'space-type', text: space.type }));
    return el('label', { className: 'space-item' }, [
      checkbox,
      el('span', { className: 'space-name', text: space.name || space.key || '(ohne Namen)' }),
      el('span', { className: 'space-badges' }, badges),
    ]);
  }

  function renderSpacesList() {
    const box = $('spaces-list');
    if (!box) return;
    clear(box);

    const all = state.spaces || [];
    const filter = spacesFilterValue();
    const rows = all.filter((s) => !filter
      || (s.name || '').toLowerCase().includes(filter)
      || (s.key || '').toLowerCase().includes(filter));
    // Bereits gespeicherte Keys, die die API nicht (mehr) liefert, bleiben
    // abwählbar – z. B. archivierte oder nicht sichtbare Spaces.
    const missing = [...selectedSpaces].filter((key) => !all.some((s) => s.key === key));

    if (!all.length) {
      box.appendChild(el('p', {
        className: 'spaces-empty',
        text: 'Noch keine Spaces geladen. „Spaces laden“ ruft die verfügbaren Spaces aus Confluence ab.',
      }));
    } else if (!rows.length && !missing.length) {
      box.appendChild(el('p', { className: 'spaces-empty', text: 'Kein Space passt zum Filter.' }));
    } else {
      for (const space of rows) box.appendChild(spaceRow(space));
    }
    for (const key of missing) {
      box.appendChild(spaceRow({ key, name: key, type: 'nicht in Liste' }));
    }
    updateSpacesSelectionHint();
  }

  async function loadSpaces(force) {
    const btn = $('spaces-load-btn');
    if (btn) btn.disabled = true;
    setSpacesStatus('Lade Spaces aus Confluence …');
    try {
      const data = await api('GET', `/api/spaces${force ? '?refresh=1' : ''}`);
      state.spaces = Array.isArray(data.spaces) ? data.spaces : [];
      state.spacesFetchedAt = data.fetched_at || null;
      let status = `${state.spaces.length} Space(s) geladen`;
      if (state.spacesFetchedAt) status += ` · Stand: ${formatDate(state.spacesFetchedAt)}`;
      setSpacesStatus(data.warning ? `${status} · ${data.warning}` : status, !!data.warning);
      renderSpacesList();
    } catch (err) {
      setSpacesStatus(`Spaces konnten nicht geladen werden: ${err.message}`, true);
      const box = $('spaces-list');
      if (box && !state.spaces) {
        clear(box);
        box.appendChild(el('p', { className: 'spaces-empty', text: err.message }));
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function startSettingsJob(mode) {
    const fb = $('settings-feedback');
    const { payload, errors } = collectJobForm({ allowMissingStartPage: true });
    const realErrors = errors.filter((m) => !m.startsWith('Bitte eine Confluence'));
    if (realErrors.length) {
      showFeedback(fb, realErrors.join(' · '), 'error');
      return;
    }

    const selected = [...selectedSpaces];
    if (mode === 'spaces') {
      if (!selected.length) {
        showFeedback(fb, 'Bitte mindestens einen Space auswählen.', 'error');
        return;
      }
      payload.space_keys = selected;
      payload.modified_since = '';
    } else {
      const dateInput = $('setting-modified-since');
      const since = dateInput ? dateInput.value.trim() : '';
      if (!since) {
        showFeedback(fb, 'Bitte ein Datum unter „Geänderte Seiten seit“ angeben.', 'error');
        return;
      }
      payload.modified_since = since;
      const onlySelected = $('setting-modified-scope-selected').checked;
      payload.space_keys = onlySelected ? selected : [];
    }
    payload.start_page = '';
    payload.space_key = '';
    // Reine Confluence-Läufe: ohne Jira-Basis-URL sind die Jira-Optionen nicht nutzbar.
    const jiraConfigured = !!(state.config && state.config.configured && state.config.configured.jira);
    if (!jiraConfigured) payload.index_linked_jira_issues = false;

    const btns = [$('spaces-index-btn'), $('modified-index-btn')];
    btns.forEach((b) => { if (b) b.disabled = true; });
    try {
      const res = await api('POST', '/api/jobs', payload);
      showFeedback(fb, `Job #${res.job_id} gestartet – Fortschritt im Tab „Indexierung“.`, 'success');
      setHash('index');
      pollJob();
    } catch (err) {
      showFeedback(fb, err.message, 'error');
    } finally {
      btns.forEach((b) => { if (b) b.disabled = false; });
    }
  }

  function initSettings() {
    $('settings-form').addEventListener('submit', (e) => { e.preventDefault(); saveSettings(); });
    $('settings-save-btn').addEventListener('click', saveSettings);
    $('settings-test-btn').addEventListener('click', testConnection);
    $('settings-test-btn-bottom').addEventListener('click', testConnection);
    $('setting-http-verify-ssl').addEventListener('change', syncSslFields);
    $('spaces-load-btn').addEventListener('click', () => loadSpaces(true));
    $('spaces-filter').addEventListener('input', renderSpacesList);
    $('spaces-filter').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') e.preventDefault();  // Filter-Enter darf nicht speichern
    });
    $('spaces-select-all').addEventListener('click', () => {
      for (const space of state.spaces || []) selectedSpaces.add(space.key);
      renderSpacesList();
    });
    $('spaces-select-none').addEventListener('click', () => {
      selectedSpaces.clear();
      renderSpacesList();
    });
    $('spaces-index-btn').addEventListener('click', () => startSettingsJob('spaces'));
    $('modified-index-btn').addEventListener('click', () => startSettingsJob('modified'));
    for (const src of SOURCES) {
      $(`setting-${src}-auth-type`).addEventListener('change', () => syncAuthFields(src));
      $(`setting-${src}-token-clear`).addEventListener('click', () => {
        pendingTokenClear[src] = true;
        $(`setting-${src}-token`).value = '';
        $(`setting-${src}-token-status`).textContent = 'Wird beim Speichern entfernt';
        $(`setting-${src}-token-clear`).hidden = true;
      });
    }
  }

  function syncSslFields() {
    const verify = $('setting-http-verify-ssl').checked;
    $('setting-http-ca-bundle').disabled = !verify;
  }

  function syncAuthFields(src) {
    const type = $(`setting-${src}-auth-type`).value;
    $(`group-${src}-username`).hidden = type !== 'basic';
    $(`group-${src}-token`).hidden = type === 'none';
    $(`setting-${src}-token`).closest('.form-group').querySelector('label').textContent =
      type === 'basic' ? 'Passwort / API-Token' : 'Personal Access Token';
  }

  async function loadSettings() {
    const fb = $('settings-feedback');
    let s;
    try {
      s = await api('GET', '/api/settings');
    } catch (err) {
      showFeedback(fb, `Einstellungen konnten nicht geladen werden: ${err.message}`, 'error');
      return;
    }
    state.settings = s;
    selectedSpaces = new Set(
      (s.confluence && Array.isArray(s.confluence.selected_spaces))
        ? s.confluence.selected_spaces
        : [],
    );
    for (const [id, section, key, kind] of SETTINGS_FIELDS) {
      const input = $(id);
      const value = s[section] ? s[section][key] : undefined;
      if (!input) continue;
      if (kind === 'bool') input.checked = !!value;
      else if (kind === 'lines') input.value = (value || []).join('\n');
      else if (kind === 'csv') input.value = (value || []).join(', ');
      else input.value = value ?? '';
    }
    for (const src of SOURCES) {
      const auth = s[src]?.auth || {};
      pendingTokenClear[src] = false;
      $(`setting-${src}-auth-type`).value = auth.type || 'none';
      $(`setting-${src}-username`).value = auth.username || '';
      const tokenInput = $(`setting-${src}-token`);
      tokenInput.value = '';
      tokenInput.placeholder = auth.token_set ? 'unverändert (Token gesetzt)' : 'Token eingeben …';
      $(`setting-${src}-token-status`).hidden = !auth.token_set;
      $(`setting-${src}-token-status`).textContent = 'Token gesetzt';
      $(`setting-${src}-token-clear`).hidden = !auth.token_set;
      syncAuthFields(src);
    }
    syncSslFields();
    const sinceInput = $('setting-modified-since');
    if (sinceInput && !sinceInput.value) sinceInput.value = isoDaysAgo(7);
    renderSpacesList();
    if (!spacesAutoLoaded) {
      spacesAutoLoaded = true;
      loadSpaces(false);
    }
  }

  function collectSettings() {
    const patch = {};
    const errors = [];
    for (const [id, section, key, kind] of SETTINGS_FIELDS) {
      const input = $(id);
      if (!input) continue;
      let value;
      if (kind === 'bool') value = input.checked;
      else if (kind === 'lines') value = input.value.split(/[\n,]+/).map((v) => v.trim()).filter(Boolean);
      else if (kind === 'csv') value = input.value.split(/[\s,;]+/).map((v) => v.trim()).filter(Boolean);
      else if (kind === 'int' || kind === 'float') {
        const raw = input.value.trim().replace(',', '.');
        const num = Number(raw);
        if (raw === '' || !Number.isFinite(num) || (kind === 'int' && !Number.isInteger(num))) {
          const label = input.labels && input.labels[0] ? input.labels[0].textContent.trim() : key;
          errors.push(`${label}: ${kind === 'int' ? 'Ganzzahl' : 'Zahl'} erwartet`);
          continue;
        }
        value = num;
      } else value = input.value.trim();
      (patch[section] = patch[section] || {})[key] = value;
    }
    (patch.confluence = patch.confluence || {}).selected_spaces = [...selectedSpaces];
    for (const src of SOURCES) {
      const auth = { type: $(`setting-${src}-auth-type`).value, username: $(`setting-${src}-username`).value.trim() };
      const token = $(`setting-${src}-token`).value;
      if (token) auth.token = token;
      if (pendingTokenClear[src] && !token) auth.clear_token = true;
      patch[src].auth = auth;
    }
    if (patch.graph && patch.graph.default_hops > patch.graph.max_hops) {
      errors.push('Standard-Hops darf nicht größer als Max. Hops sein');
    }
    if (patch.mcp) {
      for (const [key, label] of [['default_max_chars', 'MCP-Standardausgabe'], ['max_output_chars', 'Maximale MCP-Ausgabe']]) {
        if (patch.mcp[key] < 100 || patch.mcp[key] > 2000000) {
          errors.push(`${label}: muss zwischen 100 und 2.000.000 Zeichen liegen`);
        }
      }
      if (patch.mcp.default_max_chars > patch.mcp.max_output_chars) {
        errors.push('MCP-Standardausgabe darf nicht größer als die maximale Ausgabe sein');
      }
    }
    return { patch, errors };
  }

  async function saveSettings() {
    const fb = $('settings-feedback');
    const { patch, errors } = collectSettings();
    if (errors.length) { showFeedback(fb, errors.join(' · '), 'error'); return; }
    const btns = [$('settings-save-btn'), $('settings-save-btn-bottom')];
    btns.forEach((b) => { b.disabled = true; });
    const before = state.settings;
    try {
      await api('PUT', '/api/settings', patch);
      await loadSettings();
      await loadConfig();
      fillJobForm(true);
      state.graphLoadedKey = null;
      const restart = before && (before.server.host !== patch.server.host || before.server.port !== patch.server.port ||
        before.database.path !== patch.database.path);
      showFeedback(fb, restart ? 'Gespeichert. Server-/Datenbankänderungen werden nach einem Neustart wirksam.' : 'Einstellungen gespeichert.', 'success');
    } catch (err) {
      showFeedback(fb, err.message, 'error');
    } finally {
      btns.forEach((b) => { b.disabled = false; });
    }
  }

  async function testConnection() {
    const box = $('settings-test-results');
    const btns = [$('settings-test-btn'), $('settings-test-btn-bottom')];
    btns.forEach((b) => { b.disabled = true; });
    clear(box);
    box.hidden = false;
    box.appendChild(el('div', { className: 'field-hint', text: 'Teste Verbindung mit den gespeicherten Einstellungen …' }));
    try {
      const res = await api('POST', '/api/settings/test');
      clear(box);
      for (const src of SOURCES) {
        const r = res[src] || { ok: false, message: 'Keine Antwort' };
        box.appendChild(el('div', { className: 'test-result-row' }, [
          el('span', { className: 'test-source-name', text: TYPE_LABEL[src] }),
          el('span', { className: r.ok ? 'test-status-ok' : 'test-status-error', text: r.ok ? 'OK' : 'Fehler' }),
          el('span', { className: 'test-message', text: r.message || '' }),
        ]));
      }
      box.appendChild(el('div', { className: 'field-hint', text: 'Hinweis: Getestet werden die gespeicherten Werte – vorher speichern.' }));
    } catch (err) {
      clear(box);
      box.appendChild(el('div', { className: 'feedback-msg feedback-error', text: err.message }));
    } finally {
      btns.forEach((b) => { b.disabled = false; });
    }
  }

  // ------------------------------------------------------------------ boot

  async function boot() {
    initTabs();
    try {
      await loadConfig();
    } catch (err) {
      $('global-banners').appendChild(el('div', { className: 'banner banner-danger', text: `Konfiguration konnte nicht geladen werden: ${err.message}` }));
    }
    initSearch();
    initIndexing();
    initGraph();
    initSettings();
    fillJobForm(false);
    loadProjects();
    loadStats();

    // Legacy deep link: /?focus=jira:ENG-42
    const legacyFocus = new URLSearchParams(window.location.search).get('focus');
    if (legacyFocus && !window.location.hash) {
      history.replaceState(null, '', window.location.pathname + `#graph?${new URLSearchParams({ focus: legacyFocus })}`);
    }
    window.addEventListener('hashchange', onRoute);
    onRoute();
    pollJob();
    window.addEventListener('resize', fitGraphViewport);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
