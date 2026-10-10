from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest

from knowledge.atlassian_http import HttpResponse, RateLimitedClient, RequestBudget
from knowledge.confluence import ConfluenceClient
from knowledge.links import LinkContext
from knowledge.models import EDGE_PARENT_OF, InvalidResponseError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict | list:
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


class TestConfluenceClient(unittest.TestCase):
    def setUp(self):
        self.stop_event = threading.Event()
        self.budget = RequestBudget(max_requests=50, max_duration_seconds=60)
        self.link_ctx = LinkContext(
            confluence_bases=("https://intern/confluence",),
            jira_bases=("https://jira.intern",),
            confluence_base="https://intern/confluence",
            jira_base="https://jira.intern",
            project_keys=frozenset({"PROJ"}),
        )

    def test_server_fetch_document_with_children_pagination_and_context_path(self):
        page_payload = load_fixture("confluence_server_page.json")
        child_p1 = load_fixture("confluence_server_children_p1.json")
        child_p2 = load_fixture("confluence_server_children_p2.json")

        requested_urls = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            if "child/page?start=2" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(child_p2).encode("utf-8"),
                )
            elif "child/page" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(child_p1).encode("utf-8"),
                )
            else:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(page_payload).encode("utf-8"),
                )

        client = RateLimitedClient(
            source="confluence",
            base_url="https://intern/confluence",
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

        conf = ConfluenceClient(
            http=client, api_mode="server", ctx=self.link_ctx
        )
        doc = conf.fetch_document("12345", include_children=True)

        self.assertEqual(doc.uid, "confluence:12345")
        self.assertEqual(doc.title, "Architecture Overview")
        self.assertEqual(doc.space_or_project, "ENG")
        self.assertEqual(doc.source_version, "3")

        # Verify no duplicate context path in requests
        for u in requested_urls:
            self.assertNotIn("/confluence/confluence", u)
            self.assertTrue(u.startswith("https://intern/confluence/rest/api/"))

        # Verify child pages
        self.assertEqual(conf.list_child_pages("12345", 2), [("12346", "Backend Service"), ("12347", "Frontend Client")])

        # Verify outgoing links contain parent_of edges
        child_links = [
            lk for lk in doc.links if lk.kind == EDGE_PARENT_OF
        ]
        self.assertEqual(len(child_links), 3)
        self.assertEqual(child_links[0].uid, "confluence:12346")
        self.assertEqual(child_links[0].relation_label, "parent of")

        # Verify link to Jira issue PROJ-101 in body was extracted
        jira_links = [
            lk for lk in doc.links if lk.uid == "jira:PROJ-101"
        ]
        self.assertEqual(len(jira_links), 1)

    def test_include_children_false(self):
        page_payload = load_fixture("confluence_server_page.json")
        requested_urls = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(page_payload).encode("utf-8"),
            )

        client = RateLimitedClient(
            source="confluence",
            base_url="https://intern/confluence",
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

        conf = ConfluenceClient(
            http=client, api_mode="server", ctx=self.link_ctx
        )
        doc = conf.fetch_document("12345", include_children=False)

        # Only one request should be made
        self.assertEqual(len(requested_urls), 1)
        self.assertFalse([lk for lk in doc.links if lk.kind == EDGE_PARENT_OF])

    def test_missing_body_raises_invalid_response_error(self):
        payload_no_body = {
            "id": "12345",
            "title": "Empty Page",
            "body": {},
        }

        def transport(url, headers, timeout, ssl_context, max_bytes):
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(payload_no_body).encode("utf-8"),
            )

        client = RateLimitedClient(
            source="confluence",
            base_url="https://intern/confluence",
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

        conf = ConfluenceClient(
            http=client, api_mode="server", ctx=self.link_ctx
        )
        with self.assertRaises(InvalidResponseError):
            conf.fetch_document("12345", include_children=True)

    def test_resolve_title(self):
        def transport(url, headers, timeout, ssl_context, max_bytes):
            if "title=Single" in url:
                payload = {"results": [{"id": "1001", "title": "Single"}]}
            elif "title=Multiple" in url:
                payload = {
                    "results": [
                        {"id": "1001", "title": "Multiple"},
                        {"id": "1002", "title": "Multiple"},
                    ]
                }
            else:
                payload = {"results": []}
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps(payload).encode("utf-8"),
            )

        client = RateLimitedClient(
            source="confluence",
            base_url="https://intern/confluence",
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

        conf = ConfluenceClient(
            http=client, api_mode="server", ctx=self.link_ctx
        )
        # Exactly 1 hit -> returns page id
        self.assertEqual(conf.resolve_title("ENG", "Single"), "1001")
        # 0 hits -> None
        self.assertIsNone(conf.resolve_title("ENG", "Unknown"))
        # 2 hits -> None
        self.assertIsNone(conf.resolve_title("ENG", "Multiple"))

    def test_cloud_v2_fetch_document_and_space_cache(self):
        cloud_page = load_fixture("confluence_cloud_page.json")
        space_payload = {"id": "1001", "key": "CLOUD", "name": "Cloud Space"}
        children_payload = {
            "results": [
                {"id": "99998", "title": "Child Page", "type": "page"},
                {
                    "id": "88888",
                    "title": "Whiteboard",
                    "type": "whiteboard",
                },  # Should be ignored
            ],
            "_links": {},
        }

        space_requests = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            if "/api/v2/spaces/1001" in url:
                space_requests.append(url)
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(space_payload).encode("utf-8"),
                )
            elif "children" in url:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(children_payload).encode("utf-8"),
                )
            else:
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(cloud_page).encode("utf-8"),
                )

        client = RateLimitedClient(
            source="confluence",
            base_url="https://cloud.atlassian.net/wiki",
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

        conf = ConfluenceClient(
            http=client, api_mode="cloud_v2", ctx=self.link_ctx
        )
        doc1 = conf.fetch_document("99999", include_children=True)

        self.assertEqual(doc1.uid, "confluence:99999")
        self.assertEqual(doc1.space_or_project, "CLOUD")
        self.assertIsNone(doc1.status_label)  # "current" is the normal state
        self.assertEqual(len(space_requests), 1)

        # Only type == "page" children included
        children = [lk.uid for lk in doc1.links if lk.kind == EDGE_PARENT_OF]
        self.assertEqual(children, ["confluence:99998"])

        # Second fetch should use space cache
        doc2 = conf.fetch_document("99999", include_children=False)
        self.assertEqual(doc2.space_or_project, "CLOUD")
        self.assertEqual(len(space_requests), 1)  # Cached!


if __name__ == "__main__":
    unittest.main()
