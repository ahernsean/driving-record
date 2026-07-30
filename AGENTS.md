# CI validation

Before pushing a pull-request change, read `.github/workflows/ci.yml` and run
the matching checks. Do not report a check as passing unless its command has
completed with exit code 0 and its pytest/formatter summary is present in the
terminal output. An empty or prematurely returned tool response is not test
evidence.

For the current workflow, run the quality and non-browser suite exactly as CI
does:

```sh
ruff check .
ruff format --check .
mypy
pytest -m "not browser" --cov --cov-report=term-missing --cov-fail-under=95
git diff --check
```

Also run `pytest -m browser` with CI's WebKit setup when available. If local
WebKit is unavailable, state that limitation explicitly and run the project's
configured local-browser fallback only when it is documented by the test.
Inspect each PR's GitHub check results after pushing; distinguish a pre-existing
or unrelated failure from one introduced by the branch with log evidence.
