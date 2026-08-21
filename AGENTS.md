# CI validation

Before pushing a pull-request change, make sure your changes are tested. Read
`.github/workflows/ci.yml` to get a sense of the tests that will run in CI.
Don't assume a test is passing unless its command has completed with exit code
0 and its pytest/formatter summary is present in the terminal output. An empty
or prematurely returned tool response is not test evidence.

For the current workflow, note that CI runs with `PYTHONPATH=.`:

```sh
PYTHONPATH=. .venv/bin/ruff check .
PYTHONPATH=. .venv/bin/ruff format --check .
PYTHONPATH=. .venv/bin/mypy
PYTHONPATH=. .venv/bin/pytest -m "not browser" --cov --cov-report=term-missing --cov-fail-under=95
git diff --check
```

Also run `pytest -m browser` with CI's WebKit setup when available. On this
Rocky host, the bundled WebKit cannot launch because of its system-library
requirements. Use a rootless `podman` (or Docker) installation and network
access to the Microsoft Playwright image to run WebKit in a matching container:

```sh
WEBKIT_VERSION="$(.venv/bin/python -c 'from importlib.metadata import version; print(version("playwright"))')"
podman run --rm -d --name driving-record-webkit --network host \
  "mcr.microsoft.com/playwright:v${WEBKIT_VERSION}-noble" \
  npx -y "playwright@${WEBKIT_VERSION}" run-server --port 31747 --host 127.0.0.1
podman logs driving-record-webkit  # wait for "Listening on ws://127.0.0.1:31747/"
DRIVING_LOG_WEBKIT_WS_ENDPOINT=ws://127.0.0.1:31747/ \
  PYTHONPATH=. .venv/bin/pytest -m browser
podman stop driving-record-webkit
```

The image and installed Python Playwright version must match because the
run-server protocol is version-specific. `--network host` lets containerized
WebKit reach the temporary `127.0.0.1` server created by the tests. If this
setup is unavailable, state that limitation explicitly and run the project's
configured Chromium fallback only when the test documents it.
Inspect each PR's GitHub check results after pushing; distinguish a pre-existing
or unrelated failure from one introduced by the branch with log evidence.
