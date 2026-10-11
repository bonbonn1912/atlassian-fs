"""Configuration loading, validation, persistence and job parameter checks."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

from .models import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

CONFLUENCE_API_MODES = ("server", "cloud_v2")   # server = Server/Data Center REST /rest/api/content
JIRA_API_MODES = ("server_v2", "cloud_v3")      # server_v2 = Server/Data Center REST /rest/api/2
AUTH_TYPES = ("none", "bearer", "basic")
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
    "http": {
        "verify_ssl": ("bool",),
        "ca_bundle": ("str", 0, 1000),
        "use_system_proxy": ("bool",),
        "max_response_mb": ("int", 1, 500),
        "user_agent": ("str", 1, 200),
    },
    "crawl_defaults": {k: v for k, v in JOB_FIELD_SPECS.items()
                       if k not in ("confluence_sleep_seconds", "jira_sleep_seconds")},
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

    defaults = dict(config["crawl_defaults"])
    defaults["confluence_sleep_seconds"] = config["confluence"]["sleep_seconds"]
    defaults["jira_sleep_seconds"] = config["jira"]["sleep_seconds"]

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

        if params.get("space_key"):
            raw_sk = str(params["space_key"]).strip().upper()
            if not _SPACE_KEY_RE.match(raw_sk):
                errors.append("space_key: Ungültiges Format für Space-Key")
            else:
                params["space_key"] = raw_sk

        if params.get("refresh_only"):
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
            if section == "crawl_defaults" and key == "modified_since":
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

            merged = _deep_merge(self._cfg, clean)
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
    return cfg


def resolve_db_path(cfg: dict) -> Path:
    p = Path(cfg.get("database", {}).get("path") or "data/knowledge.sqlite")
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p
