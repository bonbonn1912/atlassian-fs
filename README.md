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
- **Startseite:** URL oder Page-ID einer Confluence-Seite, `space:KEY` bzw. eine Space-URL
  (`https://confluence.intern/display/KEY`) für einen kompletten Space-Crawl – oder eine
  Space-Auswahl/`space_keys` (siehe Abschnitt 8). Jira-Inhalte werden über Links aus Confluence
  bzw. in der Space-/Änderungs-Indexierung erreicht.
- **Tiefen- und Mengenbeschränkungen:** Konfigurieren Sie Seiten-Limits, Traversierungstiefe und Request-Budgets.
- Der Crawler analysiert HTML-Inhalte, extrahiert Hyperlinks und Jira-Issue-Keys (`PROJ-123`), chunked Inhalte und aktualisiert den FTS5-Index sowie den Wissensgraphen.

### 7. Wissensgraph & Suche nutzen

- **Suche:** Suchanfragen mit FTS5-Ranking (BM25), Filterung nach Typ (`confluence`, `jira`) oder Projekt.
- **Graph:** Interaktiver Graph mit Nachbarschaftsanzeige (Hops 1–3), Kantentypen (`links_to`, `mentions_jira`, `jira_parent_of`, `parent_of` etc.).
- **Top-Dokumente:** Rangliste der wichtigsten Dokumente (ein-/ausgehende Kanten); per Klick direkt
  in Suche oder Graph springen (`GET /api/top-documents?types=&limit=&offset=&project=&q=`).

Für große Graphen lässt sich unter **Einstellungen → Max. sichtbare Knoten im Graph**
`graph.max_visible_nodes` bis auf **100000** erhöhen (Standard bleibt 5000).
Die Graph-Werkzeugleiste übernimmt diese Obergrenze nach dem Neuladen der Seite.
Ab 5001 Knoten wird ein mehrstufiges Layout verwendet: verbundene Gruppen werden
angeordnet und anschließend in sämtliche Einzelknoten aufgelöst. Die Positionen
ändern sich dadurch; Farben, Formen, Kantenstile und Bedienung bleiben erhalten.
Kein angeforderter Knoten wird für die Darstellung stichprobenartig entfernt.

Große Ansichten werden mit OffscreenCanvas in einem Worker gezeichnet. Räumliche
Indizes begrenzen die Arbeit beim Hineinzoomen; während Zoom/Pan wird das letzte
Bild bewegt und anschließend scharf nachgezeichnet. Ohne OffscreenCanvas bleibt
der Canvas-Fallback verfügbar, beansprucht aber den UI-Thread. Große Graph-Antworten
werden bei entsprechender Browser-Unterstützung mit gzip übertragen. Die tatsächliche
Ladezeit hängt zusätzlich von Kantenzahl, Datenbank, Hardware und Verbindung ab.

**3D-Zeitflug:** Der zusätzliche Schalter neben dem Wichtigkeits-Modus öffnet eine
räumliche Ansicht. Confluence-Seiten werden nach Space gruppiert, verknüpfte
Jira-Tickets dem am stärksten verbundenen Space zugeordnet. Tickets ohne
Confluence-Verbindung bilden eigene Jira-Projekt-Cluster. Die neueren Inhalte
liegen vorne, die älteren weiter hinten im Raum. Confluence verwendet immer die
letzte Änderung; über **Jira-Zeitachse** lassen sich **Erstellt am** oder **Letzte
Änderung** wählen. Über **Zeitachse** lässt sich eine durchgehende Achse mit gut
sichtbaren Jahres- und Monatsmarkierungen entlang der Zeitrichtung einblenden (standardmäßig ausgeschaltet).
Fehlende Datumswerte erscheinen in einer separaten Ebene vor
der Zeitachse und werden nicht als alte Einträge ausgegeben. Das Erstellungsdatum
bereits indexierter Tickets wird beim nächsten erneuten Abruf ergänzt.

