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

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
