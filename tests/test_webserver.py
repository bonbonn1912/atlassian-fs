"""Unit tests for knowledge/webserver.py and app.py HTTP API."""
from __future__ import annotations

import http.client
import gzip
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from knowledge import db
from knowledge.config import ConfigStore
from knowledge.indexer import JobConflict
from knowledge.models import (
    Chunk,
    EDGE_LINKS_TO_CONFLUENCE,
    FetchedDocument,
    LinkTarget,
)
from knowledge.webserver import close_thread_connection, create_server


class FakeJobManager:
    """Mock JobManager for isolated webserver unit tests."""

    def __init__(self) -> None:
        self._running = False
        self._current_job: dict | None = None
        self._jobs: dict[int, dict] = {}
        self._next_id = 1

    def is_running(self) -> bool:
        return self._running

    def start(self, payload: dict) -> dict:
        if self._running:
            raise JobConflict("Ein Job läuft bereits")
        if not payload.get("start_page") and not payload.get("refresh_only"):
            raise ValueError("start_page ist erforderlich")
        job_id = self._next_id
        self._next_id += 1
        self._running = True
        job_info = {
            "job_id": job_id,
            "state": "running",
            "phase": "crawling",
            "progress": {"pages_indexed": 0},
        }
        self._current_job = job_info
        self._jobs[job_id] = job_info
        return {"job_id": job_id, "state": "running"}

    def stop(self, job_id: int) -> dict:
        if job_id not in self._jobs:
            raise KeyError(f"Job {job_id} nicht gefunden")
        job = self._jobs[job_id]
        job["state"] = "stopped"
        self._running = False
        return job

    def status(self, job_id: int | None = None) -> dict | None:
        if job_id is None:
            return self._current_job
        return self._jobs.get(job_id)

    def shutdown(self) -> None:
        self._running = False


class TestWebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.test_dir = tempfile.mkdtemp(prefix="test_webserver_")
        cls.dir_path = Path(cls.test_dir)

        # Create temporary web directory with static files
        cls.web_dir = cls.dir_path / "web"
        cls.web_dir.mkdir(parents=True)
        (cls.web_dir / "index.html").write_text("<!DOCTYPE html><html><body>Test</body></html>", encoding="utf-8")
        (cls.web_dir / "style.css").write_text("body { color: blue; }", encoding="utf-8")
        (cls.web_dir / "app.js").write_text("console.log('app');", encoding="utf-8")
        for filename in ('graph-flight.js', 'graph-flight-layout.js'):
            (cls.web_dir / filename).write_text("console.log('flight');", encoding="utf-8")
        (cls.web_dir / "graph-render-worker.js").write_text("console.log('worker');", encoding="utf-8")
        (cls.web_dir / "graph.js").write_text("console.log('graph');", encoding="utf-8")

        # Database
        cls.db_path = cls.dir_path / "knowledge.sqlite"
        conn = db.open_db(cls.db_path)

        # Seed database with sample documents for search and graph tests
        doc_jira = FetchedDocument(
            uid="jira:ENG-1",
            source_type="jira",
            source_id="ENG-1",
            title="Implement feature X",
            url="https://jira.example.intern/browse/ENG-1",
            body_text="We need to implement feature X for the client.",
            space_or_project="ENG",
            links=[LinkTarget(uid="confluence:12345", kind=EDGE_LINKS_TO_CONFLUENCE)],
            labels=["core", "backend"],
        )
        chunk_jira = Chunk(
            heading="Description",
            body="We need to implement feature X for the client.",
        )
        db.save_document(conn, doc_jira, [chunk_jira])

        doc_conf = FetchedDocument(
            uid="confluence:12345",
            source_type="confluence",
            source_id="12345",
            title="Architecture Overview",
            url="https://confluence.example.intern/pages/12345",
            body_text="Architecture overview documentation describing feature X.",
            space_or_project="ARCH",
            links=[],
            labels=["docs"],
        )
        chunk_conf = Chunk(
            heading="Overview",
            body="Architecture overview documentation describing feature X.",
        )
        db.save_document(conn, doc_conf, [chunk_conf])

        conn.close()

        # ConfigStore
        cls.config_path = cls.dir_path / "config.json"
        cls.config_store = ConfigStore(cls.config_path)

        # Fake JobManager
        cls.fake_jm = FakeJobManager()

        # Create server on 127.0.0.1 port 0 (dynamic port)
        cls.server = create_server(
            cls.config_store,
            cls.fake_jm,
            "127.0.0.1",
            0,
            web_dir=cls.web_dir,
            db_path=cls.db_path,
        )
        cls.port = cls.server.server_address[1]

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2.0)
        close_thread_connection()
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self) -> None:
        # Reset FakeJobManager state between tests
        self.fake_jm._running = False
        self.fake_jm._current_job = None
        self.fake_jm._jobs.clear()
        self.fake_jm._next_id = 1

    def make_request(
        self,
        method: str,
        path: str,
        data: dict | str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        url = f"http://127.0.0.1:{self.port}{path}"
        req_headers = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            req_headers.update(headers)

        body_bytes = None
        if data is not None:
            if isinstance(data, dict):
                body_bytes = json.dumps(data).encode("utf-8")
                if "Content-Type" not in req_headers:
                    req_headers["Content-Type"] = "application/json"
            elif isinstance(data, str):
                body_bytes = data.encode("utf-8")
            else:
                body_bytes = data

        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, resp_headers, resp.read()
        except urllib.error.HTTPError as exc:
            resp_headers = {k.lower(): v for k, v in exc.headers.items()}
            body = exc.read()
            exc.close()
            return exc.code, resp_headers, body

    # 1. Static file serving and security headers
    def test_mcp_output_settings_via_api(self) -> None:
        original = self.config_store.get()['mcp']
        try:
            values = {'default_max_chars': 100000, 'max_output_chars': 250000}
            status, _, body = self.make_request('PUT', '/api/settings', {'mcp': values})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['mcp'], values)
            status, _, body = self.make_request('GET', '/api/settings')
            self.assertEqual(json.loads(body)['mcp'], values)
            self.assertEqual(ConfigStore(self.config_path).get()['mcp'], values)
            status, _, _ = self.make_request('PUT', '/api/settings', {'mcp': {'max_output_chars': 50000}})
            self.assertEqual(status, 400)
            self.assertEqual(self.config_store.get()['mcp'], values)
        finally:
            self.config_store.update({'mcp': original})

    def test_static_index_html_with_security_headers(self) -> None:
        status, headers, body = self.make_request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!DOCTYPE html>", body)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn("default-src 'self'", headers.get("content-security-policy", ""))
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(headers.get("x-frame-options"), "DENY")

    def test_static_css_and_js(self) -> None:
        status, headers, body = self.make_request("GET", "/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/css")
        self.assertIn(b"color: blue", body)

        status, headers, body = self.make_request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "application/javascript")
        self.assertIn(b"console.log", body)

    def test_graph_flight_assets_are_served(self) -> None:
        for filename in ('graph-flight.js', 'graph-flight-layout.js'):
            with self.subTest(filename=filename):
                status, headers, body = self.make_request('GET', '/' + filename)
                self.assertEqual(status, 200)
                self.assertEqual(headers['content-type'], 'application/javascript')
                self.assertIn(b'flight', body)

    def test_graph_render_worker_is_served(self) -> None:
        status, headers, body = self.make_request('GET', '/graph-render-worker.js')
        self.assertEqual(status, 200)
        self.assertEqual(headers['content-type'], 'application/javascript')
        self.assertIn(b'worker', body)

    def test_static_path_traversal_protection(self) -> None:
        # Test using http.client to prevent client-side path normalization
        conn = http.client.HTTPConnection("127.0.0.1", self.port)

        # /../config.json
        conn.request("GET", "/../config.json", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        resp.read()

        # /%2e%2e/config.json
        conn.request("GET", "/%2e%2e/config.json", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        resp.read()

        # /index.html/../style.css
        conn.request("GET", "/index.html/../style.css", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        resp.read()
        conn.close()

    # 2. /api/config
    def test_api_config_structure(self) -> None:
        status, headers, body = self.make_request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        data = json.loads(body.decode("utf-8"))
        self.assertIn("defaults", data)
        self.assertIn("limits", data)
        self.assertIn("search", data)
        self.assertIn("graph", data)
        self.assertIn("configured", data)
        self.assertIn("confluence", data["configured"])
        self.assertIn("jira", data["configured"])
        self.assertIn("confluence_sleep_seconds", data["defaults"])
        self.assertIn("jira_sleep_seconds", data["defaults"])

    # 3. /api/settings GET and PUT
    def test_api_settings_get_and_put(self) -> None:
        # GET settings
        status, _, body = self.make_request("GET", "/api/settings")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("confluence", data)
        self.assertIn("auth", data["confluence"])
        self.assertEqual(data["confluence"]["auth"]["token"], "")
        self.assertIn("token_set", data["confluence"]["auth"])

        # PUT valid patch
        patch = {"crawl_defaults": {"max_confluence_pages": 42}}
        status, _, body = self.make_request("PUT", "/api/settings", data=patch)
        self.assertEqual(status, 200)
        updated = json.loads(body.decode("utf-8"))
        self.assertEqual(updated["crawl_defaults"]["max_confluence_pages"], 42)

        # PUT invalid patch (negative pages) -> 400
        bad_patch = {"crawl_defaults": {"max_confluence_pages": -1}}
        status, _, body = self.make_request("PUT", "/api/settings", data=bad_patch)
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "bad_request")

    def test_api_settings_conflict_when_job_running(self) -> None:
        self.fake_jm._running = True
        patch = {"confluence": {"base_url": "https://newconfluence.intern"}}
        status, _, body = self.make_request("PUT", "/api/settings", data=patch)
        self.assertEqual(status, 409)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "job_conflict")
        self.assertIn("Während einer laufenden Indexierung", err["error"]["message"])

    def test_api_settings_selected_spaces_roundtrip(self) -> None:
        """Spaces chosen in the UI are stored verbatim (case-sensitive keys)."""
        patch = {"confluence": {"selected_spaces": ["ENG", "~jsmith"]}}
        status, _, body = self.make_request("PUT", "/api/settings", data=patch)
        self.assertEqual(status, 200)
        updated = json.loads(body.decode("utf-8"))
        self.assertEqual(updated["confluence"]["selected_spaces"], ["ENG", "~jsmith"])

        status, _, body = self.make_request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body.decode("utf-8"))["confluence"]["selected_spaces"],
            ["ENG", "~jsmith"],
        )

        self.make_request("PUT", "/api/settings", data={"confluence": {"selected_spaces": []}})

    def test_api_settings_rejects_invalid_job_defaults(self) -> None:
        """Values the job validation would reject must not be stored as defaults."""
        status, _, body = self.make_request(
            "PUT", "/api/settings", data={"crawl_defaults": {"modified_since": "irgendwann"}})
        self.assertEqual(status, 400)

        status, _, body = self.make_request(
            "PUT", "/api/settings", data={"crawl_defaults": {"space_keys": ["A B"]}})
        self.assertEqual(status, 400)

        status, _, body = self.make_request(
            "PUT", "/api/settings", data={"confluence": {"selected_spaces": ["bad key"]}})
        self.assertEqual(status, 400)

        # Relative dates are accepted and normalized
        status, _, body = self.make_request(
            "PUT", "/api/settings", data={"crawl_defaults": {"modified_since": "7d"}})
        self.assertEqual(status, 200)
        updated = json.loads(body.decode("utf-8"))
        self.assertRegex(updated["crawl_defaults"]["modified_since"], r"^\d{4}-\d{2}-\d{2}$")
        self.make_request("PUT", "/api/settings", data={"crawl_defaults": {"modified_since": ""}})

    # 4. /api/settings/test
    def test_api_settings_test_unconfigured(self) -> None:
        status, _, body = self.make_request("POST", "/api/settings/test")
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("confluence", res)
        self.assertIn("jira", res)
        self.assertFalse(res["confluence"]["ok"])
        self.assertIn("Nicht konfiguriert", res["confluence"]["message"])

    # 4b. /api/spaces
    def test_api_spaces_unconfigured(self) -> None:
        status, _, body = self.make_request("GET", "/api/spaces")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "bad_request")
        self.assertIn("Confluence-Basis-URL", err["error"]["message"])

    def test_api_spaces_listing_cache_and_errors(self) -> None:
        class FakeSpacesClient:
            def __init__(self, calls: list) -> None:
                self.calls = calls

            def list_spaces(self, max_items: int = 2000) -> list[dict]:
                self.calls.append(max_items)
                return [{
                    "key": "ENG", "name": "Engineering", "type": "global", "id": "1",
                    "url": "https://confluence.example.intern/display/ENG",
                }]

        self.config_store.update({"confluence": {"base_url": "https://confluence.example.intern"}})
        calls: list[int] = []
        self.server.confluence_factory = lambda cfg: FakeSpacesClient(calls)
        try:
            status, _, body = self.make_request("GET", "/api/spaces")
            self.assertEqual(status, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual([s["key"] for s in data["spaces"]], ["ENG"])
            self.assertFalse(data["cached"])
            self.assertEqual(len(calls), 1)

            # Second request is served from the in-memory cache
            status, _, body = self.make_request("GET", "/api/spaces")
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 200)
            self.assertTrue(data["cached"])
            self.assertEqual(len(calls), 1)

            # ?refresh=1 forces a new upstream fetch
            status, _, body = self.make_request("GET", "/api/spaces?refresh=1")
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 200)
            self.assertFalse(data["cached"])
            self.assertEqual(len(calls), 2)

            # Changing the Confluence connection settings invalidates the cache
            status, _, body = self.make_request(
                "PUT", "/api/settings",
                data={"confluence": {"base_url": "https://confluence-mirror.example.intern"}})
            self.assertEqual(status, 200)
            status, _, body = self.make_request("GET", "/api/spaces")
            data = json.loads(body.decode("utf-8"))
            self.assertFalse(data["cached"])
            self.assertEqual(len(calls), 3)

            # Only selecting spaces (crawl defaults) keeps the cache valid
            status, _, body = self.make_request(
                "PUT", "/api/settings", data={"confluence": {"selected_spaces": ["ENG"]}})
            self.assertEqual(status, 200)
            status, _, body = self.make_request("GET", "/api/spaces")
            data = json.loads(body.decode("utf-8"))
            self.assertTrue(data["cached"])
            self.assertEqual(len(calls), 3)

            # Upstream failure with cached data -> stale cache instead of an error
            def failing_factory(cfg):
                raise RuntimeError("Upstream nicht erreichbar")

            self.server.confluence_factory = failing_factory
            status, _, body = self.make_request("GET", "/api/spaces?refresh=1")
            self.assertEqual(status, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertTrue(data["stale"])
            self.assertIn("Upstream nicht erreichbar", data["warning"])
            self.assertEqual([s["key"] for s in data["spaces"]], ["ENG"])

            # Upstream failure without cache -> 502
            self.server.spaces_cache = None
            status, _, body = self.make_request("GET", "/api/spaces")
            self.assertEqual(status, 502)
            err = json.loads(body.decode("utf-8"))
            self.assertEqual(err["error"]["code"], "upstream_error")
        finally:
            self.server.confluence_factory = None
            self.server.spaces_cache = None
            self.config_store.update({"confluence": {"base_url": "", "selected_spaces": []}})

    # 5. /api/jobs POST, GET current, GET {id}, POST {id}/stop
    def test_api_jobs_lifecycle(self) -> None:
        # POST valid job
        job_req = {"start_page": "https://confluence.example.intern/pages/100"}
        status, _, body = self.make_request("POST", "/api/jobs", data=job_req)
        self.assertEqual(status, 202)
        job_data = json.loads(body.decode("utf-8"))
        self.assertEqual(job_data["job_id"], 1)
        self.assertEqual(job_data["state"], "running")

        # GET /api/jobs/current
        status, _, body = self.make_request("GET", "/api/jobs/current")
        self.assertEqual(status, 200)
        curr = json.loads(body.decode("utf-8"))
        self.assertIsNotNone(curr["job"])
        self.assertEqual(curr["job"]["job_id"], 1)

        # GET /api/jobs/1
        status, _, body = self.make_request("GET", "/api/jobs/1")
        self.assertEqual(status, 200)
        j1 = json.loads(body.decode("utf-8"))
        self.assertEqual(j1["job"]["job_id"], 1)

        # Conflict starting second job while first is running
        status, _, body = self.make_request("POST", "/api/jobs", data=job_req)
        self.assertEqual(status, 409)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "job_conflict")

        # POST /api/jobs/1/stop
        status, _, body = self.make_request("POST", "/api/jobs/1/stop")
        self.assertEqual(status, 200)
        stopped = json.loads(body.decode("utf-8"))
        self.assertEqual(stopped["job"]["state"], "stopped")

        # Non-existent job GET and STOP -> 404
        status, _, _ = self.make_request("GET", "/api/jobs/999")
        self.assertEqual(status, 404)

        status, _, _ = self.make_request("POST", "/api/jobs/999/stop")
        self.assertEqual(status, 404)

    def test_api_jobs_invalid_payload(self) -> None:
        # Missing start_page
        status, _, body = self.make_request("POST", "/api/jobs", data={})
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "bad_request")

    def test_api_jobs_refresh_only(self) -> None:
        job_req = {"refresh_only": True}
        status, _, body = self.make_request("POST", "/api/jobs", data=job_req)
        self.assertEqual(status, 202)
        job_data = json.loads(body.decode("utf-8"))
        self.assertEqual(job_data["state"], "running")
        self.fake_jm._running = False

    # 6. /api/search
    def test_api_search(self) -> None:
        status, _, body = self.make_request("GET", "/api/search?q=feature")
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("results", res)
        self.assertGreaterEqual(len(res["results"]), 1)

        # Invalid type filter -> 400
        status, _, body = self.make_request("GET", "/api/search?q=test&type=invalid_type")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "bad_request")

    # 7. /api/projects
    def test_api_projects(self) -> None:
        status, _, body = self.make_request("GET", "/api/projects")
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("projects", res)
        # Should include ENG from seeded document
        proj_keys = [p["project"] for p in res["projects"]]
        self.assertIn("ENG", proj_keys)

    # 8. /api/documents/{uid}
    def test_api_document_detail(self) -> None:
        # URL encoded jira:ENG-1 -> jira%3AENG-1
        status, _, body = self.make_request("GET", "/api/documents/jira%3AENG-1")
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("document", res)
        self.assertEqual(res["document"]["uid"], "jira:ENG-1")
        self.assertEqual(res["document"]["title"], "Implement feature X")

        # Unknown document -> 404
        status, _, _ = self.make_request("GET", "/api/documents/jira%3ANOTFOUND")
        self.assertEqual(status, 404)

    # 9. /api/graph
    @patch('knowledge.graph.get_subgraph', return_value={'nodes': [{'uid': f'jira:{i}', 'title': 'Test document'} for i in range(100)], 'edges': []})
    def test_graph_compression_negotiation(self, _get_subgraph) -> None:
        status, headers, body = self.make_request('GET', '/api/graph', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('content-encoding'), 'gzip')
        self.assertEqual(headers.get('vary'), 'Accept-Encoding')
        expected = json.loads(gzip.decompress(body))
        for encoding in ('identity', 'gzip;q=0', 'br'):
            status, headers, body = self.make_request('GET', '/api/graph', headers={'Accept-Encoding': encoding})
            self.assertEqual(status, 200)
            self.assertNotIn('content-encoding', headers)
            self.assertEqual(json.loads(body), expected)

    def test_api_graph(self) -> None:
        status, _, body = self.make_request("GET", "/api/graph?center=jira:ENG-1&hops=1")
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("nodes", res)
        self.assertIn("edges", res)
        uids = [n["uid"] for n in res["nodes"]]
        self.assertIn("jira:ENG-1", uids)

        # only_with_gitlab filter
        status, _, body = self.make_request("GET", "/api/graph?only_with_gitlab=true")
        self.assertEqual(status, 200)
        res_gitlab = json.loads(body.decode("utf-8"))
        self.assertIn("nodes", res_gitlab)

        # Invalid hops -> 400
        status, _, body = self.make_request("GET", "/api/graph?hops=abc")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "bad_request")

    # 10. /api/stats
    def test_api_stats(self) -> None:
        status, _, body = self.make_request("GET", "/api/stats")
        self.assertEqual(status, 200)
        stats = json.loads(body.decode("utf-8"))
        self.assertIn("documents_total", stats)
        self.assertIn("chunks_total", stats)
        self.assertIn("edges_total", stats)
        self.assertEqual(stats["documents_total"], 2)

    # 11. Security: DNS Rebinding Protection (Host Header)
    def test_host_header_validation(self) -> None:
        # Evil host header -> 403
        status, _, body = self.make_request("GET", "/api/config", headers={"Host": "attacker.com"})
        self.assertEqual(status, 403)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "forbidden")

    # 12. Security: Origin Header Validation
    def test_origin_header_validation(self) -> None:
        # Evil origin on POST -> 403
        status, _, body = self.make_request(
            "POST",
            "/api/jobs",
            data={"start_page": "test"},
            headers={"Origin": "http://attacker.com"},
        )
        self.assertEqual(status, 403)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "forbidden")

        # Valid localhost origin on POST -> passes origin check (proceeds to job creation)
        status, _, _ = self.make_request(
            "POST",
            "/api/jobs",
            data={"start_page": "test"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status, 202)

    # 13. Security: Body size and Content-Type validation
    def test_payload_too_large(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            "POST",
            "/api/jobs",
            headers={
                "Host": f"127.0.0.1:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(1024 * 1024 + 100),
            },
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        body = resp.read()
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "payload_too_large")
        conn.close()

    def test_unsupported_media_type(self) -> None:
        status, _, body = self.make_request(
            "POST",
            "/api/jobs",
            data="not json",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "unsupported_media_type")

    # 14. HTTP 405 Method Not Allowed and 404 Route Not Found
    def test_method_not_allowed(self) -> None:
        # POST to GET-only route
        status, _, body = self.make_request("POST", "/api/config")
        self.assertEqual(status, 405)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "method_not_allowed")

        # GET to POST-only route
        status, _, body = self.make_request("GET", "/api/jobs")
        self.assertEqual(status, 405)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "method_not_allowed")

    def test_route_not_found(self) -> None:
        status, _, body = self.make_request("GET", "/api/nonexistent_route")
        self.assertEqual(status, 404)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "not_found")

    # 15. /api/top-documents
    def test_api_top_documents(self) -> None:
        status, _, body = self.make_request("GET", "/api/top-documents?limit=10")
        self.assertEqual(status, 200)
        res = json.loads(body.decode("utf-8"))
        self.assertIn("documents", res)
        self.assertIn("total", res)
        self.assertIn("limit", res)
        self.assertEqual(res["limit"], 10)

        # Invalid type -> 400
        status, _, body = self.make_request("GET", "/api/top-documents?types=unknown")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "bad_request")

        # Invalid limit -> 400
        status, _, body = self.make_request("GET", "/api/top-documents?limit=xyz")
        self.assertEqual(status, 400)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["error"]["code"], "bad_request")


if __name__ == "__main__":
    unittest.main()
