"""JobManager and BFS crawler implementation for Confluence and Jira knowledge indexing.

Provides thread-safe job management, live status merging, and depth/budget-limited
crawling according to CONTRACTS.md and implementation.md (§5, §9, §10, §11, §17).
"""

from __future__ import annotations

import collections
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable

from . import db
from .config import ConfigStore, validate_job_params
from .models import (
    AtlassianError,
    BudgetExceeded,
    Chunk,
    ConfigError,
    CrawlStopped,
    EDGE_JIRA_PARENT_OF,
    EDGE_JIRA_RELATES_TO,
    EDGE_LINKS_TO,
    EDGE_LINKS_TO_CONFLUENCE,
    EDGE_MENTIONS_JIRA,
    EDGE_PARENT_OF,
    FetchedDocument,
    ForbiddenError,
    InvalidResponseError,
    JOB_ACTIVE_STATES,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_RUNNING,
    JOB_STOPPED,
    JOB_STOPPING,
    KnowledgeError,
    LinkTarget,
    NotFoundError,
    STATE_ERROR,
    STATE_FORBIDDEN,
    STATE_INDEXED,
    STATE_MISSING,
    STATE_SKIPPED,
    confluence_uid,
    is_unresolved_uid,
    jira_uid,
    parse_uid,
    split_unresolved_uid,
)

logger = logging.getLogger(__name__)


class JobConflict(KnowledgeError):
    """Raised when a job is started while another job is running or stopping."""


from .atlassian_http import RequestBudget


def _default_client_factory(
    cfg: dict,
    params: dict,
    stop_event: threading.Event,
    budget: RequestBudget,
    on_event: Callable[[dict], None],
) -> tuple[Any, Any | None]:
    """Default client factory creating ConfluenceClient and optional JiraClient.

    Lazily imports atlassian_http, confluence, jira, and links modules.
    """
    from . import atlassian_http, confluence, jira, links

    http_cfg = cfg.get("http") or {}
    verify_ssl = bool(http_cfg.get("verify_ssl", False))
    ca_bundle = str(http_cfg.get("ca_bundle", "") or "")
    use_system_proxy = bool(http_cfg.get("use_system_proxy", True))
    user_agent = str(http_cfg.get("user_agent", "local-knowledge/1.0"))
    max_response_mb = int(http_cfg.get("max_response_mb", 20))
    max_response_bytes = max_response_mb * 1024 * 1024

    ssl_context = atlassian_http.make_ssl_context(verify_ssl, ca_bundle)
    transport = atlassian_http.default_transport(use_system_proxy)

    ctx = links.LinkContext.from_config(cfg)
    index_cfg = cfg.get("index") or {}

    timeout = float(params.get("request_timeout_seconds", 20.0))
    max_retries = int(params.get("max_retries", 4))

    # Confluence
    conf_cfg = cfg.get("confluence") or {}
    conf_base = (conf_cfg.get("base_url") or "").strip().rstrip("/")
    conf_aliases = [conf_base] + [
        a.strip().rstrip("/")
        for a in conf_cfg.get("aliases") or []
        if isinstance(a, str) and a.strip()
    ]
    conf_sleep = float(
        params.get("confluence_sleep_seconds", conf_cfg.get("sleep_seconds", 1.0))
    )
    conf_auth = atlassian_http.build_auth_headers(conf_cfg.get("auth"))

    conf_http = atlassian_http.RateLimitedClient(
        source="confluence",
        base_url=conf_base,
        allowed_bases=conf_aliases,
        sleep_seconds=conf_sleep,
        timeout=timeout,
        max_retries=max_retries,
        stop_event=stop_event,
        budget=budget,
        ssl_context=ssl_context,
        auth_headers=conf_auth,
        user_agent=user_agent,
        max_response_bytes=max_response_bytes,
        transport=transport,
        on_event=on_event,
    )
    confluence_client = confluence.ConfluenceClient(
        conf_http,
        conf_cfg.get("api_mode", "server"),
        ctx,
        index_cfg=index_cfg,
    )

    # Jira
    jira_cfg = cfg.get("jira") or {}
    jira_base = (jira_cfg.get("base_url") or "").strip().rstrip("/")
    jira_client = None
    if jira_base:
        jira_aliases = [jira_base] + [
            a.strip().rstrip("/")
            for a in jira_cfg.get("aliases") or []
            if isinstance(a, str) and a.strip()
        ]
        jira_sleep = float(
            params.get("jira_sleep_seconds", jira_cfg.get("sleep_seconds", 1.0))
        )
        jira_auth = atlassian_http.build_auth_headers(jira_cfg.get("auth"))

        jira_http = atlassian_http.RateLimitedClient(
            source="jira",
            base_url=jira_base,
            allowed_bases=jira_aliases,
            sleep_seconds=jira_sleep,
            timeout=timeout,
            max_retries=max_retries,
            stop_event=stop_event,
            budget=budget,
            ssl_context=ssl_context,
            auth_headers=jira_auth,
            user_agent=user_agent,
            max_response_bytes=max_response_bytes,
            transport=transport,
            on_event=on_event,
        )
        jira_client = jira.JiraClient(
            jira_http,
            jira_cfg.get("api_mode", "server_v2"),
            ctx,
            index_cfg=index_cfg,
        )

    return confluence_client, jira_client


