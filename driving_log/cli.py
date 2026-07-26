from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from driving_log import __version__
from driving_log.archive import create_archive, restore_archive, verify_archive
from driving_log.config import Settings
from driving_log.csv_backup import export_csv, import_csv
from driving_log.db import Database
from driving_log.migrations import LATEST_SCHEMA_VERSION
from driving_log.seed import apply_seed, preview_seed


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
    seed = sub.add_parser("seed")
    seed.add_argument("--preview", action="store_true")
    seed.add_argument("--pdf", type=Path, default=Path("records/2026-07-02 Daniel driving log.pdf"))
    seed.add_argument("--log", type=Path, default=Path("records/log.txt"))
    csv_parser = sub.add_parser("csv")
    csv_sub = csv_parser.add_subparsers(dest="csv_action", required=True)
    csv_export = csv_sub.add_parser("export")
    csv_export.add_argument("--out", type=Path, required=True)
    csv_import = csv_sub.add_parser("import")
    csv_import.add_argument("--in", dest="input", type=Path, required=True)
    archive = sub.add_parser("archive")
    archive_sub = archive.add_subparsers(dest="archive_action", required=True)
    archive_create = archive_sub.add_parser("create")
    archive_create.add_argument("--out", type=Path)
    archive_sub.add_parser("list")
    archive_verify = archive_sub.add_parser("verify")
    archive_verify.add_argument("archive", type=Path, nargs="?")
    archive_restore = archive_sub.add_parser("restore")
    archive_restore.add_argument("archive", type=Path)
    archive_restore.add_argument("--confirm", action="store_true")
    imports = sub.add_parser("imports")
    imports.add_argument("action", choices=("status",))
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
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    if args.command == "seed":
        result = (
            preview_seed(args.pdf, args.log)
            if args.preview
            else apply_seed(database, args.pdf, args.log)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "csv":
        if args.csv_action == "export":
            args.out.write_bytes(export_csv(database))
            print(args.out)
        else:
            result = import_csv(database, args.input.read_bytes(), args.input.name)
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "archive":
        if args.archive_action == "create":
            print(create_archive(database, settings.archive_dir, args.out))
        elif args.archive_action == "list":
            for path in sorted(settings.archive_dir.glob("*.tar.gz"), reverse=True):
                print(path)
        elif args.archive_action == "verify":
            selected = args.archive
            if selected is None:
                candidates = sorted(settings.archive_dir.glob("*.tar.gz"), reverse=True)
                if not candidates:
                    raise SystemExit("no archives found")
                selected = candidates[0]
            print(json.dumps(verify_archive(selected), indent=2, sort_keys=True))
        else:
            quarantine = restore_archive(settings.database_path, args.archive, confirm=args.confirm)
            print(f"restored; previous database retained at {quarantine}")
        return 0
    if args.command == "imports":
        connection = database.connect()
        rows = connection.execute(
            "SELECT id, source_type, source_name, imported_at, status, summary_json "
            "FROM import_batches ORDER BY imported_at DESC"
        ).fetchall()
        connection.close()
        print(json.dumps([dict(row) for row in rows], indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
