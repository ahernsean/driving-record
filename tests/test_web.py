from __future__ import annotations

import re
import tempfile
import unittest
import uuid
from pathlib import Path

import anyio
import httpx
import pytest

from driving_log.app import create_app
from driving_log.config import Settings
from driving_log.security import InvalidFormToken, create_form_token, verify_form_token


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
            form_secret="unit-test-form-secret",
        )
        self.app = create_app(self.settings)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_async(self, function: object) -> None:
        anyio.run(function)  # type: ignore[arg-type]

    def token(self, action: str) -> str:
        return create_form_token(self.settings.form_secret, action)

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
                self.assertIn("Start a drive", dashboard.text)
                self.assertNotIn("login", dashboard.text.lower())
                response = await client.post(
                    "/drives",
                    headers={"Origin": "http://testserver"},
                    data={
                        "form_token": self.token("drive.create"),
                        "request_id": str(uuid.uuid4()),
                        "driver_name": "Daniel Ahern",
                        "supervisor_name": "Sean Ahern",
                        "started_at_local": "2026-07-20T12:00",
                        "ended_at_local": "2026-07-20T12:30",
                        "road_type": "local",
                    },
                )
                self.assertEqual(response.status_code, 303)
                detail = await client.get(response.headers["location"])
                self.assertIn("30m", detail.text)
                listing = await client.get("/drives")
                self.assertIn("Drive history", listing.text)
                self.assertIn("local", listing.text)

        self.run_async(scenario)

    @pytest.mark.security
    def test_cross_origin_host_content_type_and_token_are_rejected(self) -> None:
        async def scenario() -> None:
            async with (
                self.app.router.lifespan_context(self.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                ) as client,
            ):
                path = "/live/start"
                data = {
                    "form_token": self.token("live.start"),
                    "request_id": str(uuid.uuid4()),
                }
                self.assertEqual((await client.post(path, data=data)).status_code, 403)
                self.assertEqual(
                    (
                        await client.post(
                            path,
                            data=data,
                            headers={"Origin": "https://evil.example"},
                        )
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    (
                        await client.post(
                            path,
                            data=data,
                            headers={
                                "Origin": "http://testserver",
                                "Host": "evil.example",
                            },
                        )
                    ).status_code,
                    400,
                )
                self.assertEqual(
                    (
                        await client.post(
                            path,
                            content=b"{}",
                            headers={
                                "Origin": "http://testserver",
                                "Content-Type": "application/json",
                            },
                        )
                    ).status_code,
                    415,
                )
                self.assertEqual(
                    (
                        await client.post(
                            path,
                            data={
                                "form_token": self.token("drive.create"),
                                "request_id": str(uuid.uuid4()),
                            },
                            headers={"Origin": "http://testserver"},
                        )
                    ).status_code,
                    403,
                )

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
                            "form_token": self.token("live.start"),
                            "request_id": str(uuid.uuid4()),
                        },
                    )
                    self.assertEqual(start.status_code, 303)
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
                            "form_token": self.token("live.end"),
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
                    self.assertIn("The end time is safely stored", completion.text)
                    # The HTTP scenario is intentionally short, so correct the end into
                    # the future enough to produce a valid minute-based record.
                    finalized = await newest.post(
                        f"/live/{selected_id}/finalize",
                        headers={"Origin": "http://testserver"},
                        data={
                            "form_token": self.token("live.finalize"),
                            "request_id": str(uuid.uuid4()),
                            "road_type": "local",
                            "corrected_end_local": "2026-07-27T12:00",
                            "acknowledge_warnings": "yes",
                        },
                    )
                    self.assertEqual(finalized.status_code, 303)
                    dashboard = await newest.get("/")
                    self.assertIn("Recorded drives", dashboard.text)

        self.run_async(scenario)

    @pytest.mark.security
    def test_form_tokens_are_signed_scoped_and_expiring(self) -> None:
        token = create_form_token("secret", "drive.create", now=100, ttl=10)
        verify_form_token("secret", token, "drive.create", now=110)
        with self.assertRaises(InvalidFormToken):
            verify_form_token("secret", token, "drive.update", now=105)
        with self.assertRaises(InvalidFormToken):
            verify_form_token("secret", token, "drive.create", now=111)
        with self.assertRaises(InvalidFormToken):
            verify_form_token("other", token, "drive.create", now=105)


if __name__ == "__main__":
    unittest.main()
