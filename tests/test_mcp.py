from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from knowledge import db, models, search
from knowledge.config import ConfigStore, load_config
from knowledge.mcp_server import McpServer, run_stdio


class TestMcpServer(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.open_db(":memory:")

        # Add sample documents
        doc1 = models.FetchedDocument(
            uid="confluence:101",
            source_type="confluence",
            source_id="101",
            title="Database Connection Architecture",
            url="https://wiki.corp/101",
            body_text="Guide to connection pool scaling and database timeout settings." * 20,
            space_or_project="ENG",
            status_label="CURRENT",
            document_type="page",
            source_updated_at="2026-03-01T12:00:00Z",
            labels=["db", "infra"],
        )
        chunks1 = [
            models.Chunk(
                heading="Pool",
                body="Guide to connection pool scaling and database timeout settings.",
            )
        ]
        db.save_document(self.conn, doc1, chunks1)

        doc2 = models.FetchedDocument(
            uid="jira:ENG-42",
            source_type="jira",
            source_id="ENG-42",
            title="Fix connection timeout in service",
            url="https://jira.corp/ENG-42",
            body_text="Jira issue for tracking pool connection timeout in production.",
            space_or_project="ENG",
            status_label="IN PROGRESS",
            document_type="Bug",
            source_updated_at="2026-03-02T12:00:00Z",
            labels=["bug"],
        )
        chunks2 = [
            models.Chunk(
                heading="Bug details",
                body="Jira issue for tracking pool connection timeout in production.",
            )
        ]
        db.save_document(self.conn, doc2, chunks2)

        # Add link
        db.add_edge(self.conn, "jira:ENG-42", models.LinkTarget(uid="confluence:101", kind="links_to"))

        self.server = McpServer(self.conn, {})

    def tearDown(self) -> None:
        self.conn.close()

    def test_initialize_negotiation(self) -> None:
        # Supported older version
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        res = self.server.handle(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 1)
        self.assertEqual(res["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(res["result"]["serverInfo"]["name"], "local-knowledge")
        self.assertIn("tools", res["result"]["capabilities"])

        # Unsupported version falls back to newest
        msg_unsupported = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
        res2 = self.server.handle(msg_unsupported)
        self.assertEqual(res2["result"]["protocolVersion"], "2025-06-18")

    def test_notifications_have_no_response(self) -> None:
        msg1 = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.assertIsNone(self.server.handle(msg1))

        msg2 = {"jsonrpc": "2.0", "method": "initialized"}
        self.assertIsNone(self.server.handle(msg2))

    def test_ping(self) -> None:
        msg = {"jsonrpc": "2.0", "id": "ping-1", "method": "ping"}
        res = self.server.handle(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "ping-1")
        self.assertEqual(res["result"], {})

    def test_tools_list(self) -> None:
        msg = {"jsonrpc": "2.0", "id": 10, "method": "tools/list"}
        res = self.server.handle(msg)
        self.assertIsNotNone(res)
        tools = res["result"]["tools"]
        self.assertEqual(len(tools), 6)

        names = {t["name"] for t in tools}
        expected = {
            "search_knowledge",
            "get_document",
            "get_document_neighbors",
            "get_graph_subgraph",
            "find_related_documents",
            "get_index_stats",
        }
        self.assertEqual(names, expected)

        for t in tools:
            self.assertIn("name", t)
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])

    def test_tool_call_search_knowledge_matches_search(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "search_knowledge",
                "arguments": {"query": "ENG-42 timeout", "limit": 5},
            },
        }
        res = self.server.handle(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 20)
        tool_res = res["result"]
        self.assertFalse(tool_res["isError"])
        self.assertIn("structuredContent", tool_res)

        direct_res = search.search(self.conn, "ENG-42 timeout", limit=5)
        # Same ranking and links; MCP omits the HTML-only snippet_parts field.
        expected = [{k: v for k, v in r.items() if k != "snippet_parts"} for r in direct_res["results"]]
        self.assertEqual(tool_res["structuredContent"]["results"], expected)

    def test_tool_call_get_document_truncated(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "get_document",
                "arguments": {"uid": "confluence:101", "max_chars": 150},
            },
        }
        res = self.server.handle(msg)
        tool_res = res["result"]
        self.assertFalse(tool_res["isError"])
        doc = tool_res["structuredContent"]
        self.assertEqual(doc["uid"], "confluence:101")
        self.assertTrue(doc["truncated"])
        self.assertEqual(len(doc["text"]), 150)

    def test_live_output_settings_default_cap_and_large_request(self) -> None:
        self.conn.execute("UPDATE documents SET body_text=? WHERE uid='confluence:101'", ('ä' * 100000,))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            settings = ConfigStore(path)
            settings.update({'mcp': {'default_max_chars': 300, 'max_output_chars': 60000}})
            server = McpServer(self.conn, config_loader=lambda: load_config(path))

            def get(**args):
                response = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                          'params': {'name': 'get_document',
                                                     'arguments': {'uid': 'confluence:101', **args}}})
                self.assertFalse(response['result']['isError'])
                return response['result']['structuredContent']

            self.assertEqual(len(get()['text']), 300)
            capped = get(max_chars=100000)
            self.assertEqual(capped['returned_chars'], 60000)
            self.assertEqual(capped['max_chars'], 60000)
            self.assertEqual(capped['total_chars'], 100000)
            self.assertTrue(capped['truncated'])
            # UI saves in another process; the already-running MCP must reload.
            settings.update({'mcp': {'default_max_chars': 80000, 'max_output_chars': 100000}})
            self.assertEqual(len(get()['text']), 80000)
            self.assertFalse(get(max_chars=100000)['truncated'])
            path.write_text('{broken', encoding='utf-8')
            result = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                    'params': {'name': 'get_document', 'arguments': {'uid': 'confluence:101'}}})
            self.assertTrue(result['result']['isError'])

    def test_document_output_rejects_non_integer_and_out_of_range(self) -> None:
        for value in (True, False, 100.5, '1000', 99, 2000001):
            with self.subTest(value=value):
                result = self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                             'params': {'name': 'get_document',
                                                        'arguments': {'uid': 'confluence:101', 'max_chars': value}}})
                self.assertTrue(result['result']['isError'])

    def test_tool_call_unknown_uid_is_error(self) -> None:
        for tool_name, args in [
            ("get_document", {"uid": "confluence:MISSING"}),
            ("get_document_neighbors", {"uid": "jira:MISSING"}),
            ("get_graph_subgraph", {"uid": "jira:MISSING"}),
            ("find_related_documents", {"uid": "confluence:MISSING"}),
        ]:
            with self.subTest(tool=tool_name):
                msg = {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": args},
                }
                res = self.server.handle(msg)
                self.assertTrue(res["result"]["isError"])
                self.assertIn("nicht gefunden", res["result"]["content"][0]["text"])

    def test_tool_call_parameter_validation(self) -> None:
        # Unknown parameter
        msg1 = {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {"name": "search_knowledge", "arguments": {"query": "test", "extra": 123}},
        }
        res1 = self.server.handle(msg1)
        self.assertTrue(res1["result"]["isError"])
        self.assertIn("Unbekannter Parameter", res1["result"]["content"][0]["text"])

        # Limit out of range
        msg2 = {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {"name": "search_knowledge", "arguments": {"query": "test", "limit": 99}},
        }
        res2 = self.server.handle(msg2)
        self.assertTrue(res2["result"]["isError"])

    def test_tool_call_unknown_tool_returns_invalid_params(self) -> None:
        msg = {
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {"name": "non_existent_tool", "arguments": {}},
        }
        res = self.server.handle(msg)
        self.assertEqual(res["error"]["code"], -32602)

    def test_unknown_method_error(self) -> None:
        msg = {"jsonrpc": "2.0", "id": 40, "method": "unknown_function"}
        res = self.server.handle(msg)
        self.assertEqual(res["error"]["code"], -32601)

    def test_invalid_request_errors(self) -> None:
        # Batch array
        res1 = self.server.handle([{"jsonrpc": "2.0"}])
        self.assertEqual(res1["error"]["code"], -32600)

        # Missing jsonrpc
        res2 = self.server.handle({"id": 1, "method": "ping"})
        self.assertEqual(res2["error"]["code"], -32600)

    def test_missing_database_returns_is_error(self) -> None:
        """When the database file does not exist, tool calls must return isError with help message."""
        missing_path = Path("/tmp/definitely_nonexistent_atlassian_fs.sqlite3")
        if missing_path.exists():
            missing_path.unlink()

        server = McpServer(missing_path, {})
        msg = {
            "jsonrpc": "2.0",
            "id": 50,
            "method": "tools/call",
            "params": {"name": "get_index_stats", "arguments": {}},
        }
        res = server.handle(msg)
        self.assertIsNotNone(res)
        tool_res = res["result"]
        self.assertTrue(tool_res["isError"])
        self.assertIn("init-db", tool_res["content"][0]["text"])

    def test_run_stdio_end_to_end(self) -> None:
        """Test stdio execution loop with io.StringIO including parse error and success."""
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp_file:
            db_path = Path(tmp_file.name)
            conn = db.open_db(db_path)
            conn.close()

            input_lines = [
                '{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n',
                'this is not valid json\n',
                '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}\n',
            ]
            stdin = io.StringIO("".join(input_lines))
            stdout = io.StringIO()

            run_stdio(db_path, {}, stdin=stdin, stdout=stdout)

            output_lines = [json.loads(line) for line in stdout.getvalue().strip().split("\n") if line.strip()]
            self.assertEqual(len(output_lines), 3)

            # Response 1: ping
            self.assertEqual(output_lines[0]["id"], 1)
            self.assertEqual(output_lines[0]["result"], {})

            # Response 2: parse error
            self.assertEqual(output_lines[1]["error"]["code"], -32700)

            # Response 3: tools/list
            self.assertEqual(output_lines[2]["id"], 2)
            self.assertEqual(len(output_lines[2]["result"]["tools"]), 6)


if __name__ == "__main__":
    unittest.main()
