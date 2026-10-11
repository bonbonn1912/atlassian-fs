from __future__ import annotations

import unittest
from knowledge import db, graph, models


class TestGraph(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.open_db(":memory:")

        # Create documents:
        # A (Confluence) -> B (Jira)
        # B (Jira) -> C (Confluence)
        # C (Confluence) -> D (Jira)
        # B (Jira) -> E (Jira)
        # F (Confluence, isolated)
        # G (Reference / discovered node)
        docs = [
            ("confluence:A", "confluence", "A", "Page A", "ENG", "page", "CURRENT", "tag1, tag2", "Short body for A"),
            ("jira:B", "jira", "B", "Issue B", "ENG", "Story", "DONE", "tag2", "Body for B " * 50),
            ("confluence:C", "confluence", "C", "Page C", "HR", "page", "CURRENT", "", "Body for C"),
            ("jira:D", "jira", "D", "Issue D", "HR", "Bug", "OPEN", "bug", "Body for D"),
            ("jira:E", "jira", "E", "Issue E", "ENG", "Subtask", "IN PROGRESS", "", "Body for E"),
            ("confluence:F", "confluence", "F", "Isolated Page F", "ENG", "page", "DRAFT", "", "Body for F"),
        ]

        for uid, stype, sid, title, proj, dtype, status, labels, body in docs:
            d = models.FetchedDocument(
                uid=uid,
                source_type=stype,
                source_id=sid,
                title=title,
                url=f"https://example.com/{uid}",
                body_text=body,
                space_or_project=proj,
                document_type=dtype,
                status_label=status,
                labels=[l.strip() for l in labels.split(",") if l.strip()],
                source_version="1",
                source_updated_at="2026-03-01T00:00:00Z",
            )
            db.save_document(self.conn, d, [models.Chunk(heading="", body=body[:50])])

        # Add unindexed reference node
        db.ensure_node(self.conn, "confluence:G", "Ref G", "https://example.com/G")

        # Edges:
        # A -> B (links_to)
        # B -> C (links_to)
        # C -> D (mentions)
        # B -> E (parent_of)
        # A -> G (links_to)
        db.add_edge(self.conn, "confluence:A", models.LinkTarget(uid="jira:B", kind="links_to"))
        db.add_edge(self.conn, "jira:B", models.LinkTarget(uid="confluence:C", kind="links_to"))
        db.add_edge(self.conn, "confluence:C", models.LinkTarget(uid="jira:D", kind="mentions"))
        db.add_edge(self.conn, "jira:B", models.LinkTarget(uid="jira:E", kind="parent_of"))
        db.add_edge(self.conn, "confluence:A", models.LinkTarget(uid="confluence:G", kind="links_to"))

    def tearDown(self) -> None:
        self.conn.close()

    def test_get_subgraph_with_center(self) -> None:
        # 1-hop around jira:B: neighbors are A (in), C (out), E (out)
        sub = graph.get_subgraph(self.conn, "jira:B", hops=1)
        self.assertIsNotNone(sub)
        self.assertEqual(sub["center"], "jira:B")
        self.assertFalse(sub["truncated"])
        self.assertEqual(sub["hidden_nodes"], 0)

        node_uids = {n["uid"] for n in sub["nodes"]}
        self.assertEqual(node_uids, {"jira:B", "confluence:A", "confluence:C", "jira:E"})

        # Center must be first node in deterministic ordering
        self.assertEqual(sub["nodes"][0]["uid"], "jira:B")

        # In-degree and out-degree must be reported on nodes
        b_node = next(n for n in sub["nodes"] if n["uid"] == "jira:B")
        self.assertEqual(b_node["in_degree"], 1)
        self.assertEqual(b_node["out_degree"], 2)
        self.assertEqual(b_node["degree"], 3)

        # Edges must only be between included nodes
        for e in sub["edges"]:
            self.assertIn(e["source"], node_uids)
            self.assertIn(e["target"], node_uids)

    def test_get_subgraph_hops_clamping(self) -> None:
        # Hops clamped to 0..3
        # hops = 0: only center
        sub0 = graph.get_subgraph(self.conn, "jira:B", hops=0)
        self.assertEqual(len(sub0["nodes"]), 1)
        self.assertEqual(sub0["nodes"][0]["uid"], "jira:B")
        self.assertEqual(len(sub0["edges"]), 0)

        # hops = 10 -> clamped to 3 hops (from A: B, G at hop 1; C, E at hop 2; D at hop 3)
        sub3 = graph.get_subgraph(self.conn, "confluence:A", hops=10)
        node_uids = {n["uid"] for n in sub3["nodes"]}
        self.assertIn("jira:D", node_uids)

    def test_get_subgraph_max_nodes_truncation(self) -> None:
        # Limit to 2 nodes
        sub = graph.get_subgraph(self.conn, "jira:B", hops=1, max_nodes=2)
        self.assertTrue(sub["truncated"])
        self.assertEqual(len(sub["nodes"]), 2)
        # jira:B has 3 neighbors (A, C, E), only 1 neighbor fits, so 2 hidden nodes
        self.assertEqual(sub["hidden_nodes"], 2)

    def test_get_subgraph_type_filter_preserves_center(self) -> None:
        """Center node must remain included even if types filter doesn't match center."""
        # Center is confluence:A, but filter is types='jira'
        sub = graph.get_subgraph(self.conn, "confluence:A", hops=1, types="jira")
        self.assertIsNotNone(sub)
        self.assertEqual(sub["nodes"][0]["uid"], "confluence:A")
        # Neighbor jira:B is jira -> included; confluence:G is confluence -> excluded
        node_uids = {n["uid"] for n in sub["nodes"]}
        self.assertIn("jira:B", node_uids)
        self.assertNotIn("confluence:G", node_uids)

    def test_get_subgraph_edge_kinds_filter(self) -> None:
        # Only 'parent_of' edges
        sub = graph.get_subgraph(self.conn, "jira:B", hops=1, edge_kinds=["parent_of"])
        node_uids = {n["uid"] for n in sub["nodes"]}
        self.assertEqual(node_uids, {"jira:B", "jira:E"})
        self.assertEqual(len(sub["edges"]), 1)
        self.assertEqual(sub["edges"][0]["kind"], "parent_of")

    def test_get_subgraph_unknown_center_returns_none(self) -> None:
        sub = graph.get_subgraph(self.conn, "confluence:NONEXISTENT")
        self.assertIsNone(sub)

    def test_get_subgraph_without_center(self) -> None:
        # Without center: top nodes by degree
        sub = graph.get_subgraph(self.conn, None, max_nodes=3)
        self.assertIsNone(sub["center"])
        self.assertEqual(len(sub["nodes"]), 3)
        # jira:B has highest degree (incident edges: A->B, B->C, B->E = 3)
        self.assertEqual(sub["nodes"][0]["uid"], "jira:B")
        self.assertTrue(sub["truncated"])
        self.assertGreater(sub["hidden_nodes"], 0)

    def test_get_document_detail(self) -> None:
        doc = graph.get_document_detail(self.conn, "jira:B", max_chars=50)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["uid"], "jira:B")
        self.assertEqual(doc["type"], "jira")
        self.assertEqual(doc["source_id"], "B")
        self.assertEqual(doc["title"], "Issue B")
        self.assertEqual(doc["project"], "ENG")
        self.assertEqual(doc["status"], "DONE")
        self.assertEqual(doc["document_type"], "Story")
        self.assertEqual(doc["source_version"], "1")
        self.assertEqual(doc["updated_at"], "2026-03-01T00:00:00Z")
        self.assertEqual(doc["state"], "indexed")
        self.assertIsNotNone(doc["fetched_at"])
        self.assertIsNone(doc["last_error"])

        # Labels as list
        self.assertEqual(doc["labels"], ["tag2"])

        # Truncation
        self.assertTrue(doc["truncated"])
        self.assertEqual(len(doc["text"]), 50)

        # Outgoing: B -> C (links_to), B -> E (parent_of)
        self.assertEqual(len(doc["outgoing"]), 2)
        out_targets = {e["target"]["uid"] for e in doc["outgoing"]}
        self.assertEqual(out_targets, {"confluence:C", "jira:E"})

        # Incoming: A -> B (links_to)
        self.assertEqual(len(doc["incoming"]), 1)
        self.assertEqual(doc["incoming"][0]["source"]["uid"], "confluence:A")

    def test_get_document_detail_not_found(self) -> None:
        doc = graph.get_document_detail(self.conn, "jira:UNKNOWN")
        self.assertIsNone(doc)

    def test_get_neighbors(self) -> None:
        # Invalid direction
        with self.assertRaises(ValueError):
            graph.get_neighbors(self.conn, "jira:B", direction="sideways")

        # Unknown UID
        self.assertIsNone(graph.get_neighbors(self.conn, "jira:UNKNOWN"))

        # Both
        nbrs_both = graph.get_neighbors(self.conn, "jira:B", direction="both")
        self.assertEqual(nbrs_both["total"], 3)
        self.assertEqual(len(nbrs_both["outgoing"]), 2)
        self.assertEqual(len(nbrs_both["incoming"]), 1)

        # Out only
        nbrs_out = graph.get_neighbors(self.conn, "jira:B", direction="out")
        self.assertEqual(nbrs_out["total"], 2)
        self.assertEqual(len(nbrs_out["outgoing"]), 2)
        self.assertEqual(len(nbrs_out["incoming"]), 0)

        # In only
        nbrs_in = graph.get_neighbors(self.conn, "jira:B", direction="in")
        self.assertEqual(nbrs_in["total"], 1)
        self.assertEqual(len(nbrs_in["outgoing"]), 0)
        self.assertEqual(len(nbrs_in["incoming"]), 1)

    def test_find_related(self) -> None:
        # Unknown UID
        self.assertIsNone(graph.find_related(self.conn, "jira:UNKNOWN"))

        # Around confluence:A:
        # Direct neighbor is jira:B and confluence:G.
        # 2nd-degree neighbors through B are C and E (they share B with A).
        related = graph.find_related(self.conn, "confluence:A", limit=10)
        self.assertIsNotNone(related)
        uids = [r["uid"] for r in related["related"]]

        # Self must be excluded
        self.assertNotIn("confluence:A", uids)
        # Direct neighbor jira:B and 2-hop C and E must be found
        self.assertIn("jira:B", uids)
        self.assertIn("confluence:C", uids)
        self.assertIn("jira:E", uids)

        # Direct neighbor has is_direct=True
        b_entry = next(r for r in related["related"] if r["uid"] == "jira:B")
        self.assertTrue(b_entry["is_direct"])

        # 2nd-degree neighbor has shared_count >= 1
        c_entry = next(r for r in related["related"] if r["uid"] == "confluence:C")
        self.assertEqual(c_entry["shared_count"], 1)
        self.assertFalse(c_entry["is_direct"])

    def test_get_top_documents(self) -> None:
        # Add an extra link pointing to jira:B to make it the top linked document (2 incoming)
        # D -> B
        db.add_edge(self.conn, "jira:D", models.LinkTarget(uid="jira:B", kind="links_to"))

        res = graph.get_top_documents(self.conn, limit=10)
        self.assertGreaterEqual(res["total"], 6)
        docs = res["documents"]
        # jira:B should be rank 1 with 2 incoming links
        self.assertEqual(docs[0]["uid"], "jira:B")
        self.assertEqual(docs[0]["incoming_count"], 2)
        self.assertEqual(docs[0]["outgoing_count"], 2)
        self.assertEqual(docs[0]["total_degree"], 4)

        # Type filter: only confluence
        conf_res = graph.get_top_documents(self.conn, types="confluence")
        for d in conf_res["documents"]:
            self.assertEqual(d["type"], "confluence")

        # Project filter: HR
        hr_res = graph.get_top_documents(self.conn, project="HR")
        for d in hr_res["documents"]:
            self.assertEqual(d["project"], "HR")

        # Query filter: "Issue D"
        q_res = graph.get_top_documents(self.conn, query="Issue D")
        self.assertEqual(len(q_res["documents"]), 1)
        self.assertEqual(q_res["documents"][0]["uid"], "jira:D")

        # Pagination: limit=2, offset=1
        page = graph.get_top_documents(self.conn, limit=2, offset=1)
        self.assertEqual(len(page["documents"]), 2)
        self.assertEqual(page["documents"][0]["uid"], docs[1]["uid"])

        # Invalid type check
        with self.assertRaises(ValueError):
            graph.get_top_documents(self.conn, types="invalid_type")


if __name__ == "__main__":
    unittest.main()
