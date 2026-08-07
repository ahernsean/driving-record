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

Also run `pytest -m browser` with CI's WebKit setup when available. If local
WebKit is unavailable, state that limitation explicitly and run the project's
configured local-browser fallback only when it is documented by the test.
Inspect each PR's GitHub check results after pushing; distinguish a pre-existing
or unrelated failure from one introduced by the branch with log evidence.
