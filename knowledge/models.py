"""Shared data classes, exceptions and UID helpers.

This module is the contract between parsers, API clients, the indexer and the
storage layer. Keep it dependency-free.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

SOURCE_TYPES = ("confluence", "jira")

# Edge kinds (spec 8.2). Edges are always stored with the *fetched* document as
# source, so a refresh can atomically replace all outgoing edges.
EDGE_LINKS_TO = "links_to"                        # Confluence -> Confluence
EDGE_PARENT_OF = "parent_of"                      # Confluence parent -> child
EDGE_MENTIONS_JIRA = "mentions_jira"              # Confluence -> Jira
EDGE_JIRA_RELATES_TO = "jira_relates_to"          # Jira -> Jira (issue links, mentions)
EDGE_JIRA_PARENT_OF = "jira_parent_of"            # Jira -> Jira (relation_label "parent of" / "child of")
EDGE_LINKS_TO_CONFLUENCE = "links_to_confluence"  # Jira -> Confluence
EDGE_KINDS = (
    EDGE_LINKS_TO, EDGE_PARENT_OF, EDGE_MENTIONS_JIRA,
    EDGE_JIRA_RELATES_TO, EDGE_JIRA_PARENT_OF, EDGE_LINKS_TO_CONFLUENCE,
)

# Document states (spec 9).
STATE_DISCOVERED = "discovered"
STATE_INDEXED = "indexed"
STATE_SKIPPED = "skipped"
STATE_MISSING = "missing"
STATE_FORBIDDEN = "forbidden"
STATE_ERROR = "error"
DOC_STATES = (STATE_DISCOVERED, STATE_INDEXED, STATE_SKIPPED,
              STATE_MISSING, STATE_FORBIDDEN, STATE_ERROR)

# Job states.
JOB_RUNNING = "running"
JOB_STOPPING = "stopping"
JOB_STOPPED = "stopped"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_INTERRUPTED = "interrupted"
JOB_ACTIVE_STATES = (JOB_RUNNING, JOB_STOPPING)

UNRESOLVED_PREFIX = "unresolved:"


# --------------------------------------------------------------------------- UIDs

def confluence_uid(page_id: str | int) -> str:
    return f"confluence:{str(page_id).strip()}"


def jira_uid(issue_key: str) -> str:
    return f"jira:{issue_key.strip().upper()}"


def unresolved_page_uid(space_key: str, title: str) -> str:
    """Reference to a Confluence page known only by space key + title.

    Such nodes are never fetched directly; the indexer may resolve them via the
    (budgeted) title resolver. Space key may be empty if unknown.
    """
    title = " ".join(title.split())
    return f"confluence:{UNRESOLVED_PREFIX}{space_key.strip().upper()}:{title}"


def parse_uid(uid: str) -> tuple[str, str]:
    """Split a UID into (source_type, source_id). Raises ValueError."""
    if not isinstance(uid, str) or ":" not in uid:
        raise ValueError(f"invalid uid: {uid!r}")
    source_type, source_id = uid.split(":", 1)
    if source_type not in SOURCE_TYPES or not source_id:
        raise ValueError(f"invalid uid: {uid!r}")
    return source_type, source_id


def is_document_uid(uid: str) -> bool:
    """True for uids of fetchable documents (Confluence/Jira).

    Pseudo uids such as `space:ENG` (a space root URL used as start page) are
    not documents and must never become graph nodes.
    """
    return isinstance(uid, str) and uid.startswith(("confluence:", "jira:"))


def is_unresolved_uid(uid: str) -> bool:
    return uid.startswith("confluence:" + UNRESOLVED_PREFIX)


def split_unresolved_uid(uid: str) -> tuple[str, str]:
    """'confluence:unresolved:SPACE:Title' -> ('SPACE', 'Title')."""
    rest = uid[len("confluence:" + UNRESOLVED_PREFIX):]
    space, _, title = rest.partition(":")
    return space, title


def edge_kind_for(source_type: str, target_uid: str) -> str:
    """Default edge kind for a plain link from a document of source_type."""
    target_type = parse_uid(target_uid)[0]
    if source_type == "confluence":
        return EDGE_MENTIONS_JIRA if target_type == "jira" else EDGE_LINKS_TO
    return EDGE_JIRA_RELATES_TO if target_type == "jira" else EDGE_LINKS_TO_CONFLUENCE


# --------------------------------------------------------------------------- data

@dataclass(frozen=True)
class LinkTarget:
    """A provable link from the fetched document to another node."""
    uid: str
    kind: str
    relation_label: str = ""
    source_field: str = ""      # e.g. "body", "ac:link", "jira-macro", "issuelinks", "remotelink"
    anchor_text: str = ""
    evidence_url: str = ""      # original href / URL the link was derived from
    title_hint: str = ""        # title for reference nodes if known


@dataclass(frozen=True)
class Chunk:
    heading: str
    body: str


@dataclass
class FetchedDocument:
    """A fully fetched and parsed Confluence page or Jira issue."""
    uid: str
    source_type: str
    source_id: str
    title: str
    url: str
    body_text: str
    sections: list[tuple[str, str]] = field(default_factory=list)  # (heading, text)
    links: list[LinkTarget] = field(default_factory=list)
    space_or_project: str | None = None
    status_label: str | None = None
    document_type: str | None = None
    source_version: str | None = None
    source_updated_at: str | None = None
    labels: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for part in (self.title, self.body_text, self.status_label or "",
                     self.document_type or "", ",".join(self.labels)):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\x00")
        return h.hexdigest()


# --------------------------------------------------------------------------- errors

class KnowledgeError(Exception):
    """Base class for all application errors."""


class ConfigError(KnowledgeError):
    pass


class CrawlStopped(KnowledgeError):
    """Stop event was set; no further requests may be started."""


class BudgetExceeded(KnowledgeError):
    """Request or time budget of the job is exhausted."""


class HostNotAllowed(KnowledgeError):
    """A URL outside the configured Atlassian hosts was about to be requested."""


class AtlassianError(KnowledgeError):
    """HTTP/API level error. status is None for network errors/timeouts."""

    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class NotFoundError(AtlassianError):
    pass


class ForbiddenError(AtlassianError):
    """HTTP 401 or 403."""


class InvalidResponseError(AtlassianError):
    """Response is not JSON, too large, or lacks required fields."""
