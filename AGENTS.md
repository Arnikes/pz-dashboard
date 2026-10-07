# Repository checks

Use Python 3.12 and the pinned dependencies in `requirements-dev.txt`.
Use the checkout's virtual environment (`.venv/Scripts/python.exe` on Windows;
`.venv-linux/bin/python` on Linux/WSL when following `CONTRIBUTING.md`).

After the final Python edit, run `python -m ruff format .`, then
`python -m ruff check .` and `python -m ruff format --check .` with that interpreter.
Check the whole repository, even when only one file was changed: CI checks the
whole tree, and a clean lint result does not imply clean formatting.

Before reporting a change ready for commit or CI, run `python scripts/check.py`
with the same interpreter. If any files change afterward, rerun the affected
checks; Python changes always require another repository-wide lint/format pass.
If the full check cannot finish, report which checks passed and what remains
unverified. Do not disable CI checks or add exclusions to hide a failure.

Keep repository documentation in English and user-facing text bilingual as
described in `CONTRIBUTING.md`.
