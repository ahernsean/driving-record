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

from driving_log.auth import password_hash


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _launch_browser(playwright: object) -> object:
    webkit_endpoint = os.environ.get("DRIVING_LOG_WEBKIT_WS_ENDPOINT")
    if webkit_endpoint:
        return playwright.webkit.connect(webkit_endpoint)  # type: ignore[union-attr]
    if os.environ.get("DRIVING_LOG_BROWSER") == "chromium-local":
        return playwright.chromium.launch(  # type: ignore[union-attr]
            executable_path="/usr/bin/google-chrome", headless=True
        )
    return playwright.webkit.launch(headless=True)  # type: ignore[union-attr]


@pytest.mark.browser
def test_quick_live_drive_save_reports_short_duration_without_hanging() -> None:
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
                browser = _launch_browser(playwright)
                page = browser.new_page()
                dialogs: list[str] = []
                page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
                page.goto(url)
                page.get_by_role("button", name="Start a drive").click()
                page.wait_for_url("**/live")
                page.get_by_role("button", name="End drive").click()
                page.get_by_text("Drive ended.").wait_for()
                page.get_by_role("button", name="Save completed drive").click()
                page.wait_for_timeout(500)
                assert dialogs == ["drive must be at least 30 seconds"]
                assert page.url == f"{url}/live"
                browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


@pytest.mark.browser
def test_overlap_warning_distinguishes_dst_fallback_folds() -> None:
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
                raise AssertionError("test server did not become ready")

            with sync_playwright() as playwright:
                browser = _launch_browser(playwright)
                page = browser.new_page()
                page.goto(f"{url}/drives/new")
                page.locator('[name="start_fold"]').evaluate("element => element.value = '0'")
                page.locator('[name="end_fold"]').evaluate("element => element.value = '0'")
                page.locator('[name="started_at_local"]').fill("2026-11-01T01:00")
                page.locator('[name="ended_at_local"]').fill("2026-11-01T01:30")
                page.get_by_role("button", name="Save drive").click()
                page.get_by_role("heading", name="Drive details").wait_for()

                page.goto(f"{url}/drives/new")
                page.locator('[name="start_fold"]').evaluate("element => element.value = '1'")
                page.locator('[name="end_fold"]').evaluate("element => element.value = '1'")
                page.locator('[name="started_at_local"]').fill("2026-11-01T01:00")
                page.locator('[name="ended_at_local"]').fill("2026-11-01T01:30")
                assert page.locator("[data-overlap-warning]").is_hidden()
                browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


