"""Text parsing, link extraction, and chunking for Confluence and Jira content.

Python standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
import html.parser
import logging
import re
from typing import Any

from knowledge.links import (
    LinkContext,
    classify_document_url,
    find_plain_issue_keys,
    jira_issue_url,
    normalize_issue_key,
)
from knowledge.models import (
    Chunk,
    LinkTarget,
    confluence_uid,
    edge_kind_for,
    jira_uid,
    unresolved_page_uid,
)

logger = logging.getLogger(__name__)


@dataclass
class ParsedContent:
    """Result of content parsing: sections, extracted links, and plain text."""

    sections: list[tuple[str, str]]
    links: list[LinkTarget]
    plain_text: str


def _clean_text(text: str) -> str:
    """Normalize whitespace per line and collapse multiple blank lines."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        lines.append(line)
    result: list[str] = []
    prev_empty = True
    for line in lines:
        if not line:
            if not prev_empty:
                result.append("")
                prev_empty = True
        else:
            result.append(line)
            prev_empty = False
    return "\n".join(result).strip()


def _build_plain_text(sections: list[tuple[str, str]]) -> str:
    """Combine sections into a single readable text string."""
    parts = []
    for h, t in sections:
        h_clean = h.strip()
        t_clean = t.strip()
        if h_clean and t_clean:
            parts.append(f"{h_clean}\n{t_clean}")
        elif h_clean:
            parts.append(h_clean)
        elif t_clean:
            parts.append(t_clean)
    return "\n\n".join(parts)


# --- Confluence Storage Format Parser ---


