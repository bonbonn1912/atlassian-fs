from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# Hard upper bound for a single subgraph response (the frontend renders on canvas).
MAX_SUBGRAPH_NODES = 20000
# Stay well below SQLite's bound-parameter limit when using IN (...) lists.
_SQL_CHUNK = 500

_NODE_COLUMNS = ("id, uid, source_type, title, url, state, space_or_project, "
                 "document_type, status_label")


def _chunks(items: Sequence[int], size: int = _SQL_CHUNK) -> Iterable[Sequence[int]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _make_node_dict(
    doc_row: sqlite3.Row | dict[str, Any],
    degree: int = 0,
    in_degree: int = 0,
    out_degree: int = 0,
) -> dict[str, Any]:
    """Format standard graph node dictionary."""
    return {
        "uid": doc_row["uid"],
        "type": doc_row["source_type"],
        "title": doc_row["title"],
        "url": doc_row["url"],
        "state": doc_row["state"],
        "project": doc_row["space_or_project"],
        "document_type": doc_row["document_type"],
        "status": doc_row["status_label"],
        "degree": degree,
        "in_degree": in_degree,
        "out_degree": out_degree,
    }


def _load_edge_index(
    conn: sqlite3.Connection, edge_kinds: set[str] | None
) -> tuple[list[tuple[int, int, int]], dict[int, int], dict[int, int], dict[int, int]]:
    """All (edge_id, source, target) of the allowed kinds plus degree, in_degree, out_degree per doc.

    Loading the compact edge list once is far cheaper than one query per BFS node and
    keeps large subgraphs (thousands of nodes) fast.
    """
    edges: list[tuple[int, int, int]] = []
    degree: dict[int, int] = defaultdict(int)
    in_degree: dict[int, int] = defaultdict(int)
    out_degree: dict[int, int] = defaultdict(int)
    for eid, s, t, kind in conn.execute("SELECT id, source_doc, target_doc, kind FROM edges"):
        degree[s] += 1
        degree[t] += 1
        out_degree[s] += 1
        in_degree[t] += 1
        if edge_kinds is None or kind in edge_kinds:
            edges.append((eid, s, t))
    return edges, degree, in_degree, out_degree


def _fetch_nodes(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, sqlite3.Row]:
    out: dict[int, sqlite3.Row] = {}
    for chunk in _chunks(list(ids)):
        p = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT {_NODE_COLUMNS} FROM documents WHERE id IN ({p})", chunk):
            out[row["id"]] = row
    return out


def _fetch_edges(conn: sqlite3.Connection, edge_ids: Sequence[int]) -> list[dict[str, Any]]:
    rows: list[sqlite3.Row] = []
    for chunk in _chunks(sorted(edge_ids)):
        p = ",".join("?" * len(chunk))
        rows.extend(conn.execute(
            f"""SELECT e.id, s.uid AS source, t.uid AS target, e.kind, e.relation_label,
                       e.source_field, e.anchor_text, e.evidence_url
                FROM edges e
                JOIN documents s ON s.id = e.source_doc
                JOIN documents t ON t.id = e.target_doc
                WHERE e.id IN ({p})""", chunk))
    rows.sort(key=lambda r: r["id"])
    return [{
        "id": r["id"], "source": r["source"], "target": r["target"], "kind": r["kind"],
        "relation_label": r["relation_label"], "source_field": r["source_field"],
        "anchor_text": r["anchor_text"], "evidence_url": r["evidence_url"],
    } for r in rows]


def get_subgraph(
    conn: sqlite3.Connection,
    center: str | None,
    *,
    hops: int = 1,
    max_nodes: int = 150,
    types: str = "all",
    edge_kinds: Sequence[str] | None = None,
) -> dict | None:
    """Return a neighborhood subgraph around a center node, or top nodes by degree.

    Args:
        conn: sqlite3 connection
        center: UID of root document, or None for global view
        hops: exploration radius, clamped to 0..3
        max_nodes: maximum nodes to return, clamped to 1..MAX_SUBGRAPH_NODES
        types: filter by document source_type ('all', 'confluence', 'jira')
        edge_kinds: optional allowed edge kinds (e.g. ['links_to', 'parent_of'])

    Returns:
        dict with center, nodes, edges, truncated, hidden_nodes; or None if center not found.
    """
    if types not in ("all", "confluence", "jira"):
        raise ValueError(f"Invalid types: {types!r}; must be one of 'all', 'confluence', 'jira'")
    effective_hops = max(0, min(int(hops), 3))
    effective_max_nodes = max(1, min(int(max_nodes), MAX_SUBGRAPH_NODES))
    kinds = set(edge_kinds) if edge_kinds is not None else None

    center_id: int | None = None
    if center is not None:
        row = conn.execute("SELECT id FROM documents WHERE uid = ?", (center,)).fetchone()
        if row is None:
            return None
        center_id = row["id"]

    edges, degree, in_degree, out_degree = _load_edge_index(conn, kinds)
    type_of: dict[int, str] = {}
    if types != "all" or center_id is None:
        type_of = {r[0]: r[1] for r in conn.execute("SELECT id, source_type FROM documents")}

    def type_ok(doc_id: int) -> bool:
        return types == "all" or type_of.get(doc_id) == types

    truncated = False
    hidden: set[int] = set()
    if center_id is not None:
        adjacency: dict[int, list[int]] = defaultdict(list)
        for _eid, s, t in edges:
            adjacency[s].append(t)
            adjacency[t].append(s)
        selected: list[int] = [center_id]
        seen = {center_id}
        frontier = [center_id]
        for _ in range(effective_hops):
            nxt: list[int] = []
            # Expand high-degree neighbours first so truncation keeps the hubs.
            candidates: list[int] = []
            for node in frontier:
                for nb in adjacency.get(node, ()):
                    if nb not in seen and type_ok(nb):
                        seen.add(nb)
                        candidates.append(nb)
            candidates.sort(key=lambda d: (-degree.get(d, 0), d))
            for nb in candidates:
                if len(selected) < effective_max_nodes:
                    selected.append(nb)
                    nxt.append(nb)
                else:
                    truncated = True
                    hidden.add(nb)
            frontier = nxt
            if not frontier:
                break
    else:
        eligible = [d for d in type_of if type_ok(d)]
        eligible.sort(key=lambda d: (-degree.get(d, 0), d))
        selected = eligible[:effective_max_nodes]
        if len(eligible) > effective_max_nodes:
            truncated = True
            hidden = set(eligible[effective_max_nodes:])

    selected_set = set(selected)
    rows = _fetch_nodes(conn, selected)
    order = sorted(selected_set, key=lambda d: (0 if d == center_id else 1, -degree.get(d, 0), rows[d]["uid"]))
    nodes = [
        _make_node_dict(
            rows[d],
            degree.get(d, 0),
            in_degree.get(d, 0),
            out_degree.get(d, 0),
        )
        for d in order if d in rows
    ]
    edge_ids = [eid for eid, s, t in edges if s in selected_set and t in selected_set]
    return {
        "center": center,
        "nodes": nodes,
        "edges": _fetch_edges(conn, edge_ids),
        "truncated": truncated,
        "hidden_nodes": len(hidden),
    }


def get_document_detail(
    conn: sqlite3.Connection,
    uid: str,
    *,
    max_chars: int = 4000,
    neighbor_limit: int = 100,
) -> dict | None:
    """Return comprehensive document details with truncated text and linked neighbors.

    Args:
        conn: sqlite3 connection
        uid: document identifier
        max_chars: cutoff for body_text
        neighbor_limit: max incoming and outgoing edges returned

    Returns:
        document detail dict or None if not found
    """
    row = conn.execute(
        """
        SELECT id, uid, source_type, source_id, title, url, space_or_project,
               status_label, document_type, body_text, source_version,
               source_updated_at, state, fetched_at, last_error, labels
        FROM documents WHERE uid = ?
        """,
        (uid,),
    ).fetchone()

    if row is None:
        return None

    body_text = row["body_text"] or ""
    truncated = len(body_text) > max_chars
    text = body_text[:max_chars]

    labels_raw = row["labels"] or ""
    labels = [lbl.strip() for lbl in labels_raw.split(",") if lbl.strip()]

    doc_id = row["id"]

    # Outgoing edges
    out_sql = """
        SELECT e.id, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url,
               t.uid, t.source_type, t.title, t.url, t.state, t.space_or_project,
               t.document_type, t.status_label
        FROM edges e
        JOIN documents t ON t.id = e.target_doc
        WHERE e.source_doc = ?
        ORDER BY e.id ASC
        LIMIT ?
    """
    out_rows = conn.execute(out_sql, (doc_id, neighbor_limit)).fetchall()
    outgoing = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "relation_label": r["relation_label"],
            "source_field": r["source_field"],
            "anchor_text": r["anchor_text"],
            "evidence_url": r["evidence_url"],
            "target": {
                "uid": r["uid"],
                "type": r["source_type"],
                "title": r["title"],
                "url": r["url"],
                "state": r["state"],
                "project": r["space_or_project"],
                "document_type": r["document_type"],
                "status": r["status_label"],
            },
        }
        for r in out_rows
    ]

    # Incoming edges
    in_sql = """
        SELECT e.id, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url,
               s.uid, s.source_type, s.title, s.url, s.state, s.space_or_project,
               s.document_type, s.status_label
        FROM edges e
        JOIN documents s ON s.id = e.source_doc
        WHERE e.target_doc = ?
        ORDER BY e.id ASC
        LIMIT ?
    """
    in_rows = conn.execute(in_sql, (doc_id, neighbor_limit)).fetchall()
    incoming = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "relation_label": r["relation_label"],
            "source_field": r["source_field"],
            "anchor_text": r["anchor_text"],
            "evidence_url": r["evidence_url"],
            "source": {
                "uid": r["uid"],
                "type": r["source_type"],
                "title": r["title"],
                "url": r["url"],
                "state": r["state"],
                "project": r["space_or_project"],
                "document_type": r["document_type"],
                "status": r["status_label"],
            },
        }
        for r in in_rows
    ]

    return {
        "uid": row["uid"],
        "type": row["source_type"],
        "source_id": row["source_id"],
        "title": row["title"],
        "url": row["url"],
        "project": row["space_or_project"],
        "status": row["status_label"],
        "document_type": row["document_type"],
        "source_version": row["source_version"],
        "updated_at": row["source_updated_at"],
        "state": row["state"],
        "fetched_at": row["fetched_at"],
        "last_error": row["last_error"],
        "labels": labels,
        "text": text,
        "truncated": truncated,
        "outgoing": outgoing,
        "incoming": incoming,
    }


