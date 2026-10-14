"""Configuration loading, validation, persistence and job parameter checks."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from .models import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

CONFLUENCE_API_MODES = ("server", "cloud_v2")   # server = Server/Data Center REST /rest/api/content
JIRA_API_MODES = ("server_v2", "cloud_v3")      # server_v2 = Server/Data Center REST /rest/api/2
AUTH_TYPES = ("none", "bearer", "basic")
GITLAB_START_UID = "gitlab-sync"                 # crawl_jobs.start_page_uid of GitLab-only jobs
GITLAB_MAX_PROJECTS = 200
MCP_OUTPUT_MIN_CHARS = 100
MCP_OUTPUT_MAX_CHARS = 2_000_000
MCP_DEFAULT_OUTPUT = {"default_max_chars": 20_000, "max_output_chars": 500_000}


def mcp_output_limits(cfg: dict) -> tuple[int, int]:
    """Validate both persisted settings and programmatically supplied MCP config."""
    values = {**MCP_DEFAULT_OUTPUT, **cfg.get("mcp", {})}
    for key, value in values.items():
        if key not in MCP_DEFAULT_OUTPUT:
            continue
        if type(value) is not int or not MCP_OUTPUT_MIN_CHARS <= value <= MCP_OUTPUT_MAX_CHARS:
            raise ConfigError(f"mcp.{key}: Ganzzahl zwischen {MCP_OUTPUT_MIN_CHARS} "
                              f"und {MCP_OUTPUT_MAX_CHARS} erwartet")
    default, maximum = values["default_max_chars"], values["max_output_chars"]
    if default > maximum:
        raise ConfigError("MCP-Standardausgabe darf nicht größer als die maximale Ausgabe sein")
    return default, maximum

DEFAULT_CONFIG: dict[str, Any] = {
    "confluence": {
        "base_url": "",            # web root, e.g. https://confluence.intern or https://intern/confluence
        "api_mode": "server",
        "sleep_seconds": 1.0,
        "aliases": [],             # further base URLs that point to the same instance
        "selected_spaces": [],     # space keys chosen in the settings UI
        "auth": {"type": "none", "username": "", "token": ""},
    },
    "jira": {
        "base_url": "",            # web root, e.g. https://jira.intern
        "api_mode": "server_v2",
        "sleep_seconds": 1.0,
        "aliases": [],
        "project_keys": [],        # optional allow-list for plain-text issue key scanning
        "auth": {"type": "none", "username": "", "token": ""},
    },
    "gitlab": {
        "base_url": "",            # web root of the self-managed instance, e.g. https://gitlab.intern
        "token": "",               # personal access token (scope read_api), sent as PRIVATE-TOKEN
        "issue_prefixes": ["API", "BUG"],  # Jira project keys scanned in commit messages
        "project_ids": [],         # numeric project IDs or full paths ("group/sub/project")
        "ref_name": "",            # branch/tag; empty = default branch of each project
        "all_branches": False,     # list commits of all refs (ref_name is ignored then)
        "since": "",               # optional lower bound (YYYY-MM-DD, ISO timestamp, 7d/2w)
        "sleep_seconds": 0.5,
        "incremental": True,       # continue from the last complete sync of a project/ref
        "require_issue_key": True,   # only index commits mentioning a configured key (False = all)
    },
    "http": {
        "verify_ssl": False,       # internal self-hosted instances with private CA
        "ca_bundle": "",           # optional PEM file; used only when verify_ssl is true
        "use_system_proxy": True,  # honour HTTP(S)_PROXY / NO_PROXY env vars
        "max_response_mb": 20,
        "user_agent": "local-knowledge/1.0",
    },
    "crawl_defaults": {
        "start_page": "",
        "space_key": "",
        "space_keys": [],
        "modified_since": "",
        "batch_fetch_size": 100,
        "max_confluence_pages": 100,
        "max_jira_issues": 100,
        "max_total_documents": 200,
        "max_confluence_depth": 2,
        "max_jira_link_hops": 1,
        "max_links_per_document": 100,
        "max_api_requests": 1000,
        "max_duration_minutes": 60,
        "include_child_pages": True,
        "follow_confluence_links": True,
        "index_linked_jira_issues": True,
        "follow_jira_issue_links": True,
        "follow_jira_to_confluence": False,
        "scan_plain_text_issue_keys": False,
        "include_remote_links": True,
        "resolve_page_titles": True,
        "refresh_existing": False,
        "refresh_only": False,
        "gitlab_only": False,
        "max_gitlab_commits": 1000,
        "gitlab_since": "",
        "request_timeout_seconds": 20,
        "max_retries": 4,
    },
    "index": {"chunk_target_chars": 1200, "chunk_overlap_chars": 150, "min_chunk_chars": 80,
              "max_document_chars": 2_000_000},
    "search": {"max_results": 10, "max_query_chars": 200},
    "mcp": dict(MCP_DEFAULT_OUTPUT),
    "graph": {"max_visible_nodes": 5000, "default_hops": 1, "max_hops": 3},
    "server": {"host": "127.0.0.1", "port": 8765},
    "database": {"path": "data/knowledge.sqlite"},
}

# ----------------------------------------------------------------- field specs
# (type, min, max) — type is "int", "float", "bool", "str", "enum:<a|b>", "list"

JOB_FIELD_SPECS: dict[str, tuple] = {
    "start_page": ("str", 0, 2000),
    "space_key": ("str", 0, 100),
    "space_keys": ("spacekeylist",),
    "modified_since": ("str", 0, 40),
    "batch_fetch_size": ("int", 1, 100),
    "max_confluence_pages": ("int", 0, 100_000),
    "max_jira_issues": ("int", 0, 100_000),
    "max_total_documents": ("int", 1, 200_000),
    "max_confluence_depth": ("int", 0, 50),
    "max_jira_link_hops": ("int", 0, 50),
    "max_links_per_document": ("int", 1, 10_000),
    "max_api_requests": ("int", 1, 1_000_000),
    "max_duration_minutes": ("int", 1, 7 * 24 * 60),
    "confluence_sleep_seconds": ("float", 0, 120),
    "jira_sleep_seconds": ("float", 0, 120),
    "include_child_pages": ("bool",),
    "follow_confluence_links": ("bool",),
    "index_linked_jira_issues": ("bool",),
    "follow_jira_issue_links": ("bool",),
    "follow_jira_to_confluence": ("bool",),
    "scan_plain_text_issue_keys": ("bool",),
    "include_remote_links": ("bool",),
    "resolve_page_titles": ("bool",),
    "refresh_existing": ("bool",),
    "refresh_only": ("bool",),
    "gitlab_only": ("bool",),
    "max_gitlab_commits": ("int", 1, 1_000_000),
    "gitlab_since": ("str", 0, 40),
    "gitlab_sleep_seconds": ("float", 0, 120),
    "request_timeout_seconds": ("float", 1, 120),
    "max_retries": ("int", 0, 8),
}

SETTINGS_FIELD_SPECS: dict[str, dict[str, tuple]] = {
    "confluence": {
        "base_url": ("url",),
        "api_mode": ("enum", CONFLUENCE_API_MODES),
        "sleep_seconds": ("float", 0, 120),
        "aliases": ("urllist",),
        "selected_spaces": ("spacekeylist",),
    },
    "jira": {
        "base_url": ("url",),
        "api_mode": ("enum", JIRA_API_MODES),
        "sleep_seconds": ("float", 0, 120),
        "aliases": ("urllist",),
        "project_keys": ("keylist",),
    },
    "gitlab": {
        "base_url": ("url",),
        "issue_prefixes": ("prefixlist",),
        "project_ids": ("projectlist",),
        "ref_name": ("str", 0, 255),
        "all_branches": ("bool",),
        "since": ("str", 0, 40),
        "sleep_seconds": ("float", 0, 120),
        "incremental": ("bool",),
        "require_issue_key": ("bool",),
    },
    "http": {
        "verify_ssl": ("bool",),
        "ca_bundle": ("str", 0, 1000),
        "use_system_proxy": ("bool",),
        "max_response_mb": ("int", 1, 500),
        "user_agent": ("str", 1, 200),
    },
    "crawl_defaults": {k: v for k, v in JOB_FIELD_SPECS.items()
                       if k not in ("confluence_sleep_seconds", "jira_sleep_seconds",
                                    "gitlab_sleep_seconds")},
    "index": {
        "chunk_target_chars": ("int", 200, 20_000),
        "chunk_overlap_chars": ("int", 0, 5_000),
        "min_chunk_chars": ("int", 0, 5_000),
        "max_document_chars": ("int", 10_000, 50_000_000),
    },
    "search": {"max_results": ("int", 1, 10), "max_query_chars": ("int", 10, 1000)},
    "mcp": {key: ("int", MCP_OUTPUT_MIN_CHARS, MCP_OUTPUT_MAX_CHARS)
            for key in MCP_DEFAULT_OUTPUT},
    "graph": {
        "max_visible_nodes": ("int", 10, 100_000),
        "default_hops": ("int", 0, 3),
        "max_hops": ("int", 1, 3),
    },
    "server": {"host": ("str", 1, 255), "port": ("int", 1, 65535)},
    "database": {"path": ("str", 1, 1000)},
}
# start_page and space_key may be empty in the defaults.
SETTINGS_FIELD_SPECS["crawl_defaults"]["start_page"] = ("str", 0, 2000)
SETTINGS_FIELD_SPECS["crawl_defaults"]["space_key"] = ("str", 0, 100)

AUTH_FIELDS = ("type", "username", "token")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_SPACE_KEY_RE = re.compile(r"^[A-Z0-9_~-]+$")
# Space keys from the Confluence API keep their case: personal spaces use keys
# like "~jsmith"; the pattern only blocks characters that would break CQL.
_SPACE_KEY_LIST_RE = re.compile(r"^[A-Za-z0-9_.~:@-]+$")
_MODIFIED_SINCE_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RELATIVE_SINCE_RE = re.compile(r"^(\d{1,4})([dw])$", re.IGNORECASE)
# Jira project keys usable as commit-message prefixes (API, BUG, ...).
_ISSUE_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")
# GitLab project reference: numeric ID or full namespace path.
_GITLAB_PROJECT_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_GITLAB_PROJECT_PATH_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.+-]*(/[A-Za-z0-9_.][A-Za-z0-9_.+-]*)+$")
# Branch/tag names: reject control chars and characters git forbids in refs.
_GIT_REF_RE = re.compile(r"^[^\x00-\x20~^:?*\[\\\x7f]+$")


def _check_value(spec: tuple, value: Any, name: str) -> tuple[Any, str | None]:
    """Validate and coerce a single value. Returns (value, error_or_None)."""
    kind = spec[0]
    if kind == "bool":
        if isinstance(value, bool):
            return value, None
        return None, f"{name}: Wahrheitswert erwartet"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None, f"{name}: Ganzzahl erwartet"
        try:
            num = float(value)
        except ValueError:
            return None, f"{name}: Ganzzahl erwartet"
        if not math.isfinite(num) or num != int(num):
            return None, f"{name}: Ganzzahl erwartet"
        num = int(num)
        if not spec[1] <= num <= spec[2]:
            return None, f"{name}: muss zwischen {spec[1]} und {spec[2]} liegen"
        return num, None
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None, f"{name}: Zahl erwartet"
        try:
            num = float(value)
        except ValueError:
            return None, f"{name}: Zahl erwartet"
        if num != num or not spec[1] <= num <= spec[2]:
            return None, f"{name}: muss zwischen {spec[1]} und {spec[2]} liegen"
        return num, None
    if kind == "str":
        if not isinstance(value, str):
            return None, f"{name}: Text erwartet"
        value = value.strip()
        if not spec[1] <= len(value) <= spec[2]:
            if len(value) < spec[1]:
                return None, f"{name}: Pflichtfeld"
            return None, f"{name}: zu lang (max. {spec[2]} Zeichen)"
        return value, None
    if kind == "enum":
        if value not in spec[1]:
            return None, f"{name}: erlaubt sind {', '.join(spec[1])}"
        return value, None
    if kind == "url":
        if not isinstance(value, str):
            return None, f"{name}: URL erwartet"
        value = value.strip().rstrip("/")
        if value and not (value.startswith("http://") or value.startswith("https://")):
            return None, f"{name}: muss mit http:// oder https:// beginnen"
        return value, None
    if kind == "urllist":
        if isinstance(value, str):
            value = [v for v in (p.strip() for p in value.replace("\n", ",").split(",")) if v]
        if not isinstance(value, list):
            return None, f"{name}: Liste erwartet"
        out = []
        for item in value:
            item, err = _check_value(("url",), item, name)
            if err:
                return None, err
            if item:
                out.append(item)
        return out, None
    if kind == "keylist":
        if isinstance(value, str):
            value = [v for v in (p.strip() for p in value.replace("\n", ",").split(",")) if v]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return None, f"{name}: Liste erwartet"
        return [v.strip().upper() for v in value if v.strip()], None
    if kind == "prefixlist":
        if isinstance(value, str):
            value = [v for v in (p.strip() for p in value.replace("\n", ",").split(",")) if v]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return None, f"{name}: Liste erwartet"
        out = []
        for item in value:
            item = item.strip().upper().rstrip("-")
            if not item:
                continue
            if not _ISSUE_PREFIX_RE.match(item):
                return None, f"{name}: Ungültiger Jira-Projektschlüssel {item!r}"
            if item not in out:
                out.append(item)
        return out, None
    if kind == "projectlist":
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = [v for v in (p.strip() for p in str(value).replace("\n", ",").split(",")) if v]
        if not isinstance(value, list):
            return None, f"{name}: Liste erwartet"
        out = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                return None, f"{name}: Projekt-ID oder Pfad erwartet"
            item = str(item).strip()
            if not item.lower().startswith(("http://", "https://")):
                item = item.strip("/")
            if not item:
                continue
            if not _is_gitlab_project_ref(item):
                return None, f"{name}: Ungültige GitLab-Projekt-ID bzw. -Pfad {item!r}"
            if item not in out:
                out.append(item)
        if len(out) > GITLAB_MAX_PROJECTS:
            return None, f"{name}: höchstens {GITLAB_MAX_PROJECTS} Projekte"
        return out, None
    if kind == "spacekeylist":
        # Like keylist but preserves case and validates the key format –
        # Confluence personal space keys ("~jsmith") are case-sensitive.
        if isinstance(value, str):
            value = [v for v in (p.strip() for p in value.replace("\n", ",").split(",")) if v]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return None, f"{name}: Liste erwartet"
        out = []
        for item in value:
            item = item.strip()
            if not item:
                continue
            if not _SPACE_KEY_LIST_RE.match(item):
                return None, f"{name}: Ungültiger Space-Key {item!r}"
            if item not in out:
                out.append(item)
        return out, None
    raise AssertionError(kind)


# ----------------------------------------------------------------- job params

def _normalize_modified_since(value: Any, name: str = "modified_since") -> tuple[str, str | None]:
    """Normalize an ISO date/timestamp or a relative offset (7d, 2w) to YYYY-MM-DD."""
    raw = str(value or "").strip()
    if not raw:
        return "", None

    relative = _RELATIVE_SINCE_RE.fullmatch(raw)
    if relative:
        amount = int(relative.group(1))
        days = amount * (7 if relative.group(2).lower() == "w" else 1)
        if amount < 1 or days > 3650:
            return "", f"{name}: Zeitraum muss zwischen 1 Tag und 10 Jahren liegen"
        stamp = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        return stamp, None

    date_only = _MODIFIED_SINCE_DATE_RE.fullmatch(raw)
    if date_only:
        try:
            parsed = datetime(int(date_only.group(1)), int(date_only.group(2)),
                              int(date_only.group(3)))
        except ValueError as exc:
            return "", f"{name}: ungültiges Datum ({exc})"
        return parsed.date().isoformat(), None

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "", (f"{name}: Datum als JJJJ-MM-TT, ISO-Zeitstempel oder relativ "
                    "(z. B. 7d, 2w) erwartet")
    return parsed.date().isoformat(), None


def validate_job_params(payload: Any, config: dict) -> tuple[dict, list[str]]:
    """Merge a job start request with config defaults and validate it.

    Returns (params, errors). Unknown fields are errors (HTTP 400). Missing
    fields fall back to crawl_defaults / per-source sleep defaults.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {}, ["Request-Body muss ein JSON-Objekt sein"]
    unknown = sorted(set(payload) - set(JOB_FIELD_SPECS))
    if unknown:
        errors.append("Unbekannte Felder: " + ", ".join(unknown))

    # Programmatic configs may predate newer fields: fall back to DEFAULT_CONFIG.
    defaults = {**DEFAULT_CONFIG["crawl_defaults"], **(config.get("crawl_defaults") or {})}
    defaults["confluence_sleep_seconds"] = (config.get("confluence") or {}).get(
        "sleep_seconds", DEFAULT_CONFIG["confluence"]["sleep_seconds"])
    defaults["jira_sleep_seconds"] = (config.get("jira") or {}).get(
        "sleep_seconds", DEFAULT_CONFIG["jira"]["sleep_seconds"])
    defaults["gitlab_sleep_seconds"] = (config.get("gitlab") or {}).get(
        "sleep_seconds", DEFAULT_CONFIG["gitlab"]["sleep_seconds"])

    params: dict[str, Any] = {}
    for name, spec in JOB_FIELD_SPECS.items():
        value = payload.get(name, defaults.get(name))
        checked, err = _check_value(spec, value, name)
        if err:
            errors.append(err)
        else:
            params[name] = checked

    if not errors:
        since, since_err = _normalize_modified_since(params.get("modified_since", ""))
        if since_err:
            errors.append(since_err)
        params["modified_since"] = since
        gitlab_since, gitlab_since_err = _normalize_modified_since(
            params.get("gitlab_since", ""), "gitlab_since")
        if gitlab_since_err:
            errors.append(gitlab_since_err)
        params["gitlab_since"] = gitlab_since

        if params.get("space_key"):
            raw_sk = str(params["space_key"]).strip().upper()
            if not _SPACE_KEY_RE.match(raw_sk):
                errors.append("space_key: Ungültiges Format für Space-Key")
            else:
                params["space_key"] = raw_sk

        if params.get("gitlab_only"):
            # GitLab commit indexing is independent of Confluence/Jira.
            params["start_page"] = GITLAB_START_UID
            if params.get("refresh_only"):
                errors.append("gitlab_only und refresh_only schließen sich aus")
            errors.extend(gitlab_config_errors(config))
        elif params.get("refresh_only"):
            if not params.get("start_page"):
                params["start_page"] = "refresh:existing"
            params["refresh_existing"] = True
            if not config.get("confluence", {}).get("base_url") and not config.get("jira", {}).get("base_url"):
                errors.append("Weder Confluence- noch Jira-Basis-URL ist konfiguriert (Einstellungen)")
        else:
            if params.get("modified_since") and not params.get("space_keys") and params.get("space_key"):
                # A single space key filters the update run to that space.
                params["space_keys"] = [params["space_key"]]
            if params.get("modified_since"):
                params["start_page"] = f"recent:{params['modified_since']}"
            elif params.get("space_keys"):
                params["start_page"] = "space:" + ",".join(params["space_keys"])
            elif params.get("space_key") and not params.get("start_page"):
                params["start_page"] = f"space:{params['space_key']}"
            elif not params.get("start_page"):
                errors.append(
                    "start_page: Pflichtfeld (oder Space-Key, Space-Auswahl bzw. "
                    "Änderungsdatum angeben)"
                )
            if not config.get("confluence", {}).get("base_url"):
                errors.append("Confluence-Basis-URL ist nicht konfiguriert (Einstellungen)")
            if params.get("index_linked_jira_issues") and not config.get("jira", {}).get("base_url"):
                errors.append("Jira-Basis-URL ist nicht konfiguriert (Einstellungen), "
                              "oder 'Jira-Tickets indexieren' deaktivieren")

    return params, errors


