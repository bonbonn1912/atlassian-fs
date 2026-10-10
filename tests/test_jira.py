from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest

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
