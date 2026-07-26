from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest
from playwright.sync_api import sync_playwright


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.browser
def test_mobile_webkit_live_drive_recovery() -> None:
    port = _available_port()
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DRIVING_LOG_STATE_DIR": temporary,
            "DRIVING_LOG_PORT": str(port),
            "DRIVING_LOG_PUBLIC_HOST": f"127.0.0.1:{port}",
        }
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "driving_log.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                if server.poll() is not None:
                    output = server.stdout.read() if server.stdout else ""
                    raise AssertionError(f"test server exited early:\n{output}")
                try:
                    if urllib.request.urlopen(f"{url}/health/ready", timeout=0.2).status == 200:
                        break
                except Exception:
                    time.sleep(0.05)
            else:
                output = ""
                if server.poll() is not None and server.stdout:
                    output = server.stdout.read()
                raise AssertionError(f"test server did not become ready\n{output}")

            with sync_playwright() as playwright:
                local_chrome = os.environ.get("DRIVING_LOG_BROWSER") == "chromium-local"
                browser = (
                    playwright.chromium.launch(
                        executable_path="/usr/bin/google-chrome", headless=True
                    )
                    if local_chrome
                    else playwright.webkit.launch(headless=True)
                )
                context = browser.new_context(
                    viewport={"width": 390, "height": 844},
                    device_scale_factor=3,
                    is_mobile=True,
                    has_touch=True,
                )
                page = context.new_page()
                page.goto(url)
                assert "Daniel Driving Log" in page.title()
                assert page.locator(".progress-card").is_visible()
                assert page.locator(".progress-arc-total").is_visible()
                assert (
                    page.locator(".progress-arc-total").evaluate("el => getComputedStyle(el).fill")
                    == "none"
                )
                assert "?v=" in page.locator('link[rel="stylesheet"]').get_attribute("href")
                assert page.locator("text=Night driving").is_visible()
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                assert overflow <= 0
                smallest_button = page.locator("button, a.button").evaluate_all(
                    "els => Math.min(...els.map(el => el.getBoundingClientRect().height))"
                )
                assert smallest_button >= 44
                page.get_by_role("button", name="Start a drive").click()
                page.wait_for_url("**/live")
                first_duration = page.locator("[data-duration]").text_content()
                page.wait_for_timeout(1400)
                assert page.locator("[data-duration]").text_content() != first_duration
                context.close()

                fresh = browser.new_context(
                    viewport={"width": 320, "height": 568},
                    is_mobile=True,
                    has_touch=True,
                )
                recovered = fresh.new_page()
                recurring_requests: list[str] = []
                recovered.on(
                    "request",
                    lambda request: (
                        recurring_requests.append(request.url)
                        if "/live/state" in request.url
                        else None
                    ),
                )
                recovered.goto(f"{url}/live")
                assert recovered.locator("[data-live-start]").is_visible()
                recovered.wait_for_timeout(2200)
                assert recurring_requests == []
                overflow = recovered.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                assert overflow <= 0
                recovered.get_by_role("button", name="End drive").click()
                recovered.get_by_text("The end time is safely stored").wait_for()
                fresh.close()
                browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
