from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest
import urllib.parse

from knowledge.atlassian_http import HttpResponse, RateLimitedClient, RequestBudget
from knowledge.jira import JiraClient
from knowledge.links import LinkContext
from knowledge.models import (
    EDGE_JIRA_PARENT_OF,
    EDGE_JIRA_RELATES_TO,
    EDGE_LINKS_TO_CONFLUENCE,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict | list:
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


class TestJiraClient(unittest.TestCase):
    def setUp(self):
        self.stop_event = threading.Event()
        self.budget = RequestBudget(max_requests=50, max_duration_seconds=60)
        self.link_ctx = LinkContext(
            confluence_bases=("https://confluence.intern",),
            jira_bases=("https://jira.intern", "https://cloud.atlassian.net"),
            confluence_base="https://confluence.intern",
            jira_base="https://jira.intern",
            project_keys=frozenset({"PROJ", "OPS", "CLOUD"}),
        )

    def test_server_v2_fetch_document(self):
        issue_payload = load_fixture("jira_server_issue.json")
        remotelinks_payload = load_fixture("jira_remotelinks.json")

        def transport(url, headers, timeout, ssl_context, max_bytes):
            if "remotelink" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(remotelinks_payload).encode("utf-8"),
                )
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(issue_payload).encode("utf-8"),
            )

        client = RateLimitedClient(
            source="jira",
            base_url="https://jira.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=0,
            stop_event=self.stop_event,
            budget=self.budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
        )

        jira = JiraClient(
            http=client, api_mode="server_v2", ctx=self.link_ctx
        )
        doc = jira.fetch_document("PROJ-101", include_remote_links=True)

        self.assertEqual(doc.uid, "jira:PROJ-101")
        self.assertEqual(doc.title, "PROJ-101: Implement Knowledge Graph Service")
        self.assertEqual(doc.source_created_at, "2026-03-01T09:00:00.000+0100")
        self.assertEqual(doc.source_updated_at, "2026-03-10T14:30:00.000+0100")
        self.assertIn("Status: In Progress", doc.body_text)
        self.assertEqual(doc.space_or_project, "PROJ")
        self.assertEqual(doc.document_type, "Story")
        self.assertEqual(doc.status_label, "In Progress")
        self.assertIn("Overview", doc.body_text)

        # 1. Parent edge: X -> P ("jira_parent_of", "child of")
        parent_links = [
            lk
            for lk in doc.links
            if lk.uid == "jira:PROJ-100" and lk.kind == EDGE_JIRA_PARENT_OF
        ]
        self.assertEqual(len(parent_links), 1)
        self.assertEqual(parent_links[0].relation_label, "child of")

        # 2. Subtasks edge: X -> S ("jira_parent_of", "parent of")
        subtask_links = [
            lk
            for lk in doc.links
            if lk.uid == "jira:PROJ-102" and lk.kind == EDGE_JIRA_PARENT_OF
        ]
        self.assertEqual(len(subtask_links), 1)
        self.assertEqual(subtask_links[0].relation_label, "parent of")

        # 3. Issue links (inward / outward relation_label)
        inward_links = [
            lk
            for lk in doc.links
            if lk.uid == "jira:OPS-50" and lk.kind == EDGE_JIRA_RELATES_TO
        ]
        self.assertEqual(len(inward_links), 1)
        self.assertEqual(inward_links[0].relation_label, "is blocked by")

        outward_links = [
            lk
            for lk in doc.links
            if lk.uid == "jira:PROJ-200" and lk.kind == EDGE_JIRA_RELATES_TO
        ]
        self.assertEqual(len(outward_links), 1)
        self.assertEqual(outward_links[0].relation_label, "relates to")

        # 4. Remote links -> links_to_confluence
        confluence_links = [
            lk for lk in doc.links if lk.kind == EDGE_LINKS_TO_CONFLUENCE
        ]
        self.assertTrue(len(confluence_links) >= 1)
        self.assertTrue(
            any(
                "Architecture+Overview" in lk.evidence_url
                for lk in confluence_links
            )
        )

    def test_cloud_v3_fetch_document(self):
        cloud_payload = load_fixture("jira_cloud_issue.json")

        def transport(url, headers, timeout, ssl_context, max_bytes):
            if "remotelink" in url:
                return HttpResponse(200, {"content-type": "application/json"}, b"[]")
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(cloud_payload).encode("utf-8"),
            )

        client = RateLimitedClient(
            source="jira",
            base_url="https://cloud.atlassian.net",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=0,
            stop_event=self.stop_event,
            budget=self.budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
        )

        jira = JiraClient(
            http=client, api_mode="cloud_v3", ctx=self.link_ctx
        )
        doc = jira.fetch_document("CLOUD-10", include_remote_links=True)

        self.assertEqual(doc.uid, "jira:CLOUD-10")
        self.assertEqual(doc.title, "CLOUD-10: Cloud Migrations")
        self.assertEqual(doc.source_created_at, "2026-04-01T08:00:00.000Z")
        self.assertEqual(doc.source_updated_at, "2026-04-02T10:00:00.000Z")
        self.assertEqual(doc.document_type, "Task")
        self.assertEqual(doc.status_label, "To Do")
        self.assertIn("Migration details in ADF", doc.body_text)

    def test_remote_links_403_non_fatal(self):
        issue_payload = load_fixture("jira_server_issue.json")

        def transport(url, headers, timeout, ssl_context, max_bytes):
            if "remotelink" in url:
                return HttpResponse(403, {}, b"Forbidden")
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(issue_payload).encode("utf-8"),
            )

        client = RateLimitedClient(
            source="jira",
            base_url="https://jira.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=0,
            stop_event=self.stop_event,
            budget=self.budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
        )

        jira = JiraClient(
            http=client, api_mode="server_v2", ctx=self.link_ctx
        )
        # Should not raise ForbiddenError
        doc = jira.fetch_document("PROJ-101", include_remote_links=True)
        self.assertEqual(doc.uid, "jira:PROJ-101")

    def test_remote_link_with_space_url_is_ignored(self):
        """Space root URLs in remote links must not become graph nodes."""
        issue_payload = load_fixture("jira_server_issue.json")
        remotelinks = [
            {"object": {"url": "https://confluence.intern/display/ENG", "title": "Space ENG"}},
            {"object": {"url": "https://confluence.intern/pages/viewpage.action?pageId=12345",
                        "title": "DOC-1"}},
        ]

        def transport(url, headers, timeout, ssl_context, max_bytes):
            payload = remotelinks if "remotelink" in url else issue_payload
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        client = RateLimitedClient(
            source="jira", base_url="https://jira.intern", allowed_bases=(), sleep_seconds=0.0,
            timeout=10, max_retries=0, stop_event=threading.Event(),
            budget=RequestBudget(max_requests=10, max_duration_seconds=60), ssl_context=None,
            auth_headers={}, transport=transport)
        jira = JiraClient(client, "server_v2", self.link_ctx)
        doc = jira.fetch_document("PROJ-101", include_remote_links=True)

        remote_uids = [lk.uid for lk in doc.links if lk.source_field == "remotelink"]
        self.assertEqual(remote_uids, ["confluence:12345"])

    def test_moved_issue_key(self):
        moved_payload = {
            "id": "70001",
            "key": "NEW-202",
            "fields": {
                "summary": "This issue was moved to NEW project",
                "description": "Moved description",
                "status": {"name": "Open"},
                "issuetype": {"name": "Bug"},
                "project": {"key": "NEW"},
            },
        }

        def transport(url, headers, timeout, ssl_context, max_bytes):
            if "remotelink" in url:
                return HttpResponse(200, {"content-type": "application/json"}, b"[]")
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(moved_payload).encode("utf-8"),
            )

        client = RateLimitedClient(
            source="jira",
            base_url="https://jira.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=0,
            stop_event=self.stop_event,
            budget=self.budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
        )

        jira = JiraClient(
            http=client, api_mode="server_v2", ctx=self.link_ctx
        )
        # Request OLD-101, but response has NEW-202
        doc = jira.fetch_document("OLD-101", include_remote_links=True)
        self.assertEqual(doc.uid, "jira:NEW-202")
        self.assertEqual(doc.source_id, "NEW-202")
        self.assertEqual(doc.space_or_project, "NEW")
        # Dates missing from fields must be None (not manufactured)
        self.assertIsNone(doc.source_created_at)
        self.assertIsNone(doc.source_updated_at)


