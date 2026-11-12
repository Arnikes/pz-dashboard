# Contributing

Use Python 3.12, Node.js 24 and Lua 5.1. Put `lua5.1` or `lua` on `PATH` for
the player-map tests (`sudo apt-get install lua5.1` on Ubuntu). The frontend is
served directly as HTML, CSS and JavaScript.

## Setup and checks

Windows / PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Linux / WSL (use a separate environment from Windows):

```bash
python3.12 -m venv .venv-linux
.venv-linux/bin/python -m pip install -r requirements-dev.txt
.venv-linux/bin/python -m playwright install --with-deps chromium
```

Run the following from the repository root. Here `python` means the checkout's
virtual-environment interpreter (`.venv/Scripts/python.exe` on Windows or
`.venv-linux/bin/python` on Linux/WSL).

```bash
python -m ruff format .
python -m ruff check .
python -m ruff format --check .
python scripts/check.py
```

Run the three repository-wide Ruff commands after the final Python edit. Run
`scripts/check.py` before committing. It checks installed dependencies, Ruff lint
and formatting, generated catalogs, every JavaScript file in `dashboard/static/`,
and all backend and Chromium tests.

Browser tests use real static assets with loopback fixtures for API and SSE.
Use the [isolated B42 stack](docs/acceptance-b42.md) for real game-server, Steam,
and RCON checks. The full suite uses up to four isolated workers. Use
`--workers 2` to limit parallelism or `--workers 0` to debug serially.
Reports go to `test-results/check-*/results.xml`. A failed run replays its
failures with traces and screenshots under that run's `diagnostic/` directory.
Use `--trace` to capture the initial run. Direct pytest runs write to
`test-results/direct/` by default.

```bash
python -m pytest -q tests/test_core.py
python -m pytest -q tests/browser --headed
python -m playwright show-trace path/to/trace.zip
```

Use English for repository documentation. When UI strings change, update both
`dashboard/static/locales/en.json` and `ru.json`, run
`python scripts/build_i18n.py`, and verify both languages. Keep UI assets local.
Use `PZ_VISUAL_EVIDENCE=1` for optional browser-test review captures and follow
the separate [README screenshot instructions](docs/screenshots/README.md) for
publication. Keep credentials, live server data and local test output out of Git.

## Graph and benchmarks

Install the optional Graphify tool separately from the test environment:

```bash
uv tool install --python 3.12 --constraints requirements-graphify.txt graphifyy
graphify extract . --code-only --no-cluster --max-workers 4
graphify diagnose multigraph
graphify cluster-only . --no-label --no-viz
graphify query "StreamCache"
graphify export html
```

Results are in the ignored `graphify-out/` directory. After code changes, run
`graphify update .`. This uses local syntax trees. Use `--force` for intentional
code deletions if the shrink guard blocks the update. Verify framework callbacks
and dynamically registered entry points in source and tests. The clustered graph
can merge distinct edges between the same nodes, so inspect the raw graph for
individual call sites.

Run synthetic benchmarks from the checkout's virtual environment:

```bash
python scripts/benchmark_cpu.py
python scripts/benchmark_resources.py
python scripts/benchmark_filesystem.py --baseline HEAD
```

The filesystem benchmark uses the specified revision for Workshop scanning and
backup listing. Replace `HEAD` with a trusted local commit to measure against
an earlier implementation. The gzip check uses `tar` as its reference.

## Repository map

| Path | Purpose |
| --- | --- |
| `dashboard/app.py`, `auth.py` | HTTP API and administrator sessions |
| `dashboard/ops.py`, `actions.py` | Operations, schedulers and action dispatch |
| `dashboard/config*.py`, `settingsmodel.py`, `workshop.py` | Profiles, revisions, validation and Workshop metadata |
| `dashboard/payloads.py`, `dockerlib.py`, `rcon.py` | Live data, Docker and RCON |
| `dashboard/static/` | UI, translations and PWA |
| `tests/`, `scripts/` | Tests, fixtures and maintenance commands |

See the [extension contracts](docs/extensions.md) for explicit application
composition and trusted extension examples. [PRODUCT.md](PRODUCT.md) and
[DESIGN.md](DESIGN.md) record product scope and UI rules used by the bundled
Impeccable frontend tooling. Local preview output goes in ignored
`.impeccable/` paths and release artwork in `output/`. Preserve the
[Impeccable license](.agents/skills/impeccable/LICENSE) and
[notice](.agents/skills/impeccable/NOTICE.md) when redistributing the tooling.

The [Gauntlet Loop skill](.agents/skills/gauntlet-loop/SKILL.md) runs
reference-based reviews. Its helper can be checked with
`python .agents/skills/gauntlet-loop/scripts/gauntlet_state.py self-test`.
Preserve its [attribution](.agents/skills/gauntlet-loop/references/attribution.md).

## GitHub and Gitea CI

Both workflows run the quality suite and smoke-test the panel image with its
shipped health check on `main` and `master` pushes, pull requests, manual
runs and `v*` tags. The [GitHub workflow](.github/workflows/ci.yml) runs the
jobs in parallel and falls back to Amazon ECR Public if the Docker Hub base image
pull fails or times out. The [Gitea workflow](.gitea/workflows/ci.yml) runs the
image job after the quality suite. Both publish the image that passed the smoke test.

### Publish a GitHub release

Push a new `v*` tag to GitHub. Before tagging, commit optional English release
notes in `docs/releases/<tag>.md`. The workflow adds the pinned image digest,
generated change notes, source archives and SHA-256 checksums to the release.
It keeps partial uploads in a draft for retry.

If `origin` points to Gitea, add the project's GitHub remote if needed:

```bash
git remote add github https://github.com/Arnikes/pz-dashboard.git
```

Point local `main` at the verified release commit. Use an unused version and
the GitHub remote's name:

```bash
git push github main
git tag -a v1.10.0 main -m "PZ Console v1.10.0"
git push github v1.10.0
```

The GitHub release job publishes `linux/amd64` images to
`ghcr.io/<owner>/<repository>` with a version tag and `sha-<full-commit>`.
Stable `vMAJOR.MINOR.PATCH` tags also update `latest`. Version suffixes such as
`-rc.1` mark prereleases. The workflow uses `GITHUB_TOKEN` with
`contents: write` and `packages: write`; allow those permissions for Actions
and grant the repository access to the GHCR package. Set package visibility to
public for anonymous pulls. See [deploying a release image](docs/installation.md#released-images-from-ghcr).

### Publish and deploy with Gitea

Pushes to `main`, `v*` tags and manual runs publish `latest` and
`sha-<7-character-commit>`, plus the version tag for tag runs. Use a
Docker-compatible release tag and configure the actor's `REGISTRY_TOKEN` secret
with `write:package`.

To enable SSH deployment after publication, set `DEPLOY_ENABLED=true` and
the variables `DEPLOY_HOST`, `DEPLOY_USER` and `DEPLOY_PATH`, then add the
`DEPLOY_SSH_KEY` secret. The target Compose service `pz-dashboard` must use
`image:` pointing to the registry image.
