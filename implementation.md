# implementation.md – Lokaler Confluence- & Jira-Knowledge-Graph

> **Zweck:** Umsetzbare Spezifikation für eine minimalistische lokale Python-Anwendung.
> **Stand:** Oktober 2026; Ziel ist ein MVP ohne Docker, Node.js, externe Datenbank oder verpflichtende pip-Pakete.
> **Technologie:** Python 3.10+, SQLite mit FTS5, HTML, CSS, Vanilla-JavaScript, REST-APIs und ein lokaler MCP-Server.
> **Priorität:** Verlässliche Indexierung, schnelle Google-artige Suche, nachvollziehbare Dokumentverknüpfungen.
> **Nicht Teil des MVP:** Login, eigene Authentifizierung, Benutzerverwaltung, OAuth-/Token-Dialoge oder Rollenmodell.

## 1. Problem und Ziel

Gemini hat bereits Zugriff auf Confluence und Jira über MCP. Das wiederholte Nachladen einzelner Seiten und Tickets ist jedoch langsam; Informationen sind schlecht lokal verknüpft und schwer schnell wiederzufinden.

Diese Anwendung soll:

1. Im Browser eine **Confluence-Startseite als URL oder Page-ID** annehmen.
2. Von ihr aus verlinkte Confluence-Seiten und darin referenzierte **Jira-Tickets** ermitteln.
3. Optional weitere Unterseiten, verlinkte Jira-Tickets und Jira-Issue-Verknüpfungen verfolgen.
4. Inhalte und Beziehungen **lokal in SQLite** speichern und mit FTS5 indexieren.
5. Eine **Google-artige Suchseite** anbieten: Suchfeld, Relevanzsortierung, höchstens zehn Dokumenttreffer.
6. Einen **interaktiven Knowledge Graph** mit Confluence-Seiten und Jira-Tickets anzeigen.
7. Indexierungen über das **HTML-Frontend starten und stoppen** sowie ihren Fortschritt anzeigen.
8. Anzahl Seiten/Tickets, Linktiefe, Requests und **Sleep zwischen REST-Anfragen** einstellbar machen.
9. Dieselben lokalen Daten über effiziente **Read-only-MCP-Tools** für Gemini bereitstellen.
10. Nach der Indexierung bei Suchanfragen **keine Atlassian-API-Aufrufe** benötigen.

### 1.1 Wesentliche Definitionen

- **Dokument:** Vollständig abgerufene Confluence-Seite oder vollständig abgerufenes Jira-Issue.
- **Referenz:** Bekanntes, verlinktes Ziel, dessen Inhalt noch nicht heruntergeladen wurde.
- **Knoten:** Ein Dokument oder eine Referenz im Knowledge Graph.
- **Kante:** Nachweisbare Verknüpfung aus einer Quellseite oder einem Quellticket.
- **Crawl-Job:** Eine vom Frontend gestartete Indexierung mit festen Parametern.
- **Lokale Suche:** SQLite-FTS-Abfrage ohne Live-Abfragen an Jira oder Confluence.

## 2. Rahmenbedingungen und Nicht-Ziele

- Ausführung auf einem gewöhnlichen Firmenrechner: `python app.py serve`.
- Keine Container, Build-Tools, Node, npm, React, Elasticsearch oder separaten Server.
- Python-Standardbibliothek: `sqlite3`, `http.server`, `urllib`, `threading`, `html.parser`, `json`, `re`, `queue`, `logging`, `time`, `pathlib`, `hashlib`, `argparse`.
- Frontend lokal ausgeliefert, **keine CDNs** und keine JavaScript-Bibliotheken erforderlich.
- Eine lokal gespeicherte SQLite-Datei; Suchdaten über FTS5, Graphdaten über relationale Tabellen.
- Nur **ein Crawl-Worker gleichzeitig**, mehrere parallele lokale Leseabfragen erlaubt.
- Keine Volltextindizierung von Attachments, Kommentaren, PDFs oder Office-Dateien im MVP.
- Kein LLM, keine Embeddings, keine erfundenen semantischen Beziehungen im MVP.
- Keine eigene API-Authentifizierungsimplementierung: Atlassian-Endpunkte müssen über die vorhandene Firmenumgebung erreichbar sein; HTTP 401/403 wird als Zugriffsfehler angezeigt.
- Webserver bindet standardmäßig ausschließlich an `127.0.0.1`.
- Ein Knowledge Graph in diesem MVP ist ein **belegbarer Dokument-Linkgraph**, kein Fakten-/Ontologiegraph.

## 3. Architektur

```text
┌────────────────────── Webbrowser ─────────────────────────────┐
│ Suche: Query → max. 10 Treffer mit Snippets + Quelllinks      │
│ Indexierung: URL, Limits, Sleep, Start, Stopp, Fortschritt     │
│ Graph: Knoten, Kanten, Filter, Inspektor, Original öffnen     │
└──────────────────────────────┬────────────────────────────────┘
                               │ HTTP 127.0.0.1:8765
┌──────────────────────────────▼────────────────────────────────┐
│ Python ThreadingHTTPServer                                    │
│ /api/search   /api/graph   /api/documents   /api/jobs         │
│                   │                            │             │
│             Such-/Graphservice              Crawl-Worker     │
│                   │                            │             │
│                   │              ┌─────────────┴────────┐    │
│                   │         Confluence REST         Jira REST │
│                   │              └─────────────┬────────┘    │
│                   └────────────────────────────┤             │
│                                    SQLite + FTS5              │
└──────────────────────────────────────┬───────────────────────┘
                                       │ read-only
                            MCP stdio: python app.py mcp
                                       │
                               Gemini/MCP-Client
```

