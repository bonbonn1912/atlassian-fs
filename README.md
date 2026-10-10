# Confluence & Jira Knowledge Graph (local-knowledge)

Lokaler semantischer Wissensgraph, Volltextsuche (FTS5) und Model Context Protocol (MCP) Server für Jira Server/Data Center und Confluence Server/Data Center. Vollständig in Python ohne externe PyPI-Abhängigkeiten (nur Standardbibliothek).

---

## 🎯 Zweck des Projekts

Dieses Werkzeug indiziert verknüpfte Inhalte aus Atlassian Confluence und Jira (Server & Data Center oder Cloud) in eine lokale SQLite-Datenbank. Es baut einen durchsuchbaren Wissensgraphen auf, analysiert Querverweise zwischen Tickets und Dokumentationsseiten und stellt die Wissensbasis über:
- Ein responsives Web-Frontend (mit Volltextsuche, Graph-Visualisierung und Crawl-Steuerung),
- Eine REST-API, und
- Einen **MCP-Server** (Model Context Protocol) für KI-Assistenten (wie Gemini CLI, Claude Desktop, Cursor etc.)

lokal zur Verfügung.

---

## 📋 Voraussetzungen

- **Python 3.10 oder neuer** (getestet mit Python 3.10–3.14).
- **SQLite3 mit FTS5-Unterstützung** (bei Python auf macOS und Linux standardmäßig enthalten).
- **Keine externen Abhängigkeiten!** Sämtliche Funktionalität basiert ausschließlich auf der Python-Standardbibliothek (`sqlite3`, `http.server`, `urllib`, `dataclasses`, `html.parser` etc.).

---

## 🚀 Schnellstart

### 1. Datenbank initialisieren

```bash
python3 app.py init-db
```
Erstellt die SQLite-Datenbank (Standardpfad `data/knowledge.sqlite`) inklusive FTS5-Tabellen, Triggern und Schema-Versionierung.

### 2. Webserver & API starten

```bash
python3 app.py serve
```
Optional können Host und Port übergeben werden:
```bash
python3 app.py serve --host 127.0.0.1 --port 8765
```

### 3. Web-Frontend im Browser öffnen

