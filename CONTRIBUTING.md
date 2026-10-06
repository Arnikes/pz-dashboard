# Contributing

Use Python **3.12** (matching `.python-version` and the Docker image) and Node.js **24**
for JavaScript syntax checks. The UI uses plain HTML/CSS/JavaScript; no frontend build
step is required. Runtime and development dependencies are pinned.

## Setup and checks

Windows / PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts/check.py
```

Linux / WSL (use a separate environment from Windows):

```bash
python3.12 -m venv .venv-linux
.venv-linux/bin/python -m pip install -r requirements-dev.txt
.venv-linux/bin/python -m playwright install --with-deps chromium
.venv-linux/bin/python scripts/check.py
```

With the environment activated, `python scripts/check.py` is the shared local/CI entrypoint.
It checks dependency consistency, Ruff lint/format, generated i18n catalogs,
JavaScript syntax, backend tests, and Chromium browser tests.

Browser tests serve real static assets on loopback and intercept API/SSE with isolated
fixtures. They do not need Docker, Steam, PZ, RCON, or Telegram.
Temporary pytest files go in `.tmp-pytest/`; failed browser traces/screenshots go in
`test-results/`. Both are ignored.

## Useful commands

```bash
python -m ruff check .
python -m ruff format .
python scripts/build_i18n.py --check
python -m pytest -q tests/test_core.py
python -m pytest -q tests/browser --headed
python -m playwright show-trace test-results/<test-name>/trace.zip
```

When changing visible strings, update source-keyed English/Russian translations via
`scripts/build_i18n.py` and check both languages. Keep UI assets local; see
[font provenance](dashboard/static/fonts/README.md).
When changing configuration or Workshop behavior, cover revisions, secret masking,
and failed operations with isolated tests. Real integration uses the
[separate acceptance stack](docs/acceptance-b42.md).

## Repository map

| Path | Purpose |
| --- | --- |
| `dashboard/app.py`, `auth.py` | HTTP/API and administrator sessions |
| `dashboard/ops.py`, `actions.py` | Operations, schedulers, action dispatch |
| `dashboard/config*.py`, `settingsmodel.py` | Profiles, revisions, parsing, recovery, validation |
| `dashboard/workshop.py` | Workshop metadata and B42 resolution |
| `dashboard/payloads.py`, `dockerlib.py`, `rcon.py` | Shared live data, Docker and RCON |
| `dashboard/static/` | Frontend, translations, fonts, icons, PWA |
| `tests/`, `tests/browser/` | Backend and browser fixtures/checks |
| `scripts/` | Quality, catalogs, acceptance utilities, benchmarks |
| `docs/` | Installation, workflows, acceptance, screenshot source |
| `.github/workflows/ci.yml` | GitHub checks and Docker smoke test |
| `.gitea/workflows/` | Existing Gitea checks and optional registry/deploy jobs |

## GitHub and Gitea CI

GitHub CI runs quality checks, builds the panel image, and checks `/api/health`.
It does not publish images or deploy a server. Browser failure artifacts are retained.
The existing Gitea workflows remain available for installations using that forge.

Gitea CD publishes `latest`, `sha-<commit>`, and version tags with `REGISTRY_TOKEN`
(scope `write:package`). SSH deployment is enabled only with `DEPLOY_ENABLED=true`
and `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`, `DEPLOY_SSH_KEY`.
Use a registry token belonging to the actor publishing the image.
A deployed panel must use `image:` instead of a local `build:` to pull that image.

## Publication hygiene

Never commit environment files, world saves, archives, session files, tokens, or
local test output. Keep fixtures fictional and screenshot regeneration isolated.
Historical prototypes and superseded UI evidence are available through Git history.
The bundled Impeccable skill and Codex hooks are development tools; they are not
part of the runtime image. Preserve their license and notice when redistributing.

Use English for repository documentation. The interface continues to support both languages.
Describe the behavior changed and checks run in pull requests.
