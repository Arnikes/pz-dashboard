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

The full suite runs in isolated pytest-xdist processes, using up to four CPUs.
Each process has its own browser, loopback servers, and temporary test files;
individual tests still get fresh browser contexts. Work stealing balances slow
browser scenarios with shorter backend tests. Simultaneous `scripts/check.py`
invocations in the same checkout queue their test stage to avoid oversubscribing
CPU and browser memory; each active suite still uses its configured worker count.
The slowest 20 test phases are
printed after each run. The passing path avoids recording and compressing traces
that would be discarded. Each check uses a private failure cache and a unique
`test-results/check-*/` artifact directory, so concurrent checks cannot erase
each other's evidence. After a failed suite, its cached failures are automatically
replayed with traces and screenshots retained in that run's `diagnostic/`
subdirectory, even if the replay passes.
The original failure still fails the check. Use `python scripts/check.py --trace`
to capture traces on the initial run, including intermittent failures.
Use `python scripts/check.py --workers 2` to limit resource usage, or
`python scripts/check.py --workers 0` for a serial run when debugging or comparing
timings. Direct `python -m pytest` commands remain serial unless given `-n`.

Before committing, format the whole repository with the same virtual environment:
`python -m ruff format .`, then run `python scripts/check.py` after the final edit.
Running only `ruff check` or selected tests is insufficient: Ruff lint and format
are separate checks, and CI checks formatting across the entire repository.
If you edit Python after a successful check, repeat the lint and format checks.

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
| `.github/workflows/ci.yml` | GitHub checks, Docker smoke test, GHCR and releases |
| `.gitea/workflows/` | Existing Gitea checks and optional registry/deploy jobs |

## GitHub and Gitea CI

GitHub CI runs on pushes to `main`/`master`, pull requests, manual runs, and pushed
tags matching `v*`. It runs quality checks, builds the panel image, and checks
`/api/health`. Browser failure artifacts are retained. Only a **tag push** publishes
to GHCR and creates a GitHub Release; branch, pull-request, and manual runs perform checks.

GitHub builds use Docker's official `python:3.12-alpine` image from
[Amazon ECR Public](https://gallery.ecr.aws/docker/library/python) to avoid
Docker Hub's anonymous pull quota on shared runners. No registry credentials are
needed to pull this base image. Local builds default to Docker Hub; use
`--build-arg PYTHON_BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-alpine`
with `docker build` to select the same mirror.

For tag pushes, the exact tested image is transferred to the release job instead of
being rebuilt. The release job uses the automatic `GITHUB_TOKEN` with `packages: write`
and `contents: write`; no registry password or personal access token is needed.
Repository/organization policy must allow those permissions. The image's source
label links the package to the GitHub repository. For an existing GHCR package,
grant that repository Actions write access in the package settings. New packages
default to private; set package visibility to public for anonymous pulls. See
[GitHub's Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

### Publish a GitHub release

Commit the workflow, screenshots, and release changes before tagging. Push the
commit and tag to **GitHub** (a tag pushed only to Gitea does not run GitHub Actions).
If `origin` points to Gitea, first add your GitHub repository as a separate remote:

```bash
git remote add github https://github.com/YOUR-OWNER/pz-console.git
git push github HEAD:main
git tag -a v1.0.0 -m "PZ Console v1.0.0"
git push github v1.0.0
```

If `origin` already points to GitHub, use `origin` instead of `github`.

After all checks pass, the workflow publishes the `linux/amd64` panel image as
`ghcr.io/<owner>/<repository>:v1.0.0` and `:sha-<full-commit>` (the repository path
is lowercased). A stable `vMAJOR.MINOR.PATCH` tag also updates `:latest`.
Tags such as `v1.1.0-rc.1` create prereleases and leave `latest` unchanged.
Other `v*` tags also publish; characters unsupported by Docker are replaced by
hyphens and image tags are limited to 128 characters. Release notes include the
versioned image and its immutable digest, so deployments can pin the exact image.

The GitHub Release includes generated change notes, `pz-console-source.tar.gz`,
`pz-console-source.zip`, and `SHA256SUMS`, built from the tagged commit's tracked
files. It remains a draft until all assets are uploaded. Failed uploads can be
retried by rerunning the workflow; an already published release is left intact.
Use a new version tag for each release rather than moving published tags.
The workflow does not restart a running server; see
[using a released image](docs/installation.md#released-images-from-ghcr).

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