def _is_gitlab_project_path(path: str) -> bool:
    return bool(_GITLAB_PROJECT_PATH_RE.match(path)) and not any(
        part in (".", "..") for part in path.split("/"))


def _is_gitlab_project_ref(item: str) -> bool:
    """Numeric ID, namespace path or a project web URL (host checked later)."""
    if _GITLAB_PROJECT_ID_RE.match(item) or _is_gitlab_project_path(item):
        return True
    if item.lower().startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(item)
        return bool(parts.netloc) and "@" not in parts.netloc and not parts.query
    return False


def normalize_gitlab_project_ref(item: str, base_url: str) -> str:
    """Turn a project entry into an API project reference (ID or path).

    Accepts `42`, `group/sub/project` and web URLs like
    `https://gitlab.com/group/project[.git][/-/tree/main]`. URLs must point to
    the configured instance (same scheme, host, port and context path) – a URL
    of another host is rejected, so the token is never sent elsewhere.
    Raises ConfigError.
    """
    item = str(item).strip()
    if not item.lower().startswith(("http://", "https://")):
        item = item.strip("/")
        if _GITLAB_PROJECT_ID_RE.match(item) or _is_gitlab_project_path(item):
            return item
        raise ConfigError(f"Ungültige GitLab-Projekt-ID bzw. -Pfad {item!r}")
    base = urllib.parse.urlsplit((base_url or "").strip().rstrip("/"))
    url = urllib.parse.urlsplit(item)
    if not base.netloc:
        raise ConfigError("GitLab-Projekt-URL angegeben, aber keine GitLab-Basis-URL konfiguriert")
    if ("@" in url.netloc or url.scheme.lower() != base.scheme.lower()
            or url.netloc.lower() != base.netloc.lower()):
        raise ConfigError(f"GitLab-Projekt-URL {item!r} gehört nicht zu {base_url}")
    base_path = base.path.rstrip("/")
    path = urllib.parse.unquote(url.path)
    if base_path:
        if not (path == base_path or path.startswith(base_path + "/")):
            raise ConfigError(f"GitLab-Projekt-URL {item!r} gehört nicht zu {base_url}")
        path = path[len(base_path):]
    path = path.split("/-/", 1)[0].strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not _is_gitlab_project_path(path):
        raise ConfigError(f"GitLab-Projekt-URL {item!r} enthält keinen Projektpfad")
    return path


