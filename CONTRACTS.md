# CONTRACTS.md – verbindliche Modulschnittstellen

Diese Datei ergänzt `implementation.md` (fachliche Spezifikation, bitte lesen) um die
konkreten Python-Schnittstellen. Alle Module halten sich exakt daran.

## Globale Regeln

- Python 3.10+, **nur Standardbibliothek**. `from __future__ import annotations` oben in jedem Modul.
- Fertige Basismodule (NICHT ändern, nur importieren): `knowledge/models.py`,
  `knowledge/config.py`, `knowledge/db.py`. Wenn dir dort etwas fehlt: im Abschlussbericht melden,
  lokal im eigenen Modul lösen.
- Jeder Agent schreibt **nur seine eigenen Dateien** (siehe Zuständigkeit unten) plus eigene Tests
  `tests/test_<modul>.py` (unittest, keine Netzwerkzugriffe, Fixtures unter `tests/fixtures/`).
- Tests laufen mit: `python3 -m unittest discover -s tests -v` aus dem Projektroot.
- Logging über `logging.getLogger(__name__)`, niemals `print` (MCP nutzt stdout!).
- UI-/Fehlertexte für Nutzer auf Deutsch, Code/Kommentare auf Englisch.

## Zielumgebung: self-hosted Jira & Confluence (Server/Data Center)

- Standard ist **Server/Data Center**: Confluence `api_mode="server"`, Jira `api_mode="server_v2"`.
  Cloud (`cloud_v2` / `cloud_v3`) wird zusätzlich unterstützt.
- **TLS-Zertifikatsprüfung ist per Default deaktiviert** (`config["http"]["verify_ssl"] = False`),
  per UI einschaltbar; optional `ca_bundle` (PEM-Pfad) bei `verify_ssl=True`.
- `base_url` ist die Web-Wurzel inkl. Context-Path, ohne Slash am Ende, z. B.
  `https://confluence.intern`, `https://intern.firma.de/confluence`, `https://jira.intern`.
  Für Confluence Cloud ist `base_url` z. B. `https://firma.atlassian.net/wiki`.
- Optionale Auth (für DC üblich): `auth.type` = `none` | `bearer` (Personal Access Token,
  Header `Authorization: Bearer <token>`) | `basic` (`username` + `token` als Passwort).
- `aliases`: weitere Basis-URLs derselben Instanz (z. B. alter Hostname) – Links darauf werden
  erkannt; API-Requests gehen aber **immer** an `base_url`.

## Konfiguration (`knowledge/config.py`)

`ConfigStore(path).get()` liefert ein Dict (siehe `DEFAULT_CONFIG`). Relevante Schlüssel:

```
cfg["confluence"] = {base_url, api_mode, sleep_seconds, aliases, selected_spaces, auth:{type,username,token}}
cfg["jira"]       = {base_url, api_mode, sleep_seconds, aliases, project_keys, auth:{...}}
cfg["http"]       = {verify_ssl, ca_bundle, use_system_proxy, max_response_mb, user_agent}
cfg["crawl_defaults"], cfg["index"], cfg["search"], cfg["graph"], cfg["server"], cfg["database"]
```

`validate_job_params(payload, cfg) -> (params, errors)` – `params` enthält alle Felder aus
`JOB_FIELD_SPECS` (inkl. `confluence_sleep_seconds`, `jira_sleep_seconds`,
`request_timeout_seconds`, `max_retries`, `include_remote_links`, `resolve_page_titles`,
`space_keys`, `modified_since` und `batch_fetch_size`). `space_keys` wird als
`spacekeylist` validiert (Format, Dedupe, Case bleibt erhalten); `modified_since` akzeptiert
`JJJJ-MM-TT`, ISO-Zeitstempel (auch mit Zeitzone/`Z`) oder relativ (`7d`, `2w`) und wird auf
ein ISO-Datum normalisiert. `ConfigStore` nutzt einen RLock (`update()` liest die Config beim
Serialisieren erneut), prüft `graph.default_hops <= graph.max_hops`, wendet `apply_env_overrides`
in `get()` an und schreibt `config.json` mit Rechten 0600.
`ConfigStore.get_public()` = Config ohne Tokens (`auth.token=""`, `auth.token_set=bool`).
`ConfigStore.update(patch)` validiert + speichert, wirft `ConfigError`.

## Modelle (`knowledge/models.py`)

