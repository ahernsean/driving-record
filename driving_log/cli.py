from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys

from driving_log import __version__
from driving_log.config import Settings
from driving_log.db import Database
from driving_log.migrations import LATEST_SCHEMA_VERSION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="driving-log")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    db = sub.add_parser("db")
    db.add_argument("action", choices=("check",))
    return parser


def doctor(settings: Settings) -> dict[str, object]:
    result: dict[str, object] = {
        "application_version": __version__,
        "python_version": sys.version.split()[0],
        "sqlite_version": sqlite3.sqlite_version,
        "supported_schema_version": LATEST_SCHEMA_VERSION,
        "database_path": str(settings.database_path.resolve()),
        "archive_path": str(settings.archive_dir.resolve()),
        "state_free_bytes": shutil.disk_usage(settings.state_dir).free
        if settings.state_dir.exists()
        else None,
    }
    database = Database(settings.database_path)
    try:
        database.initialize()
        connection = database.connect()
        result.update(
            {
                "ready": True,
                "schema_version": connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout_ms": connection.execute("PRAGMA busy_timeout").fetchone()[0],
                "database_mode": oct(settings.database_path.stat().st_mode & 0o777),
            }
        )
        connection.close()
    except Exception as exc:
        result.update({"ready": False, "error": str(exc)})
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.command == "serve":
        import uvicorn

        host = args.host or settings.host
        port = args.port or settings.port
        if host != settings.host:
            os.environ["DRIVING_LOG_HOST"] = host
            settings = Settings.from_env()
        uvicorn.run("driving_log.app:create_app", host=host, port=port, factory=True)
        return 0
    if args.command == "doctor":
        result = doctor(settings)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
        return 0 if result.get("ready") else 1
    if args.command == "db":
        result = doctor(settings)
        print(result.get("quick_check", result.get("error", "unknown")))
        return 0 if result.get("ready") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
