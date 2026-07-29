from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from driving_log.archive import (
    ARCHIVE_FORMAT,
    application_lock,
    create_archive,
    restore_archive,
    verify_archive,
)
from driving_log.csv_backup import CSV_COLUMNS, LEGACY_CSV_COLUMNS, export_csv, import_csv
from driving_log.db import Database, utc_now_text
from driving_log.records import ConflictError, DriveInput, NotFoundError, RecordService
from driving_log.seed import (
    apply_seed,
    extract_pdf,
    parse_log_text,
    parse_pdf_text,
    preview_seed,
)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state" / "driving.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.service = RecordService(self.database)
        self.zone = ZoneInfo("America/New_York")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def drive(
        self,
        start: datetime | None = None,
        minutes: int = 30,
        **changes: object,
    ) -> DriveInput:
        selected_start = start or datetime(2026, 7, 20, 12, tzinfo=self.zone)
        values: dict[str, object] = {
            "driver_name": "Daniel Ahern",
            "supervisor_name": "Sean Ahern",
            "supervisor_dl_number": None,
            "supervisor_dl_state": None,
            "started_at_utc": selected_start.astimezone(UTC),
            "ended_at_utc": (selected_start + timedelta(minutes=minutes)).astimezone(UTC),
            "road_type": "local",
        }
        values.update(changes)
        return DriveInput(**values)  # type: ignore[arg-type]