Im fokussierten Raum: ziehen zum Umsehen, Mausrad oder W/S zum Fliegen, A/D
seitwärts und Q/E vertikal. Beim Vorbeiflug werden die Titel nahegelegener Objekte
automatisch eingeblendet (maximal ~8 im Blickfeld, ohne Klick). **Startposition**
setzt die Kamera zurück. Klick öffnet
den bestehenden Inspektor, Doppelklick lädt das Objekt als Graph-Zentrum. Der
Wichtigkeits-Modus bleibt zusätzlich verwendbar. Die Flugansicht benötigt WebGL;
falls es nicht verfügbar ist, bleibt die 2D-Ansicht nutzbar.

### 8. Spaces auswählen & geänderte Seiten nachziehen

Im Reiter **Einstellungen** gibt es den Abschnitt **Confluence-Spaces & Aktualisierung**:

- **Spaces laden:** listet alle Confluence-Spaces auf, die mit dem konfigurierten Token sichtbar sind
  (`GET /api/spaces`, 10 Minuten Cache, `?refresh=1` erzwingt einen neuen Abruf). Der Eintrag
  `verify_ssl: false` gilt auch hier, self-signed Zertifikate sind also kein Hindernis.
- **Auswahl:** Spaces ankreuzen und mit **Einstellungen speichern** dauerhaft in
  `confluence.selected_spaces` ablegen. **Ausgewählte Spaces indexieren** startet sofort einen
  Space-Crawl über alle markierten Spaces (inkl. isolierter Seiten ohne eingehende Links).
  Alternativ lassen sich die Keys im Reiter **Indexierung** als kommagetrennte Liste
  (`space_keys`) angeben.
- **Geänderte Seiten seit Datum:** Datum wählen (Standard: vor 7 Tagen) und
  **Geänderte Seiten indexieren** starten. Der Job fragt Confluence per CQL
  (`type=page and lastmodified >= "<Datum>"`, optional `space in (...)`) ab und aktualisiert
  genau diese Seiten – ideal für den regelmäßigen Abgleich, z. B. „alle Seiten seit letzter Woche“.
  Der Modus ist auch als Job-Parameter `modified_since` verfügbar (ISO-Datum oder relativ
  `7d` / `2w`). Bereits indexierte Seiten werden dabei erneut abgerufen, neue Links können
  zusätzlich in den Index wandern.
- **Der klassische Weg bleibt erhalten:** `start_page` (URL/Page-ID), ein einzelner
  `space_key` und der bestehende `refresh_only`-Lauf funktionieren unverändert.

### 9. Batch-Abruf (bis zu 100 Dokumente pro Request)

Der Crawler lädt Dokumente gebündelt statt einzeln:

- **Confluence:** `GET /rest/api/content/search?cql=id in (…,&limit=100` (Server/DC) bzw.
  `GET /api/v2/pages?id=…&id=…&body-format=storage` (Cloud) – bis zu **100 Seiten in einem Request**
  inkl. Body, Version, Space und Labels.
- **Jira:** `GET /rest/api/2/search?jql=key in (…)&maxResults=100` (Server/DC) bzw.
  `/rest/api/3/search/jql` mit Fallback auf `/rest/api/3/search` (Cloud) – bis zu **100 Tickets pro
  Request**. Remote-Links bleiben Einzelrequests (`/issue/{key}/remotelink`), außer
  `include_remote_links=false`.
- **Stellschraube:** `batch_fetch_size` (1–100, Standard 100) im Reiter *Indexierung*
  (Netzwerk & Rate-Limiting) bzw. in den Crawl-Standardwerten. Zusammen mit dem Sleep zwischen
  Requests sinkt die Laufzeit grob auf `Seiten/100 × Sleep` statt `Seiten × Sleep`.
- Auch die Auflistung nutzt 100 Einträge je Request (Spaces, Space-Seiten, CQL-Suche, Kindseiten).
- **Kindseiten** werden gebündelt abgefragt: bis zu 25 Elternseiten pro CQL-Request
  (`type=page and ancestor in (…)`); nur bei sehr dichten Hierarchien (>100 Treffer) fällt der
  Client auf exakte Einzelabfragen zurück. Bei Space-Crawls kosten die Eltern-Kind-Kanten
  ohnehin keinen Extra-Request (Ableitung aus `expand=ancestors`).
- **Titel-Links** (`/display/SPACE/Titel`) werden mit bis zu 50 Titeln pro Request aufgelöst und
  dauerhaft in `title_resolutions` gecacht – Folgeläufe kosten dafür keine Requests mehr.