UIDs: `confluence:<pageId>`, `jira:<KEY-1>`, ungelöste Titelreferenz
`confluence:unresolved:<SPACEKEY>:<Titel>` (`unresolved_page_uid`). Hilfen: `confluence_uid`,
`jira_uid`, `parse_uid`, `is_unresolved_uid`, `split_unresolved_uid`, `edge_kind_for`.
Datenklassen: `LinkTarget(uid, kind, relation_label, source_field, anchor_text, evidence_url, title_hint)`,
`Chunk(heading, body)`, `FetchedDocument(...)`. Kanten-Konstanten `EDGE_*`, Zustände `STATE_*`, `JOB_*`.
Exceptions: `CrawlStopped`, `BudgetExceeded`, `HostNotAllowed`, `AtlassianError(status,url)`,
`NotFoundError`, `ForbiddenError` (401/403), `InvalidResponseError`, `ConfigError`.

**Kantenrichtung:** Quelle ist immer das *abgerufene* Dokument. Jira-Parent: Issue X mit
`fields.parent = P` → Kante X→P, kind `jira_parent_of`, relation_label `"child of"`;
Subtask S von X → Kante X→S, relation_label `"parent of"`. Confluence-Kinder:
Seite → Kind, kind `parent_of`. Jira-issuelinks: Kante X→Y, kind `jira_relates_to`,
relation_label = `type.outward` (bei `outwardIssue`) bzw. `type.inward` (bei `inwardIssue`),
source_field `"issuelinks"`.

## DB (`knowledge/db.py`)

`connect(path, readonly=False)`, `open_db(path)` (connect+init_db), `init_db`, `transaction(conn)`,
`get_document(conn, uid)->dict|None`, `ensure_node(conn, uid, title_hint, url)` (ohne eigene Transaktion),
`add_edge(conn, source_uid, link)`, `save_document(conn, doc, chunks)` (atomar),
`mark_document_state(conn, uid, state, error)`, `get_outgoing_links(conn, uid)->list[LinkTarget]`,
Jobs: `create_job`, `update_job(conn, job_id, **cols)`, `get_job`, `get_latest_job`,
`mark_interrupted_jobs`, `queue_add`, `queue_set_state`, `queue_counts`, `add_job_log`, `get_job_log`,
`get_stats(conn)`. Verbindungen sind autocommit; eine Verbindung pro Thread.
Tabelle `documents` hat zusätzlich Spalte `labels` (kommagetrennt).

---

## Modul `knowledge/links.py` (Agent „parsing“)

```python
@dataclass(frozen=True)
class LinkContext:
    confluence_bases: tuple[str, ...]   # base_url + aliases, normalized (no trailing slash)
    jira_bases: tuple[str, ...]
    confluence_base: str                # canonical base (first)
    jira_base: str
    project_keys: frozenset[str] = frozenset()   # allow-list for plain-text keys (empty = all)

    @classmethod
    def from_config(cls, cfg: dict) -> "LinkContext": ...

ISSUE_KEY_RE: re.Pattern            # r"\b[A-Z][A-Z0-9_]+-[1-9][0-9]*\b"
def normalize_issue_key(key: str) -> str | None
def classify_url(href: str, ctx: LinkContext) -> tuple[str, str] | None
    # -> (target_uid, normalized_evidence_url) or None if not an internal Confluence/Jira object.
    # Relative hrefs ("/pages/viewpage.action?pageId=1", "/display/ENG/Foo") are resolved
    # against ctx.confluence_base. Hosts are compared case-insensitively incl. port, and the
    # base path (context path) must prefix the URL path. Recognize at least:
    #  Confluence: /pages/viewpage.action?pageId=123, /spaces/SPACE/pages/123[/slug],
    #              /wiki/spaces/SPACE/pages/123 (cloud), /display/SPACE/Page+Title  -> unresolved uid
    #              (title URL-decoded, '+' -> ' '), /pages/editpage.action?pageId=, ?pageId= in general
    #  Jira:       /browse/KEY-1, /projects/X/issues/KEY-1, ?selectedIssue=KEY-1, /jira/software/.../KEY-1
    # If confluence and jira share a host, the path prefix decides (longest matching base wins).
    # Ignore mailto:, javascript:, #anchors, attachments (/download/...), and unknown hosts.
def parse_start_page(value: str, ctx: LinkContext) -> str | None
    # "12345" -> "confluence:12345"; any Confluence URL -> its uid (may be unresolved title uid);
    # returns None if not recognizable.
def confluence_page_url(ctx, page_id: str) -> str   # f"{base}/pages/viewpage.action?pageId={id}"
def jira_issue_url(ctx, key: str) -> str             # f"{jira_base}/browse/{KEY}"
def find_plain_issue_keys(text: str, ctx: LinkContext) -> list[str]   # unique, in order, filtered by project_keys
```

## Modul `knowledge/text.py` (Agent „parsing“)

