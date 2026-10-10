"""Jira REST client: Server/Data Center (REST v2, wiki markup) and Cloud v3 (ADF)."""
from __future__ import annotations

import logging
import urllib.parse

from .atlassian_http import RateLimitedClient
from .links import LinkContext, classify_url, jira_issue_url, normalize_issue_key
from .models import (
    EDGE_JIRA_PARENT_OF, EDGE_JIRA_RELATES_TO, AtlassianError, FetchedDocument,
    InvalidResponseError, LinkTarget, edge_kind_for, jira_uid, parse_uid,
)
from .text import adf_to_content, jira_wiki_to_content, truncate_text

logger = logging.getLogger(__name__)

API_MODES = ("server_v2", "cloud_v3")
JIRA_FIELDS = ("summary,description,status,issuetype,project,labels,components,"
               "created,updated,issuelinks,parent,subtasks")


class JiraClient:
    def __init__(self, http: RateLimitedClient, api_mode: str, ctx: LinkContext,
                 index_cfg: dict | None = None):
        if api_mode not in API_MODES:
            raise ValueError(f"Unbekannter Jira-API-Modus: {api_mode}")
        self.http = http
        self.api_mode = api_mode
        self.ctx = ctx
        self.max_document_chars = int((index_cfg or {}).get("max_document_chars", 2_000_000))

    @property
    def _api(self) -> str:
        return "/rest/api/2" if self.api_mode == "server_v2" else "/rest/api/3"

    def _link(self, key: str, *, kind: str, label: str, field: str, title: str = "") -> LinkTarget:
        return LinkTarget(uid=jira_uid(key), kind=kind, relation_label=label, source_field=field,
                          anchor_text=key, evidence_url=jira_issue_url(self.ctx, key),
                          title_hint=f"{key}: {title}" if title else "")

    def fetch_document(self, issue_key: str, *, include_remote_links: bool,
                       scan_plain_keys: bool = False) -> FetchedDocument:
        requested = normalize_issue_key(issue_key)
        if not requested:
            raise ValueError(f"Ungültiger Jira-Key: {issue_key!r}")
        data = self.http.get_json(f"{self._api}/issue/{urllib.parse.quote(requested)}",
                                  {"fields": JIRA_FIELDS})
        if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
            raise InvalidResponseError(f"Jira-Issue {requested}: Antwort ohne 'fields'")
        key = normalize_issue_key(str(data.get("key") or "")) or requested
        fields = data["fields"]

        summary = str(fields.get("summary") or "").strip()
        status = (fields.get("status") or {}).get("name")
        issue_type = (fields.get("issuetype") or {}).get("name")
        project = (fields.get("project") or {}).get("key") or key.split("-", 1)[0]
        labels = [str(l) for l in (fields.get("labels") or []) if l]
        components = [str(c.get("name")) for c in (fields.get("components") or [])
                      if isinstance(c, dict) and c.get("name")]

        description = fields.get("description")
        if isinstance(description, dict):
            parsed = adf_to_content(description, self.ctx, source_type="jira",
                                    scan_plain_keys=scan_plain_keys)
        else:
            parsed = jira_wiki_to_content(description if isinstance(description, str) else "",
                                          self.ctx, scan_plain_keys=scan_plain_keys)

        header_lines = [f"{key}: {summary}" if summary else key]
        if status:
            header_lines.append(f"Status: {status}")
        if issue_type:
            header_lines.append(f"Typ: {issue_type}")
        if labels:
            header_lines.append("Labels: " + ", ".join(labels))
        if components:
            header_lines.append("Komponenten: " + ", ".join(components))
        header = "\n".join(header_lines)

        sections: list[tuple[str, str]] = [("", header)]
        for heading, text in parsed.sections:
            if heading or text:
                sections.append((heading, text))
        body_text = header + ("\n\n" + parsed.plain_text if parsed.plain_text else "")
        body_text, truncated = truncate_text(body_text, self.max_document_chars)
        if truncated:
            logger.warning("Jira-Issue %s gekürzt auf %d Zeichen", key, self.max_document_chars)
            sections = [("", body_text)]

        links: list[LinkTarget] = []
        parent = fields.get("parent")
        if isinstance(parent, dict):
            pkey = normalize_issue_key(str(parent.get("key") or ""))
            if pkey:
                links.append(self._link(pkey, kind=EDGE_JIRA_PARENT_OF, label="child of", field="parent",
                                        title=str((parent.get("fields") or {}).get("summary") or "")))
        for sub in fields.get("subtasks") or []:
            if isinstance(sub, dict):
                skey = normalize_issue_key(str(sub.get("key") or ""))
                if skey:
                    links.append(self._link(skey, kind=EDGE_JIRA_PARENT_OF, label="parent of",
                                            field="subtasks",
                                            title=str((sub.get("fields") or {}).get("summary") or "")))
        for il in fields.get("issuelinks") or []:
            if not isinstance(il, dict):
                continue
            ltype = il.get("type") or {}
            if isinstance(il.get("outwardIssue"), dict):
                other, label = il["outwardIssue"], ltype.get("outward") or ltype.get("name") or "relates to"
            elif isinstance(il.get("inwardIssue"), dict):
                other, label = il["inwardIssue"], ltype.get("inward") or ltype.get("name") or "relates to"
            else:
                continue
            okey = normalize_issue_key(str(other.get("key") or ""))
            if okey:
                links.append(self._link(okey, kind=EDGE_JIRA_RELATES_TO, label=str(label),
                                        field="issuelinks",
                                        title=str((other.get("fields") or {}).get("summary") or "")))
        links.extend(parsed.links)
        if include_remote_links:
            links.extend(self._remote_links(key))

        return FetchedDocument(
            uid=jira_uid(key), source_type="jira", source_id=key,
            title=f"{key}: {summary}" if summary else key,
            url=jira_issue_url(self.ctx, key), body_text=body_text, sections=sections,
            links=_dedupe(links), space_or_project=project, status_label=status,
            document_type=issue_type, source_version=None,
            source_updated_at=fields.get("updated"), labels=labels)

    def _remote_links(self, key: str) -> list[LinkTarget]:
        """Remote links pointing to known Confluence/Jira objects. Failures are non-fatal."""
        try:
            data = self.http.get_json(f"{self._api}/issue/{urllib.parse.quote(key)}/remotelink")
        except AtlassianError as exc:  # incl. 403/404/invalid response; not Stop/Budget
            logger.info("Remote-Links für %s nicht abrufbar: %s", key, exc)
            return []
        out: list[LinkTarget] = []
        for item in data if isinstance(data, list) else []:
            obj = (item or {}).get("object") or {} if isinstance(item, dict) else {}
            url = obj.get("url")
            if not isinstance(url, str):
                continue
            classified = classify_url(url, self.ctx)
            if not classified:
                continue
            uid, evidence = classified
            if uid == jira_uid(key):
                continue
            title = str(obj.get("title") or "")
            out.append(LinkTarget(uid=uid, kind=edge_kind_for("jira", uid),
                                  relation_label=str((item.get("relationship") or "")) or "remote link",
                                  source_field="remotelink", anchor_text=title, evidence_url=evidence,
                                  title_hint=title if parse_uid(uid)[0] == "confluence" else ""))
        return out

    def test_connection(self) -> dict:
        try:
            info = self.http.get_json(f"{self._api}/serverInfo")
        except AtlassianError as exc:
            return {"ok": False, "message": str(exc), "status": exc.status}
        except Exception as exc:
            return {"ok": False, "message": str(exc) or exc.__class__.__name__}
        if not isinstance(info, dict):
            return {"ok": False, "message": "Unerwartete Antwort – ist die Basis-URL korrekt?"}
        version = info.get("version") or "?"
        deployment = info.get("deploymentType") or ""
        msg = f"Verbindung erfolgreich (Jira {version}{', ' + deployment if deployment else ''})"
        # serverInfo is often anonymous; verify the credentials separately.
        try:
            me = self.http.get_json(f"{self._api}/myself")
            user = (me or {}).get("displayName") or (me or {}).get("name") if isinstance(me, dict) else None
            if user:
                msg += f", angemeldet als {user}"
        except AtlassianError as exc:
            return {"ok": False, "message": f"Server erreichbar, aber Anmeldung fehlgeschlagen: {exc}",
                    "status": exc.status}
        return {"ok": True, "message": msg, "base_url": self.http.base_url}


def _dedupe(links: list[LinkTarget]) -> list[LinkTarget]:
    seen: set[tuple[str, str, str]] = set()
    out = []
    for link in links:
        key = (link.uid, link.kind, link.relation_label)
        if key not in seen:
            seen.add(key)
            out.append(link)
    return out
