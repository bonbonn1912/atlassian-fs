"""Unit tests for JobManager and BFS crawler in knowledge/indexer.py.

All tests are hermetic and run in-memory without network calls.
"""

from __future__ import annotations

import collections
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest

from knowledge import db
from knowledge.config import ConfigError, ConfigStore, save_config
from knowledge.indexer import JobConflict, JobManager, RequestBudget
from knowledge.models import (
    AtlassianError,
    BudgetExceeded,
    Chunk,
    CrawlStopped,
    EDGE_JIRA_PARENT_OF,
    EDGE_JIRA_RELATES_TO,
    EDGE_LINKS_TO,
    EDGE_LINKS_TO_CONFLUENCE,
    EDGE_MENTIONS_JIRA,
    EDGE_PARENT_OF,
    FetchedDocument,
    ForbiddenError,
    InvalidResponseError,
    JOB_ACTIVE_STATES,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_RUNNING,
    JOB_STOPPED,
    JOB_STOPPING,
    LinkTarget,
    NotFoundError,
    STATE_ERROR,
    STATE_FORBIDDEN,
    STATE_INDEXED,
    STATE_MISSING,
    STATE_SKIPPED,
    confluence_uid,
    jira_uid,
    unresolved_page_uid,
)


class FakeConfluenceClient:
    """Mock ConfluenceClient serving documents from an in-memory dictionary."""

    def __init__(
        self,
        pages: dict[str, FetchedDocument | Exception],
        title_resolutions: dict[tuple[str, str], str] | None = None,
        budget: RequestBudget | None = None,
        on_fetch: callable | None = None,
        space_map: dict[str, list[tuple[str, str]]] | None = None,
        modified_pages: list[dict] | None = None,
    ) -> None:
        self.pages = pages
        self.title_resolutions = title_resolutions or {}
        self.budget = budget
        self.on_fetch = on_fetch
        self.space_map = space_map
        self.modified_pages = modified_pages
        self.fetch_calls: list[str] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self.space_calls: list[str] = []
        self.modified_calls: list[tuple[str, tuple[str, ...]]] = []

    def fetch_document(
        self,
        page_id: str,
        *,
        include_children: bool = True,
        scan_plain_keys: bool = False,
    ) -> FetchedDocument:
        if self.budget is not None:
            self.budget.consume()
        self.fetch_calls.append(page_id)
        if self.on_fetch:
            self.on_fetch(page_id)
        if page_id not in self.pages:
            raise NotFoundError(f"Confluence page not found: {page_id}")
        doc = self.pages[page_id]
        if isinstance(doc, Exception):
            raise doc
        return doc

    def resolve_title(self, space: str, title: str) -> str | None:
        if self.budget is not None:
            self.budget.consume()
        self.resolve_calls.append((space, title))
        return self.title_resolutions.get((space, title))

    def list_space_pages(self, space_key: str, max_items: int = 100_000) -> list[tuple[str, str]]:
        if self.budget is not None:
            self.budget.consume()
        self.space_calls.append(space_key)
        if self.space_map is not None:
            return list(self.space_map.get(space_key, []))[:max_items]
        results = []
        for pid, doc in self.pages.items():
            if not isinstance(doc, Exception):
                if not getattr(doc, "space_or_project", None) or doc.space_or_project == space_key:
                    results.append((pid, doc.title or f"Page {pid}"))
            else:
                results.append((pid, f"Page {pid}"))
            if len(results) >= max_items:
                break
        return results

    def list_pages_modified_since(
        self,
        since: str,
        space_keys: list[str] | None = None,
        max_items: int = 10_000,
    ) -> list[dict]:
        if self.budget is not None:
            self.budget.consume()
        self.modified_calls.append((since, tuple(space_keys or [])))
        if self.modified_pages is not None:
            return list(self.modified_pages)[:max_items]
        out = []
        for pid, doc in self.pages.items():
            if isinstance(doc, Exception):
                continue
            out.append({
                "id": pid,
                "title": doc.title or "",
                "space": getattr(doc, "space_or_project", "") or "",
                "updated_at": getattr(doc, "source_updated_at", "") or "",
            })
            if len(out) >= max_items:
                break
        return out