```python
@dataclass
class ParsedContent:
    sections: list[tuple[str, str]]   # (heading, text); text before first heading has heading ""
    links: list[LinkTarget]           # kind set via models.edge_kind_for(source_type, uid)
    plain_text: str                   # whole readable text (sections joined)

def parse_confluence_storage(storage: str, ctx: LinkContext, *, space_key: str = "",
                             scan_plain_keys: bool = False) -> ParsedContent
    # Confluence storage format (XHTML + ac:/ri: namespaces) via html.parser.HTMLParser
    # (convert_charrefs=True). Handles: <a href>, <ac:link><ri:page ri:content-title ri:space-key/>
    # (missing space-key => use space_key param) -> resolve to unresolved_page_uid unless
    # <ri:content-entity ri:content-id> / ri:content-id present -> confluence_uid;
    # <ac:link-body>/<ac:plain-text-link-body> as anchor text; Jira macro
    # <ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">ABC-1</ac:parameter>
    # (source_field "jira-macro"; ignore jqlQuery macros), smart links / inline cards
    # (<a href data-card-appearance> are plain <a>), CDATA in <ac:plain-text-body> (code) as text,
    # headings h1-h6 start new sections, block tags (p, li, tr, br, div, table cells) produce
    # line breaks / separators, <ac:parameter> values are NOT body text (except none),
    # script/style ignored. Whitespace collapsed per line. Dedupe links on (uid, kind) keeping first.
    # scan_plain_keys=True adds mentions_jira links with source_field "plain_text".
def adf_to_content(adf: dict | None, ctx: LinkContext, *, source_type: str = "jira",
                   scan_plain_keys: bool = False) -> ParsedContent
    # Atlassian Document Format (Jira Cloud v3). Recursive, never crashes on unknown nodes
    # (just recurse into "content"). heading -> new section; paragraph/listItem/codeBlock/table rows
    # -> lines; bulletList "- ", orderedList "1. "; hardBreak -> newline; links from
    # marks[type=link].attrs.href, inlineCard/blockCard/embedCard attrs.url; mention -> attrs.text.
def jira_wiki_to_content(text: str | None, ctx: LinkContext, *, scan_plain_keys: bool = False
                         ) -> ParsedContent
    # Jira Server/DC v2 description = wiki markup string. h1.-h6. -> sections; strip {code}/{noformat}
    # markers (keep content), {quote}, {panel...}, *bold*, _it_, [text|url] -> text + link, [url],
    # bare http(s) URLs, [~user] removed, |table|cells| -> " | ". Links classified via classify_url.
def chunk_sections(title: str, sections: list[tuple[str, str]], *, target_chars: int = 1200,
                   overlap_chars: int = 150, min_chars: int = 80) -> list[Chunk]
    # Split per section on paragraph/line/sentence boundaries into ~target_chars bodies,
    # overlap of the previous chunk's tail (whole words) prefixed; merge chunks shorter than
    # min_chars into neighbour; never return empty bodies; if nothing -> [Chunk("", title)].
def truncate_text(text: str, max_chars: int) -> tuple[str, bool]
```

## Modul `knowledge/atlassian_http.py` (Agent „api“)

