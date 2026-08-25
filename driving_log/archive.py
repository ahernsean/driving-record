from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from driving_log import __version__
from driving_log.db import Database
from driving_log.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS

ARCHIVE_FORMAT = "driving-log-archive-v1"
REQUIRED_TABLES = {
    "schema_migrations",
    "configuration",
    "drives",
    "drive_warnings",
    "live_drives",
    "import_batches",
    "import_rows",
    "audit_events",
}
SCHEMA_TABLES = {
    3: {"supervisor_profiles"},
    4: {"saved_locations"},
}


class ArchiveSchemaTooNewError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def application_lock(state_dir: Path, *, exclusive: bool) -> Iterator[None]:
    import fcntl

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_dir / ".application.lock"
    with lock_path.open("a+b") as lock:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            action = "restore" if exclusive else "start the application"
            raise RuntimeError(
                f"cannot {action} while driving-log-web.service is running; "
                "stop the web service first"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def create_archive(database: Database, archive_dir: Path, out: Path | None = None) -> Path:
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_dir = out.parent if out else archive_dir
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = out or target_dir / f"driving-log-{stamp}.tar.gz"
    if target.exists():
        raise FileExistsError(f"refusing to overwrite archive: {target}")
    with tempfile.TemporaryDirectory(dir=target_dir) as temporary:
        workspace = Path(temporary)
        snapshot = workspace / "database.sqlite3"
        source = database.connect()
        destination = sqlite3.connect(snapshot)
        source.backup(destination)
        destination.close()
        source.close()
        snapshot_connection = sqlite3.connect(snapshot)
        check = snapshot_connection.execute("PRAGMA quick_check").fetchone()[0]
        snapshot_connection.close()
        if check != "ok":
            raise RuntimeError(f"snapshot quick_check failed: {check}")
        schema_connection = sqlite3.connect(snapshot)
        schema_row = schema_connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        schema_connection.close()
        schema_version = int(schema_row[0])
        if schema_version > LATEST_SCHEMA_VERSION:
            raise ArchiveSchemaTooNewError(
                f"database schema {schema_version} is newer than the running application "
                f"supports ({LATEST_SCHEMA_VERSION}); restart driving-log-web.service and retry"
            )
        manifest = {
            "archive_format": ARCHIVE_FORMAT,
            "application_version": __version__,
            "schema_version": schema_version,
            "created_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "database_size": snapshot.stat().st_size,
            "database_sha256": _sha256(snapshot),
        }
        (workspace / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary_bundle = target_dir / f".{target.name}.tmp-{os.getpid()}"
        with tarfile.open(temporary_bundle, "w:gz") as bundle:
            bundle.add(snapshot, arcname="database.sqlite3")
            bundle.add(workspace / "manifest.json", arcname="manifest.json")
        with temporary_bundle.open("rb") as bundle_file:
            os.fsync(bundle_file.fileno())
        os.chmod(temporary_bundle, 0o600)
        os.replace(temporary_bundle, target)
        directory_fd = os.open(target_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    verify_archive(target)
    return target


def verify_archive(path: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        with tarfile.open(path, "r:gz") as bundle:
            names = set(bundle.getnames())
            if names != {"database.sqlite3", "manifest.json"}:
                raise ValueError("archive contains unexpected or missing entries")
            for member in bundle.getmembers():
                if (
                    not member.isfile()
                    or Path(member.name).is_absolute()
                    or ".." in Path(member.name).parts
                ):
                    raise ValueError("archive contains an unsafe entry")
            bundle.extractall(workspace, filter="data")
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("archive_format") != ARCHIVE_FORMAT:
            raise ValueError("unsupported archive format")
        if int(manifest.get("schema_version", -1)) > LATEST_SCHEMA_VERSION:
            raise ArchiveSchemaTooNewError("archive schema is newer than this application")
        snapshot = workspace / "database.sqlite3"
        if _sha256(snapshot) != manifest.get("database_sha256"):
            raise ValueError("archive database hash mismatch")
        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        schema_version = int(manifest.get("schema_version", -1))
        required_tables = set(REQUIRED_TABLES)
        for version, version_tables in SCHEMA_TABLES.items():
            if schema_version >= version:
                required_tables.update(version_tables)
        if not required_tables.issubset(tables):
            connection.close()
            raise ValueError("archive database is missing required authoritative tables")
        migrations = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        connection.close()
        if check != "ok":
            raise ValueError(f"archive database quick_check failed: {check}")
        expected = {migration.version: migration for migration in MIGRATIONS}
        for version, name, checksum in migrations:
            migration = expected.get(version)
            if not migration or migration.name != name or migration.checksum != checksum:
                raise ValueError(f"archive migration {version} does not match this application")
        actual_schema = max((int(row[0]) for row in migrations), default=0)
        if actual_schema != int(manifest.get("schema_version", -1)):
            raise ValueError("archive manifest schema does not match its database")
        return {**manifest, "verified": True, "archive_sha256": _sha256(path)}


def restore_archive(database_path: Path, archive: Path, *, confirm: bool) -> Path:
    if not confirm:
        raise ValueError("restore requires explicit confirmation")
    verify_archive(archive)
    database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with application_lock(database_path.parent, exclusive=True):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = database_path.parent / f"quarantine-{stamp}"
        quarantine.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(dir=database_path.parent) as temporary:
            workspace = Path(temporary)
            with tarfile.open(archive, "r:gz") as bundle:
                member = bundle.getmember("database.sqlite3")
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("archive database is missing")
                restored = workspace / "restored.sqlite3"
                with restored.open("wb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            os.chmod(restored, 0o600)
            for suffix in ("", "-wal", "-shm"):
                current = Path(f"{database_path}{suffix}")
                if current.exists():
                    os.replace(current, quarantine / current.name)
            os.replace(restored, database_path)
            directory_fd = os.open(database_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        try:
            restored_database = Database(database_path)
            restored_database.initialize()
        except Exception:
            failed = quarantine / "failed-restored.sqlite3"
            if database_path.exists():
                os.replace(database_path, failed)
            previous = quarantine / database_path.name
            if previous.exists():
                os.replace(previous, database_path)
            raise
        return quarantine
