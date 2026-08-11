from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import anyio
import httpx

from driving_log.app import create_app
from driving_log.auth import Authenticator, _encode, password_hash, verify_password
from driving_log.cli import doctor, main
from driving_log.config import Settings
from driving_log.db import BUSY_TIMEOUT_MS, Database, MigrationMismatchError, SchemaTooNewError
from driving_log.logging_config import JsonFormatter
from driving_log.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS, SCHEMA_V1
from driving_log.solar import (
    AmbiguousLocalTimeError,
    LocalTimeError,
    NonexistentLocalTimeError,
    apex_daylight_window,
    resolve_local,
    split_day_night,
    week_segments,
)
from driving_log.validation import validate_interval, validate_road_type


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state" / "driving.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_password_hashes_and_signed_sessions_reject_invalid_values(self) -> None:
        encoded_password = password_hash("password")
        self.assertTrue(verify_password("password", encoded_password))
        self.assertFalse(verify_password("password", "unknown$1$2$3$x$y"))
        self.assertFalse(verify_password("password", "not-a-password-hash"))
        with self.assertRaisesRegex(ValueError, "empty"):
            password_hash("")
        authenticator = Authenticator(encoded_password, encoded_password, "session-secret")
        token = authenticator.make_session("Sean Ahern")
        self.assertEqual(authenticator.session_user(token), "Sean Ahern")
        self.assertIsNone(authenticator.session_user(f"{token}changed"))

        def signed(payload: dict[str, object]) -> str:
            body = _encode(payload)
            signature = hmac.new(b"session-secret", body.encode(), hashlib.sha256).hexdigest()
            return f"{body}.{signature}"

        self.assertIsNone(authenticator.session_user(signed({"user": "Sean Ahern"})))
        self.assertIsNone(authenticator.session_user(signed({"user": "Other", "exp": 9999999999})))
        self.assertIsNone(authenticator.session_user(signed({"user": "Sean Ahern", "exp": 0})))

    def test_initializes_schema_and_production_pragmas(self) -> None:
        database = Database(self.path)
        database.initialize()
        connection = database.connect()
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], BUSY_TIMEOUT_MS)
        self.assertEqual(
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
            LATEST_SCHEMA_VERSION,
        )
        connection.close()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(f"{self.path.name}{suffix}")
            if sidecar.exists():
                self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)

    def test_migrates_v1_drives_with_blank_end_locations(self) -> None:
        self.path.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.path)
        connection.executescript(SCHEMA_V1)
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, ?, ?, 'now')",
            (MIGRATIONS[0].name, MIGRATIONS[0].checksum),
        )
        connection.execute(
            """
            INSERT INTO drives (
              id, driver_name, started_at_utc, ended_at_utc, timezone_name,
              started_utc_offset_minutes, ended_utc_offset_minutes, duration_minutes,
              day_minutes, night_minutes, solar_calculation_version, solar_latitude,
              solar_longitude, tzdata_version, road_type, source, created_at, updated_at
            ) VALUES (
              'drive', 'Daniel Ahern', '2026-07-20T16:00:00Z',
              '2026-07-20T16:30:00Z', 'America/New_York', -240, -240, 30, 30, 0,
              'apex-v1', 35.73, -78.85, 'test', 'local', 'manual', 'now', 'now'
            )
            """
        )
        connection.commit()
        connection.close()

        archive_dir = Path(self.temp.name) / "custom-archives"
        database = Database(self.path, archive_dir)
        database.initialize()
        pre_migration = list(archive_dir.glob("driving-log-pre-migration-v1-*.tar.gz"))
        self.assertEqual(len(pre_migration), 1)
        self.assertFalse((self.path.parent / "archives").exists())
        upgraded = database.connect()
        self.assertEqual(
            upgraded.execute("SELECT end_location FROM drives WHERE id='drive'").fetchone()[0],
            "",
        )
        self.assertEqual(
            upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
            LATEST_SCHEMA_VERSION,
        )
        upgraded.close()

    def test_refuses_schema_newer_than_application(self) -> None:
        database = Database(self.path)
        database.initialize()
        connection = database.connect()
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
            (LATEST_SCHEMA_VERSION + 1, "future", "future", "now"),
        )
        connection.close()
        with self.assertRaises(SchemaTooNewError):
            database.initialize()

    def test_refuses_modified_migration_history(self) -> None:
        database = Database(self.path)
        database.initialize()
        connection = database.connect()
        connection.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
        connection.close()
        with self.assertRaises(MigrationMismatchError):
            database.initialize()

    def test_transaction_rolls_back(self) -> None:
        database = Database(self.path)
        database.initialize()
        with (
            self.assertRaisesRegex(RuntimeError, "fail"),
            database.transaction() as connection,
        ):
            connection.execute("INSERT INTO configuration VALUES ('x', 'y', 'now')")
            raise RuntimeError("fail")
        connection = database.connect()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM configuration").fetchone()[0], 0)
        connection.close()

    def test_health_and_audit(self) -> None:
        database = Database(self.path)
        self.assertFalse(database.health()["ready"])
        database.initialize()
        with database.transaction() as connection:
            database.audit(
                connection,
                event_id="event",
                request_id="request",
                action="test",
                payload_hash="hash",
                entity_type="drive",
                entity_id="drive",
                outcome="ok",
            )
        self.assertTrue(database.health()["ready"])
        connection = database.connect()
        self.assertEqual(
            connection.execute("SELECT action FROM audit_events").fetchone()[0], "test"
        )
        connection.close()