Rufen Sie im Browser auf:
[http://127.0.0.1:8765](http://127.0.0.1:8765)

### 4. Einstellungen konfigurieren

Öffnen Sie den Reiter **Einstellungen** im Frontend oder kopieren Sie `config.example.json` nach `config.json`:
- **Confluence-Basis-URL:** z. B. `https://confluence.example.intern`
- **Jira-Basis-URL:** z. B. `https://jira.example.intern`
- **Authentifizierung:** Bearer Personal Access Token (PAT) empfohlen. Kann für interne Instanzen ohne Authentifizierung auch leer gelassen werden.
- **TLS / SSL:** Die TLS-Zertifikatsprüfung ist standardmäßig **deaktiviert** (`verify_ssl: false`), da interne Firmeninstanzen häufig selbstsignierte oder interne Firmen-CAs nutzen. Wenn ein CA-Bundle vorhanden ist, kann der Pfad unter `ca_bundle` eingetragen und die Prüfung aktiviert werden.

#### Umgebungsvariablen (Alternative zu config.json)
Alternativ zu Klartext-Tokens in `config.json` können Umgebungsvariablen verwendet werden:
- `CONFLUENCE_TOKEN`: Personal Access Token für Confluence
- `CONFLUENCE_USERNAME`: Benutzername (bei Basic-Auth)
- `CONFLUENCE_AUTH_TYPE`: `bearer`, `basic` oder `none`
- `JIRA_TOKEN`: Personal Access Token für Jira
- `JIRA_USERNAME`: Benutzername (bei Basic-Auth)
- `JIRA_AUTH_TYPE`: `bearer`, `basic` oder `none`

### 5. Verbindung testen

Klicken Sie im Reiter **Einstellungen** auf **Verbindung testen** (bzw. `POST /api/settings/test`). Confluence und Jira werden über schlanke API-Abfragen geprüft.

### 6. Erste Indexierung starten

Starten Sie über den Reiter **Indexierung** einen Crawl:
- **Startseite:** z. B. URL einer Confluence-Startseite, ein Confluence-Space-Key oder ein Jira-Projektkürzel (z. B. `PROJ`).
- **Tiefen- und Mengenbeschränkungen:** Konfigurieren Sie Seiten-Limits, Traversierungstiefe und Request-Budgets.
- Der Crawler analysiert HTML-Inhalte, extrahiert Hyperlinks und Jira-Issue-Keys (`PROJ-123`), chunked Inhalte und aktualisiert den FTS5-Index sowie den Wissensgraphen.

### 7. Wissensgraph & Suche nutzen

- **Suche:** Suchanfragen mit FTS5-Ranking (BM25), Filterung nach Typ (`confluence`, `jira`) oder Projekt.
- **Graph:** Interaktiver Graph mit Nachbarschaftsanzeige (Hops 1–3), Kantentypen (`links_to`, `mentions_jira`, `jira_parent_of`, `parent_of` etc.).

---

## 🧪 Demo-Daten ohne Jira/Confluence

Zum lokalen Ausprobieren ohne Zugriff auf Atlassian:

```bash
python3 app.py seed-demo            # ~100 Confluence-Seiten + ~70 Jira-Tickets anlegen
python3 app.py seed-demo --reset    # vorher ALLE vorhandenen Daten löschen
```

Die Demo enthält vier Spaces (ENG, OPS, PROD, SEC), drei Jira-Projekte (ENG, OPS, SEC) mit
Epics, Stories, Subtasks, Issue-Links sowie Rücklinks auf Confluence. Außerdem gibt es
Beispiele für nicht indexierte, nicht auffindbare und gesperrte Referenzen. Die Links zeigen
auf `https://confluence.demo.intern` bzw. `https://jira.demo.intern`, sofern keine echten
Basis-URLs konfiguriert sind.

## 🤖 MCP-Einrichtung (Model Context Protocol)

Der integrierte MCP-Server kommuniziert über `stdio` und erlaubt es KI-Assistenten wie Gemini CLI oder Claude Desktop, direkt auf die lokale Wissensbasis zuzugreifen (Tools: `search_knowledge`, `get_document`, `get_neighbors`, `knowledge_stats`).

### Beispiel für `~/.gemini/settings.json` (oder Gemini CLI Konfiguration):

```json
{
  "mcpServers": {
    "local-knowledge": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/atlassian-fs/app.py", "mcp"]
    }
  }
}
```

> **Wichtig:** Beim Aufruf von `app.py mcp` gehen alle Protokoll-Nachrichten über `stdout`. Jegliches Logging wird automatisch auf `stderr` umgeleitet, um das JSON-RPC-Protokoll nicht zu stören.

---

## 🛠️ CLI-Befehlsübersicht (`app.py`)

| Befehl | Beschreibung | Wichtige Optionen |
|---|---|---|
| `python3 app.py init-db` | Erstellt/migriert SQLite-Schema und FTS5-Index. | `--config PATH` |
| `python3 app.py serve` | Startet den Webserver und Background JobManager. | `--host`, `--port`, `--config PATH` |
| `python3 app.py status` | Gibt Datenbank- und Index-Statistiken als JSON auf `stdout` aus. | `--config PATH` |
| `python3 app.py mcp` | Startet den MCP-Server über `stdio` für KI-Assistenten. | `--config PATH` |

---

## 🏗️ Architektur & Modulübersicht

| Modul | Zweck | Wesentliche Abhängigkeiten |
|---|---|---|
| `app.py` | CLI-Einstiegspunkt für `serve`, `mcp`, `init-db` und `status`. | `argparse`, `sys`, `json`, `knowledge.*` |
| `knowledge/webserver.py` | HTTP-Server (`ThreadingHTTPServer`), Sicherheits-Header, DNS-Rebinding-Schutz, REST-API & statisches File-Serving. | `http.server`, `urllib`, `knowledge.db`, `knowledge.config` |
| `knowledge/db.py` | SQLite-Datenbankschicht mit WAL-Modus, FTS5-Volltextsuche, Migrationspfad und Dokument-/Kanten-Verwaltung. | `sqlite3` |
| `knowledge/config.py` | Konfigurationsmanagement, Validierung (`SETTINGS_FIELD_SPECS`, `JOB_FIELD_SPECS`), Env-Overrides und `ConfigStore` (Dateirechte 0600). | `json`, `os`, `threading` |
| `knowledge/models.py` | Datenmodelle (`FetchedDocument`, `Chunk`, `LinkTarget`), UID-Helfer und Ausnahmehierarchie. | `dataclasses` |
| `knowledge/atlassian_http.py` | Robuster HTTP-Client mit Token-Bucket-Ratenbegrenzung, Exponential Backoff, SSL-Context-Factory und Request-Budget-Überwachung. | `urllib.request`, `ssl`, `time` |
| `knowledge/confluence.py` | Confluence REST API Client (Server/DC v1 & Cloud v2), Pagination, Child-Pages und Body-Extraktion. | `knowledge.atlassian_http` |
| `knowledge/jira.py` | Jira REST API Client (Server/DC v2 & Cloud v3), JQL-Suche, Issue-Links, Epics und Subtasks. | `knowledge.atlassian_http` |
| `knowledge/links.py` | URL-Normalisierung, Basis-URL-Matching und Klassifizierung von Links in UIDs (`confluence:<id>`, `jira:<key>`). | `urllib.parse`, `re` |
| `knowledge/text.py` | HTML- und ADF-Parsing zu sauberem Plaintext, Chunking mit Satzgrenzenerkennung und Überlappung. | `html.parser` |
| `knowledge/indexer.py` | Multithreaded Crawler (`JobManager`) mit BFS-Traversierung, Budgetkontrolle, Atomic DB-Writes und Abbruch-Signalisierung. | `threading`, `collections.deque` |
| `knowledge/search.py` | FTS5-Volltextsuche mit BM25-Scoring, Snippet-Generierung und Filterung nach Dokumenttyp/Projekt. | `knowledge.db` |
| `knowledge/graph.py` | Teilgraph-Extraktion für Graph-Visualisierung, Nachbarschaftssuche (Hops) und Detailansichten. | `knowledge.db` |
| `knowledge/mcp_server.py` | JSON-RPC 2.0 stdio MCP-Server mit Tools für KI-Agenten (`search_knowledge`, `get_document`, etc.). | `sys`, `json` |
| `web/` | Vanilla JS / CSS Frontend ohne Build-Tools (Responsive UI, Graph, Search, Live-Job-Status). | HTML5, CSS3, JavaScript |

---

## 🔒 Sicherheitshinweise

- **Lokale Bindung:** Der Server bindet per Voreinstellung strikt auf `127.0.0.1`. Es wird kein ungesicherter Port im LAN/WAN geöffnet.
- **DNS-Rebinding-Schutz:** Der Webserver prüft den `Host`-Header auf erlaubte Werte (`127.0.0.1`, `localhost` oder den explizit konfigurierten Host). Abweichende Host-Header werden mit HTTP 403 abgewiesen.
- **CSRF- & Origin-Schutz:** Zustandsändernde Aufrufe (`POST`, `PUT`) verifizieren den `Origin`-Header.
- **Security-Header:** Alle HTTP-Antworten enthalten strenge Header (`Content-Security-Policy: default-src 'self' ...`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, sowie `Cache-Control: no-store` für die API).
- **Zertifikatsprüfung (TLS):** `verify_ssl` ist per Voreinstellung auf `false` gesetzt, da viele interne Atlassian Server/DC-Installationen mit firmeninternen CAs betrieben werden. Sobald die Zertifikatskette im Betriebssystem oder über `ca_bundle` hinterlegt ist, sollte `verify_ssl: true` aktiviert werden.
- **Dateirechte config.json:** Die Konfigurationsdatei kann vertrauliche Zugangsdaten enthalten. `ConfigStore` schreibt `config.json` atomar mit restriktiven Dateirechten (`0600` / nur Besitzer les- und schreibbar). `config.json` ist in `.gitignore` eingetragen und darf nicht ins Repository eingecheckt werden.

---

## 🧪 Tests ausführen

Die gesamte Testsuite kann ohne Netzwerkzugriff auf Confluence oder Jira ausgeführt werden:

```bash
# Alle Tests ausführen
python3 -m unittest discover tests -v

# Spezifisch den Webserver testen
python3 -m unittest tests.test_webserver -v
```