def normalize_gitlab_projects(gitlab_cfg: dict) -> list[str]:
    """Normalized, de-duplicated project references of a gitlab config section."""
    out: list[str] = []
    for item in gitlab_cfg.get("project_ids") or []:
        ref = normalize_gitlab_project_ref(str(item), gitlab_cfg.get("base_url") or "")
        if ref not in out:
            out.append(ref)
    return out


def gitlab_config_errors(config: dict) -> list[str]:
    """Problems that prevent a GitLab sync with the given config (empty = ok)."""
    gl = config.get("gitlab") or {}
    errors = []
    if not gl.get("base_url"):
        errors.append("GitLab-Basis-URL ist nicht konfiguriert (Einstellungen)")
    if not gl.get("project_ids"):
        errors.append("Keine GitLab-Projekte konfiguriert (gitlab.project_ids)")
    else:
        try:
            normalize_gitlab_projects(gl)
        except ConfigError as exc:
            errors.append(f"gitlab.project_ids: {exc}")
    ref = str(gl.get("ref_name") or "")
    if ref and not _GIT_REF_RE.match(ref):
        errors.append("gitlab.ref_name: ungültiger Branch-/Tag-Name")
    _, since_err = _normalize_modified_since(gl.get("since", ""), "gitlab.since")
    if since_err:
        errors.append(since_err)
    return errors


