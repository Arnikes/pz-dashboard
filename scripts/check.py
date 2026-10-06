"""Run the same quality checks locally and in CI, from any working directory."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Python 3.12, matching dashboard/Dockerfile.")
    # Keep pytest's numbered temporary directories local to this checkout.
    temp_root = ROOT / ".tmp-pytest"
    temp_root.mkdir(exist_ok=True)
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(temp_root))
    commands = [
        [sys.executable, "-m", "pip", "check"],
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "scripts/build_i18n.py", "--check"],
        ["node", "--check", "dashboard/static/i18n.js"],
        ["node", "--check", "dashboard/static/localize.js"],
        ["node", "--check", "dashboard/static/catalogs.js"],
        ["node", "--check", "dashboard/static/app.js"],
        ["node", "--check", "dashboard/static/editor.js"],
        ["node", "--check", "dashboard/static/login.js"],
        ["node", "--check", "dashboard/static/pwa.js"],
        ["node", "--check", "dashboard/static/sw.js"],
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests",
            "--tracing=retain-on-failure",
            "--screenshot=only-on-failure",
        ],
    ]
    for command in commands:
        print("+ " + " ".join(command), flush=True)
        try:
            subprocess.run(command, cwd=ROOT, check=True)
        except FileNotFoundError:
            raise SystemExit(f"Required executable not found: {command[0]}") from None
        except subprocess.CalledProcessError as error:
            raise SystemExit(error.returncode) from None


if __name__ == "__main__":
    main()