class _ConfluenceStorageHTMLParser(html.parser.HTMLParser):
    """HTMLParser for Confluence Storage Format XML/XHTML."""

    def __init__(self, ctx: LinkContext, space_key: str = "") -> None:
        super().__init__()
        self.ctx = ctx
        self.default_space = space_key.strip().upper()

        self.sections: list[tuple[str, str]] = []
        self.current_heading = ""
        self.buf: list[str] = []

        self.in_heading = False
        self.heading_buf: list[str] = []

        self.ignore_depth = 0
        self.param_depth = 0
        self.current_param_name = ""
        self.current_param_buf: list[str] = []

        self.in_macro = False
        self.macro_name = ""
        self.macro_params: dict[str, str] = {}

        self.in_ac_link = False
        self.ac_link_target: dict[str, str] = {}
        self.in_link_body = False
        self.link_body_buf: list[str] = []

        self.current_a_href: str | None = None
        self.current_a_text: list[str] = []

        self.links: list[LinkTarget] = []
        self.seen_links: set[tuple[str, str]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict: dict[str, str] = {
            k: (v or "") for k, v in attrs if k is not None
        }

        if tag in ("script", "style"):
            self.ignore_depth += 1
            return
        if self.ignore_depth > 0:
            return

        # Headings h1..h6
        if tag.startswith("h") and len(tag) == 2 and tag[1] in "123456":
            self._flush_section()
            self.in_heading = True
            self.heading_buf = []
            return

        # Confluence structured macros
        if tag == "ac:structured-macro":
            self.in_macro = True
            self.macro_name = attr_dict.get("ac:name", "")
            self.macro_params = {}
            return

        if tag == "ac:parameter":
            self.param_depth += 1
            self.current_param_name = attr_dict.get("ac:name", "")
            self.current_param_buf = []
            return

        # Confluence ac:link
        if tag == "ac:link":
            self.in_ac_link = True
            self.ac_link_target = {}
            self.link_body_buf = []
            return

        if self.in_ac_link and tag == "ri:page":
            if "ri:content-title" in attr_dict:
                self.ac_link_target["content_title"] = attr_dict["ri:content-title"]
            if "ri:space-key" in attr_dict:
                self.ac_link_target["space_key"] = attr_dict["ri:space-key"]
            if "ri:content-id" in attr_dict:
                self.ac_link_target["content_id"] = attr_dict["ri:content-id"]
            return

        if self.in_ac_link and tag == "ri:content-entity":
            if "ri:content-id" in attr_dict:
                self.ac_link_target["content_id"] = attr_dict["ri:content-id"]
            return

        if self.in_ac_link and tag in ("ac:link-body", "ac:plain-text-link-body"):
            self.in_link_body = True
            return

        # HTML anchor <a>
        if tag == "a":
            self.current_a_href = attr_dict.get("href")
            self.current_a_text = []
            return

        # Block-level tags produce spacing
        if tag in ("p", "div", "li", "tr", "blockquote", "pre", "table", "ul", "ol"):
            self.buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self.ignore_depth > 0:
                self.ignore_depth -= 1
            return
        if self.ignore_depth > 0:
            return

        if tag.startswith("h") and len(tag) == 2 and tag[1] in "123456":
            self.in_heading = False
            self.current_heading = " ".join("".join(self.heading_buf).split())
            return

        if tag == "ac:parameter":
            if self.param_depth > 0:
                self.param_depth -= 1
            if self.in_macro:
                self.macro_params[self.current_param_name] = "".join(
                    self.current_param_buf
                ).strip()
            return

        if tag == "ac:structured-macro":
            if self.macro_name == "jira":
                key = self.macro_params.get("key")
                if key:
                    norm_k = normalize_issue_key(key)
                    if norm_k:
                        uid = jira_uid(norm_k)
                        kind = edge_kind_for("confluence", uid)
                        ev_url = jira_issue_url(self.ctx, norm_k) if self.ctx.jira_base else ""
                        self._add_link(
                            uid=uid,
                            kind=kind,
                            source_field="jira-macro",
                            anchor_text=norm_k,
                            evidence_url=ev_url,
                            title_hint=norm_k,
                        )
                        self.buf.append(f" {norm_k} ")
            self.in_macro = False
            self.macro_name = ""
            self.macro_params = {}
            return

        if self.in_ac_link and tag in ("ac:link-body", "ac:plain-text-link-body"):
            self.in_link_body = False
            return

        if tag == "ac:link":
            cid = self.ac_link_target.get("content_id")
            title = self.ac_link_target.get("content_title")
            space = self.ac_link_target.get("space_key") or self.default_space
            body_txt = " ".join("".join(self.link_body_buf).split())

            if cid and cid.strip():
                uid = confluence_uid(cid.strip())
                kind = edge_kind_for("confluence", uid)
                self._add_link(
                    uid=uid,
                    kind=kind,
                    source_field="ac:link",
                    anchor_text=body_txt or title or "",
                    evidence_url="",
                    title_hint=title or "",
                )
            elif title and title.strip():
                uid = unresolved_page_uid(space, title.strip())
                kind = edge_kind_for("confluence", uid)
                self._add_link(
                    uid=uid,
                    kind=kind,
                    source_field="ac:link",
                    anchor_text=body_txt or title.strip(),
                    evidence_url="",
                    title_hint=title.strip(),
                )

            # If no link body was present, emit title to readable text
            if not body_txt and title:
                self.buf.append(f" {title} ")

            self.in_ac_link = False
            self.ac_link_target = {}
            self.link_body_buf = []
            return

        if tag == "a":
            if self.current_a_href:
                classified = classify_document_url(self.current_a_href, self.ctx)
                if classified:
                    uid, norm_url = classified
                    kind = edge_kind_for("confluence", uid)
                    anchor = " ".join("".join(self.current_a_text).split())
                    self._add_link(
                        uid=uid,
                        kind=kind,
                        source_field="body",
                        anchor_text=anchor,
                        evidence_url=norm_url,
                        title_hint=anchor,
                    )
            self.current_a_href = None
            self.current_a_text = []
            return

        if tag in ("p", "div", "li", "blockquote", "pre"):
            self.buf.append("\n")
        elif tag == "br":
            self.buf.append("\n")
        elif tag in ("td", "th"):
            self.buf.append(" | ")
        elif tag == "tr":
            self.buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self.ignore_depth > 0:
            return
        if self.in_heading:
            self.heading_buf.append(data)
            if self.current_a_href is not None:
                self.current_a_text.append(data)
            return

        if self.param_depth > 0:
            self.current_param_buf.append(data)
            return

        if self.current_a_href is not None:
            self.current_a_text.append(data)

        if self.in_link_body:
            self.link_body_buf.append(data)

        self.buf.append(data)

    def unknown_decl(self, data: str) -> None:
        # Handle CDATA in <ac:plain-text-body> etc.
        if data.upper().startswith("CDATA["):
            content = data[6:]
            self.handle_data(content)

    def _flush_section(self) -> None:
        text = _clean_text("".join(self.buf))
        if self.current_heading or text:
            self.sections.append((self.current_heading, text))
        self.buf = []
        self.current_heading = ""

    def _add_link(
        self,
        uid: str,
        kind: str,
        source_field: str,
        anchor_text: str,
        evidence_url: str,
        title_hint: str,
    ) -> None:
        key = (uid, kind)
        if key not in self.seen_links:
            self.seen_links.add(key)
            self.links.append(
                LinkTarget(
                    uid=uid,
                    kind=kind,
                    relation_label="",
                    source_field=source_field,
                    anchor_text=anchor_text,
                    evidence_url=evidence_url,
                    title_hint=title_hint,
                )
            )

    def finish(self) -> None:
        self._flush_section()
        if not self.sections:
            self.sections.append(("", ""))


def parse_confluence_storage(
    storage: str,
    ctx: LinkContext,
    *,
    space_key: str = "",
    scan_plain_keys: bool = False,
) -> ParsedContent:
    """Parse Confluence storage format XHTML into sections, links, and plain text."""
    if not storage or not isinstance(storage, str):
        return ParsedContent(sections=[("", "")], links=[], plain_text="")

    parser = _ConfluenceStorageHTMLParser(ctx, space_key=space_key)
    try:
        parser.feed(storage)
    except Exception as exc:
        # Never crash on malformed input, but do not hide the truncation either.
        logger.warning("Confluence-Storage nur teilweise geparst (%s) – Rest übersprungen", exc)
    parser.finish()

    plain_text = _build_plain_text(parser.sections)

    if scan_plain_keys:
        for k in find_plain_issue_keys(plain_text, ctx):
            uid = jira_uid(k)
            kind = edge_kind_for("confluence", uid)
            ev_url = jira_issue_url(ctx, k) if ctx.jira_base else ""
            parser._add_link(
                uid=uid,
                kind=kind,
                source_field="plain_text",
                anchor_text=k,
                evidence_url=ev_url,
                title_hint=k,
            )

    return ParsedContent(
        sections=parser.sections,
        links=parser.links,
        plain_text=plain_text,
    )


# --- Atlassian Document Format (ADF) Parser ---


def adf_to_content(
    adf: dict | None,
    ctx: LinkContext,
    *,
    source_type: str = "jira",
    scan_plain_keys: bool = False,
) -> ParsedContent:
    """Convert Atlassian Document Format (ADF) JSON structure into sections, links, and plain text.

    Recursive, safe against unknown nodes, and handles Jira Cloud v3 representations.
    """
    if not adf or not isinstance(adf, dict):
        return ParsedContent(sections=[("", "")], links=[], plain_text="")

    sections: list[tuple[str, str]] = []
    links: list[LinkTarget] = []
    seen_links: set[tuple[str, str]] = set()

    current_heading = ""
    current_lines: list[str] = []

    def _add_link(
        uid: str,
        kind: str,
        source_field: str,
        anchor_text: str,
        evidence_url: str,
        title_hint: str = "",
    ) -> None:
        key = (uid, kind)
        if key not in seen_links:
            seen_links.add(key)
            links.append(
                LinkTarget(
                    uid=uid,
                    kind=kind,
                    relation_label="",
                    source_field=source_field,
                    anchor_text=anchor_text,
                    evidence_url=evidence_url,
                    title_hint=title_hint,
                )
            )

    def _extract_inline(node: Any) -> str:
        """Extract inline text and record any marks or card links."""
        if not isinstance(node, dict):
            return ""

        ntype = node.get("type")

        if ntype == "text":
            text = node.get("text", "")
            for mark in node.get("marks") or []:
                if isinstance(mark, dict) and mark.get("type") == "link":
                    href = (mark.get("attrs") or {}).get("href")
                    if href:
                        classified = classify_document_url(href, ctx)
                        if classified:
                            uid, norm_url = classified
                            kind = edge_kind_for(source_type, uid)
                            _add_link(
                                uid=uid,
                                kind=kind,
                                source_field="body",
                                anchor_text=text,
                                evidence_url=norm_url,
                                title_hint=text,
                            )
            return text

        if ntype == "hardBreak":
            return "\n"

        if ntype == "mention":
            attrs = node.get("attrs") or {}
            return attrs.get("text") or ""

        if ntype in ("inlineCard", "blockCard", "embedCard"):
            attrs = node.get("attrs") or {}
            url = attrs.get("url", "")
            if url:
                classified = classify_document_url(url, ctx)
                if classified:
                    uid, norm_url = classified
                    kind = edge_kind_for(source_type, uid)
                    _add_link(
                        uid=uid,
                        kind=kind,
                        source_field="body",
                        anchor_text=url,
                        evidence_url=norm_url,
                        title_hint="",
                    )
            return url

        if ntype == "emoji":
            attrs = node.get("attrs") or {}
            return attrs.get("text") or ""

        # Recurse on content if any; block-level children are separated so that
        # e.g. two paragraphs inside a listItem do not glue words together.
        children = node.get("content") or []
        if isinstance(children, list):
            sep = "" if ntype in ("paragraph", "heading", "codeBlock") else " "
            return sep.join(t for t in (_extract_inline(c) for c in children) if t)

        return ""

    def _flush_section() -> None:
        nonlocal current_heading, current_lines
        text = _clean_text("\n".join(current_lines))
        if current_heading or text:
            sections.append((current_heading, text))
        current_heading = ""
        current_lines = []

    def _process_block(block: Any) -> None:
        nonlocal current_heading, current_lines
        if not isinstance(block, dict):
            return

        btype = block.get("type")

        if btype == "heading":
            _flush_section()
            h_text = _extract_inline(block)
            current_heading = " ".join(h_text.split())
            return

        if btype == "paragraph":
            p_text = _extract_inline(block)
            if p_text:
                current_lines.append(p_text)
            return

        if btype == "bulletList":
            items = block.get("content") or []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        i_text = _extract_inline(item).strip()
                        if i_text:
                            current_lines.append(f"- {i_text}")
            return

        if btype == "orderedList":
            items = block.get("content") or []
            if isinstance(items, list):
                for idx, item in enumerate(items, start=1):
                    if isinstance(item, dict):
                        i_text = _extract_inline(item).strip()
                        if i_text:
                            current_lines.append(f"{idx}. {i_text}")
            return

        if btype == "codeBlock":
            code_text = _extract_inline(block)
            if code_text:
                current_lines.append(code_text)
            return

        if btype == "table":
            rows = block.get("content") or []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("type") == "tableRow":
                        cells = row.get("content") or []
                        cell_texts = []
                        if isinstance(cells, list):
                            for cell in cells:
                                if isinstance(cell, dict):
                                    c_text = " ".join(_extract_inline(cell).split())
                                    cell_texts.append(c_text)
                        if cell_texts:
                            current_lines.append(" | ".join(cell_texts))
            return

        if btype in ("inlineCard", "blockCard", "embedCard"):
            card_text = _extract_inline(block)
            if card_text:
                current_lines.append(card_text)
            return

        if btype == "rule":
            current_lines.append("---")
            return

        # Unknown container or doc node: recurse into children
        content = block.get("content")
        if isinstance(content, list):
            for child in content:
                _process_block(child)
        else:
            # Fallback inline extraction
            fallback = _extract_inline(block)
            if fallback:
                current_lines.append(fallback)

    root_content = adf.get("content")
    if isinstance(root_content, list):
        for blk in root_content:
            _process_block(blk)
    else:
        _process_block(adf)

    _flush_section()
    if not sections:
        sections.append(("", ""))

    plain_text = _build_plain_text(sections)

    if scan_plain_keys:
        for k in find_plain_issue_keys(plain_text, ctx):
            uid = jira_uid(k)
            kind = edge_kind_for(source_type, uid)
            ev_url = jira_issue_url(ctx, k) if ctx.jira_base else ""
            _add_link(
                uid=uid,
                kind=kind,
                source_field="plain_text",
                anchor_text=k,
                evidence_url=ev_url,
                title_hint=k,
            )

    return ParsedContent(
        sections=sections,
        links=links,
        plain_text=plain_text,
    )


# --- Jira Wiki Markup Parser ---


def jira_wiki_to_content(
    text: str | None,
    ctx: LinkContext,
    *,
    scan_plain_keys: bool = False,
) -> ParsedContent:
    """Parse Jira Server/DC wiki markup into sections, links, and plain text."""
    if not text or not isinstance(text, str):
        return ParsedContent(sections=[("", "")], links=[], plain_text="")

    raw = text

    # Strip code / noformat / quote / panel / color macros but keep inner content
    raw = re.sub(r"\{code(?::[^\}]*)?\}(.*?)\{code\}", r"\1", raw, flags=re.DOTALL)
    raw = re.sub(r"\{noformat\}(.*?)\{noformat\}", r"\1", raw, flags=re.DOTALL)
    raw = re.sub(r"\{quote\}(.*?)\{quote\}", r"\1", raw, flags=re.DOTALL)
    raw = re.sub(r"\{panel(?::[^\}]*)?\}(.*?)\{panel\}", r"\1", raw, flags=re.DOTALL)
    raw = re.sub(r"\{color(?::[^\}]*)?\}(.*?)\{color\}", r"\1", raw, flags=re.DOTALL)

    # Strip user mentions [~username]
    raw = re.sub(r"\[~[^\]]+\]", "", raw)

    links: list[LinkTarget] = []
    seen_links: set[tuple[str, str]] = set()

    def _add_link(
        uid: str,
        kind: str,
        source_field: str,
        anchor_text: str,
        evidence_url: str,
        title_hint: str = "",
    ) -> None:
        key = (uid, kind)
        if key not in seen_links:
            seen_links.add(key)
            links.append(
                LinkTarget(
                    uid=uid,
                    kind=kind,
                    relation_label="",
                    source_field=source_field,
                    anchor_text=anchor_text,
                    evidence_url=evidence_url,
                    title_hint=title_hint,
                )
            )

    # Handle wiki links [text|url] or [url]
    def _link_replacer(match: re.Match) -> str:
        inner = match.group(1).strip()
        if "|" in inner:
            anchor, url = inner.split("|", 1)
            anchor = anchor.strip()
            url = url.strip()
        else:
            anchor = inner
            url = inner

        classified = classify_document_url(url, ctx)
        if classified:
            uid, norm_url = classified
            kind = edge_kind_for("jira", uid)
            _add_link(
                uid=uid,
                kind=kind,
                source_field="body",
                anchor_text=anchor,
                evidence_url=norm_url,
                title_hint=anchor,
            )
            return anchor

        k = normalize_issue_key(url)
        if k:
            uid = jira_uid(k)
            kind = edge_kind_for("jira", uid)
            ev_url = jira_issue_url(ctx, k) if ctx.jira_base else ""
            _add_link(
                uid=uid,
                kind=kind,
                source_field="body",
                anchor_text=anchor,
                evidence_url=ev_url,
                title_hint=k,
            )
            return anchor

        # If it wasn't a valid link or issue key, keep original brackets if no pipe
        if "|" in inner:
            return anchor
        return match.group(0)

    raw = re.sub(r"\[([^\]]+)\]", _link_replacer, raw)

    # Scan for bare URLs
    for m in re.finditer(r"https?://[^\s<>\[\]\"\'\(\)]+", raw):
        url = m.group(0).rstrip(".,;:!?")
        classified = classify_document_url(url, ctx)
        if classified:
            uid, norm_url = classified
            kind = edge_kind_for("jira", uid)
            _add_link(
                uid=uid,
                kind=kind,
                source_field="body",
                anchor_text=url,
                evidence_url=norm_url,
                title_hint="",
            )

    # Strip inline styles: *bold*, _italic_, +underline+, {{monospace}}
    raw = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", raw)
    raw = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", raw)
    raw = re.sub(r"(?<!\w)\+([^\+\n]+)\+(?!\w)", r"\1", raw)
    raw = re.sub(r"\{\{([^\}\n]+)\}\}", r"\1", raw)

    # Parse into sections and format tables
    sections: list[tuple[str, str]] = []
    curr_heading = ""
    curr_lines: list[str] = []

    for line in raw.splitlines():
        line_s = line.strip()
        hm = re.match(r"^h[1-6]\.\s*(.*)$", line_s)
        if hm:
            stext = _clean_text("\n".join(curr_lines))
            if curr_heading or stext:
                sections.append((curr_heading, stext))
            curr_heading = " ".join(hm.group(1).split())
            curr_lines = []
            continue

        # Tables: ||Header||Header|| or |Cell|Cell|
        if line_s.startswith("||") and line_s.endswith("||"):
            cells = [c.strip() for c in line_s.strip("|").split("||")]
            curr_lines.append(" | ".join(c for c in cells if c))
            continue
        elif line_s.startswith("|") and line_s.endswith("|"):
            cells = [c.strip() for c in line_s.strip("|").split("|")]
            curr_lines.append(" | ".join(c for c in cells if c))
            continue

        curr_lines.append(line)

    stext = _clean_text("\n".join(curr_lines))
    if curr_heading or stext or not sections:
        sections.append((curr_heading, stext))

    plain_text = _build_plain_text(sections)

    if scan_plain_keys:
        for k in find_plain_issue_keys(plain_text, ctx):
            uid = jira_uid(k)
            kind = edge_kind_for("jira", uid)
            ev_url = jira_issue_url(ctx, k) if ctx.jira_base else ""
            _add_link(
                uid=uid,
                kind=kind,
                source_field="plain_text",
                anchor_text=k,
                evidence_url=ev_url,
                title_hint=k,
            )

    return ParsedContent(
        sections=sections,
        links=links,
        plain_text=plain_text,
    )


# --- Chunking and Helpers ---


def _split_section_text(
    text: str,
    target_chars: int,
    overlap_chars: int,
    min_chars: int,
) -> list[str]:
    """Split section text into ~target_chars chunks with overlap."""
    text = text.strip()
    if not text:
        return []

    if len(text) <= target_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    overlap = ""

    while start < len(text):
        avail = target_chars - (len(overlap) + 1 if overlap else 0)
        # Guarantee progress even with overlap >= target or min_chars == 0.
        avail = max(avail, min_chars, target_chars // 2, 1)
        target_end = start + avail

        if target_end >= len(text):
            chunk = (f"{overlap} {text[start:]}").strip() if overlap else text[start:]
            chunks.append(chunk)
            break

        # If remaining text after target_end is shorter than min_chars, include it
        if len(text) - target_end < min_chars:
            chunk = (f"{overlap} {text[start:]}").strip() if overlap else text[start:]
            chunks.append(chunk)
            break

        # Find best split boundary near target_end
        best_pos = -1

        # 1. Paragraph boundary (\n\n)
        pos = text.rfind("\n\n", start + min_chars, target_end)
        if pos != -1:
            best_pos = pos + 2
        else:
            # 2. Line boundary (\n)
            pos = text.rfind("\n", start + min_chars, target_end)
            if pos != -1:
                best_pos = pos + 1
            else:
                # 3. Sentence boundary ([.!?] followed by whitespace)
                matches = list(re.finditer(r"[.!?]\s+", text[start:target_end]))
                if matches and matches[-1].end() >= min_chars:
                    best_pos = start + matches[-1].end()
                else:
                    # 4. Word boundary (space)
                    pos = text.rfind(" ", start + min_chars, target_end)
                    if pos != -1:
                        best_pos = pos + 1
                    else:
                        # 5. Hard break for long texts without spaces
                        best_pos = target_end

        end = best_pos if best_pos > start else target_end
        raw_slice = text[start:end].strip()
        chunk = (f"{overlap} {raw_slice}").strip() if overlap else raw_slice
        chunks.append(chunk)

        # Compute overlap for subsequent chunk
        if overlap_chars > 0 and len(chunk) > overlap_chars:
            tail = chunk[-overlap_chars:]
            sp = tail.find(" ")
            if sp != -1 and sp < len(tail) - 1:
                overlap = tail[sp + 1:].strip()
            else:
                overlap = tail.strip()
        else:
            overlap = ""

        start = end

    return chunks


def chunk_sections(
    title: str,
    sections: list[tuple[str, str]],
    *,
    target_chars: int = 1200,
    overlap_chars: int = 150,
    min_chars: int = 80,
) -> list[Chunk]:
    """Split sections into ~target_chars chunks with overlap and min_chars merging."""
    chunks: list[Chunk] = []

    for heading, text in sections:
        clean_t = text.strip()
        if not clean_t:
            continue

        sub_bodies = _split_section_text(
            clean_t,
            target_chars=target_chars,
            overlap_chars=overlap_chars,
            min_chars=min_chars,
        )

        for b in sub_bodies:
            chunks.append(Chunk(heading=heading, body=b))

    # Merge short chunks (< min_chars) into neighbours
    if len(chunks) > 1:
        merged: list[Chunk] = []
        for c in chunks:
            if not merged:
                merged.append(c)
                continue
            prev = merged[-1]
            if len(c.body) < min_chars:
                # Merge current into previous
                extra = (
                    f"\n\n{c.heading}\n{c.body}".strip()
                    if c.heading and c.heading != prev.heading
                    else f"\n\n{c.body}"
                )
                merged[-1] = Chunk(
                    heading=prev.heading,
                    body=(prev.body + extra).strip(),
                )
            elif len(prev.body) < min_chars:
                # Merge previous into current
                extra = (
                    f"{prev.heading}\n{prev.body}\n\n".strip()
                    if prev.heading and prev.heading != c.heading
                    else f"{prev.body}\n\n"
                )
                merged[-1] = Chunk(
                    heading=c.heading or prev.heading,
                    body=(extra + c.body).strip(),
                )
            else:
                merged.append(c)
        chunks = merged

    # Filter out empty bodies
    chunks = [c for c in chunks if c.body.strip()]

    # If nothing was generated, return fallback chunk with document title
    if not chunks:
        chunks = [Chunk(heading="", body=title)]

    return chunks


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate text to max_chars characters if needed.

    Returns:
        (truncated_text, is_truncated)
    """
    if text is None:
        return ("", False)
    if len(text) <= max_chars:
        return (text, False)
    return (text[:max_chars], True)
