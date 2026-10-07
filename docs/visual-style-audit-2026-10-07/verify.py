"""Replay the accepted style fixes with isolated editor and offline fixtures."""

from pathlib import Path
import subprocess
import sys
import uuid


def main():
    folder = Path(__file__).resolve().parent
    root = folder.parents[1]
    target = root / "tests/browser/test_tmp_visual_style_fixes.py"
    if target.exists():
        raise SystemExit(f"Refusing to overwrite existing verification fixture: {target}")
    temporary = root / f".tmp-visual-style-verification-{uuid.uuid4().hex}"
    source = (folder / "verification-fixture.txt").read_text(encoding="utf-8")
    source = source.replace(
        ' / ".tmp-visual-style-fix-evidence"', f" / {temporary.name!r} / 'evidence'"
    )
    target.write_text(source, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                str(target),
                f"--basetemp={temporary / 'tests'}",
            ],
            cwd=root,
            check=False,
        )
        print(f"Verification evidence: {temporary / 'evidence'}")
        return result.returncode
    finally:
        target.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