```python
Transport = Callable[[str, dict[str, str], float, ssl.SSLContext | None, int], "HttpResponse"]
# transport(url, headers, timeout, ssl_context, max_bytes) -> HttpResponse; raises OSError/
# TimeoutError/urllib.error.URLError on network errors. Default: urllib (GET only).
@dataclass
class HttpResponse:
    status: int; headers: dict[str, str]  # lower-case keys
    body: bytes

def make_ssl_context(verify_ssl: bool, ca_bundle: str = "") -> ssl.SSLContext
    # verify_ssl False -> check_hostname=False, verify_mode=CERT_NONE (self-signed internal CA)
def build_auth_headers(auth: dict) -> dict[str, str]
def default_transport(use_system_proxy: bool) -> Transport
    # urllib.request.build_opener(ProxyHandler({}) if not use_system_proxy, HTTPSHandler(context=...)),
    # HTTP errors (4xx/5xx) must be returned as HttpResponse, not raised. Reads at most max_bytes+1
    # bytes; the client raises InvalidResponseError if larger. Do NOT follow redirects to other hosts
    # (custom HTTPRedirectHandler: allow only same scheme+host+port, otherwise return the 3xx).

class RequestBudget:
    def __init__(self, max_requests: int, max_duration_seconds: float, clock=time.monotonic)
    used: int (property); remaining_seconds() -> float
    def check(self) -> None        # raises BudgetExceeded if requests used up or time over
    def consume(self) -> None      # check() then used += 1   (thread-safe)

class RateLimitedClient:
    def __init__(self, source: str, base_url: str, *, allowed_bases: Sequence[str],
                 sleep_seconds: float, timeout: float, max_retries: int,
                 stop_event: threading.Event, budget: RequestBudget,
                 ssl_context: ssl.SSLContext | None, auth_headers: dict[str, str],
                 user_agent: str = "local-knowledge/1.0", max_response_bytes: int = 20 * 2**20,
                 transport: Transport | None = None, on_event: Callable[[dict], None] | None = None,
                 clock=time.monotonic, rand=random.random)
    def get_json(self, path_or_url: str, params: dict | None = None) -> Any
    def get_json_with_headers(self, path_or_url, params=None) -> tuple[Any, dict[str, str]]
```
Regeln (spec §11): relative Pfade werden an `base_url` gehängt; absolute URLs müssen mit
`base_url` beginnen (sonst `HostNotAllowed`, kein Request). Vor **jedem** Versuch: `stop_event`
prüfen (`CrawlStopped`), `budget.consume()` (`BudgetExceeded`), Abstand seit Start des letzten
Requests dieser Quelle ≥ `sleep_seconds` – Warten über `stop_event.wait()` (abbrechbar; danach
`CrawlStopped` wenn gesetzt), während des Wartens `on_event({"phase":"rate_limit_wait",
"source":..., "seconds":rest, "reason":"sleep"|"429"|"retry"})`, nach dem Warten
`on_event({"phase":"request","source":...,"url":...})`. 429 → `Retry-After` (Sekunden oder
HTTP-Datum) als Mindestwartezeit, sonst Backoff 2,4,8,16… (max 300 s, +Jitter ≤ 10 %).
429/502/503/504/Netzfehler/Timeout → bis `max_retries` Wiederholungen; jeder Versuch zählt
gegen das Budget. Nach Erschöpfen: `AtlassianError`. 404 → `NotFoundError`, 401/403 →
`ForbiddenError` (Meldung: „Zugriff verweigert (HTTP 403) – Berechtigung/Token prüfen“),
andere 4xx → `AtlassianError` (ohne Retry). Antwort > max bytes oder kein JSON →
`InvalidResponseError`. Header: `Accept: application/json`, `User-Agent`, Auth.
Ein HTML-Login-Redirect (200 mit text/html) ist `ForbiddenError("... nicht angemeldet ...")`.

## Modul `knowledge/confluence.py` (Agent „api“)

```python
class ConfluenceClient:
    def __init__(self, http: RateLimitedClient, api_mode: str, ctx: LinkContext,
                 index_cfg: dict | None = None)
    def fetch_document(self, page_id: str, *, include_children: bool, max_children: int = 500,
                       scan_plain_keys: bool = False) -> FetchedDocument
    def list_child_pages(self, page_id: str, max_items: int) -> list[tuple[str, str]]  # (id, title)
    def list_space_pages(self, space_key: str, max_items: int = 100_000) -> list[tuple[str, str]]
    def list_spaces(self, max_items: int = 2000) -> list[dict]  # {key,name,type,id,url}
    def list_pages_modified_since(self, since: str, space_keys: list[str] | None = None,
                                  max_items: int = 10_000) -> list[dict]  # {id,title,space,updated_at}
    def fetch_documents_bulk(self, page_ids: list[str], *, include_children: bool = False,
                             max_children: int = 500, scan_plain_keys: bool = False,
                             chunk_size: int = 100) -> dict[str, FetchedDocument | Exception]
    def resolve_title(self, space_key: str, title: str) -> str | None   # page id; None if 0 or >1 hits
    def test_connection(self) -> dict   # {"ok": bool, "message": str, ...}
```
- **server**: `GET /rest/api/content/{id}?expand=body.storage,version,space,history.lastUpdated,ancestors`
  → `id,title,type,space.key,version.number,version.when,body.storage.value,_links.webui`.
  Kinder: `GET /rest/api/content/{id}/child/page?limit=100&start=N`, Pagination über `_links.next`
  (relativ zu Context-Path! `_links.next` beginnt oft mit `/rest/...` ohne Context-Path →
  an `base_url` anhängen; falls schon mit Context-Path, nicht doppeln) bzw. `start+=size`
  solange `size==limit`. Titel-Resolver: `GET /rest/api/content?type=page&spaceKey=X&title=Y&limit=2`.
  Test: `GET /rest/api/space?limit=1`.
- **cloud_v2**: `GET /api/v2/pages/{id}?body-format=storage` (base_url endet auf `/wiki`), Space-Key
  über `GET /api/v2/spaces/{spaceId}` (pro spaceId gecacht), Kinder `GET /api/v2/pages/{id}/direct-children?limit=100`
  (nur `type=="page"`, Pagination `_links.next` relativ zu Host-Wurzel, z. B. `/wiki/api/v2/...`),
  Resolver `GET /rest/api/content?type=page&spaceKey=..&title=..` (v1 existiert in Cloud), Test `GET /api/v2/spaces?limit=1`.
