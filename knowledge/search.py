from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

# Pattern for issue keys (e.g. ENG-42, PROJ_X-123) and general unicode word tokens
TOKEN_PATTERN = re.compile(r'([A-Za-z][A-Za-z0-9_]+-[1-9][0-9]*)|(\w+)', re.UNICODE)
ISSUE_KEY_PATTERN = re.compile(r'\b[A-Za-z][A-Za-z0-9_]+-[1-9][0-9]*\b')


def _build_fts_query(
    query: str, max_query_chars: int = 200
) -> tuple[str, set[str], list[str]]:
    """Build a safe FTS5 query string from user input.

    Returns:
        (fts_query, exact_issue_keys, raw_tokens)
    """
    query = (query or "")[:max_query_chars]
    tokens: list[tuple[str, bool]] = []
    exact_issue_keys: set[str] = set()
    raw_tokens: list[str] = []

    for m in TOKEN_PATTERN.finditer(query):
        issue, word = m.groups()
        if issue:
            key_upper = issue.upper()
            exact_issue_keys.add(key_upper)
            # Prompt: "Issue-Key-Tokens als Phrase 'ENG 42' suchen."
            tokens.append((key_upper.replace("-", " "), True))
            raw_tokens.append(issue.lower())
        elif word:
            tokens.append((word, False))
            raw_tokens.append(word.lower())

    if not tokens:
        return "", exact_issue_keys, raw_tokens

    clauses: list[str] = []
    for i, (tok, is_issue) in enumerate(tokens):
        is_last = i == len(tokens) - 1
        safe_tok = tok.replace('"', '""')
        # Prefix match for last token if len >= 3 ("tok"*)
        orig_len = len(tok)
        if is_last and orig_len >= 3:
            clauses.append(f'"{safe_tok}"*')
        else:
            clauses.append(f'"{safe_tok}"')

    return " OR ".join(clauses), exact_issue_keys, raw_tokens


