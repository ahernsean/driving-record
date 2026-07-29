from __future__ import annotations

import io
import re
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio
import httpx
from pypdf import PdfReader

from driving_log.app import create_app
from driving_log.config import DEFAULT_TIMEZONE, Settings
from driving_log.web import _format_local_datetime, _parse_local


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
                self.assertIn("<strong>2%</strong>", dashboard.text)
                self.assertNotIn("1.7%", dashboard.text)
                manual_form = await client.get("/drives/new")
                self.assertNotIn('name="supervisor_dl_number"', manual_form.text)
                self.assertNotIn('name="supervisor_dl_state"', manual_form.text)
                self.assertIn("data-time-editor", manual_form.text)
                self.assertIn('aria-label="Duration hours"', manual_form.text)
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
                    detail = await newest.get(finalized.headers["location"])
                    self.assertIn("Home", detail.text)
                    dashboard = await newest.get("/")
                    self.assertIn("Recorded drives", dashboard.text)

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