def get_neighbors(
    conn: sqlite3.Connection,
    uid: str,
    *,
    direction: str = "both",
    limit: int = 50,
) -> dict | None:
    """Return incoming and/or outgoing neighbors for a document node.

    Args:
        conn: sqlite3 connection
        uid: document UID
        direction: 'both', 'in', or 'out'
        limit: maximum number of neighbors returned (max 200)

    Returns:
        dict with uid, direction, outgoing, incoming, and total; or None if not found.
    """
    if direction not in ("both", "in", "out"):
        raise ValueError(f"Invalid direction: {direction!r}; must be one of 'both', 'in', 'out'")

    doc = conn.execute("SELECT id, uid FROM documents WHERE uid = ?", (uid,)).fetchone()
    if doc is None:
        return None

    doc_id = doc["id"]
    eff_limit = max(1, min(int(limit), 200))

    outgoing: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []

    if direction in ("both", "out"):
        out_sql = """
            SELECT e.id, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url,
                   t.uid, t.source_type, t.title, t.url, t.state, t.space_or_project,
                   t.document_type, t.status_label
            FROM edges e
            JOIN documents t ON t.id = e.target_doc
            WHERE e.source_doc = ?
            ORDER BY e.id ASC
            LIMIT ?
        """
        rows = conn.execute(out_sql, (doc_id, eff_limit)).fetchall()
        for r in rows:
            outgoing.append({
                "id": r["id"],
                "kind": r["kind"],
                "relation_label": r["relation_label"],
                "source_field": r["source_field"],
                "anchor_text": r["anchor_text"],
                "evidence_url": r["evidence_url"],
                "direction": "out",
                "target": {
                    "uid": r["uid"],
                    "type": r["source_type"],
                    "title": r["title"],
                    "url": r["url"],
                    "state": r["state"],
                    "project": r["space_or_project"],
                    "document_type": r["document_type"],
                    "status": r["status_label"],
                },
            })

    if direction in ("both", "in"):
        rem_limit = eff_limit - len(outgoing) if direction == "both" else eff_limit
        if rem_limit > 0:
            in_sql = """
                SELECT e.id, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url,
                       s.uid, s.source_type, s.title, s.url, s.state, s.space_or_project,
                       s.document_type, s.status_label
                FROM edges e
                JOIN documents s ON s.id = e.source_doc
                WHERE e.target_doc = ?
                ORDER BY e.id ASC
                LIMIT ?
            """
            rows = conn.execute(in_sql, (doc_id, rem_limit)).fetchall()
            for r in rows:
                incoming.append({
                    "id": r["id"],
                    "kind": r["kind"],
                    "relation_label": r["relation_label"],
                    "source_field": r["source_field"],
                    "anchor_text": r["anchor_text"],
                    "evidence_url": r["evidence_url"],
                    "direction": "in",
                    "source": {
                        "uid": r["uid"],
                        "type": r["source_type"],
                        "title": r["title"],
                        "url": r["url"],
                        "state": r["state"],
                        "project": r["space_or_project"],
                        "document_type": r["document_type"],
                        "status": r["status_label"],
                    },
                })

    return {
        "uid": uid,
        "direction": direction,
        "outgoing": outgoing,
        "incoming": incoming,
        "total": len(outgoing) + len(incoming),
    }


