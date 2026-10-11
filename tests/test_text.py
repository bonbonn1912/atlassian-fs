"""Unit tests for knowledge/text.py.

Covers:
- Confluence Storage format:
  - ac:link / ri:page (with/without space-key)
  - ri:content-id and ri:content-entity
  - Jira macro (key extracted, jqlQuery ignored)
  - CDATA code block
  - Headings -> sections
  - HTML Table
  - Deduplication and scan_plain_keys
- ADF (Atlassian Document Format):
  - heading, paragraph, bulletList, orderedList, hardBreak, codeBlock, table, inlineCard/blockCard, link mark, unknown node
- Jira Wiki Markup:
  - h1..h6 headings -> sections
  - [text|url], [url], bare URLs
  - {code}, {noformat}, {quote}, {panel} stripping
  - Tables ||h1|| and |c1|
  - [~user] mention stripping
- Chunk sections:
  - Overlap prefixed
  - min_chars merging
  - Long text without spaces (lange Texte ohne Leerzeichen)
  - Empty sections / fallback
- truncate_text helper
"""

import json
from pathlib import Path
import unittest

from knowledge.links import LinkContext
from knowledge.models import EDGE_LINKS_TO, EDGE_MENTIONS_JIRA, Chunk
from knowledge.text import (
    adf_to_content,
    chunk_sections,
    jira_wiki_to_content,
    parse_confluence_storage,
    truncate_text,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestText(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = LinkContext.from_config(
            {
                "confluence": {
                    "base_url": "https://confluence.intern",
                    "aliases": ["https://wiki.intern"],
                },
                "jira": {
                    "base_url": "https://jira.intern",
                    "aliases": [],
                    "project_keys": ["PROJ", "AUTH", "DEV"],
                },
            }
        )

    # --- Confluence Storage Format Tests ---

    def test_confluence_storage_fixture(self) -> None:
        fixture_path = FIXTURES_DIR / "confluence_storage_server.html"
        storage = fixture_path.read_text(encoding="utf-8")

        parsed = parse_confluence_storage(storage, self.ctx, space_key="DEFAULT")

        # Check sections
        headings = [h for h, _ in parsed.sections]
        self.assertIn("", headings)  # Intro text before first heading
        self.assertIn("System Overview", headings)
        self.assertIn("Confluence Links", headings)
        self.assertIn("Jira Integration", headings)
        self.assertIn("Code Example", headings)
        self.assertIn("Data Table", headings)

        # Intro text
        intro_sec = next(t for h, t in parsed.sections if h == "")
        self.assertIn("Introduction paragraph before any heading.", intro_sec)

        # Code section with CDATA
        code_sec = next(t for h, t in parsed.sections if h == "Code Example")
        self.assertIn("def calculate(a, b):", code_sec)
        self.assertIn("return a + b", code_sec)
        # Parameter value "python" should NOT be in readable body
        self.assertNotIn("python", code_sec)

        # Table section
        table_sec = next(t for h, t in parsed.sections if h == "Data Table")
        self.assertIn("Component | Status | Owner", table_sec)
        self.assertIn("Frontend | Active | Alice", table_sec)

        # Check links
        uids = {link.uid: link for link in parsed.links}

        # ac:link with ri:space-key
        self.assertIn("confluence:unresolved:ENG:Architecture Guide", uids)
        self.assertEqual(
            uids["confluence:unresolved:ENG:Architecture Guide"].anchor_text,
            "Read the Arch Guide",
        )
        self.assertEqual(
            uids["confluence:unresolved:ENG:Architecture Guide"].source_field,
            "ac:link",
        )

        # ac:link without space-key (falls back to space_key="DEFAULT")
        self.assertIn("confluence:unresolved:DEFAULT:Local Notes", uids)
        self.assertEqual(
            uids["confluence:unresolved:DEFAULT:Local Notes"].title_hint,
            "Local Notes",
        )

        # ri:content-entity with content-id
        self.assertIn("confluence:98765", uids)
        self.assertEqual(uids["confluence:98765"].anchor_text, "Page 98765")

        # ri:page with ri:content-id
        self.assertIn("confluence:54321", uids)

        # HTML link with umlaut
        self.assertIn("confluence:unresolved:ENG:München Office", uids)
        self.assertEqual(
            uids["confluence:unresolved:ENG:München Office"].source_field, "body"
        )

        # Relative viewpage action
        self.assertIn("confluence:112233", uids)

        # Foreign external link ignored
        for link in parsed.links:
            self.assertNotIn("external-site.com", link.uid)

        # Jira macro
        self.assertIn("jira:PROJ-42", uids)
        self.assertEqual(uids["jira:PROJ-42"].source_field, "jira-macro")
        self.assertEqual(uids["jira:PROJ-42"].kind, EDGE_MENTIONS_JIRA)

        # Jira HTML link
        self.assertIn("jira:PROJ-99", uids)
        self.assertEqual(uids["jira:PROJ-99"].source_field, "body")

    def test_confluence_storage_deduplication_and_scan_plain_keys(self) -> None:
        # Same link multiple times
        storage = """
        <p><a href="/pages/viewpage.action?pageId=100">First Link</a></p>
        <p><a href="/pages/viewpage.action?pageId=100">Second Link</a></p>
        <p>Text mentioning PROJ-42 and PROJ-99 and UNKNOWN-1.</p>
        """
        # scan_plain_keys=False
        parsed = parse_confluence_storage(storage, self.ctx, scan_plain_keys=False)
        self.assertEqual(len(parsed.links), 1)
        self.assertEqual(parsed.links[0].uid, "confluence:100")
        self.assertEqual(parsed.links[0].anchor_text, "First Link")

        # scan_plain_keys=True
        parsed_scanned = parse_confluence_storage(
            storage, self.ctx, scan_plain_keys=True
        )
        uids = [l.uid for l in parsed_scanned.links]
        self.assertIn("confluence:100", uids)
        self.assertIn("jira:PROJ-42", uids)
        self.assertIn("jira:PROJ-99", uids)
        # UNKNOWN-1 filtered out because project_keys is [PROJ, AUTH, DEV]
        self.assertNotIn("jira:UNKNOWN-1", uids)

    def test_confluence_storage_script_style_ignored(self) -> None:
        storage = """
        <script>var x = 42; alert('ignore me');</script>
        <style>body { color: red; }</style>
        <p>Real readable content.</p>
        """
        parsed = parse_confluence_storage(storage, self.ctx)
        self.assertNotIn("alert", parsed.plain_text)
        self.assertNotIn("color: red", parsed.plain_text)
        self.assertIn("Real readable content.", parsed.plain_text)

    def test_confluence_storage_space_root_link_is_skipped_without_truncating(self) -> None:
        """Space root URLs are not documents: no link, but parsing continues."""
        storage = (
            "<p>Siehe <a href=\"https://confluence.intern/display/ENG\">Space ENG</a>.</p>"
            "<h2>Abschnitt 2</h2><p>Weiterer Inhalt.</p>"
        )
        parsed = parse_confluence_storage(storage, self.ctx)
        self.assertEqual(parsed.links, [])
        self.assertIn("Space ENG", parsed.plain_text)
        # Der Rest des Dokuments darf nicht abgeschnitten werden
        self.assertIn("Abschnitt 2", [h for h, _ in parsed.sections])
        self.assertIn("Weiterer Inhalt.", parsed.plain_text)

    def test_adf_space_root_link_is_skipped(self) -> None:
        doc = {
            "type": "doc",
            "content": [{
                "type": "paragraph",
                "content": [{
                    "type": "text", "text": "Space ENG",
                    "marks": [{"type": "link",
                               "attrs": {"href": "https://confluence.intern/spaces/ENG"}}],
                }],
            }],
        }
        parsed = adf_to_content(doc, self.ctx, source_type="jira")
        self.assertEqual(parsed.links, [])
        self.assertIn("Space ENG", parsed.plain_text)

    def test_jira_wiki_space_root_link_is_skipped(self) -> None:
        parsed = jira_wiki_to_content(
            "Siehe [Space ENG|https://confluence.intern/display/ENG] und "
            "[PROJ-1|https://jira.intern/browse/PROJ-1].",
            self.ctx,
        )
        uids = [lk.uid for lk in parsed.links]
        self.assertNotIn("space:ENG", uids)
        self.assertIn("jira:PROJ-1", uids)

    # --- ADF Tests ---

    def test_adf_fixture(self) -> None:
        fixture_path = FIXTURES_DIR / "jira_adf.json"
        adf = json.loads(fixture_path.read_text(encoding="utf-8"))

        parsed = adf_to_content(adf, self.ctx, source_type="jira")

        # Sections
        headings = [h for h, _ in parsed.sections]
        self.assertIn("", headings)
        self.assertIn("Reproduction Steps", headings)
        self.assertIn("Smart Links and Cards", headings)
        self.assertIn("Code Snippet", headings)
        self.assertIn("Status Table", headings)

        # Mentions in text
        self.assertIn("@John Doe", parsed.plain_text)

        # Lists in text
        steps_sec = next(t for h, t in parsed.sections if h == "Reproduction Steps")
        self.assertIn("1. Navigate to the portal", steps_sec)
        self.assertIn("2. Click Related Confluence Page", steps_sec)

        # Code block
        code_sec = next(t for h, t in parsed.sections if h == "Code Snippet")
        self.assertIn("assert login() is False", code_sec)

        # Table
        table_sec = next(t for h, t in parsed.sections if h == "Status Table")
        self.assertIn("Browser | Result", table_sec)
        self.assertIn("Chrome | Failed", table_sec)

        # Unknown custom node didn't crash and extracted content
        self.assertIn("Nested inside custom extension node.", parsed.plain_text)

        # Links
        uids = {l.uid: l for l in parsed.links}
        self.assertIn("confluence:102030", uids)
        self.assertEqual(uids["confluence:102030"].anchor_text, "Related Confluence Page")

        self.assertIn("jira:AUTH-10", uids)
        self.assertIn("jira:AUTH-1", uids)

    def test_adf_malformed_and_none(self) -> None:
        res_none = adf_to_content(None, self.ctx)
        self.assertEqual(res_none.sections, [("", "")])
        self.assertEqual(res_none.links, [])
        self.assertEqual(res_none.plain_text, "")

        res_empty = adf_to_content({}, self.ctx)
        self.assertEqual(res_empty.sections, [("", "")])

    # --- Jira Wiki Markup Tests ---

    def test_jira_wiki_markup(self) -> None:
        markup = """
        h2. Summary
        Check [Architecture Guide|https://confluence.intern/spaces/ENG/pages/123/arch] or [PROJ-42].
        Also direct URL [https://jira.intern/browse/DEV-55].
        Bare URL: https://confluence.intern/display/DEV/Overview
        User mention [~johndoe] should be stripped.

        h3. Code Block
        {code:python}
        def greet():
            print("hello")
        {code}

        h3. Table
        ||Col A||Col B||
        |Val 1|Val 2|

        *bold text* and _italic text_ and {{monospace}}.
        """
        parsed = jira_wiki_to_content(markup, self.ctx)

        # Sections
        headings = [h for h, _ in parsed.sections]
        self.assertIn("Summary", headings)
        self.assertIn("Code Block", headings)
        self.assertIn("Table", headings)

        # Stripped user mention
        self.assertNotIn("[~johndoe]", parsed.plain_text)
        self.assertNotIn("johndoe", parsed.plain_text)

        # Code block contents preserved, delimiters stripped
        self.assertNotIn("{code", parsed.plain_text)
        self.assertIn('print("hello")', parsed.plain_text)

        # Table formatting
        table_sec = next(t for h, t in parsed.sections if h == "Table")
        self.assertIn("Col A | Col B", table_sec)
        self.assertIn("Val 1 | Val 2", table_sec)

        # Inline style markers stripped
        self.assertIn("bold text", parsed.plain_text)
        self.assertNotIn("*bold text*", parsed.plain_text)
        self.assertIn("italic text", parsed.plain_text)
        self.assertNotIn("_italic text_", parsed.plain_text)

        # Links
        uids = {l.uid: l for l in parsed.links}
        self.assertIn("confluence:123", uids)
        self.assertEqual(uids["confluence:123"].anchor_text, "Architecture Guide")

        self.assertIn("jira:PROJ-42", uids)
        self.assertIn("jira:DEV-55", uids)
        self.assertIn("confluence:unresolved:DEV:Overview", uids)

    def test_jira_wiki_none_and_empty(self) -> None:
        res_none = jira_wiki_to_content(None, self.ctx)
        self.assertEqual(res_none.sections, [("", "")])
        self.assertEqual(res_none.links, [])
        self.assertEqual(res_none.plain_text, "")

        res_empty = jira_wiki_to_content("", self.ctx)
        self.assertEqual(res_empty.sections, [("", "")])

    # --- Chunking Tests ---

    def test_chunk_sections_overlap_and_splitting(self) -> None:
        # Create text with several sentences to test splitting and overlap
        sentences = [
            f"Sentence {i} gives important details about system operation."
            for i in range(1, 30)
        ]
        full_text = " ".join(sentences)
        sections = [("Operations", full_text)]

        chunks = chunk_sections(
            "Doc Title",
            sections,
            target_chars=200,
            overlap_chars=50,
            min_chars=40,
        )

        self.assertGreater(len(chunks), 1)
        for i, c in enumerate(chunks):
            self.assertEqual(c.heading, "Operations")
            self.assertGreater(len(c.body), 0)
            if i > 0:
                # Overlap: words from end of chunk i-1 should appear at start of chunk i
                prev_words = chunks[i - 1].body.split()[-3:]
                curr_words = c.body.split()[:5]
                # At least one word from the previous tail should overlap
                overlap_found = any(w in curr_words for w in prev_words)
                self.assertTrue(
                    overlap_found,
                    f"Expected overlap between chunk {i-1} and {i}",
                )

    def test_chunk_sections_min_chars_merging(self) -> None:
        # Section with main content and tiny trailing leftover
        sec_text = "Word " * 50  # ~250 chars
        chunks = chunk_sections(
            "Doc",
            [("Sec1", sec_text)],
            target_chars=200,
            overlap_chars=30,
            min_chars=80,
        )
        # Leftover is ~50 chars, smaller than min_chars=80 -> must be merged into 1 chunk
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].heading, "Sec1")

    def test_chunk_sections_long_text_without_spaces(self) -> None:
        # "lange Texte ohne Leerzeichen"
        long_unbroken = "A" * 3000
        sections = [("Unbroken", long_unbroken)]

        chunks = chunk_sections(
            "Doc",
            sections,
            target_chars=500,
            overlap_chars=50,
            min_chars=80,
        )

        self.assertGreater(len(chunks), 1)
        # Make sure total content is preserved and no crash / infinite loop occurs
        total_len = sum(len(c.body) for c in chunks)
        self.assertGreaterEqual(total_len, 3000)

    def test_chunk_sections_empty_sections_fallback(self) -> None:
        # Empty sections list
        chunks1 = chunk_sections("Default Fallback Title", [])
        self.assertEqual(chunks1, [Chunk(heading="", body="Default Fallback Title")])

        # Sections with empty or whitespace text
        chunks2 = chunk_sections(
            "Another Title",
            [("Section 1", ""), ("Section 2", "   \n\t  ")],
        )
        self.assertEqual(chunks2, [Chunk(heading="", body="Another Title")])

    # --- Truncate Text Tests ---

    def test_truncate_text(self) -> None:
        self.assertEqual(truncate_text("Short", 10), ("Short", False))
        self.assertEqual(truncate_text("ExactLen10", 10), ("ExactLen10", False))
        self.assertEqual(truncate_text("LongerThanTen", 10), ("LongerThan", True))
        self.assertEqual(truncate_text("", 10), ("", False))
        self.assertEqual(truncate_text(None, 10), ("", False))


if __name__ == "__main__":
    unittest.main()