class RecordTests(ServiceCase):
    def test_rejects_fractional_timestamp_even_with_whole_minute_duration(self) -> None:
        start = datetime(2026, 7, 20, 16, 0, 0, 123456, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "minute-aligned"):
            self.service.create(
                self.drive(
                    started_at_utc=start,
                    ended_at_utc=start + timedelta(minutes=30),
                )
            )

    def test_repeated_local_time_is_resolved_but_visibly_flagged(self) -> None:
        start = datetime(2026, 11, 1, 1, 30, tzinfo=self.zone, fold=0).astimezone(UTC)
        drive = self.service.create(
            self.drive(
                started_at_utc=start,
                ended_at_utc=start + timedelta(minutes=30),
            )
        )
        warnings = self.service.warnings_for(drive["id"])
        self.assertIn("ambiguous_local_time", {warning["code"] for warning in warnings})
        self.assertIn("first occurrence", warnings[0]["message"])

    def test_create_retry_conflict_edit_and_delete(self) -> None:
        request = str(uuid.uuid4())
        created = self.service.create(self.drive(), request_id=request)
        retried = self.service.create(self.drive(), request_id=request)
        self.assertEqual(created["id"], retried["id"])
        with self.assertRaises(ConflictError):
            self.service.create(self.drive(minutes=31), request_id=request)

        edit_request = str(uuid.uuid4())
        updated = self.service.update(
            created["id"],
            self.drive(minutes=45, notes="corrected"),
            expected_version=1,
            request_id=edit_request,
        )
        self.assertEqual(updated["version"], 2)
        retried_update = self.service.update(
            created["id"],
            self.drive(minutes=45, notes="corrected"),
            expected_version=1,
            request_id=edit_request,
        )
        self.assertEqual(retried_update["version"], 2)
        with self.assertRaises(ConflictError):
            self.service.update(
                created["id"],
                self.drive(minutes=46),
                expected_version=1,
                request_id=str(uuid.uuid4()),
            )

        deleted = self.service.delete(
            created["id"], expected_version=2, request_id=str(uuid.uuid4())
        )
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(self.service.totals()["drive_count"], 0)

    def test_update_request_is_bound_to_target_drive(self) -> None:
        first = self.service.create(self.drive())
        second = self.service.create(self.drive(datetime(2026, 7, 20, 13, tzinfo=self.zone)))
        request = str(uuid.uuid4())
        self.service.update(
            first["id"],
            self.drive(minutes=45),
            expected_version=1,
            request_id=request,
        )
        with self.assertRaises(ConflictError):
            self.service.update(
                second["id"],
                self.drive(minutes=45),
                expected_version=1,
                request_id=request,
            )

    def test_overlap_intrinsic_and_weekly_warnings_are_derived(self) -> None:
        start = datetime(2026, 7, 19, 8, tzinfo=self.zone)
        first = self.service.create(self.drive(start, 301))
        second = self.service.create(self.drive(start + timedelta(minutes=300), 300))
        warnings = self.service.warnings_for(first["id"])
        self.assertEqual({warning["code"] for warning in warnings}, {"long_drive", "overlap"})
        second_warnings = self.service.warnings_for(second["id"])
        self.assertEqual(
            {warning["code"] for warning in second_warnings},
            {"overlap", "weekly_overage"},
        )
        totals = self.service.totals()
        self.assertEqual(totals["total_minutes"], 601)
        self.assertEqual(totals["weeks"][0]["overage_minutes"], 1)  # type: ignore[index]
        self.service.delete(first["id"], expected_version=1, request_id=str(uuid.uuid4()))
        self.assertEqual(self.service.warnings_for(second["id"]), [])

    def test_historical_source_duplicate_is_replaced_by_current_overlap(self) -> None:
        first = self.service.create(self.drive())
        second = self.service.create(self.drive())
        with self.database.transaction() as connection:
            for drive in (first, second):
                connection.execute(
                    """
                    INSERT INTO drive_warnings VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        drive["id"],
                        "seed_possible_duplicate",
                        "Source contains another entry with the same start and duration",
                        utc_now_text(),
                    ),
                )
        self.assertEqual(
            {warning["code"] for warning in self.service.warnings_for(second["id"])},
            {"overlap"},
        )
        self.service.update(
            second["id"],
            self.drive(datetime(2026, 7, 20, 13, tzinfo=self.zone)),
            expected_version=1,
            request_id=str(uuid.uuid4()),
        )
        self.assertEqual(self.service.warnings_for(first["id"]), [])
        self.assertEqual(self.service.warnings_for(second["id"]), [])

    def test_seed_diagnostic_is_not_a_current_drive_warning(self) -> None:
        drive = self.service.create(self.drive())
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO drive_warnings VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    drive["id"],
                    "seed_ambiguous_duration",
                    "Duration was inferred from the written start and end timestamps",
                    utc_now_text(),
                ),
            )
        self.assertEqual(self.service.warnings_for(drive["id"]), [])

    def test_actionable_import_warning_is_cleared_by_reviewing_drive(self) -> None:
        drive = self.service.create(self.drive())
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO drive_warnings VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    drive["id"],
                    "seed_ambiguous_duration",
                    "Written duration does not match start and end timestamps; duration was used",
                    utc_now_text(),
                ),
            )
        self.assertEqual(
            self.service.warnings_for(drive["id"]),
            [
                {
                    "code": "seed_ambiguous_duration",
                    "message": (
                        "Written duration does not match start and end timestamps; "
                        "duration was used"
                    ),
                    "action": "review_import",
                }
            ],
        )
        self.service.update(
            drive["id"],
            self.drive(),
            expected_version=1,
            request_id=str(uuid.uuid4()),
        )
        self.assertEqual(self.service.warnings_for(drive["id"]), [])
        connection = self.database.connect_readonly()
        try:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM drive_warnings WHERE drive_id=?", (drive["id"],)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(remaining, 0)

    def test_edit_recomputes_intrinsic_warning_from_current_values(self) -> None:
        drive = self.service.create(self.drive(minutes=301))
        self.assertEqual(
            {warning["code"] for warning in self.service.warnings_for(drive["id"])},
            {"long_drive"},
        )
        self.service.update(
            drive["id"],
            self.drive(minutes=30),
            expected_version=1,
            request_id=str(uuid.uuid4()),
        )
        self.assertEqual(self.service.warnings_for(drive["id"]), [])

    def test_retry_stale_delete_missing_reverse_and_midnight_guards(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive duration"):
            self.service.create(self.drive(minutes=-1))
        with self.assertRaisesRegex(NotFoundError, "drive not found"):
            self.service.get(str(uuid.uuid4()))

        created = self.service.create(self.drive())
        first_request = str(uuid.uuid4())
        self.service.update(
            created["id"],
            self.drive(minutes=40),
            expected_version=1,
            request_id=first_request,
        )
        self.service.update(
            created["id"],
            self.drive(minutes=50),
            expected_version=2,
            request_id=str(uuid.uuid4()),
        )
        with self.assertRaisesRegex(ConflictError, "since changed"):
            self.service.update(
                created["id"],
                self.drive(minutes=40),
                expected_version=1,
                request_id=first_request,
            )
        with self.assertRaisesRegex(ConflictError, "changed before deletion"):
            self.service.delete(
                created["id"],
                expected_version=1,
                request_id=str(uuid.uuid4()),
            )
        delete_request = str(uuid.uuid4())
        deleted = self.service.delete(
            created["id"],
            expected_version=3,
            request_id=delete_request,
        )
        self.assertEqual(
            self.service.delete(
                created["id"],
                expected_version=3,
                request_id=delete_request,
            )["id"],
            deleted["id"],
        )

        midnight = self.service.create(
            self.drive(
                datetime(2026, 7, 20, 23, 45, tzinfo=self.zone),
                minutes=30,
            )
        )
        self.assertIn(
            "crosses_midnight",
            {warning["code"] for warning in self.service.warnings_for(midnight["id"])},
        )


class CsvTests(ServiceCase):
    def test_round_trip_and_duplicate_file_are_idempotent(self) -> None:
        first = self.service.create(
            self.drive(end_location="Apex Friendship High School"),
            request_id=str(uuid.uuid4()),
        )
        content = export_csv(self.database)
        other_path = Path(self.temporary.name) / "other.sqlite3"
        other = Database(other_path)
        other.initialize()
        summary = import_csv(other, content, "backup.csv")
        self.assertEqual(summary["created"], 1)
        self.assertEqual(import_csv(other, content, "backup.csv"), summary)
        self.assertEqual(RecordService(other).list_drives()[0]["id"], first["id"])
        self.assertEqual(
            RecordService(other).list_drives()[0]["end_location"],
            "Apex Friendship High School",
        )
        self.assertEqual(export_csv(other), content)

    def test_imports_legacy_csv_without_an_end_location(self) -> None:
        self.service.create(self.drive(end_location="Home"))
        current = list(csv.DictReader(io.StringIO(export_csv(self.database).decode())))[0]
        legacy = {column: current[column] for column in LEGACY_CSV_COLUMNS}
        legacy["format_version"] = "1"
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=LEGACY_CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(legacy)
        content = output.getvalue().encode()
        summary = import_csv(self.database, content, "legacy.csv")
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["warnings"], 1)
        audit = self.database.connect_readonly()
        batch = audit.execute(
            "SELECT format_version FROM import_batches WHERE id=?",
            (summary["batch_id"],),
        ).fetchone()
        row = audit.execute(
            "SELECT error_message FROM import_rows WHERE import_batch_id=?",
            (summary["batch_id"],),
        ).fetchone()
        audit.close()
        self.assertEqual(batch["format_version"], "1")
        self.assertIn("not compared", row["error_message"])
        other = Database(Path(self.temporary.name) / "legacy.sqlite3")
        other.initialize()
        import_csv(other, content, "legacy.csv")
        self.assertEqual(RecordService(other).list_drives()[0]["end_location"], "")

    def test_rejects_duplicate_ids_before_mutation(self) -> None:
        self.service.create(self.drive())
        content = export_csv(self.database).decode()
        rows = list(csv.reader(io.StringIO(content)))
        duplicated = io.StringIO(newline="")
        writer = csv.writer(duplicated, lineterminator="\n")
        writer.writerows([rows[0], rows[1], rows[1]])
        other = Database(Path(self.temporary.name) / "empty.sqlite3")
        other.initialize()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            import_csv(other, duplicated.getvalue().encode(), "bad.csv")
        self.assertEqual(RecordService(other).list_drives(), [])

    def test_rejects_conflicting_existing_uuid_atomically(self) -> None:
        drive_id = str(uuid.uuid4())
        self.service.create(self.drive(), drive_id=drive_id)
        content = export_csv(self.database)
        rows = list(csv.DictReader(io.StringIO(content.decode())))
        rows[0]["duration_minutes"] = "999"
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        with self.assertRaises(ConflictError):
            import_csv(self.database, output.getvalue().encode(), "conflict.csv")

    def test_rejects_inconsistent_derived_fields_before_commit(self) -> None:
        self.service.create(self.drive())
        rows = list(csv.DictReader(io.StringIO(export_csv(self.database).decode())))
        rows[0]["night_minutes"] = str(int(rows[0]["night_minutes"]) + 1)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        other = Database(Path(self.temporary.name) / "derived.sqlite3")
        other.initialize()
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            import_csv(other, output.getvalue().encode(), "derived.csv")
        self.assertEqual(RecordService(other).list_drives(), [])

    def test_validation_incomplete_retry_and_unchanged_existing_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            import_csv(self.database, b"\xff", "invalid.csv")
        with self.assertRaisesRegex(ValueError, "headers"):
            import_csv(self.database, b"id,wrong\n1,2\n", "headers.csv")

        self.service.create(self.drive())
        content = export_csv(self.database)
        rows = list(csv.DictReader(io.StringIO(content.decode())))
        invalid_version = io.StringIO(newline="")
        writer = csv.DictWriter(invalid_version, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        rows[0]["format_version"] = "999"
        writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "unsupported format"):
            import_csv(self.database, invalid_version.getvalue().encode(), "version.csv")

        rows[0]["format_version"] = "2"
        rows[0]["started_at_utc"] = "not-a-date"
        invalid_date = io.StringIO(newline="")
        writer = csv.DictWriter(invalid_date, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "invalid drive data"):
            import_csv(self.database, invalid_date.getvalue().encode(), "date.csv")

        summary = import_csv(self.database, content, "same.csv")
        self.assertEqual(summary["skipped"], 1)
        incomplete = b"format_version,id\n"
        digest = hashlib.sha256(incomplete).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO import_batches
                (id, source_type, source_name, content_sha256, format_version,
                 imported_at, raw_snapshot, status, summary_json)
                VALUES (?, 'csv', 'incomplete.csv', ?, '1', ?, ?, 'applying', '{}')
                """,
                (
                    str(uuid.uuid4()),
                    digest,
                    datetime.now(UTC).isoformat(),
                    gzip.compress(incomplete),
                ),
            )
        with self.assertRaisesRegex(ConflictError, "prior import is incomplete"):
            import_csv(self.database, incomplete, "incomplete.csv")


