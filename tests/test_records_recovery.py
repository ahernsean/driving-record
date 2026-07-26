from __future__ import annotations

import csv
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
from zoneinfo import ZoneInfo

import pytest

from driving_log.archive import (
    ARCHIVE_FORMAT,
    application_lock,
    create_archive,
    restore_archive,
    verify_archive,
)
from driving_log.csv_backup import CSV_COLUMNS, export_csv, import_csv
from driving_log.db import Database
from driving_log.records import ConflictError, DriveInput, RecordService
from driving_log.seed import parse_log_text, parse_pdf_text


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


class CsvTests(ServiceCase):
    def test_round_trip_and_duplicate_file_are_idempotent(self) -> None:
        first = self.service.create(self.drive(), request_id=str(uuid.uuid4()))
        content = export_csv(self.database)
        other_path = Path(self.temporary.name) / "other.sqlite3"
        other = Database(other_path)
        other.initialize()
        summary = import_csv(other, content, "backup.csv")
        self.assertEqual(summary["created"], 1)
        self.assertEqual(import_csv(other, content, "backup.csv"), summary)
        self.assertEqual(RecordService(other).list_drives()[0]["id"], first["id"])
        self.assertEqual(export_csv(other), content)

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


class ArchiveTests(ServiceCase):
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
        self.assertEqual(rows[1].warnings[0][0], "seed_ambiguous_duration")

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


if __name__ == "__main__":
    unittest.main()
