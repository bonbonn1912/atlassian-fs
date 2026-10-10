"""Unit tests for JobManager and BFS crawler in knowledge/indexer.py.

All tests are hermetic and run in-memory without network calls.
"""

from __future__ import annotations

import collections
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
    ) -> None:
        self.pages = pages
        self.title_resolutions = title_resolutions or {}
        self.budget = budget
        self.on_fetch = on_fetch
        self.fetch_calls: list[str] = []
        self.resolve_calls: list[tuple[str, str]] = []

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
) -> FetchedDocument:
    source_type, source_id = uid.split(":", 1)
    return FetchedDocument(
        uid=uid,
        source_type=source_type,
        source_id=source_id,
        title=title,
        url=f"https://wiki.example.com/item/{source_id}",
        body_text=body,
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


if __name__ == "__main__":
    unittest.main()