- Fehlt `body.storage.value` → `InvalidResponseError`. `type != "page"` (z. B. blogpost) wird trotzdem
  indexiert, `document_type` = type.
- URL: `base_url + _links.webui` (Host stets aus Config), Fallback `confluence_page_url`.
- `FetchedDocument`: uid `confluence:<id>`, space_or_project = Space-Key, document_type `page`,
  source_version = Versionsnummer, source_updated_at = version.when / createdAt, links = Body-Links
  + Kinder (`parent_of`, source_field `"children"`, title_hint = Kindtitel).
  body_text via text.parse_confluence_storage, abgeschnitten auf index_cfg["max_document_chars"].
- **Space-Discovery** (`list_spaces`): server `GET /rest/api/space?limit=100&start=N`,
  cloud_v2 `GET /api/v2/spaces?limit=100` (Cursor über `_links.next`). Ergebnis dedupliziert,
  nach Name sortiert; `url` = `base_url + /display/<key>` (server) bzw. `base_url + /spaces/<key>` (cloud).
- **Änderungs-Suche** (`list_pages_modified_since`): CQL-Suche
  `GET /rest/api/content/search?cql=type=page and lastmodified >= "<Datum>" [and space in (...)] order by lastmodified desc`
  (existiert in Server/DC und Cloud), Pagination über `_links.next` bzw. `start+=size`.
  Nur `type=="page"`-Treffer. Leeres `since` → leere Liste.
- **Batch-Abruf** (`fetch_documents_bulk`, bis zu 100 IDs je Request): server
  `GET /rest/api/content/search?cql=id in (1,2,...)&limit=100&expand=body.storage,version,space,history.lastUpdated,metadata.labels`,
  cloud_v2 `GET /api/v2/pages?id=..&id=..&body-format=storage&limit=100` (Cursor via `_links.next`).
  Ergebnis `{page_id: FetchedDocument|Exception}`; IDs, die die Antwort nicht enthält, werden
  einzeln nachgeladen (nur so lässt sich 403 „forbidden" von 404 „missing" unterscheiden).
  `include_children` kostet wie bisher
  einen zusätzlichen Request je Seite (kein Bulk-Endpunkt vorhanden).
  Listen-/Suchlimits sind auf 100 je Request erhöht (`CHILD_PAGE_SIZE`, `SPACE_PAGE_SIZE`,
  `SEARCH_PAGE_SIZE`, `BULK_PAGE_SIZE`). `list_pages_modified_since` validiert Space-Keys
  gegen `^[A-Za-z0-9_.~:@-]+$` (CQL-Injektion) und wirft sonst `ValueError`.

## Modul `knowledge/jira.py` (Agent „api“)

```python
JIRA_FIELDS = "summary,description,status,issuetype,project,labels,components,created,updated,issuelinks,parent,subtasks"
class JiraClient:
    def __init__(self, http: RateLimitedClient, api_mode: str, ctx: LinkContext,
                 index_cfg: dict | None = None)
    def fetch_document(self, issue_key: str, *, include_remote_links: bool,
                       scan_plain_keys: bool = False) -> FetchedDocument
    def fetch_documents_bulk(self, issue_keys: list[str], *, include_remote_links: bool = True,
                             scan_plain_keys: bool = False,
                             chunk_size: int = 100) -> dict[str, FetchedDocument | Exception]
    def test_connection(self) -> dict   # server_v2: GET /rest/api/2/serverInfo ; cloud_v3: /rest/api/3/serverInfo
```
- **server_v2**: `GET /rest/api/2/issue/{KEY}?fields=...`, Beschreibung = Wiki-Markup-String
  (`text.jira_wiki_to_content`). Remote-Links `GET /rest/api/2/issue/{KEY}/remotelink`.
- **cloud_v3**: `/rest/api/3/...`, Beschreibung = ADF (`text.adf_to_content`).
- **Batch-Abruf** (`fetch_documents_bulk`, bis zu 100 Keys je Request):
  server_v2 `GET /rest/api/2/search?jql=key in (A-1, A-2)&fields=...&maxResults=100&validateQuery=false`,
  cloud_v3 `GET /rest/api/3/search/jql` mit Fallback auf das klassische `/rest/api/3/search`
  (bei 400/404/405/410/501). Keys, die JQL nicht liefert (verschoben/gelöscht), werden einzeln
  nachgeladen – dadurch bleibt die „moved key"-Erkennung (`uid` aus der Antwort) erhalten.
  Remote-Links bleiben
  Einzelrequests (`GET /issue/{key}/remotelink`), außer `include_remote_links=false`.
