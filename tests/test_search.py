from __future__ import annotations

import unittest
from knowledge import db, models, search


class TestSearch(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.open_db(":memory:")

        # Add sample documents
        # 1. Confluence doc with architecture
        doc1 = models.FetchedDocument(
            uid="confluence:101",
            source_type="confluence",
            source_id="101",
            title="Database Connection Pool Architecture",
            url="https://wiki.corp/101",
            body_text="Detailed design of our database connection pool service and pooling config.",
            space_or_project="ENG",
            status_label="CURRENT",
            document_type="page",
            source_updated_at="2026-03-01T10:00:00Z",
            labels=["db", "arch"],
        )
        chunks1 = [
            models.Chunk(
                heading="Pool Config",
                body="Detailed design of our database connection pool service and pooling config.",
            )
        ]
        db.save_document(self.conn, doc1, chunks1)

        # 2. Confluence doc matching only pool
        doc2 = models.FetchedDocument(
            uid="confluence:102",
            source_type="confluence",
            source_id="102",
            title="Swimming Pool Social Event",
            url="https://wiki.corp/102",
            body_text="Come join us at the pool for drinks and swimming.",
            space_or_project="HR",
            status_label="CURRENT",
            document_type="page",
            source_updated_at="2026-03-02T10:00:00Z",
            labels=["social"],
        )
        chunks2 = [
            models.Chunk(
                heading="Event",
                body="Come join us at the pool for drinks and swimming.",
            )
        ]
        db.save_document(self.conn, doc2, chunks2)

        # 3. Jira doc with key ENG-42
        doc3 = models.FetchedDocument(
            uid="jira:ENG-42",
            source_type="jira",
            source_id="ENG-42",
            title="Fix login timeout error",
            url="https://jira.corp/ENG-42",
            body_text="Users report login failure and timeout on auth service.",
            space_or_project="ENG",
            status_label="IN PROGRESS",
            document_type="Bug",
            source_updated_at="2026-03-03T10:00:00Z",
            labels=["auth"],
        )
        chunks3 = [
            models.Chunk(
                heading="Bug Report",
                body="Users report login failure and timeout on auth service.",
            )
        ]
        db.save_document(self.conn, doc3, chunks3)

        # 4. Confluence doc mentioning login multiple times
        doc4 = models.FetchedDocument(
            uid="confluence:103",
            source_type="confluence",
            source_id="103",
            title="Login Service SSO Guide",
            url="https://wiki.corp/103",
            body_text="Login login login SSO authentication guide for developers.",
            space_or_project="ENG",
            status_label="CURRENT",
            document_type="page",
            source_updated_at="2026-03-04T10:00:00Z",
            labels=["auth"],
        )
        chunks4 = [
            models.Chunk(
                heading="Login SSO",
                body="Login login login SSO authentication guide for developers.",
            )
        ]
        db.save_document(self.conn, doc4, chunks4)

        # 5. Reference node (unindexed)
        db.ensure_node(self.conn, "confluence:999", "Unindexed Reference", "https://wiki.corp/999")

    def tearDown(self) -> None:
        self.conn.close()

    def test_special_characters_safety(self) -> None:
        """Sonderzeichen wie " * ( ) : ^ - AND OR NOT NEAR dürfen keinen Fehler auslösen."""
        dangerous_queries = [
            '" * ( ) : ^ - AND OR NOT NEAR',
            'AND',
            'OR',
            'NOT',
            'NEAR/3',
            '*',
            '""',
            'test:123',
            'foo "bar" baz*',
            '***',
            '(()))((',
            '---',
            '^^^',
            ': : :',
            '"unclosed quote',
        ]
        for q in dangerous_queries:
            with self.subTest(query=q):
                res = search.search(self.conn, q)
                self.assertIsInstance(res, dict)
                self.assertIn("results", res)
                self.assertIn("duration_ms", res)
                self.assertIsInstance(res["duration_ms"], float)

    def test_empty_and_whitespace_query(self) -> None:
        res1 = search.search(self.conn, "")
        self.assertEqual(res1["total_returned"], 0)
        self.assertEqual(res1["results"], [])

        res2 = search.search(self.conn, "    ")
        self.assertEqual(res2["total_returned"], 0)
        self.assertEqual(res2["results"], [])

    def test_limit_hard_capped_at_10(self) -> None:
        """Limit must be hard-capped at 10 even if caller requests more."""
        # Insert 15 documents matching the term 'multi'
        for i in range(15):
            d = models.FetchedDocument(
                uid=f"confluence:batch-{i}",
                source_type="confluence",
                source_id=f"batch-{i}",
                title=f"Batch Document {i} multi",
                url=f"https://wiki.corp/batch-{i}",
                body_text=f"Content for multi document {i}",
                space_or_project="ENG",
            )
            db.save_document(self.conn, d, [models.Chunk(heading="", body="Content multi")])

        res = search.search(self.conn, "multi", limit=50)
        self.assertLessEqual(res["total_returned"], 10)
        self.assertLessEqual(len(res["results"]), 10)
        self.assertEqual(res["total_returned"], 10)

    def test_type_filter_and_invalid_type(self) -> None:
        # Invalid type must raise ValueError
        with self.assertRaises(ValueError):
            search.search(self.conn, "login", type="invalid_type")

        # Confluence only
        res_conf = search.search(self.conn, "login", type="confluence")
        for r in res_conf["results"]:
            self.assertEqual(r["type"], "confluence")

        # Jira only
        res_jira = search.search(self.conn, "login", type="jira")
        for r in res_jira["results"]:
            self.assertEqual(r["type"], "jira")

    def test_project_filter(self) -> None:
        res = search.search(self.conn, "pool", project="ENG")
        self.assertEqual(res["total_returned"], 1)
        self.assertEqual(res["results"][0]["uid"], "confluence:101")
        self.assertEqual(res["results"][0]["project"], "ENG")

    def test_unindexed_ignored(self) -> None:
        res = search.search(self.conn, "Unindexed")
        self.assertEqual(res["total_returned"], 0)

    def test_exact_jira_key_priority(self) -> None:
        """Query with exact Jira key must prioritize that indexed Jira document first."""
        # Query "eng-42 login" should rank jira:ENG-42 first even though confluence:103 has many 'login' hits
        res = search.search(self.conn, "eng-42 login")
        self.assertGreaterEqual(res["total_returned"], 1)
        self.assertEqual(res["results"][0]["uid"], "jira:ENG-42")

        # Direct search for ENG-42
        res2 = search.search(self.conn, "ENG-42")
        self.assertEqual(res2["total_returned"], 1)
        self.assertEqual(res2["results"][0]["uid"], "jira:ENG-42")

    def test_token_coverage_bonus(self) -> None:
        """Document covering more query tokens should rank above document covering fewer."""
        res = search.search(self.conn, "database connection pool")
        self.assertGreaterEqual(res["total_returned"], 2)
        # confluence:101 has all 3 tokens, confluence:102 only has 'pool'
        self.assertEqual(res["results"][0]["uid"], "confluence:101")

    def test_result_structure_and_snippets(self) -> None:
        res = search.search(self.conn, "database")
        self.assertEqual(res["total_returned"], 1)
        item = res["results"][0]

        expected_keys = {
            "uid", "type", "title", "url", "project", "snippet",
            "snippet_parts", "updated_at", "status", "document_type"
        }
        self.assertEqual(set(item.keys()), expected_keys)
        self.assertEqual(item["uid"], "confluence:101")
        self.assertEqual(item["type"], "confluence")
        self.assertEqual(item["title"], "Database Connection Pool Architecture")
        self.assertEqual(item["project"], "ENG")
        self.assertEqual(item["status"], "CURRENT")
        self.assertEqual(item["document_type"], "page")
        self.assertEqual(item["updated_at"], "2026-03-01T10:00:00Z")

        # Snippet markers: plain snippet must not have \x02 or \x03
        self.assertNotIn("\x02", item["snippet"])
        self.assertNotIn("\x03", item["snippet"])
        self.assertIsInstance(item["snippet_parts"], list)
        self.assertGreater(len(item["snippet_parts"]), 0)
        has_hit = any(part["hit"] for part in item["snippet_parts"])
        self.assertTrue(has_hit)

    def test_list_projects(self) -> None:
        projects = search.list_projects(self.conn)
        self.assertIsInstance(projects, list)
        proj_map = {(p["type"], p["project"]): p["count"] for p in projects}
        self.assertEqual(proj_map.get(("confluence", "ENG")), 2)
        self.assertEqual(proj_map.get(("confluence", "HR")), 1)
        self.assertEqual(proj_map.get(("jira", "ENG")), 1)


if __name__ == "__main__":
    unittest.main()