def find_related(
    conn: sqlite3.Connection,
    uid: str,
    *,
    limit: int = 10,
) -> dict | None:
    """Find direct neighbors and 2nd-degree neighbors sharing >=1 direct neighbor.

    Args:
        conn: sqlite3 connection
        uid: document UID
        limit: max results returned (max 50)

    Returns:
        dict with uid, total_returned, and related list; or None if uid not found.
    """
    doc = conn.execute("SELECT id, uid FROM documents WHERE uid = ?", (uid,)).fetchone()
    if doc is None:
        return None

    doc_id = doc["id"]
    eff_limit = max(1, min(int(limit), 50))

    # 1. Direct neighbors of doc_id
    direct_nbr_sql = """
        SELECT DISTINCT CASE WHEN source_doc = ? THEN target_doc ELSE source_doc END AS nbr_id
        FROM edges
        WHERE (source_doc = ? OR target_doc = ?)
          AND (CASE WHEN source_doc = ? THEN target_doc ELSE source_doc END) != ?
    """
    direct_rows = conn.execute(direct_nbr_sql, (doc_id, doc_id, doc_id, doc_id, doc_id)).fetchall()
    direct_nbr_ids = {r["nbr_id"] for r in direct_rows}

    # 2. Candidate nodes: direct neighbors + 2-hop neighbors sharing >=1 direct neighbor
    candidate_scores: dict[int, dict[str, Any]] = {}

    for d_id in direct_nbr_ids:
        candidate_scores[d_id] = {
            "cand_id": d_id,
            "shared_count": 0,
            "is_direct": True,
        }

    if direct_nbr_ids:
        p = ",".join("?" for _ in direct_nbr_ids)
        second_hop_sql = f"""
            SELECT CASE WHEN source_doc IN ({p}) THEN target_doc ELSE source_doc END AS cand_id,
                   COUNT(DISTINCT CASE WHEN source_doc IN ({p}) THEN source_doc ELSE target_doc END) AS shared_cnt
            FROM edges
            WHERE (source_doc IN ({p}) OR target_doc IN ({p}))
            GROUP BY cand_id
            HAVING cand_id != ?
        """
        second_params = list(direct_nbr_ids) * 4 + [doc_id]
        second_rows = conn.execute(second_hop_sql, second_params).fetchall()
        for r in second_rows:
            c_id = r["cand_id"]
            s_cnt = int(r["shared_cnt"])
            if c_id not in candidate_scores:
                candidate_scores[c_id] = {
                    "cand_id": c_id,
                    "shared_count": s_cnt,
                    "is_direct": False,
                }
            else:
                candidate_scores[c_id]["shared_count"] = s_cnt

    if not candidate_scores:
        return {
            "uid": uid,
            "total_returned": 0,
            "related": [],
        }

    # Fetch document records for all candidates
    cand_ids = list(candidate_scores.keys())
    cand_p = ",".join("?" for _ in cand_ids)
    docs_sql = f"""
        SELECT id, uid, source_type, title, url, state, space_or_project,
               document_type, status_label
        FROM documents
        WHERE id IN ({cand_p})
    """
    doc_rows = conn.execute(docs_sql, cand_ids).fetchall()

    candidates_list: list[dict[str, Any]] = []
    for d in doc_rows:
        meta = candidate_scores[d["id"]]
        shared_count = meta["shared_count"]
        is_direct = meta["is_direct"]
        # Score = shared count + direct bonus (1 if direct)
        score = shared_count + (1 if is_direct else 0)
        candidates_list.append({
            "uid": d["uid"],
            "type": d["source_type"],
            "title": d["title"],
            "url": d["url"],
            "state": d["state"],
            "project": d["space_or_project"],
            "document_type": d["document_type"],
            "status": d["status_label"],
            "score": score,
            "shared_count": shared_count,
            "is_direct": is_direct,
        })

    # Sort deterministically: score DESC, shared_count DESC, is_direct DESC, uid ASC
    candidates_list.sort(key=lambda x: (-x["score"], -x["shared_count"], not x["is_direct"], x["uid"]))
    final_related = candidates_list[:eff_limit]

    return {
        "uid": uid,
        "total_returned": len(final_related),
        "related": final_related,
    }