# ----------------------------------------------------------------- settings

def validate_settings_patch(patch: Any) -> tuple[dict, list[str]]:
    """Validate a partial settings update from the frontend.

    Auth handling: {"auth": {"type": "bearer", "token": "..."}}; an empty or
    missing token keeps the stored token, {"auth": {"clear_token": true}}
    removes it.
    """
    errors: list[str] = []
    clean: dict[str, Any] = {}
    if not isinstance(patch, dict):
        return {}, ["Request-Body muss ein JSON-Objekt sein"]
    for section, values in patch.items():
        if section not in SETTINGS_FIELD_SPECS:
            errors.append(f"Unbekannter Abschnitt: {section}")
            continue
        if not isinstance(values, dict):
            errors.append(f"{section}: Objekt erwartet")
            continue
        specs = SETTINGS_FIELD_SPECS[section]
        out: dict[str, Any] = {}
        for key, value in values.items():
            name = f"{section}.{key}"
            if section == "gitlab" and key in ("token", "clear_token"):
                if key == "token" and not isinstance(value, str):
                    errors.append(f"{name}: Text erwartet")
                elif key == "token":
                    out["token"] = value.strip()
                else:
                    out["clear_token"] = bool(value)
                continue
            if key == "auth" and section in ("confluence", "jira"):
                auth, err = _check_auth(value, name)
                if err:
                    errors.append(err)
                else:
                    out["auth"] = auth
                continue
            if key not in specs:
                errors.append(f"Unbekanntes Feld: {name}")
                continue
            checked, err = _check_value(specs[key], value, name)
            if err:
                errors.append(err)
                continue
            if section == "gitlab" and key == "ref_name" and checked and not _GIT_REF_RE.match(checked):
                errors.append(f"{name}: ungültiger Branch-/Tag-Name")
                continue
            if (section == "crawl_defaults" and key in ("modified_since", "gitlab_since")) or (
                    section == "gitlab" and key == "since"):
                checked, err = _normalize_modified_since(checked, name)
                if err:
                    errors.append(err)
                    continue
            out[key] = checked
        clean[section] = out
    return clean, errors


