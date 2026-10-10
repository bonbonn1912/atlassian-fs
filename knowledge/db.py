"""SQLite storage: schema, migrations, documents, edges, chunks and jobs.

Connections are opened in autocommit mode (isolation_level=None); multi-statement
writes use the `transaction()` context manager so a document's content, chunks
and outgoing edges are always replaced atomically.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import (
    JOB_ACTIVE_STATES, JOB_INTERRUPTED, STATE_DISCOVERED, STATE_ERROR,
    STATE_INDEXED, STATE_SKIPPED, Chunk, FetchedDocument, KnowledgeError,
    LinkTarget, parse_uid,
)

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS documents (\n  id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL CHECK(source_type IN ('confluence','jira')),
  source_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '', space_or_project TEXT,
  status_label TEXT, document_type TEXT,
  body_text TEXT NOT NULL DEFAULT '', source_version TEXT,
  source_updated_at TEXT, content_hash TEXT,
  state TEXT NOT NULL DEFAULT 'discovered', fetched_at TEXT, last_error TEXT,
  labels TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_docs_type_state ON documents(source_type,state);
CREATE INDEX IF NOT EXISTS idx_docs_project ON documents(space_or_project);

CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY,
  source_doc INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  target_doc INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, relation_label TEXT NOT NULL DEFAULT '',
  source_field TEXT NOT NULL DEFAULT '', anchor_text TEXT NOT NULL DEFAULT '',
  evidence_url TEXT NOT NULL DEFAULT '',
  UNIQUE(source_doc,target_doc,kind,relation_label)
);
CREATE INDEX IF NOT EXISTS idx_edge_source ON edges(source_doc);
CREATE INDEX IF NOT EXISTS idx_edge_target ON edges(target_doc);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, title TEXT NOT NULL,
  heading TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
  UNIQUE(document_id,ordinal)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  title, heading, body, content='chunks', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid,title,heading,body)
  VALUES(new.id,new.title,new.heading,new.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,title,heading,body)
  VALUES('delete',old.id,old.title,old.heading,old.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,title,heading,body)
  VALUES('delete',old.id,old.title,old.heading,old.body);
  INSERT INTO chunks_fts(rowid,title,heading,body)
  VALUES(new.id,new.title,new.heading,new.body);
END;

CREATE TABLE IF NOT EXISTS crawl_jobs (
  id INTEGER PRIMARY KEY, config_json TEXT NOT NULL, state TEXT NOT NULL,
  start_page_uid TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  pages_indexed INTEGER NOT NULL DEFAULT 0, jira_indexed INTEGER NOT NULL DEFAULT 0,
  api_requests INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0, current_uid TEXT, last_message TEXT
);
CREATE TABLE IF NOT EXISTS crawl_queue (
  job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  document_uid TEXT NOT NULL, depth INTEGER NOT NULL,
  jira_hops INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
  PRIMARY KEY(job_id,document_uid)
);
CREATE TABLE IF NOT EXISTS crawl_log (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, level TEXT NOT NULL, document_uid TEXT, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crawl_log_job ON crawl_log(job_id, id);
"""

MIGRATIONS: list[tuple[int, str]] = [(1, SCHEMA_V1)]

JOB_COLUMNS = ("state", "start_page_uid", "finished_at", "pages_indexed", "jira_indexed",
               "api_requests", "skipped", "errors", "current_uid", "last_message")

MAX_EDGES_PER_DOCUMENT = 5000