if __name__ == "__main__":
    unittest.main()


class TestJiraRemoteLinksToggle(unittest.TestCase):
    def test_no_remote_link_request_when_disabled(self):
        issue_payload = load_fixture("jira_server_issue.json")
        urls = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            urls.append(url)
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(issue_payload).encode("utf-8"))

        client = RateLimitedClient(
            source="jira", base_url="https://jira.intern", allowed_bases=(), sleep_seconds=0.0,
            timeout=10, max_retries=0, stop_event=threading.Event(),
            budget=RequestBudget(max_requests=5, max_duration_seconds=60), ssl_context=None,
            auth_headers={}, transport=transport)
        ctx = LinkContext.from_config({"confluence": {"base_url": "https://confluence.intern"},
                                       "jira": {"base_url": "https://jira.intern"}})
        doc = JiraClient(client, "server_v2", ctx).fetch_document("PROJ-101", include_remote_links=False)
        self.assertEqual(len(urls), 1)
        self.assertIn("fields=summary", urls[0])
        self.assertFalse([lk for lk in doc.links if lk.source_field == "remotelink"])

    def test_fetch_documents_bulk_server_uses_one_jql_search(self):
        issue = load_fixture("jira_server_issue.json")
        requested_urls: list[str] = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            if "remotelink" in url:
                return HttpResponse(200, {"content-type": "application/json"}, b"[]")
            if "/search" in url:
                payload = {"issues": [issue], "total": 1, "maxResults": 100}
            else:
                payload = issue
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        client = RateLimitedClient(
            source="jira", base_url="https://jira.intern", allowed_bases=(), sleep_seconds=0.0,
            timeout=10, max_retries=0, stop_event=threading.Event(),
            budget=RequestBudget(max_requests=10, max_duration_seconds=60), ssl_context=None,
            auth_headers={}, transport=transport)
        ctx = LinkContext.from_config({"confluence": {"base_url": "https://confluence.intern"},
                                       "jira": {"base_url": "https://jira.intern"}})
        bulk = JiraClient(client, "server_v2", ctx).fetch_documents_bulk(
            [issue["key"], "PROJ-999"], include_remote_links=False)

        search_urls = [u for u in requested_urls if "/search?" in u]
        self.assertEqual(len(search_urls), 1)  # one request for both keys
        self.assertIn("jql=key", search_urls[0])
        self.assertIn("PROJ-999", urllib.parse.unquote_plus(search_urls[0]))
        self.assertIn("maxResults=2", search_urls[0])
        self.assertEqual(bulk[issue["key"]].source_id, issue["key"])
        # Keys JQL cannot match (moved) are resolved individually: the returned
        # document carries the new key so the indexer can record the move.
        self.assertTrue(any("/issue/PROJ-999" in u for u in requested_urls))
        self.assertEqual(bulk["PROJ-999"].uid, "jira:PROJ-101")

    def test_fetch_documents_bulk_cloud_falls_back_to_classic_search(self):
        issue = load_fixture("jira_cloud_issue.json")
        requested_urls: list[str] = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            if "/search/jql" in url:
                return HttpResponse(404, {"content-type": "application/json"},
                                    json.dumps({"errorMessages": ["not found"]}).encode("utf-8"))
            if "/search" in url:
                return HttpResponse(200, {"content-type": "application/json"},
                                    json.dumps({"issues": [issue], "total": 1}).encode("utf-8"))
            return HttpResponse(200, {"content-type": "application/json"}, b"[]")

        client = RateLimitedClient(
            source="jira", base_url="https://cloud.atlassian.net", allowed_bases=(),
            sleep_seconds=0.0, timeout=10, max_retries=0, stop_event=threading.Event(),
            budget=RequestBudget(max_requests=10, max_duration_seconds=60), ssl_context=None,
            auth_headers={}, transport=transport)
        ctx = LinkContext.from_config({"confluence": {"base_url": "https://confluence.intern"},
                                       "jira": {"base_url": "https://cloud.atlassian.net"}})
        bulk = JiraClient(client, "cloud_v3", ctx).fetch_documents_bulk(
            [issue["key"]], include_remote_links=False)

        self.assertTrue(any("/rest/api/3/search/jql" in u for u in requested_urls))
        self.assertTrue(any("/rest/api/3/search?" in u for u in requested_urls))
        self.assertEqual(bulk[issue["key"]].source_id, issue["key"])

    def test_fetch_documents_bulk_falls_back_to_single_requests(self):
        """A broken search endpoint must not mark every issue as error."""
        issue = load_fixture("jira_server_issue.json")
        requested_urls: list[str] = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            if "/search" in url:
                return HttpResponse(503, {"content-type": "application/json"}, b"{}")
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(issue).encode("utf-8"))

        client = RateLimitedClient(
            source="jira", base_url="https://jira.intern", allowed_bases=(), sleep_seconds=0.0,
            timeout=10, max_retries=0, stop_event=threading.Event(),
            budget=RequestBudget(max_requests=10, max_duration_seconds=60), ssl_context=None,
            auth_headers={}, transport=transport)
        ctx = LinkContext.from_config({"confluence": {"base_url": "https://confluence.intern"},
                                       "jira": {"base_url": "https://jira.intern"}})
        bulk = JiraClient(client, "server_v2", ctx).fetch_documents_bulk(
            [issue["key"]], include_remote_links=False)

        self.assertTrue(any("/rest/api/2/search?" in u for u in requested_urls))
        self.assertTrue(any(f"/rest/api/2/issue/{issue['key']}" in u for u in requested_urls))
        self.assertEqual(bulk[issue["key"]].source_id, issue["key"])

    def test_fetch_documents_bulk_tolerates_remote_link_budget_exhaustion(self):
        """Remote links are optional enrichment: the fetched issue must survive."""
        issue = load_fixture("jira_server_issue.json")

        def transport(url, headers, timeout, ssl_context, max_bytes):
            payload = {"issues": [issue]} if "/search" in url else []
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        client = RateLimitedClient(
            source="jira", base_url="https://jira.intern", allowed_bases=(), sleep_seconds=0.0,
            timeout=10, max_retries=0, stop_event=threading.Event(),
            budget=RequestBudget(max_requests=1, max_duration_seconds=60), ssl_context=None,
            auth_headers={}, transport=transport)
        ctx = LinkContext.from_config({"confluence": {"base_url": "https://confluence.intern"},
                                       "jira": {"base_url": "https://jira.intern"}})
        bulk = JiraClient(client, "server_v2", ctx).fetch_documents_bulk(
            [issue["key"]], include_remote_links=True)

        doc = bulk[issue["key"]]
        self.assertEqual(doc.source_id, issue["key"])
        self.assertEqual([lk for lk in doc.links if lk.source_field == "remotelink"], [])