def _parse_snippet(raw: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse an FTS5 snippet containing \x02 and \x03 markers.

    Returns:
        (plain_snippet, snippet_parts)
    """
    if not raw:
        return "", []

    plain = raw.replace("\x02", "").replace("\x03", "")
    parts: list[dict[str, Any]] = []
    tokens = re.split(r"(\x02[^\x03]*\x03)", raw)
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("\x02") and tok.endswith("\x03"):
            hit_text = tok[1:-1]
            if hit_text:
                parts.append({"text": hit_text, "hit": True})
        else:
            parts.append({"text": tok, "hit": False})

    return plain, parts


def _calculate_token_coverage(text: str, raw_tokens: list[str]) -> float:
    """Calculate the ratio of distinct query tokens present in text."""
    if not raw_tokens:
        return 1.0
    text_lower = text.lower()
    distinct_tokens = set(raw_tokens)
    matched = 0
    for tok in distinct_tokens:
        if "-" in tok:
            parts = tok.split("-")
            if tok in text_lower or " ".join(parts) in text_lower or all(p in text_lower for p in parts):
                matched += 1
        elif tok in text_lower:
            matched += 1
    return matched / len(distinct_tokens)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    type: str = "all",
    project: str | None = None,
    limit: int = 10,
    max_query_chars: int = 200,
) -> dict:
    """Execute a full-text search with BM25 ranking and token coverage bonus.

    Args:
        conn: sqlite3 database connection (read-only safe)
        query: user search string
        type: document filter: 'all', 'confluence', or 'jira'
        project: optional filter by space_or_project
        limit: max documents returned (hard-capped at 10)
        max_query_chars: query string cutoff limit

    Returns:
        dict with query, total_returned, duration_ms, and results list
    """
    start_time = time.perf_counter()

    if type not in ("all", "confluence", "jira"):
        raise ValueError(f"Invalid type: {type!r}; must be one of 'all', 'confluence', 'jira'")

    # Hard-cap limit at 10
    effective_limit = max(0, min(int(limit), 10))

    truncated_query = (query or "")[:max_query_chars]
    fts_query, exact_issue_keys, raw_tokens = _build_fts_query(truncated_query, max_query_chars)

    if not fts_query:
        return {
            "query": truncated_query,
            "total_returned": 0,
            "duration_ms": round((time.perf_counter() - start_time) * 1000.0, 1),
            "results": [],
        }

    # Query candidate chunks up to 200
    sql_base = """
        SELECT c.document_id, c.id AS chunk_id,
               bm25(chunks_fts, 6.0, 3.0, 1.0) AS relevance,
               snippet(chunks_fts, 2, '\x02', '\x03', ' … ', 24) AS excerpt,
               snippet(chunks_fts, -1, '\x02', '\x03', ' … ', 24) AS full_excerpt,
               d.uid, d.source_type, d.source_id, d.title, d.url, d.space_or_project,
               d.source_updated_at, d.status_label, d.document_type,
               c.title AS chunk_title, c.heading AS chunk_heading, c.body AS chunk_body
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN documents d ON d.id = c.document_id
        WHERE chunks_fts MATCH ? AND d.state = 'indexed'
    """
    params: list[Any] = [fts_query]

    if type != "all":
        sql_base += " AND d.source_type = ?"
        params.append(type)

    if project is not None and project != "":
        sql_base += " AND d.space_or_project = ?"
        params.append(project)

    sql_base += " ORDER BY relevance ASC, c.document_id ASC LIMIT 200"

    try:
        cur = conn.execute(sql_base, params)
        candidate_rows = cur.fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("FTS query failed: %s; error: %s", fts_query, exc)
        return {
            "query": truncated_query,
            "total_returned": 0,
            "duration_ms": round((time.perf_counter() - start_time) * 1000.0, 1),
            "results": [],
        }

    # Aggregate best chunk per document
    best_chunks_by_doc: dict[str, dict[str, Any]] = {}

    for row in candidate_rows:
        doc_uid = row["uid"]
        relevance = float(row["relevance"])  # negative in FTS5 bm25
        bm25_positive = -relevance

        # Snippet selection: prefer body excerpt with hit markers
        raw_snippet = ""
        if "\x02" in (row["excerpt"] or ""):
            raw_snippet = row["excerpt"]
        elif "\x02" in (row["full_excerpt"] or ""):
            raw_snippet = row["full_excerpt"]
        elif row["excerpt"]:
            raw_snippet = row["excerpt"]
        elif row["full_excerpt"]:
            raw_snippet = row["full_excerpt"]

        chunk_text = f"{row['chunk_title'] or ''} {row['chunk_heading'] or ''} {row['chunk_body'] or ''}"
        coverage = _calculate_token_coverage(f"{row['title'] or ''} {chunk_text}", raw_tokens)
        score = bm25_positive * (1.0 + 2.0 * coverage)

        if doc_uid not in best_chunks_by_doc or score > best_chunks_by_doc[doc_uid]["score"]:
            plain_snip, snip_parts = _parse_snippet(raw_snippet)
            best_chunks_by_doc[doc_uid] = {
                "uid": doc_uid,
                "type": row["source_type"],
                "source_id": row["source_id"],
                "title": row["title"],
                "url": row["url"],
                "project": row["space_or_project"],
                "snippet": plain_snip,
                "snippet_parts": snip_parts,
                "updated_at": row["source_updated_at"],
                "status": row["status_label"],
                "document_type": row["document_type"],
                "score": score,
            }

    # Guarantee exact issue key Jira documents are included and prioritized
    if exact_issue_keys:
        for key in exact_issue_keys:
            jira_uid = f"jira:{key}"
            if jira_uid not in best_chunks_by_doc:
                # Check if this exact indexed jira document exists in the database
                check_sql = "SELECT * FROM documents WHERE uid = ? AND state = 'indexed'"
                check_params: list[Any] = [jira_uid]
                if type != "all":
                    check_sql += " AND source_type = ?"
                    check_params.append(type)
                if project is not None and project != "":
                    check_sql += " AND space_or_project = ?"
                    check_params.append(project)

                doc_row = conn.execute(check_sql, check_params).fetchone()
                if doc_row:
                    body = (doc_row["body_text"] or "")[:200]
                    best_chunks_by_doc[jira_uid] = {
                        "uid": doc_row["uid"],
                        "type": doc_row["source_type"],
                        "source_id": doc_row["source_id"],
                        "title": doc_row["title"],
                        "url": doc_row["url"],
                        "project": doc_row["space_or_project"],
                        "snippet": body,
                        "snippet_parts": [{"text": body, "hit": False}] if body else [],
                        "updated_at": doc_row["source_updated_at"],
                        "status": doc_row["status_label"],
                        "document_type": doc_row["document_type"],
                        "score": 999999.0,
                    }

    # Sorting:
    # 1. Exact Jira issue key match first
    # 2. Combined score descending
    # 3. UID ascending as tie-breaker
    def sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
        is_exact_jira = 1 if (item["type"] == "jira" and (item.get("source_id") or "").upper() in exact_issue_keys) else 0
        return (
            -is_exact_jira,
            -item["score"],
            item["uid"],
        )

    sorted_docs = sorted(best_chunks_by_doc.values(), key=sort_key)
    final_results = sorted_docs[:effective_limit]

    # Clean internal score and source_id fields from public output
    cleaned_results = []
    for item in final_results:
        cleaned_results.append({
            "uid": item["uid"],
            "type": item["type"],
            "title": item["title"],
            "url": item["url"],
            "project": item["project"],
            "snippet": item["snippet"],
            "snippet_parts": item["snippet_parts"],
            "updated_at": item["updated_at"],
            "status": item["status"],
            "document_type": item["document_type"],
        })

    duration_ms = round((time.perf_counter() - start_time) * 1000.0, 1)

    return {
        "query": truncated_query,
        "total_returned": len(cleaned_results),
        "duration_ms": duration_ms,
        "results": cleaned_results,
    }


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    """List distinct projects and spaces for indexed documents with counts."""
    sql = """
        SELECT source_type AS type, space_or_project AS project, COUNT(*) AS count
        FROM documents
        WHERE state = 'indexed' AND space_or_project IS NOT NULL AND space_or_project != ''
        GROUP BY source_type, space_or_project
        ORDER BY count DESC, project ASC
    """
    rows = conn.execute(sql).fetchall()
    return [{"type": r["type"], "project": r["project"], "count": r["count"]} for r in rows]