class DatabaseError(KnowledgeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------- connections

def check_fts5() -> None:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        con.close()
    except sqlite3.OperationalError as exc:
        raise DatabaseError(
            "Die SQLite-Version dieser Python-Installation unterstützt kein FTS5. "
            "Bitte eine Python-Distribution mit FTS5 verwenden (z. B. python.org-Installer)."
        ) from exc


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection usable from any thread (one connection per thread!)."""
    path = Path(path)
    if readonly:
        if not path.exists():
            raise DatabaseError(f"Datenbank {path} existiert nicht. Zuerst 'python app.py init-db' ausführen.")
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                               check_same_thread=False, isolation_level=None, timeout=5)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    check_fts5()
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        with transaction(conn):
            for statement in _split_sql(sql):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?,?)",
                         (version, now_iso()))
    return max(v for v, _ in MIGRATIONS)


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    init_db(conn)
    return conn


def _split_sql(sql: str) -> list[str]:
    """Split a script into statements, keeping trigger bodies together."""
    statements, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any exception. Not re-entrant."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ----------------------------------------------------------------- documents

def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def get_document(conn: sqlite3.Connection, uid: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone())


def get_document_by_id(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def ensure_node(conn: sqlite3.Connection, uid: str, title_hint: str = "", url: str = "") -> int:
    """Create a reference node (state 'discovered') if missing. Returns its id.

    Does not open a transaction itself; call inside one for atomic batches.
    """
    source_type, source_id = parse_uid(uid)
    conn.execute(
        "INSERT OR IGNORE INTO documents(uid, source_type, source_id, title, url, state) "
        "VALUES(?,?,?,?,?,?)",
        (uid, source_type, source_id, title_hint or "", url or "", STATE_DISCOVERED))
    if title_hint:
        conn.execute("UPDATE documents SET title=? WHERE uid=? AND title=''", (title_hint, uid))
    if url:
        conn.execute("UPDATE documents SET url=? WHERE uid=? AND url=''", (url, uid))
    return conn.execute("SELECT id FROM documents WHERE uid=?", (uid,)).fetchone()[0]


def _insert_edge(conn: sqlite3.Connection, source_id: int, link: LinkTarget) -> bool:
    target_id = ensure_node(conn, link.uid, link.title_hint, "")
    if target_id == source_id:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO edges(source_doc, target_doc, kind, relation_label, source_field, "
        "anchor_text, evidence_url) VALUES(?,?,?,?,?,?,?)",
        (source_id, target_id, link.kind, link.relation_label or "", link.source_field or "",
         (link.anchor_text or "")[:500], (link.evidence_url or "")[:2000]))
    return cur.rowcount > 0


def add_edge(conn: sqlite3.Connection, source_uid: str, link: LinkTarget) -> bool:
    """Insert one edge (creating both nodes if needed). Atomic on its own."""
    with transaction(conn):
        source_id = ensure_node(conn, source_uid)
        return _insert_edge(conn, source_id, link)


def save_document(conn: sqlite3.Connection, doc: FetchedDocument, chunks: list[Chunk]) -> dict:
    """Atomically store a fetched document, its chunks and its outgoing edges.

    - Chunks are only rewritten if the content hash changed (or none exist yet).
    - All outgoing edges of the document are replaced by doc.links.
    - Incoming edges from other documents are preserved.
    Returns {"id": int, "content_changed": bool, "edges": int}.
    """
    content_hash = doc.content_hash()
    labels = ",".join(doc.labels)
    with transaction(conn):
        doc_id = ensure_node(conn, doc.uid)
        old = conn.execute("SELECT content_hash, state FROM documents WHERE id=?", (doc_id,)).fetchone()
        has_chunks = conn.execute("SELECT 1 FROM chunks WHERE document_id=? LIMIT 1", (doc_id,)).fetchone()
        changed = old["content_hash"] != content_hash or not has_chunks
        conn.execute(
            "UPDATE documents SET title=?, url=?, space_or_project=?, status_label=?, document_type=?, "
            "body_text=?, source_version=?, source_updated_at=?, content_hash=?, state=?, "
            "fetched_at=?, last_error=NULL, labels=? WHERE id=?",
            (doc.title, doc.url, doc.space_or_project, doc.status_label, doc.document_type,
             doc.body_text, doc.source_version, doc.source_updated_at, content_hash,
             STATE_INDEXED, now_iso(), labels, doc_id))
        if changed:
            conn.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            if not chunks:
                chunks = [Chunk(heading="", body=doc.title)]
            conn.executemany(
                "INSERT INTO chunks(document_id, ordinal, title, heading, body) VALUES(?,?,?,?,?)",
                [(doc_id, i, doc.title, c.heading or "", c.body) for i, c in enumerate(chunks)])
        conn.execute("DELETE FROM edges WHERE source_doc=?", (doc_id,))
        edges = 0
        for link in doc.links[:MAX_EDGES_PER_DOCUMENT]:
            if _insert_edge(conn, doc_id, link):
                edges += 1
    return {"id": doc_id, "content_changed": changed, "edges": edges}


def mark_document_state(conn: sqlite3.Connection, uid: str, state: str, error: str | None = None) -> None:
    """Record a failed/skipped fetch without destroying previously indexed content.

    - 'error' on an already indexed document keeps state 'indexed' and only sets last_error.
    - 'skipped' never demotes an indexed document.
    - 'missing' / 'forbidden' set the state (document leaves the search results).
    """
    with transaction(conn):
        ensure_node(conn, uid)
        row = conn.execute("SELECT state FROM documents WHERE uid=?", (uid,)).fetchone()
        if row["state"] == STATE_INDEXED and state in (STATE_ERROR, STATE_SKIPPED):
            if error:
                conn.execute("UPDATE documents SET last_error=? WHERE uid=?", (error, uid))
            return
        conn.execute("UPDATE documents SET state=?, last_error=? WHERE uid=?", (state, error, uid))


def get_outgoing_links(conn: sqlite3.Connection, uid: str) -> list[LinkTarget]:
    """Stored outgoing edges of a document as LinkTargets (deterministic order)."""
    rows = conn.execute(
        "SELECT t.uid, t.title, e.kind, e.relation_label, e.source_field, e.anchor_text, e.evidence_url "
        "FROM edges e JOIN documents s ON s.id=e.source_doc JOIN documents t ON t.id=e.target_doc "
        "WHERE s.uid=? ORDER BY e.id", (uid,)).fetchall()
    return [LinkTarget(uid=r["uid"], kind=r["kind"], relation_label=r["relation_label"],
                       source_field=r["source_field"], anchor_text=r["anchor_text"],
                       evidence_url=r["evidence_url"], title_hint=r["title"]) for r in rows]


def get_existing_document_uids(conn: sqlite3.Connection) -> list[str]:
    """Return all UIDs of documents that have been previously indexed or had errors."""
    rows = conn.execute(
        "SELECT uid FROM documents WHERE state IN (?, ?) ORDER BY id ASC",
        (STATE_INDEXED, STATE_ERROR),
    ).fetchall()
    return [r[0] for r in rows]


# ----------------------------------------------------------------- jobs

def create_job(conn: sqlite3.Connection, params: dict, start_page_uid: str) -> int:
    cur = conn.execute(
        "INSERT INTO crawl_jobs(config_json, state, start_page_uid, started_at, last_message) "
        "VALUES(?,?,?,?,?)",
        (json.dumps(params, ensure_ascii=False), "running", start_page_uid, now_iso(), "Gestartet"))
    return cur.lastrowid


def update_job(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    bad = set(fields) - set(JOB_COLUMNS)
    if bad:
        raise ValueError(f"unknown job columns: {bad}")
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE crawl_jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def _job_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    job = dict(row)
    try:
        job["params"] = json.loads(job.pop("config_json"))
    except (TypeError, ValueError):
        job["params"] = {}
    return job


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    return _job_row(conn.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone())


def get_latest_job(conn: sqlite3.Connection) -> dict | None:
    return _job_row(conn.execute("SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1").fetchone())


def mark_interrupted_jobs(conn: sqlite3.Connection) -> int:
    placeholders = ",".join("?" * len(JOB_ACTIVE_STATES))
    cur = conn.execute(
        f"UPDATE crawl_jobs SET state=?, finished_at=?, last_message=? WHERE state IN ({placeholders})",
        (JOB_INTERRUPTED, now_iso(), "Durch Neustart unterbrochen", *JOB_ACTIVE_STATES))
    return cur.rowcount


def queue_add(conn: sqlite3.Connection, job_id: int, uid: str, depth: int, jira_hops: int) -> bool:
    """Insert (job_id, uid) once. Returns False if already queued in this job."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO crawl_queue(job_id, document_uid, depth, jira_hops) VALUES(?,?,?,?)",
        (job_id, uid, depth, jira_hops))
    return cur.rowcount > 0


