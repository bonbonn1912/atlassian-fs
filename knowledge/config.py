"""Configuration loading, validation, persistence and job parameter checks."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from .models import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

CONFLUENCE_API_MODES = ("server", "cloud_v2")   # server = Server/Data Center REST /rest/api/content
JIRA_API_MODES = ("server_v2", "cloud_v3")      # server_v2 = Server/Data Center REST /rest/api/2
AUTH_TYPES = ("none", "bearer", "basic")

DEFAULT_CONFIG: dict[str, Any] = {
    "confluence": {
        "base_url": "",            # web root, e.g. https://confluence.intern or https://intern/confluence
        "api_mode": "server",
        "sleep_seconds": 1.0,
        "aliases": [],             # further base URLs that point to the same instance
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
        "request_timeout_seconds": 20,
        "max_retries": 4,
    },
    "index": {"chunk_target_chars": 1200, "chunk_overlap_chars": 150, "min_chunk_chars": 80,
              "max_document_chars": 2_000_000},
    "search": {"max_results": 10, "max_query_chars": 200},
    "graph": {"max_visible_nodes": 150, "default_hops": 1, "max_hops": 3},
    "server": {"host": "127.0.0.1", "port": 8765},
    "database": {"path": "data/knowledge.sqlite"},
}

# ----------------------------------------------------------------- field specs
# (type, min, max) — type is "int", "float", "bool", "str", "enum:<a|b>", "list"

JOB_FIELD_SPECS: dict[str, tuple] = {
    "start_page": ("str", 1, 2000),
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
    "request_timeout_seconds": ("float", 1, 120),
    "max_retries": ("int", 0, 8),
}

SETTINGS_FIELD_SPECS: dict[str, dict[str, tuple]] = {
    "confluence": {
        "base_url": ("url",),
        "api_mode": ("enum", CONFLUENCE_API_MODES),
        "sleep_seconds": ("float", 0, 120),
        "aliases": ("urllist",),
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
    "graph": {
        "max_visible_nodes": ("int", 10, 2000),
        "default_hops": ("int", 0, 3),
        "max_hops": ("int", 1, 3),
    },
    "server": {"host": ("str", 1, 255), "port": ("int", 1, 65535)},
    "database": {"path": ("str", 1, 1000)},
}
# start_page may be empty in the defaults.
SETTINGS_FIELD_SPECS["crawl_defaults"]["start_page"] = ("str", 0, 2000)

AUTH_FIELDS = ("type", "username", "token")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


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
        if num != int(num):
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
    raise AssertionError(kind)


# ----------------------------------------------------------------- job params

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
    if not errors and not config["confluence"].get("base_url"):
        errors.append("Confluence-Basis-URL ist nicht konfiguriert (Einstellungen)")
    if not errors and params.get("index_linked_jira_issues") and not config["jira"].get("base_url"):
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
            else:
                out[key] = checked
        clean[section] = out
    return clean, errors


def _check_auth(value: Any, name: str) -> tuple[dict | None, str | None]:
    if not isinstance(value, dict):
        return None, f"{name}: Objekt erwartet"
    out: dict[str, Any] = {}
    for key, val in value.items():
        if key == "type":
            if val not in AUTH_TYPES:
                return None, f"{name}.type: erlaubt sind {', '.join(AUTH_TYPES)}"
            out["type"] = val
        elif key in ("username", "token"):
            if not isinstance(val, str) or len(val) > 4000:
                return None, f"{name}.{key}: Text erwartet"
            out[key] = val.strip()
        elif key == "clear_token":
            if not isinstance(val, bool):
                return None, f"{name}.clear_token: Wahrheitswert erwartet"
            out["clear_token"] = val
        else:
            return None, f"Unbekanntes Feld: {name}.{key}"
    return out, None


class ConfigStore:
    """Thread-safe holder for the current configuration backed by a JSON file."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        self._config = load_config(self.path)

    def get(self) -> dict:
        """Deep copy of the effective config (incl. env overrides and secrets)."""
        with self._lock:
            return apply_env_overrides(copy.deepcopy(self._config))

    def get_public(self) -> dict:
        """Config safe to send to the browser: tokens replaced by token_set flags."""
        return redact(self.get())

    def update(self, patch: dict) -> dict:
        """Validate and persist a partial update. Raises ConfigError."""
        clean, errors = validate_settings_patch(patch)
        if errors:
            raise ConfigError("; ".join(errors))
        with self._lock:
            new = copy.deepcopy(self._config)
            for section, values in clean.items():
                for key, value in values.items():
                    if key == "auth":
                        auth = new[section].setdefault("auth", {})
                        if value.get("clear_token"):
                            auth["token"] = ""
                        if "type" in value:
                            auth["type"] = value["type"]
                        if "username" in value:
                            auth["username"] = value["username"]
                        if value.get("token"):
                            auth["token"] = value["token"]
                    else:
                        new[section][key] = value
            graph = new["graph"]
            if graph["default_hops"] > graph["max_hops"]:
                raise ConfigError("graph.default_hops darf nicht größer als graph.max_hops sein")
            save_config(self.path, new)
            self._config = new
            return redact(apply_env_overrides(copy.deepcopy(new)))


def load_config(path: str | Path | None = None) -> dict:
    """Load config.json (if present) merged over DEFAULT_CONFIG."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"config.json konnte nicht gelesen werden: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("config.json muss ein JSON-Objekt sein")
    cfg = _deep_merge(DEFAULT_CONFIG, data)
    for section in ("confluence", "jira"):
        cfg[section]["base_url"] = (cfg[section].get("base_url") or "").strip().rstrip("/")
    return cfg


def save_config(path: str | Path, config: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # may contain tokens
    except OSError:
        pass
    os.replace(tmp, path)


def apply_env_overrides(cfg: dict) -> dict:
    """CONFLUENCE_TOKEN / JIRA_TOKEN (+ _USERNAME, _AUTH_TYPE) override the file."""
    for section in ("confluence", "jira"):
        prefix = section.upper()
        auth = cfg[section].setdefault("auth", {"type": "none", "username": "", "token": ""})
        token = os.environ.get(f"{prefix}_TOKEN")
        if token:
            auth["token"] = token
            auth["type"] = os.environ.get(f"{prefix}_AUTH_TYPE") or (
                auth.get("type") if auth.get("type") != "none" else "bearer")
        user = os.environ.get(f"{prefix}_USERNAME")
        if user:
            auth["username"] = user
    return cfg


def redact(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    for section in ("confluence", "jira"):
        auth = out[section].get("auth", {})
        auth["token_set"] = bool(auth.get("token"))
        auth["token"] = ""
    return out


def resolve_db_path(cfg: dict) -> Path:
    p = Path(cfg["database"]["path"])
    return p if p.is_absolute() else PROJECT_ROOT / p