class FakeBulkConfluenceClient(FakeConfluenceClient):
    """Like FakeConfluenceClient but with the bulk fetch API used for batching."""

    def __init__(self, *args, space_parents: dict[str, str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bulk_calls: list[list[str]] = []
        self.bulk_include_children: list[bool] = []
        self.space_parents = space_parents or {}
        self.child_calls: list[str] = []

    def list_space_pages_with_parents(
        self, space_key: str, max_items: int = 100_000
    ) -> list[tuple[str, str, str]]:
        if self.space_parents:
            if self.budget is not None:
                self.budget.consume()
            out = []
            for pid, parent in self.space_parents.items():
                doc = self.pages.get(pid)
                title = doc.title if isinstance(doc, FetchedDocument) else f"Page {pid}"
                out.append((pid, title or f"Page {pid}", parent))
            return out[:max_items]
        # No ancestor data configured -> behave like the plain listing
        return [
            (pid, title, "")
            for pid, title in FakeConfluenceClient.list_space_pages(self, space_key, max_items)
        ]

    def enrich_children(self, doc: FetchedDocument, max_children: int = 500) -> None:
        if self.budget is not None:
            self.budget.consume()
        self.child_calls.append(doc.source_id)

    def fetch_documents_bulk(
        self,
        page_ids: list[str],
        *,
        include_children: bool = False,
        max_children: int = 500,
        scan_plain_keys: bool = False,
    ) -> dict[str, FetchedDocument | Exception]:
        if self.budget is not None:
            self.budget.consume()
        self.bulk_calls.append(list(page_ids))
        self.bulk_include_children.append(bool(include_children))
        out: dict[str, FetchedDocument | Exception] = {}
        for pid in page_ids:
            doc = self.pages.get(pid)
            out[pid] = doc if doc is not None else NotFoundError(f"Confluence page not found: {pid}")
        return out


class FakeJiraClient:
    """Mock JiraClient serving documents from an in-memory dictionary."""

    def __init__(
        self,
        issues: dict[str, FetchedDocument | Exception],
        budget: RequestBudget | None = None,
        on_fetch: callable | None = None,
    ) -> None:
        self.issues = issues
        self.budget = budget
        self.on_fetch = on_fetch
        self.fetch_calls: list[str] = []

    def fetch_document(
        self,
        issue_key: str,
        *,
        include_remote_links: bool = True,
        scan_plain_keys: bool = False,
    ) -> FetchedDocument:
        if self.budget is not None:
            self.budget.consume()
        self.fetch_calls.append(issue_key)
        if self.on_fetch:
            self.on_fetch(issue_key)
        if issue_key not in self.issues:
            raise NotFoundError(f"Jira issue not found: {issue_key}")
        doc = self.issues[issue_key]
        if isinstance(doc, Exception):
            raise doc
        return doc


class FakeBulkJiraClient(FakeJiraClient):
    """Like FakeJiraClient but with the bulk fetch API used for batching."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bulk_calls: list[list[str]] = []

    def fetch_documents_bulk(
        self,
        issue_keys: list[str],
        *,
        include_remote_links: bool = True,
        scan_plain_keys: bool = False,
        chunk_size: int = 100,
    ) -> dict[str, FetchedDocument | Exception]:
        if self.budget is not None:
            self.budget.consume()
        self.bulk_calls.append(list(issue_keys))
        out: dict[str, FetchedDocument | Exception] = {}
        for key in issue_keys:
            doc = self.issues.get(key)
            out[key] = doc if doc is not None else NotFoundError(f"Jira issue not found: {key}")
        return out


class FakeBulkTitleConfluenceClient(FakeBulkConfluenceClient):
    """Bulk client that also offers the batched title resolver."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bulk_resolve_calls: list[list[tuple[str, str]]] = []

    def resolve_titles_bulk(self, pairs, chunk_size: int = 50):
        if self.budget is not None:
            self.budget.consume()
        self.bulk_resolve_calls.append(list(pairs))
        return {key: self.title_resolutions.get(key) for key in pairs}


def make_test_config(tmp_dir: Path) -> ConfigStore:
    cfg_file = tmp_dir / "config.json"
    save_config(cfg_file, {
        "confluence": {
            "base_url": "https://wiki.example.com",
            "aliases": ["https://confluence.example.com"],
            "auth": {"type": "bearer", "token": "conf-token"},
            "sleep_seconds": 0.0,
        },
        "jira": {
            "base_url": "https://jira.example.com",
            "aliases": [],
            "auth": {"type": "bearer", "token": "jira-token"},
            "project_keys": ["PROJ", "TEST"],
            "sleep_seconds": 0.0,
        },
        "http": {
            "verify_ssl": False,
            "max_response_mb": 5,
        },
        "index": {
            "chunk_target_chars": 500,
            "chunk_overlap_chars": 50,
            "min_chunk_chars": 20,
        },
    })
    return ConfigStore(cfg_file)


def make_doc(
    uid: str,
    title: str,
    links: list[LinkTarget] | None = None,
    body: str = "Test body content",
    sections: list[tuple[str, str]] | None = None,
    space_or_project: str | None = None,
) -> FetchedDocument:
    source_type, source_id = uid.split(":", 1)
    return FetchedDocument(
        uid=uid,
        source_type=source_type,
        source_id=source_id,
        title=title,
        url=f"https://wiki.example.com/item/{source_id}",
        body_text=body,
        space_or_project=space_or_project,
        sections=sections or [("Section 1", body)],
        links=links or [],
    )


def wait_job_done(mgr: JobManager, job_id: int, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = mgr.status(job_id)
        if st and st["state"] not in JOB_ACTIVE_STATES:
            return st
        time.sleep(0.01)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s: {mgr.status(job_id)}")


class TestIndexer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "test.db"
        self.config_store = make_test_config(self.tmp_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_bfs_order_and_dedupe_cycles(self) -> None:
        """Verify BFS queue FIFO ordering and cycle deduplication."""
        # 1 -> 2, 3
        # 2 -> 3, 1 (cycles)
        # 3 -> 1
        p1 = make_doc(
            confluence_uid("1"),
            "Page 1",
            [
                LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("3"), kind=EDGE_LINKS_TO),
            ],
        )
        p2 = make_doc(
            confluence_uid("2"),
            "Page 2",
            [
                LinkTarget(uid=confluence_uid("3"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("1"), kind=EDGE_LINKS_TO),
            ],
        )
        p3 = make_doc(
            confluence_uid("3"),
            "Page 3",
            [
                LinkTarget(uid=confluence_uid("1"), kind=EDGE_LINKS_TO),
            ],
        )

        conf_client = FakeConfluenceClient({"1": p1, "2": p2, "3": p3})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"start_page": "1", "follow_confluence_links": True})
        job_id = res["job_id"]
        st = wait_job_done(mgr, job_id)

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 3)
        # BFS order: 1, then 2, then 3
        self.assertEqual(conf_client.fetch_calls, ["1", "2", "3"])

        # Check DB documents
        with db.connect(self.db_path) as conn:
            self.assertIsNotNone(db.get_document(conn, confluence_uid("1")))
            self.assertIsNotNone(db.get_document(conn, confluence_uid("2")))
            self.assertIsNotNone(db.get_document(conn, confluence_uid("3")))

    def test_confluence_depth_limit(self) -> None:
        """Verify crawling stops when max_confluence_depth is reached."""
        # 1 (depth 0) -> 2 (depth 1) -> 3 (depth 2) -> 4 (depth 3)
        p1 = make_doc(confluence_uid("1"), "P1", [LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO)])
        p2 = make_doc(confluence_uid("2"), "P2", [LinkTarget(uid=confluence_uid("3"), kind=EDGE_LINKS_TO)])
        p3 = make_doc(confluence_uid("3"), "P3", [LinkTarget(uid=confluence_uid("4"), kind=EDGE_LINKS_TO)])
        p4 = make_doc(confluence_uid("4"), "P4", [])

        conf_client = FakeConfluenceClient({"1": p1, "2": p2, "3": p3, "4": p4})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({
            "start_page": "1",
            "follow_confluence_links": True,
            "max_confluence_depth": 2,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(conf_client.fetch_calls, ["1", "2", "3"])
        self.assertNotIn("4", conf_client.fetch_calls)

    def test_jira_hops_limit(self) -> None:
        """Verify Jira traversal stops when max_jira_link_hops is exceeded."""
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [LinkTarget(uid=jira_uid("PROJ-1"), kind=EDGE_MENTIONS_JIRA)],
        )
        j1 = make_doc(
            jira_uid("PROJ-1"),
            "J1",
            [LinkTarget(uid=jira_uid("PROJ-2"), kind=EDGE_JIRA_RELATES_TO)],
        )
        j2 = make_doc(
            jira_uid("PROJ-2"),
            "J2",
            [LinkTarget(uid=jira_uid("PROJ-3"), kind=EDGE_JIRA_RELATES_TO)],
        )
        j3 = make_doc(
            jira_uid("PROJ-3"),
            "J3",
            [LinkTarget(uid=jira_uid("PROJ-4"), kind=EDGE_JIRA_RELATES_TO)],
        )
        j4 = make_doc(jira_uid("PROJ-4"), "J4", [])

        conf_client = FakeConfluenceClient({"1": p1})
        jira_client = FakeJiraClient({"PROJ-1": j1, "PROJ-2": j2, "PROJ-3": j3, "PROJ-4": j4})

        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, jira_client),
        )

        res = mgr.start({
            "start_page": "1",
            "index_linked_jira_issues": True,
            "follow_jira_issue_links": True,
            "max_jira_link_hops": 2,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 1)
        self.assertEqual(st["jira_indexed"], 3)  # PROJ-1 (0 hops), PROJ-2 (1 hop), PROJ-3 (2 hops)
        self.assertEqual(jira_client.fetch_calls, ["PROJ-1", "PROJ-2", "PROJ-3"])

    def test_type_budgets(self) -> None:
        """Verify type-specific page and issue limits skip further items of that type."""
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [
                LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=jira_uid("PROJ-1"), kind=EDGE_MENTIONS_JIRA),
                LinkTarget(uid=jira_uid("PROJ-2"), kind=EDGE_MENTIONS_JIRA),
                LinkTarget(uid=jira_uid("PROJ-3"), kind=EDGE_MENTIONS_JIRA),
            ],
        )
        p2 = make_doc(confluence_uid("2"), "P2", [])
        j1 = make_doc(jira_uid("PROJ-1"), "J1", [])
        j2 = make_doc(jira_uid("PROJ-2"), "J2", [])
        j3 = make_doc(jira_uid("PROJ-3"), "J3", [])

        conf_client = FakeConfluenceClient({"1": p1, "2": p2})
        jira_client = FakeJiraClient({"PROJ-1": j1, "PROJ-2": j2, "PROJ-3": j3})

        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, jira_client),
        )

        res = mgr.start({
            "start_page": "1",
            "follow_confluence_links": True,
            "index_linked_jira_issues": True,
            "max_confluence_pages": 1,
            "max_jira_issues": 2,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 1)
        self.assertEqual(st["jira_indexed"], 2)
        self.assertEqual(st["skipped"], 2)  # P2 and PROJ-3 skipped due to budget
        self.assertEqual(conf_client.fetch_calls, ["1"])
        self.assertEqual(jira_client.fetch_calls, ["PROJ-1", "PROJ-2"])

        # Check queue states in DB
        with db.connect(self.db_path) as conn:
            counts = db.queue_counts(conn, res["job_id"])
            self.assertEqual(counts.get("done", 0), 3)
            self.assertEqual(counts.get("not_fetched", 0), 2)

    def test_max_total_documents(self) -> None:
        """Verify max_total_documents stops the crawl and sets state to completed."""
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [
                LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("3"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("4"), kind=EDGE_LINKS_TO),
            ],
        )
        p2 = make_doc(confluence_uid("2"), "P2", [])
        p3 = make_doc(confluence_uid("3"), "P3", [])
        p4 = make_doc(confluence_uid("4"), "P4", [])

        conf_client = FakeConfluenceClient({"1": p1, "2": p2, "3": p3, "4": p4})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({
            "start_page": "1",
            "follow_confluence_links": True,
            "max_total_documents": 2,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 2)
        self.assertIn("Budget erschöpft", st["last_message"])

    def test_budget_exceeded_requests(self) -> None:
        """Verify RequestBudget exhaustion terminates the crawl cleanly with completed."""
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [
                LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("3"), kind=EDGE_LINKS_TO),
            ],
        )
        p2 = make_doc(confluence_uid("2"), "P2", [])
        p3 = make_doc(confluence_uid("3"), "P3", [])

        budget_holder = []

        def factory(cfg, params, stop_evt, budget, on_evt):
            budget_holder.append(budget)
            client = FakeConfluenceClient(
                {"1": p1, "2": p2, "3": p3},
                budget=budget,
            )
            return client, None

        mgr = JobManager(self.config_store, self.db_path, client_factory=factory)

        res = mgr.start({
            "start_page": "1",
            "follow_confluence_links": True,
            "max_api_requests": 2,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertIn("Budget erschöpft", st["last_message"])
        self.assertEqual(st["pages_indexed"], 2)

    def test_max_links_per_document(self) -> None:
        """Verify max_links_per_document only enqueues the first N links."""
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [
                LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("3"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("4"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("5"), kind=EDGE_LINKS_TO),
            ],
        )
        p2 = make_doc(confluence_uid("2"), "P2")
        p3 = make_doc(confluence_uid("3"), "P3")
        p4 = make_doc(confluence_uid("4"), "P4")
        p5 = make_doc(confluence_uid("5"), "P5")

        conf_client = FakeConfluenceClient({"1": p1, "2": p2, "3": p3, "4": p4, "5": p5})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({
            "start_page": "1",
            "follow_confluence_links": True,
            "max_links_per_document": 2,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        # Only pages 1, 2, 3 should have been fetched
        self.assertEqual(conf_client.fetch_calls, ["1", "2", "3"])

        # All 4 edges should still be saved in the database
        with db.connect(self.db_path) as conn:
            links = db.get_outgoing_links(conn, confluence_uid("1"))
            self.assertEqual(len(links), 4)

    def test_refresh_existing_false_reuses_edges(self) -> None:
        """Verify refresh_existing=False reuses existing edges without API calls."""
        # Pre-seed DB with document 1 linking to document 2
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO)],
        )
        p2 = make_doc(confluence_uid("2"), "P2", [])

        conf_client = FakeConfluenceClient({"1": p1, "2": p2})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        with db.connect(self.db_path) as conn:
            db.save_document(conn, p1, [Chunk(heading="", body=p1.body_text)])
            db.save_document(conn, p2, [Chunk(heading="", body=p2.body_text)])

        res = mgr.start({
            "start_page": "1",
            "follow_confluence_links": True,
            "refresh_existing": False,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        # No fetch calls made because both documents were already indexed
        self.assertEqual(conf_client.fetch_calls, [])
        self.assertEqual(st["pages_indexed"], 0)
        with db.connect(self.db_path) as conn:
            q = conn.execute("SELECT document_uid, state FROM crawl_queue WHERE job_id=?", (res["job_id"],)).fetchall()
            self.assertEqual({r[0] for r in q}, {confluence_uid("1"), confluence_uid("2")})
            self.assertTrue(all(r[1] == "done" for r in q))

    def test_missing_forbidden_and_error_states(self) -> None:
        """Verify error handling transitions to missing, forbidden, and error states."""
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [
                LinkTarget(uid=confluence_uid("404"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("403"), kind=EDGE_LINKS_TO),
                LinkTarget(uid=confluence_uid("500"), kind=EDGE_LINKS_TO),
            ],
        )

        pages: dict[str, FetchedDocument | Exception] = {
            "1": p1,
            "404": NotFoundError("Page not found 404"),
            "403": ForbiddenError("Access forbidden 403"),
            "500": InvalidResponseError("Server error 500"),
        }

        conf_client = FakeConfluenceClient(pages)
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"start_page": "1", "follow_confluence_links": True})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 1)
        self.assertEqual(st["errors"], 1)  # InvalidResponseError increments errors

        with db.connect(self.db_path) as conn:
            d404 = db.get_document(conn, confluence_uid("404"))
            self.assertIsNotNone(d404)
            self.assertEqual(d404["state"], STATE_MISSING)

            d403 = db.get_document(conn, confluence_uid("403"))
            self.assertIsNotNone(d403)
            self.assertEqual(d403["state"], STATE_FORBIDDEN)

            d500 = db.get_document(conn, confluence_uid("500"))
            self.assertIsNotNone(d500)
            self.assertEqual(d500["state"], STATE_ERROR)

            counts = db.queue_counts(conn, res["job_id"])
            self.assertEqual(counts.get("missing"), 1)
            self.assertEqual(counts.get("forbidden"), 1)
            self.assertEqual(counts.get("error"), 1)

    def test_stop_during_running_job(self) -> None:
        """Verify calling stop() terminates the worker cleanly into stopped state."""
        fetch_started = threading.Event()
        can_proceed = threading.Event()

        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO)],
        )
        p2 = make_doc(confluence_uid("2"), "P2", [])

        def on_fetch(page_id: str) -> None:
            if page_id == "1":
                fetch_started.set()
                can_proceed.wait(timeout=5.0)

        conf_client = FakeConfluenceClient({"1": p1, "2": p2}, on_fetch=on_fetch)
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"start_page": "1", "follow_confluence_links": True})
        job_id = res["job_id"]

        self.assertTrue(fetch_started.wait(timeout=2.0))
        self.assertTrue(mgr.is_running())

        # Call stop()
        stop_res = mgr.stop(job_id)
        self.assertIn(stop_res["state"], (JOB_STOPPING, JOB_STOPPED))

        # Release worker
        can_proceed.set()

        st = wait_job_done(mgr, job_id)
        self.assertEqual(st["state"], JOB_STOPPED)
        self.assertFalse(mgr.is_running())

    def test_second_start_raises_job_conflict(self) -> None:
        """Verify starting a second job while one is running raises JobConflict."""
        block_worker = threading.Event()
        p1 = make_doc(confluence_uid("1"), "P1")

        def on_fetch(page_id: str) -> None:
            block_worker.wait(timeout=5.0)

        conf_client = FakeConfluenceClient({"1": p1}, on_fetch=on_fetch)
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"start_page": "1"})
        self.assertEqual(res["state"], JOB_RUNNING)

        # Attempt second start
        with self.assertRaises(JobConflict):
            mgr.start({"start_page": "1"})

        block_worker.set()
        wait_job_done(mgr, res["job_id"])

    def test_unknown_field_raises_config_error(self) -> None:
        """Verify invalid payload parameters raise ConfigError immediately."""
        mgr = JobManager(self.config_store, self.db_path)
        with self.assertRaises(ConfigError):
            mgr.start({"start_page": "1", "invalid_unexpected_field": 123})

    def test_unresolved_titles_resolved_in_one_batch_and_cached(self) -> None:
        """Multiple /display/SPACE/Title links cost one request; results are cached."""
        links = [
            LinkTarget(uid=unresolved_page_uid("ENG", "Titel 2"), kind=EDGE_LINKS_TO),
            LinkTarget(uid=unresolved_page_uid("ENG", "Titel 3"), kind=EDGE_LINKS_TO),
        ]
        p1 = make_doc(confluence_uid("1"), "P1", links, space_or_project="ENG")
        p2 = make_doc(confluence_uid("2"), "Titel 2", [], space_or_project="ENG")
        p3 = make_doc(confluence_uid("3"), "Titel 3", [], space_or_project="ENG")
        conf_client = FakeBulkTitleConfluenceClient(
            {"1": p1, "2": p2, "3": p3},
            title_resolutions={("ENG", "Titel 2"): "2", ("ENG", "Titel 3"): "3"},
        )
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        st = wait_job_done(mgr, mgr.start({"start_page": "1"})["job_id"])
        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 3)
        self.assertEqual(len(conf_client.bulk_resolve_calls), 1)   # ein Sammel-Request
        self.assertEqual(conf_client.bulk_resolve_calls[0],
                         [("ENG", "Titel 2"), ("ENG", "Titel 3")])
        self.assertEqual(conf_client.resolve_calls, [])           # kein Einzelabruf

        # Zweiter Lauf: Auflösung kommt aus dem persistenten Cache (keine Requests)
        st2 = wait_job_done(mgr, mgr.start({"start_page": "1"})["job_id"])
        self.assertEqual(st2["state"], JOB_COMPLETED)
        self.assertEqual(len(conf_client.bulk_resolve_calls), 1)
        with db.connect(self.db_path) as conn:
            known, page_id = db.get_title_resolution(conn, "ENG", "Titel 2")
            self.assertTrue(known)
            self.assertEqual(page_id, "2")

    def test_mark_interrupted_on_init(self) -> None:
        """Verify active jobs from previous process crashes are marked interrupted on init."""
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            job_id = db.create_job(conn, {"start_page": "1"}, confluence_uid("1"))
            db.update_job(conn, job_id, state=JOB_RUNNING)

        # Re-initialize JobManager
        mgr = JobManager(self.config_store, self.db_path)
        st = mgr.status(job_id)
        self.assertIsNotNone(st)
        self.assertEqual(st["state"], JOB_INTERRUPTED)

    def test_unresolved_title_resolution(self) -> None:
        """Verify unresolved page titles are resolved via resolve_title when enabled."""
        unresolved_target = unresolved_page_uid("ENG", "Architecture")
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [LinkTarget(uid=unresolved_target, kind=EDGE_LINKS_TO)],
        )
        p_arch = make_doc(confluence_uid("999"), "Architecture Page")

        conf_client = FakeConfluenceClient(
            {"1": p1, "999": p_arch},
            title_resolutions={("ENG", "Architecture"): "999"},
        )

        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        # With resolve_page_titles=True
        res = mgr.start({
            "start_page": "1",
            "follow_confluence_links": True,
            "resolve_page_titles": True,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 2)
        self.assertIn("999", conf_client.fetch_calls)

        # Verify resolved edge in DB
        with db.connect(self.db_path) as conn:
            links = db.get_outgoing_links(conn, confluence_uid("1"))
            uids = [l.uid for l in links]
            self.assertIn(confluence_uid("999"), uids)

    def test_start_page_unresolved_display_url(self) -> None:
        """Verify start_page as /display/SPACE/Title URL is resolved before crawling."""
        p_home = make_doc(confluence_uid("100"), "Overview")
        conf_client = FakeConfluenceClient(
            {"100": p_home},
            title_resolutions={("ENG", "Overview"): "100"},
        )

        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({
            "start_page": "https://wiki.example.com/display/ENG/Overview",
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 1)
        self.assertEqual(conf_client.fetch_calls, ["100"])

        # Test failure when start page title cannot be resolved
        conf_client2 = FakeConfluenceClient({}, title_resolutions={})
        mgr2 = JobManager(
            self.config_store,
            self.tmp_path / "test2.db",
            client_factory=lambda *args: (conf_client2, None),
        )

        res2 = mgr2.start({
            "start_page": "https://wiki.example.com/display/ENG/Unknown",
        })
        st2 = wait_job_done(mgr2, res2["job_id"])
        self.assertEqual(st2["state"], JOB_FAILED)
        self.assertIn("Startseite konnte nicht aufgelöst werden", st2["last_message"])

    def test_moved_jira_issue_key(self) -> None:
        """Verify redirect edge and skipped state when a Jira issue key has moved."""
        p1 = make_doc(
            confluence_uid("1"),
            "P1",
            [LinkTarget(uid=jira_uid("OLD-1"), kind=EDGE_MENTIONS_JIRA)],
        )
        # Server returns NEW-2 when OLD-1 was requested
        j_moved = make_doc(jira_uid("NEW-2"), "Moved Issue")

        conf_client = FakeConfluenceClient({"1": p1})
        jira_client = FakeJiraClient({"OLD-1": j_moved})

        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, jira_client),
        )

        res = mgr.start({
            "start_page": "1",
            "index_linked_jira_issues": True,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["jira_indexed"], 1)

        with db.connect(self.db_path) as conn:
            # NEW-2 should be saved as indexed
            doc_new = db.get_document(conn, jira_uid("NEW-2"))
            self.assertIsNotNone(doc_new)
            self.assertEqual(doc_new["state"], STATE_INDEXED)

            # OLD-1 should be marked skipped
            doc_old = db.get_document(conn, jira_uid("OLD-1"))
            self.assertIsNotNone(doc_old)
            self.assertEqual(doc_old["state"], STATE_SKIPPED)

            # Edge OLD-1 -> NEW-2
            links = db.get_outgoing_links(conn, jira_uid("OLD-1"))
            self.assertTrue(any(l.uid == jira_uid("NEW-2") and l.relation_label == "moved to" for l in links))


    def test_refresh_only_empty_db_raises_config_error(self) -> None:
        """Verify refresh_only=True on an empty database raises ConfigError."""
        mgr = JobManager(self.config_store, self.db_path)
        with self.assertRaises(ConfigError) as ctx:
            mgr.start({"refresh_only": True})
        self.assertIn("Keine bestehenden Dokumente", str(ctx.exception))

    def test_refresh_only_updates_existing_and_does_not_enqueue_new_links(self) -> None:
        """Verify refresh_only=True updates existing documents without discovering new ones."""
        # 1. Seed DB with existing Confluence page and Jira ticket
        p1_initial = make_doc(
            confluence_uid("1"),
            "P1 Initial Title",
            [],
        )
        j1_initial = make_doc(
            jira_uid("PROJ-1"),
            "J1 Initial Summary",
            [],
        )
        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            db.save_document(conn, p1_initial, [Chunk(heading="Title", body="Initial body 1")])
            db.save_document(conn, j1_initial, [Chunk(heading="Summary", body="Initial summary PROJ-1")])

        # 2. Setup clients returning updated documents that contain links to new docs
        p1_updated = FetchedDocument(
            uid=confluence_uid("1"),
            source_type="confluence",
            source_id="1",
            title="P1 Updated Title",
            url="https://confluence.example.com/pages/1",
            body_text="Updated body with new link to page 2",
            space_or_project="TEST",
            links=[LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO)],
            labels=[],
        )
        p2_new = make_doc(confluence_uid("2"), "P2 New Page", [])

        j1_updated = FetchedDocument(
            uid=jira_uid("PROJ-1"),
            source_type="jira",
            source_id="PROJ-1",
            title="PROJ-1 Updated Summary",
            url="https://jira.example.com/browse/PROJ-1",
            body_text="Updated issue linking to PROJ-2",
            space_or_project="PROJ",
            links=[LinkTarget(uid=jira_uid("PROJ-2"), kind=EDGE_JIRA_RELATES_TO)],
            labels=[],
        )
        j2_new = make_doc(jira_uid("PROJ-2"), "PROJ-2 New Issue", [])

        conf_client = FakeConfluenceClient({"1": p1_updated, "2": p2_new})
        jira_client = FakeJiraClient({"PROJ-1": j1_updated, "PROJ-2": j2_new})

        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, jira_client),
        )

        res = mgr.start({"refresh_only": True})
        st = wait_job_done(mgr, res["job_id"])
        self.assertEqual(st["start_page_uid"], "refresh:existing")

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 1)
        self.assertEqual(st["jira_indexed"], 1)

        # Ensure ONLY page 1 and PROJ-1 were fetched; page 2 and PROJ-2 were NOT fetched
        self.assertEqual(conf_client.fetch_calls, ["1"])
        self.assertEqual(jira_client.fetch_calls, ["PROJ-1"])

        with db.connect(self.db_path) as conn:
            # Check updated contents
            doc1 = db.get_document(conn, confluence_uid("1"))
            self.assertIsNotNone(doc1)
            self.assertEqual(doc1["title"], "P1 Updated Title")

            doc_j1 = db.get_document(conn, jira_uid("PROJ-1"))
            self.assertIsNotNone(doc_j1)
            self.assertEqual(doc_j1["title"], "PROJ-1 Updated Summary")

            # Check that p2 was not created as indexed
            doc2 = db.get_document(conn, confluence_uid("2"))
            self.assertTrue(doc2 is None or doc2["state"] != STATE_INDEXED)

    def test_space_crawl_discovers_all_pages_including_orphans(self) -> None:
        """Verify space_key crawl indexes all pages in the space including unlinked/orphan pages."""
        p1 = make_doc(
            confluence_uid("1"),
            "Linked Page 1",
            [LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO)],
            space_or_project="MYSPACE",
        )
        p2 = make_doc(confluence_uid("2"), "Linked Page 2", [], space_or_project="MYSPACE")
        # Page 3 is an isolated orphan page with no links to/from p1 or p2
        p3 = make_doc(confluence_uid("3"), "Isolated Orphan Page", [], space_or_project="MYSPACE")

        conf_client = FakeConfluenceClient({"1": p1, "2": p2, "3": p3})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"space_key": "MYSPACE"})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 3)
        self.assertEqual(set(conf_client.fetch_calls), {"1", "2", "3"})

        with db.connect(self.db_path) as conn:
            d3 = db.get_document(conn, confluence_uid("3"))
            self.assertIsNotNone(d3)
            self.assertEqual(d3["state"], STATE_INDEXED)
            self.assertEqual(d3["title"], "Isolated Orphan Page")

    def test_space_crawl_via_space_url(self) -> None:
        """Verify start_page with space URL triggers space crawl."""
        p1 = make_doc(confluence_uid("10"), "Root", [], space_or_project="KAFKA")
        p2 = make_doc(confluence_uid("20"), "Orphan", [], space_or_project="KAFKA")

        conf_client = FakeConfluenceClient({"10": p1, "20": p2})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"start_page": "https://wiki.example.com/display/KAFKA"})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 2)
        self.assertEqual(set(conf_client.fetch_calls), {"10", "20"})

    def test_page_crawl_still_works_and_ignores_orphans(self) -> None:
        """Verify legacy page crawl starting from page ID only follows links and does NOT crawl orphans."""
        p1 = make_doc(
            confluence_uid("1"),
            "Page 1",
            [LinkTarget(uid=confluence_uid("2"), kind=EDGE_LINKS_TO)],
            space_or_project="TEAM",
        )
        p2 = make_doc(confluence_uid("2"), "Page 2", [], space_or_project="TEAM")
        p3 = make_doc(confluence_uid("3"), "Orphan Page 3", [], space_or_project="TEAM")

        conf_client = FakeConfluenceClient({"1": p1, "2": p2, "3": p3})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"start_page": "1", "follow_confluence_links": True})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 2)
        self.assertEqual(set(conf_client.fetch_calls), {"1", "2"})

    def test_multi_space_crawl_indexes_all_selected_spaces(self) -> None:
        """space_keys starts one crawl over every selected space (incl. orphans)."""
        p1 = make_doc(confluence_uid("1"), "A1", [], space_or_project="ALPHA")
        p2 = make_doc(confluence_uid("2"), "B1", [], space_or_project="BETA")
        p3 = make_doc(confluence_uid("3"), "B2", [], space_or_project="BETA")

        conf_client = FakeConfluenceClient(
            {"1": p1, "2": p2, "3": p3},
            space_map={"ALPHA": [("1", "A1")], "BETA": [("2", "B1"), ("3", "B2")]},
        )
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"space_keys": ["ALPHA", "BETA"]})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 3)
        self.assertEqual(set(conf_client.fetch_calls), {"1", "2", "3"})

        with db.connect(self.db_path) as conn:
            job = db.get_job(conn, res["job_id"])
            self.assertEqual(job["start_page_uid"], "space:ALPHA,BETA")
            d3 = db.get_document(conn, confluence_uid("3"))
            self.assertEqual(d3["space_or_project"], "BETA")

    def test_space_keys_preserve_case_for_personal_spaces(self) -> None:
        """Personal space keys are case-sensitive and must not be uppercased."""
        p1 = make_doc(confluence_uid("9"), "Persoenliche Seite", [], space_or_project="~jsmith")
        conf_client = FakeConfluenceClient(
            {"9": p1}, space_map={"~jsmith": [("9", "Persoenliche Seite")]},
        )
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"space_keys": ["~jsmith"]})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(conf_client.space_calls, ["~jsmith"])
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.get_job(conn, res["job_id"])["start_page_uid"], "space:~jsmith")

    def test_single_space_crawl_still_works(self) -> None:
        """The legacy single space_key path keeps working unchanged."""
        p1 = make_doc(confluence_uid("1"), "A1", [], space_or_project="MYSPACE")
        conf_client = FakeConfluenceClient({"1": p1})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        st = wait_job_done(mgr, mgr.start({"space_key": "MYSPACE"})["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(conf_client.space_calls, ["MYSPACE"])

    def test_modified_since_job_refreshes_changed_pages(self) -> None:
        """modified_since refetches already indexed pages that changed since a date."""
        p1 = make_doc(confluence_uid("1"), "Alter Titel", [], space_or_project="ENG")
        conf_client = FakeConfluenceClient({"1": p1})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        first_job = mgr.start({"start_page": "1"})
        self.assertEqual(wait_job_done(mgr, first_job["job_id"])["state"], JOB_COMPLETED)
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.get_document(conn, confluence_uid("1"))["title"], "Alter Titel")

        conf_client.pages["1"] = make_doc(
            confluence_uid("1"), "Neuer Titel", [], space_or_project="ENG"
        )
        conf_client.modified_pages = [{
            "id": "1", "title": "Neuer Titel", "space": "ENG",
            "updated_at": "2026-10-05T10:00:00.000Z",
        }]

        res = mgr.start({"modified_since": "2026-10-04", "space_keys": ["ENG"]})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(conf_client.modified_calls, [("2026-10-04", ("ENG",))])
        self.assertEqual(conf_client.fetch_calls.count("1"), 2)  # fetched again
        with db.connect(self.db_path) as conn:
            job = db.get_job(conn, res["job_id"])
            self.assertEqual(job["start_page_uid"], "recent:2026-10-04")
            self.assertEqual(db.get_document(conn, confluence_uid("1"))["title"], "Neuer Titel")

    def test_modified_since_without_results_completes(self) -> None:
        """An update run without changed pages completes with a hint, not a failure."""
        conf_client = FakeConfluenceClient({}, modified_pages=[])
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        st = wait_job_done(mgr, mgr.start({"modified_since": "2026-10-04"})["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertIn("Keine Seiten", st["last_message"])
        self.assertEqual(conf_client.modified_calls, [("2026-10-04", ())])

    def test_relative_modified_since_is_normalized(self) -> None:
        """Relative values like 7d/2w are converted to an ISO date."""
        conf_client = FakeConfluenceClient({}, modified_pages=[])
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"modified_since": "7d"})
        st = wait_job_done(mgr, res["job_id"])

        since = conf_client.modified_calls[0][0]
        self.assertRegex(since, r"^\d{4}-\d{2}-\d{2}$")
        expected = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        self.assertEqual(since, expected)
        with db.connect(self.db_path) as conn:
            self.assertEqual(
                db.get_job(conn, res["job_id"])["start_page_uid"], f"recent:{expected}"
            )

    def test_invalid_modified_since_raises_config_error(self) -> None:
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (FakeConfluenceClient({}), None),
        )
        with self.assertRaises(ConfigError):
            mgr.start({"modified_since": "irgendwann"})

    def test_invalid_space_key_in_selection_raises_config_error(self) -> None:
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (FakeConfluenceClient({}), None),
        )
        with self.assertRaises(ConfigError):
            mgr.start({"space_keys": ["ok", "böse"]})

    def test_batch_prefetch_fetches_many_pages_with_one_request(self) -> None:
        """A space crawl of 25 pages must need a single bulk request."""
        page_ids = [str(i) for i in range(1, 26)]
        pages = {
            pid: make_doc(confluence_uid(pid), f"Seite {pid}", [], space_or_project="BIG")
            for pid in page_ids
        }
        conf_client = FakeBulkConfluenceClient(
            pages, space_map={"BIG": [(pid, f"Seite {pid}") for pid in page_ids]},
        )
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"space_keys": ["BIG"], "batch_fetch_size": 100})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 25)
        self.assertEqual(len(conf_client.bulk_calls), 1)
        self.assertEqual(len(conf_client.bulk_calls[0]), 25)
        self.assertEqual(conf_client.fetch_calls, [])  # kein Einzelabruf

    def test_batch_prefetch_respects_batch_size(self) -> None:
        """batch_fetch_size chunks the lookahead into multiple bulk requests."""
        page_ids = ["1", "2", "3"]
        pages = {
            pid: make_doc(confluence_uid(pid), f"Seite {pid}", [], space_or_project="SMALL")
            for pid in page_ids
        }
        conf_client = FakeBulkConfluenceClient(
            pages, space_map={"SMALL": [(pid, f"Seite {pid}") for pid in page_ids]},
        )
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"space_keys": ["SMALL"], "batch_fetch_size": 2})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 3)
        self.assertEqual([len(call) for call in conf_client.bulk_calls], [2, 1])
        self.assertEqual(conf_client.fetch_calls, [])

    def test_batch_prefetch_includes_jira_issues(self) -> None:
        """Bulk prefetch must cover Jira too (regression: jira_uid import)."""
        p1 = make_doc(
            confluence_uid("1"), "P1",
            [LinkTarget(uid=jira_uid("PROJ-1"), kind=EDGE_MENTIONS_JIRA)],
            space_or_project="ENG",
        )
        j1 = make_doc(jira_uid("PROJ-1"), "J1", [], space_or_project="PROJ")
        conf_client = FakeBulkConfluenceClient(
            {"1": p1}, space_map={"ENG": [("1", "P1")]},
        )
        jira_client = FakeBulkJiraClient({"PROJ-1": j1})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, jira_client),
        )

        res = mgr.start({
            "space_keys": ["ENG"], "batch_fetch_size": 100, "include_child_pages": False,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 1)
        self.assertEqual(st["jira_indexed"], 1)
        self.assertEqual(conf_client.bulk_calls, [["1"]])
        self.assertEqual(jira_client.bulk_calls, [["PROJ-1"]])
        self.assertEqual(conf_client.fetch_calls, [])   # kein Einzelabruf
        self.assertEqual(jira_client.fetch_calls, [])   # kein Einzelabruf

    def test_space_crawl_derives_child_edges_without_extra_requests(self) -> None:
        """Parent/child edges come from the listing (expand=ancestors), not per page."""
        pages = {
            "1": make_doc(confluence_uid("1"), "Root", [], space_or_project="TREE"),
            "2": make_doc(confluence_uid("2"), "Kind 1", [], space_or_project="TREE"),
            "3": make_doc(confluence_uid("3"), "Kind 2", [], space_or_project="TREE"),
        }
        conf_client = FakeBulkConfluenceClient(
            pages, space_parents={"1": "", "2": "1", "3": "1"},
        )
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"space_keys": ["TREE"], "batch_fetch_size": 100})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 3)
        self.assertEqual(conf_client.bulk_include_children, [False])  # kein Kind-Request
        self.assertEqual(conf_client.child_calls, [])                 # keine Extra-Requests
        with db.connect(self.db_path) as conn:
            children = {lk.uid for lk in db.get_outgoing_links(conn, confluence_uid("1"))}
            self.assertEqual(children, {confluence_uid("2"), confluence_uid("3")})

    def test_prefetch_continues_after_moved_key_alias(self) -> None:
        """A prefetched uid that became 'seen' (moved key) must not stop batching."""
        jira_links = [
            LinkTarget(uid=jira_uid("OLD-1"), kind=EDGE_MENTIONS_JIRA),
            LinkTarget(uid=jira_uid("NEW-1"), kind=EDGE_MENTIONS_JIRA),
            LinkTarget(uid=jira_uid("PROJ-2"), kind=EDGE_MENTIONS_JIRA),
            LinkTarget(uid=jira_uid("PROJ-3"), kind=EDGE_MENTIONS_JIRA),
        ]
        p1 = make_doc(confluence_uid("1"), "P1", jira_links, space_or_project="ENG")
        moved = make_doc(jira_uid("NEW-1"), "Umgezogen", [], space_or_project="PROJ")
        jira_client = FakeBulkJiraClient({
            "OLD-1": moved, "NEW-1": moved,
            "PROJ-2": make_doc(jira_uid("PROJ-2"), "J2", [], space_or_project="PROJ"),
            "PROJ-3": make_doc(jira_uid("PROJ-3"), "J3", [], space_or_project="PROJ"),
        })
        conf_client = FakeBulkConfluenceClient(
            {"1": p1}, space_map={"ENG": [("1", "P1")]},
        )
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, jira_client),
        )

        res = mgr.start({
            "space_keys": ["ENG"], "batch_fetch_size": 2, "include_child_pages": False,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        # Second batch must happen even though NEW-1 stayed in the prefetch cache
        self.assertEqual(
            jira_client.bulk_calls,
            [["OLD-1", "NEW-1"], ["PROJ-2", "PROJ-3"]],
        )
        self.assertEqual(jira_client.fetch_calls, [])  # kein Einzelabruf

    def test_budget_exhaustion_still_indexes_prefetched_documents(self) -> None:
        """Documents already fetched by the batch are stored even if the budget ran out."""
        page_ids = ["1", "2", "3"]
        pages = {
            pid: make_doc(confluence_uid(pid), f"Seite {pid}", [], space_or_project="SMALL")
            for pid in page_ids
        }

        def factory(cfg, params, stop_event, budget, on_event):
            return FakeBulkConfluenceClient(
                pages,
                space_map={"SMALL": [(pid, f"Seite {pid}") for pid in page_ids]},
                budget=budget,
            ), None

        mgr = JobManager(self.config_store, self.db_path, client_factory=factory)
        res = mgr.start({
            "space_keys": ["SMALL"], "batch_fetch_size": 100,
            "include_child_pages": False, "max_api_requests": 2,
        })
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 3)      # trotz Budget 2 vorab geladen
        self.assertEqual(st["skipped"], 0)

    def test_budget_drain_does_not_start_new_batches(self) -> None:
        """With an exhausted budget the drain must not trigger another bulk request."""
        page_ids = [str(i) for i in range(1, 9)]
        pages = {
            pid: make_doc(confluence_uid(pid), f"Seite {pid}", [], space_or_project="BIG")
            for pid in page_ids
        }

        def factory(cfg, params, stop_event, budget, on_event):
            return FakeBulkConfluenceClient(
                pages,
                space_map={"BIG": [(pid, f"Seite {pid}") for pid in page_ids]},
                budget=budget,
            ), None

        mgr = JobManager(self.config_store, self.db_path, client_factory=factory)
        res = mgr.start({
            "space_keys": ["BIG"], "batch_fetch_size": 8,
            "include_child_pages": False, "max_api_requests": 2,
        })
        st = wait_job_done(mgr, res["job_id"])

        # 1 Space-Liste + 1 Bulk-Request = Budget 2; alle 8 vorab geladenen Seiten
        # müssen trotzdem gespeichert werden (kein Nachladen, kein Verwerfen).
        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertEqual(st["pages_indexed"], 8)
        self.assertEqual(st["skipped"], 0)

    def test_zero_page_budget_completes_instead_of_failing(self) -> None:
        """max_confluence_pages=0 must not look like an empty (broken) space."""
        conf_client = FakeConfluenceClient({"1": make_doc(confluence_uid("1"), "P1")})
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"space_key": "MYSPACE", "max_confluence_pages": 0})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertIn("max_confluence_pages ist 0", st["last_message"])
        self.assertEqual(conf_client.space_calls, [])  # kein Request

    def test_stop_during_recent_discovery_stops_job(self) -> None:
        """Stopping the job while the CQL search runs must end as 'stopped'."""
        conf_client = FakeConfluenceClient({}, modified_pages=[])
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        def on_list(since, space_keys=None, max_items=10_000):
            raise CrawlStopped()

        conf_client.list_pages_modified_since = on_list  # type: ignore[assignment]
        res = mgr.start({"modified_since": "2026-10-04"})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_STOPPED)
        self.assertEqual(st["last_message"], "Gestoppt")

    def test_stop_during_start_page_title_resolution_stops_job(self) -> None:
        """Stop during resolve_title() of the start page must not end as 'failed'."""
        conf_client = FakeConfluenceClient({}, title_resolutions={})

        def on_resolve(space, title):
            raise CrawlStopped()

        conf_client.resolve_title = on_resolve  # type: ignore[assignment]
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )

        res = mgr.start({"start_page": "https://wiki.example.com/display/ENG/Titel"})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_STOPPED)
        self.assertEqual(st["last_message"], "Gestoppt")

    def test_budget_exceeded_during_space_discovery_completes(self) -> None:
        """Budget exhaustion during the space listing ends 'completed', not 'failed'."""
        conf_client = FakeConfluenceClient({})

        def on_list(space_key, max_items=100_000):
            raise BudgetExceeded("Request-Budget erschöpft (1/1)")

        conf_client.list_space_pages = on_list  # type: ignore[assignment]
        mgr = JobManager(
            self.config_store,
            self.db_path,
            client_factory=lambda *args: (conf_client, None),
        )
        res = mgr.start({"space_key": "MYSPACE"})
        st = wait_job_done(mgr, res["job_id"])

        self.assertEqual(st["state"], JOB_COMPLETED)
        self.assertIn("Budget erschöpft", st["last_message"])


if __name__ == "__main__":
    unittest.main()