class TimeTests(unittest.TestCase):
    def test_rejects_spring_gap(self) -> None:
        with self.assertRaises(NonexistentLocalTimeError):
            resolve_local(datetime(2026, 3, 8, 2, 30))

    def test_requires_fold_for_ambiguous_time(self) -> None:
        value = datetime(2026, 11, 1, 1, 30)
        with self.assertRaises(AmbiguousLocalTimeError):
            resolve_local(value)
        first = resolve_local(value, fold=0)
        second = resolve_local(value, fold=1)
        self.assertEqual(second.astimezone(UTC) - first.astimezone(UTC), timedelta(hours=1))
        with self.assertRaises(LocalTimeError):
            resolve_local(value, fold=2)

    def test_rejects_aware_local_input(self) -> None:
        with self.assertRaises(LocalTimeError):
            resolve_local(datetime.now(UTC))

    def test_day_night_split_at_injected_boundaries(self) -> None:
        zone = ZoneInfo("America/New_York")

        def window(day: date) -> tuple[datetime, datetime]:
            return (
                datetime(day.year, day.month, day.day, 6, tzinfo=zone),
                datetime(day.year, day.month, day.day, 18, tzinfo=zone),
            )

        start = datetime(2026, 7, 1, 5, 30, tzinfo=zone).astimezone(UTC)
        end = datetime(2026, 7, 1, 6, 30, tzinfo=zone).astimezone(UTC)
        self.assertEqual(split_day_night(start, end, daylight_window=window), (30, 30))

    def test_crosses_sunday_week_boundary(self) -> None:
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 7, 25, 23, 30, tzinfo=zone).astimezone(UTC)
        end = datetime(2026, 7, 26, 0, 30, tzinfo=zone).astimezone(UTC)
        self.assertEqual([part[2] for part in week_segments(start, end)], [30, 30])

    def test_apex_fixture_and_interval_validation(self) -> None:
        sunrise, sunset = apex_daylight_window(date(2026, 7, 1))
        self.assertLess(sunrise.hour, 7)
        self.assertGreater(sunset.hour, 19)
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 7, 1, 12, tzinfo=zone).astimezone(UTC)
        value = validate_interval(start, start + timedelta(hours=5, minutes=1))
        self.assertEqual(value.duration_minutes, 301)
        self.assertIn("long_drive", value.warnings)
        midnight_utc = datetime(2026, 7, 1, 23, 30, tzinfo=UTC)
        local_interval = validate_interval(
            midnight_utc,
            midnight_utc + timedelta(hours=1),
            timezone_name="America/New_York",
        )
        self.assertNotIn("crosses_midnight", local_interval.warnings)
        with self.assertRaises(ValueError):
            validate_interval(start, start)
        with self.assertRaisesRegex(ValueError, "minute-aligned"):
            validate_interval(start, start + timedelta(seconds=61))
        self.assertEqual(validate_road_type(" Local "), "local")
        with self.assertRaises(ValueError):
            validate_road_type("racetrack")

    def test_rejects_naive_and_reverse_splits(self) -> None:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        with self.assertRaises(ValueError):
            split_day_night(now.replace(tzinfo=None), now + timedelta(hours=1))
        with self.assertRaises(ValueError):
            week_segments(now, now)


