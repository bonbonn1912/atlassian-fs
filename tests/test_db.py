from __future__ import annotations

import sqlite3
import unittest

from knowledge import db, models


class TestDatabaseMigrationsAndDates(unittest.TestCase):
    def test_v2_to_v3_migration(self) -> None:
        """Migrating an existing V2 database adds source_created_at column."""
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row

        # Simulate database at V2
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.executescript(db.SCHEMA_V1)
        conn.executescript(db.SCHEMA_V2)
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (2, '2026-01-01T00:00:00Z')")

        # Verify source_created_at does not exist yet
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
        self.assertNotIn("source_created_at", cols_before)

        # Apply migrations
        latest_ver = db.init_db(conn)
        self.assertEqual(latest_ver, 3)

        # Verify source_created_at exists and migration recorded
        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
        self.assertIn("source_created_at", cols_after)
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        self.assertEqual(versions, [1, 2, 3])

        # Verify idempotency
        re_init_ver = db.init_db(conn)
        self.assertEqual(re_init_ver, 3)
        conn.close()

    def test_migration_duplicate_column_tolerance(self) -> None:
        """If column source_created_at was already added before migration recorded, init_db succeeds."""
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.executescript(db.SCHEMA_V1)
        conn.executescript(db.SCHEMA_V2)
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (2, '2026-01-01T00:00:00Z')")

        # Manually alter table beforehand
        conn.execute("ALTER TABLE documents ADD COLUMN source_created_at TEXT")

        # init_db must handle 'duplicate column name' gracefully
        latest_ver = db.init_db(conn)
        self.assertEqual(latest_ver, 3)
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        self.assertEqual(versions, [1, 2, 3])
        conn.close()

    def test_missing_dates_are_not_manufactured(self) -> None:
        """Missing dates must stay None and not be manufactured from fetched_at or now."""
        conn = db.open_db(":memory:")
        doc = models.FetchedDocument(
            uid="jira:PROJ-1",
            source_type="jira",
            source_id="PROJ-1",
            title="Issue 1",
            url="https://example.com/1",
            body_text="Body content",
            source_created_at=None,
            source_updated_at=None,
        )
        db.save_document(conn, doc, [])

        row = conn.execute("SELECT source_created_at, source_updated_at, fetched_at FROM documents WHERE uid=?", (doc.uid,)).fetchone()
        self.assertIsNone(row["source_created_at"])
        self.assertIsNone(row["source_updated_at"])
        self.assertIsNotNone(row["fetched_at"])
        conn.close()

    def test_partial_update_preserves_creation_and_update_dates(self) -> None:
        """Partial updates with None/empty dates must preserve existing timestamps."""
        conn = db.open_db(":memory:")
        doc = models.FetchedDocument(
            uid="jira:PROJ-2",
            source_type="jira",
            source_id="PROJ-2",
            title="Issue 2",
            url="https://example.com/2",
            body_text="Initial Body",
            source_created_at="2026-01-10T10:00:00Z",
            source_updated_at="2026-01-11T12:00:00Z",
        )
        db.save_document(conn, doc, [])

        # Simulate partial update where dates are not supplied (None)
        partial_doc = models.FetchedDocument(
            uid="jira:PROJ-2",
            source_type="jira",
            source_id="PROJ-2",
            title="Issue 2 (Updated Title)",
            url="https://example.com/2",
            body_text="Updated Body",
            source_created_at=None,
            source_updated_at=None,
        )
        db.save_document(conn, partial_doc, [])

        row = conn.execute("SELECT title, source_created_at, source_updated_at FROM documents WHERE uid=?", (doc.uid,)).fetchone()
        self.assertEqual(row["title"], "Issue 2 (Updated Title)")
        self.assertEqual(row["source_created_at"], "2026-01-10T10:00:00Z")
        self.assertEqual(row["source_updated_at"], "2026-01-11T12:00:00Z")
        conn.close()

    def test_refetch_short_circuit_backfills_dates(self) -> None:
        """When content_hash is unchanged, save_document backfills newly added dates without rechunking."""
        conn = db.open_db(":memory:")
        # Initially saved with no dates (e.g. from an older indexing run)
        doc1 = models.FetchedDocument(
            uid="confluence:100",
            source_type="confluence",
            source_id="100",
            title="Architecture",
            url="https://example.com/arch",
            body_text="Immutable architecture text.",
            source_created_at=None,
            source_updated_at=None,
        )
        res1 = db.save_document(conn, doc1, [models.Chunk(heading="Overview", body="Immutable architecture text.")])
        self.assertTrue(res1["content_changed"])

        chunks_count = conn.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?", (res1["id"],)).fetchone()[0]
        self.assertEqual(chunks_count, 1)

        # Re-fetch with unchanged body/title, but now with timestamps
        doc2 = models.FetchedDocument(
            uid="confluence:100",
            source_type="confluence",
            source_id="100",
            title="Architecture",
            url="https://example.com/arch",
            body_text="Immutable architecture text.",
            source_created_at="2026-01-01T00:00:00Z",
            source_updated_at="2026-02-01T00:00:00Z",
        )
        res2 = db.save_document(conn, doc2, [models.Chunk(heading="Overview", body="Immutable architecture text.")])
        # Chunks short-circuited
        self.assertFalse(res2["content_changed"])

        # DB record backfilled
        row = conn.execute("SELECT source_created_at, source_updated_at FROM documents WHERE uid=?", (doc2.uid,)).fetchone()
        self.assertEqual(row["source_created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["source_updated_at"], "2026-02-01T00:00:00Z")
        conn.close()

    def test_fetched_document_positional_args_backward_compatibility(self) -> None:
        """Callers using positional arguments up to 14 arguments remain compatible."""
        # 14 original arguments: uid .. labels
        doc = models.FetchedDocument(
            "jira:X-1",               # 0: uid
            "jira",                   # 1: source_type
            "X-1",                    # 2: source_id
            "Title",                  # 3: title
            "https://example.com/x",  # 4: url
            "Body",                   # 5: body_text
            [],                       # 6: sections
            [],                       # 7: links
            "PROJ",                   # 8: space_or_project
            "Open",                   # 9: status_label
            "Bug",                    # 10: document_type
            "1",                      # 11: source_version
            "2026-03-01T00:00:00Z",   # 12: source_updated_at
            ["tag1"],                 # 13: labels
            "2026-01-01T00:00:00Z",   # 14: source_created_at
        )
        self.assertEqual(doc.source_created_at, "2026-01-01T00:00:00Z")
        self.assertEqual(doc.source_updated_at, "2026-03-01T00:00:00Z")
        self.assertEqual(doc.labels, ["tag1"])


if __name__ == "__main__":
    unittest.main()
