from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from driving_log.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS

BUSY_TIMEOUT_MS = 5_000


class DatabaseError(RuntimeError):
    pass


class SchemaTooNewError(DatabaseError):
    pass


class MigrationMismatchError(DatabaseError):
    pass


def utc_now_text() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.ready = False
        self.read_only_reason: str | None = None

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            connection.close()
            raise DatabaseError(f"failed to enable WAL mode: {mode}")
        connection.execute("PRAGMA synchronous = FULL")
        self._restrict_database_files()
        return connection

    def _restrict_database_files(self) -> None:
        for path in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        existed = self.path.exists()
        connection = self.connect()
        try:
            self._check_integrity(connection)
            self._apply_migrations(connection)
            self._check_integrity(connection)
            self._verify_migrations(connection)
            self.ready = True
            self.read_only_reason = None
        except Exception as exc:
            self.ready = False
            self.read_only_reason = str(exc)
            raise
        finally:
            connection.close()
        if not existed:
            os.chmod(self.path, 0o600)

    @staticmethod
    def _check_integrity(connection: sqlite3.Connection) -> None:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise DatabaseError(f"database quick_check failed: {result}")

    @staticmethod
    def _ensure_migration_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    def _apply_migrations(self, connection: sqlite3.Connection) -> None:
        self._ensure_migration_table(connection)
        current = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if current > LATEST_SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"database schema {current} is newer than supported {LATEST_SCHEMA_VERSION}"
            )
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            try:
                connection.executescript(f"BEGIN IMMEDIATE;\n{migration.sql}\n")
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        utc_now_text(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _verify_migrations(connection: sqlite3.Connection) -> None:
        expected = {migration.version: migration for migration in MIGRATIONS}
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        for row in rows:
            migration = expected.get(row["version"])
            if (
                migration is None
                or row["name"] != migration.name
                or row["checksum"] != migration.checksum
            ):
                raise MigrationMismatchError(f"migration {row['version']} does not match code")

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def health(self) -> dict[str, object]:
        if not self.ready:
            return {"ready": False, "reason": self.read_only_reason or "not initialized"}
        try:
            connection = self.connect()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            writable = connection.execute(
                "SELECT CASE WHEN sqlite_compileoption_used('OMIT_AUTOINIT') THEN 1 ELSE 1 END"
            ).fetchone()[0]
            connection.close()
            return {
                "ready": quick_check == "ok" and version == LATEST_SCHEMA_VERSION and writable == 1,
                "quick_check": quick_check,
                "schema_version": version,
            }
        except Exception as exc:
            return {"ready": False, "reason": str(exc)}

    def audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        request_id: str | None,
        action: str,
        payload_hash: str | None,
        entity_type: str,
        entity_id: str | None,
        outcome: str,
        metadata: dict[str, object] | None = None,
        actor_identity: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events
            (id, request_id, action, payload_hash, entity_type, entity_id, outcome,
             metadata_json, actor_identity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                request_id,
                action,
                payload_hash,
                entity_type,
                entity_id,
                outcome,
                json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
                actor_identity,
                utc_now_text(),
            ),
        )
