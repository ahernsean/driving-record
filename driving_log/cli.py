from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from driving_log import __version__
from driving_log.archive import create_archive, restore_archive, verify_archive
from driving_log.config import Settings
from driving_log.csv_backup import export_csv, import_csv
from driving_log.db import Database
from driving_log.migrations import LATEST_SCHEMA_VERSION
from driving_log.operations import (
    apply_retention,
    install_user_units,
    process_restore_request,
    replicate_archive,
    run_systemctl,
    service_snapshot,
    tailscale_snapshot,
)
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
    for command in ("start", "stop", "restart", "status", "logs"):
        sub.add_parser(command)
    install = sub.add_parser("install")
    install.add_argument("--public-host", required=True)
    install.add_argument("--external-archive-dir", type=Path)
    live = sub.add_parser("live")
    live.add_argument("action", choices=("status",))
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
    archive_sub.add_parser("replicate")
    archive_restore = archive_sub.add_parser("restore")
    archive_restore.add_argument("archive", type=Path)
    archive_restore.add_argument("--confirm", action="store_true")
    archive_request = archive_sub.add_parser("restore-request")
    archive_request.add_argument("operation_id")
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
    database = Database(settings.database_path, settings.archive_dir)
    try:
        database.check_existing()
        connection = database.connect_readonly()
        open_live = connection.execute(
            "SELECT id, status, started_at_utc, provisional_ended_at_utc "
            "FROM live_drives WHERE status IN ('active','ending')"
        ).fetchone()
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
                "open_live_drive": dict(open_live) if open_live else None,
                "incomplete_imports": connection.execute(
                    "SELECT COUNT(*) FROM import_batches WHERE status != 'completed'"
                ).fetchone()[0],
            }
        )
        connection.close()
    except Exception as exc:
        result.update({"ready": False, "error": str(exc)})
    archives = sorted(
        settings.archive_dir.glob("*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    result["newest_archive"] = str(archives[0]) if archives else None
    if archives:
        try:
            result["newest_archive_verification"] = verify_archive(archives[0])
        except Exception as exc:
            result["newest_archive_verification"] = {
                "verified": False,
                "error": str(exc),
            }
    else:
        result["newest_archive_verification"] = {"verified": False}
    services = service_snapshot()
    tailscale = tailscale_snapshot()
    result["services"] = services
    result["tailscale"] = tailscale
    result["external_archive_dir"] = os.environ.get("DRIVING_LOG_EXTERNAL_ARCHIVE_DIR")
    result["warnings"] = []
    if not result["external_archive_dir"]:
        cast_warnings = result["warnings"]
        assert isinstance(cast_warnings, list)
        cast_warnings.append("external archives are not configured")
    production = not settings.public_host.startswith(("127.0.0.1", "localhost", "testserver"))
    if production:
        web_status = services.get("driving-log-web.service", {})
        service_ok = isinstance(web_status, dict) and web_status.get("ActiveState") == "active"
        configuration = tailscale.get("configuration", {})
        tcp = configuration.get("TCP", {}) if isinstance(configuration, dict) else {}
        web = configuration.get("Web", {}) if isinstance(configuration, dict) else {}
        https_ok = (
            isinstance(tcp, dict)
            and isinstance(tcp.get("8443"), dict)
            and bool(tcp["8443"].get("HTTPS"))
        )
        driving_handler = web.get(settings.public_host, {}) if isinstance(web, dict) else {}
        wordle_handler = (
            web.get(settings.public_host.rsplit(":", 1)[0] + ":80", {})
            if isinstance(web, dict)
            else {}
        )
        driving_ok = "8766" in json.dumps(driving_handler)
        wordle_ok = "8765" in json.dumps(wordle_handler)
        archive_status = result["newest_archive_verification"]
        archive_ok = isinstance(archive_status, dict) and bool(archive_status.get("verified"))
        result["deployment_checks"] = {
            "web_service_active": service_ok,
            "archive_verified": archive_ok,
            "tailscale_https_8443": https_ok and driving_ok,
            "wordle_http_80_preserved": wordle_ok,
        }
        result["ready"] = bool(
            result.get("ready")
            and service_ok
            and archive_ok
            and https_ok
            and driving_ok
            and wordle_ok
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.command == "install":
        install_result = install_user_units(
            Path(__file__).parent.parent,
            public_host=args.public_host,
            external_archive_dir=args.external_archive_dir,
        )
        print(json.dumps(install_result, indent=2, sort_keys=True))
        return 0
    if args.command in {"start", "stop", "restart"}:
        if args.command == "start":
            systemctl_result = run_systemctl(
                "enable",
                "--now",
                "driving-log-web.service",
                "driving-log-archive.timer",
            )
        else:
            systemctl_result = run_systemctl(args.command, "driving-log-web.service")
        print(systemctl_result.stdout, end="")
        return 0
    if args.command == "status":
        print(json.dumps(service_snapshot(), indent=2, sort_keys=True))
        return 0
    if args.command == "logs":
        return subprocess.run(
            [
                "journalctl",
                "--user-unit",
                "driving-log-web.service",
                "-n",
                "100",
                "--no-pager",
            ]
        ).returncode
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
        doctor_result = doctor(settings)
        if args.json:
            print(json.dumps(doctor_result, indent=2, sort_keys=True))
        else:
            for key, value in doctor_result.items():
                print(f"{key}: {value}")
        return 0 if doctor_result.get("ready") else 1
    if args.command == "db":
        check_result = doctor(settings)
        print(check_result.get("quick_check", check_result.get("error", "unknown")))
        return 0 if check_result.get("ready") else 1
    if args.command == "archive" and args.archive_action == "restore":
        quarantine = restore_archive(settings.database_path, args.archive, confirm=args.confirm)
        print(f"restored; previous database retained at {quarantine}")
        return 0
    if args.command == "archive" and args.archive_action == "restore-request":
        result_path = process_restore_request(
            args.operation_id,
            settings.restore_dir,
            settings.database_path,
            settings.form_secret,
        )
        print(result_path)
        return 0
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    if args.command == "seed":
        seed_result = (
            preview_seed(args.pdf, args.log)
            if args.preview
            else apply_seed(database, args.pdf, args.log)
        )
        print(json.dumps(seed_result, indent=2, sort_keys=True))
        return 0
    if args.command == "csv":
        if args.csv_action == "export":
            args.out.write_bytes(export_csv(database))
            print(args.out)
        else:
            import_result = import_csv(database, args.input.read_bytes(), args.input.name)
            print(json.dumps(import_result, indent=2, sort_keys=True))
        return 0
    if args.command == "archive":
        if args.archive_action == "create":
            created = create_archive(database, settings.archive_dir, args.out)
            removed = apply_retention(settings.archive_dir)
            print(created)
            if removed:
                print(f"retention removed {len(removed)} expired archives")
        elif args.archive_action == "list":
            for path in sorted(
                settings.archive_dir.glob("*.tar.gz"),
                key=lambda candidate: candidate.stat().st_mtime,
                reverse=True,
            ):
                print(path)
        elif args.archive_action == "verify":
            selected = args.archive
            if selected is None:
                candidates = sorted(
                    settings.archive_dir.glob("*.tar.gz"),
                    key=lambda candidate: candidate.stat().st_mtime,
                    reverse=True,
                )
                if not candidates:
                    raise SystemExit("no archives found")
                selected = candidates[0]
            print(json.dumps(verify_archive(selected), indent=2, sort_keys=True))
        else:
            destination_text = os.environ.get("DRIVING_LOG_EXTERNAL_ARCHIVE_DIR")
            if not destination_text:
                print(
                    "warning: DRIVING_LOG_EXTERNAL_ARCHIVE_DIR is not configured; "
                    "archives remain on the Rocky disk"
                )
                return 0
            candidates = sorted(
                settings.archive_dir.glob("*.tar.gz"),
                key=lambda candidate: candidate.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise SystemExit("no local archive to replicate")
            print(replicate_archive(candidates[0], Path(destination_text)))
        return 0
    if args.command == "live":
        connection = database.connect()
        row = connection.execute(
            "SELECT id, status, started_at_utc, provisional_ended_at_utc "
            "FROM live_drives WHERE status IN ('active','ending')"
        ).fetchone()
        connection.close()
        print(json.dumps(dict(row) if row else None, indent=2, sort_keys=True))
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
