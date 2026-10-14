"""Tests for the GitLab integration (client, config, migration, indexer, API).

No network: a fake transport emulates the GitLab REST API v4 for both a
self-managed instance (with context path) and GitLab.com.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse
import urllib.request

from knowledge import db, gitlab, models
from knowledge.atlassian_http import (
    HttpResponse,
    RateLimitedClient,
    RequestBudget,
    build_gitlab_headers,
    redact_url,
)
from knowledge.config import (
    ConfigError,
    ConfigStore,
    DEFAULT_CONFIG,
    _deep_merge,
    apply_env_overrides,
    normalize_gitlab_project_ref,
    validate_job_params,
    validate_settings_patch,
)
from knowledge.indexer import JobManager
from knowledge.models import (
    BudgetExceeded,
    Chunk,
    FetchedDocument,
    ForbiddenError,
    HostNotAllowed,
    InvalidResponseError,
)

TOKEN = "glpat-SECRET-token-1234"


def sha(n: int) -> str:
    return f"{n:040x}"


def make_commit(n: int, message: str, date: str, web_base: str = "") -> dict:
    full = sha(n)
    return {
        "id": full, "short_id": full[:8], "title": message.split("\n", 1)[0],
        "message": message, "author_name": "Dev", "author_email": "dev@example.com",
        "authored_date": date, "committed_date": date, "created_at": date,
        "parent_ids": [], "web_url": f"{web_base}/-/commit/{full}" if web_base else "",
    }


class FakeGitLab:
    """Callable transport emulating /api/v4 (version, projects, commits)."""

    def __init__(self, base_url: str, projects: list[dict], commits: dict[str, list[dict]],
                 *, token: str = TOKEN, page_mode: str = "x-next-page", per_page_cap: int = 100):
        self.base = base_url.rstrip("/")
        self.projects = projects
        self.commits = commits
        self.token = token
        self.page_mode = page_mode
        self.per_page_cap = per_page_cap
        self.calls: list[tuple[str, dict]] = []
        self.fail_commit_pages: set[int] = set()

    def __call__(self, url, headers, timeout, ssl_context, max_bytes):
        self.calls.append((url, dict(headers)))
        if not url.startswith(self.base + "/"):
            raise AssertionError(f"request outside base: {url}")
        parts = urllib.parse.urlsplit(url)
        base_path = urllib.parse.urlsplit(self.base).path
        path = parts.path[len(base_path):]
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        if self.token and headers.get("PRIVATE-TOKEN") != self.token:
            return self._json(401, {"message": "401 Unauthorized"})
        if path == "/api/v4/version":
            return self._json(200, {"version": "17.5.0", "revision": "abc"})
        if path == "/api/v4/projects":
            return self._project_listing(query)
        prefix = "/api/v4/projects/"
        if not path.startswith(prefix):
            return self._json(404, {"message": "404 Not Found"})
        rest = path[len(prefix):]
        project_part, _, tail = rest.partition("/")
        ref = urllib.parse.unquote(project_part)
        if "/" in project_part:
            # an unencoded namespace path would hit a different route in GitLab
            return self._json(404, {"error": "404 Not Found"})
        project = next((p for p in self.projects
                        if str(p["id"]) == ref or p["path_with_namespace"] == ref), None)
        if project is None:
            return self._json(404, {"message": "404 Project Not Found"})
        if not tail:
            return self._json(200, project)
        if tail != "repository/commits":
            return self._json(404, {"message": "404 Not Found"})
        page = int(query.get("page", "1"))
        if page in self.fail_commit_pages:
            return self._json(500, {"message": "boom"})
        per_page = min(int(query.get("per_page", "20")), self.per_page_cap)
        items = list(self.commits.get(str(project["id"]), []))
        if "since" in query:
            items = [c for c in items if c["committed_date"] >= query["since"]]
        chunk = items[(page - 1) * per_page: page * per_page]
        more = page * per_page < len(items)
        hdrs = {"content-type": "application/json"}
        if self.page_mode == "x-next-page":
            hdrs["x-next-page"] = str(page + 1) if more else ""
            hdrs["x-page"] = str(page)
        elif self.page_mode == "link" and more:
            # absolute URL on another host (external_url differs) must not be requested
            hdrs["link"] = (f'<https://other.example/api/v4/projects/{project["id"]}'
                            f'/repository/commits?page={page + 1}&per_page={per_page}>; rel="next"')
        return self._json(200, chunk, hdrs)

    def _project_listing(self, query: dict) -> HttpResponse:
        """Membership listing; projects may carry an `access_level` (default 30)."""
        min_level = int(query.get("min_access_level", "0"))
        items = [p for p in self.projects
                 if query.get("membership") != "true" or p.get("member", True)]
        items = [p for p in items if p.get("access_level", 30) >= min_level]
        page = int(query.get("page", "1"))
        per_page = min(int(query.get("per_page", "20")), self.per_page_cap)
        chunk = items[(page - 1) * per_page: page * per_page]
        more = page * per_page < len(items)
        return self._json(200, chunk, {"content-type": "application/json",
                                       "x-next-page": str(page + 1) if more else ""})

    @staticmethod
    def _json(status: int, data, headers: dict | None = None) -> HttpResponse:
        return HttpResponse(status=status, headers=headers or {"content-type": "application/json"},
                            body=json.dumps(data).encode())

    def commit_requests(self) -> list[dict]:
        out = []
        for url, _ in self.calls:
            parts = urllib.parse.urlsplit(url)
            if parts.path.endswith("/repository/commits"):
                out.append({k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()})
        return out


def make_cfg(base_url: str, project_ids: list[str], **gitlab_overrides) -> dict:
    gl = {"base_url": base_url, "token": TOKEN, "project_ids": project_ids, "sleep_seconds": 0.0}
    gl.update(gitlab_overrides)
    return _deep_merge(DEFAULT_CONFIG, {"gitlab": gl, "jira": {"base_url": "https://jira.intern"}})


def make_client(cfg: dict, transport, budget: RequestBudget | None = None,
                stop: threading.Event | None = None) -> gitlab.GitLabClient:
    http = gitlab.build_gitlab_http(cfg, stop_event=stop or threading.Event(),
                                    budget=budget or RequestBudget(1000, 600),
                                    sleep_seconds=0.0, max_retries=0, transport=transport)
    return gitlab.GitLabClient(http, cfg)


# --------------------------------------------------------------------------- keys

class TestIssueKeys(unittest.TestCase):
    def test_prefix_matching_boundaries_and_order(self) -> None:
        pattern = gitlab.issue_key_pattern(["API", "BUG"])
        text = ("API-2323 changed this; see [BUG-7], feature/API-12-x, API-2323 again, "
                "XAPI-1 no, API-0 no, api-5 no, BUG-9a no, OTHER-1 no, (BUG-10)")
        self.assertEqual(gitlab.find_issue_keys(text, pattern),
                         ["API-2323", "BUG-7", "API-12", "BUG-10"])

    def test_no_prefixes_means_no_links(self) -> None:
        self.assertIsNone(gitlab.issue_key_pattern([]))
        self.assertEqual(gitlab.find_issue_keys("API-1", None), [])

    def test_overlapping_prefixes(self) -> None:
        pattern = gitlab.issue_key_pattern(["AP", "API"])
        self.assertEqual(gitlab.find_issue_keys("API-3 AP-4", pattern), ["API-3", "AP-4"])

    def test_effective_prefixes_fall_back_to_jira_project_keys(self) -> None:
        cfg = _deep_merge(DEFAULT_CONFIG, {"gitlab": {"issue_prefixes": []},
                                           "jira": {"project_keys": ["ENG"]}})
        self.assertEqual(gitlab.effective_prefixes(cfg), ["ENG"])

    def test_scan_signature_changes_with_prefixes_filter_and_ref(self) -> None:
        base = make_cfg("https://g", ["1"])
        sig = gitlab.scan_signature(base)
        self.assertEqual(sig, gitlab.scan_signature(make_cfg("https://g", ["1"], issue_prefixes=["BUG", "API"])))
        for override in ({"issue_prefixes": ["API"]}, {"require_issue_key": False},
                         {"ref_name": "develop"}, {"all_branches": True}):
            self.assertNotEqual(sig, gitlab.scan_signature(make_cfg("https://g", ["1"], **override)), override)


# --------------------------------------------------------------------------- config

class TestGitLabConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        gl = DEFAULT_CONFIG["gitlab"]
        self.assertEqual(gl["issue_prefixes"], ["API", "BUG"])
        self.assertTrue(gl["require_issue_key"])
        self.assertEqual(gl["project_ids"], [])

    def test_settings_validation(self) -> None:
        clean, errors = validate_settings_patch({"gitlab": {
            "issue_prefixes": "api, BUG-, API", "project_ids": [42, "grp/sub/proj", "42"],
            "ref_name": "release/1.0", "base_url": "https://gitlab.com/"}})
        self.assertEqual(errors, [])
        self.assertEqual(clean["gitlab"]["issue_prefixes"], ["API", "BUG"])
        self.assertEqual(clean["gitlab"]["project_ids"], ["42", "grp/sub/proj"])
        self.assertEqual(clean["gitlab"]["base_url"], "https://gitlab.com")
        for bad in ({"issue_prefixes": ["1API"]}, {"project_ids": ["../etc"]},
                    {"project_ids": [True]}, {"ref_name": "a b"}, {"ref_name": "x..y:z"},
                    {"since": "yesterday"}, {"unknown": 1}, {"token": 5}):
            _, errors = validate_settings_patch({"gitlab": bad})
            self.assertTrue(errors, bad)

    def test_project_url_normalization(self) -> None:
        n = normalize_gitlab_project_ref
        self.assertEqual(n("https://gitlab.com/dw191299/bonbot", "https://gitlab.com"), "dw191299/bonbot")
        self.assertEqual(n("https://gitlab.com/dw191299/bonbot.git", "https://gitlab.com"), "dw191299/bonbot")
        self.assertEqual(n("https://GITLAB.com/g/s/p/-/tree/main", "https://gitlab.com"), "g/s/p")
        self.assertEqual(n("https://intern/gitlab/a/b", "https://intern/gitlab"), "a/b")
        self.assertEqual(n("7", "https://gitlab.com"), "7")
        for bad, base in (("https://evil.example/a/b", "https://gitlab.com"),
                          ("http://gitlab.com/a/b", "https://gitlab.com"),
                          ("https://user:pw@gitlab.com/a/b", "https://gitlab.com"),
                          ("https://intern/other/a/b", "https://intern/gitlab"),
                          ("https://gitlab.com/onlyone", "https://gitlab.com"),
                          ("https://gitlab.com/a/b", "")):
            with self.assertRaises(ConfigError, msg=bad):
                n(bad, base)

    def test_token_is_hidden_kept_cleared_and_env_overridable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            public = store.update({"gitlab": {"base_url": "https://gitlab.com", "token": TOKEN,
                                              "project_ids": ["https://gitlab.com/dw191299/bonbot"]}})
            self.assertEqual(public["gitlab"]["token"], "")
            self.assertTrue(public["gitlab"]["token_set"])
            self.assertEqual(public["gitlab"]["project_ids"], ["dw191299/bonbot"])
            self.assertNotIn(TOKEN, json.dumps(public))
            store.update({"gitlab": {"token": "", "ref_name": "main"}})
            self.assertEqual(store.get()["gitlab"]["token"], TOKEN)
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            store.update({"gitlab": {"clear_token": True}})
            self.assertEqual(store.get()["gitlab"]["token"], "")
            with self.assertRaises(ConfigError):
                store.update({"gitlab": {"project_ids": ["https://evil.example/a/b"]}})
            with patch.dict(os.environ, {"GITLAB_TOKEN": "env-token-xyz"}):
                self.assertEqual(store.get()["gitlab"]["token"], "env-token-xyz")
                self.assertEqual(store.get_public()["gitlab"]["token"], "")

    def test_legacy_config_file_without_gitlab_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"confluence": {"base_url": "https://c"}}))
            cfg = ConfigStore(path).get()
            self.assertEqual(cfg["gitlab"]["issue_prefixes"], ["API", "BUG"])
            self.assertFalse(cfg["crawl_defaults"]["gitlab_only"])

    def test_job_params_gitlab_only(self) -> None:
        _, errors = validate_job_params({"gitlab_only": True}, DEFAULT_CONFIG)
        self.assertTrue(any("GitLab-Basis-URL" in e for e in errors))
        self.assertTrue(any("project_ids" in e for e in errors))
        cfg = make_cfg("https://gitlab.com", ["dw191299/bonbot"])
        cfg["confluence"]["base_url"] = ""   # independent of Confluence
        params, errors = validate_job_params(
            {"gitlab_only": True, "gitlab_since": "2026-01-02", "max_gitlab_commits": 5}, cfg)
        self.assertEqual(errors, [])
        self.assertEqual(params["start_page"], "gitlab-sync")
        self.assertEqual(params["gitlab_since"], "2026-01-02")
        self.assertEqual(params["max_gitlab_commits"], 5)
        self.assertEqual(params["gitlab_sleep_seconds"], 0.0)
        _, errors = validate_job_params({"gitlab_only": True, "refresh_only": True}, cfg)
        self.assertTrue(errors)
        _, errors = validate_job_params({"gitlab_only": True, "gitlab_since": "bad"}, cfg)
        self.assertTrue(errors)

    def test_job_params_from_legacy_programmatic_config(self) -> None:
        cfg = {"confluence": {"base_url": "https://c", "sleep_seconds": 1},
               "jira": {"base_url": "", "sleep_seconds": 1},
               "crawl_defaults": {"start_page": "1", "index_linked_jira_issues": False}}
        params, errors = validate_job_params({}, cfg)
        self.assertEqual(errors, [])
        self.assertFalse(params["gitlab_only"])


# --------------------------------------------------------------------------- http/redaction

class TestRedaction(unittest.TestCase):
    def test_redact_url(self) -> None:
        self.assertEqual(redact_url("https://g/api?private_token=abc&x=1"), "https://g/api?private_token=***&x=1")
        self.assertEqual(redact_url("https://user:pw@g/x"), "https://***@g/x")

    def test_token_header_and_no_leak_in_errors(self) -> None:
        self.assertEqual(build_gitlab_headers(TOKEN), {"PRIVATE-TOKEN": TOKEN})
        self.assertEqual(build_gitlab_headers(""), {})

        def transport(url, headers, timeout, ctx, max_bytes):
            raise OSError(f"connection reset while sending {headers['PRIVATE-TOKEN']}")

        client = RateLimitedClient("gitlab", "https://g", allowed_bases=["https://g"], sleep_seconds=0,
                                   timeout=1, max_retries=0, stop_event=threading.Event(),
                                   budget=RequestBudget(10, 60), ssl_context=None,
                                   auth_headers=build_gitlab_headers(TOKEN), transport=transport)
        with self.assertRaises(models.AtlassianError) as cm:
            client.get_json("/api/v4/version?private_token=" + TOKEN)
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN, cm.exception.url)
        self.assertNotIn(TOKEN, repr(client))

    def test_public_gitlab_always_verifies_tls(self) -> None:
        self.assertTrue(gitlab.gitlab_verify_ssl(make_cfg("https://gitlab.com", ["a/b"])))
        self.assertFalse(gitlab.gitlab_verify_ssl(make_cfg("https://gitlab.intern", ["a/b"])))


# --------------------------------------------------------------------------- client

GITLAB_COM_PROJECT = {"id": 4711, "path_with_namespace": "dw191299/bonbot", "name": "bonbot",
                      "web_url": "https://gitlab.com/dw191299/bonbot", "default_branch": "main"}


class TestGitLabClient(unittest.TestCase):
    def test_gitlab_com_nested_path_project_and_commits(self) -> None:
        commits = [make_commit(i, f"API-{i} change {i}", f"2026-10-{10 - i % 9:02d}T10:00:00Z",
                               GITLAB_COM_PROJECT["web_url"]) for i in range(1, 251)]
        fake = FakeGitLab("https://gitlab.com", [GITLAB_COM_PROJECT], {"4711": commits})
        cfg = make_cfg("https://gitlab.com", ["https://gitlab.com/dw191299/bonbot"])
        client = make_client(cfg, fake)
        self.assertEqual(client.project_refs(), ["dw191299/bonbot"])
        project = client.get_project("dw191299/bonbot")
        self.assertEqual(project["id"], "4711")
        self.assertTrue(fake.calls[0][0].startswith("https://gitlab.com/api/v4/projects/dw191299%2Fbonbot"))
        self.assertEqual(fake.calls[0][1]["PRIVATE-TOKEN"], TOKEN)
        pages = list(client.iter_commit_pages("4711"))
        self.assertEqual([len(p) for p in pages], [100, 100, 50])
        self.assertEqual(fake.commit_requests()[0]["per_page"], "100")
        doc = client.commit_to_document(project, pages[0][0])
        self.assertEqual(doc.uid, f"gitlab:4711:{sha(1)}")
        self.assertEqual(doc.source_type, "gitlab")
        self.assertEqual(doc.document_type, "commit")
        self.assertEqual(doc.space_or_project, "dw191299/bonbot")
        self.assertEqual(doc.url, f"https://gitlab.com/dw191299/bonbot/-/commit/{sha(1)}")
        self.assertEqual([(l.uid, l.kind) for l in doc.links], [("jira:API-1", "mentions_jira")])
        self.assertIn("API-1", doc.body_text)

    def test_self_managed_context_path_and_ref(self) -> None:
        project = {"id": 9, "path_with_namespace": "team/svc", "web_url": "https://intern/gitlab/team/svc"}
        fake = FakeGitLab("https://intern/gitlab", [project],
                          {"9": [make_commit(1, "BUG-1 fix", "2026-10-01T00:00:00Z")]})
        cfg = make_cfg("https://intern/gitlab", ["team/svc"], ref_name="develop")
        client = make_client(cfg, fake)
        pages = list(client.iter_commit_pages("9", since="2026-09-01T00:00:00Z"))
        self.assertEqual(len(pages[0]), 1)
        req = fake.commit_requests()[0]
        self.assertEqual(req["ref_name"], "develop")
        self.assertEqual(req["since"], "2026-09-01T00:00:00Z")
        doc = client.commit_to_document(client.get_project("team/svc"), pages[0][0])
        self.assertEqual(doc.url, f"https://intern/gitlab/team/svc/-/commit/{sha(1)}")

    def test_all_branches_flag(self) -> None:
        fake = FakeGitLab("https://g", [GITLAB_COM_PROJECT], {"4711": []})
        client = make_client(make_cfg("https://g", ["4711"], all_branches=True, ref_name="x"), fake)
        self.assertEqual(list(client.iter_commit_pages("4711")), [])
        req = fake.commit_requests()[0]
        self.assertEqual(req["all"], "true")
        self.assertNotIn("ref_name", req)

    def test_link_header_pagination_stays_on_configured_host(self) -> None:
        commits = [make_commit(i, "x", "2026-10-01T00:00:00Z") for i in range(1, 151)]
        fake = FakeGitLab("https://gitlab.com", [GITLAB_COM_PROJECT], {"4711": commits}, page_mode="link")
        client = make_client(make_cfg("https://gitlab.com", ["4711"]), fake)
        self.assertEqual(sum(len(p) for p in client.iter_commit_pages("4711")), 150)
        self.assertTrue(all(u.startswith("https://gitlab.com/") for u, _ in fake.calls))

    def test_pagination_without_headers_uses_page_size(self) -> None:
        commits = [make_commit(i, "x", "2026-10-01T00:00:00Z") for i in range(1, 201)]
        fake = FakeGitLab("https://g", [GITLAB_COM_PROJECT], {"4711": commits}, page_mode="none")
        client = make_client(make_cfg("https://g", ["4711"]), fake)
        self.assertEqual(sum(len(p) for p in client.iter_commit_pages("4711")), 200)
        self.assertEqual(len(fake.commit_requests()), 3)   # 100 + 100 + empty page

    def test_pagination_is_bounded(self) -> None:
        def transport(url, headers, timeout, ctx, max_bytes):
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0])
            return FakeGitLab._json(200, [make_commit(page, "x", "2026-01-01")], {"x-next-page": str(page + 1)})

        client = make_client(make_cfg("https://g", ["1"]), transport)
        with self.assertRaises(InvalidResponseError):
            list(client.iter_commit_pages("1", max_pages=3))
        with self.assertRaises(BudgetExceeded):
            list(make_client(make_cfg("https://g", ["1"]), transport, budget=RequestBudget(2, 60))
                 .iter_commit_pages("1"))

    def test_invalid_token_and_test_connection(self) -> None:
        fake = FakeGitLab("https://gitlab.com", [GITLAB_COM_PROJECT], {}, token="other")
        client = make_client(make_cfg("https://gitlab.com", ["dw191299/bonbot"]), fake)
        with self.assertRaises(ForbiddenError):
            client.get_project("dw191299/bonbot")
        result = client.test_connection()
        self.assertFalse(result["ok"])
        self.assertIn("read_api", result["message"])
        ok = make_client(make_cfg("https://gitlab.com", ["dw191299/bonbot", "nope/missing"]),
                         FakeGitLab("https://gitlab.com", [GITLAB_COM_PROJECT], {})).test_connection()
        self.assertFalse(ok["ok"])
        self.assertEqual(ok["version"], "17.5.0")
        self.assertEqual(ok["projects"][0]["path"], "dw191299/bonbot")
        self.assertIn("nope/missing", ok["message"])

    def test_test_connection_without_token_reads_public_projects(self) -> None:
        class PublicGitLab(FakeGitLab):
            # gitlab.com: /version needs auth, public projects do not
            def __call__(self, url, headers, timeout, ssl_context, max_bytes):
                if url.endswith("/api/v4/version") and not headers.get("PRIVATE-TOKEN"):
                    self.calls.append((url, dict(headers)))
                    return self._json(401, {"message": "401 Unauthorized"})
                return super().__call__(url, headers, timeout, ssl_context, max_bytes)

        fake = PublicGitLab("https://gitlab.com", [GITLAB_COM_PROJECT], {}, token="")
        result = make_client(make_cfg("https://gitlab.com", ["dw191299/bonbot"], token=""),
                             fake).test_connection()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["projects"][0]["path"], "dw191299/bonbot")
        self.assertIn("ohne Token", result["message"])

    def test_list_accessible_projects_paginates_and_filters_role(self) -> None:
        projects = [{"id": i, "path_with_namespace": f"grp/p{i:03d}", "name": f"p{i}",
                     "visibility": "private", "access_level": 10 if i % 10 == 0 else 30}
                    for i in range(1, 251)]
        projects.append({"id": 999, "path_with_namespace": "other/public", "member": False})
        fake = FakeGitLab("https://gitlab.com", projects, {}, per_page_cap=100)
        client = make_client(make_cfg("https://gitlab.com", []), fake)
        listing = client.list_accessible_projects()
        self.assertFalse(listing["truncated"])
        paths = [p["path"] for p in listing["projects"]]
        self.assertEqual(len(paths), 225)          # 25 guest-only projects dropped
        self.assertNotIn("grp/p010", paths)
        self.assertNotIn("other/public", paths)
        self.assertEqual(listing["projects"][0]["visibility"], "private")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][0]).query)
        self.assertEqual(query["membership"], ["true"])
        self.assertEqual(query["min_access_level"], ["20"])
        truncated = client.list_accessible_projects(max_pages=2)
        self.assertTrue(truncated["truncated"])
        self.assertEqual(len(truncated["projects"]), 200)

    def test_list_accessible_projects_requires_token(self) -> None:
        fake = FakeGitLab("https://gitlab.com", [GITLAB_COM_PROJECT], {}, token="")
        client = make_client(make_cfg("https://gitlab.com", [], token=""), fake)
        with self.assertRaises(models.AtlassianError):
            client.list_accessible_projects()
        self.assertEqual(fake.calls, [])

    def test_test_connection_checks_all_configured_projects(self) -> None:
        projects = [{"id": i, "path_with_namespace": f"grp/p{i}"} for i in range(1, 8)]
        refs = [f"grp/p{i}" for i in range(1, 8)] + ["grp/missing"]
        result = make_client(make_cfg("https://gitlab.com", refs),
                             FakeGitLab("https://gitlab.com", projects, {})).test_connection()
        self.assertEqual(len(result["projects"]), 7)
        self.assertFalse(result["ok"])
        self.assertIn("grp/missing", result["message"])

    def test_absolute_url_on_other_host_is_rejected(self) -> None:
        client = make_client(make_cfg("https://gitlab.com", ["1"]), FakeGitLab("https://gitlab.com", [], {}))
        with self.assertRaises(HostNotAllowed):
            client.http.get_json("https://evil.example/api/v4/version")


# --------------------------------------------------------------------------- migration

class TestMigrationV4(unittest.TestCase):
    def test_v3_database_is_upgraded_without_losing_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "k.sqlite"
            conn = db.connect(path)
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            for version, sql in db.MIGRATIONS[:3]:
                for statement in db._split_sql(sql):
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_migrations VALUES(?, 'x')", (version,))
            doc = FetchedDocument("confluence:1", "confluence", "1", "Alpha page", "u", "alpha body",
                                  links=[models.LinkTarget("jira:API-1", "mentions_jira")])
            db.save_document(conn, doc, [Chunk("", "alpha body unique")])
            job_id = db.create_job(conn, {}, "confluence:1")
            with self.assertRaises(sqlite3.IntegrityError):   # CHECK constraint before v4
                conn.execute("INSERT INTO documents(uid, source_type, source_id) "
                             "VALUES('gitlab:1:abc', 'gitlab', '1:abc')")
            conn.close()

            conn = db.open_db(path)
            self.assertEqual(db.init_db(conn), 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 1)
            hits = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'unique'").fetchall()
            self.assertEqual(len(hits), 1)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            db.ensure_node(conn, "gitlab:1:abc")
            db.update_job(conn, job_id, gitlab_indexed=3)
            self.assertEqual(db.get_job(conn, job_id)["gitlab_indexed"], 3)
            db.save_gitlab_sync_state(conn, "1", "", "2026-10-01T00:00:00Z", "abc", "sig")
            self.assertEqual(db.get_gitlab_sync_state(conn, "1", "")["scan_signature"], "sig")
            idx = {r[1] for r in conn.execute("PRAGMA index_list(documents)")}
            self.assertTrue({"idx_docs_type_state", "idx_docs_project"} <= idx)
            # deleting a document still cascades after the rebuild
            self.assertTrue(db.delete_document(conn, "confluence:1"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 0)
            self.assertIn("gitlab", db.get_stats(conn)["by_type_state"])
            conn.close()


# --------------------------------------------------------------------------- indexer

def wait_done(mgr: JobManager, job_id: int, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while mgr.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    return mgr.status(job_id)


class TestGitLabJobs(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="test_gitlab_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db_path = Path(self.dir) / "k.sqlite"
        self.store = ConfigStore(Path(self.dir) / "config.json")
        self.store.update({
            "confluence": {"base_url": ""},
            "jira": {"base_url": "https://jira.intern"},
            "gitlab": {"base_url": "https://gitlab.com", "token": TOKEN, "sleep_seconds": 0,
                       "project_ids": ["https://gitlab.com/dw191299/bonbot"]},
        })
        self.commits = [
            make_commit(3, "API-2323 changed this", "2026-10-03T00:00:00Z", GITLAB_COM_PROJECT["web_url"]),
            make_commit(2, "chore: no key", "2026-10-02T00:00:00Z", GITLAB_COM_PROJECT["web_url"]),
            make_commit(1, "BUG-7 and API-2323\n\nbody", "2026-10-01T00:00:00Z", GITLAB_COM_PROJECT["web_url"]),
        ]
        self.fake = FakeGitLab("https://gitlab.com", [GITLAB_COM_PROJECT], {"4711": self.commits})
        conn = db.open_db(self.db_path)
        db.save_document(conn, FetchedDocument("jira:API-2323", "jira", "API-2323", "API-2323: Login",
                                               "https://jira.intern/browse/API-2323", "Login"),
                         [Chunk("", "Login")])
        conn.close()

    def manager(self) -> JobManager:
        def factory(cfg, params, stop_event, budget, on_event):
            http = gitlab.build_gitlab_http(cfg, stop_event=stop_event, budget=budget, sleep_seconds=0,
                                            max_retries=0, on_event=on_event, transport=self.fake)
            return gitlab.GitLabClient(http, cfg)

        def no_atlassian(*_args):
            raise AssertionError("Atlassian clients must not be built for gitlab_only")

        return JobManager(self.store, self.db_path, client_factory=no_atlassian,
                          gitlab_client_factory=factory)

    def run_job(self, payload: dict | None = None) -> dict:
        mgr = self.manager()
        job_id = mgr.start({"gitlab_only": True, **(payload or {})})["job_id"]
        return wait_done(mgr, job_id)

    def query(self, sql: str, args=()) -> list:
        conn = db.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def test_full_then_incremental_sync(self) -> None:
        status = self.run_job()
        self.assertEqual(status["state"], "completed", status["last_message"])
        self.assertEqual(status["start_page_uid"], "gitlab-sync")
        self.assertEqual(status["gitlab_indexed"], 2)
        self.assertEqual(status["skipped"], 1)
        uids = {r[0] for r in self.query("SELECT uid FROM documents WHERE source_type='gitlab'")}
        self.assertEqual(uids, {f"gitlab:4711:{sha(3)}", f"gitlab:4711:{sha(1)}"})
        edges = self.query(
            "SELECT s.uid, t.uid, e.kind, t.state, t.url FROM edges e JOIN documents s ON s.id=e.source_doc "
            "JOIN documents t ON t.id=e.target_doc ORDER BY s.uid, t.uid")
        self.assertIn((f"gitlab:4711:{sha(3)}", "jira:API-2323", "mentions_jira", "indexed",
                       "https://jira.intern/browse/API-2323"), edges)
        # BUG-7 is not indexed in Jira: standalone reference node with a Jira URL
        self.assertIn((f"gitlab:4711:{sha(1)}", "jira:BUG-7", "mentions_jira", "discovered",
                       "https://jira.intern/browse/BUG-7"), edges)
        state = self.query("SELECT project_id, ref, last_sha FROM gitlab_sync_state")
        self.assertEqual(state, [("4711", "", sha(3))])
        self.assertNotIn("since", self.fake.commit_requests()[0])
        # no token in persisted job data / logs
        dump = json.dumps(self.query("SELECT * FROM crawl_jobs") + self.query("SELECT * FROM crawl_log"))
        self.assertNotIn(TOKEN, dump)

        self.fake.calls.clear()
        status = self.run_job()
        self.assertEqual(status["state"], "completed")
        self.assertIn("since", self.fake.commit_requests()[0])

    def test_prefix_change_forces_full_scan_and_backfill(self) -> None:
        self.run_job()
        self.store.update({"gitlab": {"issue_prefixes": ["BUG"]}})
        self.fake.calls.clear()
        status = self.run_job()
        self.assertEqual(status["state"], "completed")
        self.assertNotIn("since", self.fake.commit_requests()[0])
        uids = {r[0] for r in self.query("SELECT uid FROM documents WHERE source_type='gitlab'")}
        self.assertEqual(uids, {f"gitlab:4711:{sha(1)}"})   # API-only commit removed
        self.store.update({"gitlab": {"require_issue_key": False}})
        self.run_job()
        self.assertEqual(self.query("SELECT COUNT(*) FROM documents WHERE source_type='gitlab'")[0][0], 3)

    def test_explicit_full_rescan(self) -> None:
        self.run_job()
        self.fake.calls.clear()
        self.run_job({"refresh_existing": True})
        self.assertNotIn("since", self.fake.commit_requests()[0])

    def test_commit_budget_and_total_budget_skip_checkpoint(self) -> None:
        status = self.run_job({"max_gitlab_commits": 1})
        self.assertEqual(status["state"], "completed")
        self.assertIn("Commit-Budget", status["last_message"])
        self.assertEqual(status["gitlab_indexed"], 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM gitlab_sync_state")[0][0], 0)
        status = self.run_job({"max_total_documents": 1})
        self.assertEqual(status["gitlab_indexed"], 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM gitlab_sync_state")[0][0], 0)

    def test_request_budget_exhausted(self) -> None:
        status = self.run_job({"max_api_requests": 1})   # project lookup only
        self.assertEqual(status["state"], "completed")
        self.assertIn("Budget erschöpft", status["last_message"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM gitlab_sync_state")[0][0], 0)

    def test_bad_commit_prevents_checkpoint(self) -> None:
        original = gitlab.GitLabClient.commit_to_document

        def flaky(client, project, commit):
            if commit["id"] == sha(1):
                raise ValueError("broken commit")
            return original(client, project, commit)

        with patch.object(gitlab.GitLabClient, "commit_to_document", flaky):
            status = self.run_job()
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["errors"], 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM gitlab_sync_state")[0][0], 0)

    def test_unreadable_project_fails_and_listing_error_fails(self) -> None:
        self.store.update({"gitlab": {"project_ids": ["nope/missing"]}})
        status = self.run_job()
        self.assertEqual(status["state"], "failed")
        self.assertIn("nope/missing", status["last_message"])
        self.store.update({"gitlab": {"project_ids": ["nope/missing", "4711"]}})
        self.assertEqual(self.run_job()["state"], "completed")
        self.fake.fail_commit_pages.add(1)
        status = self.run_job({"refresh_existing": True})
        self.assertEqual(status["state"], "failed")

    def test_stop(self) -> None:
        entered, release = threading.Event(), threading.Event()
        inner = self.fake

        def slow(*args):
            entered.set()
            release.wait(5)
            return inner(*args)

        self.fake = slow
        mgr = self.manager()
        job_id = mgr.start({"gitlab_only": True})["job_id"]
        self.assertTrue(entered.wait(5))
        mgr.stop(job_id)
        release.set()
        self.assertEqual(wait_done(mgr, job_id)["state"], "stopped")
        self.assertEqual(self.query("SELECT COUNT(*) FROM gitlab_sync_state")[0][0], 0)

    def test_atlassian_refresh_ignores_gitlab_commits(self) -> None:
        self.run_job()
        conn = db.connect(self.db_path)
        try:
            uids = db.get_existing_document_uids(conn, ("confluence", "jira"))
        finally:
            conn.close()
        self.assertEqual(uids, ["jira:API-2323"])


# --------------------------------------------------------------------------- web API

class TestGitLabWebApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from knowledge.webserver import create_server

        cls.dir = tempfile.mkdtemp(prefix="test_gitlab_web_")
        cls.db_path = Path(cls.dir) / "k.sqlite"
        conn = db.open_db(cls.db_path)
        for uid, kind in (("confluence:1", "confluence"), ("jira:API-1", "jira"),
                          (f"gitlab:4711:{sha(1)}", "gitlab")):
            db.save_document(conn, FetchedDocument(uid, kind, uid.split(":", 1)[1], "Shared title",
                                                   "", "Shared title"), [Chunk("", "Shared title")])
        db.add_edge(conn, f"gitlab:4711:{sha(1)}", models.LinkTarget("jira:API-1", "mentions_jira"))
        conn.close()
        cls.store = ConfigStore(Path(cls.dir) / "config.json")
        cls.store.update({"gitlab": {"base_url": "https://gitlab.com", "token": TOKEN,
                                     "project_ids": ["dw191299/bonbot"]}})

        class Jobs:
            def is_running(self):
                return False

        cls.server = create_server(cls.store, Jobs(), "127.0.0.1", 0, web_dir=cls.dir, db_path=cls.db_path)
        cls.server.gitlab_transport = FakeGitLab("https://gitlab.com", [GITLAB_COM_PROJECT], {})
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Host": f"127.0.0.1:{self.port}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_search_include_gitlab_and_type(self) -> None:
        _, res = self.request("GET", "/api/search?q=Shared")
        self.assertEqual(len(res["results"]), 3)
        _, res = self.request("GET", "/api/search?q=Shared&include_gitlab=false")
        self.assertEqual({r["type"] for r in res["results"]}, {"confluence", "jira"})
        _, res = self.request("GET", "/api/search?q=Shared&type=gitlab")
        self.assertEqual([r["type"] for r in res["results"]], ["gitlab"])
        status, _ = self.request("GET", "/api/search?q=Shared&include_gitlab=maybe")
        self.assertEqual(status, 400)

    def test_graph_and_top_documents_filters(self) -> None:
        _, res = self.request("GET", "/api/graph?types=jira,gitlab")
        self.assertEqual({n["type"] for n in res["nodes"]}, {"jira", "gitlab"})
        _, res = self.request("GET", "/api/graph?include_gitlab=0")
        self.assertTrue(all(n["type"] != "gitlab" for n in res["nodes"]))
        status, _ = self.request("GET", "/api/graph?types=bogus")
        self.assertEqual(status, 400)
        status, res = self.request("GET", "/api/top-documents?types=confluence,gitlab")
        self.assertEqual(status, 200)

    def test_settings_and_connection_test_hide_token(self) -> None:
        status, res = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(res["gitlab"]["token"], "")
        self.assertTrue(res["gitlab"]["token_set"])
        status, res = self.request("POST", "/api/settings/test", {})
        self.assertEqual(status, 200)
        self.assertTrue(res["gitlab"]["ok"], res["gitlab"])
        self.assertNotIn(TOKEN, json.dumps(res))
        _, cfg = self.request("GET", "/api/config")
        self.assertTrue(cfg["configured"]["gitlab"])
        self.assertIn("gitlab_only", cfg["limits"])

    def test_gitlab_projects_endpoint(self) -> None:
        status, res = self.request("GET", "/api/gitlab/projects")
        self.assertEqual(status, 200, res)
        self.assertEqual([p["path"] for p in res["projects"]], ["dw191299/bonbot"])
        self.assertEqual(res["configured"], ["dw191299/bonbot"])
        self.assertFalse(res["truncated"])
        self.assertNotIn(TOKEN, json.dumps(res))
        status, _ = self.request("POST", "/api/gitlab/projects", {})
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