class SettingsTests(unittest.TestCase):
    def test_refuses_non_loopback_production_bind(self) -> None:
        prior = os.environ.get("DRIVING_LOG_HOST")
        os.environ["DRIVING_LOG_HOST"] = "0.0.0.0"
        try:
            with self.assertRaisesRegex(ValueError, "non-loopback"):
                Settings.from_env()
        finally:
            if prior is None:
                del os.environ["DRIVING_LOG_HOST"]
            else:
                os.environ["DRIVING_LOG_HOST"] = prior

    def test_settings_directories_and_localhost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            prior_state = os.environ.get("DRIVING_LOG_STATE_DIR")
            prior_host = os.environ.get("DRIVING_LOG_HOST")
            os.environ["DRIVING_LOG_STATE_DIR"] = str(state)
            os.environ["DRIVING_LOG_HOST"] = "localhost"
            try:
                settings = Settings.from_env()
                settings.ensure_directories()
                self.assertTrue(settings.archive_dir.is_dir())
                self.assertEqual(settings.state_dir.stat().st_mode & 0o777, 0o700)
            finally:
                if prior_state is None:
                    del os.environ["DRIVING_LOG_STATE_DIR"]
                else:
                    os.environ["DRIVING_LOG_STATE_DIR"] = prior_state
                if prior_host is None:
                    del os.environ["DRIVING_LOG_HOST"]
                else:
                    os.environ["DRIVING_LOG_HOST"] = prior_host


class ApplicationTests(unittest.TestCase):
    def test_health_endpoints_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            settings = Settings(
                state_dir=state,
                database_path=state / "database.sqlite3",
                archive_dir=state / "archives",
                restore_dir=state / "restore",
                host="127.0.0.1",
                port=8766,
                public_host="testserver",
                auth_required=False,
            )
            app = create_app(settings)

            async def check_health() -> None:
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                    ) as client,
                ):
                    self.assertEqual((await client.get("/health/live")).status_code, 200)
                    self.assertEqual((await client.get("/health/ready")).status_code, 200)

            anyio.run(check_health)
            report = doctor(settings)
            self.assertTrue(report["ready"])
            self.assertEqual(report["quick_check"], "ok")

    def test_cli_doctor_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prior = os.environ.get("DRIVING_LOG_STATE_DIR")
            os.environ["DRIVING_LOG_STATE_DIR"] = temporary
            try:
                Database(Path(temporary) / "driving-log.sqlite3").initialize()
                self.assertEqual(main(["doctor", "--json"]), 0)
                self.assertEqual(main(["db", "check"]), 0)
                self.assertEqual(main(["archive", "create"]), 0)
                self.assertEqual(main(["archive", "list"]), 0)
                self.assertEqual(main(["archive", "verify"]), 0)
                self.assertEqual(main(["imports", "status"]), 0)
            finally:
                if prior is None:
                    del os.environ["DRIVING_LOG_STATE_DIR"]
                else:
                    os.environ["DRIVING_LOG_STATE_DIR"] = prior

    def test_cli_seed_csv_and_archive_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prior = os.environ.get("DRIVING_LOG_STATE_DIR")
            os.environ["DRIVING_LOG_STATE_DIR"] = temporary
            root = Path(temporary)
            pdf = root / "seed.pdf"
            log = root / "log.txt"
            csv_input = root / "input.csv"
            pdf.write_bytes(b"pdf")
            log.write_text("log")
            csv_input.write_bytes(b"csv")
            try:
                with mock.patch("driving_log.cli.preview_seed", return_value={"rows": 0}):
                    self.assertEqual(
                        main(["seed", "--preview", "--pdf", str(pdf), "--log", str(log)]),
                        0,
                    )
                with mock.patch("driving_log.cli.apply_seed", return_value={"created": 0}):
                    self.assertEqual(main(["seed", "--pdf", str(pdf), "--log", str(log)]), 0)
                exported = root / "export.csv"
                with mock.patch("driving_log.cli.export_csv", return_value=b"header\n"):
                    self.assertEqual(main(["csv", "export", "--out", str(exported)]), 0)
                with mock.patch("driving_log.cli.import_csv", return_value={"created": 0}):
                    self.assertEqual(main(["csv", "import", "--in", str(csv_input)]), 0)
                archive = root / "explicit.tar.gz"
                self.assertEqual(main(["archive", "create", "--out", str(archive)]), 0)
                self.assertEqual(main(["archive", "verify", str(archive)]), 0)
                with mock.patch(
                    "driving_log.cli.restore_archive", return_value=root / "quarantine"
                ):
                    self.assertEqual(
                        main(["archive", "restore", str(archive), "--confirm"]),
                        0,
                    )
            finally:
                if prior is None:
                    del os.environ["DRIVING_LOG_STATE_DIR"]
                else:
                    os.environ["DRIVING_LOG_STATE_DIR"] = prior

    def test_json_log_formatter(self) -> None:
        import logging

        record = logging.LogRecord("test", logging.INFO, "", 0, "message", (), None)
        formatted = JsonFormatter().format(record)
        self.assertIn('"severity":"info"', formatted)


if __name__ == "__main__":
    unittest.main()