- Antwort-Key kann vom angefragten Key abweichen (verschoben) → uid aus Antwort-Key.
- Suchtext/body_text: `KEY + Summary + Beschreibung + Status + Typ + Labels + Components`
  (Section "" = Kopf mit Key/Summary/Status/Typ/Labels, danach Beschreibungsabschnitte).
- Kanten: issuelinks (siehe oben), parent/subtasks, Beschreibung-Links (edge_kind_for),
  Remote-Links (`object.url` → classify_url; Jira→Confluence `links_to_confluence`,
  source_field `"remotelink"`, anchor = object.title). Remote-Link-Fehler (403/404) nicht fatal.
- title = `"KEY: Summary"`, url = `jira_issue_url`, space_or_project = Projekt-Key,
  status_label = status.name, document_type = issuetype.name, source_updated_at = fields.updated.

## Modul `knowledge/indexer.py` (Agent „indexer“)

```python
class JobConflict(KnowledgeError): ...
class JobManager:
    def __init__(self, config_store: ConfigStore, db_path: str | Path,
                 client_factory: Callable[[dict, dict, threading.Event, RequestBudget, Callable], tuple] | None = None)
        # client_factory(cfg, params, stop_event, budget, on_event) -> (ConfluenceClient, JiraClient|None)
        # default builds RateLimitedClients from cfg (+ per-job sleep/timeout/retries from params).
        # On init: open db, init_db, mark_interrupted_jobs.
    def start(self, payload: dict) -> dict      # validate_job_params; ConfigError(400) on errors;
                                                # JobConflict(409) if a job is running/stopping;
                                                # start_page via links.parse_start_page (else ConfigError);
                                                # returns {"job_id": id, "state": "running"} immediately
    def stop(self, job_id: int) -> dict         # idempotent, returns status dict; KeyError if unknown
    def status(self, job_id: int | None = None) -> dict | None   # None = current or latest job
    def is_running(self) -> bool
    def shutdown(self, timeout: float = 10) -> None
```
Statusdict: alle crawl_jobs-Spalten + `params`, `elapsed_seconds`, `queue_size`, `retry_wait_seconds`
(oder null), `wait_source`, `phase` (`"request"`/`"rate_limit_wait"`/`"saving"`/`"idle"`),
`budget`: `{api_requests:{used,max}, confluence_pages:{used,max}, jira_issues:{used,max},
total_documents:{used,max}, duration_seconds:{used,max}}`, `log`: letzte 50 Logeinträge.
Crawler-Algorithmus exakt nach spec §10 (BFS, Budgets §10.3, refresh_existing, gespeicherte Kanten
wiederverwenden, Referenzknoten, `max_links_per_document` begrenzt nur das Einplanen,
`mark_document_state` für missing/forbidden/error, unresolved-Titel über
`ConfluenceClient.resolve_title` wenn `resolve_page_titles` → Kante zur echten Seite ergänzen,
Fehler eines Dokuments brechen den Job nicht ab; `CrawlStopped` → `stopped`; `BudgetExceeded` → Job endet
`completed` mit Meldung „Budget erschöpft …“; unerwartete Exception → `failed`). Fortschritt wird
mindestens nach jedem Dokument in crawl_jobs geschrieben.

Zusätzliche Start-Modi (alle abwärtskompatibel zu `start_page` / `space_key` / `refresh_only`):

- `space_keys: ["A", "B"]` → Space-Crawl über mehrere Spaces (`start_page_uid = "space:A,B"`),
  Seiten aller Spaces werden in einer Queue dedupliziert; ein nicht lesbarer Space wird geloggt
  und übersprungen, erst wenn kein einziger Space Seiten liefert, schlägt der Job fehl.
- `modified_since: "JJJJ-MM-TT"` (auch `7d`/`2w` oder ISO-Zeitstempel; wird auf ein Datum
  normalisiert) → Änderungs-Crawl (`start_page_uid = "recent:<Datum>"`): Seiten aus
  `ConfluenceClient.list_pages_modified_since` (optional gefiltert auf `space_keys`) werden
  eingereiht und **immer neu abgerufen**, auch wenn sie bereits indexiert sind. Ohne Treffer endet
  der Job erfolgreich mit Hinweis „Keine Seiten gefunden …“. Ausgehende Links werden normal
  weiterverfolgt (bereits indexierte Ziele bleiben wiederverwendet).
- Validierung: Space-Keys `^[A-Z0-9_~-]+$` (Großbuchstaben, dedupliziert), Datums-/Relativ-Format
  sonst 400. Für `space_keys` gilt `^[A-Za-z0-9_.~:@-]+$` (case-sensitiv, Personal Spaces) und
  Dedupe; ein einzelner `space_key` neben `modified_since` wird zum Filter `space_keys=[key]`.