- Für maximale Geschwindigkeit bleiben zwei Optionen abschaltbar: „Unterseiten berücksichtigen"
  (Eltern-Kind-Kanten) und „Jira-Remote-Links abrufen" (Jira→Confluence-Kanten).

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

### Dokumentausgabe und Tabellen

Unter **Einstellungen → MCP-Dokumentausgabe** lassen sich die Standardausgabe
(anfangs 20.000 Zeichen) und die maximale Ausgabe (anfangs 500.000 Zeichen)
einstellen. Beide Werte erlauben 100 bis 2.000.000 Zeichen; die Standardausgabe darf
die maximale Ausgabe nicht überschreiten. Gespeicherte Werte gelten beim nächsten
Dokumentabruf auch in einem bereits laufenden `app.py mcp`-Prozess, sofern UI und
MCP dieselbe Konfigurationsdatei verwenden (`--config`). Nach einem Code-Update
müssen bestehende Serverprozesse einmal neu gestartet werden.

`get_document` verwendet ohne `max_chars` die eingestellte Standardausgabe.
Ein expliziter Wert wird auf die konfigurierte maximale Ausgabe begrenzt.
Die Antwort enthält `max_chars` (angewendetes Limit), `total_chars`,
`returned_chars` und `truncated`. Das Limit betrifft den Dokumenttext in Zeichen,
nicht Tokens, Metadaten oder die Größe der gesamten JSON-Antwort. Sehr große
Antworten können zusätzlich vom KI-Client begrenzt werden.

Confluence-Storage- und Jira-ADF-Tabellen werden zellenweise in Markdown umgewandelt.
Leere Zellen bleiben erhalten; Absätze innerhalb einer Zelle werden mit ` / ` getrennt,
verbundene Zellen auf ihre Spalten/Zeilen verteilt und Kopfzeilen zusammengeführt.
Verschachtelte Confluence-Tabellen erscheinen als beschrifteter Text in der äußeren
Zelle. Tabellen ohne Kopfzeile erhalten neutrale Spaltennamen. Jira-Wiki-Tabellen
verwenden dieselbe Darstellung. Große Tabellen werden für die Suche an Zeilengrenzen
geteilt, mit wiederholtem Tabellenkopf pro Chunk. Eine einzelne sehr große Zeile darf
die Chunk-Zielgröße überschreiten, um ihre Zellzuordnung zu erhalten.

Bereits gespeicherte Seiten brauchen einen erneuten Abruf, da die Datenbank das
ursprüngliche HTML nicht speichert. Nach Ende eines laufenden Crawls kann dies mit
`python3 app.py refresh` erfolgen; dafür gelten die eingestellten Job-Budgets.

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

Die JavaScript-Regressionstests benötigen Node.js (nur eingebaute Module):

```bash
node --test tests/graph.test.cjs
```

Reproduzierbarer Browser-Benchmark mit **100000 Knoten und 199998 Kanten**:

```bash
python3 -m http.server 8766 --bind 127.0.0.1
# http://127.0.0.1:8766/tests/graph-benchmark.html?nodes=100000
# Mit &fallback=1 lässt sich der Canvas-Fallback prüfen.
```

Der Benchmark misst Datenaufbereitung, Layout, erstes vollständiges Bild und
Zeichenaufrufe auf dem UI-Thread. Er verwendet synthetische Daten im Browser;
API- und Netzwerkzeit sind nicht enthalten. Zur Bewertung schwacher Geräte die
CPU-Drosselung der Browser-Entwicklerwerkzeuge aktivieren oder auf Zielhardware
messen. Kurze Zeichenaufrufe während Pan/Zoom messen die Cache-Nutzung, nicht die
Rasterisierungsdauer des nächsten vollständigen Bildes im Worker.

Der separate 3D-Benchmark ist unter
`http://127.0.0.1:8766/tests/graph-flight-benchmark.html?nodes=100000` erreichbar.
Er verwendet zwölf Space-Cluster, verknüpfte Tickets und unterschiedliche
Erstellungs-/Änderungsdaten. Die Zeitachse kann während der Messung umgeschaltet
werden. Auch dieser Benchmark enthält keine API- oder Netzwerkzeit.
