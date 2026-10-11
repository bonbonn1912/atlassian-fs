"""HTTP Webserver and REST API for local-knowledge application."""
from __future__ import annotations

import gzip
import http.server
import json
import logging
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
from typing import Any
import urllib.parse

from . import config, db, models
from .config import ConfigStore

logger = logging.getLogger(__name__)

# Max request body: 1 MB
MAX_BODY_SIZE = 1024 * 1024

# Space listing cache: reuse results for 10 minutes unless ?refresh=1 is given.
SPACES_CACHE_TTL_SECONDS = 600.0
SPACES_MAX_REQUESTS = 400
SPACES_BUDGET_SECONDS = 300.0

# Static file mapping
STATIC_FILES: dict[str, str] = {
    "/": "index.html",
    "/index.html": "index.html",
    "/style.css": "style.css",
    "/app.js": "app.js",
    "/graph.js": "graph.js",
    "/graph-flight.js": "graph-flight.js",
    "/graph-flight-layout.js": "graph-flight-layout.js",
    "/graph-render-worker.js": "graph-render-worker.js",
    "/graph-layout-worker.js": "graph-layout-worker.js",
}

CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "application/javascript",
}

_thread_local = threading.local()


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    """Thread-local SQLite connection for read-only database queries."""
    conn = getattr(_thread_local, "conn", None)
    stored_path = getattr(_thread_local, "path", None)
    if conn is None or stored_path != str(db_path):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        conn = db.connect(db_path)
        _thread_local.conn = conn
        _thread_local.path = str(db_path)
    return conn


def close_thread_connection() -> None:
    """Close thread-local DB connection if open."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _thread_local.conn = None
        _thread_local.path = None


class KnowledgeServer(http.server.ThreadingHTTPServer):
    """Threading HTTP server with daemon threads and application references."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[http.server.BaseHTTPRequestHandler],
        config_store: ConfigStore,
        job_manager: Any,
        web_dir: Path,
        db_path: Path,
        confluence_factory: Any | None = None,
    ) -> None:
        self.config_store = config_store
        self.job_manager = job_manager
        self.web_dir = web_dir
        self.db_path = db_path
        # Optional test/plugin hook: factory(cfg) -> object with list_spaces()
        self.confluence_factory = confluence_factory
        self.spaces_cache: dict[str, Any] | None = None
        self.spaces_lock = threading.Lock()
        self.spaces_fetch_lock = threading.Lock()
        super().__init__(server_address, RequestHandlerClass)


