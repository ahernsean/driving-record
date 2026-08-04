from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import anyio
import httpx
from pypdf import PdfReader

from driving_log.app import create_app
from driving_log.config import DEFAULT_TIMEZONE, Settings
from driving_log.db import utc_now_text
from driving_log.migrations import LATEST_SCHEMA_VERSION
from driving_log.records import DriveInput, RecordService
from driving_log.web import (
    _drive_group_key,
    _duration_bucket,
    _format_local_datetime,
    _format_minutes,
    _local_datetime_in_zone,
    _parse_filter_date,
    _parse_filter_minutes,
    _parse_local,
    _part_of_day,
    _weather_categories,
)


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        state = Path(self.temporary.name)
        self.settings = Settings(
            state_dir=state,
            database_path=state / "database.sqlite3",
            archive_dir=state / "archives",
            restore_dir=state / "restore",
            host="127.0.0.1",
            port=8766,
            public_host="testserver",
        )
        self.app = create_app(self.settings)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_async(self, function: object) -> None:
        anyio.run(function)  # type: ignore[arg-type]

    def test_dashboard_has_no_login_and_manual_record_flow(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as client,
            ):
                dashboard = await client.get("/")
                self.assertEqual(dashboard.status_code, 200)
                self.assertEqual(dashboard.headers["cache-control"], "no-store")
                self.assertIn("Start a drive", dashboard.text)
                self.assertNotIn("login", dashboard.text.lower())
                self.assertRegex(
                    dashboard.text,
                    r"/static/app\.css\?v=[0-9a-f]{12}",
                )
                response = await client.post(
                    "/drives",
                    headers={"Origin": "http://testserver"},
                    data={
                        "request_id": str(uuid.uuid4()),
                        "driver_name": "Daniel Ahern",
                        "supervisor_name": "Sean Ahern",
                        "started_at_local": "2026-07-20T12:00",
                        "ended_at_local": "2026-07-20T12:30",
                        "road_type": "local",
                        "end_location": "Apex Friendship High School",
                    },
                )
                self.assertEqual(response.status_code, 303)
                drive_id = response.headers["location"].rsplit("/", 1)[-1]
                batch_id = str(uuid.uuid4())
                raw_source = "* 2026-07-20 12:00 PM-12:30 PM: 45 minutes, local roads"
                with self.app.state.database.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO import_batches VALUES (?, 'seed_log_txt', ?, ?, '1', ?, ?,
                                                           'completed', '{}')
                        """,
                        (
                            batch_id,
                            "log.txt",
                            hashlib.sha256(raw_source.encode()).hexdigest(),
                            utc_now_text(),
                            gzip.compress(raw_source.encode()),
                        ),
                    )
                    connection.execute(
                        "UPDATE drives SET import_batch_id=? WHERE id=?", (batch_id, drive_id)
                    )
                    connection.execute(
                        """
                        INSERT INTO import_rows VALUES (?, ?, 'seed_log_txt', 'log.txt', '1',
                                                        ?, ?, ?, 'created', NULL)
                        """,
                        (
                            str(uuid.uuid4()),
                            batch_id,
                            raw_source,
                            json.dumps(
                                {
                                    "started_at_utc": "2026-07-20T16:00:00Z",
                                    "ended_at_utc": "2026-07-20T16:45:00Z",
                                    "source_day_minutes": 45,
                                    "source_night_minutes": None,
                                }
                            ),
                            drive_id,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO drive_warnings VALUES (?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            drive_id,
                            "seed_ambiguous_duration",
                            (
                                "Written duration does not match start and end timestamps; "
                                "duration was used"
                            ),
                            utc_now_text(),
                        ),
                    )
                malformed = await client.post(
                    "/drives",
                    data={
                        "request_id": str(uuid.uuid4()),
                        "started_at_local": "not-a-date",
                        "ended_at_local": "2026-07-20T12:30",
                    },
                )
                self.assertEqual(malformed.status_code, 400)
                self.assertIn("Invalid form", malformed.text)
                detail = await client.get(response.headers["location"])
                self.assertIn("30m", detail.text)
                self.assertIn("30m day", detail.text)
                self.assertNotIn("0m night", detail.text)
                self.assertNotIn("Supervisor license", detail.text)
                self.assertIn("Monday, Jul 20, 2026 at 12:00 PM EDT", detail.text)
                self.assertIn("Monday, Jul 20, 2026 at 12:30 PM EDT", detail.text)
                self.assertNotIn("Started (UTC)", detail.text)
                self.assertIn("Apex Friendship High School", detail.text)
                self.assertIn("Written duration does not match", detail.text)
                self.assertIn("Review and save this drive", detail.text)
                self.assertIn("Imported source evidence", detail.text)
                self.assertIn(raw_source, detail.text)
                self.assertIn("Importer read start", detail.text)
                self.assertIn("Source day duration", detail.text)
                self.assertIn("0h 45m", detail.text)
                listing = await client.get("/drives")
                self.assertIn("Drive history", listing.text)
                self.assertIn("local", listing.text)
                self.assertIn("Monday, Jul 20, 2026 at 12:00 PM EDT", listing.text)
                self.assertIn("Apex Friendship High School", listing.text)
                self.assertIn("30m day", listing.text)
                self.assertNotIn("0m night", listing.text)
                night_drive = await client.post(
                    "/drives",
                    headers={"Origin": "http://testserver"},
                    data={
                        "request_id": str(uuid.uuid4()),
                        "driver_name": "Daniel Ahern",
                        "supervisor_name": "Sean Ahern",
                        "started_at_local": "2026-07-20T22:00",
                        "ended_at_local": "2026-07-20T22:30",
                        "road_type": "local",
                    },
                )
                self.assertEqual(night_drive.status_code, 303)
                night_detail = await client.get(night_drive.headers["location"])
                self.assertIn("30m night", night_detail.text)
                self.assertNotIn("0h 0m day", night_detail.text)
                listing = await client.get("/drives")
                self.assertIn("30m night", listing.text)
                self.assertNotIn("0h 0m day", listing.text)
                dashboard = await client.get("/")
                self.assertIn(
                    'aria-label="2 percent of required driving completed"', dashboard.text
                )
                self.assertIn(">2%</text>", dashboard.text)
                self.assertNotIn("1.7%", dashboard.text)
                self.assertNotIn("data-drive-saved-banner", dashboard.text)
                manual_form = await client.get("/drives/new")
                self.assertNotIn('name="supervisor_dl_number"', manual_form.text)
                self.assertNotIn('name="supervisor_dl_state"', manual_form.text)
                self.assertIn("data-time-editor", manual_form.text)
                self.assertIn('aria-label="Duration hours"', manual_form.text)
                self.assertNotIn("data-precise-start=", manual_form.text)
                self.assertNotIn("data-minimum-duration-seconds", manual_form.text)
                edit_form = await client.get(f"{response.headers['location']}/edit")
                self.assertIn("data-time-editor", edit_form.text)
                self.assertIn('value="2026-07-20T12:00"', edit_form.text)
                self.assertIn('value="2026-07-20T12:30"', edit_form.text)
                version_match = re.search(r'name="version" value="(\d+)"', edit_form.text)
                self.assertIsNotNone(version_match)
                updated = await client.post(
                    f"{response.headers['location']}/edit",
                    data={
                        "request_id": str(uuid.uuid4()),
                        "version": version_match.group(1),  # type: ignore[union-attr]
                        "driver_name": "Daniel Ahern",
                        "supervisor_name": "Sean Ahern",
                        "started_at_local": "2026-07-20T12:00",
                        "ended_at_local": "2026-07-20T12:45",
                        "road_type": "local",
                        "end_location": "Home",
                    },
                )
                self.assertEqual(updated.status_code, 303)
                updated_detail = await client.get(updated.headers["location"])
                self.assertIn("45m", updated_detail.text)
                self.assertIn("Home", updated_detail.text)
                self.assertNotIn("Written duration does not match", updated_detail.text)
                updated_version = re.search(r'name="version" value="(\d+)"', updated_detail.text)
                self.assertIsNotNone(updated_version)
                deleted = await client.post(
                    f"{response.headers['location']}/delete",
                    data={
                        "request_id": str(uuid.uuid4()),
                        "version": updated_version.group(1),  # type: ignore[union-attr]
                    },
                )
                self.assertEqual(deleted.status_code, 303)
                self.assertEqual(deleted.headers["location"], "/drives")
                archive = await client.post(
                    "/archives",
                    data={"request_id": str(uuid.uuid4())},
                )
                self.assertEqual(archive.status_code, 303)
                archives = await client.get(archive.headers["location"])
                self.assertIn("Verified archive", archives.text)
                imports = await client.get("/imports")
                self.assertIn("Imports and exports", imports.text)
                csv_export = await client.get("/csv/export")
                self.assertEqual(csv_export.status_code, 200)
                self.assertIn("text/csv", csv_export.headers["content-type"])

        self.run_async(scenario)

    def test_invalid_form_returns_plain_json_detail_for_async_submits(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                ) as client,
            ):
                malformed = await client.post(
                    "/drives",
                    headers={"Accept": "application/json"},
                    data={
                        "request_id": str(uuid.uuid4()),
                        "started_at_local": "not-a-date",
                        "ended_at_local": "2026-07-20T12:30",
                    },
                )
                self.assertEqual(malformed.status_code, 400)
                self.assertEqual(malformed.headers["content-type"], "application/json")
                body = malformed.json()
                self.assertNotIn("<", body["detail"])
                self.assertNotIn("DRIVING_LOG_THEME", body["detail"])

        self.run_async(scenario)

    def test_drive_history_filters_totals_and_groups(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                ) as client,
            ):
                records = RecordService(self.app.state.database)
                for start, end, supervisor, road_type, weather in (
                    (
                        datetime(2026, 7, 20, 16, tzinfo=UTC),
                        datetime(2026, 7, 20, 16, 45, tzinfo=UTC),
                        "Sean Ahern",
                        "highway",
                        "clear",
                    ),
                    (
                        datetime(2026, 7, 22, 2, tzinfo=UTC),
                        datetime(2026, 7, 22, 2, 30, tzinfo=UTC),
                        "Alex Smith",
                        "local",
                        "rain",
                    ),
                    (
                        datetime(2026, 7, 21, 0, 30, tzinfo=UTC),
                        datetime(2026, 7, 21, 1, tzinfo=UTC),
                        "Sean Ahern",
                        "local",
                        "clear",
                    ),
                ):
                    records.create(
                        DriveInput(
                            driver_name="Daniel Ahern",
                            supervisor_name=supervisor,
                            supervisor_dl_number=None,
                            supervisor_dl_state=None,
                            started_at_utc=start,
                            ended_at_utc=end,
                            road_type=road_type,
                            weather=weather,
                        ),
                        request_id=str(uuid.uuid4()),
                    )

                filtered = await client.get(
                    "/drives?road_type=highway&supervisor=Sean+Ahern&group_by=road_type"
                )
                self.assertEqual(filtered.status_code, 200)
                self.assertIn("1 drive · 0h 45m", filtered.text)
                self.assertIn("0h 45m day", filtered.text)
                self.assertIn("Highway", filtered.text)
                self.assertNotIn("0h 30m · local", filtered.text)

                afternoon = await client.get("/drives?part_of_day=afternoon")
                self.assertIn("1 drive · 0h 45m", afternoon.text)
                self.assertIn("0h 45m · highway", afternoon.text)
                self.assertNotIn("0h 30m · local", afternoon.text)

                grouped = await client.get("/drives?group_by=weather")
                self.assertIn("clear", grouped.text)
                self.assertIn("rain", grouped.text)
                self.assertIn("0h 45m day", grouped.text)
                self.assertIn("0h 30m night", grouped.text)

                weather_filtered = await client.get("/drives?weather=clear&weather=rain")
                self.assertIn("3 drives · 1h 45m", weather_filtered.text)
                self.assertIn(
                    'type="checkbox" name="weather" value="clear" checked', weather_filtered.text
                )
                self.assertIn(
                    'type="checkbox" name="weather" value="rain" checked', weather_filtered.text
                )

                with_warning = next(
                    row for row in records.list_drives() if row["road_type"] == "highway"
                )
                with self.app.state.database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO drive_warnings VALUES (?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            with_warning["id"],
                            "review_needed",
                            "Review this drive",
                            "",
                        ),
                    )
                warnings_only = await client.get("/drives?warnings_only=1")
                self.assertIn("1 drive · 0h 45m", warnings_only.text)
                self.assertIn("0h 45m · highway", warnings_only.text)
                self.assertNotIn("0h 30m · local", warnings_only.text)
                self.assertIn(
                    'type="checkbox" name="warnings_only" value="1" checked', warnings_only.text
                )

                time_grouped = await client.get("/drives?group_by=part_of_day")
                self.assertIn(
                    '<span class="group-label">Nighttime (dusk–dawn)</span>',
                    time_grouped.text,
                )
                self.assertIn("Morning (dawn–12 PM)", time_grouped.text)
                self.assertIn("Nighttime (dusk–dawn)", time_grouped.text)
                self.assertIn("2 drives · 1h 00m", time_grouped.text)
                self.assertNotIn('<details class="history-controls" open>', time_grouped.text)
                self.assertIn("Last week", time_grouped.text)
                self.assertIn("Last year", time_grouped.text)

                day_minutes = sum(
                    int(row["day_minutes"])
                    for row in records.list_drives()
                    if int(row["day_minutes"])
                )
                daytime = await client.get("/drives?day_night=day")
                self.assertIn(f"2 drives · {_format_minutes(day_minutes)}", daytime.text)
                self.assertIn("counted toward Daytime", daytime.text)

                night_minutes = sum(
                    int(row["night_minutes"])
                    for row in records.list_drives()
                    if int(row["night_minutes"])
                )
                nighttime = await client.get("/drives?day_night=night")
                self.assertIn(f"2 drives · {_format_minutes(night_minutes)}", nighttime.text)
                self.assertIn("counted toward Nighttime", nighttime.text)

                day_night_grouped = await client.get("/drives?group_by=day_night")
                self.assertEqual(day_night_grouped.text.count("counted toward Day"), 1)
                self.assertEqual(day_night_grouped.text.count("counted toward Night"), 1)

                date_grouped = await client.get("/drives?group_by=date")
                self.assertLess(
                    date_grouped.text.index("Tuesday, Jul 21, 2026"),
                    date_grouped.text.index("Monday, Jul 20, 2026"),
                )

                records.create(
                    DriveInput(
                        driver_name="Daniel Ahern",
                        supervisor_name="Alex Smith",
                        supervisor_dl_number=None,
                        supervisor_dl_state=None,
                        started_at_utc=datetime(2026, 7, 20, 2, tzinfo=UTC),
                        ended_at_utc=datetime(2026, 7, 20, 2, 30, tzinfo=UTC),
                        road_type="local",
                        weather="clear",
                        timezone_name=DEFAULT_TIMEZONE,
                    ),
                    request_id=str(uuid.uuid4()),
                )
                eastern_date_grouped = await client.get("/drives?group_by=date")
                self.assertIn("Sunday, Jul 19, 2026", eastern_date_grouped.text)

                all_time = await client.get(
                    "/drives?period=all&start_date=2026-07-21&end_date=2026-07-21"
                )
                self.assertIn("4 drives · 2h 15m", all_time.text)
                self.assertIn('name="start_date" value=""', all_time.text)
                self.assertIn('name="end_date" value=""', all_time.text)

                for query in (
                    "period=today",
                    "period=week",
                    "period=last_week",
                    "period=month",
                    "period=last_month",
                    "period=year",
                    "period=last_year",
                    "period=custom&start_date=2026-07-20&end_date=2026-07-21",
                    "supervisor=none",
                    "day_night=day",
                    "day_night=night",
                    "road_type=local",
                    "weather=cloudy",
                    "min_duration=30&max_duration=45",
                    "group_by=supervisor",
                    "group_by=day_night",
                    "group_by=road_type",
                    "group_by=duration",
                ):
                    self.assertEqual((await client.get(f"/drives?{query}")).status_code, 200)

                for query in (
                    "period=invalid",
                    "group_by=invalid",
                    "day_night=invalid",
                    "warnings_only=true",
                    "part_of_day=invalid",
                    "supervisor=invalid",
                    "weather=invalid",
                    "period=custom&start_date=2026-07-22&end_date=2026-07-20",
                    "min_duration=45&max_duration=30",
                ):
                    self.assertEqual((await client.get(f"/drives?{query}")).status_code, 400)

        self.run_async(scenario)

    def test_time_of_day_uses_solar_dawn_and_dusk_boundaries(self) -> None:
        self.assertEqual(
            _part_of_day(datetime(2026, 7, 20, 21, tzinfo=UTC), DEFAULT_TIMEZONE), "evening"
        )
        self.assertEqual(
            _part_of_day(datetime(2026, 7, 21, 2, tzinfo=UTC), DEFAULT_TIMEZONE), "night"
        )

    def test_history_filter_helpers(self) -> None:
        self.assertEqual(
            _weather_categories("Cloudy, wet roads, and fog"), ("cloudy", "wet", "fog")
        )
        self.assertEqual(_weather_categories(None), ("unspecified",))
        self.assertEqual(
            [_duration_bucket(value) for value in (29, 30, 60, 120)],
            [
                "Under 30 minutes",
                "30–59 minutes",
                "1–2 hours",
                "2+ hours",
            ],
        )
        self.assertIsNone(_parse_filter_date("", "Date"))
        self.assertEqual(_parse_filter_date("2026-07-20", "Date").isoformat(), "2026-07-20")
        with self.assertRaisesRegex(ValueError, "Date must be a valid date"):
            _parse_filter_date("not-a-date", "Date")
        self.assertIsNone(_parse_filter_minutes("", "Minutes"))
        self.assertEqual(_parse_filter_minutes("30", "Minutes"), 30)
        for value, message in (("not-a-number", "whole number"), ("-1", "cannot be negative")):
            with self.assertRaisesRegex(ValueError, message):
                _parse_filter_minutes(value, "Minutes")
        self.assertEqual(
            _local_datetime_in_zone(datetime(2026, 7, 20, 16), DEFAULT_TIMEZONE).tzinfo,
            ZoneInfo(DEFAULT_TIMEZONE),
        )
        row = cast(
            sqlite3.Row,
            {
                "started_at_utc": "2026-07-20T16:00:00Z",
                "timezone_name": DEFAULT_TIMEZONE,
                "supervisor_name": " Sean Ahern ",
                "day_minutes": 30,
                "night_minutes": 15,
                "road_type": "highway",
                "weather": "clear rain",
                "duration_minutes": 45,
            },
        )
        self.assertEqual(_drive_group_key(row, "date")[0], "2026-07-20")
        self.assertEqual(_drive_group_key(row, "supervisor"), ("sean ahern", "Sean Ahern"))
        self.assertEqual(_drive_group_key(row, "day_night"), ("mixed", "Day and night"))
        self.assertEqual(_drive_group_key(row, "part_of_day"), ("night", "Nighttime (dusk–dawn)"))
        self.assertEqual(_drive_group_key(row, "road_type"), ("highway", "Highway"))
        self.assertEqual(_drive_group_key(row, "weather"), ("clear-rain", "Clear · Rain"))
        self.assertEqual(_drive_group_key(row, "duration"), ("30–59 minutes", "30–59 minutes"))
        self.assertEqual(_drive_group_key(row, "unknown"), ("all", "All drives"))

    def test_timestamp_formatter_uses_local_date_and_dst_offset(self) -> None:
        self.assertEqual(
            _format_local_datetime("2026-07-20T02:30:00Z"),
            "Sunday, Jul 19, 2026 at 10:30 PM EDT",
        )
        self.assertEqual(
            _format_local_datetime("2026-01-20T17:30:00Z"),
            "Tuesday, Jan 20, 2026 at 12:30 PM EST",
        )
        self.assertEqual(
            _parse_local("2026-11-01T01:30"),
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        )

    def test_archive_schema_mismatch_requests_restart_without_archive(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as client,
            ):
                with self.app.state.database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO schema_migrations VALUES (?, 'future', 'future', ?)",
                        (LATEST_SCHEMA_VERSION + 1, utc_now_text()),
                    )
                response = await client.post("/archives", data={})
                self.assertEqual(response.status_code, 503)
                self.assertIn("restart driving-log-web.service", response.json()["detail"])
                self.assertEqual(list(self.settings.archive_dir.glob("*.tar.gz")), [])

        self.run_async(scenario)

    def test_dashboard_progress_rounds_half_up_without_early_completion(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as client,
            ):

                async def add_drive(day: int, duration_minutes: int) -> None:
                    start = datetime(2026, 7, day, 12, tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
                    response = await client.post(
                        "/drives",
                        data={
                            "request_id": str(uuid.uuid4()),
                            "driver_name": "Daniel Ahern",
                            "started_at_local": start.strftime("%Y-%m-%dT%H:%M"),
                            "ended_at_local": (
                                start + timedelta(minutes=duration_minutes)
                            ).strftime("%Y-%m-%dT%H:%M"),
                            "road_type": "local",
                        },
                    )
                    self.assertEqual(response.status_code, 303)

                await add_drive(1, 18)
                dashboard = await client.get("/")
                self.assertIn(
                    'aria-label="1 percent of required driving completed"', dashboard.text
                )
                self.assertIn(">1%</text>", dashboard.text)

                await add_drive(2, 72)
                dashboard = await client.get("/")
                self.assertIn(
                    'aria-label="3 percent of required driving completed"', dashboard.text
                )
                self.assertIn(">3%</text>", dashboard.text)

                for day in range(3, 13):
                    await add_drive(day, 300)
                await add_drive(13, 300)
                await add_drive(14, 209)
                dashboard = await client.get("/")
                self.assertIn(
                    'aria-label="99 percent of required driving completed"', dashboard.text
                )
                self.assertIn(">99%</text>", dashboard.text)

                await add_drive(15, 1)
                dashboard = await client.get("/")
                self.assertIn(
                    'aria-label="100 percent of required driving completed"', dashboard.text
                )
                self.assertIn(">100%</text>", dashboard.text)

                await add_drive(16, 288)
                dashboard = await client.get("/")
                self.assertIn(
                    'aria-label="108 percent of required driving completed"', dashboard.text
                )
                self.assertIn(">108%</text>", dashboard.text)

        self.run_async(scenario)

    def test_live_drive_recovers_in_fresh_client_and_finalizes_once(self) -> None:
        async def scenario() -> None:
            async with self.app.router.lifespan_context(self.app):
                transport = httpx.ASGITransport(app=self.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as first:
                    start = await first.post(
                        "/live/start",
                        headers={"Origin": "http://testserver"},
                        data={
                            "request_id": str(uuid.uuid4()),
                        },
                    )
                    self.assertEqual(start.status_code, 303)
                    live_state = await first.get("/live/state")
                    self.assertEqual(live_state.status_code, 200)
                    self.assertIsNotNone(live_state.json()["live"])
                    live_started_at_utc = str(live_state.json()["live"]["started_at_utc"])
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as fresh:
                    page = await fresh.get("/live")
                    self.assertIn("data-live-start", page.text)
                    live_id = re.search(r"/live/([^/]+)/end", page.text)
                    self.assertIsNotNone(live_id)
                    selected_id = live_id.group(1)  # type: ignore[union-attr]
                    ended = await fresh.post(
                        f"/live/{selected_id}/end",
                        headers={"Origin": "http://testserver"},
                        data={
                            "request_id": str(uuid.uuid4()),
                        },
                    )
                    self.assertEqual(ended.status_code, 303)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as newest:
                    completion = await newest.get("/live")
                    self.assertIn("<strong>Drive ended.</strong>", completion.text)
                    self.assertNotIn('name="acknowledge_warnings"', completion.text)
                    self.assertNotIn('name="supervisor_dl_number"', completion.text)
                    self.assertNotIn('name="supervisor_dl_state"', completion.text)
                    self.assertIn("data-time-editor", completion.text)
                    self.assertIn("data-precise-start=", completion.text)
                    self.assertIn("data-precise-end=", completion.text)
                    self.assertIn('data-minimum-duration-seconds="30"', completion.text)
                    self.assertIn("data-short-drive-warning", completion.text)
                    self.assertIn('name="end_location"', completion.text)
                    start_match = re.search(
                        r'name="started_at_local" value="([^"]+)"', completion.text
                    )
                    self.assertIsNotNone(start_match)
                    # Work in UTC first so the synthetic five-minute drive remains
                    # valid across DST gaps and folds.
                    corrected_end_local = (
                        datetime.fromisoformat(
                            live_started_at_utc.replace("Z", "+00:00")
                        ).astimezone(UTC)
                        + timedelta(minutes=5)
                    ).astimezone(ZoneInfo(DEFAULT_TIMEZONE))
                    finalization_data = {
                        "request_id": str(uuid.uuid4()),
                        "road_type": "local",
                        "started_at_local": start_match.group(1),  # type: ignore[union-attr]
                        "ended_at_local": corrected_end_local.strftime("%Y-%m-%dT%H:%M"),
                        "end_fold": str(corrected_end_local.fold),
                        "end_location": "Home",
                    }
                    finalized = await newest.post(
                        f"/live/{selected_id}/finalize",
                        headers={"Origin": "http://testserver"},
                        data=finalization_data,
                    )
                    self.assertEqual(finalized.status_code, 303)
                    replay = await newest.post(
                        f"/live/{selected_id}/finalize",
                        headers={"Origin": "http://testserver"},
                        data=finalization_data,
                    )
                    self.assertEqual(replay.status_code, 303)
                    self.assertEqual(replay.headers["location"], finalized.headers["location"])
                    saved_location = finalized.headers["location"]
                    self.assertTrue(saved_location.startswith("/?saved="))
                    saved_drive_id = saved_location.removeprefix("/?saved=")
                    dashboard = await newest.get(saved_location)
                    self.assertIn("Recorded drives", dashboard.text)
                    self.assertIn("0h 05m drive saved", dashboard.text)
                    self.assertIn(f'href="/drives/{saved_drive_id}/edit"', dashboard.text)

        self.run_async(scenario)

    def test_live_drive_resume_and_cancel_routes(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as client,
            ):
                await client.post("/live/start", data={"request_id": str(uuid.uuid4())})
                live_id = (await client.get("/live/state")).json()["live"]["id"]
                await client.post(
                    f"/live/{live_id}/end",
                    data={"request_id": str(uuid.uuid4())},
                )
                resumed = await client.post(
                    f"/live/{live_id}/resume",
                    data={"request_id": str(uuid.uuid4())},
                )
                self.assertEqual(resumed.status_code, 303)
                await client.post(
                    f"/live/{live_id}/end",
                    data={"request_id": str(uuid.uuid4())},
                )
                cancelled = await client.post(
                    f"/live/{live_id}/cancel",
                    data={"request_id": str(uuid.uuid4())},
                )
                self.assertEqual(cancelled.status_code, 303)
                self.assertEqual(cancelled.headers["location"], "/")

        self.run_async(scenario)

    def test_dmv_profile_and_pdf_download_flow(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                    follow_redirects=False,
                ) as client,
            ):
                initial = await client.get("/dmv")
                self.assertEqual(initial.status_code, 200)
                self.assertIn("Add license information", initial.text)
                created = await client.post(
                    "/dmv/profiles",
                    data={
                        "request_id": str(uuid.uuid4()),
                        "display_name": "Sean Ahern",
                        "dl_number": "SYNTHETIC-1234",
                        "dl_state": "NC",
                    },
                )
                self.assertEqual(created.status_code, 303)
                self.assertEqual(created.headers["location"], "/dmv")
                profile_page = await client.get("/dmv")
                self.assertIn("••••••••••1234", profile_page.text)
                self.assertIn("SYNTHETIC-1234", profile_page.text)
                profile_id = re.search(r'name="profile_id" value="([^"]+)"', profile_page.text)
                self.assertIsNotNone(profile_id)
                selected_profile_id = profile_id.group(1)  # type: ignore[union-attr]
                updated = await client.post(
                    "/dmv/profiles",
                    data={
                        "request_id": str(uuid.uuid4()),
                        "profile_id": selected_profile_id,
                        "version": "1",
                        "display_name": "Sean Ahern",
                        "dl_number": "UPDATED-5678",
                        "dl_state": "NC",
                    },
                )
                self.assertEqual(updated.status_code, 303)

                drive = await client.post(
                    "/drives",
                    data={
                        "request_id": str(uuid.uuid4()),
                        "driver_name": "Daniel Ahern",
                        "supervisor_name": "Sean Ahern",
                        "started_at_local": "2026-07-20T12:00",
                        "ended_at_local": "2026-07-20T12:30",
                        "road_type": "local",
                    },
                )
                self.assertEqual(drive.status_code, 303)
                dashboard = await client.get("/")
                self.assertNotIn("SYNTHETIC-1234", dashboard.text)
                download = await client.get("/dmv/export")
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.headers["cache-control"], "no-store")
                self.assertIn("attachment", download.headers["content-disposition"])
                reader = PdfReader(io.BytesIO(download.content))
                self.assertEqual(len(reader.pages), 2)
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                self.assertIn("UPDATED-5678, NC", text)
                deleted = await client.post(
                    f"/dmv/profiles/{selected_profile_id}/delete",
                    data={"request_id": str(uuid.uuid4()), "version": "2"},
                )
                self.assertEqual(deleted.status_code, 303)
                self.assertNotIn("UPDATED-5678", (await client.get("/dmv")).text)

        self.run_async(scenario)


if __name__ == "__main__":
    unittest.main()
