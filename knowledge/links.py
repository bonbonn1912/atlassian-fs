"""Link classification, URL normalization, and issue key handling.

Python standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING
import urllib.parse
from urllib.parse import parse_qs, unquote_plus, urlsplit, urlunsplit

from knowledge.models import confluence_uid, is_document_uid, jira_uid, unresolved_page_uid

# Issue key pattern: e.g. PROJ-123, ABC_DEF-99
ISSUE_KEY_RE: re.Pattern = re.compile(r"\b[A-Z][A-Z0-9_]+-[1-9][0-9]*\b")


def normalize_issue_key(key: str | None) -> str | None:
    """Normalize issue key to uppercase and validate against ISSUE_KEY_RE.

    Returns normalized key string or None if invalid.
    """
    if not key or not isinstance(key, str):
        return None
    k = key.strip().upper()
    if re.fullmatch(r"[A-Z][A-Z0-9_]+-[1-9][0-9]*", k):
        return k
    return None


@dataclass(frozen=True)
class LinkContext:
    """Context for link classification and URL resolution."""

    confluence_bases: tuple[str, ...]
    jira_bases: tuple[str, ...]
    confluence_base: str
    jira_base: str
    project_keys: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, cfg: dict) -> LinkContext:
        """Construct LinkContext from config dict."""
        conf_cfg = cfg.get("confluence") or {}
        jira_cfg = cfg.get("jira") or {}

        c_base = (conf_cfg.get("base_url") or "").strip().rstrip("/")
        c_aliases = [
            a.strip().rstrip("/")
            for a in conf_cfg.get("aliases") or []
            if a and isinstance(a, str) and a.strip()
        ]
        c_bases_list = []
        for b in [c_base] + c_aliases:
            if b and b not in c_bases_list:
                c_bases_list.append(b)

        j_base = (jira_cfg.get("base_url") or "").strip().rstrip("/")
        j_aliases = [
            a.strip().rstrip("/")
            for a in jira_cfg.get("aliases") or []
            if a and isinstance(a, str) and a.strip()
        ]
        j_bases_list = []
        for b in [j_base] + j_aliases:
            if b and b not in j_bases_list:
                j_bases_list.append(b)

        raw_keys = jira_cfg.get("project_keys") or []
        p_keys = frozenset(
            k.strip().upper()
            for k in raw_keys
            if k and isinstance(k, str) and k.strip()
        )

        return cls(
            confluence_bases=tuple(c_bases_list),
            jira_bases=tuple(j_bases_list),
            confluence_base=c_base,
            jira_base=j_base,
            project_keys=p_keys,
        )


def _norm_netloc(netloc: str, scheme: str) -> str:
    """Normalize netloc (lowercase, remove default ports)."""
    netloc = netloc.lower()
    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            return host
    return netloc


def classify_url(href: str, ctx: LinkContext) -> tuple[str, str] | None:
    """Classify a URL as Confluence or Jira link.

    Resolves relative URLs against ctx.confluence_base.
    Ignores non-http(s) schemes, anchors, attachments (/download/...), and foreign hosts.
    Longest matching base URL wins if hosts overlap.

    Returns:
        (target_uid, normalized_evidence_url) or None if not recognized.
    """
    if not href or not isinstance(href, str):
        return None

    href = href.strip()
    if not href:
        return None

    # Ignore anchors, mailto, javascript, tel, data
    if href.startswith(("#", "mailto:", "javascript:", "tel:", "data:")):
        return None

    parsed = urlsplit(href)

    # Relative URL handling. Root-relative hrefs are tried against the host root
    # first (HTML semantics, e.g. "/browse/ABC-1" on a shared Cloud host) and then
    # against the Confluence base incl. context path (REST "_links.webui" style).
    if not parsed.scheme and not parsed.netloc:
        if not ctx.confluence_base:
            return None
        base_parsed = urlsplit(ctx.confluence_base)
        if href.startswith("/"):
            candidates = [f"{base_parsed.scheme}://{base_parsed.netloc}{href}",
                          f"{ctx.confluence_base}{href}"]
        else:
            candidates = [f"{ctx.confluence_base}/{href}"]
        for candidate in candidates:
            result = _classify_absolute(urlsplit(candidate), ctx)
            if result:
                return result
        return None
    return _classify_absolute(parsed, ctx)


def _classify_absolute(parsed, ctx: LinkContext) -> tuple[str, str] | None:
    if parsed.scheme.lower() not in ("http", "https"):
        return None

    u_netloc = _norm_netloc(parsed.netloc, parsed.scheme.lower())
    u_path = parsed.path

    # Match against configured bases
    # Store: (length_of_base_path, system_type, base_path)
    matches: list[tuple[int, str, str]] = []

    for b in ctx.confluence_bases:
        bp = urlsplit(b)
        bn = _norm_netloc(bp.netloc, bp.scheme.lower())
        if bn == u_netloc:
            bpath = bp.path.rstrip("/")
            if not bpath or u_path == bpath or u_path.startswith(bpath + "/"):
                matches.append((len(bpath), "confluence", bpath))

    for b in ctx.jira_bases:
        bp = urlsplit(b)
        bn = _norm_netloc(bp.netloc, bp.scheme.lower())
        if bn == u_netloc:
            bpath = bp.path.rstrip("/")
            if not bpath or u_path == bpath or u_path.startswith(bpath + "/"):
                matches.append((len(bpath), "jira", bpath))

    if not matches:
        return None  # Foreign host

    # Longest match wins
    matches.sort(key=lambda m: m[0], reverse=True)
    _, system_type, b_path = matches[0]

    rel_path = u_path[len(b_path):]
    if not rel_path.startswith("/"):
        rel_path = "/" + rel_path

    # Ignore attachments
    if "/download/" in rel_path or rel_path.startswith("/download"):
        return None

    # Normalized evidence URL (strip fragment, normalize path)
    norm_path = u_path.rstrip("/") if len(u_path) > 1 else u_path
    evidence_url = urlunsplit(
        (parsed.scheme.lower(), u_netloc, norm_path, parsed.query, "")
    )

    if system_type == "confluence":
        qs = parse_qs(parsed.query)
        # 1. viewpage.action?pageId=123 / editpage.action?pageId=123
        if "pageId" in qs and qs["pageId"] and qs["pageId"][0].isdigit():
            return confluence_uid(qs["pageId"][0]), evidence_url

        # 2. /spaces/SPACE/pages/123(/slug) or /wiki/spaces/SPACE/pages/123(/slug)
        m = re.match(r"^(?:/wiki)?/spaces/[^/]+/pages/(\d+)(?:/.*)?$", rel_path)
        if m:
            return confluence_uid(m.group(1)), evidence_url

        # 3. /pages/123(/slug)
        m = re.match(r"^/pages/(\d+)(?:/.*)?$", rel_path)
        if m:
            return confluence_uid(m.group(1)), evidence_url

        # 4. /display/SPACE/Title or /wiki/display/SPACE/Title
        m = re.match(r"^(?:/wiki)?/display/([^/]+)/([^/?#]+)", rel_path)
        if m:
            space = m.group(1).strip().upper()
            raw_title = unquote_plus(m.group(2))
            title = " ".join(raw_title.split())
            if title:
                return unresolved_page_uid(space, title), evidence_url

        # 5. /display/SPACE or /spaces/SPACE or /wiki/spaces/SPACE (space root)
        m = re.match(r"^(?:/wiki)?/(?:display|spaces)/([^/?#]+)/?$", rel_path)
        if m:
            space = m.group(1).strip()  # case-sensitive for personal spaces
            if space:
                return f"space:{space}", evidence_url

        return None

    elif system_type == "jira":
        qs = parse_qs(parsed.query)
        # 1. selectedIssue=KEY-1
        if "selectedIssue" in qs and qs["selectedIssue"]:
            k = normalize_issue_key(qs["selectedIssue"][0])
            if k:
                return jira_uid(k), evidence_url

        # 2. /browse/KEY-1
        m = re.match(r"^/browse/([A-Za-z][A-Za-z0-9_]+-[1-9][0-9]*)(?:/.*)?$", rel_path)
        if m:
            return jira_uid(m.group(1)), evidence_url

        # 3. /projects/PROJ/issues/KEY-1
        m = re.match(r"^/projects/[^/]+/issues/([A-Za-z][A-Za-z0-9_]+-[1-9][0-9]*)(?:/.*)?$", rel_path)
        if m:
            return jira_uid(m.group(1)), evidence_url

        # 4. Trailing issue key segment in Jira path (e.g. /jira/software/c/projects/.../boards/1?modal=detail&selectedIssue=... or path segment)
        for seg in reversed(rel_path.strip("/").split("/")):
            k = normalize_issue_key(seg)
            if k:
                return jira_uid(k), evidence_url

        return None

    return None


def classify_document_url(href: str, ctx: LinkContext) -> tuple[str, str] | None:
    """Like classify_url, but only for fetchable documents.

    Space root URLs (`/display/ENG`, `/spaces/ENG`) classify to the pseudo uid
    `space:ENG`, which is only meaningful as a start page – never as a graph
    node. Those (and any other non-document uid) are dropped here.
    """
    classified = classify_url(href, ctx)
    if not classified or not is_document_uid(classified[0]):
        return None
    return classified


def parse_start_page(value: str, ctx: LinkContext) -> str | None:
    """Parse start_page parameter.

    Accepts:
    - digits -> confluence:<id>
    - Confluence URL -> classify_url(...)[0]
    - confluence:... as-is
    - space:... as-is
    - invalid -> None
    """
    if not value or not isinstance(value, str):
        return None

    val = value.strip()
    if not val:
        return None

    if val.isdigit():
        return confluence_uid(val)

    if val.startswith("space:"):
        # Case is preserved: personal space keys ("~jsmith") are case-sensitive.
        rest = val.split(":", 1)[1].strip()
        if rest:
            return f"space:{rest}"
        return None

    if val.startswith("confluence:"):
        rest = val.split(":", 1)[1]
        if rest.isdigit() or rest.startswith("unresolved:"):
            return val
        return None

    classified = classify_url(val, ctx)
    if classified and (classified[0].startswith("confluence:") or classified[0].startswith("space:")):
        return classified[0]

    return None


def confluence_page_url(ctx: LinkContext, page_id: str) -> str:
    """Canonical Confluence page URL for given page_id."""
    pid = str(page_id).strip()
    return f"{ctx.confluence_base}/pages/viewpage.action?pageId={pid}"


def jira_issue_url(ctx: LinkContext, key: str) -> str:
    """Canonical Jira issue URL for given issue key."""
    k = key.strip().upper()
    return f"{ctx.jira_base}/browse/{k}"


def find_plain_issue_keys(text: str, ctx: LinkContext) -> list[str]:
    """Find issue keys in plain text.

    Scans text with ISSUE_KEY_RE. If ctx.project_keys is configured, only matching
    prefixes are returned. Results are uppercase and deduplicated preserving order.
    """
    if not text or not isinstance(text, str):
        return []

    found = ISSUE_KEY_RE.findall(text)
    out: list[str] = []
    seen: set[str] = set()

    for raw_k in found:
        k = raw_k.upper()
        if k in seen:
            continue
        if ctx.project_keys:
            proj = k.split("-", 1)[0]
            if proj not in ctx.project_keys:
                continue
        seen.add(k)
        out.append(k)

    return out