- **Batch-Prefetch im BFS-Loop**: Vor dem Abarbeiten schaut der Worker bis zu
  `batch_fetch_size` (Default 100, Spec 1–100) noch nicht abgerufene Dokumente aus der Queue
  voraus (getrennt nach Typ, begrenzt durch die Rest-Budgets) und lädt Confluence- und
  Jira-Dokumente über `fetch_documents_bulk` in wenigen Requests. Clients ohne Bulk-API
  (z. B. Test-Fakes) fallen automatisch auf Einzelabrufe zurück; pro Batch wird eine
  Info-Logzeile „Batch-Vorabruf: N Dokument(e) in M Request(s)“ geschrieben.

## Module `knowledge/search.py`, `knowledge/graph.py`, `knowledge/mcp_server.py` (Agent „read“)

```python
def search(conn, query: str, *, type: str = "all", project: str | None = None,
           limit: int = 10, max_query_chars: int = 200) -> dict
    # -> {"query", "total_returned", "duration_ms", "results": [
    #      {"uid","type","title","url","project","snippet","snippet_parts":[{"text","hit"}],
    #       "updated_at","status","document_type"}]}
    # limit hard-capped at 10. type in all|confluence|jira (ValueError otherwise).
    # Safe FTS query: tokens = unicode word tokens (\w+, and issue keys kept as phrase "ABC-12"),
    # each quoted "tok" (double quotes escaped), joined with OR; prefix match for last token
    # if len>=3 ("tok"*). bm25(chunks_fts, 6.0, 3.0, 1.0); candidate limit 200 chunks;
    # aggregate best chunk per document; token coverage bonus; exact issue-key match in query
    # -> that jira doc ranked first; tie-breaker uid. Only state='indexed'. snippet() with
    # markers \x02/\x03 converted to snippet_parts. Empty query -> empty results.
def list_projects(conn) -> list[dict]   # [{"type","project","count"}] of indexed docs

def get_subgraph(conn, center: str | None, *, hops: int = 1, max_nodes: int = 150,
                 types: str = "all", edge_kinds: Sequence[str] | None = None) -> dict
    # BFS over edges in both directions from center (or, without center, the max_nodes nodes with
    # highest degree). Returns {"center","nodes":[{uid,type,title,url,state,project,document_type,
    # status,degree}],"edges":[{id,source,target,kind,relation_label,source_field,anchor_text,
    # evidence_url}],"truncated":bool,"hidden_nodes":int}. Deterministic order.
def get_document_detail(conn, uid: str, *, max_chars: int = 4000, neighbor_limit: int = 100) -> dict | None
    # document fields (without huge body: "text" truncated + "truncated" flag), labels list,
    # "outgoing": [{edge..., "target": node}], "incoming": [...]
def get_neighbors(conn, uid, *, direction: str = "both", limit: int = 50) -> dict | None
def find_related(conn, uid, *, limit: int = 10) -> dict | None
    # direct neighbours + documents sharing >=1 direct neighbour (score = shared count), excl. self
```
MCP (`knowledge/mcp_server.py`): `def run_stdio(db_path, cfg, stdin=sys.stdin, stdout=sys.stdout) -> None`
und `class McpServer(db_path, cfg)` mit `handle(message: dict) -> dict | None`.
Protokoll: JSON-RPC 2.0, eine Nachricht pro Zeile, unterstützte Protokollversionen
`["2025-06-18", "2025-03-26", "2024-11-05"]` (Client-Version übernehmen falls unterstützt, sonst
neueste). `initialize` → `{protocolVersion, capabilities:{tools:{listChanged:false}},
serverInfo:{name:"local-knowledge",version:"1.0.0"}, instructions}`; `notifications/*` → keine Antwort;
`ping` → `{}`; `tools/list`; `tools/call` → `{"content":[{"type":"text","text": json}],
"structuredContent": {...}, "isError": bool}`; unbekannte Methode -32601, Parse-Error -32700,
Invalid Request -32600, Invalid params -32602. DB **read-only** (`db.connect(path, readonly=True)`),
Verbindung lazy öffnen (DB existiert evtl. noch nicht → Toolfehler isError statt Absturz).
Sechs Tools wie spec §15.2 mit `inputSchema`; `search_knowledge` ruft `search.search` auf.

## Module `knowledge/webserver.py` + `app.py` (Agent „web“)

`app.py` Subcommands: `serve [--host --port --config]`, `mcp [--config]`, `init-db`, `status`.
`webserver.py`: `ThreadingHTTPServer`, `def create_server(config_store, job_manager, host, port)`
und `def serve(config_store)`. Pro Request-Thread eigene DB-Verbindung (thread-local).
Routen (alle JSON, Fehler als `{"error": {"code": "...", "message": "..."}}`):