**Trennung:** Crawling nutzt die Atlassian-APIs. Websuche, Graph und MCP lesen ausschließlich die lokale DB.

## 4. Projektstruktur

```text
local-knowledge/
├── app.py                  # serve | mcp | init-db | status
├── config.json             # Atlassian-Basis-URLs und Standardwerte
├── knowledge/
│   ├── db.py               # SQLite, Migrationen
│   ├── atlassian_http.py   # Requests, Sleep, Retry, Pagination
│   ├── confluence.py       # Cloud REST v2
│   ├── jira.py             # Cloud REST v3 + ADF
│   ├── links.py            # IDs/URLs normalisieren
│   ├── indexer.py          # BFS, Queue, Budgets, Stopp
│   ├── text.py             # HTML/ADF → Text + Chunks
│   ├── search.py           # FTS5 + Ranking
│   ├── graph.py            # Knoten, Kanten, Nachbarn
│   ├── webserver.py        # HTTP-Routen
│   └── mcp_server.py       # stdio JSON-RPC/MCP
├── web/                    # index.html, style.css, app.js
├── data/knowledge.sqlite   # lokale Datenbank, nicht committen
├── tests/                  # unittest + gespeicherte REST-Fixtures
├── .gitignore
└── README.md
```

- Nur ein Hintergrund-Crawl-Worker; mehrere Leser über SQLite-WAL.
- MCP ist ein separater Prozess mit nur lesender SQLite-Verbindung.
- `data/`, Logs und `__pycache__/` nicht einchecken.

## 5. Konfiguration und Frontend-Parameter

### 5.1 Beispiel `config.json`

```json
{
  "confluence": {"base_url": "https://company.atlassian.net", "api_mode": "cloud_v2", "sleep_seconds": 1.0},
  "jira": {"base_url": "https://company.atlassian.net", "api_mode": "cloud_v3", "sleep_seconds": 1.0},
  "crawl_defaults": {
    "start_page": "", "max_confluence_pages": 100, "max_jira_issues": 100,
    "max_total_documents": 200, "max_confluence_depth": 2, "max_jira_link_hops": 1,
    "max_links_per_document": 100, "max_api_requests": 1000, "max_duration_minutes": 60,
    "include_child_pages": true, "follow_confluence_links": true,
    "index_linked_jira_issues": true, "follow_jira_issue_links": true,
    "follow_jira_to_confluence": false, "scan_plain_text_issue_keys": false,
    "refresh_existing": false, "request_timeout_seconds": 20, "max_retries": 4
  },
  "index": {"chunk_target_chars": 1200, "chunk_overlap_chars": 150, "min_chunk_chars": 80},
  "search": {"max_results": 10, "max_query_chars": 200},
  "graph": {"max_visible_nodes": 150, "default_hops": 1, "max_hops": 3},
  "server": {"host": "127.0.0.1", "port": 8765}
}
```

### 5.2 Indexierungsformular im Browser

| Feld | Standard | Verhalten |
|---|---:|---|
| Confluence-Start-URL oder Page-ID | leer | Pflichtfeld; Einstieg |
| Max. Confluence-Seiten | 100 | Harte Grenze erfolgreicher Seitenabrufe |
| Max. Jira-Tickets | 100 | Harte Grenze erfolgreicher Issue-Abrufe |
| Max. Dokumente gesamt | 200 | Summe beider Typen |
| Confluence-Tiefe | 2 | Startseite = 0, BFS über Seitenlinks/Kinder |
| Jira-Link-Hops | 1 | Jira-Issue → verlinktes Jira-Issue |
| Max. Links je Dokument | 100 | Begrenzt neu eingeplante Ziele |
| Max. API-Requests | 1000 | Alle Aufrufe inkl. Retries und Pagination |
| Max. Dauer in Minuten | 60 | Zeitbudget inkl. Wartezeiten |
| Confluence-Sleep | 1,0 s | Mindestabstand zwischen Confluence-Requests |
| Jira-Sleep | 1,0 s | Mindestabstand zwischen Jira-Requests |
| Unterseiten berücksichtigen | an | Child-Pages abrufen |
| Confluence-Links folgen | an | Expliziten Seitenlinks folgen |
| Jira-Tickets indexieren | an | Verlinkte Issues herunterladen |
| Jira-Issue-Links folgen | an | Jira-Links/Parent/Subtasks traversieren |
| Jira → Confluence folgen | aus | Rücklinks optional verfolgen |
| Issue-Keys aus Fließtext | aus | Potenziell ungenaue Erkennung |
| Bestehende Inhalte aktualisieren | aus | Erneutes Abrufen |

Parameter vor Jobstart prüfen: Grenzen positive Ganzzahlen, Tiefen >= 0,
Sleep 0–120 s, Timeout 1–120 s, Retry 0–8; Jobparameter während Lauf unveränderlich.
Stoppen bewahrt bereits gespeicherte Daten; unbesuchte Ziele bleiben Referenzen.

## 6. Confluence-API als Datenquelle

**Zieladapter:** Confluence Cloud REST v2. Für Confluence Data Center wäre ein eigener Adapter nötig;
die beiden API-Schemata werden nicht stillschweigend vermischt.

### 6.1 Seiten abrufen

```http
GET https://company.atlassian.net/wiki/api/v2/pages/{pageId}?body-format=storage
Accept: application/json
```

