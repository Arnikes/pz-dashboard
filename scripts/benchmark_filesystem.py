"""Compare filesystem CPU/RAM work with a trusted local Git baseline.

Uses synthetic temporary files only, without Docker, RCON or game-server data.
Baseline functions are compiled from the specified checkout revision; only use
revisions you trust. CPU timings exclude fixture setup and subprocess CPU time.
"""

import argparse
import ast
import gc
import io
import json
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import config  # noqa: E402
import ops  # noqa: E402
import workshop  # noqa: E402


def baseline_function(reference, module, name):
    source = subprocess.run(
        ["git", "show", f"{reference}:dashboard/{module.__name__}.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<baseline>", "exec"), namespace)
    return namespace[name]


def measure(function):
    samples = []
    for _ in range(5):
        gc.collect()
        cpu, wall = time.process_time(), time.perf_counter()
        iterations = 0
        # Accumulate work across coarse Windows CPU-clock ticks, while capping
        # subprocess-heavy samples at one second of wall time.
        while time.process_time() - cpu < 0.25 and time.perf_counter() - wall < 1.0:
            result = function()
            iterations += 1
        samples.append(
            (
                (time.process_time() - cpu) / iterations,
                (time.perf_counter() - wall) / iterations,
            )
        )
    gc.collect()
    tracemalloc.start()
    function()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, {
        "median_python_cpu_seconds": round(statistics.median(s[0] for s in samples), 6),
        "median_wall_seconds": round(statistics.median(s[1] for s in samples), 6),
        "peak_python_bytes": peak,
    }


def compare(before, after):
    old_result, old_metrics = measure(before)
    new_result, new_metrics = measure(after)
    assert old_result == new_result
    return {"before": old_metrics, "after": new_metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", required=True, help="Trusted local Git revision before the change"
    )
    args = parser.parse_args()
    previous_scan = baseline_function(args.baseline, workshop, "local_files")
    previous_backups = baseline_function(args.baseline, ops, "list_backups")
    temp_root = ROOT / ".tmp-pytest"
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="resource-benchmark-", dir=temp_root) as temporary:
        root = Path(temporary)
        workshop_root = root / "workshop"
        for index in range(20):
            folder = workshop_root / "123" / "mods" / f"mod-{index}" / "media"
            folder.mkdir(parents=True)
            (folder.parent / "mod.info").write_text(f"name=Example {index}")
            for asset in range(500):
                (folder / f"texture-{asset}.png").touch()
        backup_root = root / "backups"
        backup_root.mkdir()
        for index in range(2000):
            (backup_root / f"world-{index}.tar.gz").touch()
        archive_path = root / "many-files.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for index in range(20000):
                member = tarfile.TarInfo(f"Saves/Multiplayer/world/map_{index:06d}.bin")
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))

        def previous_validation():
            subprocess.run(
                ["tar", "-tzf", str(archive_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                check=True,
            )

        with (
            patch.object(workshop, "roots", lambda: [workshop_root]),
            patch.dict(previous_scan.__globals__, {"roots": lambda: [workshop_root]}),
            patch.dict(config.CFG, {"backup_dir": str(backup_root)}),
        ):
            results = {
                "baseline": args.baseline,
                "python": sys.version.split()[0],
                "median_of_runs": 5,
                "workshop_10000_assets_20_metadata_files": compare(
                    lambda: previous_scan(["123"]), lambda: workshop.local_files(["123"])
                ),
                "overview_count_2000_backups": compare(
                    lambda: len(previous_backups()), ops.count_backups
                ),
                "list_2000_backups": compare(previous_backups, ops.list_backups),
                "validate_gzip_20000_tar_members": compare(
                    previous_validation, lambda: ops._validate_backup_gzip(str(archive_path))
                ),
            }
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
