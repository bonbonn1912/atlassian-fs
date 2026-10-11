"""Confluence REST client: Server/Data Center (/rest/api/content) and Cloud v2.

Produces FetchedDocument objects; all requests go through RateLimitedClient so
that sleep, retries, budgets and the stop event apply uniformly.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from .atlassian_http import RateLimitedClient
from .links import LinkContext, confluence_page_url
from .models import (
    EDGE_PARENT_OF, AtlassianError, BudgetExceeded, CrawlStopped, FetchedDocument,
    InvalidResponseError, LinkTarget, NotFoundError, confluence_uid,
)
from .text import parse_confluence_storage, truncate_text

logger = logging.getLogger(__name__)

API_MODES = ("server", "cloud_v2")
# Atlassian caps these endpoints at 100 items per request (server REST and
# cloud v2 both accept 100), so a full page costs a single request.
CHILD_PAGE_SIZE = 100
SPACE_PAGE_SIZE = 100
SEARCH_PAGE_SIZE = 100
# Maximum ids per bulk page request (Confluence search / v2 pages limit).
BULK_PAGE_SIZE = 100
SERVER_PAGE_EXPAND = "body.storage,version,space,history.lastUpdated,metadata.labels"


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

    def _document_from_payload(self, data: dict, pid: str, *,
                               scan_plain_keys: bool) -> FetchedDocument:
        """Build a FetchedDocument from a v1 (server) or v2 (cloud) page payload."""
        storage = self._storage(data, pid)
        if self.api_mode == "server":
            space_key = str((data.get("space") or {}).get("key") or "")
            version = data.get("version") or {}
            updated_at = (((data.get("history") or {}).get("lastUpdated") or {}).get("when")
                          or version.get("when"))
            labels = [str(l.get("name")) for l in
                      (((data.get("metadata") or {}).get("labels") or {}).get("results") or [])
                      if isinstance(l, dict) and l.get("name")]
        else:
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

        return FetchedDocument(
            uid=confluence_uid(pid), source_type="confluence", source_id=pid, title=title,
            url=url, body_text=body_text, sections=sections, links=list(parsed.links),
            space_or_project=space_key or None,
            status_label=status if status and status != "current" else None,
            document_type=str(data.get("type") or "page"),
            source_version=str(version.get("number")) if version.get("number") is not None else None,
            source_updated_at=updated_at, labels=labels)

    def _add_child_links(self, doc: FetchedDocument, max_children: int) -> None:
        for child_id, child_title in self.list_child_pages(doc.source_id, max_children):
            doc.links.append(LinkTarget(
                uid=confluence_uid(child_id), kind=EDGE_PARENT_OF, relation_label="parent of",
                source_field="children", anchor_text=child_title,
                evidence_url=confluence_page_url(self.ctx, child_id), title_hint=child_title))

    def fetch_document(self, page_id: str, *, include_children: bool, max_children: int = 500,
                       scan_plain_keys: bool = False) -> FetchedDocument:
        pid = str(page_id).strip()
        if not pid.isdigit():
            raise ValueError(f"Ungültige Confluence-Page-ID: {page_id!r}")
        qpid = urllib.parse.quote(pid)
        if self.api_mode == "server":
            data = self.http.get_json(f"/rest/api/content/{qpid}", {"expand": SERVER_PAGE_EXPAND})
        else:
            data = self.http.get_json(f"/api/v2/pages/{qpid}", {"body-format": "storage"})
        if not isinstance(data, dict):
            raise InvalidResponseError(f"Confluence-Seite {pid}: unerwartete Antwort")
        doc = self._document_from_payload(data, pid, scan_plain_keys=scan_plain_keys)
        if include_children:
            self._add_child_links(doc, max_children)
        return doc

    def fetch_documents_bulk(
        self,
        page_ids: list[str],
        *,
        include_children: bool = False,
        max_children: int = 500,
        scan_plain_keys: bool = False,
        chunk_size: int = BULK_PAGE_SIZE,
    ) -> dict[str, FetchedDocument | Exception]:
        """Fetch up to `chunk_size` pages (max. 100) with one API request each.

        Server: CQL search `id in (...)`; Cloud v2: `/api/v2/pages?id=...`.
        Returns {page_id: FetchedDocument | Exception}; ids the API does not
        return (deleted, no permission) map to NotFoundError.
        """
        out: dict[str, FetchedDocument | Exception] = {}
        ids: list[str] = []
        for raw in page_ids:
            pid = str(raw).strip()
            if not pid.isdigit():
                out[str(raw)] = ValueError(f"Ungültige Confluence-Page-ID: {raw!r}")
                continue
            if pid not in ids:
                ids.append(pid)

        step = max(1, min(int(chunk_size), BULK_PAGE_SIZE))
        for start in range(0, len(ids), step):
            chunk = ids[start:start + step]
            try:
                payloads = self._bulk_page_payloads(chunk)
            except (CrawlStopped, BudgetExceeded):
                raise
            except Exception as exc:
                # Endpoint blocked/broken (e.g. CQL search disabled): fall back
                # to the single-page endpoint instead of failing the whole chunk.
                logger.info(
                    "Bulk-Seitenabruf fehlgeschlagen (%s) – Einzelabruf für %d Seite(n)",
                    exc, len(chunk),
                )
                for pid in chunk:
                    try:
                        out[pid] = self.fetch_document(
                            pid, include_children=False, scan_plain_keys=scan_plain_keys)
                    except Exception as single_exc:
                        out[pid] = single_exc
                continue
            for pid in chunk:
                item = payloads.get(pid)
                if item is None:
                    # Distinguish 403 (forbidden) from 404 (missing) like the
                    # single-page path; only happens for ids the API omitted.
                    try:
                        out[pid] = self.fetch_document(
                            pid, include_children=False, scan_plain_keys=scan_plain_keys)
                    except Exception as exc:
                        out[pid] = exc
                    continue
                try:
                    out[pid] = self._document_from_payload(item, pid, scan_plain_keys=scan_plain_keys)
                except Exception as exc:
                    out[pid] = exc

        if not include_children:
            return out

        page_ids = [pid for pid, item in out.items() if not isinstance(item, Exception)]
        children_map: dict[str, list[tuple[str, str]]] | None = None
        if page_ids:
            try:
                children_map = self.list_children_bulk(page_ids, max_children)
            except CrawlStopped:
                raise
            except BudgetExceeded as exc:
                # Child lists are an enrichment: keep the fetched documents
                # instead of throwing away the whole batch.
                logger.warning(
                    "Kinderlisten nicht vollständig abrufbar (%s) – Dokumente ohne Kindkanten", exc
                )
            except Exception as exc:
                logger.warning("Kindseiten nicht abrufbar (%s) – Fallback Einzelabfragen", exc)

        for pid, item in out.items():
            if isinstance(item, Exception):
                continue
            if children_map is not None:
                for cid, ctitle in children_map.get(pid, []):
                    item.links.append(LinkTarget(
                        uid=confluence_uid(cid), kind=EDGE_PARENT_OF,
                        relation_label="parent of", source_field="children",
                        anchor_text=ctitle, evidence_url=confluence_page_url(self.ctx, cid),
                        title_hint=ctitle))
                continue
            try:
                self._add_child_links(item, max_children)
            except CrawlStopped:
                raise
            except BudgetExceeded as exc:
                logger.warning(
                    "Kinderlisten nicht vollständig abrufbar (%s) – Dokumente ohne Kindkanten", exc
                )
                break
            except Exception as exc:
                # Same for a single broken child listing (403/404/…): the page
                # itself was fetched successfully and must be kept.
                logger.warning("Kinderliste von %s nicht abrufbar: %s", pid, exc)
        return out

    def _bulk_page_payloads(self, page_ids: list[str]) -> dict[str, dict]:
        """Raw page payloads for up to 100 ids in one request, keyed by id."""
        found: dict[str, dict] = {}
        if self.api_mode == "server":
            cql = "id in (" + ",".join(page_ids) + ")"
            next_url: str | None = None
            first_params: dict[str, Any] | None = {
                "cql": cql, "limit": len(page_ids), "expand": SERVER_PAGE_EXPAND,
            }
            for _ in range(5):  # bounded: at most `limit` results are expected
                if len(found) >= len(page_ids):
                    break
                if next_url:
                    data = self.http.get_json(next_url)
                elif first_params is not None:
                    data = self.http.get_json("/rest/api/content/search", first_params)
                else:
                    break
                if not isinstance(data, dict):
                    raise InvalidResponseError("Bulk-Seitenabruf: unerwartete Antwort")
                results = data.get("results") or []
                for item in results:
                    if isinstance(item, dict) and str(item.get("id") or "").isdigit():
                        found[str(item["id"])] = item
                nxt = (data.get("_links") or {}).get("next")
                if nxt and results and len(found) < len(page_ids):
                    next_url = self._resolve(nxt)
                    first_params = None
                else:
                    break
            return found

        query = urllib.parse.urlencode(
            [("id", pid) for pid in page_ids]
            + [("body-format", "storage"), ("limit", str(len(page_ids)))]
        )
        next_url = f"/api/v2/pages?{query}"
        for _ in range(5):  # bounded cursor pagination
            if len(found) >= len(page_ids):
                break
            data = self.http.get_json(next_url)
            if not isinstance(data, dict):
                raise InvalidResponseError("Bulk-Seitenabruf: unerwartete Antwort")
            results = data.get("results") or []
            for item in results:
                if isinstance(item, dict) and str(item.get("id") or "").isdigit():
                    found[str(item["id"])] = item
            nxt = (data.get("_links") or {}).get("next")
            if nxt and results and len(found) < len(page_ids):
                next_url = self._resolve(nxt)
            else:
                break
        return found

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

    def list_space_pages(self, space_key: str, max_items: int = 100_000) -> list[tuple[str, str]]:
        """All pages (id, title) in a space, paginated within the request budget."""
        items = self._list_space_page_items(space_key, max_items)
        return [(str(item["id"]), str(item.get("title") or "")) for item in items]

    def list_space_pages_with_parents(
        self, space_key: str, max_items: int = 100_000
    ) -> list[tuple[str, str, str]]:
        """All pages (id, title, direct_parent_id) of a space.

        Uses `expand=ancestors` so callers can build parent/child edges from a
        single listing instead of one child request per page. `direct_parent_id`
        is empty for root pages or when the API omits the ancestors.
        """
        items = self._list_space_page_items(space_key, max_items, expand="ancestors")
        out: list[tuple[str, str, str]] = []
        for item in items:
            ancestors = [a for a in (item.get("ancestors") or []) if isinstance(a, dict)]
            parent_id = str(ancestors[-1].get("id") or "") if ancestors else ""
            out.append((str(item["id"]), str(item.get("title") or ""), parent_id))
        return out

    def enrich_children(self, doc: FetchedDocument, max_children: int = 500) -> None:
        """Add `parent_of` links to an already fetched document (one request)."""
        self._add_child_links(doc, max_children)

    def list_children_bulk(
        self, page_ids: list[str], max_children: int = 500, group_size: int = 25,
    ) -> dict[str, list[tuple[str, str]]]:
        """Direct children (id, title) for several pages with few requests.

        Uses CQL `type=page and ancestor in (id1, id2, …)` and derives each
        result's direct parent from its ancestor chain. Groups whose answer may
        be truncated (>=100 matches) or unsupported fall back to one request
        per page, so the result is always exact.
        """
        unique: list[str] = []
        for raw in page_ids:
            pid = str(raw).strip()
            if pid.isdigit() and pid not in unique:
                unique.append(pid)
        out: dict[str, list[tuple[str, str]]] = {pid: [] for pid in unique}
        if not unique:
            return out

        claimed_unsupported = getattr(self, "_children_cql_supported", None) is False
        step = max(1, min(int(group_size), 25))
        for start in range(0, len(unique), step):
            group = unique[start:start + step]
            results: list[dict] | None = None
            if not claimed_unsupported:
                cql = "type=page and ancestor in (" + ",".join(group) + ")"
                try:
                    data = self.http.get_json(
                        "/rest/api/content/search",
                        {"cql": cql, "limit": 100, "expand": "ancestors"},
                    )
                    results = [r for r in ((data or {}).get("results") or [])
                               if isinstance(r, dict)]
                except (CrawlStopped, BudgetExceeded):
                    raise
                except Exception as exc:
                    # Endpoint/Version unterstützt die CQL-Abfrage nicht
                    self._children_cql_supported = False
                    logger.info("Kindseiten-CQL nicht verfügbar (%s) – Einzelabfragen", exc)
                    results = None

            if results is None or len(results) >= 100:
                # truncated/unsupported -> exakt per Einzelabfrage
                for pid in group:
                    try:
                        out[pid] = self.list_child_pages(pid, max_children)
                    except (CrawlStopped, BudgetExceeded):
                        raise
                    except Exception as exc:
                        # Seite gelöscht/gesperrt -> keine Kindkanten
                        logger.info("Kinder von %s nicht abrufbar: %s", pid, exc)
                        out[pid] = []
                continue
            for item in results:
                ancestors = [a for a in (item.get("ancestors") or []) if isinstance(a, dict)]
                parent_id = str(ancestors[-1].get("id") or "") if ancestors else ""
                cid = str(item.get("id") or "")
                if parent_id in out and cid.isdigit() and len(out[parent_id]) < max_children:
                    out[parent_id].append((cid, str(item.get("title") or "")))
        return out

    def _list_space_page_items(
        self, space_key: str, max_items: int, *, expand: str = ""
    ) -> list[dict]:
        """Raw page payloads of a space, paginated within the request budget."""
        space_key = (space_key or "").strip()
        if not space_key:
            return []
        limit = min(CHILD_PAGE_SIZE, max_items)
        q_space = urllib.parse.quote(space_key)
        suffix = f"&expand={urllib.parse.quote(expand)}" if expand else ""
        next_url: str | None = self._resolve(
            f"/rest/api/content?spaceKey={q_space}&type=page&limit={limit}&start=0{suffix}"
        )
        out: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while next_url and len(out) < max_items:
            data = self.http.get_json(next_url)
            if not isinstance(data, dict):
                raise InvalidResponseError(f"Seitenliste von Space '{space_key}': unerwartete Antwort")
            results = data.get("results") or []
            offset = int(data.get("start") or offset) + len(results)
            for item in results:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "page") != "page":
                    continue
                pid = str(item.get("id") or "")
                if pid.isdigit() and pid not in seen:
                    seen.add(pid)
                    out.append(item)
                    if len(out) >= max_items:
                        break
            nxt = (data.get("_links") or {}).get("next")
            if not nxt and len(results) >= limit:
                nxt = (f"/rest/api/content?spaceKey={q_space}&type=page&limit={limit}"
                       f"&start={offset}{suffix}")
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

    def resolve_titles_bulk(
        self, pairs: list[tuple[str, str]], chunk_size: int = 50,
    ) -> dict[tuple[str, str], str | None]:
        """Resolve several (space, title) pairs with few requests (CQL per space).

        One request covers up to `chunk_size` titles of the same space
        (`type=page and title in ("A","B") and space in ("KEY")`). Titles that
        cannot be embedded into CQL safely are resolved individually; ambiguous
        (0 or >1 match) or truncated results fall back to single lookups.
        """
        out: dict[tuple[str, str], str | None] = {}
        by_space: dict[str, list[str]] = {}
        for raw_space, raw_title in pairs:
            space = (raw_space or "").strip()
            title = (raw_title or "").strip()
            key = (space, title)
            if key in out:
                continue
            if not title or '"' in title or "\\" in title or '"' in space or "\\" in space:
                out[key] = self.resolve_title(space, title)
                continue
            by_space.setdefault(space, []).append(title)

        step = max(1, min(int(chunk_size), 50))
        for space, titles in by_space.items():
            for start in range(0, len(titles), step):
                chunk = titles[start:start + step]
                quoted = ", ".join(f'"{t}"' for t in chunk)
                cql = f"type=page and title in ({quoted})"
                if space:
                    cql += f' and space in ("{space}")'
                limit = min(100, len(chunk) * 4)
                data = self.http.get_json(
                    "/rest/api/content/search",
                    {"cql": cql, "limit": limit, "expand": "space"},
                )
                results = [r for r in ((data or {}).get("results") or []) if isinstance(r, dict)]
                if len(results) >= limit:
                    # Antwort evtl. abgeschnitten -> korrektheit geht vor Tempo
                    for title in chunk:
                        out[(space, title)] = self.resolve_title(space, title)
                    continue
                hits: dict[str, list[str]] = {}
                for item in results:
                    pid = str(item.get("id") or "")
                    if pid.isdigit():
                        hits.setdefault(str(item.get("title") or ""), []).append(pid)
                for title in chunk:
                    ids = hits.get(title, [])
                    out[(space, title)] = ids[0] if len(ids) == 1 else None
        return out

    # ------------------------------------------------------------ discovery

    def _space_url(self, space_key: str) -> str:
        base = self.http.base_url.rstrip("/")
        quoted = urllib.parse.quote(space_key)
        if self.api_mode == "server":
            return f"{base}/display/{quoted}"
        return f"{base}/spaces/{quoted}"

    def list_spaces(self, max_items: int = 2000) -> list[dict]:
        """All spaces visible to the configured account.

        Returns dicts with key, name, type, id and a browsable URL, sorted by
        key. Server/DC uses `/rest/api/space`, Cloud `/api/v2/spaces`.
        """
        limit = max(1, min(SPACE_PAGE_SIZE, max_items))
        if self.api_mode == "server":
            next_url: str | None = self._resolve(f"/rest/api/space?limit={limit}&start=0")
        else:
            next_url = self._resolve(f"/api/v2/spaces?limit={limit}")

        out: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while next_url and len(out) < max_items:
            data = self.http.get_json(next_url)
            if not isinstance(data, dict):
                raise InvalidResponseError("Spaces: unerwartete Antwort")
            results = data.get("results") or []
            offset = int(data.get("start") or offset) + len(results)
            for item in results:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "key": key,
                    "name": str(item.get("name") or "").strip() or key,
                    "type": str(item.get("type") or "").strip(),
                    "id": str(item.get("id") or "").strip(),
                    "url": self._space_url(key),
                })
                if len(out) >= max_items:
                    break
            nxt = (data.get("_links") or {}).get("next")
            if not nxt and self.api_mode == "server" and len(results) >= limit:
                nxt = f"/rest/api/space?limit={limit}&start={offset}"
            next_url = self._resolve(nxt) if nxt and results else None

        out.sort(key=lambda s: (s["name"].casefold(), s["key"]))
        return out

    def list_pages_modified_since(
        self,
        since: str,
        space_keys: list[str] | None = None,
        max_items: int = 10_000,
    ) -> list[dict]:
        """Pages with `lastmodified >= since` (inclusive), newest first.

        `since` is an ISO date (YYYY-MM-DD). Uses the CQL search endpoint that
        exists on both Server/DC and Cloud. Returns dicts with id, title, space
        and updated_at.
        """
        since = (since or "").strip()
        if not since:
            return []
        cql = f'type=page and lastmodified >= "{since}"'
        keys = []
        for raw_key in space_keys or []:
            key = str(raw_key).strip()
            if not key:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.~:@-]+", key):
                # Keys are interpolated into CQL; never pass through quotes/backslashes.
                raise ValueError(f"Ungültiger Space-Key für die CQL-Suche: {raw_key!r}")
            keys.append(key)
        if keys:
            quoted = ", ".join(f'"{k}"' for k in keys)
            cql += f" and space in ({quoted})"
        cql += " order by lastmodified desc"

        limit = max(1, min(SEARCH_PAGE_SIZE, max_items))
        next_url: str | None = None
        first_path = "/rest/api/content/search"
        first_params: dict[str, Any] | None = {
            "cql": cql, "limit": limit, "expand": "version,space",
        }

        out: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while len(out) < max_items:
            if next_url:
                data = self.http.get_json(next_url)
            elif first_params is not None:
                data = self.http.get_json(first_path, first_params)
            else:
                break
            if not isinstance(data, dict):
                raise InvalidResponseError(
                    f"Suche nach geänderten Seiten (seit {since}): unerwartete Antwort"
                )
            results = data.get("results") or []
            offset = int(data.get("start") or offset) + len(results)
            for item in results:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "page") != "page":
                    continue
                pid = str(item.get("id") or "")
                if not pid.isdigit() or pid in seen:
                    continue
                seen.add(pid)
                out.append({
                    "id": pid,
                    "title": str(item.get("title") or "").strip(),
                    "space": str((item.get("space") or {}).get("key") or "").strip(),
                    "updated_at": str((item.get("version") or {}).get("when") or "").strip(),
                })
                if len(out) >= max_items:
                    break
            nxt = (data.get("_links") or {}).get("next")
            if not nxt and self.api_mode == "server" and len(results) >= limit:
                nxt = (f"{first_path}?cql={urllib.parse.quote(cql)}"
                       f"&limit={limit}&expand={urllib.parse.quote('version,space')}&start={offset}")
            if nxt and results:
                next_url = self._resolve(nxt)
                first_params = None
            else:
                break
        return out

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