Benötigt: `id`, `title`, `spaceId`, `parentId`, `version`, `body.storage.value`, `_links.webui`.
Wenn `body.storage.value` fehlt, Fehlzustand anzeigen statt leeren erfolgreichen Index zu behaupten.
Die eigentliche Original-URL aus `_links.webui` und fester Hostbasis rekonstruieren.

### 6.2 Kindseiten finden

```http
GET https://company.atlassian.net/wiki/api/v2/pages/{pageId}/direct-children?limit=50
```

- Nur Ergebnisse mit `type="page"` übernehmen; Whiteboards, Dateien etc. nicht indexieren.
- Pagination über `_links.next` beziehungsweise HTTP-`Link`-Header, jedoch nur innerhalb des Request-Budgets.
- `parent_of`-Kante pro Kind speichern; wenn Tiefe erlaubt, Kindseite in die Queue aufnehmen.
- Keine zusätzlichen Netzwerkrequests für Kinder, wenn `include_child_pages=false`.

### 6.3 Confluence-Links erkennen

- Gewöhnliche Links: `<a href="...">`, relative und absolute URLs.
- Seiten-URLs: `/wiki/spaces/SPACE/pages/12345/...` und `viewpage.action?pageId=12345`.
- Storage-Format: `ac:link`, `ri:page`, `ri:content-id`, `ri:content-title`, `ri:space-key`.
- Eingebettete Jira-Issues: Jira-Makros, Smart-Links, `browse/PROJ-123` und erkannte Issue-URLs.
- HTML-Tags/Attribute mit `html.parser` auswerten; XML-artige Makros mit robustem Parser behandeln.
- Zielseiten mit numerischer ID direkt normalisieren.
- Nur titelbasierte `ri:page`-Links über einen späteren, explizit begrenzten Resolver auflösen;
  wenn nicht eindeutig zuordenbar, `unresolved` als Referenz speichern, **nicht raten**.
- Links ohne bekannte interne Confluence-/Jira-Zielkennung nicht crawlen.

## 7. Jira-API als Datenquelle

**Zieladapter:** Jira Cloud REST v3. Jira-Tickets werden voll indexiert, nicht nur als URL-Platzhalter gespeichert.

### 7.1 Issue abrufen

```http
GET https://company.atlassian.net/rest/api/3/issue/PROJ-123?fields=summary,description,status,issuetype,project,labels,components,created,updated,issuelinks,parent,subtasks
Accept: application/json
```

Mindestens erfassen: `id`, `key`, `fields.summary`, `fields.description`, `fields.status.name`,
`fields.issuetype.name`, `fields.project.key`, `fields.labels`, `fields.updated`,
`fields.issuelinks`, `fields.parent`, `fields.subtasks`.
Die Jira-Beschreibung kann **Atlassian Document Format (ADF)** enthalten, nicht HTML.

### 7.2 Jira-Referenzen aus Confluence

1. Confluence-Seite herunterladen und in Klartext + Linkreferenzen zerlegen.
2. Jede erkannte Jira-URL `/browse/KEY-123` auf `(jira, KEY-123)` normalisieren.
3. Auch strukturierte Jira-Makros und Smart-Link-Ziel-URLs berücksichtigen.
4. Jira-Knoten anlegen und `mentions_jira`-Kante von Confluence zu Jira speichern.
5. Wenn `index_linked_jira_issues=true` und Limits reichen, Ticket in die Jira-Queue aufnehmen.
6. Ticket über Jira-REST lesen, bereinigen, chunkweise indexieren und mit Original-URL verknüpfen.
7. Weitere Issue-Verknüpfungen gemäß `max_jira_link_hops` und Jobbudget abarbeiten.
8. Nur bei `scan_plain_text_issue_keys=true` zusätzlich nackte Keys im Fließtext suchen.

**Wichtig:** Ein Jira-Key wird nur dann automatisch als sichere Referenz akzeptiert, wenn er aus
Jira-URL, strukturiertem Macro/Smart Link oder Jira-API stammt. Reine Fließtexttreffer
sind optional und können falsche Verknüpfungen liefern.

### 7.3 Jira-zu-Jira-Kanten

- `fields.issuelinks`: `inwardIssue`/`outwardIssue` und Relationstyp übernehmen.
- `fields.parent` / `fields.subtasks`: `parent_of`- oder `subtask_of`-Kanten anlegen.
- Label und Status als Dokumentattribute speichern, **nicht** als künstliche Verlinkung.
- Optional Remote-Links: `GET /rest/api/3/issue/{key}/remotelink`.
- Remote-Link-Aufrufe sind API-Requests und müssen denselben Jira-Sleep und dieselben Limits einhalten.
- Confluence-URLs aus Jira-ADF und Remote-Links als `links_to_confluence`-Kanten speichern.
- Rückwärts-Crawling nach Confluence nur bei `follow_jira_to_confluence=true`.
- Beziehungen direkt aus der Quelle samt Typ und Quellbeleg speichern.

### 7.4 ADF nach Text transformieren

- Rekursiv ADF-Knoten durchlaufen: `doc`, `paragraph`, `heading`, `bulletList`, `orderedList`,
  `listItem`, `text`, `hardBreak`, `codeBlock`, `table`, `tableRow`, `tableCell`.
