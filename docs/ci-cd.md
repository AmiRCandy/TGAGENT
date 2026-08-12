# CI/CD

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push to
`main` and every pull request. Four jobs, chosen so that each failure is
unambiguous and none of them duplicates another.

## Jobs

### `lint` — style and types

One interpreter, because neither ruff nor mypy behaves differently across
versions and a matrix here would only burn minutes.

```bash
ruff check --output-format=github .
ruff format --check --diff .
mypy
```

mypy runs in **strict** mode over `src/tgagent` (65 files, zero errors).

### `test` — the suite

| OS | Python |
|---|---|
| ubuntu | 3.11, 3.12, 3.13 |
| windows | 3.12 |

Windows is in the matrix deliberately: the sandbox genuinely behaves differently
there (no `setrlimit`), and that difference has broken things before. One job is
enough to catch it without tripling the matrix.

`fail-fast: false`, so one version failing does not hide the others.

### `security`

Split from `lint` so a finding is unambiguous in the checks list.

```bash
bandit -r src/tgagent -c pyproject.toml --skip B102,B307
pip-audit --strict --desc
```

`B102`/`B307` (`exec`/`eval`) are skipped because the sandbox worker uses `exec`
**by design** — that is the entire feature. It is reviewed by hand and by the
isolation tests rather than by a static rule.

The job also fails the build if a session file or `.env` is ever tracked:

```bash
if git ls-files | grep -E '\.(session|session-journal)$|^\.env$'; then
  echo "::error::Session or .env files must never be committed."
  exit 1
fi
```

A committed session file is a live account takeover, so this is a hard failure
rather than a warning.

### `package` — does it actually install?

The check that catches a missing dependency or a broken entry point, which no
amount of unit testing will find:

```bash
python -m build
twine check dist/*
python -m venv /tmp/clean
/tmp/clean/bin/pip install dist/*.whl
/tmp/clean/bin/tgagent version
```

A clean virtualenv, the built wheel, and the console script actually running.

### `ci-ok`

A single required status. Branch protection needs one entry rather than a list
that drifts out of date as the matrix changes.

## Making it fast

- **Concurrency group per ref** — a new push cancels the in-flight run.
- **pip caching** keyed on `pyproject.toml`.
- **`lint` and `security` run in parallel** with `test`; nothing waits on
  anything it does not need.
- **No coverage threshold gate.** Coverage is uploaded as an artifact for
  inspection. A percentage does not tell you whether the *security-relevant*
  paths are covered, so gating on it would be theatre.

Typical wall clock: 2–4 minutes.

## No credentials anywhere

CI has no Telegram account and no LLM key, and it sets:

```yaml
TGAGENT_TELEGRAM__API_ID: "0"
TGAGENT_TELEGRAM__API_HASH: ""
```

so that a test which somehow tried to connect fails loudly rather than silently
picking up a developer's real credentials.

## Release

[`.github/workflows/release.yml`](../.github/workflows/release.yml), triggered by
a `v*` tag.

1. Build sdist and wheel.
2. `twine check`.
3. **Verify the tag matches `__about__.py`.** A tag that disagrees with the
   package version produces a release nobody can reproduce.
4. Publish to PyPI via **trusted publishing** — no long-lived token stored in the
   repository.

```bash
# bump src/tgagent/__about__.py, then:
git tag v0.2.0 && git push origin v0.2.0
```

`workflow_dispatch` with `dry_run: true` builds and validates without publishing.

## Dependency updates

[`dependabot.yml`](../.github/dependabot.yml): weekly for pip, monthly for
actions and Docker. Dev-tool bumps are **grouped into one PR** — reviewing five
separate ruff patches is not a good use of anyone's time.

Telethon **major** upgrades are excluded. Telethon 2.x is a breaking rewrite;
migrating is a deliberate task, not something to be nudged into by a bot.

## Running CI locally

```bash
ruff check . && ruff format --check .
mypy
pytest
bandit -r src/tgagent -c pyproject.toml --skip B102,B307 -q
pip-audit --strict
python -m build && twine check dist/*
```

Or install the pre-commit hooks — see [development](development.md).
