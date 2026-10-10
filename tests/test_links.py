"""Unit tests for knowledge/links.py.

Covers:
- viewpage.action?pageId (absolute, relative, editpage)
- /spaces/X/pages/123/slug
- /display/SPACE/Page+Title (URL encoding, umlauts)
- Context-Path-Base
- Alias-Host
- Foreign host ignored
- Jira /browse/KEY and selectedIssue
- Cloud same host (longest match /wiki vs /)
- Attachments, anchors, invalid schemes ignored
- normalize_issue_key
- parse_start_page
- find_plain_issue_keys with and without project_keys filter
- confluence_page_url and jira_issue_url
"""

import unittest

from knowledge.links import (
    LinkContext,
    classify_url,
    confluence_page_url,
    find_plain_issue_keys,
    jira_issue_url,
    normalize_issue_key,
    parse_start_page,
)


class TestLinks(unittest.TestCase):
    def setUp(self) -> None:
        self.config_server = {
            "confluence": {
                "base_url": "https://confluence.intern",
                "aliases": ["https://wiki.intern", "http://confluence.local:8080"],
            },
            "jira": {
                "base_url": "https://jira.intern",
                "aliases": ["https://bugs.intern"],
                "project_keys": ["PROJ", "AUTH", "DEV"],
            },
        }
        self.ctx_server = LinkContext.from_config(self.config_server)

        # Context-path setup (e.g. intern.firma.de/confluence and intern.firma.de/jira)
        self.config_context_path = {
            "confluence": {
                "base_url": "https://intern.firma.de/confluence",
                "aliases": [],
            },
            "jira": {
                "base_url": "https://intern.firma.de/jira",
                "aliases": [],
            },
        }
        self.ctx_context_path = LinkContext.from_config(self.config_context_path)

        # Cloud setup: Confluence and Jira share host, Confluence has /wiki prefix
        self.config_cloud = {
            "confluence": {
                "base_url": "https://firma.atlassian.net/wiki",
                "aliases": [],
            },
            "jira": {
                "base_url": "https://firma.atlassian.net",
                "aliases": [],
            },
        }
        self.ctx_cloud = LinkContext.from_config(self.config_cloud)

    def test_normalize_issue_key(self) -> None:
        self.assertEqual(normalize_issue_key("PROJ-1"), "PROJ-1")
        self.assertEqual(normalize_issue_key("proj-123"), "PROJ-123")
        self.assertEqual(normalize_issue_key("  ABC_DEF-99  "), "ABC_DEF-99")
        # Invalid keys
        self.assertIsNone(normalize_issue_key(None))
        self.assertIsNone(normalize_issue_key(""))
        self.assertIsNone(normalize_issue_key("PROJ"))
        self.assertIsNone(normalize_issue_key("PROJ-"))
        self.assertIsNone(normalize_issue_key("123-PROJ"))
        self.assertIsNone(normalize_issue_key("-123"))
        self.assertIsNone(normalize_issue_key("PROJ-0"))  # number starts at 1

    def test_viewpage_action_page_id(self) -> None:
        # Absolute URL
        res = classify_url(
            "https://confluence.intern/pages/viewpage.action?pageId=12345",
            self.ctx_server,
        )
        self.assertIsNotNone(res)
        uid, norm = res
        self.assertEqual(uid, "confluence:12345")
        self.assertEqual(
            norm, "https://confluence.intern/pages/viewpage.action?pageId=12345"
        )

        # Edit page action
        res_edit = classify_url(
            "https://confluence.intern/pages/editpage.action?pageId=67890",
            self.ctx_server,
        )
        self.assertIsNotNone(res_edit)
        self.assertEqual(res_edit[0], "confluence:67890")

        # Relative URL resolved against confluence_base
        res_rel = classify_url(
            "/pages/viewpage.action?pageId=54321",
            self.ctx_server,
        )
        self.assertIsNotNone(res_rel)
        self.assertEqual(res_rel[0], "confluence:54321")
        self.assertEqual(
            res_rel[1], "https://confluence.intern/pages/viewpage.action?pageId=54321"
        )

    def test_spaces_pages_slug(self) -> None:
        # /spaces/SPACE/pages/123/slug
        res = classify_url(
            "https://confluence.intern/spaces/ENG/pages/98765/my-design-doc",
            self.ctx_server,
        )
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "confluence:98765")
        self.assertEqual(
            res[1],
            "https://confluence.intern/spaces/ENG/pages/98765/my-design-doc",
        )

        # Without slug
        res2 = classify_url(
            "https://confluence.intern/spaces/ENG/pages/98765",
            self.ctx_server,
        )
        self.assertIsNotNone(res2)
        self.assertEqual(res2[0], "confluence:98765")

        # Short /pages/123
        res3 = classify_url(
            "https://confluence.intern/pages/98765",
            self.ctx_server,
        )
        self.assertIsNotNone(res3)
        self.assertEqual(res3[0], "confluence:98765")

    def test_display_space_title_encoding_and_umlauts(self) -> None:
        # /display/SPACE/Page+Title
        res = classify_url(
            "https://confluence.intern/display/DEV/Page+Title",
            self.ctx_server,
        )
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "confluence:unresolved:DEV:Page Title")

        # Umlauts and URL encoding
        res_umlaut = classify_url(
            "https://confluence.intern/display/HR/M%C3%BCnchen+Bier%26Pretzel",
            self.ctx_server,
        )
        self.assertIsNotNone(res_umlaut)
        self.assertEqual(
            res_umlaut[0], "confluence:unresolved:HR:München Bier&Pretzel"
        )

    def test_context_path_base(self) -> None:
        # Confluence on /confluence
        c_res = classify_url(
            "https://intern.firma.de/confluence/display/DEV/Home",
            self.ctx_context_path,
        )
        self.assertIsNotNone(c_res)
        self.assertEqual(c_res[0], "confluence:unresolved:DEV:Home")

        # Jira on /jira
        j_res = classify_url(
            "https://intern.firma.de/jira/browse/KEY-123",
            self.ctx_context_path,
        )
        self.assertIsNotNone(j_res)
        self.assertEqual(j_res[0], "jira:KEY-123")

    def test_alias_hosts(self) -> None:
        # Wiki alias
        res_wiki = classify_url(
            "https://wiki.intern/pages/viewpage.action?pageId=1001",
            self.ctx_server,
        )
        self.assertIsNotNone(res_wiki)
        self.assertEqual(res_wiki[0], "confluence:1001")

        # Port alias
        res_port = classify_url(
            "http://confluence.local:8080/display/ENG/Architecture",
            self.ctx_server,
        )
        self.assertIsNotNone(res_port)
        self.assertEqual(res_port[0], "confluence:unresolved:ENG:Architecture")

        # Jira alias
        res_jira = classify_url(
            "https://bugs.intern/browse/DEV-99",
            self.ctx_server,
        )
        self.assertIsNotNone(res_jira)
        self.assertEqual(res_jira[0], "jira:DEV-99")

    def test_foreign_host_ignored(self) -> None:
        self.assertIsNone(classify_url("https://google.com", self.ctx_server))
        self.assertIsNone(
            classify_url("https://external.com/browse/PROJ-1", self.ctx_server)
        )
        self.assertIsNone(
            classify_url(
                "https://other-confluence.com/display/DEV/Home", self.ctx_server
            )
        )

    def test_jira_browse_and_selected_issue(self) -> None:
        # /browse/KEY
        res_browse = classify_url(
            "https://jira.intern/browse/PROJ-42",
            self.ctx_server,
        )
        self.assertIsNotNone(res_browse)
        self.assertEqual(res_browse[0], "jira:PROJ-42")

        # /projects/KEY/issues/KEY
        res_proj = classify_url(
            "https://jira.intern/projects/PROJ/issues/PROJ-42",
            self.ctx_server,
        )
        self.assertIsNotNone(res_proj)
        self.assertEqual(res_proj[0], "jira:PROJ-42")

        # selectedIssue query parameter
        res_selected = classify_url(
            "https://jira.intern/secure/RapidBoard.jspa?rapidView=1&selectedIssue=AUTH-10",
            self.ctx_server,
        )
        self.assertIsNotNone(res_selected)
        self.assertEqual(res_selected[0], "jira:AUTH-10")

    def test_cloud_same_host_longest_match(self) -> None:
        # Confluence on firma.atlassian.net/wiki
        c_res = classify_url(
            "https://firma.atlassian.net/wiki/spaces/SPACE/pages/5555/slug",
            self.ctx_cloud,
        )
        self.assertIsNotNone(c_res)
        self.assertEqual(c_res[0], "confluence:5555")

        c_disp = classify_url(
            "https://firma.atlassian.net/wiki/display/SPACE/Title",
            self.ctx_cloud,
        )
        self.assertIsNotNone(c_disp)
        self.assertEqual(c_disp[0], "confluence:unresolved:SPACE:Title")

        # Jira on firma.atlassian.net
        j_res = classify_url(
            "https://firma.atlassian.net/browse/CLOUD-99",
            self.ctx_cloud,
        )
        self.assertIsNotNone(j_res)
        self.assertEqual(j_res[0], "jira:CLOUD-99")

    def test_ignored_schemes_anchors_and_attachments(self) -> None:
        self.assertIsNone(classify_url("mailto:dev@firma.de", self.ctx_server))
        self.assertIsNone(classify_url("javascript:void(0)", self.ctx_server))
        self.assertIsNone(classify_url("#section-heading", self.ctx_server))
        self.assertIsNone(
            classify_url(
                "https://confluence.intern/download/attachments/123/doc.pdf",
                self.ctx_server,
            )
        )
        self.assertIsNone(classify_url("", self.ctx_server))

    def test_parse_start_page(self) -> None:
        # Digits
        self.assertEqual(
            parse_start_page("12345", self.ctx_server), "confluence:12345"
        )
        self.assertEqual(
            parse_start_page("  999  ", self.ctx_server), "confluence:999"
        )

        # UID as-is
        self.assertEqual(
            parse_start_page("confluence:42", self.ctx_server), "confluence:42"
        )

        # Confluence URL
        self.assertEqual(
            parse_start_page(
                "https://confluence.intern/pages/viewpage.action?pageId=777",
                self.ctx_server,
            ),
            "confluence:777",
        )

        # Invalid
        self.assertIsNone(parse_start_page("not-a-page", self.ctx_server))
        self.assertIsNone(parse_start_page("https://google.com", self.ctx_server))
        self.assertIsNone(parse_start_page(None, self.ctx_server))

    def test_canonical_urls(self) -> None:
        self.assertEqual(
            confluence_page_url(self.ctx_server, "123"),
            "https://confluence.intern/pages/viewpage.action?pageId=123",
        )
        self.assertEqual(
            jira_issue_url(self.ctx_server, "PROJ-1"),
            "https://jira.intern/browse/PROJ-1",
        )

    def test_find_plain_issue_keys_with_project_filter(self) -> None:
        text = "Check PROJ-10 and AUTH-20 and OTHER-30, then re-check PROJ-10 and DEV-1."
        # ctx_server has project_keys: PROJ, AUTH, DEV. OTHER should be filtered out.
        keys = find_plain_issue_keys(text, self.ctx_server)
        self.assertEqual(keys, ["PROJ-10", "AUTH-20", "DEV-1"])

    def test_find_plain_issue_keys_without_filter(self) -> None:
        # Context without project_keys allows any valid key
        cfg = {"jira": {"base_url": "https://jira.intern"}}
        ctx = LinkContext.from_config(cfg)
        text = "Keys: FOO-1, BAR-2, and FOO-1 again, plus XYZ-99."
        keys = find_plain_issue_keys(text, ctx)
        self.assertEqual(keys, ["FOO-1", "BAR-2", "XYZ-99"])


if __name__ == "__main__":
    unittest.main()