- `text.text` übernehmen, harte Umbrüche und Blöcke in sinnvoller Form absetzen.
- Überschriften und Listen erhalten; Duplikate durch Darstellungsknoten vermeiden.
- Links aus `marks[].attrs.href` und `inlineCard.attrs.url` extrahieren.
- Fehlende oder unbekannte ADF-Knotentypen nicht zum Absturz führen lassen.
- Ein Jira-Dokument enthält in Suchtext: `KEY + Summary + Beschreibung + Status + Typ + Labels`.

## 8. Einheitliche Dokument- und Linknormalisierung

### 8.1 Dokumentidentität

- Confluence: `document_uid = "confluence:" + page_id`.
- Jira: `document_uid = "jira:" + issue_key.upper()`.
- Bei mehreren Instanzen zusätzlich einen festen `instance_id`-Präfix verwenden.
- Unterschiedliche URLs auf dasselbe Objekt müssen denselben UID ergeben.
- Querystrings, URL-Fragmente, Slash-Varianten und URL-Encoding normalisieren.
- Niemals beliebige Ziel-URLs aus Seiteninhalt mittels `urllib` abrufen.
- API-Requests ausschließlich an konfigurierte Atlassian-Basis-Hosts senden.

### 8.2 Kantenarten

| Kante | Beispiel | Bedeutung |
|---|---|---|
| `links_to` | Confluence → Confluence | Expliziter Seitenlink |
| `parent_of` | Confluence → Confluence | Kind-/Elternstruktur |
| `mentions_jira` | Confluence → Jira | Verlinktes Ticket |
| `jira_relates_to` | Jira → Jira | Jira-Issue-Link samt Originalrelation |
| `jira_parent_of` | Jira → Jira | Eltern-Ticket / Subtask |
| `links_to_confluence` | Jira → Confluence | Jira-Text/Remote-Link auf Seite |

- Kanten sind **gerichtet**; ein Graph darf sie visuell als Verbindung darstellen.
- `relation_label`, `source_field`, `evidence_url` und ggf. Ankertext mitspeichern.
- Nicht aufgelöste Ziele sind `discovered`; vollständig geladene Ziele `indexed`.
- Externe, nicht unterstützte URLs bleiben optional als Referenzliste außerhalb des Hauptgraphen.

## 9. SQLite-Datenmodell

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL CHECK(source_type IN ('confluence','jira')),
  source_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '', space_or_project TEXT,
  status_label TEXT, document_type TEXT,
  body_text TEXT NOT NULL DEFAULT '', source_version TEXT,
  source_updated_at TEXT, content_hash TEXT,
  state TEXT NOT NULL DEFAULT 'discovered', fetched_at TEXT, last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_type_state ON documents(source_type,state);
CREATE INDEX IF NOT EXISTS idx_docs_project ON documents(space_or_project);

CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY,
  source_doc INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  target_doc INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, relation_label TEXT NOT NULL DEFAULT '',
  source_field TEXT NOT NULL DEFAULT '', anchor_text TEXT NOT NULL DEFAULT '',
  evidence_url TEXT NOT NULL DEFAULT '',
  UNIQUE(source_doc,target_doc,kind,relation_label)
);
CREATE INDEX IF NOT EXISTS idx_edge_source ON edges(source_doc);
CREATE INDEX IF NOT EXISTS idx_edge_target ON edges(target_doc);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, title TEXT NOT NULL,
  heading TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
  UNIQUE(document_id,ordinal)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  title, heading, body, content='chunks', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid,title,heading,body)
  VALUES(new.id,new.title,new.heading,new.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,title,heading,body)
  VALUES('delete',old.id,old.title,old.heading,old.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,title,heading,body)
  VALUES('delete',old.id,old.title,old.heading,old.body);
  INSERT INTO chunks_fts(rowid,title,heading,body)
  VALUES(new.id,new.title,new.heading,new.body);
END;

