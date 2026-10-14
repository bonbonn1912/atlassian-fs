from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# Hard upper bound for a single subgraph response (the frontend renders on canvas).
MAX_SUBGRAPH_NODES = 100_000
# Stay well below SQLite's bound-parameter limit when using IN (...) lists.
_SQL_CHUNK = 500

_NODE_COLUMNS = ("id, uid, source_type, title, url, state, space_or_project, "
                 "document_type, status_label, source_created_at, source_updated_at")

VALID_GRAPH_TYPES = (
    "all", "confluence", "jira", "gitlab",
    "jira,gitlab", "confluence,gitlab", "confluence,jira",
)


def _chunks(items: Sequence[int], size: int = _SQL_CHUNK) -> Iterable[Sequence[int]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _get_field(row: sqlite3.Row | dict[str, Any], key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


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
        "source_created_at": _get_field(doc_row, "source_created_at"),
        "source_updated_at": _get_field(doc_row, "source_updated_at"),
        "document_type": doc_row["document_type"],
        "status": doc_row["status_label"],
        "degree": degree,
        "in_degree": in_degree,
        "out_degree": out_degree,
    }


def _load_edge_index(
    conn: sqlite3.Connection,
    edge_kinds: set[str] | None,
    include_gitlab: bool = True,
) -> tuple[list[tuple[int, int, int]], dict[int, int], dict[int, int], dict[int, int]]:
    """All (edge_id, source, target) of the allowed kinds plus degree, in_degree, out_degree per doc.

    Loading the compact edge list once is far cheaper than one query per BFS node and
    keeps large subgraphs (thousands of nodes) fast.
    """
    edges: list[tuple[int, int, int]] = []
    degree: dict[int, int] = defaultdict(int)
    in_degree: dict[int, int] = defaultdict(int)
    out_degree: dict[int, int] = defaultdict(int)
    if include_gitlab:
        sql = "SELECT id, source_doc, target_doc, kind FROM edges"
        rows = conn.execute(sql)
    else:
        sql = """
            SELECT e.id, e.source_doc, e.target_doc, e.kind
            FROM edges e
            JOIN documents s ON s.id = e.source_doc
            JOIN documents t ON t.id = e.target_doc
            WHERE s.source_type != 'gitlab' AND t.source_type != 'gitlab'
        """
        rows = conn.execute(sql)

    for eid, s, t, kind in rows:
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


def _overview_with_commit_clusters(
    eligible: list[int],
    edges: list[tuple[int, int, int]],
    type_of: dict[int, str],
    degree: dict[int, int],
    max_nodes: int,
) -> list[int]:
    """Select nodes for overview graph, prioritizing commit clusters within half the budget."""
    eligible_set = set(eligible)
    commits_by_target: dict[int, list[int]] = defaultdict(list)
    for _eid, s, t in edges:
        if type_of.get(s) == "gitlab" and s in eligible_set and t in eligible_set:
            commits_by_target[t].append(s)
    if not commits_by_target:
        return eligible[:max_nodes]

    budget = max_nodes // 2
    selected: list[int] = []
    seen: set[int] = set()
    targets = sorted(commits_by_target, key=lambda t: (-len(commits_by_target[t]), -degree.get(t, 0), t))
    for target in targets:
        cluster = [d for d in [target, *sorted(set(commits_by_target[target]))] if d not in seen]
        if not cluster:
            continue
        if len(selected) + len(cluster) > budget:
            break
        selected.extend(cluster)
        seen.update(cluster)

    for d in eligible:
        if len(selected) >= max_nodes:
            break
        if d not in seen:
            selected.append(d)
            seen.add(d)
    return selected


def get_subgraph(
    conn: sqlite3.Connection,
    center: str | None,
    *,
    hops: int = 1,
    max_nodes: int = 150,
    types: str = "all",
    edge_kinds: Sequence[str] | None = None,
    include_gitlab: bool = True,
    only_with_gitlab: bool = False,
) -> dict | None:
    """Return a neighborhood subgraph around a center node, or top nodes by degree.

    Args:
        conn: sqlite3 connection
        center: UID of root document, or None for global view
        hops: exploration radius, clamped to 0..3
        max_nodes: maximum nodes to return, clamped to 1..MAX_SUBGRAPH_NODES
        types: filter by document source_type ('all', 'confluence', 'jira', etc.)
        edge_kinds: optional allowed edge kinds (e.g. ['links_to', 'parent_of'])
        include_gitlab: whether gitlab nodes are considered
        only_with_gitlab: whether to show only Jira tickets that have linked gitlab commits

    Returns:
        dict with center, nodes, edges, truncated, hidden_nodes; or None if center not found.
    """
    if types not in VALID_GRAPH_TYPES:
        raise ValueError(f"Invalid types: {types!r}; must be one of {VALID_GRAPH_TYPES}")
    effective_hops = max(0, min(int(hops), 3))
    effective_max_nodes = max(1, min(int(max_nodes), MAX_SUBGRAPH_NODES))
    kinds = set(edge_kinds) if edge_kinds is not None else None

    center_id: int | None = None
    if center is not None:
        row = conn.execute("SELECT id, source_type FROM documents WHERE uid = ?", (center,)).fetchone()
        if row is None:
            return None
        if not include_gitlab and row["source_type"] == "gitlab":
            return None
        center_id = row["id"]

    edges, degree, in_degree, out_degree = _load_edge_index(conn, kinds, include_gitlab)
    type_of: dict[int, str] = {r[0]: r[1] for r in conn.execute("SELECT id, source_type FROM documents")}

    jira_with_gitlab: set[int] = set()
    if only_with_gitlab or include_gitlab:
        for _eid, s, t in edges:
            if type_of.get(s) == "gitlab" and type_of.get(t) == "jira":
                jira_with_gitlab.add(t)
            elif type_of.get(t) == "gitlab" and type_of.get(s) == "jira":
                jira_with_gitlab.add(s)

    allowed_types = set(types.split(",")) if types != "all" else None

    def type_ok(doc_id: int) -> bool:
        st = type_of.get(doc_id)
        if not include_gitlab and st == "gitlab":
            return False
        if allowed_types is not None and st not in allowed_types:
            return False
        if only_with_gitlab and st == "jira" and doc_id not in jira_with_gitlab:
            return False
        return True

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
            candidates: list[int] = []
            for node in frontier:
                for nb in adjacency.get(node, ()):
                    if nb not in seen and type_ok(nb):
                        seen.add(nb)
                        candidates.append(nb)
            candidates.sort(key=lambda d: (0 if type_of.get(d) == "gitlab" else 1, -degree.get(d, 0), d))
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
        selected = _overview_with_commit_clusters(eligible, edges, type_of, degree, effective_max_nodes)
        if len(eligible) > len(selected):
            truncated = True
            hidden = set(eligible[len(selected):])

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
    include_gitlab: bool = True,
) -> dict | None:
    """Return comprehensive document details with truncated text and linked neighbors.

    Args:
        conn: sqlite3 connection
        uid: document identifier
        max_chars: cutoff for body_text
        neighbor_limit: max incoming and outgoing edges returned
        include_gitlab: whether gitlab documents/edges are included

    Returns:
        document detail dict or None if not found
    """
    row = conn.execute(
        """
        SELECT id, uid, source_type, source_id, title, url, space_or_project,
               status_label, document_type, body_text, source_version,
               source_created_at, source_updated_at, state, fetched_at, last_error, labels
        FROM documents WHERE uid = ?
        """,
        (uid,),
    ).fetchone()

    if row is None:
        return None

    if not include_gitlab and row["source_type"] == "gitlab":
        return None

    body_text = row["body_text"] or ""
    truncated = len(body_text) > max_chars
    text = body_text[:max_chars]

    labels_raw = row["labels"] or ""
    labels = [lbl.strip() for lbl in labels_raw.split(",") if lbl.strip()]

    doc_id = row["id"]

    gitlab_filter_target = "AND t.source_type != 'gitlab'" if not include_gitlab else ""
    gitlab_filter_source = "AND s.source_type != 'gitlab'" if not include_gitlab else ""

    # Outgoing edges
    out_sql = f"""
        SELECT e.id, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url,
               t.uid, t.source_type, t.title, t.url, t.state, t.space_or_project,
               t.document_type, t.status_label, t.source_created_at, t.source_updated_at
        FROM edges e
        JOIN documents t ON t.id = e.target_doc
        WHERE e.source_doc = ? {gitlab_filter_target}
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
                "source_created_at": r["source_created_at"],
                "source_updated_at": r["source_updated_at"],
                "document_type": r["document_type"],
                "status": r["status_label"],
            },
        }
        for r in out_rows
    ]

    # Incoming edges
    in_sql = f"""
        SELECT e.id, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url,
               s.uid, s.source_type, s.title, s.url, s.state, s.space_or_project,
               s.document_type, s.status_label, s.source_created_at, s.source_updated_at
        FROM edges e
        JOIN documents s ON s.id = e.source_doc
        WHERE e.target_doc = ? {gitlab_filter_source}
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
                "source_created_at": r["source_created_at"],
                "source_updated_at": r["source_updated_at"],
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
        "source_created_at": row["source_created_at"],
        "source_updated_at": row["source_updated_at"],
        "updated_at": row["source_updated_at"],
        "state": row["state"],
        "fetched_at": row["fetched_at"],
        "last_error": row["last_error"],
        "labels": labels,
        "text": text,
        "total_chars": len(body_text),
        "returned_chars": len(text),
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
    include_gitlab: bool = True,
) -> dict | None:
    """Return paginated incoming and/or outgoing edges for a specific document node.

    Args:
        conn: sqlite3 connection
        uid: document UID
        direction: 'both', 'in', or 'out'
        limit: max edges returned per direction (1..200)
        include_gitlab: whether gitlab documents/edges are included

    Returns:
        dict with uid, direction, outgoing, incoming, and total; or None if uid not found.
    """
    if direction not in ("both", "in", "out"):
        raise ValueError(f"Invalid direction: {direction!r}; must be one of 'both', 'in', 'out'")

    doc = conn.execute("SELECT id, uid, source_type FROM documents WHERE uid = ?", (uid,)).fetchone()
    if doc is None:
        return None

    if not include_gitlab and doc["source_type"] == "gitlab":
        return None

    doc_id = doc["id"]
    eff_limit = max(1, min(int(limit), 200))

    outgoing: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []

    gitlab_filter_target = "AND t.source_type != 'gitlab'" if not include_gitlab else ""
    gitlab_filter_source = "AND s.source_type != 'gitlab'" if not include_gitlab else ""

    if direction in ("both", "out"):
        out_sql = f"""
            SELECT e.id, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url,
                   t.uid, t.source_type, t.title, t.url, t.state, t.space_or_project,
                   t.document_type, t.status_label, t.source_created_at, t.source_updated_at
            FROM edges e
            JOIN documents t ON t.id = e.target_doc
            WHERE e.source_doc = ? {gitlab_filter_target}
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
                    "source_created_at": r["source_created_at"],
                    "source_updated_at": r["source_updated_at"],
                    "document_type": r["document_type"],
                    "status": r["status_label"],
                },
            })

    if direction in ("both", "in"):
        rem_limit = eff_limit - len(outgoing) if direction == "both" else eff_limit
        if rem_limit > 0:
            in_sql = f"""
                SELECT e.id, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url,
                       s.uid, s.source_type, s.title, s.url, s.state, s.space_or_project,
                       s.document_type, s.status_label, s.source_created_at, s.source_updated_at
                FROM edges e
                JOIN documents s ON s.id = e.source_doc
                WHERE e.target_doc = ? {gitlab_filter_source}
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
                        "source_created_at": r["source_created_at"],
                        "source_updated_at": r["source_updated_at"],
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
    include_gitlab: bool = True,
) -> dict | None:
    """Find direct neighbors and 2nd-degree neighbors sharing >=1 direct neighbor.

    Args:
        conn: sqlite3 connection
        uid: document UID
        limit: max results returned (max 50)
        include_gitlab: whether gitlab documents/edges are considered

    Returns:
        dict with uid, total_returned, and related list; or None if uid not found.
    """
    doc = conn.execute("SELECT id, uid, source_type FROM documents WHERE uid = ?", (uid,)).fetchone()
    if doc is None:
        return None

    if not include_gitlab and doc["source_type"] == "gitlab":
        return None

    doc_id = doc["id"]
    eff_limit = max(1, min(int(limit), 50))

    edge_filter = "AND s.source_type != 'gitlab' AND t.source_type != 'gitlab'" if not include_gitlab else ""

    # 1. Direct neighbors of doc_id
    direct_nbr_sql = f"""
        SELECT DISTINCT CASE WHEN e.source_doc = ? THEN e.target_doc ELSE e.source_doc END AS nbr_id
        FROM edges e
        JOIN documents s ON s.id = e.source_doc
        JOIN documents t ON t.id = e.target_doc
        WHERE (e.source_doc = ? OR e.target_doc = ?)
          AND (CASE WHEN e.source_doc = ? THEN e.target_doc ELSE e.source_doc END) != ?
          {edge_filter}
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
            SELECT CASE WHEN e.source_doc IN ({p}) THEN e.target_doc ELSE e.source_doc END AS cand_id,
                   COUNT(DISTINCT CASE WHEN e.source_doc IN ({p}) THEN e.source_doc ELSE e.target_doc END) AS shared_cnt
            FROM edges e
            JOIN documents s ON s.id = e.source_doc
            JOIN documents t ON t.id = e.target_doc
            WHERE (e.source_doc IN ({p}) OR e.target_doc IN ({p}))
              {edge_filter}
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
               document_type, status_label, source_created_at, source_updated_at
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
            "source_created_at": d["source_created_at"],
            "source_updated_at": d["source_updated_at"],
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
    include_gitlab: bool = True,
) -> dict[str, Any]:
    """Return documents ranked by how many other documents link to them (incoming count / importance).

    Args:
        conn: sqlite3 connection
        limit: maximum documents to return (1..500)
        offset: pagination offset (>=0)
        types: 'all', 'confluence', 'jira', or comma-separated
        project: optional space_or_project filter
        query: optional text search in title, uid, or source_id
        include_gitlab: whether gitlab documents are included

    Returns:
        dict with total, limit, offset, and documents list.
    """
    if types not in VALID_GRAPH_TYPES:
        raise ValueError(f"Invalid types: {types!r}; must be one of {VALID_GRAPH_TYPES}")
    eff_limit = max(1, min(int(limit), 500))
    eff_offset = max(0, int(offset))

    where_clauses: list[str] = []
    params: list[Any] = []

    if types != "all":
        parts = [t.strip() for t in types.split(",") if t.strip()]
        placeholders = ",".join("?" for _ in parts)
        where_clauses.append(f"d.source_type IN ({placeholders})")
        params.extend(parts)

    if not include_gitlab:
        where_clauses.append("d.source_type != 'gitlab'")

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

    edge_source = "edges"
    if not include_gitlab:
        edge_source = """(
            SELECT e.* FROM edges e
            JOIN documents s ON s.id = e.source_doc
            JOIN documents t ON t.id = e.target_doc
            WHERE s.source_type != 'gitlab' AND t.source_type != 'gitlab'
        )"""

    sql = f"""
        SELECT d.id, d.uid, d.source_type, d.source_id, d.title, d.url,
               d.space_or_project, d.document_type, d.status_label, d.state,
               d.source_created_at, d.source_updated_at,
               COALESCE(inc.cnt, 0) AS incoming_count,
               COALESCE(outg.cnt, 0) AS outgoing_count
        FROM documents d
        LEFT JOIN (
            SELECT target_doc, COUNT(*) AS cnt
            FROM {edge_source}
            GROUP BY target_doc
        ) inc ON inc.target_doc = d.id
        LEFT JOIN (
            SELECT source_doc, COUNT(*) AS cnt
            FROM {edge_source}
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
            "source_created_at": r["source_created_at"],
            "source_updated_at": r["source_updated_at"],
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
