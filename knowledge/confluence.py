"""Confluence REST client: Server/Data Center (/rest/api/content) and Cloud v2.

Produces FetchedDocument objects; all requests go through RateLimitedClient so
that sleep, retries, budgets and the stop event apply uniformly.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from .atlassian_http import RateLimitedClient
from .links import LinkContext, confluence_page_url
from .models import (
    EDGE_PARENT_OF, AtlassianError, FetchedDocument, InvalidResponseError,
    LinkTarget, confluence_uid,
)
from .text import parse_confluence_storage, truncate_text

logger = logging.getLogger(__name__)

API_MODES = ("server", "cloud_v2")
CHILD_PAGE_SIZE = 50


class ConfluenceClient:
    def __init__(self, http: RateLimitedClient, api_mode: str, ctx: LinkContext,
                 index_cfg: dict | None = None):
        if api_mode not in API_MODES:
            raise ValueError(f"Unbekannter Confluence-API-Modus: {api_mode}")
        self.http = http
        self.api_mode = api_mode
        self.ctx = ctx
        self.max_document_chars = int((index_cfg or {}).get("max_document_chars", 2_000_000))
        self._space_cache: dict[str, str] = {}

    # ------------------------------------------------------------ helpers

    def _resolve(self, link: str) -> str:
        """Turn a `_links.next`/`webui` value into an absolute URL below base_url.

        Atlassian returns these either relative to the context path
        ("/rest/api/...") or relative to the host root ("/confluence/rest/...",
        "/wiki/api/v2/..."). Never duplicate the context path.
        """
        link = (link or "").strip()
        if link.startswith(("http://", "https://")):
            return link
        base = urllib.parse.urlsplit(self.http.base_url)
        base_path = base.path.rstrip("/")
        if not link.startswith("/"):
            link = "/" + link
        if base_path and (link == base_path or link.startswith(base_path + "/")):
            return f"{base.scheme}://{base.netloc}{link}"
        return f"{self.http.base_url}{link}"

    @staticmethod
    def _storage(data: dict, page_id: str) -> str:
        storage = ((data.get("body") or {}).get("storage") or {})
        value = storage.get("value")
        if not isinstance(value, str):
            raise InvalidResponseError(
                f"Confluence-Seite {page_id}: Antwort enthält keinen Storage-Body "
                "(fehlende Leserechte oder unerwartetes API-Format)")
        return value

    def _space_key_cloud(self, space_id: str) -> str:
        if not space_id:
            return ""
        if space_id not in self._space_cache:
            data = self.http.get_json(f"/api/v2/spaces/{urllib.parse.quote(space_id)}")
            self._space_cache[space_id] = str((data or {}).get("key") or "")
        return self._space_cache[space_id]

    # ------------------------------------------------------------ public API

    def fetch_document(self, page_id: str, *, include_children: bool, max_children: int = 500,
                       scan_plain_keys: bool = False) -> FetchedDocument:
        pid = str(page_id).strip()
        if not pid.isdigit():
            raise ValueError(f"Ungültige Confluence-Page-ID: {page_id!r}")
        qpid = urllib.parse.quote(pid)
        if self.api_mode == "server":
            data = self.http.get_json(
                f"/rest/api/content/{qpid}",
                {"expand": "body.storage,version,space,history.lastUpdated,metadata.labels"})
            if not isinstance(data, dict):
                raise InvalidResponseError(f"Confluence-Seite {pid}: unerwartete Antwort")
            storage = self._storage(data, pid)
            space_key = str((data.get("space") or {}).get("key") or "")
            version = data.get("version") or {}
            updated_at = (((data.get("history") or {}).get("lastUpdated") or {}).get("when")
                          or version.get("when"))
            labels = [str(l.get("name")) for l in
                      (((data.get("metadata") or {}).get("labels") or {}).get("results") or [])
                      if isinstance(l, dict) and l.get("name")]
            status = data.get("status")
        else:
            data = self.http.get_json(f"/api/v2/pages/{qpid}", {"body-format": "storage"})
            if not isinstance(data, dict):
                raise InvalidResponseError(f"Confluence-Seite {pid}: unerwartete Antwort")
            storage = self._storage(data, pid)
            space_key = self._space_key_cloud(str(data.get("spaceId") or ""))
            version = data.get("version") or {}
            updated_at = version.get("createdAt") or data.get("createdAt")
            labels = []
            status = data.get("status")

        title = str(data.get("title") or "").strip() or f"Seite {pid}"
        webui = (data.get("_links") or {}).get("webui")
        url = self._resolve(webui) if webui else confluence_page_url(self.ctx, pid)

        parsed = parse_confluence_storage(storage, self.ctx, space_key=space_key,
                                          scan_plain_keys=scan_plain_keys)
        body_text, truncated = truncate_text(parsed.plain_text, self.max_document_chars)
        sections = parsed.sections
        if truncated:
            logger.warning("Confluence-Seite %s gekürzt auf %d Zeichen", pid, self.max_document_chars)
            sections = [("", body_text)]

        links: list[LinkTarget] = []
        if include_children:
            for child_id, child_title in self.list_child_pages(pid, max_children):
                links.append(LinkTarget(
                    uid=confluence_uid(child_id), kind=EDGE_PARENT_OF, relation_label="parent of",
                    source_field="children", anchor_text=child_title,
                    evidence_url=confluence_page_url(self.ctx, child_id), title_hint=child_title))
        links.extend(parsed.links)

        return FetchedDocument(
            uid=confluence_uid(pid), source_type="confluence", source_id=pid, title=title,
            url=url, body_text=body_text, sections=sections, links=links,
            space_or_project=space_key or None,
            status_label=status if status and status != "current" else None,
            document_type=str(data.get("type") or "page"),
            source_version=str(version.get("number")) if version.get("number") is not None else None,
            source_updated_at=updated_at, labels=labels)

    def list_child_pages(self, page_id: str, max_items: int) -> list[tuple[str, str]]:
        """Direct child pages (id, title), paginated within the request budget."""
        qpid = urllib.parse.quote(str(page_id))
        if self.api_mode == "server":
            next_url: str | None = self._resolve(
                f"/rest/api/content/{qpid}/child/page?limit={CHILD_PAGE_SIZE}&start=0")
        else:
            next_url = self._resolve(f"/api/v2/pages/{qpid}/direct-children?limit={CHILD_PAGE_SIZE}")
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        while next_url and len(out) < max_items:
            data = self.http.get_json(next_url)
            if not isinstance(data, dict):
                raise InvalidResponseError(f"Kinderliste von {page_id}: unerwartete Antwort")
            results = data.get("results") or []
            for item in results:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "page") != "page":
                    continue  # whiteboards, databases, folders, ...
                cid = str(item.get("id") or "")
                if cid.isdigit() and cid not in seen:
                    seen.add(cid)
                    out.append((cid, str(item.get("title") or "")))
                    if len(out) >= max_items:
                        break
            nxt = (data.get("_links") or {}).get("next")
            if not nxt and self.api_mode == "server" and len(results) >= CHILD_PAGE_SIZE:
                start = int(data.get("start") or 0) + len(results)
                nxt = f"/rest/api/content/{qpid}/child/page?limit={CHILD_PAGE_SIZE}&start={start}"
            next_url = self._resolve(nxt) if nxt and results else None
        return out

    def resolve_title(self, space_key: str, title: str) -> str | None:
        """Page id for an exact (space, title) pair; None if 0 or >1 matches."""
        if not title:
            return None
        params: dict[str, Any] = {"type": "page", "title": title, "limit": 2}
        if space_key:
            params["spaceKey"] = space_key
        data = self.http.get_json("/rest/api/content", params)
        results = [r for r in ((data or {}).get("results") or []) if isinstance(r, dict)]
        if len(results) != 1:
            return None
        pid = str(results[0].get("id") or "")
        return pid if pid.isdigit() else None

    def test_connection(self) -> dict:
        path = "/rest/api/space" if self.api_mode == "server" else "/api/v2/spaces"
        try:
            data = self.http.get_json(path, {"limit": 1})
        except AtlassianError as exc:
            return {"ok": False, "message": str(exc), "status": exc.status}
        except Exception as exc:  # network, TLS, budget, ...
            return {"ok": False, "message": str(exc) or exc.__class__.__name__}
        results = (data or {}).get("results") if isinstance(data, dict) else None
        if results is None:
            return {"ok": False, "message": "Unerwartete Antwort – ist die Basis-URL korrekt?"}
        hint = "" if results else " (keine Spaces sichtbar – Berechtigungen/Token prüfen)"
        return {"ok": True, "message": f"Verbindung erfolgreich{hint}", "base_url": self.http.base_url}
