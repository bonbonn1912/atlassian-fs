#!/usr/bin/env python3
"""CLI entry point for local-knowledge application."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Python version check >= 3.10
if sys.version_info < (3, 10):
    sys.stderr.write(
        f"Fehler: Python 3.10 oder neuer wird benötigt (aktuell: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}).\n"
    )
    sys.exit(1)

from knowledge import db
from knowledge.config import ConfigStore, resolve_db_path


def setup_logging() -> None:
    """Configure logging to stderr exclusively so stdout remains clean for MCP / status output."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_init_db(args: argparse.Namespace) -> None:
    try:
        db.check_fts5()
    except db.DatabaseError as exc:
        sys.stderr.write(f"Fehler: {exc}\n")
        sys.exit(1)

    config_store = ConfigStore(args.config)
    cfg = config_store.get()
    db_path = resolve_db_path(cfg)

    conn = db.connect(db_path)
    version = db.init_db(conn)
    print(f"Datenbank initialisiert: {db_path} (Schemaversion {version})")


def cmd_status(args: argparse.Namespace) -> None:
    config_store = ConfigStore(args.config)
    cfg = config_store.get()
    db_path = resolve_db_path(cfg)

    if not Path(db_path).exists():
        sys.stderr.write(
            f"Fehler: Datenbank '{db_path}' existiert nicht. Bitte zuerst 'python app.py init-db' ausführen.\n"
        )
        sys.exit(1)

    conn = db.connect(db_path)
    stats = db.get_stats(conn)
    print(json.dumps(stats, indent=2, ensure_ascii=False))


def cmd_seed_demo(args: argparse.Namespace) -> None:
    from knowledge import demo

    config_store = ConfigStore(args.config)
    cfg = config_store.get()
    db_path = resolve_db_path(cfg)
    conn = db.open_db(db_path)
    result = demo.seed_demo(conn, cfg, reset=args.reset)
    print(f"Demo-Daten geschrieben nach {db_path}: {result['pages']} Confluence-Seiten, "
          f"{result['issues']} Jira-Tickets (Links zeigen auf {result['confluence_base']} / {result['jira_base']})")


def cmd_refresh(args: argparse.Namespace) -> None:
    from knowledge.indexer import JobManager

    config_store = ConfigStore(args.config)
    cfg = config_store.get()
    db_path = resolve_db_path(cfg)

    if not Path(db_path).exists():
        sys.stderr.write(
            f"Fehler: Datenbank '{db_path}' existiert nicht. Bitte zuerst 'python app.py init-db' ausführen.\n"
        )
        sys.exit(1)

    mgr = JobManager(config_store, db_path)
    res = mgr.start({"refresh_only": True})
    job_id = res["job_id"]
    print(f"Aktualisierungsjob gestartet (ID {job_id}). Warte auf Fertigstellung...")
    while mgr.is_running():
        status = mgr.status(job_id)
        if status:
            msg = status.get("last_message") or ""
            pages = status.get("pages_indexed", 0)
            jira = status.get("jira_indexed", 0)
            errs = status.get("errors", 0)
            sys.stderr.write(f"\r[{status.get('state')}] {msg} (Confluence: {pages}, Jira: {jira}, Fehler: {errs})")
            sys.stderr.flush()
        time.sleep(0.5)
    status = mgr.status(job_id) or {}
    sys.stderr.write("\n")
    print(f"Job beendet mit Status '{status.get('state')}': {status.get('last_message')}")


def cmd_mcp(args: argparse.Namespace) -> None:
    config_store = ConfigStore(args.config)
    cfg = config_store.get()
    db_path = resolve_db_path(cfg)

    try:
        from knowledge import mcp_server
    except ImportError as exc:
        sys.stderr.write(f"Fehler beim Laden des MCP-Servers: {exc}\n")
        sys.exit(1)

    mcp_server.run_stdio(db_path, cfg)


def cmd_serve(args: argparse.Namespace) -> None:
    config_store = ConfigStore(args.config)
    from knowledge import webserver
    webserver.serve(config_store, host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Confluence & Jira Knowledge Graph (lokale Suche & MCP-Server)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # serve
    serve_parser = subparsers.add_parser("serve", help="Webserver und API starten")
    serve_parser.add_argument("--host", default=None, help="Host-Adresse (z. B. 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=None, help="Port (z. B. 8765)")
    serve_parser.add_argument("--config", default=None, help="Pfad zu einer alternativen config.json")
    serve_parser.set_defaults(func=cmd_serve)

    # mcp
    mcp_parser = subparsers.add_parser("mcp", help="MCP-Server über stdio starten")
    mcp_parser.add_argument("--config", default=None, help="Pfad zu einer alternativen config.json")
    mcp_parser.set_defaults(func=cmd_mcp)

    # init-db
    init_parser = subparsers.add_parser("init-db", help="SQLite-Datenbank initialisieren/migrieren")
    init_parser.add_argument("--config", default=None, help="Pfad zu einer alternativen config.json")
    init_parser.set_defaults(func=cmd_init_db)

    # status
    status_parser = subparsers.add_parser("status", help="Statistiken der Datenbank als JSON ausgeben")
    status_parser.add_argument("--config", default=None, help="Pfad zu einer alternativen config.json")
    status_parser.set_defaults(func=cmd_status)

    # seed-demo
    demo_parser = subparsers.add_parser("seed-demo", help="Demo-Daten (~100 Seiten, ~60 Tickets) ohne Atlassian-Zugriff anlegen")
    demo_parser.add_argument("--reset", action="store_true", help="Vorher ALLE Dokumente, Kanten und Jobs löschen")
    demo_parser.add_argument("--config", default=None, help="Pfad zu einer alternativen config.json")
    demo_parser.set_defaults(func=cmd_seed_demo)

    # refresh
    refresh_parser = subparsers.add_parser("refresh", help="Nur bestehende Dokumente aus Confluence/Jira aktualisieren")
    refresh_parser.add_argument("--config", default=None, help="Pfad zu einer alternativen config.json")
    refresh_parser.set_defaults(func=cmd_refresh)

    return parser


def main() -> None:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()
    from knowledge.models import KnowledgeError
    try:
        args.func(args)
    except KnowledgeError as exc:
        sys.stderr.write(f"Fehler: {exc}\n")
        sys.exit(1)
    except OSError as exc:
        sys.stderr.write(f"Fehler: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