def queue_set_state(conn: sqlite3.Connection, job_id: int, uid: str, state: str) -> None:
    conn.execute("UPDATE crawl_queue SET state=? WHERE job_id=? AND document_uid=?", (state, job_id, uid))


def queue_counts(conn: sqlite3.Connection, job_id: int) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM crawl_queue WHERE job_id=? GROUP BY state", (job_id,))}


def add_job_log(conn: sqlite3.Connection, job_id: int, level: str, message: str,
                uid: str | None = None) -> None:
    conn.execute("INSERT INTO crawl_log(job_id, created_at, level, document_uid, message) VALUES(?,?,?,?,?)",
                 (job_id, now_iso(), level, uid, message[:2000]))


def get_job_log(conn: sqlite3.Connection, job_id: int, limit: int = 100) -> list[dict]:
    rows = conn.execute("SELECT created_at, level, document_uid, message FROM crawl_log "
                        "WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------- stats

def get_stats(conn: sqlite3.Connection) -> dict:
    by_type_state: dict[str, dict[str, int]] = {"confluence": {}, "jira": {}}
    for r in conn.execute("SELECT source_type, state, COUNT(*) FROM documents GROUP BY 1,2"):
        by_type_state[r[0]][r[1]] = r[2]
    edges_by_kind = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM edges GROUP BY kind")}
    return {
        "documents_total": sum(sum(v.values()) for v in by_type_state.values()),
        "indexed": {t: by_type_state[t].get(STATE_INDEXED, 0) for t in by_type_state},
        "by_type_state": by_type_state,
        "edges_total": sum(edges_by_kind.values()),
        "edges_by_kind": edges_by_kind,
        "chunks_total": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "last_job": get_latest_job(conn),
    }


def iter_rows(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params))]