@pytest.mark.browser
def test_expired_export_session_returns_to_sign_in() -> None:
    port = _available_port()
    with tempfile.TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "DRIVING_LOG_STATE_DIR": temporary,
            "DRIVING_LOG_PORT": str(port),
            "DRIVING_LOG_PUBLIC_HOST": f"127.0.0.1:{port}",
            "DRIVING_LOG_AUTH_REQUIRED": "1",
            "DRIVING_LOG_SEAN_PASSWORD_HASH": password_hash("sean-password"),
            "DRIVING_LOG_JEN_PASSWORD_HASH": password_hash("jen-password"),
            "DRIVING_LOG_SESSION_SECRET": "test-session-secret",
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
                raise AssertionError("test server did not become ready")

            with sync_playwright() as playwright:
                browser = _launch_browser(playwright)
                context = browser.new_context()
                page = context.new_page()
                page.goto(f"{url}/login?next=/imports")
                page.locator('input[name="password"]').fill("sean-password")
                page.get_by_role("button", name="Sign in").click()
                page.wait_for_url(f"{url}/imports")
                context.clear_cookies()
                page.get_by_role("link", name="Download CSV backup").click()
                page.wait_for_url(f"{url}/login?next=/csv/export")
                assert page.get_by_role("heading", name="Sign in").is_visible()
                browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


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
                browser = _launch_browser(playwright)
                context = browser.new_context(
                    viewport={"width": 390, "height": 844},
                    device_scale_factor=3,
                    is_mobile=True,
                    has_touch=True,
                    geolocation={"latitude": 35.7327, "longitude": -78.8503},
                    permissions=["geolocation"],
                )
                page = context.new_page()
                page.add_init_script(
                    """Object.defineProperty(navigator, "canShare", {
                      value: data => Array.isArray(data.files) && data.files.length === 1,
                    });
                    Object.defineProperty(navigator, "share", {
                      value: async data => {
                        window.sharedExportName = data.files[0].name;
                        window.sharedExportKeys = Object.keys(data);
                      },
                    });"""
                )
                page.goto(url)
                assert "Daniel Driving Log" in page.title()
                assert page.locator(".progress-card").is_visible()
                assert page.locator(".progress-arc-total").is_visible()
                assert page.locator(".progress-label").is_visible()
                assert page.locator(".progress-percent-value").is_visible()
                assert page.locator(".progress-percent-caption").is_visible()
                page.get_by_role("link", name="Locations").click()
                page.locator('input[name="name"]').fill("Home")
                page.locator('input[name="radius_feet"]').fill("330")
                page.get_by_role("button", name="Use my current location").click()
                page.locator("[data-location-map].leaflet-container").wait_for()
                assert page.locator("[data-location-map]").is_visible()
                assert (
                    "Check the pin and circle"
                    in page.locator("[data-location-status]").text_content()
                )
                map_html = page.locator("[data-location-map]").inner_html()
                assert "leaflet-interactive" in map_html, map_html
                assert page.get_by_role("link", name="Cancel").is_visible()
                page.locator('input[name="radius_feet"]').fill("490")
                assert page.locator("[data-location-map]").is_visible()
                page.get_by_role("button", name="Use my current location").click()
                page.wait_for_function(
                    "document.querySelector('[data-location-status]').textContent"
                    ".includes('Check the pin and circle')"
                )
                assert page.locator("[data-location-map]").is_visible()
                with page.expect_navigation():
                    page.get_by_role("button", name="Save location Home").click()
                assert page.get_by_text("Home · 490 ft radius").is_visible()
                page.get_by_role("link", name="Edit").click()
                page.locator("[data-location-map].leaflet-container").wait_for()
                page.locator("[data-location-edit]").evaluate(
                    """form => {
                      const {center, radius} = form.locationPreviewMarkers;
                      center.setLatLng([35.7330, -78.8503]);
                      center.fire('drag', {target: center});
                      radius.setLatLng([35.7330, -78.8473]);
                      radius.fire('drag', {target: radius});
                    }"""
                )
                assert page.locator('input[name="latitude"]').input_value() == "35.733"
                assert page.locator('input[name="radius_feet"]').input_value() != "490"
                page.get_by_role("link", name="Cancel").click()
                page.get_by_role("link", name="Dashboard").click()
                page.locator(".progress-percent-value").evaluate(
                    "element => { element.textContent = '108%'; }"
                )
                assert page.locator(".progress-percent-value").text_content() == "108%"
                collisions = page.locator(".progress-visual svg").evaluate(
                    """svg => {
                      const labels = [...svg.querySelectorAll('.progress-label text')]
                        .map(label => ({
                          text: label.textContent,
                          bounds: label.getBBox(),
                        }));
                      const paths = [...svg.querySelectorAll(
                        '.progress-track, .progress-arc'
                      )];
                      return paths.flatMap(path => {
                        const length = path.getTotalLength();
                        return Array.from({length: 101}, (_, index) =>
                          path.getPointAtLength(length * index / 100)
                        ).flatMap(point => labels.filter(label =>
                          point.x >= label.bounds.x - 8 &&
                          point.x <= label.bounds.x + label.bounds.width + 8 &&
                          point.y >= label.bounds.y - 8 &&
                          point.y <= label.bounds.y + label.bounds.height + 8
                        ).map(label => ({path: path.className.baseVal, text: label.text})));
                      });
                    }"""
                )
                assert collisions == []
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
                page.get_by_role("link", name="Import or export").click()
                csv_download = page.get_by_role("link", name="Download CSV backup")
                csv_download.click()
                page.get_by_role("link", name="Save CSV backup").wait_for()
                assert page.url == f"{url}/imports"
                assert (
                    'Tap "Save CSV backup" above to download.'
                    in page.locator("[data-export-status]").text_content()
                )
                page.get_by_role("link", name="Save CSV backup").click()
                page.wait_for_function("window.sharedExportName === 'driving-log.csv'")
                assert page.evaluate("window.sharedExportKeys") == ["files"]
                assert page.url == f"{url}/imports"
                page.get_by_role("link", name="History").click()
                filters = page.locator(".history-controls")
                assert filters.get_attribute("open") is None
                filters.locator("summary").click()
                start_date = page.locator('input[name="start_date"]')
                end_date = page.locator('input[name="end_date"]')
                start_box = start_date.bounding_box()
                end_box = end_date.bounding_box()
                panel_box = filters.bounding_box()
                assert start_box and end_box and panel_box
                assert abs(start_box["width"] - end_box["width"]) <= 1
                assert end_box["x"] + end_box["width"] <= panel_box["x"] + panel_box["width"]
                assert (
                    page.evaluate(
                        "document.documentElement.scrollWidth - "
                        "document.documentElement.clientWidth"
                    )
                    <= 0
                )
                assert page.locator('option[value="last_week"]').text_content() == "Last week"
                assert page.locator('option[value="last_year"]').text_content() == "Last year"
                start_date.fill("2025-08-01")
                assert page.locator('select[name="period"]').input_value() == "custom"
                end_date.fill("2025-08-10")
                start_date.fill("2025-08-15")
                assert end_date.input_value() == "2025-08-15"
                filters.get_by_role("button", name="Clear").click()
                assert filters.get_attribute("open") is not None
                assert start_date.input_value() == ""
                assert end_date.input_value() == ""
                assert page.locator('select[name="period"]').input_value() == "all"
                page.goto(url)
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
                    geolocation={"latitude": 35.7327, "longitude": -78.8503},
                    permissions=["geolocation"],
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
                recovered.get_by_text("Drive ended.").wait_for()
                assert recovered.locator('input[name="end_location"]').input_value() == "Home"
                start_input = recovered.locator('input[name="started_at_local"]')
                end_input = recovered.locator('input[name="ended_at_local"]')
                duration_minutes = recovered.locator("[data-duration-minutes]")
                assert start_input.input_value()
                original_end = end_input.input_value()
                assert original_end
                duration_minutes.fill("2")
                assert end_input.input_value() != original_end
                assert recovered.locator("[data-time-editor]").is_visible()
                assert recovered.get_by_text("Repeated-time options").count() == 0
                overflow = recovered.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                assert overflow <= 0
                assert recovered.locator('input[name="acknowledge_warnings"]').count() == 0
                assert recovered.locator('input[name="supervisor_dl_number"]').count() == 0
                assert recovered.locator('input[name="supervisor_dl_state"]').count() == 0
                recovered.goto(f"{url}/dmv")
                recovered.locator('select[name="display_name"]').select_option("Sean Ahern")
                recovered.locator('input[name="dl_number"]').fill("SYNTHETIC-1234")
                recovered.locator('input[name="dl_state"]').fill("NC")
                with recovered.expect_navigation():
                    recovered.get_by_role("button", name="Save supervising driver").click()
                assert recovered.get_by_text("••••••••••1234, NC").is_visible()
                overflow = recovered.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                assert overflow <= 0
                fresh.close()
                browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
