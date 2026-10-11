"""Unit tests for knowledge/config.py (validation, env overrides, file mode)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from knowledge.config import (
    ConfigError,
    ConfigStore,
    apply_env_overrides,
    validate_job_params,
    validate_settings_patch,
)


class TestConfigValidation(unittest.TestCase):
    def test_graph_supports_100k_but_keeps_a_hard_limit(self) -> None:
        clean, errors = validate_settings_patch({'graph': {'max_visible_nodes': 100000}})
        self.assertEqual(errors, [])
        self.assertEqual(clean['graph']['max_visible_nodes'], 100000)
        _, errors = validate_settings_patch({'graph': {'max_visible_nodes': 100001}})
        self.assertTrue(errors)

    def test_mcp_limits_persist_and_partial_updates_validate_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            store = ConfigStore(path)
            self.assertEqual(store.get()['mcp'], {'default_max_chars': 20000, 'max_output_chars': 500000})
            store.update({'mcp': {'default_max_chars': 150000, 'max_output_chars': 200000}})
            self.assertEqual(ConfigStore(path).get()['mcp']['default_max_chars'], 150000)
            saved = path.read_bytes()
            with self.assertRaises(ConfigError):
                store.update({'mcp': {'max_output_chars': 100000}})
            self.assertEqual(path.read_bytes(), saved)
            self.assertEqual(store.get()['mcp']['max_output_chars'], 200000)

    def test_mcp_limits_reject_invalid_numbers(self) -> None:
        for value in (True, None, 99, 2000001, -1, 100.5, 'NaN', float('inf')):
            with self.subTest(value=value):
                _, errors = validate_settings_patch({'mcp': {'max_output_chars': value}})
                self.assertTrue(errors)

    def test_spacekeylist_preserves_case_and_deduplicates(self) -> None:
        clean, errors = validate_settings_patch(
            {"confluence": {"selected_spaces": ["ENG", "eng", "~jsmith", "ENG"]}}
        )
        self.assertEqual(errors, [])
        self.assertEqual(clean["confluence"]["selected_spaces"], ["ENG", "eng", "~jsmith"])

    def test_spacekeylist_rejects_invalid_keys(self) -> None:
        for bad in ("A B", 'x"y', "key;drop"):
            clean, errors = validate_settings_patch({"confluence": {"selected_spaces": [bad]}})
            self.assertTrue(errors, bad)

    def test_modified_since_accepts_iso_and_relative_values(self) -> None:
        for value, expected in (
            ("2026-10-04", "2026-10-04"),
            ("2026-10-04T10:20:30Z", "2026-10-04"),
            ("2026-10-04T10:20:30+02:00", "2026-10-04"),
            ("2026-10-04 10:20:30", "2026-10-04"),
        ):
            clean, errors = validate_settings_patch({"crawl_defaults": {"modified_since": value}})
            self.assertEqual(errors, [], value)
            self.assertEqual(clean["crawl_defaults"]["modified_since"], expected)

    def test_modified_since_rejects_garbage(self) -> None:
        clean, errors = validate_settings_patch(
            {"crawl_defaults": {"modified_since": "irgendwann"}}
        )
        self.assertTrue(errors)

    def test_job_validation_space_key_becomes_update_filter(self) -> None:
        cfg = ConfigStore.__new__(ConfigStore)  # only needed as dict shape below
        del cfg
        config = {
            "crawl_defaults": {"start_page": "", "space_key": "", "space_keys": [],
                               "modified_since": "", "max_confluence_pages": 100,
                               "max_jira_issues": 100, "max_total_documents": 200,
                               "max_links_per_document": 10, "max_api_requests": 100,
                               "max_duration_minutes": 10, "batch_fetch_size": 100,
                               "request_timeout_seconds": 20.0, "max_retries": 4,
                               "include_child_pages": True, "follow_confluence_links": True,
                               "index_linked_jira_issues": False,
                               "follow_jira_issue_links": False,
                               "follow_jira_to_confluence": False,
                               "scan_plain_text_issue_keys": False,
                               "include_remote_links": True, "resolve_page_titles": True,
                               "refresh_existing": False, "refresh_only": False,
                               "max_confluence_depth": 2, "max_jira_link_hops": 1},
            "confluence": {"base_url": "https://confluence.intern", "sleep_seconds": 1.0},
            "jira": {"base_url": "", "sleep_seconds": 1.0},
        }
        params, errors = validate_job_params({"space_key": "ENG", "modified_since": "7d"}, config)
        self.assertEqual(errors, [])
        self.assertEqual(params["space_keys"], ["ENG"])
        self.assertTrue(params["start_page"].startswith("recent:"))

    def test_graph_hops_cross_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            with self.assertRaises(ConfigError):
                store.update({"graph": {"default_hops": 3, "max_hops": 1}})


class TestConfigStore(unittest.TestCase):
    def test_env_overrides_do_not_touch_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)
            old_env = {k: os.environ.get(k) for k in
                       ("CONFLUENCE_TOKEN", "CONFLUENCE_USERNAME", "CONFLUENCE_AUTH_TYPE",
                        "JIRA_TOKEN")}
            try:
                os.environ["CONFLUENCE_TOKEN"] = "env-token"
                os.environ["CONFLUENCE_USERNAME"] = "env-user"
                os.environ.pop("CONFLUENCE_AUTH_TYPE", None)
                self.assertEqual(store.get()["confluence"]["auth"]["token"], "env-token")
                self.assertEqual(store.get()["confluence"]["auth"]["username"], "env-user")
                self.assertEqual(store.get()["confluence"]["auth"]["type"], "bearer")
                # public config must not leak the token, but keep the flag
                public = store.get_public()
                self.assertEqual(public["confluence"]["auth"]["token"], "")
                self.assertTrue(public["confluence"]["auth"]["token_set"])
                # the file on disk stays untouched
                self.assertFalse(path.exists())
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_update_writes_config_with_restrictive_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)
            store.update({"confluence": {"base_url": "https://confluence.intern",
                                         "auth": {"type": "bearer", "token": "secret"}}})
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["confluence"]["auth"]["token"], "secret")

    def test_apply_env_overrides_sets_token_and_auth_type(self) -> None:
        cfg = {"confluence": {"auth": {"type": "none", "username": "", "token": ""}},
               "jira": {"auth": {"type": "none", "username": "", "token": ""}}}
        old = os.environ.get("JIRA_TOKEN")
        try:
            os.environ["JIRA_TOKEN"] = "jira-env"
            out = apply_env_overrides(cfg)
            self.assertEqual(out["jira"]["auth"]["token"], "jira-env")
            self.assertEqual(out["jira"]["auth"]["type"], "bearer")  # inferred
            self.assertEqual(out["confluence"]["auth"]["token"], "")
        finally:
            if old is None:
                os.environ.pop("JIRA_TOKEN", None)
            else:
                os.environ["JIRA_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()
