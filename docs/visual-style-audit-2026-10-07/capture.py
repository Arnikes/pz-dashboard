"""Recreate isolated visual audit evidence through the project's browser fixtures."""

from pathlib import Path
import re
import subprocess
import sys
import uuid


def main():
    folder = Path(__file__).resolve().parent
    root = folder.parents[1]
    target = root / "tests/browser/test_tmp_visual_style_audit.py"
    if target.exists():
        raise SystemExit(f"Refusing to overwrite existing capture fixture: {target}")
    source = folder / "capture-fixture.txt"
    temporary = root / f".tmp-visual-style-recapture-{uuid.uuid4().hex}"
    # Recapture the current UI without overwriting the audit's baseline evidence.
    fixture = re.sub(
        r""" / ["']docs["'] / ["']visual-style-audit-2026-10-07["'] / ["']evidence["']""",
        f" / {temporary.name!r} / 'evidence'",
        source.read_text(encoding="utf-8"),
    )
    target.write_text(fixture, encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(target), f"--basetemp={temporary}"],
            cwd=root,
            check=False,
        )
        print(f"Current UI captures: {temporary / 'evidence'}")
        return result.returncode
    finally:
        target.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
