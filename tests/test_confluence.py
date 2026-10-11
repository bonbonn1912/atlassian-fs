from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest
import urllib.parse

from knowledge.atlassian_http import HttpResponse, RateLimitedClient, RequestBudget
from knowledge.confluence import ConfluenceClient
from knowledge.links import LinkContext
from knowledge.models import (
    EDGE_PARENT_OF,
    FetchedDocument,
    ForbiddenError,
    InvalidResponseError,
)

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


    # ------------------------------------------------------------ spaces & updates

    def _make_client(self, transport, base_url="https://intern/confluence", api_mode="server"):
        client = RateLimitedClient(
            source="confluence",
            base_url=base_url,
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
        return ConfluenceClient(http=client, api_mode=api_mode, ctx=self.link_ctx)

    def test_list_spaces_server_paginates_dedupes_and_sorts(self):
        requested_urls: list[str] = []
        page1 = {
            "results": [
                {"id": "10", "key": "TEAM", "name": "Team Space", "type": "global"},
                {"id": "11", "key": "KAFKA", "name": "kafka", "type": "global"},
                {"id": "12", "key": "ARCH", "name": "Architecture", "type": "global"},
            ],
            "limit": 10, "start": 0, "size": 3,
            "_links": {"next": "/rest/api/space?limit=10&start=3"},
        }
        page2 = {
            "results": [
                {"id": "11", "key": "KAFKA", "name": "kafka", "type": "global"},
                {"id": "13", "key": "OPS", "name": "Operations", "type": "personal"},
            ],
            "limit": 10, "start": 3, "size": 2,
        }

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            payload = page2 if "start=3" in url else page1
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport)
        spaces = conf.list_spaces(max_items=10)

        self.assertEqual([s["key"] for s in spaces], ["ARCH", "KAFKA", "OPS", "TEAM"])
        self.assertEqual(spaces[0]["url"], "https://intern/confluence/display/ARCH")
        self.assertEqual([s["name"] for s in spaces],
                         ["Architecture", "kafka", "Operations", "Team Space"])
        self.assertEqual(len(requested_urls), 2)
        self.assertTrue(requested_urls[0].startswith("https://intern/confluence/rest/api/space?"))
        self.assertIn("start=3", requested_urls[1])

    def test_list_spaces_cloud_v2_follows_cursor(self):
        requested_urls: list[str] = []
        page1 = {
            "results": [{"id": "1", "key": "ENG", "name": "Engineering", "type": "global"}],
            "_links": {"next": "/wiki/api/v2/spaces?limit=50&cursor=abc"},
        }
        page2 = {
            "results": [{"id": "2", "key": "DOC", "name": "Docs", "type": "global"}],
        }

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            payload = page2 if "cursor=abc" in url else page1
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport, base_url="https://cloud.atlassian.net/wiki",
                                 api_mode="cloud_v2")
        spaces = conf.list_spaces()

        self.assertEqual([s["key"] for s in spaces], ["DOC", "ENG"])
        self.assertEqual(spaces[0]["url"], "https://cloud.atlassian.net/wiki/spaces/DOC")
        self.assertEqual(requested_urls[0],
                         "https://cloud.atlassian.net/wiki/api/v2/spaces?limit=100")
        self.assertIn("/wiki/api/v2/spaces?limit=50&cursor=abc", requested_urls[1])

    def test_list_pages_modified_since_builds_cql_and_paginates(self):
        requested_urls: list[str] = []
        page1 = {
            "results": [
                {"id": "1", "type": "page", "title": "Neue Seite",
                 "space": {"key": "ENG"}, "version": {"when": "2026-10-05T10:00:00.000Z"}},
                {"id": "99", "type": "blogpost", "title": "Blogpost",
                 "space": {"key": "ENG"}, "version": {"when": "2026-10-05T11:00:00.000Z"}},
            ],
            "limit": 50, "start": 0, "size": 2,
            "_links": {"next": "/rest/api/content/search?cql=x&limit=50&start=50"},
        }
        page2 = {
            "results": [
                {"id": "1", "type": "page", "title": "Neue Seite",
                 "space": {"key": "ENG"}, "version": {"when": "2026-10-05T10:00:00.000Z"}},
                {"id": "2", "type": "page", "title": "Zweite Seite",
                 "space": {"key": "TEAM"}, "version": {"when": "2026-10-04T08:30:00.000Z"}},
            ],
            "limit": 50, "start": 50, "size": 2,
        }

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            payload = page2 if "start=50" in url else page1
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport)
        pages = conf.list_pages_modified_since("2026-10-04", space_keys=["ENG", "TEAM"])

        self.assertEqual([p["id"] for p in pages], ["1", "2"])
        self.assertEqual(pages[0]["space"], "ENG")
        self.assertEqual(pages[1]["updated_at"], "2026-10-04T08:30:00.000Z")

        first = urllib.parse.unquote_plus(requested_urls[0])
        self.assertIn("/rest/api/content/search?", first)
        self.assertIn('type=page and lastmodified >= "2026-10-04"', first)
        self.assertIn('space in ("ENG", "TEAM")', first)

    def test_list_pages_modified_since_without_space_filter(self):
        requested_urls: list[str] = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            payload = {"results": [], "limit": 50, "start": 0, "size": 0}
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport)
        pages = conf.list_pages_modified_since("2026-10-04", space_keys=[])

        self.assertEqual(pages, [])
        first = urllib.parse.unquote_plus(requested_urls[0])
        self.assertNotIn("space in", first)
        self.assertIn("order by lastmodified desc", first)

    # ------------------------------------------------------------ bulk fetch

    def test_fetch_documents_bulk_server_uses_single_cql_request(self):
        requested_urls: list[str] = []

        def page(pid, title):
            return {
                "id": pid, "type": "page", "status": "current", "title": title,
                "space": {"key": "ENG"}, "version": {"number": 2, "when": "2026-10-05T09:00:00.000Z"},
                "body": {"storage": {"value": f"<p>Inhalt {pid}</p>"}},
                "_links": {"webui": f"/display/ENG/{pid}"},
            }

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            if "/rest/api/content/3" in url and "/search" not in url and "child" not in url:
                return HttpResponse(403, {"content-type": "application/json"},
                                    json.dumps({"message": "forbidden"}).encode("utf-8"))
            payload = {"results": [page("1", "Seite 1"), page("2", "Seite 2")],
                       "limit": 3, "start": 0, "size": 2}
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport)
        result = conf.fetch_documents_bulk(["1", "2", "3"], include_children=False)

        # one bulk request for all ids + one fallback for the id the bulk answer omitted
        self.assertEqual(len(requested_urls), 2)
        first = urllib.parse.unquote_plus(requested_urls[0])
        self.assertIn("/rest/api/content/search?", first)
        self.assertIn("cql=id in (1,2,3)", first)
        self.assertIn("limit=3", first)
        self.assertIn("/rest/api/content/3?", requested_urls[1])
        self.assertEqual(result["1"].title, "Seite 1")
        self.assertEqual(result["2"].space_or_project, "ENG")
        self.assertIsInstance(result["3"], ForbiddenError)

    def test_fetch_documents_bulk_cloud_uses_id_params(self):
        requested_urls: list[str] = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            if "/api/v2/spaces/99" in url:
                payload = {"id": "99", "key": "CLOUD", "name": "Cloud Space"}
            else:
                payload = {"results": [
                    {"id": "11", "type": "page", "status": "current", "title": "Cloud-Seite",
                     "spaceId": "99", "version": {"number": 4, "createdAt": "2026-10-05T09:00:00.000Z"},
                     "body": {"storage": {"value": "<p>Cloud-Inhalt</p>"}},
                     "_links": {"webui": "/spaces/CLOUD/pages/11"}},
                ]}
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport, base_url="https://cloud.atlassian.net/wiki",
                                 api_mode="cloud_v2")
        result = conf.fetch_documents_bulk(["11", "12"], include_children=False)

        ids_url = next(u for u in requested_urls if "/api/v2/pages?" in u)
        self.assertIn("id=11", ids_url)
        self.assertIn("id=12", ids_url)
        self.assertIn("body-format=storage", ids_url)
        self.assertEqual(result["11"].space_or_project, "CLOUD")
        self.assertEqual(result["11"].source_version, "4")
        self.assertIsInstance(result["12"], Exception)

    def test_fetch_documents_bulk_rejects_invalid_ids(self):
        conf = self._make_client(lambda *args: None)
        result = conf.fetch_documents_bulk(["abc"], include_children=False)
        self.assertIsInstance(result["abc"], ValueError)

    def test_fetch_documents_bulk_falls_back_to_single_requests(self):
        """A broken bulk endpoint must not mark every document as error."""
        requested_urls: list[str] = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            if "/search" in url:
                return HttpResponse(500, {"content-type": "application/json"}, b"{}")
            pid = url.rstrip("/").split("/")[-1].split("?")[0]
            return HttpResponse(200, {"content-type": "application/json"}, json.dumps({
                "id": pid, "type": "page", "title": f"Seite {pid}", "space": {"key": "ENG"},
                "version": {"number": 1}, "body": {"storage": {"value": "<p>x</p>"}},
                "_links": {"webui": f"/display/ENG/{pid}"},
            }).encode("utf-8"))

        conf = self._make_client(transport)
        result = conf.fetch_documents_bulk(["1", "2"], include_children=False)

        self.assertTrue(all(isinstance(result[pid], FetchedDocument) for pid in ("1", "2")))
        self.assertIn("/rest/api/content/search", requested_urls[0])
        self.assertIn("/rest/api/content/1?", requested_urls[1])
        self.assertIn("/rest/api/content/2?", requested_urls[2])

    def test_fetch_documents_bulk_tolerates_child_budget_exhaustion(self):
        """Child lists are optional enrichment: the fetched page must survive."""
        def transport(url, headers, timeout, ssl_context, max_bytes):
            payload = {"results": [{
                "id": "1", "type": "page", "title": "Seite 1", "space": {"key": "ENG"},
                "version": {"number": 1}, "body": {"storage": {"value": "<p>x</p>"}},
                "_links": {"webui": "/display/ENG/1"},
            }]} if "/search" in url else {"results": [], "limit": 100, "start": 0, "size": 0}
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        client = RateLimitedClient(
            source="confluence", base_url="https://intern/confluence", allowed_bases=(),
            sleep_seconds=0.0, timeout=10, max_retries=0, stop_event=self.stop_event,
            budget=RequestBudget(max_requests=1, max_duration_seconds=60),
            ssl_context=None, auth_headers={}, transport=transport,
        )
        conf = ConfluenceClient(http=client, api_mode="server", ctx=self.link_ctx)
        result = conf.fetch_documents_bulk(["1"], include_children=True)

        self.assertIsInstance(result["1"], FetchedDocument)
        self.assertEqual([lk for lk in result["1"].links if lk.kind == EDGE_PARENT_OF], [])
        self.assertEqual(result["1"].title, "Seite 1")

    def test_fetch_documents_bulk_keeps_page_when_child_list_forbidden(self):
        """A 403 on the child listing must not discard the fetched page."""
        def transport(url, headers, timeout, ssl_context, max_bytes):
            if "/child/page" in url:
                return HttpResponse(403, {"content-type": "application/json"}, b"{}")
            payload = {"results": [{
                "id": "1", "type": "page", "title": "Seite 1", "space": {"key": "ENG"},
                "version": {"number": 1}, "body": {"storage": {"value": "<p>x</p>"}},
                "_links": {"webui": "/display/ENG/1"},
            }]}
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport)
        result = conf.fetch_documents_bulk(["1"], include_children=True)

        self.assertIsInstance(result["1"], FetchedDocument)
        self.assertEqual(result["1"].title, "Seite 1")
        self.assertEqual(result["1"].body_text.strip(), "x")

    def test_resolve_titles_bulk_uses_one_cql_request(self):
        requested_urls: list[str] = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            payload = {"results": [
                {"id": "111", "title": "Titel A", "space": {"key": "ENG"}},
                {"id": "222", "title": "Titel B", "space": {"key": "ENG"}},
                {"id": "333", "title": "Doppelt", "space": {"key": "ENG"}},
                {"id": "444", "title": "Doppelt", "space": {"key": "ENG"}},
            ], "limit": 16, "start": 0, "size": 4}
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport)
        result = conf.resolve_titles_bulk([
            ("ENG", "Titel A"), ("ENG", "Titel B"), ("ENG", "Doppelt"), ("ENG", "Fehlt"),
        ])

        self.assertEqual(len(requested_urls), 1)   # ein Request für alle Titel
        first = urllib.parse.unquote_plus(requested_urls[0])
        self.assertIn("/rest/api/content/search?", first)
        self.assertIn('title in ("Titel A", "Titel B", "Doppelt", "Fehlt")', first)
        self.assertIn('space in ("ENG")', first)
        self.assertEqual(result[("ENG", "Titel A")], "111")
        self.assertEqual(result[("ENG", "Titel B")], "222")
        self.assertIsNone(result[("ENG", "Doppelt")])   # mehrdeutig
        self.assertIsNone(result[("ENG", "Fehlt")])     # nicht gefunden

    def test_resolve_titles_bulk_falls_back_for_cql_unsafe_titles(self):
        requested_urls: list[str] = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            requested_urls.append(url)
            payload = {"results": [{"id": "999", "title": 'Mit "Quote"', "space": {"key": "ENG"}}],
                       "limit": 2, "start": 0, "size": 1}
            return HttpResponse(200, {"content-type": "application/json"},
                                json.dumps(payload).encode("utf-8"))

        conf = self._make_client(transport)
        result = conf.resolve_titles_bulk([("ENG", 'Mit "Quote"')])

        self.assertEqual(result[("ENG", 'Mit "Quote"')], "999")
        self.assertIn("/rest/api/content?", requested_urls[0])   # Einzelabruf-Endpoint
        self.assertNotIn("/search?", requested_urls[0])


if __name__ == "__main__":
    unittest.main()
