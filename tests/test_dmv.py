from __future__ import annotations

import io
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from pypdf import PdfReader

from driving_log.archive import create_archive, restore_archive, verify_archive
from driving_log.cli import main
from driving_log.csv_backup import export_csv
from driving_log.db import Database
from driving_log.dmv import (
    CONTINUATION_ROW_HEIGHT,
    CONTINUATION_TABLE_TOP,
    DmvExportService,
    DmvRow,
    SupervisorProfileService,
    format_hours,
    mask_license,
    normalize_supervisor_name,
)
from driving_log.records import ConflictError, DriveInput, NotFoundError, RecordService


class DmvCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "driving.sqlite3", self.root / "archives")
        self.database.initialize()
        self.records = RecordService(self.database)
        self.profiles = SupervisorProfileService(self.database)
        self.zone = ZoneInfo("America/New_York")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_drives(
        self,
        count: int,
        *,
        supervisor_name: str | None = "Sean Ahern",
        start_index: int = 0,
    ) -> None:
        base = datetime(2025, 1, 1, 12, tzinfo=self.zone)
        for index in range(start_index, start_index + count):
            start = base + timedelta(days=index)
            self.records.create(
                DriveInput(
                    driver_name="Daniel Ahern",
                    supervisor_name=supervisor_name,
                    supervisor_dl_number=None,
                    supervisor_dl_state=None,
                    started_at_utc=start.astimezone(UTC),
                    ended_at_utc=(start + timedelta(minutes=30)).astimezone(UTC),
                    road_type="local",
                )
            )


class SupervisorProfileTests(DmvCase):
    @pytest.mark.security
    def test_profile_crud_matching_retry_and_privacy(self) -> None:
        request = str(uuid.uuid4())
        created = self.profiles.save(
            display_name="  Sean   Ahern ",
            dl_number="S1234567",
            dl_state="nc",
            request_id=request,
        )
        retried = self.profiles.save(
            display_name="Sean Ahern",
            dl_number="S1234567",
            dl_state="NC",
            request_id=request,
        )
        self.assertEqual(retried["id"], created["id"])
        self.assertEqual(created["normalized_name"], "sean ahern")
        self.assertEqual(mask_license(created["dl_number"]), "••••4567")
        self.assertEqual(normalize_supervisor_name(" SEAN   ahern "), "sean ahern")

        with self.assertRaisesRegex(ConflictError, "already exists"):
            self.profiles.save(
                display_name="SEAN AHERN",
                dl_number="other",
                dl_state="NC",
                request_id=str(uuid.uuid4()),
            )
        updated = self.profiles.save(
            profile_id=created["id"],
            expected_version=1,
            display_name="Sean Ahern",
            dl_number="S7654321",
            dl_state="NC",
            request_id=str(uuid.uuid4()),
        )
        self.assertEqual(updated["version"], 2)
        with self.assertRaisesRegex(ConflictError, "profile version"):
            self.profiles.save(
                profile_id=created["id"],
                expected_version=1,
                display_name="Sean Ahern",
                dl_number="stale",
                dl_state="NC",
                request_id=str(uuid.uuid4()),
            )

        connection = self.database.connect_readonly()
        audit_text = "\n".join(
            str(row["metadata_json"])
            for row in connection.execute(
                "SELECT metadata_json FROM audit_events WHERE entity_type='supervisor_profile'"
            ).fetchall()
        )
        connection.close()
        self.assertNotIn("S1234567", audit_text)
        self.assertNotIn("S7654321", export_csv(self.database).decode())

        delete_request = str(uuid.uuid4())
        self.profiles.delete(
            created["id"],
            expected_version=2,
            request_id=delete_request,
        )
        self.profiles.delete(
            created["id"],
            expected_version=2,
            request_id=delete_request,
        )
        self.assertEqual(self.profiles.list_profiles(), [])

    def test_profile_validation_and_request_reuse(self) -> None:
        with self.assertRaisesRegex(ValueError, "name is required"):
            self.profiles.save(
                display_name=" ",
                dl_number="S123",
                dl_state="NC",
                request_id=str(uuid.uuid4()),
            )
        with self.assertRaisesRegex(ValueError, "license number is required"):
            self.profiles.save(
                display_name="Sean Ahern",
                dl_number=" ",
                dl_state="NC",
                request_id=str(uuid.uuid4()),
            )
        with self.assertRaisesRegex(ValueError, "two-letter"):
            self.profiles.save(
                display_name="Sean Ahern",
                dl_number="S123",
                dl_state="North Carolina",
                request_id=str(uuid.uuid4()),
            )
        request = str(uuid.uuid4())
        self.profiles.save(
            display_name="Sean Ahern",
            dl_number="S123",
            dl_state="NC",
            request_id=request,
        )
        with self.assertRaisesRegex(ConflictError, "different profile data"):
            self.profiles.save(
                display_name="Sean Ahern",
                dl_number="different",
                dl_state="NC",
                request_id=request,
            )
        with self.assertRaisesRegex(NotFoundError, "not found"):
            self.profiles.get("missing")
        with self.assertRaisesRegex(ValueError, "negative"):
            format_hours(-1)

    def test_profile_is_preserved_by_full_archive_restore(self) -> None:
        created = self.profiles.save(
            display_name="Jen Ahern",
            dl_number="J1234567",
            dl_state="NC",
            request_id=str(uuid.uuid4()),
        )
        archive = create_archive(self.database, self.root / "archives")
        self.assertTrue(verify_archive(archive)["verified"])
        self.profiles.delete(
            created["id"],
            expected_version=1,
            request_id=str(uuid.uuid4()),
        )
        restore_archive(self.database.path, archive, confirm=True)
        restored = SupervisorProfileService(Database(self.database.path)).list_profiles()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["display_name"], "Jen Ahern")
        self.assertEqual(restored[0]["dl_number"], "J1234567")