class KnowledgeRequestHandler(http.server.BaseHTTPRequestHandler):
    """Request handler implementing security controls, static serving, and REST API."""

    server: KnowledgeServer

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - - [%s] " + format, self.address_string(), self.log_date_time_string(), *args)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            close_thread_connection()

    def _set_security_headers(self, is_json: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self' data:")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        # Static JS/CSS must never be cached so a server restart after an
        # update always delivers the current files.
        self.send_header("Cache-Control", "no-store")

    def send_json(self, status: int, data: Any) -> None:
        is_graph = urllib.parse.urlsplit(self.path).path == "/api/graph"
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":") if is_graph else None).encode("utf-8")
        compress = False
        if is_graph and len(body) >= 1024:
            for part in self.headers.get("Accept-Encoding", "").split(","):
                encoding, _, params = part.strip().partition(";")
                if encoding.lower() != "gzip":
                    continue
                try:
                    quality = float(params.strip().removeprefix("q=")) if params else 1
                except ValueError:
                    quality = 0
                compress = 0 < quality <= 1
            if compress:
                body = gzip.compress(body, compresslevel=1, mtime=0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if is_graph:
            self.send_header("Vary", "Accept-Encoding")
        if compress:
            self.send_header("Content-Encoding", "gzip")
        self._set_security_headers(is_json=True)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, code: str, message: str) -> None:
        self.send_json(status, {"error": {"code": code, "message": message}})

    def _check_host(self) -> bool:
        host_header = self.headers.get("Host", "").strip()
        if not host_header:
            return False
        if host_header.startswith("["):
            host_part = host_header.split("]")[0][1:].lower()
        else:
            host_part = host_header.split(":")[0].strip().lower()

        cfg = self.server.config_store.get()
        configured_host = (cfg.get("server", {}).get("host") or "").strip().lower()

        allowed = {"127.0.0.1", "localhost", "::1"}
        if configured_host:
            allowed.add(configured_host)
        server_addr = str(self.server.server_address[0]).lower()
        if server_addr and server_addr != "0.0.0.0":
            allowed.add(server_addr)

        return host_part in allowed

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlsplit(origin)
        origin_host = (parsed.hostname or "").strip().lower()
        if not origin_host:
            return False

        cfg = self.server.config_store.get()
        configured_host = (cfg.get("server", {}).get("host") or "").strip().lower()
        allowed = {"127.0.0.1", "localhost", "::1"}
        if configured_host:
            allowed.add(configured_host)
        server_addr = str(self.server.server_address[0]).lower()
        if server_addr and server_addr != "0.0.0.0":
            allowed.add(server_addr)

        if origin_host not in allowed:
            return False

        server_port = self.server.server_address[1]
        if parsed.port is not None and server_port not in (None, 0):
            if parsed.port != server_port:
                return False

        return True

    def _parse_json_body(self, allow_empty: bool = False) -> Any | None:
        cl_header = self.headers.get("Content-Length")
        if cl_header is None or cl_header.strip() == "":
            if allow_empty:
                return None
            self.send_error_json(400, "bad_request", "Content-Length Header erforderlich")
            return None

        try:
            content_length = int(cl_header)
        except ValueError:
            self.send_error_json(400, "bad_request", "Ungültiger Content-Length Header")
            return None

        if content_length < 0:
            self.send_error_json(400, "bad_request", "Ungültiger Content-Length Header")
            return None

        if content_length > MAX_BODY_SIZE:
            self.close_connection = True
            self.send_error_json(413, "payload_too_large", "Request-Body zu groß (max. 1 MB)")
            return None

        if content_length == 0:
            if allow_empty:
                return None
            self.send_error_json(400, "bad_request", "Request-Body darf nicht leer sein")
            return None

        ct = self.headers.get("Content-Type", "")
        media_type = ct.split(";")[0].strip().lower()
        if media_type != "application/json":
            self.send_error_json(400, "unsupported_media_type", "Content-Type muss application/json sein")
            return None

        raw_data = self.rfile.read(content_length)
        if len(raw_data) != content_length:
            self.send_error_json(400, "bad_request", "Unerwartetes Ende des Request-Body")
            return None

        try:
            return json.loads(raw_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_error_json(400, "bad_request", f"Ungültiges JSON: {exc}")
            return None

    def _handle_request(self, method: str) -> None:
        # 1. DNS Rebinding check
        if not self._check_host():
            self.send_error_json(403, "forbidden", "Ungültiger Host-Header")
            return

        # 2. Origin check for state-changing methods
        if method in ("POST", "PUT"):
            if not self._check_origin():
                self.send_error_json(403, "forbidden", "Ungültiger Origin-Header")
                return

        # 3. Path parsing
        raw_path = self.path
        parsed = urllib.parse.urlsplit(raw_path)
        path = parsed.path

        # 4. Route resolution
        route_handler, allowed_methods, extra_args = self._resolve_route(path)
        if route_handler is None:
            self.send_error_json(404, "not_found", "Route nicht gefunden")
            return

        if method not in allowed_methods:
            self.send_error_json(405, "method_not_allowed", "Methode nicht erlaubt")
            return

        # 5. Route dispatch with error mapping
        try:
            route_handler(method, parsed, *extra_args)
        except models.ConfigError as exc:
            self.send_error_json(400, "bad_request", str(exc))
        except Exception as exc:
            if exc.__class__.__name__ == "JobConflict":
                self.send_error_json(409, "job_conflict", str(exc))
            elif isinstance(exc, KeyError):
                self.send_error_json(404, "not_found", str(exc))
            elif isinstance(exc, ValueError):
                self.send_error_json(400, "bad_request", str(exc))
            else:
                logger.error("Unerwarteter Serverfehler bei %s %s: %s", method, path, exc, exc_info=True)
                self.send_error_json(500, "internal_error", "Interner Serverfehler")

    def _resolve_route(self, path: str) -> tuple[Any | None, set[str], list[Any]]:
        # Static files whitelist
        decoded_path = urllib.parse.unquote(path)
        if ".." not in decoded_path and ".." not in path:
            if path in STATIC_FILES:
                return self._handle_static, {"GET"}, [STATIC_FILES[path]]

        # API Routes
        if path == "/api/config":
            return self._handle_api_config, {"GET"}, []
        if path == "/api/settings":
            return self._handle_api_settings, {"GET", "PUT"}, []
        if path == "/api/settings/test":
            return self._handle_api_settings_test, {"POST"}, []
        if path == "/api/spaces":
            return self._handle_api_spaces, {"GET"}, []
        if path == "/api/jobs":
            return self._handle_api_jobs, {"POST"}, []
        if path == "/api/jobs/current":
            return self._handle_api_jobs_current, {"GET"}, []

        m_stop = re.fullmatch(r"/api/jobs/(\d+)/stop", path)
        if m_stop:
            return self._handle_api_job_stop, {"POST"}, [int(m_stop.group(1))]

        m_job = re.fullmatch(r"/api/jobs/(\d+)", path)
        if m_job:
            return self._handle_api_job_get, {"GET"}, [int(m_job.group(1))]

        if path == "/api/search":
            return self._handle_api_search, {"GET"}, []
        if path == "/api/projects":
            return self._handle_api_projects, {"GET"}, []

        if path.startswith("/api/documents/"):
            raw_uid = path[len("/api/documents/"):]
            if raw_uid:
                return self._handle_api_document_detail, {"GET"}, [raw_uid]

        if path == "/api/graph":
            return self._handle_api_graph, {"GET"}, []
        if path == "/api/top-documents":
            return self._handle_api_top_documents, {"GET"}, []
        if path == "/api/stats":
            return self._handle_api_stats, {"GET"}, []

        return None, set(), []

    # Route Handlers
    def _handle_static(self, method: str, parsed: urllib.parse.SplitResult, filename: str) -> None:
        web_dir_resolved = self.server.web_dir.resolve()
        file_path = (self.server.web_dir / filename).resolve()

        if not file_path.is_relative_to(web_dir_resolved) or not file_path.is_file():
            self.send_error_json(404, "not_found", "Datei nicht gefunden")
            return

        suffix = file_path.suffix.lower()
        content_type = CONTENT_TYPES.get(suffix, "application/octet-stream")
        content = file_path.read_bytes()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self._set_security_headers(is_json=False)
        self.end_headers()
        self.wfile.write(content)

    def _handle_api_config(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        cfg = self.server.config_store.get()
        defaults = dict(cfg.get("crawl_defaults", {}))
        defaults["confluence_sleep_seconds"] = cfg.get("confluence", {}).get("sleep_seconds", 1.0)
        defaults["jira_sleep_seconds"] = cfg.get("jira", {}).get("sleep_seconds", 1.0)

        limits: dict[str, Any] = {}
        for name, spec in config.JOB_FIELD_SPECS.items():
            lim: dict[str, Any] = {"type": spec[0]}
            if len(spec) >= 3:
                lim["min"] = spec[1]
                lim["max"] = spec[2]
            limits[name] = lim

        self.send_json(
            200,
            {
                "defaults": defaults,
                "limits": limits,
                "search": cfg.get("search", {}),
                "graph": cfg.get("graph", {}),
                "configured": {
                    "confluence": bool(cfg.get("confluence", {}).get("base_url")),
                    "jira": bool(cfg.get("jira", {}).get("base_url")),
                },
            },
        )

    def _handle_api_settings(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        if method == "GET":
            self.send_json(200, self.server.config_store.get_public())
            return

        # PUT
        data = self._parse_json_body(allow_empty=False)
        if data is None:
            return

        if not isinstance(data, dict):
            self.send_error_json(400, "bad_request", "Request-Body muss ein JSON-Objekt sein")
            return

        if self.server.job_manager.is_running():
            if any(k in data for k in ("confluence", "jira", "http")):
                self.send_error_json(
                    409,
                    "job_conflict",
                    "Während einer laufenden Indexierung können Verbindungseinstellungen nicht geändert werden",
                )
                return

        updated = self.server.config_store.update(data)
        # Connection settings changed -> a cached space list may be stale.
        # Selecting spaces (crawl defaults) must not invalidate it.
        conf_keys = set((data.get("confluence") or {}).keys())
        if conf_keys & {"base_url", "aliases", "api_mode", "auth"} or "http" in data:
            with self.server.spaces_lock:
                self.server.spaces_cache = None
        self.send_json(200, updated)

    def _handle_api_settings_test(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        if self.headers.get("Content-Length"):
            data = self._parse_json_body(allow_empty=True)
            if data is None and self.headers.get("Content-Length") != "0":
                return

        cfg = self.server.config_store.get()
        http_cfg = cfg.get("http", {})
        crawl_defaults = cfg.get("crawl_defaults", {})
        timeout = float(crawl_defaults.get("request_timeout_seconds", 20.0))

        from . import atlassian_http, links

        ssl_ctx = atlassian_http.make_ssl_context(
            http_cfg.get("verify_ssl", False),
            http_cfg.get("ca_bundle", ""),
        )
        transport = atlassian_http.default_transport(http_cfg.get("use_system_proxy", True))
        user_agent = http_cfg.get("user_agent", "local-knowledge/1.0")
        ctx = links.LinkContext.from_config(cfg)

        result: dict[str, Any] = {}

        # Confluence test
        conf_cfg = cfg.get("confluence", {})
        conf_base = (conf_cfg.get("base_url") or "").strip().rstrip("/")
        if not conf_base:
            result["confluence"] = {"ok": False, "message": "Nicht konfiguriert"}
        else:
            try:
                client = self._build_confluence_client(
                    cfg, max_requests=5, max_duration_seconds=60.0, max_retries=0
                )
                result["confluence"] = client.test_connection()
            except Exception as exc:
                logger.warning("Confluence test_connection Fehler: %s", exc, exc_info=True)
                result["confluence"] = {"ok": False, "message": str(exc)}

        # Jira test
        jira_cfg = cfg.get("jira", {})
        jira_base = (jira_cfg.get("base_url") or "").strip().rstrip("/")
        if not jira_base:
            result["jira"] = {"ok": False, "message": "Nicht konfiguriert"}
        else:
            try:
                from . import jira

                budget = atlassian_http.RequestBudget(5, 60.0)
                stop_ev = threading.Event()
                auth_headers = atlassian_http.build_auth_headers(jira_cfg.get("auth"))
                allowed_bases = [jira_base] + [
                    a.strip().rstrip("/") for a in jira_cfg.get("aliases", []) if a and isinstance(a, str) and a.strip()
                ]
                rl_client = atlassian_http.RateLimitedClient(
                    "jira",
                    jira_base,
                    allowed_bases=allowed_bases,
                    sleep_seconds=0.0,
                    timeout=timeout,
                    max_retries=0,
                    stop_event=stop_ev,
                    budget=budget,
                    ssl_context=ssl_ctx,
                    auth_headers=auth_headers,
                    user_agent=user_agent,
                    transport=transport,
                )
                client = jira.JiraClient(rl_client, jira_cfg.get("api_mode", "server_v2"), ctx)
                result["jira"] = client.test_connection()
            except Exception as exc:
                logger.warning("Jira test_connection Fehler: %s", exc, exc_info=True)
                result["jira"] = {"ok": False, "message": str(exc)}

        self.send_json(200, result)

    def _build_confluence_client(
        self,
        cfg: dict,
        *,
        max_requests: int,
        max_duration_seconds: float,
        max_retries: int | None = None,
    ) -> Any:
        """Short-lived ConfluenceClient with its own budget and stop event."""
        from . import atlassian_http, confluence, links

        http_cfg = cfg.get("http", {})
        crawl_defaults = cfg.get("crawl_defaults", {})
        conf_cfg = cfg.get("confluence", {})
        base = (conf_cfg.get("base_url") or "").strip().rstrip("/")
        allowed_bases = [base] + [
            a.strip().rstrip("/")
            for a in conf_cfg.get("aliases", [])
            if isinstance(a, str) and a.strip()
        ]
        rl_client = atlassian_http.RateLimitedClient(
            "confluence",
            base,
            allowed_bases=allowed_bases,
            sleep_seconds=0.0,
            timeout=float(crawl_defaults.get("request_timeout_seconds", 20.0)),
            max_retries=(
                int(crawl_defaults.get("max_retries", 4)) if max_retries is None else max_retries
            ),
            stop_event=threading.Event(),
            budget=atlassian_http.RequestBudget(max_requests, max_duration_seconds),
            ssl_context=atlassian_http.make_ssl_context(
                http_cfg.get("verify_ssl", False), http_cfg.get("ca_bundle", "")
            ),
            auth_headers=atlassian_http.build_auth_headers(conf_cfg.get("auth")),
            user_agent=http_cfg.get("user_agent", "local-knowledge/1.0"),
            transport=atlassian_http.default_transport(http_cfg.get("use_system_proxy", True)),
        )
        return confluence.ConfluenceClient(
            rl_client, conf_cfg.get("api_mode", "server"), links.LinkContext.from_config(cfg)
        )

    def _handle_api_spaces(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        """GET /api/spaces[?refresh=1] – list all visible Confluence spaces."""
        params = urllib.parse.parse_qs(parsed.query)
        force = params.get("refresh", ["0"])[0].strip().lower() in ("1", "true", "yes")

        cfg = self.server.config_store.get()
        conf_base = (cfg.get("confluence", {}).get("base_url") or "").strip()
        if not conf_base:
            self.send_error_json(
                400, "bad_request", "Confluence-Basis-URL ist nicht konfiguriert (Einstellungen)"
            )
            return

        def fresh_cache() -> dict[str, Any] | None:
            cached = self.server.spaces_cache
            if not cached or force:
                return None
            age = time.time() - float(cached.get("fetched_at_ts", 0.0))
            return cached if age < SPACES_CACHE_TTL_SECONDS else None

        cached = fresh_cache()
        if cached:
            self.send_json(200, {
                "spaces": cached["spaces"],
                "fetched_at": cached["fetched_at"],
                "cached": True,
            })
            return

        # Serialize concurrent fetches: the second request reuses the fresh cache.
        with self.server.spaces_fetch_lock:
            cached = fresh_cache()
            if cached:
                self.send_json(200, {
                    "spaces": cached["spaces"],
                    "fetched_at": cached["fetched_at"],
                    "cached": True,
                })
                return
            stale = self.server.spaces_cache
            try:
                factory = self.server.confluence_factory
                if factory is not None:
                    client = factory(cfg)
                else:
                    client = self._build_confluence_client(
                        cfg, max_requests=SPACES_MAX_REQUESTS,
                        max_duration_seconds=SPACES_BUDGET_SECONDS,
                    )
                spaces = client.list_spaces()
            except Exception as exc:
                logger.warning("Spaces konnten nicht geladen werden: %s", exc, exc_info=True)
                if stale:
                    self.send_json(200, {
                        "spaces": stale["spaces"],
                        "fetched_at": stale["fetched_at"],
                        "cached": True,
                        "stale": True,
                        "warning": str(exc),
                    })
                    return
                self.send_error_json(
                    502, "upstream_error", f"Spaces konnten nicht geladen werden: {exc}"
                )
                return

            fetched_at = db.now_iso()
            with self.server.spaces_lock:
                self.server.spaces_cache = {
                    "spaces": spaces,
                    "fetched_at": fetched_at,
                    "fetched_at_ts": time.time(),
                }
        self.send_json(200, {"spaces": spaces, "fetched_at": fetched_at, "cached": False})

    def _handle_api_jobs(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        data = self._parse_json_body(allow_empty=False)
        if data is None:
            return

        if not isinstance(data, dict):
            self.send_error_json(400, "bad_request", "Request-Body muss ein JSON-Objekt sein")
            return

        res = self.server.job_manager.start(data)
        self.send_json(202, res)

    def _handle_api_jobs_current(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        st = self.server.job_manager.status(None)
        self.send_json(200, {"job": st})

    def _handle_api_job_get(self, method: str, parsed: urllib.parse.SplitResult, job_id: int) -> None:
        st = self.server.job_manager.status(job_id)
        if st is None:
            self.send_error_json(404, "not_found", f"Job {job_id} nicht gefunden")
            return
        self.send_json(200, {"job": st})

    def _handle_api_job_stop(self, method: str, parsed: urllib.parse.SplitResult, job_id: int) -> None:
        if self.headers.get("Content-Length"):
            data = self._parse_json_body(allow_empty=True)
            if data is None and self.headers.get("Content-Length") != "0":
                return

        st = self.server.job_manager.stop(job_id)
        self.send_json(200, {"job": st})

    def _handle_api_search(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        q = params.get("q", [""])[0]
        type_param = params.get("type", ["all"])[0]
        project_param = params.get("project", [None])[0]
        if project_param == "":
            project_param = None

        from . import search

        conn = get_db_connection(self.server.db_path)
        search_cfg = self.server.config_store.get().get("search", {})
        res = search.search(conn, q, type=type_param, project=project_param,
                            limit=int(search_cfg.get("max_results", 10)),
                            max_query_chars=int(search_cfg.get("max_query_chars", 200)))
        self.send_json(200, res)

    def _handle_api_projects(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        from . import search

        conn = get_db_connection(self.server.db_path)
        projects = search.list_projects(conn)
        self.send_json(200, {"projects": projects})

    def _handle_api_document_detail(self, method: str, parsed: urllib.parse.SplitResult, raw_uid: str) -> None:
        uid = urllib.parse.unquote(raw_uid)
        from . import graph

        conn = get_db_connection(self.server.db_path)
        doc = graph.get_document_detail(conn, uid)
        if doc is None:
            self.send_error_json(404, "not_found", f"Dokument '{uid}' nicht gefunden")
            return

        self.send_json(200, {"document": doc})

    def _handle_api_graph(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        center = params.get("center", [None])[0]
        if center == "":
            center = None

        cfg = self.server.config_store.get()
        graph_cfg = cfg.get("graph", {})
        default_hops = int(graph_cfg.get("default_hops", 1))
        max_hops = int(graph_cfg.get("max_hops", 3))

        hops = default_hops
        if "hops" in params:
            try:
                hops = int(params["hops"][0])
            except ValueError:
                self.send_error_json(400, "bad_request", "hops muss eine Ganzzahl sein")
                return
        if hops < 0:
            self.send_error_json(400, "bad_request", "hops muss >= 0 sein")
            return
        hops = min(hops, max_hops)

        max_visible_nodes = int(graph_cfg.get("max_visible_nodes", 150))
        max_nodes = max_visible_nodes
        if "max_nodes" in params:
            try:
                max_nodes = int(params["max_nodes"][0])
            except ValueError:
                self.send_error_json(400, "bad_request", "max_nodes muss eine Ganzzahl sein")
                return
        if max_nodes < 1:
            self.send_error_json(400, "bad_request", "max_nodes muss >= 1 sein")
            return
        max_nodes = min(max_nodes, max_visible_nodes)

        types_param = params.get("types", ["all"])[0] or "all"
        edge_kinds_raw = params.get("edge_kinds", [None])[0]
        edge_kinds = [k.strip() for k in edge_kinds_raw.split(",") if k.strip()] if edge_kinds_raw else None

        from . import graph

        conn = get_db_connection(self.server.db_path)
        subgraph = graph.get_subgraph(
            conn,
            center,
            hops=hops,
            max_nodes=max_nodes,
            types=types_param,
            edge_kinds=edge_kinds,
        )
        self.send_json(200, subgraph)

    def _handle_api_top_documents(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        limit = 50
        offset = 0
        types_param = "all"
        project_param = None
        q_param = None

        if "limit" in params:
            try:
                limit = int(params["limit"][0])
            except ValueError:
                self.send_error_json(400, "bad_request", "limit muss eine Ganzzahl sein")
                return

        if "offset" in params:
            try:
                offset = int(params["offset"][0])
            except ValueError:
                self.send_error_json(400, "bad_request", "offset muss eine Ganzzahl sein")
                return

        if "types" in params and params["types"][0]:
            types_param = params["types"][0]
            if types_param not in ("all", "confluence", "jira"):
                self.send_error_json(400, "bad_request", f"Ungültiger Typ: {types_param}")
                return

        if "project" in params and params["project"][0]:
            project_param = params["project"][0].strip() or None

        if "q" in params and params["q"][0]:
            q_param = params["q"][0].strip() or None

        from . import graph

        conn = get_db_connection(self.server.db_path)
        data = graph.get_top_documents(
            conn,
            limit=limit,
            offset=offset,
            types=types_param,
            project=project_param,
            query=q_param,
        )
        self.send_json(200, data)

    def _handle_api_stats(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        conn = get_db_connection(self.server.db_path)
        stats = db.get_stats(conn)
        self.send_json(200, stats)

    # HTTP Methods dispatch
    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_PUT(self) -> None:
        self._handle_request("PUT")

    def do_DELETE(self) -> None:
        self._handle_request("DELETE")

    def do_PATCH(self) -> None:
        self._handle_request("PATCH")

    def do_HEAD(self) -> None:
        self._handle_request("HEAD")


def create_server(
    config_store: ConfigStore,
    job_manager: Any,
    host: str,
    port: int,
    web_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    confluence_factory: Any | None = None,
) -> KnowledgeServer:
    """Create and return a configured ThreadingHTTPServer instance."""
    web_path = Path(web_dir) if web_dir is not None else (config.PROJECT_ROOT / "web")
    database_path = Path(db_path) if db_path is not None else config.resolve_db_path(config_store.get())
    return KnowledgeServer(
        (host, port), KnowledgeRequestHandler, config_store, job_manager, web_path, database_path,
        confluence_factory=confluence_factory,
    )


class _IPv6KnowledgeServer(KnowledgeServer):
    address_family = socket.AF_INET6


def create_ipv6_loopback_server(config_store: ConfigStore, job_manager: Any, port: int,
                                db_path: Path) -> KnowledgeServer:
    """Second listener on [::1] sharing the same job manager and config."""
    return _IPv6KnowledgeServer(("::1", port), KnowledgeRequestHandler, config_store, job_manager,
                                config.PROJECT_ROOT / "web", db_path)


def serve(
    config_store: ConfigStore,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Start webserver and background job manager, running until interrupted."""
    cfg = config_store.get()
    effective_host = host if host is not None else cfg["server"]["host"]
    effective_port = port if port is not None else cfg["server"]["port"]
    db_path = config.resolve_db_path(cfg)

    # Ensure DB is initialized before server start
    db.open_db(db_path)

    from .indexer import JobManager

    job_manager = JobManager(config_store, db_path)
    server = create_server(config_store, job_manager, effective_host, effective_port, db_path=db_path)
    actual_port = server.server_address[1]
    logger.info("Server gestartet: http://%s:%d  (Beenden mit Strg+C)", effective_host, actual_port)
    if effective_host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning("Server ist nicht nur lokal erreichbar (host=%s) – es gibt keine Authentifizierung!",
                       effective_host)

    # "localhost" usually resolves to ::1 first; also listen on IPv6 loopback so
    # browsers do not report ERR_CONNECTION_REFUSED.
    servers = [server]
    if effective_host in ("127.0.0.1", "localhost"):
        try:
            server6 = create_ipv6_loopback_server(config_store, job_manager, actual_port, db_path)
        except OSError as exc:
            logger.debug("IPv6-Loopback nicht verfügbar: %s", exc)
        else:
            servers.append(server6)
            threading.Thread(target=server6.serve_forever, name="http-ipv6", daemon=True).start()
            logger.info("Zusätzlich erreichbar: http://localhost:%d  (http://[::1]:%d)", actual_port, actual_port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server wird beendet...")
    finally:
        for srv in servers[1:]:
            srv.shutdown()
            srv.server_close()
        server.server_close()
        job_manager.shutdown()
        close_thread_connection()