class JobManager:
    """Manages crawl job lifecycles and background worker execution."""

    def __init__(
        self,
        config_store: ConfigStore,
        db_path: str | Path,
        client_factory: (
            Callable[
                [dict, dict, threading.Event, RequestBudget, Callable[[dict], None]],
                tuple[Any, Any | None],
            ]
            | None
        ) = None,
    ) -> None:
        self.config_store = config_store
        self.db_path = Path(db_path)
        self.client_factory = client_factory

        self._lock = threading.RLock()
        self._live_lock = threading.RLock()
        self._worker_thread: threading.Thread | None = None
        self._current_job_id: int | None = None
        self._stop_event: threading.Event | None = None

        self._live: dict[str, Any] = {
            "phase": "idle",
            "retry_wait_seconds": None,
            "wait_source": None,
            "current_uid": None,
            "queue_size": 0,
            "pages_indexed": 0,
            "jira_indexed": 0,
            "api_requests": 0,
            "skipped": 0,
            "errors": 0,
            "reused_documents": 0,
            "last_message": None,
        }

        # Initialize DB and mark any leftover active jobs as interrupted
        conn = db.open_db(self.db_path)
        try:
            db.mark_interrupted_jobs(conn)
        finally:
            conn.close()

    def is_running(self) -> bool:
        """Return True if a crawl worker is currently active."""
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return True
        return False

    def start(self, payload: dict) -> dict:
        """Validate parameters and start a new crawl job in a background worker."""
        cfg = self.config_store.get()
        params, errors = validate_job_params(payload, cfg)
        if errors:
            raise ConfigError("; ".join(errors))

        with self._lock:
            if self.is_running():
                raise JobConflict("Ein Crawl-Job läuft bereits oder wird gerade gestoppt.")

            # Also check DB for running/stopping jobs
            conn = db.connect(self.db_path)
            try:
                latest = db.get_latest_job(conn)
                if latest and latest.get("state") in JOB_ACTIVE_STATES:
                    raise JobConflict(
                        "Ein Crawl-Job läuft bereits oder wird gerade gestoppt."
                    )
                if params.get("refresh_only"):
                    existing_uids = db.get_existing_document_uids(conn)
                    if not existing_uids:
                        raise ConfigError(
                            "Keine bestehenden Dokumente in der Datenbank vorhanden, die aktualisiert werden können."
                        )
                    existing_count = len(existing_uids)
                else:
                    existing_count = 1
            finally:
                conn.close()

            if params.get("refresh_only"):
                start_page_uid = "refresh:existing"
            elif params.get("modified_since"):
                start_page_uid = f"recent:{params['modified_since']}"
            elif params.get("space_keys"):
                start_page_uid = "space:" + ",".join(params["space_keys"])
            elif params.get("space_key") and not params.get("start_page"):
                start_page_uid = f"space:{params['space_key']}"
            else:
                # Normalize start_page using links.parse_start_page
                raw_start_page = str(params.get("start_page", ""))
                if raw_start_page.startswith("space:"):
                    # Case is preserved (personal space keys are case-sensitive).
                    start_page_uid = f"space:{raw_start_page.split(':', 1)[1].strip()}"
                else:
                    try:
                        from .links import LinkContext, parse_start_page

                        ctx = LinkContext.from_config(cfg)
                        start_page_uid = parse_start_page(raw_start_page, ctx)
                    except Exception as exc:
                        raise ConfigError(f"Fehler beim Parsen der Startseite: {exc}") from exc

                if not start_page_uid:
                    raise ConfigError(f"Ungültige Startseite: {raw_start_page!r}")

            # Create job record in DB
            conn = db.connect(self.db_path)
            try:
                job_id = db.create_job(conn, params, start_page_uid)
            finally:
                conn.close()

            self._current_job_id = job_id
            self._stop_event = threading.Event()

            with self._live_lock:
                self._live = {
                    "phase": "idle",
                    "retry_wait_seconds": None,
                    "wait_source": None,
                    "current_uid": start_page_uid,
                    "queue_size": existing_count,
                    "pages_indexed": 0,
                    "jira_indexed": 0,
                    "api_requests": 0,
                    "skipped": 0,
                    "errors": 0,
                    "reused_documents": 0,
                    "last_message": "Gestartet (Aktualisierung bestehender Dokumente)"
                    if params.get("refresh_only")
                    else (
                        "Gestartet (Aktualisierung geänderter Seiten)"
                        if start_page_uid.startswith("recent:")
                        else ("Gestartet (Space-Crawl)" if start_page_uid.startswith("space:") else "Gestartet")
                    ),
                }

            self._worker_thread = threading.Thread(
                target=self._run_worker,
                args=(job_id, params, start_page_uid, self._stop_event),
                name=f"CrawlWorker-{job_id}",
                daemon=True,
            )
            self._worker_thread.start()

            return {"job_id": job_id, "state": JOB_RUNNING}

    def stop(self, job_id: int) -> dict:
        """Signal a running job to stop. Idempotent. Raises KeyError if unknown."""
        conn = db.connect(self.db_path)
        try:
            job = db.get_job(conn, job_id)
            if job is None:
                raise KeyError(f"Job {job_id} existiert nicht")

            if job.get("state") == JOB_RUNNING:
                db.update_job(
                    conn,
                    job_id,
                    state=JOB_STOPPING,
                    last_message="Wird gestoppt...",
                )

            with self._lock:
                if self._current_job_id == job_id and self._stop_event is not None:
                    self._stop_event.set()
        finally:
            conn.close()

        status_dict = self.status(job_id)
        if status_dict is None:
            raise KeyError(f"Job {job_id} existiert nicht")
        return status_dict

    def status(self, job_id: int | None = None) -> dict | None:
        """Return merged status dictionary including DB row and in-memory live fields."""
        conn = db.connect(self.db_path)
        try:
            if job_id is None:
                if self._current_job_id is not None:
                    job_id = self._current_job_id
                else:
                    latest = db.get_latest_job(conn)
                    if latest is None:
                        return None
                    job_id = int(latest["id"])

            job = db.get_job(conn, job_id)
            if job is None:
                return None

            is_active_target = (
                self._current_job_id == job_id
                and self._worker_thread is not None
                and self._worker_thread.is_alive()
            )

            if is_active_target:
                with self._live_lock:
                    for k in (
                        "phase",
                        "retry_wait_seconds",
                        "wait_source",
                        "current_uid",
                        "queue_size",
                        "pages_indexed",
                        "jira_indexed",
                        "api_requests",
                        "skipped",
                        "errors",
                        "reused_documents",
                    ):
                        val = self._live.get(k)
                        if val is not None:
                            job[k] = val
                    if self._live.get("last_message"):
                        job["last_message"] = self._live["last_message"]
            else:
                job.setdefault("phase", "idle")
                job.setdefault("retry_wait_seconds", None)
                job.setdefault("wait_source", None)
                job.setdefault("reused_documents", 0)
                counts = db.queue_counts(conn, job_id)
                job["queue_size"] = counts.get("pending", 0)

            duration_seconds = 0
            started_at_str = job.get("started_at")
            if started_at_str:
                try:
                    s_dt = datetime.fromisoformat(started_at_str)
                    if job.get("finished_at"):
                        f_dt = datetime.fromisoformat(job["finished_at"])
                    else:
                        f_dt = datetime.now(timezone.utc)
                    duration_seconds = max(0, int((f_dt - s_dt).total_seconds()))
                except Exception:
                    duration_seconds = 0
            job["duration_seconds"] = duration_seconds

            return job
        finally:
            conn.close()

    def logs(self, job_id: int | None = None, limit: int = 100) -> list[dict]:
        """Return log entries for a job."""
        conn = db.connect(self.db_path)
        try:
            if job_id is None:
                if self._current_job_id is not None:
                    job_id = self._current_job_id
                else:
                    latest = db.get_latest_job(conn)
                    if latest is None:
                        return []
                    job_id = int(latest["id"])
            return db.get_job_log(conn, job_id, limit=limit)
        finally:
            conn.close()

    def _run_worker(
        self,
        job_id: int,
        params: dict,
        start_page_uid: str,
        stop_event: threading.Event,
    ) -> None:
        """Main crawling BFS worker executing on a background thread."""
        logger.info("Starte Crawl-Job %d (start=%s)", job_id, start_page_uid)
        conn = db.connect(self.db_path)
        try:
            cfg = self.config_store.get()
            max_requests = int(params.get("max_api_requests", 1000))
            max_duration_minutes = float(params.get("max_duration_minutes", 60))
            max_duration_seconds = max_duration_minutes * 60.0
            budget = RequestBudget(max_requests=max_requests, max_duration_seconds=max_duration_seconds)

            def on_client_event(ev: dict) -> None:
                ev_type = ev.get("type")
                with self._live_lock:
                    if ev_type == "wait_sleep":
                        self._live["phase"] = "sleep"
                        self._live["wait_source"] = ev.get("source")
                    elif ev_type == "wait_retry":
                        self._live["phase"] = "retry_wait"
                        self._live["retry_wait_seconds"] = ev.get("wait_seconds")
                        self._live["wait_source"] = ev.get("source")
                    elif ev_type == "request_start":
                        self._live["phase"] = "request"
                        self._live["retry_wait_seconds"] = None
                        self._live["wait_source"] = None
                    elif ev_type == "request_done":
                        self._live["phase"] = "idle"
                        self._live["retry_wait_seconds"] = None
                        self._live["wait_source"] = None

                db_conn = db.connect(self.db_path)
                try:
                    if ev_type == "retry":
                        db.add_job_log(
                            db_conn,
                            job_id,
                            "warning",
                            f"HTTP {ev.get('status')} für {ev.get('url')} - Versuch "
                            f"{ev.get('attempt')}/{ev.get('max_retries')} in {ev.get('wait_seconds'):.1f}s",
                            self._live.get("current_uid"),
                        )
                    elif ev_type == "request_done" and ev.get("status") == 404:
                        db.add_job_log(
                            db_conn,
                            job_id,
                            "warning",
                            f"HTTP 404 Nicht gefunden: {ev.get('url')}",
                            self._live.get("current_uid"),
                        )
                finally:
                    db_conn.close()

            factory = self.client_factory or _default_client_factory
            try:
                confluence_client, jira_client = factory(
                    cfg, params, stop_event, budget, on_client_event
                )
            except Exception as exc:
                logger.exception("Fehler beim Initialisieren der Clients für Job %d", job_id)
                db.update_job(
                    conn,
                    job_id,
                    state=JOB_FAILED,
                    finished_at=db.now_iso(),
                    last_message=f"Client-Fehler: {exc}",
                )
                db.add_job_log(conn, job_id, "error", f"Client-Fehler: {exc}")
                with self._live_lock:
                    self._live["phase"] = "idle"
                    self._live["last_message"] = f"Client-Fehler: {exc}"
                return

            resolved_titles_cache: dict[tuple[str, str], str | None] = {}
            initial_uid = start_page_uid

            def fail_job(message: str, log_level: str = "error") -> None:
                db.update_job(
                    conn,
                    job_id,
                    state=JOB_FAILED,
                    finished_at=db.now_iso(),
                    last_message=message,
                )
                db.add_job_log(conn, job_id, log_level, message)
                with self._live_lock:
                    self._live["phase"] = "idle"
                    self._live["last_message"] = message

            def finish_job(state: str, message: str) -> None:
                """Terminal state before the BFS loop started (client/stop/budget)."""
                db.update_job(
                    conn,
                    job_id,
                    state=state,
                    finished_at=db.now_iso(),
                    last_message=message,
                )
                db.add_job_log(conn, job_id, "info", f"Job beendet mit Status '{state}': {message}")
                with self._live_lock:
                    self._live["phase"] = "idle"
                    self._live["last_message"] = message

            # Start page resolution if unresolved title UID
            if is_unresolved_uid(initial_uid):
                space, title = split_unresolved_uid(initial_uid)
                resolved_page_id: str | None = None
                try:
                    if confluence_client is None:
                        raise ConfigError(
                            "Confluence-Client nicht verfügbar zur Titelauflösung"
                        )
                    resolved_page_id = confluence_client.resolve_title(space, title)
                except CrawlStopped:
                    finish_job(JOB_STOPPED, "Gestoppt")
                    return
                except BudgetExceeded as exc:
                    finish_job(JOB_COMPLETED, f"Budget erschöpft: {exc}")
                    return
                except Exception as exc:
                    logger.warning(
                        "Fehler bei Titelauflösung der Startseite %s: %s",
                        initial_uid,
                        exc,
                    )
                    resolved_page_id = None

                if not resolved_page_id:
                    err_msg = f"Startseite konnte nicht aufgelöst werden: {initial_uid}"
                    db.update_job(
                        conn,
                        job_id,
                        state=JOB_FAILED,
                        finished_at=db.now_iso(),
                        last_message=err_msg,
                    )
                    db.add_job_log(conn, job_id, "error", err_msg)
                    with self._live_lock:
                        self._live["phase"] = "idle"
                        self._live["last_message"] = err_msg
                    return

                resolved_titles_cache[(space, title)] = resolved_page_id
                initial_uid = confluence_uid(resolved_page_id)
                db.update_job(conn, job_id, start_page_uid=initial_uid)

            # BFS queue and deduplication tracking
            force_refresh_uids: set[str] = set()
            # Parent/child edges derived from the space listing (expand=ancestors):
            # saves one child request per page during space crawls.
            derived_child_links: dict[str, list[LinkTarget]] = {}
            space_page_uids: set[str] = set()
            final_message_override: str | None = None

            if initial_uid.startswith("recent:"):
                since = initial_uid.split(":", 1)[1].strip()
                selected_spaces = [
                    str(k).strip() for k in (params.get("space_keys") or []) if str(k).strip()
                ]
                scope_label = ("Spaces " + ", ".join(selected_spaces)) if selected_spaces else "alle Spaces"
                logger.info("Änderungs-Crawl für %s seit %s gestartet", scope_label, since)
                with self._live_lock:
                    self._live["last_message"] = (
                        f"Ermittle Seiten, die seit {since} geändert wurden ({scope_label})..."
                    )

                if confluence_client is None:
                    fail_job(f"Confluence-Client nicht verfügbar für Änderungs-Crawl (seit {since})")
                    return

                try:
                    max_recent = min(
                        int(params.get("max_confluence_pages", 100_000)),
                        int(params.get("max_total_documents", 100_000)),
                    )
                    recent_pages = confluence_client.list_pages_modified_since(
                        since, space_keys=selected_spaces, max_items=max_recent
                    )
                except CrawlStopped:
                    finish_job(JOB_STOPPED, "Gestoppt")
                    return
                except BudgetExceeded as exc:
                    finish_job(JOB_COMPLETED, f"Budget erschöpft: {exc}")
                    return
                except Exception as exc:
                    logger.error("Fehler beim Ermitteln geänderter Seiten seit %s: %s", since, exc, exc_info=True)
                    fail_job(f"Fehler beim Ermitteln der geänderten Seiten seit {since}: {exc}")
                    return

                queue: collections.deque[tuple[str, int, int]] = collections.deque()
                seen: set[str] = set()
                with db.transaction(conn):
                    for page in recent_pages:
                        pid = str(page.get("id") or "")
                        if not pid:
                            continue
                        puid = confluence_uid(pid)
                        if puid in force_refresh_uids:
                            continue
                        force_refresh_uids.add(puid)
                        db.ensure_node(conn, puid, title_hint=str(page.get("title") or ""))
                        space_hint = str(page.get("space") or "").strip()
                        if space_hint:
                            conn.execute(
                                "UPDATE documents SET space_or_project=? WHERE uid=? "
                                "AND (space_or_project IS NULL OR space_or_project='')",
                                (space_hint, puid),
                            )
                        db.queue_add(conn, job_id, puid, 0, 0)
                        queue.append((puid, 0, 0))

                with self._live_lock:
                    self._live["queue_size"] = len(queue)
                    self._live["last_message"] = (
                        f"{len(queue)} seit {since} geänderte Seiten entdeckt."
                        if queue
                        else f"Keine seit {since} geänderten Seiten gefunden."
                    )
                if queue:
                    db.add_job_log(
                        conn,
                        job_id,
                        "info",
                        f"{len(queue)} seit {since} geänderte Seiten entdeckt und in Queue eingereiht.",
                    )
                else:
                    final_message_override = (
                        f"Keine Seiten gefunden, die seit {since} geändert wurden ({scope_label})"
                    )
                    db.add_job_log(conn, job_id, "info", final_message_override)

            elif initial_uid.startswith("space:"):
                space_keys = [
                    s.strip()
                    for s in initial_uid.split(":", 1)[1].split(",")
                    if s.strip()
                ]
                space_label = ", ".join(space_keys)
                logger.info("Space-Crawl für '%s' gestartet", space_label)
                with self._live_lock:
                    self._live["last_message"] = f"Ermittle alle Seiten für Space '{space_label}'..."

                if confluence_client is None:
                    fail_job(f"Confluence-Client nicht verfügbar für Space-Crawl ({space_label})")
                    return

                max_space_pages = int(params.get("max_confluence_pages", 100_000))
                if max_space_pages <= 0:
                    finish_job(
                        JOB_COMPLETED,
                        "max_confluence_pages ist 0 – kein Space-Crawl durchgeführt",
                    )
                    return
                space_pages: list[tuple[str, str, str]] = []
                seen_page_ids: set[str] = set()
                failures: list[str] = []
                page_parents: dict[str, str] = {}
                list_with_parents = getattr(
                    confluence_client, "list_space_pages_with_parents", None
                )
                for space_key in space_keys:
                    remaining = max_space_pages - len(space_pages)
                    if remaining <= 0:
                        break
                    try:
                        if list_with_parents is not None:
                            found = list_with_parents(space_key, max_items=remaining)
                        else:  # clients without ancestor support (e.g. test doubles)
                            found = [
                                (pid, title, "")
                                for pid, title in confluence_client.list_space_pages(
                                    space_key, max_items=remaining
                                )
                            ]
                    except CrawlStopped:
                        finish_job(JOB_STOPPED, "Gestoppt")
                        return
                    except BudgetExceeded as exc:
                        finish_job(JOB_COMPLETED, f"Budget erschöpft: {exc}")
                        return
                    except Exception as exc:
                        logger.warning(
                            "Space '%s' konnte nicht gelesen werden: %s", space_key, exc, exc_info=True
                        )
                        failures.append(f"{space_key}: {exc}")
                        db.add_job_log(
                            conn,
                            job_id,
                            "warning",
                            f"Space '{space_key}' konnte nicht gelesen werden: {exc}",
                        )
                        continue
                    for pid, ptitle, parent_id in found:
                        if pid in seen_page_ids:
                            continue
                        seen_page_ids.add(pid)
                        space_pages.append((pid, ptitle, space_key))
                        if parent_id:
                            page_parents[pid] = str(parent_id)

                if not space_pages:
                    if failures:
                        fail_job(
                            f"Fehler beim Ermitteln der Seiten in Space(s) '{space_label}': "
                            + "; ".join(failures)
                        )
                    else:
                        fail_job(f"Keine Seiten in Space(s) '{space_label}' gefunden")
                    return

                queue: collections.deque[tuple[str, int, int]] = collections.deque(
                    [(confluence_uid(pid), 0, 0) for pid, _, _ in space_pages]
                )
                seen: set[str] = set()
                space_page_uids = {confluence_uid(pid) for pid, _, _ in space_pages}
                if page_parents:
                    # Build parent -> child edges from the ancestors we just got,
                    # so no extra child request per page is needed.
                    from .links import LinkContext, confluence_page_url

                    link_ctx = LinkContext.from_config(cfg)
                    for pid, ptitle, _ in space_pages:
                        parent_id = page_parents.get(pid) or ""
                        if not parent_id:
                            continue
                        derived_child_links.setdefault(confluence_uid(parent_id), []).append(
                            LinkTarget(
                                uid=confluence_uid(pid),
                                kind=EDGE_PARENT_OF,
                                relation_label="parent of",
                                source_field="children",
                                anchor_text=ptitle,
                                evidence_url=confluence_page_url(link_ctx, pid),
                                title_hint=ptitle,
                            )
                        )
                with db.transaction(conn):
                    for pid, ptitle, page_space in space_pages:
                        puid = confluence_uid(pid)
                        db.ensure_node(conn, puid, title_hint=ptitle)
                        conn.execute(
                            "UPDATE documents SET space_or_project=? WHERE uid=? "
                            "AND (space_or_project IS NULL OR space_or_project='')",
                            (page_space, puid),
                        )
                        db.queue_add(conn, job_id, puid, 0, 0)

                with self._live_lock:
                    self._live["queue_size"] = len(queue)
                    self._live["api_requests"] = budget.used
                    self._live["last_message"] = (
                        f"Space '{space_label}': {len(queue)} Seiten entdeckt."
                    )
                db.add_job_log(
                    conn,
                    job_id,
                    "info",
                    f"Space '{space_label}': {len(queue)} Seiten entdeckt und in Queue eingereiht.",
                )
                if failures:
                    db.add_job_log(
                        conn, job_id, "warning", "Nicht gelesen: " + "; ".join(failures)
                    )

            elif params.get("refresh_only"):
                existing_uids = db.get_existing_document_uids(conn)
                queue: collections.deque[tuple[str, int, int]] = collections.deque(
                    [(uid, 0, 0) for uid in existing_uids]
                )
                seen: set[str] = set()
                with db.transaction(conn):
                    for uid in existing_uids:
                        db.queue_add(conn, job_id, uid, 0, 0)
                num_existing = len(existing_uids)
                params["max_total_documents"] = max(
                    int(params.get("max_total_documents", 0)), num_existing
                )
                params["max_confluence_pages"] = max(
                    int(params.get("max_confluence_pages", 0)), num_existing
                )
                params["max_jira_issues"] = max(
                    int(params.get("max_jira_issues", 0)), num_existing
                )
                params["max_api_requests"] = max(
                    int(params.get("max_api_requests", 0)), num_existing * 3 + 100
                )
            else:
                queue: collections.deque[tuple[str, int, int]] = collections.deque(
                    [(initial_uid, 0, 0)]
                )
                seen: set[str] = set()
                db.queue_add(conn, job_id, initial_uid, 0, 0)

            pages_indexed = 0
            jira_indexed = 0
            skipped = 0
            errors = 0
            reused_documents = 0

            # --- batch prefetch: up to `batch_fetch_size` documents per request
            batch_size = max(1, min(int(params.get("batch_fetch_size", 100)), 100))
            bulk_confluence = getattr(confluence_client, "fetch_documents_bulk", None)
            bulk_jira = getattr(jira_client, "fetch_documents_bulk", None)
            prefetched: dict[str, Any] = {}

            def prefetch_batch(current_uid: str | None = None) -> None:
                """Prefetch upcoming documents of this job in few API requests."""
                if (not queue and not current_uid) or (bulk_confluence is None and bulk_jira is None):
                    return
                # Drop results of documents that were already handled/skipped
                # (e.g. budget skips) so prefetching keeps running.
                for stale_uid in [k for k in prefetched if k in seen]:
                    prefetched.pop(stale_uid, None)
                remaining_conf = max(0, int(params["max_confluence_pages"]) - pages_indexed)
                remaining_jira = max(0, int(params["max_jira_issues"]) - jira_indexed)
                remaining_total = max(
                    0, int(params["max_total_documents"]) - pages_indexed - jira_indexed
                )
                conf_ids: list[str] = []
                jira_keys: list[str] = []

                scan_limit = batch_size * 10  # bounded lookahead
                candidates: list[str] = [current_uid] if current_uid else []
                for cand_uid, _depth, _hops in queue:
                    if len(candidates) >= scan_limit:
                        break
                    candidates.append(cand_uid)

                for cand_uid in candidates:
                    if len(conf_ids) + len(jira_keys) >= min(batch_size * 2, remaining_total):
                        break
                    if cand_uid in seen or cand_uid in prefetched:
                        continue
                    if is_unresolved_uid(cand_uid):
                        continue  # resolved via resolve_title before queueing
                    try:
                        cand_type, cand_id = parse_uid(cand_uid)
                    except ValueError:
                        continue
                    if cand_type == "confluence":
                        if bulk_confluence is None or len(conf_ids) >= min(batch_size, remaining_conf):
                            continue
                    elif bulk_jira is None or len(jira_keys) >= min(batch_size, remaining_jira):
                        continue
                    if not params["refresh_existing"] and cand_uid not in force_refresh_uids:
                        existing = db.get_document(conn, cand_uid)
                        if existing and existing.get("state") == STATE_INDEXED:
                            continue  # reused without a request
                    if cand_type == "confluence":
                        if cand_id not in conf_ids:
                            conf_ids.append(cand_id)
                    elif cand_id not in jira_keys:
                        jira_keys.append(cand_id)

                if not conf_ids and not jira_keys:
                    return

                requests_before = budget.used
                fetched: dict[str, Any] = {}
                with self._live_lock:
                    self._live["api_requests"] = budget.used
                    self._live["last_message"] = (
                        f"Vorabruf: {len(conf_ids)} Confluence-Seite(n), "
                        f"{len(jira_keys)} Jira-Ticket(s) werden geladen …"
                    )
                try:
                    if conf_ids and bulk_confluence is not None:
                        for pid, item in bulk_confluence(
                            conf_ids,
                            # Space crawls derive child edges from the listing;
                            # only enrich when that data is unavailable.
                            include_children=(
                                params["include_child_pages"] and not derived_child_links
                            ),
                            scan_plain_keys=params["scan_plain_text_issue_keys"],
                        ).items():
                            fetched[confluence_uid(pid)] = item
                    if jira_keys and bulk_jira is not None:
                        for key, item in bulk_jira(
                            jira_keys,
                            include_remote_links=params["include_remote_links"],
                            scan_plain_keys=params["scan_plain_text_issue_keys"],
                        ).items():
                            fetched[jira_uid(key)] = item
                except (CrawlStopped, BudgetExceeded):
                    raise
                except Exception as exc:
                    logger.warning(
                        "Batch-Vorabruf fehlgeschlagen (%s) – Einzelabruf wird genutzt", exc
                    )
                    return

                prefetched.update(fetched)
                if fetched:
                    with self._live_lock:
                        self._live["api_requests"] = budget.used
                        self._live["last_message"] = (
                            f"Batch-Vorabruf: {len(fetched)} Dokument(e) in "
                            f"{budget.used - requests_before} Request(s)"
                        )
                    db.add_job_log(
                        conn,
                        job_id,
                        "info",
                        f"Batch-Vorabruf: {len(fetched)} Dokument(e) in "
                        f"{budget.used - requests_before} Request(s)",
                    )

            def resolve_titles_for(targets: list[LinkTarget]) -> None:
                """Resolve "/display/SPACE/Title" links of one document in few requests.

                Uses the per-job cache first, then the persistent title cache and
                finally `resolve_titles_bulk()` (one CQL request per space).
                """
                if not params.get("resolve_page_titles") or confluence_client is None:
                    return
                if not (params["follow_confluence_links"] or params["follow_jira_to_confluence"]):
                    return

                pairs: list[tuple[str, str]] = []
                for target in targets[: int(params["max_links_per_document"])]:
                    if not is_unresolved_uid(target.uid):
                        continue
                    space, title = split_unresolved_uid(target.uid)
                    key = (space, title)
                    if key in resolved_titles_cache:
                        continue
                    known, cached_id = db.get_title_resolution(conn, space, title)
                    if known:
                        resolved_titles_cache[key] = cached_id
                        continue
                    if key not in pairs:
                        pairs.append(key)
                if not pairs:
                    return

                bulk = getattr(confluence_client, "resolve_titles_bulk", None)
                api_before = budget.used
                try:
                    if bulk is not None:
                        results = bulk(pairs)
                    else:  # Clients ohne Bulk-API (z. B. Test-Doubles)
                        results = {key: confluence_client.resolve_title(key[0], key[1])
                                   for key in pairs}
                except (CrawlStopped, BudgetExceeded):
                    raise
                except Exception as exc:
                    logger.info("Titel-Auflösung für %d Paar(e) fehlgeschlagen: %s", len(pairs), exc)
                    return

                for key, page_id in results.items():
                    resolved_titles_cache[key] = page_id
                    db.save_title_resolution(conn, key[0], key[1], page_id)
                if len(pairs) > 1:
                    db.add_job_log(
                        conn, job_id, "info",
                        f"Titel-Auflösung: {len(pairs)} Link(s) in "
                        f"{max(1, budget.used - api_before)} Request(s)",
                    )

            def sync_progress(curr_uid: str | None, msg: str | None) -> None:
                with self._live_lock:
                    self._live["pages_indexed"] = pages_indexed
                    self._live["jira_indexed"] = jira_indexed
                    self._live["api_requests"] = budget.used
                    self._live["skipped"] = skipped
                    self._live["errors"] = errors
                    self._live["reused_documents"] = reused_documents
                    self._live["current_uid"] = curr_uid
                    self._live["queue_size"] = len(queue)
                    if msg:
                        self._live["last_message"] = msg

                db.update_job(
                    conn,
                    job_id,
                    pages_indexed=pages_indexed,
                    jira_indexed=jira_indexed,
                    api_requests=budget.used,
                    skipped=skipped,
                    errors=errors,
                    current_uid=curr_uid,
                    last_message=msg or "",
                )

            final_state = JOB_COMPLETED
            if final_message_override:
                final_message = final_message_override
            elif params.get("refresh_only"):
                final_message = "Aktualisierung bestehender Dokumente abgeschlossen"
            elif initial_uid.startswith("recent:"):
                since = initial_uid.split(":", 1)[1].strip()
                final_message = f"Aktualisierung geänderter Seiten seit {since} abgeschlossen"
            else:
                final_message = "Indexierung abgeschlossen"

            try:
                while queue:
                    if stop_event.is_set():
                        raise CrawlStopped()

                    draining = False
                    try:
                        budget.check()
                    except BudgetExceeded:
                        # Documents already prefetched cost no further requests:
                        # index them before honouring the exhausted budget.
                        if not prefetched:
                            raise
                        draining = True

                    if (pages_indexed + jira_indexed) >= params["max_total_documents"]:
                        raise BudgetExceeded(
                            f"Maximales Dokumentenbudget erreicht ({params['max_total_documents']})"
                        )

                    uid, depth, hops = queue.popleft()
                    if uid in seen:
                        continue

                    if not draining:
                        # Count only results that are still ahead of us: entries for
                        # skipped/aliased uids must not block further prefetching.
                        pending = [k for k in prefetched if k not in seen]
                        if len(pending) < max(1, batch_size // 4):
                            prefetch_batch(uid)

                    seen.add(uid)
                    src_type, src_id = parse_uid(uid)

                    # Type budget enforcement
                    if (
                        src_type == "confluence"
                        and pages_indexed >= params["max_confluence_pages"]
                    ):
                        skipped += 1
                        db.queue_set_state(conn, job_id, uid, "not_fetched")
                        db.mark_document_state(
                            conn,
                            uid,
                            STATE_SKIPPED,
                            error="Confluence-Seitenbudget erschöpft",
                        )
                        sync_progress(uid, f"Übersprungen (Budget): {uid}")
                        continue

                    if (
                        src_type == "jira"
                        and jira_indexed >= params["max_jira_issues"]
                    ):
                        skipped += 1
                        db.queue_set_state(conn, job_id, uid, "not_fetched")
                        db.mark_document_state(
                            conn,
                            uid,
                            STATE_SKIPPED,
                            error="Jira-Ticketbudget erschöpft",
                        )
                        sync_progress(uid, f"Übersprungen (Budget): {uid}")
                        continue

                    targets: list[LinkTarget] = []
                    is_reused = False

                    # Check existing document for reuse (seed pages of an update run
                    # are always fetched again, even if they are already indexed)
                    if not params["refresh_existing"] and uid not in force_refresh_uids:
                        existing = db.get_document(conn, uid)
                        if existing and existing.get("state") == STATE_INDEXED:
                            is_reused = True
                            reused_documents += 1
                            db.queue_set_state(conn, job_id, uid, "done")
                            targets = db.get_outgoing_links(conn, uid)
                            sync_progress(uid, f"Wiederverwendet: {uid}")

                    if not is_reused:
                        with self._live_lock:
                            self._live["phase"] = "request"
                            self._live["current_uid"] = uid
                        sync_progress(uid, f"Rufe ab: {uid}")

                        try:
                            cached = prefetched.pop(uid, None)
                            if isinstance(cached, Exception):
                                raise cached
                            if cached is not None:
                                doc = cached
                            elif src_type == "confluence":
                                if confluence_client is None:
                                    if params.get("refresh_only"):
                                        errors += 1
                                        db.mark_document_state(conn, uid, STATE_ERROR, "Confluence-Client nicht konfiguriert")
                                        db.queue_set_state(conn, job_id, uid, "error")
                                        db.add_job_log(conn, job_id, "error", f"Confluence-Client nicht konfiguriert für {uid}", uid)
                                        sync_progress(uid, f"Fehler: {uid} (Confluence nicht konfiguriert)")
                                        continue
                                    raise ConfigError("Confluence-Client nicht konfiguriert")
                                doc = confluence_client.fetch_document(
                                    src_id,
                                    include_children=params["include_child_pages"],
                                    scan_plain_keys=params["scan_plain_text_issue_keys"],
                                )
                            elif jira_client is None:
                                if params.get("refresh_only"):
                                    errors += 1
                                    db.mark_document_state(conn, uid, STATE_ERROR, "Jira-Client nicht konfiguriert")
                                    db.queue_set_state(conn, job_id, uid, "error")
                                    db.add_job_log(conn, job_id, "error", f"Jira-Client nicht konfiguriert für {uid}", uid)
                                    sync_progress(uid, f"Fehler: {uid} (Jira nicht konfiguriert)")
                                    continue
                                raise ConfigError("Jira-Client nicht konfiguriert")
                            else:
                                doc = jira_client.fetch_document(
                                    src_id,
                                    include_remote_links=params["include_remote_links"],
                                    scan_plain_keys=params["scan_plain_text_issue_keys"],
                                )
                        except NotFoundError as exc:
                            db.mark_document_state(conn, uid, STATE_MISSING, str(exc))
                            db.queue_set_state(conn, job_id, uid, "missing")
                            db.add_job_log(
                                conn,
                                job_id,
                                "warning",
                                f"Dokument nicht gefunden: {uid} ({exc})",
                                uid,
                            )
                            sync_progress(uid, f"Nicht gefunden: {uid}")
                            continue
                        except ForbiddenError as exc:
                            db.mark_document_state(conn, uid, STATE_FORBIDDEN, str(exc))
                            db.queue_set_state(conn, job_id, uid, "forbidden")
                            db.add_job_log(
                                conn,
                                job_id,
                                "warning",
                                f"Zugriff verweigert: {uid} ({exc})",
                                uid,
                            )
                            sync_progress(uid, f"Zugriff verweigert: {uid}")
                            continue
                        except CrawlStopped:
                            raise
                        except BudgetExceeded:
                            raise
                        except (InvalidResponseError, AtlassianError, Exception) as exc:
                            errors += 1
                            db.mark_document_state(conn, uid, STATE_ERROR, str(exc))
                            db.queue_set_state(conn, job_id, uid, "error")
                            db.add_job_log(
                                conn, job_id, "error", f"Fehler bei {uid}: {exc}", uid
                            )
                            sync_progress(uid, f"Fehler: {uid} ({exc})")
                            continue

                        # Parent/child edges: for space crawls they were derived from
                        # the listing (no extra requests); pages reached via links are
                        # enriched individually.
                        if (
                            src_type == "confluence"
                            and params["include_child_pages"]
                            and derived_child_links
                        ):
                            if doc.uid in space_page_uids:
                                known = {lk.uid for lk in doc.links}
                                for link in derived_child_links.get(doc.uid, []):
                                    if link.uid not in known:
                                        doc.links.append(link)
                            else:
                                enrich = getattr(confluence_client, "enrich_children", None)
                                if enrich is not None and not any(
                                    lk.source_field == "children" for lk in doc.links
                                ):
                                    try:
                                        enrich(doc)
                                    except CrawlStopped:
                                        raise
                                    except BudgetExceeded as exc:
                                        logger.info("Kinder von %s nicht geladen: %s", doc.uid, exc)
                                    except Exception as exc:
                                        logger.warning("Kinder von %s nicht abrufbar: %s", doc.uid, exc)

                        # Document successfully fetched -> chunk & save
                        with self._live_lock:
                            self._live["phase"] = "saving"

                        index_cfg = cfg.get("index") or {}
                        target_chars = int(index_cfg.get("chunk_target_chars", 1200))
                        overlap_chars = int(index_cfg.get("chunk_overlap_chars", 150))
                        min_chars = int(index_cfg.get("min_chunk_chars", 80))

                        chunks: list[Chunk] = []
                        if doc.sections:
                            try:
                                from . import text

                                chunks = text.chunk_sections(
                                    doc.title,
                                    doc.sections,
                                    target_chars=target_chars,
                                    overlap_chars=overlap_chars,
                                    min_chars=min_chars,
                                )
                            except (ImportError, AttributeError):
                                chunks = [
                                    Chunk(heading=h, body=b)
                                    for h, b in doc.sections
                                    if b
                                ] or [Chunk(heading="", body=doc.body_text)]
                        else:
                            chunks = [Chunk(heading="", body=doc.body_text)]

                        # Handle moved Jira issue key
                        if doc.uid != uid:
                            db.save_document(conn, doc, chunks)
                            db.add_edge(
                                conn,
                                uid,
                                LinkTarget(
                                    uid=doc.uid,
                                    kind=EDGE_JIRA_RELATES_TO,
                                    relation_label="moved to",
                                    source_field="key-redirect",
                                    title_hint=doc.title,
                                ),
                            )
                            db.mark_document_state(
                                conn,
                                uid,
                                STATE_SKIPPED,
                                error=f"Verschoben nach {doc.uid}",
                            )
                            db.queue_set_state(conn, job_id, uid, "skipped")
                            seen.add(doc.uid)
                            jira_indexed += 1
                        else:
                            db.save_document(conn, doc, chunks)
                            db.queue_set_state(conn, job_id, uid, "done")
                            if src_type == "confluence":
                                pages_indexed += 1
                            else:
                                jira_indexed += 1

                        targets = doc.links
                        sync_progress(
                            doc.uid,
                            f"Aktualisiert: {doc.title or doc.uid}"
                            if params.get("refresh_only")
                            else f"Indexiert: {doc.title or doc.uid}",
                        )

                    if params.get("refresh_only"):
                        # In refresh_only mode, only existing documents are updated;
                        # do not follow or enqueue outgoing links.
                        continue

                    # Enqueue outgoing links up to max_links_per_document
                    max_links = int(params["max_links_per_document"])
                    resolve_titles_for(targets)
                    for target in targets[:max_links]:
                        if stop_event.is_set():
                            raise CrawlStopped()

                        target_uid = target.uid

                        # Unresolved Confluence title target resolution
                        if is_unresolved_uid(target_uid):
                            if not params["resolve_page_titles"]:
                                continue

                            # Check if unresolved target would be followed
                            if src_type == "confluence":
                                if (
                                    target.kind == EDGE_PARENT_OF
                                    and not params["include_child_pages"]
                                ):
                                    continue
                                if (
                                    target.kind != EDGE_PARENT_OF
                                    and not params["follow_confluence_links"]
                                ):
                                    continue
                                if depth + 1 > params["max_confluence_depth"]:
                                    continue
                            elif src_type == "jira":
                                if not params["follow_jira_to_confluence"]:
                                    continue
                                if depth + 1 > params["max_confluence_depth"]:
                                    continue

                            space, title = split_unresolved_uid(target_uid)
                            cache_key = (space, title)
                            if cache_key in resolved_titles_cache:
                                resolved_id = resolved_titles_cache[cache_key]
                            else:
                                try:
                                    resolved_id = confluence_client.resolve_title(
                                        space, title
                                    )
                                except (CrawlStopped, BudgetExceeded):
                                    raise
                                except Exception as exc:
                                    logger.info("Titel %s/%s nicht auflösbar: %s", space, title, exc)
                                    resolved_id = None
                                resolved_titles_cache[cache_key] = resolved_id
                                db.save_title_resolution(conn, space, title, resolved_id)

                            if resolved_id:
                                resolved_uid = confluence_uid(resolved_id)
                                db.add_edge(
                                    conn,
                                    uid,
                                    LinkTarget(
                                        uid=resolved_uid,
                                        kind=target.kind,
                                        relation_label=target.relation_label,
                                        source_field="title-resolved",
                                        anchor_text=target.anchor_text,
                                        evidence_url=target.evidence_url,
                                        title_hint=title,
                                    ),
                                )
                                target_uid = resolved_uid
                            else:
                                # Resolution failed; reference node remains as-is
                                continue

                        try:
                            target_type, _ = parse_uid(target_uid)
                        except ValueError:
                            continue

                        may_follow = False
                        next_depth = depth
                        next_hops = hops

                        if src_type == "confluence":
                            if target_type == "confluence":
                                if target.kind == EDGE_PARENT_OF:
                                    if params["include_child_pages"] and (
                                        depth + 1 <= params["max_confluence_depth"]
                                    ):
                                        may_follow = True
                                        next_depth = depth + 1
                                        next_hops = 0
                                else:
                                    if params["follow_confluence_links"] and (
                                        depth + 1 <= params["max_confluence_depth"]
                                    ):
                                        may_follow = True
                                        next_depth = depth + 1
                                        next_hops = 0
                            elif target_type == "jira":
                                if params["index_linked_jira_issues"]:
                                    may_follow = True
                                    next_depth = depth
                                    next_hops = 0
                        elif src_type == "jira":
                            if target_type == "jira":
                                if (
                                    params["index_linked_jira_issues"]
                                    and params["follow_jira_issue_links"]
                                    and (hops + 1 <= params["max_jira_link_hops"])
                                ):
                                    may_follow = True
                                    next_depth = depth
                                    next_hops = hops + 1
                            elif target_type == "confluence":
                                if params["follow_jira_to_confluence"] and (
                                    depth + 1 <= params["max_confluence_depth"]
                                ):
                                    may_follow = True
                                    next_depth = depth + 1
                                    next_hops = 0

                        if may_follow and target_uid not in seen:
                            if db.queue_add(
                                conn, job_id, target_uid, next_depth, next_hops
                            ):
                                queue.append((target_uid, next_depth, next_hops))

                    with self._live_lock:
                        self._live["queue_size"] = len(queue)

            except CrawlStopped:
                final_state = JOB_STOPPED
                final_message = "Gestoppt"
                conn.execute(
                    "UPDATE crawl_queue SET state='not_fetched' WHERE job_id=? AND state='pending'",
                    (job_id,),
                )
            except BudgetExceeded as exc:
                final_state = JOB_COMPLETED
                final_message = f"Budget erschöpft: {exc}"
                cur = conn.execute(
                    "UPDATE crawl_queue SET state='not_fetched' WHERE job_id=? AND state='pending'",
                    (job_id,),
                )
                skipped += cur.rowcount
            except Exception as exc:
                logger.exception("Unerwarteter Fehler im Crawler-Worker")
                final_state = JOB_FAILED
                final_message = f"Unerwarteter Fehler: {exc}"
                db.add_job_log(conn, job_id, "error", f"Job fehlgeschlagen: {exc}")
            finally:
                with self._live_lock:
                    self._live["phase"] = "idle"
                    self._live["retry_wait_seconds"] = None
                    self._live["wait_source"] = None
                    self._live["queue_size"] = 0
                    self._live["pages_indexed"] = pages_indexed
                    self._live["jira_indexed"] = jira_indexed
                    self._live["api_requests"] = budget.used
                    self._live["skipped"] = skipped
                    self._live["errors"] = errors
                    self._live["reused_documents"] = reused_documents
                    self._live["last_message"] = final_message

                db.update_job(
                    conn,
                    job_id,
                    state=final_state,
                    finished_at=db.now_iso(),
                    pages_indexed=pages_indexed,
                    jira_indexed=jira_indexed,
                    api_requests=budget.used,
                    skipped=skipped,
                    errors=errors,
                    last_message=final_message,
                )
                db.add_job_log(
                    conn,
                    job_id,
                    "info",
                    f"Job beendet mit Status '{final_state}': {final_message}",
                )
        finally:
            conn.close()