class ArchiveTests(ServiceCase):
    @pytest.mark.security
    def test_archive_verifies_and_restore_preserves_full_state(self) -> None:
        drive = self.service.create(self.drive())
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO configuration VALUES ('driver_name','Daniel Ahern','now')"
            )
        archive = create_archive(self.database, Path(self.temporary.name) / "archives")
        manifest = verify_archive(archive)
        self.assertTrue(manifest["verified"])
        self.service.delete(drive["id"], expected_version=1, request_id=str(uuid.uuid4()))
        quarantine = restore_archive(self.path, archive, confirm=True)
        self.assertTrue(quarantine.is_dir())
        restored = RecordService(Database(self.path))
        restored.database.initialize()
        self.assertIsNone(restored.get(drive["id"])["deleted_at"])

    @pytest.mark.security
    def test_restore_requires_confirmation_and_hash_is_checked(self) -> None:
        archive = create_archive(self.database, Path(self.temporary.name) / "archives")
        with self.assertRaisesRegex(ValueError, "confirmation"):
            restore_archive(self.path, archive, confirm=False)
        damaged = Path(self.temporary.name) / "damaged.tar.gz"
        damaged_root = Path(self.temporary.name) / "damaged"
        damaged_root.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(damaged_root, filter="data")
        database_bytes = bytearray((damaged_root / "database.sqlite3").read_bytes())
        database_bytes[-1] ^= 0xFF
        (damaged_root / "database.sqlite3").write_bytes(database_bytes)
        with tarfile.open(damaged, "w:gz") as bundle:
            bundle.add(damaged_root / "database.sqlite3", arcname="database.sqlite3")
            bundle.add(damaged_root / "manifest.json", arcname="manifest.json")
        with self.assertRaises((ValueError, tarfile.TarError, EOFError)):
            verify_archive(damaged)

    def test_archive_creation_refuses_overwrite(self) -> None:
        target = Path(self.temporary.name) / "fixed.tar.gz"
        create_archive(self.database, target.parent, target)
        with self.assertRaises(FileExistsError):
            create_archive(self.database, target.parent, target)

    @pytest.mark.security
    def test_archive_rejects_hash_valid_but_empty_database(self) -> None:
        root = Path(self.temporary.name)
        empty = root / "empty.sqlite3"
        sqlite3.connect(empty).close()
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "archive_format": ARCHIVE_FORMAT,
                    "application_version": "test",
                    "schema_version": 0,
                    "created_at_utc": "2026-07-26T00:00:00Z",
                    "database_size": empty.stat().st_size,
                    "database_sha256": hashlib.sha256(empty.read_bytes()).hexdigest(),
                }
            )
        )
        archive = root / "empty.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(empty, arcname="database.sqlite3")
            bundle.add(manifest, arcname="manifest.json")
        with self.assertRaisesRegex(ValueError, "authoritative tables"):
            verify_archive(archive)

    def test_restore_refuses_while_application_holds_shared_lock(self) -> None:
        archive = create_archive(self.database, Path(self.temporary.name) / "archives")
        with (
            application_lock(self.path.parent, exclusive=False),
            self.assertRaisesRegex(RuntimeError, "stop the web service first"),
        ):
            restore_archive(self.path, archive, confirm=True)

    def test_archive_structure_manifest_migration_and_restore_rollback_guards(self) -> None:
        root = Path(self.temporary.name)
        archive = create_archive(self.database, root / "archives")

        def variant(
            name: str,
            mutate: object | None = None,
            *,
            extra: bool = False,
        ) -> Path:
            workspace = root / name
            workspace.mkdir()
            with tarfile.open(archive, "r:gz") as bundle:
                bundle.extractall(workspace, filter="data")
            if callable(mutate):
                mutate(workspace)
            if extra:
                (workspace / "extra").write_text("unexpected")
            output = root / f"{name}.tar.gz"
            with tarfile.open(output, "w:gz") as bundle:
                bundle.add(workspace / "database.sqlite3", arcname="database.sqlite3")
                bundle.add(workspace / "manifest.json", arcname="manifest.json")
                if extra:
                    bundle.add(workspace / "extra", arcname="extra")
            return output

        with self.assertRaisesRegex(ValueError, "unexpected"):
            verify_archive(variant("extra", extra=True))

        unsafe = root / "unsafe.tar.gz"
        extracted = root / "unsafe-source"
        extracted.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extract("manifest.json", extracted, filter="data")
        with tarfile.open(unsafe, "w:gz") as bundle:
            link = tarfile.TarInfo("database.sqlite3")
            link.type = tarfile.SYMTYPE
            link.linkname = "/tmp/outside"
            bundle.addfile(link)
            bundle.add(extracted / "manifest.json", arcname="manifest.json")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            verify_archive(unsafe)

        def set_manifest(workspace: Path, **changes: object) -> None:
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.update(changes)
            manifest_path.write_text(json.dumps(manifest))

        with self.assertRaisesRegex(ValueError, "unsupported archive format"):
            verify_archive(
                variant(
                    "format",
                    lambda workspace: set_manifest(workspace, archive_format="other"),
                )
            )
        with self.assertRaisesRegex(ValueError, "schema is newer"):
            verify_archive(
                variant(
                    "future",
                    lambda workspace: set_manifest(workspace, schema_version=999),
                )
            )
        with self.assertRaisesRegex(ValueError, "manifest schema"):
            verify_archive(
                variant(
                    "schema-mismatch",
                    lambda workspace: set_manifest(workspace, schema_version=0),
                )
            )

        def damage_migration(workspace: Path) -> None:
            database_path = workspace / "database.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute("UPDATE schema_migrations SET checksum='different'")
            connection.commit()
            connection.close()
            set_manifest(
                workspace,
                database_size=database_path.stat().st_size,
                database_sha256=hashlib.sha256(database_path.read_bytes()).hexdigest(),
            )

        with self.assertRaisesRegex(ValueError, "migration"):
            verify_archive(variant("migration", damage_migration))

        with self.database.transaction() as connection:
            connection.execute("INSERT INTO configuration VALUES ('rollback-test','current','now')")
        with (
            mock.patch(
                "driving_log.archive.Database.initialize",
                side_effect=RuntimeError("rehearsal failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "rehearsal failed"),
        ):
            restore_archive(self.path, archive, confirm=True)
        restored = sqlite3.connect(self.path)
        self.assertEqual(
            restored.execute(
                "SELECT value FROM configuration WHERE key='rollback-test'"
            ).fetchone()[0],
            "current",
        )
        restored.close()
        self.assertTrue(list(self.path.parent.glob("quarantine-*/failed-restored.sqlite3")))

    def test_uncommitted_subprocess_crash_rolls_back_cleanly(self) -> None:
        script = (
            "import os, sqlite3, sys;"
            "connection=sqlite3.connect(sys.argv[1], isolation_level=None);"
            "connection.execute('PRAGMA journal_mode=WAL');"
            "connection.execute('BEGIN IMMEDIATE');"
            "connection.execute("
            "\"INSERT INTO configuration VALUES ('crash-test','uncommitted','now')\""
            ");"
            "os._exit(19)"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", script, str(self.path)],
            check=False,
        )
        self.assertEqual(crashed.returncode, 19)
        connection = self.database.connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM configuration WHERE key='crash-test'"
            ).fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            connection.close()


class SeedParserTests(unittest.TestCase):
    def test_pdf_preserves_duplicate_rows(self) -> None:
        text = "\n".join(
            [
                "08/10/2025 6:27 PM      0h 8m           -       "
                "0 miles       Local      Sean Ahern",
                "08/10/2025 6:27 PM      0h 8m           -       "
                "0 miles       Local      Sean Ahern",
            ]
        )
        rows = parse_pdf_text(text, "seed.pdf")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.warnings for row in rows))
        self.assertTrue(all(row.drive.road_type == "local" for row in rows))
        self.assertNotEqual(rows[0].drive_id, rows[1].drive_id)

    def test_seed_duplicates_are_persisted_as_drives_not_warnings(self) -> None:
        pdf_text = "\n".join(
            [
                "08/10/2025 6:27 PM 0h 8m Local Sean Ahern",
                "08/10/2025 6:27 PM 0h 8m Local Sean Ahern",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf_path = root / "seed.pdf"
            log_path = root / "log.txt"
            pdf_path.write_bytes(b"pdf")
            log_path.write_text("* 2026-07-24 11:10-11:31: local roads")
            database = Database(root / "state.sqlite3")
            database.initialize()
            with mock.patch("driving_log.seed.extract_pdf", return_value=pdf_text):
                self.assertEqual(apply_seed(database, pdf_path, log_path)["created"], 3)

            connection = database.connect_readonly()
            try:
                persisted = connection.execute("SELECT COUNT(*) FROM drive_warnings").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(persisted, 0)
            records = RecordService(database)
            duplicate_drives = [
                drive for drive in records.list_drives() if drive["source"] == "seed_pdf"
            ]
            self.assertEqual(len(duplicate_drives), 2)
            for drive in duplicate_drives:
                self.assertEqual(
                    {warning["code"] for warning in records.warnings_for(drive["id"])},
                    {"overlap"},
                )

    def test_text_parser_understands_ampm_and_infers_final_duration(self) -> None:
        text = "\n".join(
            [
                "* 2026-06-27 1:34 PM: 10 minutes, daytime, regular roads, clear weather",
                "* 2026-07-24 11:10-11:31: local and highways with wet roads",
            ]
        )
        rows = parse_log_text(text, "log.txt")
        local = rows[0].drive.started_at_utc.astimezone(ZoneInfo("America/New_York"))
        self.assertEqual(local.hour, 13)
        self.assertEqual(
            (rows[1].drive.ended_at_utc - rows[1].drive.started_at_utc).total_seconds(),
            21 * 60,
        )
        self.assertEqual(rows[1].warnings, ())

    def test_seed_parser_normalizes_and_flags_nonexistent_dst_time(self) -> None:
        text = "* 2026-03-08 2:30 AM: 10 minutes, local roads"
        row = parse_log_text(text, "log.txt")[0]
        local = row.drive.started_at_utc.astimezone(ZoneInfo("America/New_York"))
        self.assertEqual((local.hour, local.minute), (3, 30))
        self.assertEqual(row.warnings[0][0], "seed_nonexistent_local_time")

    def test_seed_parser_flags_first_fold_and_keeps_unknown_road_type(self) -> None:
        text = "* 2026-11-01 1:30 AM: 10 minutes, clear weather"
        row = parse_log_text(text, "log.txt")[0]
        self.assertEqual(row.drive.road_type, "unknown")
        self.assertEqual(row.warnings[0][0], "seed_ambiguous_local_time")

    def test_parser_validation_and_format_variants(self) -> None:
        with self.assertRaisesRegex(ValueError, "no drive rows"):
            parse_pdf_text("heading only", "seed.pdf")
        with self.assertRaisesRegex(ValueError, "unexpected PDF duration"):
            parse_pdf_text(
                "08/10/2025 6:27 PM no duration Local Sean Ahern",
                "seed.pdf",
            )
        with self.assertRaisesRegex(ValueError, "no drive rows"):
            parse_log_text("heading only", "log.txt")
        with self.assertRaisesRegex(ValueError, "no usable duration"):
            parse_log_text("* 2026-07-24 11:10: local roads", "log.txt")

        pdf = parse_pdf_text(
            "08/10/2025 6:27 PM Highway - 0h 8m Sean Ahern",
            "seed.pdf",
        )[0]
        self.assertEqual(pdf.drive.road_type, "highway")
        self.assertEqual(pdf.source_night_minutes, 8)
        log = parse_log_text(
            "\n".join(
                [
                    "* 2026-07-24 11:10 PM-12:10 AM: 0:45 hours highway rain fog",
                    "* 2026-07-25 10:00: 15 minutes highway clear",
                ]
            ),
            "log.txt",
        )
        self.assertEqual(log[0].drive.road_type, "highway")
        self.assertIn("seed_ambiguous_duration", {warning[0] for warning in log[0].warnings})
        self.assertEqual(log[0].drive.weather, "rain, fog")

    def test_extract_preview_apply_retry_and_conflict(self) -> None:
        pdf_text = "08/10/2025 6:27 PM Highway - 0h 8m Sean Ahern"
        log_text = "* 2026-07-24 11:10-11:31: local and highways with wet roads"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf_path = root / "seed.pdf"
            log_path = root / "log.txt"
            pdf_path.write_bytes(b"pdf-v1")
            log_path.write_text(log_text)
            database = Database(root / "state.sqlite3")
            database.initialize()
            completed = CompletedProcess(["pdftotext"], 0, pdf_text, "")
            with mock.patch("driving_log.seed.subprocess.run", return_value=completed) as run:
                self.assertEqual(extract_pdf(pdf_path), pdf_text)
                run.assert_called_with(
                    ["pdftotext", "-layout", str(pdf_path), "-"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                preview = preview_seed(pdf_path, log_path)
                self.assertEqual(preview["total_rows"], 2)
                applied = apply_seed(database, pdf_path, log_path)
                self.assertEqual(applied["created"], 2)
                retried = apply_seed(database, pdf_path, log_path)
                self.assertEqual(retried["unchanged"], 2)

                pdf_path.write_bytes(b"pdf-v2")
                changed = "08/10/2025 6:27 PM Highway - 0h 9m Sean Ahern"
                run.return_value = CompletedProcess(["pdftotext"], 0, changed, "")
                with self.assertRaisesRegex(ConflictError, "changed since prior import"):
                    apply_seed(database, pdf_path, log_path)


if __name__ == "__main__":
    unittest.main()
