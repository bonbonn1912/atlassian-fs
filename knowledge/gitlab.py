"""GitLab REST API v4 client: commits of configured projects -> FetchedDocuments.

Works with self-managed GitLab and GitLab.com (same `/api/v4` API). The
personal access token (scope `read_api`) is sent as `PRIVATE-TOKEN` header,
never in the URL. All requests go through `RateLimitedClient`, so stop events,
request/time budgets, rate limiting (429 + Retry-After) and the host allow-list
apply exactly as for Confluence/Jira.

Commit messages are scanned for Jira keys with the configured prefixes
(`API-2323`); each hit becomes a `mentions_jira` edge to `jira:API-2323`. That
node is either an already indexed Jira issue or a reference node.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import urllib.parse
from typing import Any, Callable, Iterator, Sequence

from .atlassian_http import (
    RateLimitedClient,
    RequestBudget,
    build_gitlab_headers,
    default_transport,
    make_ssl_context,
)
from .config import normalize_gitlab_projects
from .models import (
    AtlassianError,
    EDGE_MENTIONS_JIRA,
    FetchedDocument,
    ForbiddenError,
    InvalidResponseError,
    LinkTarget,
    NotFoundError,
    gitlab_commit_uid,
    jira_uid,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v4"
COMMITS_PAGE_SIZE = 100          # GitLab maximum for offset pagination
MAX_COMMIT_PAGES = 100_000       # hard stop against broken pagination
PROJECTS_PAGE_SIZE = 100
MAX_PROJECT_PAGES = 20           # project picker: at most 2000 projects
REPORTER_ACCESS_LEVEL = 20       # lowest role that can read commits of private projects
MAX_TEST_PROJECTS = 50
DOCUMENT_TYPE = "commit"
SOURCE_FIELD = "commit_message"
# Hosts with publicly trusted certificates: TLS is always verified there, even
# if verification is disabled for internal (self-signed) instances.
PUBLIC_GITLAB_HOSTS = frozenset({"gitlab.com", "www.gitlab.com"})

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)


# --------------------------------------------------------------------------- keys

def issue_key_pattern(prefixes: Sequence[str]) -> re.Pattern | None:
    """Regex for `PREFIX-123` with Jira-like word boundaries; None if no prefixes.

    Case-sensitive on purpose: `api-1` or `UTF-8` must not create edges.
    """
    clean = sorted({p.strip().upper().rstrip("-") for p in prefixes if p and p.strip()},
                   key=lambda p: (-len(p), p))
    if not clean:
        return None
    alternatives = "|".join(re.escape(p) for p in clean)
    return re.compile(rf"(?<![A-Za-z0-9_])((?:{alternatives})-[1-9][0-9]{{0,9}})(?![A-Za-z0-9_])")


def find_issue_keys(text: str, pattern: re.Pattern | None) -> list[str]:
    """Unique issue keys in order of first appearance."""
    if not text or pattern is None:
        return []
    out: list[str] = []
    for match in pattern.finditer(text):
        key = match.group(1)
        if key not in out:
            out.append(key)
    return out


def effective_prefixes(cfg: dict) -> list[str]:
    """gitlab.issue_prefixes, falling back to jira.project_keys."""
    prefixes = (cfg.get("gitlab") or {}).get("issue_prefixes") or []
    if not prefixes:
        prefixes = (cfg.get("jira") or {}).get("project_keys") or []
    return [str(p).strip().upper() for p in prefixes if str(p).strip()]


def scan_signature(cfg: dict) -> str:
    """Fingerprint of everything that decides which commits/edges get indexed.

    Stored with each sync checkpoint; a different signature forces a full
    (non-incremental) scan so changed prefixes backfill older commits.
    """
    gl = cfg.get("gitlab") or {}
    payload = {
        "prefixes": sorted(set(effective_prefixes(cfg))),
        "require_issue_key": bool(gl.get("require_issue_key", True)),
        "ref": "*" if gl.get("all_branches") else str(gl.get("ref_name") or ""),
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def ref_key(cfg: dict) -> str:
    gl = cfg.get("gitlab") or {}
    return "*" if gl.get("all_branches") else str(gl.get("ref_name") or "")


# --------------------------------------------------------------------------- http

def gitlab_verify_ssl(cfg: dict) -> bool:
    base = ((cfg.get("gitlab") or {}).get("base_url") or "").strip()
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    if host in PUBLIC_GITLAB_HOSTS:
        return True
    return bool((cfg.get("http") or {}).get("verify_ssl", False))


def build_gitlab_http(
    cfg: dict,
    *,
    stop_event: threading.Event,
    budget: RequestBudget,
    sleep_seconds: float | None = None,
    timeout: float = 20.0,
    max_retries: int = 4,
    on_event: Callable[[dict], None] | None = None,
    transport: Any = None,
) -> RateLimitedClient:
    """RateLimitedClient for the configured GitLab instance (source 'gitlab')."""
    gl = cfg.get("gitlab") or {}
    http_cfg = cfg.get("http") or {}
    base = (gl.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise AtlassianError("GitLab-Basis-URL ist nicht konfiguriert")
    ca_bundle = str(http_cfg.get("ca_bundle", "") or "")
    verify = gitlab_verify_ssl(cfg)
    return RateLimitedClient(
        source="gitlab",
        base_url=base,
        allowed_bases=[base],
        sleep_seconds=float(gl.get("sleep_seconds", 0.5) if sleep_seconds is None else sleep_seconds),
        timeout=float(timeout),
        max_retries=int(max_retries),
        stop_event=stop_event,
        budget=budget,
        ssl_context=make_ssl_context(verify, ca_bundle if verify else ""),
        auth_headers=build_gitlab_headers(gl.get("token")),
        user_agent=str(http_cfg.get("user_agent", "local-knowledge/1.0")),
        max_response_bytes=int(http_cfg.get("max_response_mb", 20)) * 1024 * 1024,
        transport=transport or default_transport(bool(http_cfg.get("use_system_proxy", True))),
        on_event=on_event,
    )


def _project_path_param(project_ref: str) -> str:
    """Numeric IDs stay as-is, namespace paths are URL-encoded (`a%2Fb`)."""
    return urllib.parse.quote(str(project_ref).strip(), safe="")


def _next_page(headers: dict[str, str], page: int, count: int) -> int | None:
    """Next page number from x-next-page, the Link header or the page size."""
    raw = (headers.get("x-next-page") or "").strip()
    if raw:
        try:
            nxt = int(raw)
        except ValueError:
            nxt = 0
        return nxt if nxt > page else None
    if "x-next-page" in headers:      # present but empty = last page
        return None
    match = _LINK_NEXT_RE.search(headers.get("link") or "")
    if match:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(match.group(1)).query)
        try:
            nxt = int((query.get("page") or ["0"])[0])
        except ValueError:
            nxt = 0
        return nxt if nxt > page else None
    # Proxies may strip pagination headers: a full page means "maybe more".
    return page + 1 if count >= COMMITS_PAGE_SIZE else None


# --------------------------------------------------------------------------- client

class GitLabClient:
    """Read-only access to projects and commits of a GitLab instance."""

    def __init__(self, http: RateLimitedClient, cfg: dict, index_cfg: dict | None = None):
        self.http = http
        self.cfg = cfg
        gl = cfg.get("gitlab") or {}
        self.base_url = (gl.get("base_url") or "").strip().rstrip("/")
        self.prefixes = effective_prefixes(cfg)
        self.key_pattern = issue_key_pattern(self.prefixes)
        self.require_issue_key = bool(gl.get("require_issue_key", True))
        self.ref_name = str(gl.get("ref_name") or "")
        self.all_branches = bool(gl.get("all_branches", False))
        self.max_document_chars = int((index_cfg or cfg.get("index") or {}).get(
            "max_document_chars", 2_000_000))

    # ------------------------------------------------------------ projects
    def project_refs(self) -> list[str]:
        return normalize_gitlab_projects(self.cfg.get("gitlab") or {})

    def get_project(self, project_ref: str) -> dict:
        data = self.http.get_json(f"{API_PREFIX}/projects/{_project_path_param(project_ref)}")
        if not isinstance(data, dict) or data.get("id") in (None, ""):
            raise InvalidResponseError(f"Ungültige Projektantwort für {project_ref!r}")
        return {
            "id": str(data["id"]),
            "path_with_namespace": str(data.get("path_with_namespace") or project_ref),
            "name": str(data.get("name_with_namespace") or data.get("name") or project_ref),
            "web_url": str(data.get("web_url") or ""),
            "default_branch": str(data.get("default_branch") or ""),
        }

    def list_accessible_projects(
        self,
        *,
        min_access_level: int = REPORTER_ACCESS_LEVEL,
        max_pages: int = MAX_PROJECT_PAGES,
    ) -> dict:
        """Projects the token's user is a member of with at least `min_access_level`.

        Reporter (20) is the lowest role that may read the repository (commits)
        of a private project. Without a token GitLab would list every public
        project of the instance, so a token is required.
        """
        if not str((self.cfg.get("gitlab") or {}).get("token") or "").strip():
            raise AtlassianError("Zum Laden der Projekte wird ein Token benötigt "
                                 "(öffentliche Projekte lassen sich direkt als Pfad eintragen)")
        params: dict[str, Any] = {
            "membership": True,
            "min_access_level": int(min_access_level),
            "archived": False,
            "simple": True,
            "order_by": "path",
            "sort": "asc",
            "per_page": PROJECTS_PAGE_SIZE,
        }
        projects: list[dict] = []
        seen: set[str] = set()
        page = 1
        for _ in range(max_pages):
            data, headers = self.http.get_json_with_headers(
                f"{API_PREFIX}/projects", params={**params, "page": page})
            if not isinstance(data, list):
                raise InvalidResponseError("Projekt-Liste ist kein JSON-Array")
            for item in data:
                if not isinstance(item, dict) or item.get("id") in (None, ""):
                    continue
                pid = str(item["id"])
                if pid in seen:
                    continue
                seen.add(pid)
                projects.append({
                    "id": pid,
                    "path": str(item.get("path_with_namespace") or pid),
                    "name": str(item.get("name_with_namespace") or item.get("name") or pid),
                    "web_url": str(item.get("web_url") or ""),
                    "visibility": str(item.get("visibility") or ""),
                    "last_activity_at": str(item.get("last_activity_at") or ""),
                })
            if not data:
                break
            nxt = _next_page({k.lower(): v for k, v in (headers or {}).items()}, page, len(data))
            if nxt is None:
                break
            page = nxt
        else:
            return {"projects": projects, "truncated": True}
        return {"projects": projects, "truncated": False}

    # ------------------------------------------------------------ commits
    def iter_commit_pages(
        self,
        project_id: str,
        *,
        since: str = "",
        until: str = "",
        max_pages: int = MAX_COMMIT_PAGES,
    ) -> Iterator[list[dict]]:
        """Yield commit pages (newest first) until the listing is exhausted.

        The generator returning normally means the listing is complete; budget,
        stop and API errors propagate as exceptions. Pagination always uses our
        own URL with `page=N` (from `x-next-page`/`Link`), so absolute Link URLs
        of a differently named external_url never bypass the host allow-list.
        """
        path = f"{API_PREFIX}/projects/{_project_path_param(project_id)}/repository/commits"
        params: dict[str, Any] = {"per_page": COMMITS_PAGE_SIZE}
        if self.all_branches:
            params["all"] = True
        elif self.ref_name:
            params["ref_name"] = self.ref_name
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        page = 1
        for _ in range(max_pages):
            data, headers = self.http.get_json_with_headers(path, params={**params, "page": page})
            if not isinstance(data, list):
                raise InvalidResponseError("Commit-Liste ist kein JSON-Array")
            commits = [c for c in data if isinstance(c, dict) and _SHA_RE.match(str(c.get("id") or ""))]
            if commits:
                yield commits
            if not data:
                return
            nxt = _next_page({k.lower(): v for k, v in (headers or {}).items()}, page, len(data))
            if nxt is None:
                return
            page = nxt
        raise InvalidResponseError(f"Paginierung nach {max_pages} Seiten abgebrochen")

    def commit_to_document(self, project: dict, commit: dict) -> FetchedDocument:
        sha = str(commit.get("id") or "").lower()
        if not _SHA_RE.match(sha):
            raise InvalidResponseError("Commit ohne gültige SHA")
        project_id = str(project["id"])
        path = project.get("path_with_namespace") or project_id
        message = str(commit.get("message") or commit.get("title") or "").replace("\r\n", "\n")
        subject = str(commit.get("title") or message.split("\n", 1)[0]).strip()
        short_id = str(commit.get("short_id") or sha[:8])
        keys = find_issue_keys(message or subject, self.key_pattern)

        url = str(commit.get("web_url") or "")
        if not url.startswith(("http://", "https://")):
            web = project.get("web_url") or f"{self.base_url}/{path}"
            url = f"{web.rstrip('/')}/-/commit/{sha}"

        header_lines = [
            f"Commit {sha}",
            f"Projekt: {path}",
            f"Autor: {commit.get('author_name') or ''}".rstrip(),
            f"Datum: {commit.get('committed_date') or commit.get('created_at') or ''}".rstrip(),
        ]
        if keys:
            header_lines.append("Jira: " + ", ".join(keys))
        header = "\n".join(header_lines)
        body = message.strip()
        if len(body) > self.max_document_chars:
            body = body[: self.max_document_chars]
        sections = [("", header)]
        if body:
            sections.append(("Commit-Nachricht", body))

        links = [
            LinkTarget(uid=jira_uid(key), kind=EDGE_MENTIONS_JIRA, relation_label="mentions",
                       source_field=SOURCE_FIELD, anchor_text=key, evidence_url=url,
                       title_hint=key)
            for key in keys
        ]
        return FetchedDocument(
            uid=gitlab_commit_uid(project_id, sha),
            source_type="gitlab",
            source_id=f"{project_id}:{sha}",
            title=f"{short_id}: {subject}" if subject else short_id,
            url=url,
            body_text=header + ("\n\n" + body if body else ""),
            sections=sections,
            links=links,
            space_or_project=path,
            status_label=None,
            document_type=DOCUMENT_TYPE,
            source_version=sha,
            source_created_at=commit.get("authored_date") or None,
            source_updated_at=commit.get("committed_date") or commit.get("created_at") or None,
            labels=list(keys),
        )

    # ------------------------------------------------------------ test
    def test_connection(self, max_projects: int = MAX_TEST_PROJECTS) -> dict:
        """Check token + instance (`/version`) and up to `max_projects` projects."""
        has_token = bool(str((self.cfg.get("gitlab") or {}).get("token") or "").strip())
        try:
            version = self.http.get_json(f"{API_PREFIX}/version")
        except ForbiddenError as exc:
            if has_token:
                return {"ok": False, "message": f"{exc} (Token benötigt Scope read_api)"}
            # /version needs authentication; public projects are still readable.
            version = None
        except (AtlassianError, Exception) as exc:   # noqa: BLE001 - reported to the UI
            return {"ok": False, "message": self.http.redact(str(exc))}
        ver = str(version.get("version") or "") if isinstance(version, dict) else ""
        result: dict[str, Any] = {"ok": True, "version": ver, "projects": []}
        try:
            refs = self.project_refs()
        except Exception as exc:   # noqa: BLE001
            return {"ok": False, "version": ver, "message": str(exc)}
        problems = []
        for ref in refs[:max_projects]:
            try:
                project = self.get_project(ref)
                result["projects"].append({"ref": ref, "id": project["id"],
                                           "path": project["path_with_namespace"]})
            except NotFoundError:
                problems.append(f"{ref}: nicht gefunden oder keine Berechtigung")
            except Exception as exc:   # noqa: BLE001
                problems.append(f"{ref}: {self.http.redact(str(exc))}")
        if len(refs) > max_projects:
            result["unchecked_projects"] = len(refs) - max_projects
        if problems:
            result["ok"] = False
            result["message"] = "Projekte nicht lesbar: " + "; ".join(problems)
        else:
            result["message"] = (f"Verbunden mit GitLab {ver}".strip()
                                 + (f", {len(result['projects'])} Projekt(e) geprüft" if refs else
                                    " (keine Projekte konfiguriert)"))
            if not has_token:
                result["message"] += " – ohne Token, nur öffentliche Projekte"
        return result