CREATE TABLE IF NOT EXISTS crawl_jobs (
  id INTEGER PRIMARY KEY, config_json TEXT NOT NULL, state TEXT NOT NULL,
  start_page_uid TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  pages_indexed INTEGER NOT NULL DEFAULT 0, jira_indexed INTEGER NOT NULL DEFAULT 0,
  api_requests INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0, current_uid TEXT, last_message TEXT
);
CREATE TABLE IF NOT EXISTS crawl_queue (
  job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  document_uid TEXT NOT NULL, depth INTEGER NOT NULL,
  jira_hops INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
  PRIMARY KEY(job_id,document_uid)
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
```

- Inhalt, Chunks und ausgehende Kanten eines Dokuments pro Refresh in **einer Transaktion** ändern.
- Bestehende eingehende Kanten anderer Dokumente beim Refresh behalten.
- Zustände: `discovered`, `indexed`, `skipped`, `missing`, `forbidden`, `error`.
- Nur `indexed` ist suchbar; für ein Dokument mit leerem Body ggf. Titel-Chunk erzeugen.
- Bei vorübergehendem Refresh-Fehler letzten erfolgreich gespeicherten Inhalt bewahren.
- Inhalts-Hash erlaubt Erkennung unveränderter Dokumente und Vermeidung unnötiger Chunk-Updates.
- Jobs und Queue für Status/Diagnose persistieren; beim Neustart laufende Jobs `interrupted` setzen.

## 10. Indexierungsalgorithmus

### 10.1 Ablauf

- Confluence-Seiten mittels **BFS** traversieren; separate Jira-Queue mit `jira_hops` führen.
- Startseite zuerst mit `depth=0` einplanen, danach nahe Seiten und ihre Jira-Referenzen.
- Bekannte Linkziele sofort als Referenzknoten und Kante speichern, auch wenn Budget ausgeschöpft.
- Innerhalb eines Jobs `(job_id, document_uid)` deduplizieren; Zielreihenfolge deterministisch.
- Bereits indexierte Daten bei `refresh_existing=false` wiederverwenden **und ihre Kanten lesen**.
- Bei Refresh aktualisierte Links und Suchtexte atomar speichern.

### 10.2 Pseudocode

```python
queue = deque([(seed_uid, 0, 0)])
seen = set()
while queue and not stop_event.is_set():
    uid, depth, hops = queue.popleft()
    if uid in seen: continue
    seen.add(uid)
    if reached_job_budget() or reached_type_budget(uid):
        mark_as_not_fetched(uid)
        continue
    if already_indexed(uid) and not refresh_existing:
        targets = get_stored_edges(uid)
    else:
        payload = fetch_via_rate_limited_atlassian_api(uid)
        text, metadata, targets = parse_payload(payload)
        save_document_chunks_and_edges_atomically(uid, text, metadata, targets)
        increment_successful_document_counter(uid)
    for target in targets[:max_links_per_document]:
        ensure_reference_node_and_edge(uid, target)
        if may_follow(target, depth, hops) and not stop_event.is_set():
            enqueue_once(target, next_depth(target), next_hops(target))
    update_job_progress()
finalize_job('stopped' if stop_event.is_set() else 'completed')
```

### 10.3 Eindeutige Budgetregeln

1. `max_confluence_pages`, `max_jira_issues` und `max_total_documents` zählen nur erfolgreiche **neue API-Abrufe** im Job; Referenzen nicht.
2. `max_api_requests` zählt **jeden HTTP-Versuch**, einschließlich Retries, Kinderlisten, Remote-Links.
3. `max_duration_minutes` enthält alle Sleep-/Retryzeiten und beendet neue Abrufe.
4. `max_confluence_depth` gilt für Confluence→Confluence, `max_jira_link_hops` nur für Jira→Jira.
5. Jira aus einer Confluence-Seite startet bei Jira-Hop 0; Rückwärts-Crawling nur optional.
6. Limitchecks **vor jedem Request**; nach Stop Transaktion abschließen und keine Requests mehr starten.

## 11. Sleep, Rate Limits, Retry und Stopp

### 11.1 Konfigurierbares Sleep zwischen API-Anfragen

- Zwei getrennte Intervalle: `confluence.sleep_seconds` und `jira.sleep_seconds`.
- Die UI überschreibt diese Defaults **für den gestarteten Job**.
- Ein zentraler HTTP-Client merkt sich `last_request_at` **pro Quelle**.
- Vor jedem Request mindestens `sleep_seconds` seit dem **Beginn des letzten Requests** warten.
- Für konservatives Verhalten optional `last_request_finished_at` als Referenz verwenden;
  im MVP ist die Variante **Abstand zwischen Request-Starts** eindeutig festgelegt.
- `sleep_seconds=0` deaktiviert nur den festen Abstand, **nicht** 429-Backoff.
- Keine parallelen Atlassian-Requests im MVP: ein Worker, ein Request zur Zeit.
- Sleep **abbrechbar** implementieren: `stop_event.wait(seconds)` statt `time.sleep(seconds)`.
- Fortschrittsanzeige während der Wartezeit: `rate_limit_wait` mit Restzeit/Quelle.

### 11.2 429 und temporäre Fehler

- Bei HTTP 429 den Header `Retry-After` (Sekunden oder HTTP-Datum) auswerten.
- `Retry-After` als **Mindestwartezeit** behandeln; bei Bedarf zusätzlich Exponential-Backoff mit Jitter.
- Bei 502/503/504 bis zu `max_retries` erneute Versuche mit Backoff unter Beachtung von `Retry-After`.
- Default-Backoff: 2, 4, 8, 16 Sekunden; Maximalwartezeit 300 Sekunden je Versuch,
  außer ein größerer expliziter `Retry-After` muss respektiert werden.
- Bei ungewöhnlich langem `Retry-After` einen wartenden Job anzeigen; Stoppen muss sofort möglich sein.
- 400/401/403/404 nicht blind wiederholen; passende Fehlermeldungen erfassen.
- Zeitüberschreitungen und Netzfehler zählen als fehlgeschlagene Request-Versuche.
- **Alle** Versuche zählen gegen `max_api_requests`; kein verborgenes Retry-Schlupfloch.
- Nie wegen eines einzelnen fehlerhaften Dokuments den gesamten Crawl ohne Grund abbrechen.

### 11.3 Start-/Stopp-Verhalten

- `POST /api/jobs` startet den einzigen Hintergrund-Worker, gibt sofort `job_id` zurück.
- Während `running` oder `stopping` ist der Start-Button deaktiviert.
- `POST /api/jobs/{id}/stop` setzt das thread-sichere `stop_event`.
- Ein laufender HTTP-Request wird bis Timeout beendet; danach keine weiteren Requests.
- Abbrechbare Sleeps und Retry-Wartezeiten reagieren unmittelbar auf das Stop-Event.
- Aktuelle SQLite-Transaktion abschließen; keine halb geschriebenen Dokumente.
- Nach sauberem Ende Jobzustand `stopped` und `finished_at` speichern.
- Bei Prozessabsturz frühere `running`-Jobs beim Neustart als `interrupted` markieren.
- Ein erneuter Start ist ein neuer Job und kann den vorhandenen Index weiterverwenden.
- Jeder Stop ist idempotent; mehrfaches Stoppen liefert den aktuellen Jobzustand.

### 11.4 Beispiel eines stoppbaren Waits

```python
def wait_if_needed(seconds: float, stop_event) -> None:
    if seconds > 0 and stop_event.wait(seconds):
        raise CrawlStopped()
```

## 12. Google-artige Suchoberfläche

### 12.1 Nutzererlebnis

- Großes zentriertes Suchfeld, Suche per **Enter** oder Button „Suchen“.
- Darunter eine **vertikale Google-artige Ergebnisliste mit maximal 10 Dokumenten**.
- Jeder Treffer zeigt: klickbaren Titel, `Confluence`/`Jira`, Original-URL,
  markiertes Textsnippet, ggf. Space/Projekt und Aktualisierungsdatum.
- Klick auf Titel öffnet Confluence/Jira in neuem Tab; „Im Graph anzeigen“ fokussiert den Knoten.
- Filter `Alle | Confluence | Jira`, optional Space/Projekt.
- Anzeige der lokalen Suchdauer und Anzahl ausgegebener Treffer; keine Pagination im MVP.
- Leere Suche: neutraler Startzustand; ohne Treffer: „Noch keine lokalen Treffer“.
- Niemals bei einer Suchanfrage live die Atlassian-API verwenden.

### 12.2 Ranking

- SQLite-FTS5 `unicode61`; BM25-Gewichte `(Titel, Überschrift, Body) = (6, 3, 1)`.
- Exakte Jira-Keys in Titel/Issue-ID stärker gewichten.
- Suchanfrage auf 200 Zeichen begrenzen, Tokens escapen und als sichere FTS-Query aufbauen.
- OR-Verknüpfung für Recall, Token-Abdeckung zusätzlich positiv berücksichtigen.
- Beste Chunks finden, dann auf **unterschiedliche Dokumente** aggregieren.
- Pro Dokument nur das beste Snippet anzeigen, deterministischer Tie-Breaker: UID.
- Kandidatenlimit z. B. 200 Chunks, sichtbares Limit verbindlich **10 Dokumente**.
- Nicht indexierte Referenzknoten ausschließen.
- Keine ungeprüften Markdown-/HTML-Inhalte per `innerHTML` rendern; sicher escapen.

### 12.3 SQL-Beispiel

```sql
SELECT c.document_id, c.id AS chunk_id,
       bm25(chunks_fts, 6.0, 3.0, 1.0) AS relevance,
       snippet(chunks_fts, 2, '[', ']', ' … ', 24) AS excerpt
FROM chunks_fts
JOIN chunks c ON c.id = chunks_fts.rowid
JOIN documents d ON d.id = c.document_id
WHERE chunks_fts MATCH ? AND d.state = 'indexed'
ORDER BY relevance ASC, c.document_id ASC
LIMIT 200;
```

- Parameter `?` durch gebundene Werte füllen, nicht per SQL-Stringkonkatenation.
- Ausgabe von `/api/search` als `{query, total_returned, duration_ms, results:[...]}`.
- Jeder Ergebnis-Eintrag: `uid`, `type`, `title`, `url`, `project`, `snippet`, `updated_at`.
- Keine erfundenen Scores/Prozentwerte in der Anzeige; Ranking reicht.

## 13. Knowledge-Graph-Frontend

- Navigation: `Suche`, `Indexierung`, `Graph` in einer einseitigen HTML-Anwendung.
- Graph mit Vanilla-JavaScript und SVG; keine externen Pakete.
- Confluence-Knoten z. B. als blaue Kreise, Jira-Knoten als orange Rechtecke.
- Nicht indexierte Referenzen gestrichelt/grau darstellen.
- Kanten je Relation mit unterschiedlichen Linien/Stilen; Legende anzeigen.
- Zoom, Pan, Knoten anklicken, Nachbarn hervorheben, Ausschnitt zentrieren.
- Rechts ein **Inspektor** mit Titel, Typ, Quelle, Status, Kurztext,
  eingehenden/ausgehenden Links und Schaltfläche „Original öffnen“.
- Filter für Typ (`Confluence`, `Jira`, `Alle`) und Kantenart.
- Suche kann per `?focus=confluence:12345` oder `?focus=jira:ENG-42` direkt in Graph springen.
- Graph initial begrenzen: `max_visible_nodes=150`, einstellbare Hops bis maximal 3.
- Bei zu vielen Nachbarn nur erste N rendern und Hinweis „Weitere Knoten ausgeblendet“ zeigen.
- Layout reproduzierbar initialisieren; SVG-Performance vor allem bei dichten Graphen beachten.
- Nur den sichtbaren Subgraph abrufen, **nicht** ganze SQLite-DB in den Browser laden.
- Klick auf Kante zeigt Beziehungstyp und Herkunft (z. B. Jira-Issue-Link oder Confluence-Anker).

## 14. Lokale HTTP-API

| Methode | Route | Zweck |
|---|---|---|
| `GET` | `/` | Frontend ausliefern |
| `GET` | `/api/config` | Defaults und zulässige Werte |
| `POST` | `/api/jobs` | Crawl-Job mit Formularwerten starten |
| `GET` | `/api/jobs/current` | Laufenden/letzten Job abfragen |
| `GET` | `/api/jobs/{id}` | Jobstatus |
| `POST` | `/api/jobs/{id}/stop` | Stop-Event setzen |
| `GET` | `/api/search?q=...&type=all` | Maximal zehn priorisierte Dokumente |
| `GET` | `/api/documents/{uid}` | Dokument + Nachbarn |
| `GET` | `/api/graph?center=...&hops=1` | Begrenzter Subgraph |
| `GET` | `/api/stats` | Dokument-, Kanten- und Chunkzahlen |

### Beispiel Jobstart

```json
{
  "start_page": "https://company.atlassian.net/wiki/spaces/ENG/pages/12345",
  "max_confluence_pages": 60, "max_jira_issues": 40,
  "max_total_documents": 100, "max_confluence_depth": 2,
  "max_jira_link_hops": 1, "max_api_requests": 400,
  "confluence_sleep_seconds": 1.5, "jira_sleep_seconds": 2.0,
  "index_linked_jira_issues": true, "include_child_pages": true
}
```

- Antwort von `POST /api/jobs`: HTTP 202 mit `{ "job_id": 17, "state": "running" }`.
- Ein bereits laufender Job führt zu HTTP 409; unbekannte/unzulässige Felder zu HTTP 400.
- Status: `state`, `pages_indexed`, `jira_indexed`, `api_requests`, `skipped`, `errors`,
  `current_uid`, `last_message`, `elapsed_seconds`, `queue_size` und ggf. `retry_wait_seconds`.
- Frontend pollt alle 1–2 s; Start-Button bei laufendem Job deaktivieren.
- Stopp zeigt „Wird gestoppt …“ bis `stopped`, `completed` oder `failed` eintritt.
- UI darf bei unbekannter Gesamtgröße nur **Budgetauslastung**, keinen fiktiven Fertigprozentsatz, zeigen.
- Alle Fehler standardisiert als JSON mit geeignetem HTTP-Status; statische Dateien nur unter `web/`.

## 15. MCP-Server für Gemini

### 15.1 Transport

- Prozessstart: `python app.py mcp` (ohne laufenden Webserver möglich).
- MCP über **stdio**: eine JSON-RPC-Nachricht pro Zeile, UTF-8.
- `stdout` ausschließlich MCP-Nachrichten; Logging immer auf `stderr`.
- Handshake implementieren: `initialize`, `notifications/initialized`, `ping`,
  `tools/list`, `tools/call`, außerdem saubere JSON-RPC-Fehler bei ungültigen Anfragen.
- Mit dem tatsächlich verwendeten MCP-Client kompatible Protokollversion aushandeln;
  keine beliebige JSON-Zeilen-API fälschlich als MCP bezeichnen.
- Lokale SQLite-Verbindung per URI `mode=ro` öffnen; MCP darf nicht indexieren.
- Der MCP-Client muss die lokale Python-Command-Konfiguration unterstützen;
  die bestehende Gemini-Confluence-MCP-Verbindung garantiert das nicht automatisch.

### 15.2 MCP-Tools (read-only)

| Tool | Parameter | Ergebnis |
|---|---|---|
| `search_knowledge` | `query`, `type?`, `limit?` | Bis zu 10 gerankte Dokumente mit Snippet und URL |
| `get_document` | `uid`, `max_chars?` | Titel, Inhalt, Metadaten, Quelllink |
| `get_document_neighbors` | `uid`, `direction?`, `limit?` | Ein-/ausgehende Links und Relationstypen |
| `get_graph_subgraph` | `uid`, `hops?`, `max_nodes?` | Kleine Knoten-/Kantenmenge |
| `find_related_documents` | `uid`, `limit?` | Nachbarschaft + gemeinsame direkte Nachbarn |
| `get_index_stats` | keine | Status, Umfang, letzter Crawl-Job |

- Parameter strikt validieren und Maximalwerte serverseitig erzwingen.
- `search_knowledge` nutzt **dieselbe Funktion** wie `/api/search`; kein zweites Ranking.
- MCP-Suchergebnisse enthalten `uid`, `title`, `type`, `url`, `snippet` und ggf. `updated_at`.
- `get_document` schneidet den Text am Limit ab und markiert `truncated=true`.
- Bei unbekanntem UID verständliche `isError=true`-Toolantwort liefern.
- Jede Toolspezifikation erhält JSON Schema (`inputSchema`) und eine klare Beschreibung.
- Keine Live-REST-Abfragen bei MCP-Toolaufrufen; damit bleibt die Antwort schnell.

## 16. Frontend-Design

- Navigation oben: **Suche | Indexierung | Graph**.
- Suche: zentriertes breites Eingabefeld, Enter/„Suchen“, Filter, Top-10-Trefferliste,
  klickbarer Originaltitel und pro Treffer „Im Graph anzeigen“.
- Indexierung: Startseiten-URL, Seiten-/Issue-/Gesamt-/Request-Limits, Tiefen und
  separate Sleep-Felder; Checkboxen für Kinder-, Link- und Jira-Traversierung.
- Deutliche Buttons „Indexierung starten“ und „Indexierung stoppen“.
- Statusbox: läuft/stoppt/beendet, aktuelle UID, Seiten/Tickets/Requests, Fehler,
  Wartephase bei Rate-Limit, zuletzt bearbeitetes Dokument.
- Graph: SVG mit unterschiedlichen Confluence-/Jira-Knoten, Relationen und Inspektor.
- Suche und Graph müssen während eines laufenden Crawls weiterhin funktionieren.
- Keine externen Webressourcen laden; barrierearme Tastaturbedienung und Desktop-Layout.

## 17. Fehlerfälle und Datenqualität

- **Confluence-Link auf Jira:** Jira-ID normalisieren, Referenzknoten anlegen, Issue einplanen.
- **Jira-Issue-Link auf ein weiteres Issue:** Relationen speichern, Hop-Limit prüfen.
- **Doppelte URLs/Anker:** auf identische UID normalisieren, nicht erneut abrufen.
- **Nicht vorhandene Seite / Issue:** Status `missing`, Quelle als Kante behalten.
- **HTTP 401/403:** Status `forbidden`, Meldung im Joblog; keine Loginoberfläche.
- **API-Antwort ohne erwartetes Feld:** Fehler dokumentieren, keinen falschen Inhalt speichern.
- **Abgebrochener Lauf:** bisherige Dokumente und Graph bleiben vollständig nutzbar.
- **Gegenseitige Verweise:** `seen` und Queue-Unique-Key verhindern Endlosschleifen.
- **Uneindeutiger Seitentitel:** als ungelöste Referenz anzeigen, keine falsche Verknüpfung.
- **HTTP 429:** Retry-After beachten, im UI sichtbar warten, über Stopp abbrechbar.
- **Sehr große HTML-/ADF-Inhalte:** feste Antwortgrößen- und Textgrenzen einführen,
  UTF-8 korrekt behandeln, zu große Dokumente überspringen und protokollieren.
- **FTS5 nicht verfügbar:** beim Programmstart verständliche Fehlermeldung ausgeben.
- **SQL-Fehler:** Rollback pro Dokument, Indexkonsistenz bewahren, Jobfehler mitzählen.
- **Unerwartete Quell-URLs:** niemals auf nicht konfigurierte API-Hosts weiterleiten.
- **Browser-Sicherheit:** Textfelder mit `textContent` rendern; keine HTML-Inhalte injizieren.

## 18. Tests und Abnahmekriterien

**Automatisierte Tests (`unittest`, Fake-HTTP-Client und REST-Fixtures):**

1. Confluence-URLs, `ri:page`, normale Links und Jira-Smart-Links werden korrekt erkannt.
2. Jira-ADF, `issuelinks`, Parent/Subtasks und Remote-Links werden korrekt verarbeitet.
3. BFS/Deduplikation und alle Dokument-, Tiefen- und HTTP-Request-Limits greifen.
4. Fake-429 respektiert Retry-After; Stop während Sleep verhindert weiteren Request.
5. Eine abgebrochene Transaktion erzeugt keine halben Dokumente oder FTS-Chunks.
6. FTS findet Titel, Body und Jira-Key, liefert **maximal zehn verschiedene** Dokumente.
7. HTTP und MCP liefern das gleiche Suchranking und dieselben Originallinks.
8. Graph umfasst indexierte und bloß referenzierte Confluence-/Jira-Ziele.
9. MCP-Handshake, `tools/list`, `tools/call` und Fehlerfälle sind kompatibel.
10. Kein API-Aufruf an unbekannte Hosts; Fehlercodes ohne Loginoberfläche behandeln.

**Manuelle Abnahme:** Browser öffnen, Confluence-Seite mit Jira-Link indexieren,
Sleep/Counts sehen, mitten im Crawl stoppen, danach weiter lokal suchen und
Confluence→Jira→Jira-Links im Graph inspizieren; Gemini über lokalen MCP-Client testen.

## 19. Umsetzung in sinnvoller Reihenfolge

1. SQLite-Tabellen, FTS5, Migrationen und Connection-Handling implementieren.
2. IDs/URLs, Confluence-Storage und Jira-ADF samt Links parsbar machen.
3. Rate-limitierte Confluence-/Jira-REST-Clients mit Pagination bauen.
4. BFS/Issue-Queue, Budgets, atomare Speicherung und Stop-Event einführen.
5. HTTP-Routen für Indexierung/Status/Stopp und Frontendformular erstellen.
6. FTS5-Suche mit Top-10-Dokumentranking und Graph-Inspektor ergänzen.
7. Read-only-MCP-Server mit sechs Tools und identischem Suchservice anschließen.
8. Fake-API-Tests, Fehlermeldungen und README fertigstellen.

## 20. Startanleitung und Definition of Done

```bash
python app.py init-db
python app.py serve
# Browser: http://127.0.0.1:8765
# Separater MCP-Prozess (vom MCP-Client gestartet):
python app.py mcp
```

**Fertig, wenn:** Start über Confluence-URL, gemischte Seiten-/Ticketindexierung,
Sleep- und Mengengrenzen im HTML-Frontend, Start/Stopp und Fortschritt inkl. 429-Wait,
lokale Google-artige **Top-10-Suche** mit Original-URLs und Snippets,
Confluence-/Jira-Graph mit Relationstypen und lokale read-only-MCP-Tools funktionieren.
Ohne Docker, npm, Pflicht-Python-Pakete oder eigene Auth-Oberfläche.

## 21. Offizielle technische Referenzen

- [Confluence Cloud REST API v2 – Pages](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/)
- [Confluence Cloud REST API v2 – Children](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-children/)
- [Jira Cloud REST API v3 – Issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
- [Jira Cloud REST API v3 – Issue remote links](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/)
- [Jira Cloud REST API v3 – Issue links](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-links/)
- [Jira Cloud – Rate Limiting](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/)
- [Confluence Cloud – Rate Limiting](https://developer.atlassian.com/cloud/confluence/rate-limiting/)
- [Model Context Protocol – stdio-Transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [Model Context Protocol – Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)