def get_top_documents(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    types: str = "all",
    project: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Return documents ranked by how many other documents link to them (incoming count / importance).

    Args:
        conn: sqlite3 connection
        limit: maximum documents to return (1..500)
        offset: pagination offset (>=0)
        types: 'all', 'confluence', or 'jira'
        project: optional space_or_project filter
        query: optional text search in title, uid, or source_id

    Returns:
        dict with total, limit, offset, and documents list.
    """
    if types not in ("all", "confluence", "jira"):
        raise ValueError(f"Invalid types: {types!r}; must be one of 'all', 'confluence', 'jira'")
    eff_limit = max(1, min(int(limit), 500))
    eff_offset = max(0, int(offset))

    where_clauses: list[str] = []
    params: list[Any] = []

    if types != "all":
        where_clauses.append("d.source_type = ?")
        params.append(types)

    if project:
        where_clauses.append("d.space_or_project = ?")
        params.append(project)

    if query and query.strip():
        q_clean = query.strip()
        where_clauses.append("(d.title LIKE ? OR d.uid LIKE ? OR d.source_id LIKE ?)")
        pattern = f"%{q_clean}%"
        params.extend([pattern, pattern, pattern])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    count_sql = f"SELECT COUNT(*) FROM documents d {where_sql}"
    total = conn.execute(count_sql, params).fetchone()[0]

    sql = f"""
        SELECT d.id, d.uid, d.source_type, d.source_id, d.title, d.url,
               d.space_or_project, d.document_type, d.status_label, d.state,
               COALESCE(inc.cnt, 0) AS incoming_count,
               COALESCE(outg.cnt, 0) AS outgoing_count
        FROM documents d
        LEFT JOIN (
            SELECT target_doc, COUNT(*) AS cnt
            FROM edges
            GROUP BY target_doc
        ) inc ON inc.target_doc = d.id
        LEFT JOIN (
            SELECT source_doc, COUNT(*) AS cnt
            FROM edges
            GROUP BY source_doc
        ) outg ON outg.source_doc = d.id
        {where_sql}
        ORDER BY incoming_count DESC, outgoing_count DESC, d.id ASC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, (*params, eff_limit, eff_offset)).fetchall()
    documents: list[dict[str, Any]] = []
    for r in rows:
        inc_c = int(r["incoming_count"])
        out_c = int(r["outgoing_count"])
        documents.append({
            "uid": r["uid"],
            "type": r["source_type"],
            "source_id": r["source_id"],
            "title": r["title"],
            "url": r["url"],
            "project": r["space_or_project"],
            "document_type": r["document_type"],
            "status": r["status_label"],
            "state": r["state"],
            "incoming_count": inc_c,
            "outgoing_count": out_c,
            "total_degree": inc_c + out_c,
        })

    return {
        "total": total,
        "limit": eff_limit,
        "offset": eff_offset,
        "documents": documents,
    }