def _check_auth(value: Any, name: str) -> tuple[dict | None, str | None]:
    if not isinstance(value, dict):
        return None, f"{name}: Objekt erwartet"
    auth_type = value.get("type")
    if auth_type not in AUTH_TYPES:
        return None, f"{name}.type: erlaubt sind {', '.join(AUTH_TYPES)}"
    username = value.get("username", "")
    if not isinstance(username, str):
        return None, f"{name}.username: Text erwartet"
    token = value.get("token", "")
    if not isinstance(token, str):
        return None, f"{name}.token: Text erwartet"
    clear = bool(value.get("clear_token"))
    return {"type": auth_type, "username": username.strip(), "token": token.strip(),
            "clear_token": clear}, None


# ----------------------------------------------------------------- store

class ConfigStore:
    """Thread-safe configuration holder with read/write persistence."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_CONFIG_PATH
        # Reentrant: update() re-reads the config while holding the lock.
        self._lock = threading.RLock()
        self._cfg = self._load()

    def get(self) -> dict:
        with self._lock:
            return apply_env_overrides(copy.deepcopy(self._cfg))

    def get_public(self) -> dict:
        """Config without plaintext secrets; includes token_set bools."""
        cfg = self.get()
        for s in ("confluence", "jira"):
            auth = cfg.get(s, {}).get("auth") or {}
            cfg[s]["auth"] = {
                "type": auth.get("type", "none"),
                "username": auth.get("username", ""),
                "token": "",
                "token_set": bool(auth.get("token")),
            }
        gl = cfg.setdefault("gitlab", {})
        gl["token_set"] = bool(gl.get("token"))
        gl["token"] = ""
        return cfg

    def update(self, patch: dict) -> dict:
        """Validate and apply a partial settings update, then write to disk."""
        clean, errors = validate_settings_patch(patch)
        if errors:
            raise ConfigError("; ".join(errors))
        with self._lock:
            # Preserve existing tokens if the patch did not clear or change them
            for s in ("confluence", "jira"):
                if s in clean and "auth" in clean[s]:
                    incoming = clean[s]["auth"]
                    old = self._cfg.get(s, {}).get("auth") or {}
                    if incoming.get("clear_token"):
                        incoming["token"] = ""
                    elif not incoming.get("token"):
                        incoming["token"] = old.get("token", "")
                    incoming.pop("clear_token", None)
            if "gitlab" in clean:
                incoming = clean["gitlab"]
                if incoming.pop("clear_token", False):
                    incoming["token"] = ""
                elif not incoming.get("token"):
                    incoming.pop("token", None)

            merged = _deep_merge(self._cfg, clean)
            if "gitlab" in clean and ("project_ids" in clean["gitlab"] or "base_url" in clean["gitlab"]):
                # Project URLs become paths now, so later base_url changes cannot
                # silently redirect them to another host.
                try:
                    merged["gitlab"]["project_ids"] = normalize_gitlab_projects(merged["gitlab"])
                except ConfigError as exc:
                    raise ConfigError(f"gitlab.project_ids: {exc}") from exc
            mcp_output_limits(merged)
            graph = merged.get("graph") or {}
            if int(graph.get("default_hops", 1)) > int(graph.get("max_hops", 3)):
                raise ConfigError("graph.default_hops darf nicht größer als graph.max_hops sein")
            self._cfg = merged
            save_config(self._cfg, self.path)
            return self.get_public()

    def _load(self) -> dict:
        if not self.path.exists():
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise ConfigError(f"Konfigurationsdatei {self.path} ist defekt: {exc}") from exc
        return _deep_merge(DEFAULT_CONFIG, data if isinstance(data, dict) else {})


def load_config(path: str | Path | None = None) -> dict:
    return ConfigStore(path).get()


def save_config(cfg: dict | str | Path, path: str | Path | None = None) -> None:
    if isinstance(cfg, (str, Path)) and isinstance(path, dict):
        cfg, path = path, cfg
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)  # may contain tokens
    except OSError:
        pass
    tmp.replace(p)


def apply_env_overrides(cfg: dict) -> dict:
    """CONFLUENCE_TOKEN / JIRA_TOKEN (+ _USERNAME, _AUTH_TYPE) override the file."""
    for section in ("confluence", "jira"):
        prefix = section.upper()
        auth = cfg.setdefault(section, {}).setdefault(
            "auth", {"type": "none", "username": "", "token": ""})
        token = os.environ.get(f"{prefix}_TOKEN")
        if token:
            auth["token"] = token
            auth["type"] = os.environ.get(f"{prefix}_AUTH_TYPE") or (
                auth.get("type") if auth.get("type") != "none" else "bearer")
        username = os.environ.get(f"{prefix}_USERNAME")
        if username:
            auth["username"] = username
    gitlab_token = os.environ.get("GITLAB_TOKEN")
    if gitlab_token:
        cfg.setdefault("gitlab", {})["token"] = gitlab_token
    return cfg


def resolve_db_path(cfg: dict) -> Path:
    p = Path(cfg.get("database", {}).get("path") or "data/knowledge.sqlite")
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p