| Methode | Route | Antwort |
|---|---|---|
| GET | `/`, `/index.html`, `/style.css`, `/app.js`, `/graph.js` | statisch aus `web/` (nur Whitelist/realpath-Check) |
| GET | `/api/config` | `{"defaults": cfg.crawl_defaults + confluence_sleep_seconds + jira_sleep_seconds, "limits": JOB_FIELD_SPECS als {name:{type,min,max}}, "search": cfg.search, "graph": cfg.graph, "configured": {"confluence": bool, "jira": bool}}` |
| GET | `/api/settings` | `ConfigStore.get_public()` |
| PUT | `/api/settings` | Patch → 200 neue public config; 400 bei ConfigError; 409 wenn Job läuft und Verbindungsdaten geändert werden sollen |
| POST | `/api/settings/test` | `{"confluence": {...}, "jira": {...}}` via `test_connection()` mit aktueller Config (eigenes Budget 5 Requests, eigenes stop_event) |
| GET | `/api/spaces?refresh=1` | `{"spaces": [{key,name,type,id,url}], "fetched_at", "cached"}` – alle sichtbaren Confluence-Spaces (Budget 400 Requests/300 s, 10 min In-Memory-Cache; `refresh=1` erzwingt neu laden, ein Fetch wird serialisiert (Single-Flight), Cache wird bei Änderungen an `confluence`/`http` verworfen). 400 ohne Basis-URL, 502 bei Upstream-Fehler, bei Fehler + Cache stattdessen stale Cache mit `"stale": true` und `"warning"` |
| POST | `/api/jobs` | 202 `{"job_id","state"}`; 400 / 409 |
| GET | `/api/jobs/current` | Status oder `{"job": null}`? → **immer** `{"job": status|null}` |
| GET | `/api/jobs/{id}` | `{"job": status}`; 404 |
| POST | `/api/jobs/{id}/stop` | `{"job": status}` |
| GET | `/api/search?q=&type=&project=` | search.search |
| GET | `/api/projects` | `{"projects": list_projects}` |
| GET | `/api/top-documents?types=&limit=&offset=&project=&q=` | `graph.get_top_documents(...)` (Rang nach ein-/ausgehenden Kanten; `types` = `all`/`confluence`/`jira`, 400 bei ungültigem Typ/Limit) |
| GET | `/api/documents/{uid}` | `{"document": get_document_detail}`; 404 (uid URL-decoded) |
| GET | `/api/graph?center=&hops=&max_nodes=&types=&edge_kinds=a,b` | get_subgraph (hops ≤ graph.max_hops, max_nodes ≤ graph.max_visible_nodes) |
| GET | `/api/stats` | db.get_stats |

Request-Body max 1 MB, nur `application/json`. Host-Header-Check gegen DNS-Rebinding (nur
`127.0.0.1:port`, `localhost:port`, konfigurierter Host). Für POST/PUT zusätzlich
`Origin`-Check (falls vorhanden muss er auf denselben Host zeigen). Security-Header:
`X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'self'; style-src 'self'; img-src 'self' data:`,
`Cache-Control: no-store` für API. Logging auf stderr.

## Frontend `web/index.html`, `web/style.css`, `web/app.js`, `web/graph.js` (Agent „frontend“)

Vanilla JS, keine CDNs, keine Inline-Scripts/-Styles (CSP!), alles via `textContent`.
Tabs: **Suche | Indexierung | Graph | Top-Dokumente | Einstellungen**, Hash-Routing (`#search`,
`#index`, `#graph?focus=jira:ENG-42`, `#top`, `#settings`); `?focus=` als Query wird ebenfalls unterstützt.
Startseiten-Werte: Page-ID/`confluence:<id>`, Confluence-Seiten-URL, `space:KEY` oder eine
Space-URL (`/display/KEY`, `/spaces/KEY`) – die Space-URL startet einen Space-Crawl (`space:KEY`).
Einstellungen-Tab: Confluence (Basis-URL, API-Modus server/cloud_v2, Aliase, Sleep, Auth-Typ,
Auswahl der zu indexierenden Spaces + Änderungs-Indexierung „seit Datum“, siehe `/api/spaces`),
Benutzername, Token-Passwortfeld mit „gesetzt“-Anzeige + „Token entfernen“),
Jira (dito + Projekt-Keys),
HTTP (Zertifikatsprüfung-Checkbox mit Hinweis „aus = für interne Zertifikate“, CA-Bundle,
System-Proxy, max. Antwortgröße, User-Agent), Crawl-Standardwerte, Index-, Such-, Graph-Parameter,
Server (Host/Port mit Hinweis „Neustart nötig“), Buttons „Speichern“ und „Verbindung testen“.
Details siehe Agent-Auftrag.
