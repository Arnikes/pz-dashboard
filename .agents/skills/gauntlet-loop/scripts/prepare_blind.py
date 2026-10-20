#!/usr/bin/env python3
"""Create randomized neutral file copies for a best-effort blind A/B critic pass."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import secrets
import shutil
import sys
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def full_suffix(path: Path) -> str:
    return "".join(path.suffixes).lower()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy two equivalent files to randomized candidate-a/candidate-b names without persisting the mapping."
    )
    parser.add_argument("--ours", required=True, help="Our identity-scrubbed file.")
    parser.add_argument("--bar", required=True, help="The identity-scrubbed bar file.")
    parser.add_argument("--output-dir", required=True, help="A new directory for the neutral copies.")
    parser.add_argument("--seed", type=int, help="Deterministic test seed; omit during real judging.")
    args = parser.parse_args()

    ours = Path(args.ours).expanduser().resolve()
    bar = Path(args.bar).expanduser().resolve()
    output = Path(args.output_dir).expanduser().resolve()
    if not ours.is_file() or not bar.is_file():
        print("error: --ours and --bar must both be regular files", file=sys.stderr)
        return 2
    if ours == bar:
        print("error: --ours and --bar must be different files", file=sys.stderr)
        return 2
    ours_suffix = full_suffix(ours)
    bar_suffix = full_suffix(bar)
    if ours_suffix != bar_suffix:
        print("error: files must use the same full format/extension chain before blind preparation", file=sys.stderr)
        return 2
    if output.exists():
        print("error: --output-dir must not already exist", file=sys.stderr)
        return 2

    output.mkdir(parents=True)
    choose_ours_for_a = random.Random(args.seed).choice((True, False)) if args.seed is not None else bool(secrets.randbits(1))
    candidate_a = output / f"candidate-a{ours_suffix}"
    candidate_b = output / f"candidate-b{ours_suffix}"
    source_a, source_b = (ours, bar) if choose_ours_for_a else (bar, ours)
    shutil.copyfile(source_a, candidate_a)
    shutil.copyfile(source_b, candidate_b)

    result = {
        "candidate_a": str(candidate_a),
        "candidate_b": str(candidate_b),
        "candidate_a_sha256": digest(candidate_a),
        "candidate_b_sha256": digest(candidate_b),
        "ours_label": "A" if choose_ours_for_a else "B",
        "bar_label": "B" if choose_ours_for_a else "A",
        "mapping_persisted": False,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