class DmvExportTests(DmvCase):
    def test_mapping_fallback_missing_and_review(self) -> None:
        self.profiles.save(
            display_name="Sean Ahern",
            dl_number="S1234567",
            dl_state="NC",
            request_id=str(uuid.uuid4()),
        )
        self.add_drives(1, supervisor_name=" SEAN   AHERN ")
        start = datetime(2025, 1, 2, 19, tzinfo=self.zone)
        self.records.create(
            DriveInput(
                driver_name="Daniel Ahern",
                supervisor_name="Legacy Driver",
                supervisor_dl_number="L765",
                supervisor_dl_state="va",
                started_at_utc=start.astimezone(UTC),
                ended_at_utc=(start + timedelta(minutes=45)).astimezone(UTC),
            )
        )
        self.add_drives(1, supervisor_name="Jen Ahern", start_index=2)
        self.add_drives(1, supervisor_name=None, start_index=3)
        late_start = datetime(2025, 1, 5, 23, 45, tzinfo=self.zone)
        self.records.create(
            DriveInput(
                driver_name="Daniel Ahern",
                supervisor_name="Sean Ahern",
                supervisor_dl_number=None,
                supervisor_dl_state=None,
                started_at_utc=late_start.astimezone(UTC),
                ended_at_utc=(late_start + timedelta(minutes=30)).astimezone(UTC),
            )
        )

        service = DmvExportService(self.database)
        rows = service.rows()
        self.assertEqual(rows[0].supervisor_name, "SEAN AHERN")
        self.assertEqual(rows[0].supervisor_license, "S1234567, NC")
        self.assertEqual(rows[1].supervisor_license, "L765, VA")
        self.assertEqual((rows[1].day, rows[1].night, rows[1].total), ("", "0:45", "0:45"))
        self.assertEqual(rows[2].supervisor_license, "")
        self.assertEqual(rows[3].supervisor_name, "")
        self.assertEqual(rows[4].date, "01/05/2025")
        review = service.review()
        self.assertEqual(review.unmapped_supervisors, ("Jen Ahern",))
        self.assertEqual(review.blank_supervisor_count, 1)
        self.assertEqual(review.drive_count, 5)
        self.assertEqual(format_hours(80), "1:20")
        self.assertEqual(format_hours(0, blank_zero=True), "")

    def test_pdf_generation_page_counts_and_content(self) -> None:
        expectations = {
            0: 2,
            20: 2,
            21: 2,
            60: 2,
            61: 3,
            74: 3,
            120: 4,
        }
        for row_count, expected_pages in expectations.items():
            with self.subTest(row_count=row_count), tempfile.TemporaryDirectory() as temporary:
                database = Database(Path(temporary) / "driving.sqlite3")
                database.initialize()
                fixture = DmvCase.__new__(DmvCase)
                fixture.database = database
                fixture.records = RecordService(database)
                fixture.profiles = SupervisorProfileService(database)
                fixture.zone = self.zone
                fixture.add_drives(row_count)
                fixture.profiles.save(
                    display_name="Sean Ahern",
                    dl_number="SYNTHETIC-1234",
                    dl_state="NC",
                    request_id=str(uuid.uuid4()),
                )
                content = DmvExportService(database).generate()
                reader = PdfReader(io.BytesIO(content))
                self.assertEqual(len(reader.pages), expected_pages)
                self.assertNotIn("/AcroForm", reader.root_object)
                self.assertNotIn("/Names", reader.root_object)
                self.assertTrue(all("/Annots" not in page for page in reader.pages))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                self.assertNotIn("Daniel Ahern", text)
                if row_count:
                    self.assertIn("01/01/2025", text)
                    self.assertIn("SYNTHETIC-1234, NC", text)
                    self.assertEqual(
                        sum(
                            (page.extract_text() or "").count("SYNTHETIC-1234, NC")
                            for page in reader.pages
                        ),
                        row_count,
                    )
                final_text = reader.pages[-1].extract_text() or ""
                self.assertIn(format_hours(row_count * 30), final_text)
                for page in reader.pages[1:-1]:
                    self.assertNotIn(format_hours(row_count * 30), page.extract_text() or "")
                self.assertIn(f"{expected_pages} of {expected_pages}", final_text)

    def test_pdf_template_checksum_and_rendered_letter_pages(self) -> None:
        self.add_drives(120)
        pdf = self.root / "dmv.pdf"
        pdf.write_bytes(DmvExportService(self.database).generate())
        if not shutil.which("pdftoppm"):
            self.skipTest("pdftoppm is not installed")
        output = self.root / "rendered"
        completed = subprocess.run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-l",
                "4",
                "-r",
                "72",
                "-png",
                str(pdf),
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        images = sorted(self.root.glob("rendered-*.png"))
        self.assertEqual(len(images), 4)
        for image_path in images:
            with Image.open(image_path) as image:
                self.assertEqual(image.size, (612, 792))
        reader = PdfReader(pdf)

        def date_baselines(page: object) -> list[float]:
            baselines: list[float] = []

            def capture(
                text: str,
                _current_matrix: object,
                text_matrix: list[float],
                _font: object,
                _font_size: float,
            ) -> None:
                if re.fullmatch(r"\d{2}/\d{2}/\d{4}", text.strip()):
                    baselines.append(float(text_matrix[5]))

            page.extract_text(visitor_text=capture)  # type: ignore[attr-defined]
            return baselines

        for page in reader.pages[1:]:
            for baseline in date_baselines(page):
                nearest_rule = min(
                    abs(baseline - (CONTINUATION_TABLE_TOP - index * CONTINUATION_ROW_HEIGHT))
                    for index in range(41)
                )
                self.assertGreater(nearest_rule, 3.5)

    @pytest.mark.security
    def test_pdf_refuses_modified_official_template(self) -> None:
        template = self.root / "changed-template.pdf"
        template.write_bytes(
            files("driving_log").joinpath("assets/DL-4A.pdf").read_bytes() + b"changed"
        )
        with self.assertRaisesRegex(ValueError, "checksum"):
            DmvExportService(self.database, template_path=template).generate()

    def test_pdf_rows_and_totals_use_one_database_snapshot(self) -> None:
        self.add_drives(1)
        service = DmvExportService(self.database)
        original_rows = service._rows

        def insert_after_rows(connection: sqlite3.Connection) -> list[DmvRow]:
            rows = original_rows(connection)
            self.add_drives(1, start_index=1)
            return rows

        with mock.patch.object(service, "_rows", side_effect=insert_after_rows):
            reader = PdfReader(io.BytesIO(service.generate()))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("01/01/2025", text)
        self.assertNotIn("01/02/2025", text)
        self.assertNotIn("1:00", reader.pages[-1].extract_text() or "")

    def test_cli_export_uses_authoritative_database(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"DRIVING_LOG_STATE_DIR": str(self.root / "cli-state")},
            clear=False,
        ):
            destination = self.root / "cli-dmv.pdf"
            self.assertEqual(main(["dmv", "export", "--out", str(destination)]), 0)
            self.assertEqual(len(PdfReader(destination).pages), 2)
