from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from knowledge import db, graph, search

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

TOOL_DEFINITIONS = [
    {
        "name": "search_knowledge",
        "description": (
            "Search indexed Confluence pages and Jira issues using full-text search with "
            "relevance ranking. Use this tool first when looking for information on a topic, "
            "finding tickets, or discovering documentation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query terms or issue key (e.g. 'architecture guide' or 'ENG-42')",
                },
                "type": {
                    "type": "string",
                    "enum": ["all", "confluence", "jira"],
                    "description": "Filter results by document type (default 'all')",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Maximum number of results to return (max 10, default 10)",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_document",
        "description": (
            "Retrieve the full details and content of a specific Confluence page or Jira issue "
            "by its unique identifier (UID). Use this after finding a document via search to "
            "inspect its text and outgoing/incoming links."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "Document UID (e.g. 'confluence:12345' or 'jira:PROJ-123')",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 50000,
                    "description": "Maximum characters of body text to return (default 8000, max 50000)",
                },
            },
            "required": ["uid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_document_neighbors",
        "description": (
            "Get the incoming and outgoing links and relationships for a specific document node. "
            "Use this to navigate from an issue to its parent/subtasks, related tickets, or "
            "referenced Confluence pages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "Document UID to inspect (e.g. 'jira:PROJ-123')",
                },
                "direction": {
                    "type": "string",
                    "enum": ["both", "in", "out"],
                    "description": "Edge direction to traverse ('both', 'in', or 'out'; default 'both')",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum number of neighbor edges to return (default 50, max 200)",
                },
            },
            "required": ["uid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_graph_subgraph",
        "description": (
            "Retrieve a subgraph of nodes and edges centered around a given document UID within "
            "a specified number of hops, or the top nodes by connectivity. Use this to understand "
            "the local graph structure around an entity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "Center document UID (optional; if omitted, returns highest-degree nodes)",
                },
                "hops": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                    "description": "Graph traversal depth in hops (0 to 3, default 1)",
                },
                "max_nodes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 150,
                    "description": "Maximum number of nodes to include (default 50, max 150)",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "find_related_documents",
        "description": (
            "Find related documents that are direct neighbors or share common neighbors with "
            "the specified document. Use this to discover connected tickets and documentation "
            "relevant to the current context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "Document UID for finding related items",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum number of related documents to return (default 10, max 50)",
                },
            },
            "required": ["uid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_index_stats",
        "description": (
            "Get statistics about the local knowledge index, including total indexed Confluence "
            "pages, Jira issues, edges, and the status of the latest crawl job. Use this to check "
            "index readiness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

TOOL_MAP = {t["name"]: t for t in TOOL_DEFINITIONS}


class McpServer:
    """JSON-RPC 2.0 MCP server over stdio for knowledge graph queries."""

    def __init__(self, db_path: str | Path | sqlite3.Connection, cfg: dict | None = None) -> None:
        self.db_path = db_path
        self.cfg = cfg or {}
        if isinstance(db_path, sqlite3.Connection):
            self._conn: sqlite3.Connection | None = db_path
        else:
            self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if isinstance(self.db_path, (str, Path)):
            self._conn = db.connect(self.db_path, readonly=True)
            return self._conn
        raise RuntimeError("No database connection available")

    @staticmethod
    def _tool_success(data: Any) -> dict[str, Any]:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            "structuredContent": data,
            "isError": False,
        }

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {
            "content": [
                {
                    "type": "text",
                    "text": message,
                }
            ],
            "structuredContent": {"error": message},
            "isError": True,
        }

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool with strict schema validation."""
        # 1. Connect to DB
        try:
            conn = self._get_conn()
        except Exception:
            return self._tool_error(
                "Datenbank existiert nicht oder ist nicht initialisiert. "
                "Bitte 'python app.py init-db' ausführen oder eine Indexierung starten."
            )

        tool_def = TOOL_MAP[name]
        schema = tool_def["inputSchema"]
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Check unknown parameters
        for k in args:
            if k not in properties:
                return self._tool_error(f"Unbekannter Parameter: {k!r}")

        # Check required parameters
        for req in required:
            if req not in args:
                return self._tool_error(f"Erforderlicher Parameter {req!r} fehlt.")

        # Tool specific handling
        if name == "search_knowledge":
            query = args.get("query")
            if not isinstance(query, str):
                return self._tool_error("Parameter 'query' muss ein String sein.")
            stype = args.get("type", "all")
            if stype not in ("all", "confluence", "jira"):
                return self._tool_error("Parameter 'type' muss 'all', 'confluence' oder 'jira' sein.")
            limit = args.get("limit", 10)
            if not isinstance(limit, int) or limit < 1 or limit > 10:
                return self._tool_error("Parameter 'limit' muss eine ganze Zahl zwischen 1 und 10 sein.")

            res = search.search(conn, query, type=stype, limit=limit)
            # snippet_parts only exist for HTML highlighting; keep MCP answers compact.
            for item in res["results"]:
                item.pop("snippet_parts", None)
            return self._tool_success(res)

        if name == "get_document":
            uid = args.get("uid")
            if not isinstance(uid, str):
                return self._tool_error("Parameter 'uid' muss ein String sein.")
            max_chars = args.get("max_chars", 8000)
            if not isinstance(max_chars, int) or max_chars < 100 or max_chars > 50000:
                return self._tool_error("Parameter 'max_chars' muss zwischen 100 und 50000 liegen.")

            doc = graph.get_document_detail(conn, uid, max_chars=max_chars)
            if doc is None:
                return self._tool_error(f"Dokument mit UID {uid!r} nicht gefunden.")
            return self._tool_success(doc)

        if name == "get_document_neighbors":
            uid = args.get("uid")
            if not isinstance(uid, str):
                return self._tool_error("Parameter 'uid' muss ein String sein.")
            direction = args.get("direction", "both")
            if direction not in ("both", "in", "out"):
                return self._tool_error("Parameter 'direction' muss 'both', 'in' oder 'out' sein.")
            limit = args.get("limit", 50)
            if not isinstance(limit, int) or limit < 1 or limit > 200:
                return self._tool_error("Parameter 'limit' muss zwischen 1 und 200 liegen.")

            neighbors = graph.get_neighbors(conn, uid, direction=direction, limit=limit)
            if neighbors is None:
                return self._tool_error(f"Dokument mit UID {uid!r} nicht gefunden.")
            return self._tool_success(neighbors)

        if name == "get_graph_subgraph":
            uid = args.get("uid")
            if uid is not None and not isinstance(uid, str):
                return self._tool_error("Parameter 'uid' muss ein String sein.")
            hops = args.get("hops", 1)
            if not isinstance(hops, int) or hops < 0 or hops > 3:
                return self._tool_error("Parameter 'hops' muss zwischen 0 und 3 liegen.")
            max_nodes = args.get("max_nodes", 50)
            if not isinstance(max_nodes, int) or max_nodes < 1 or max_nodes > 150:
                return self._tool_error("Parameter 'max_nodes' muss zwischen 1 und 150 liegen.")

            subg = graph.get_subgraph(conn, center=uid, hops=hops, max_nodes=max_nodes)
            if subg is None:
                return self._tool_error(f"Dokument mit UID {uid!r} nicht gefunden.")
            return self._tool_success(subg)

        if name == "find_related_documents":
            uid = args.get("uid")
            if not isinstance(uid, str):
                return self._tool_error("Parameter 'uid' muss ein String sein.")
            limit = args.get("limit", 10)
            if not isinstance(limit, int) or limit < 1 or limit > 50:
                return self._tool_error("Parameter 'limit' muss zwischen 1 und 50 liegen.")

            related = graph.find_related(conn, uid, limit=limit)
            if related is None:
                return self._tool_error(f"Dokument mit UID {uid!r} nicht gefunden.")
            return self._tool_success(related)

        if name == "get_index_stats":
            stats = db.get_stats(conn)
            return self._tool_success(stats)

        return self._tool_error(f"Unbekanntes Tool: {name!r}")

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Handle incoming parsed JSON-RPC message.

        Returns response dictionary, or None if message is a notification.
        """
        if isinstance(message, list):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Batch requests are not supported"},
            }

        if not isinstance(message, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: message must be an object"},
            }

        req_id = message.get("id")

        if message.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: jsonrpc must be '2.0'"},
            }

        method = message.get("method")
        if not isinstance(method, str):
            if req_id is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32600, "message": "Invalid Request: method is required"},
                }
            return None

        # Notifications (no id or notifications/*) do not receive responses
        if method.startswith("notifications/") or method == "initialized":
            return None

        if method == "initialize":
            params = message.get("params") or {}
            client_version = params.get("protocolVersion")
            negotiated_version = (
                client_version
                if client_version in SUPPORTED_PROTOCOL_VERSIONS
                else LATEST_PROTOCOL_VERSION
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": negotiated_version,
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "local-knowledge",
                        "version": "1.0.0",
                    },
                    "instructions": (
                        "Local Confluence and Jira Knowledge Graph server. Provides full-text "
                        "search, document inspection, relationship discovery, and subgraph navigation."
                    ),
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_DEFINITIONS}}

        if method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or "name" not in params:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Invalid params: 'name' is required"},
                }

            tool_name = params["name"]
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Invalid params: arguments must be an object"},
                }

            if tool_name not in TOOL_MAP:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {tool_name!r}"},
                }

            tool_result = self._execute_tool(tool_name, arguments)
            return {"jsonrpc": "2.0", "id": req_id, "result": tool_result}

        # Unknown method
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method!r}"},
        }


def run_stdio(
    db_path: str | Path,
    cfg: dict | None = None,
    stdin: Any = sys.stdin,
    stdout: Any = sys.stdout,
) -> None:
    """Run MCP server stdio event loop."""
    server = McpServer(db_path, cfg)
    for line in stdin:
        line_str = line.strip() if isinstance(line, str) else line.decode("utf-8").strip()
        if not line_str:
            continue

        try:
            message = json.loads(line_str)
        except Exception:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            stdout.flush()
            continue

        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
