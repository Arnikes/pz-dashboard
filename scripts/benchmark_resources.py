"""Synthetic before/after resource checks; no Docker or game server required."""

import gc
import json
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import config  # noqa: E402
import dockerlib  # noqa: E402
import ops  # noqa: E402
import payloads  # noqa: E402


def measure(fn):
    gc.collect()
    tracemalloc.start()
    cpu, wall = time.process_time(), time.perf_counter()
    result = fn()
    duration = time.perf_counter() - wall
    cpu = time.process_time() - cpu
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, {
        "seconds": round(duration, 4),
        "cpu_seconds": round(cpu, 4),
        "python_peak_mib": round(peak / 1024**2, 3),
    }


def main():
    results = {}
    temp_root = ROOT / ".tmp-pytest"
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_root) as directory:
        journal = Path(directory) / "backups.jsonl"
        line = (
            json.dumps(
                {
                    "ts": "2026-10-04T12:00:00Z",
                    "name": "pz-backup.tar.gz",
                    "status": "success",
                    "detail": "x" * 150,
                }
            )
            + "\n"
        )
        with journal.open("w", encoding="utf-8") as target:
            for _ in range(200000):
                target.write(line)

        def old_journal():
            # Previous get_backup_journal read the entire journal on every call.
            lines = journal.read_text(encoding="utf-8").splitlines()
            return [json.loads(record) for record in reversed(lines[-30:])]

        before, old_metrics = measure(old_journal)
        with patch.dict(config.CFG, {"dashboard_dir": directory}):
            after, new_metrics = measure(lambda: ops.get_backup_journal(30))
        assert before == after
        results["journal"] = {
            "size_mib": round(journal.stat().st_size / 1024**2, 2),
            "before": old_metrics,
            "after": new_metrics,
        }

        calls = [0]
        raw = json.dumps({"ok": True, "items": [{"id": i, "text": "x" * 150} for i in range(1000)]})

        def collect(name, **_kwargs):
            calls[0] += 1
            return json.loads(raw)

        def old_stream():
            for _ in range(8):
                json.dumps(collect("events"), ensure_ascii=False).encode("utf-8")

        _, old_metrics = measure(old_stream)
        old_metrics["collector_calls"] = calls[0]
        calls[0] = 0
        cache = payloads.StreamCache()
        with patch.object(payloads, "stream_payload", collect):
            _, new_metrics = measure(lambda: [cache.frame("events", 12) for _ in range(8)])
        new_metrics["collector_calls"] = calls[0]
        results["eight_subscribers"] = {"before": old_metrics, "after": new_metrics}

        # Model full-log export with a local producer and disk sink for HTTP output.
        command = [
            sys.executable,
            "-c",
            "import os; chunk=b'x'*65535+b'\\n'; [os.write(1,chunk) for _ in range(512)]",
        ]
        real_run = subprocess.run

        def run_producer(args, **kwargs):
            return real_run(command, **kwargs)

        def old_export():
            code, text, _ = dockerlib.sh(command, merge_stderr=True)
            assert code == 0
            body = text.encode("utf-8")
            with tempfile.TemporaryFile(dir=directory) as response:
                response.write(body)
            return len(body)

        def new_export():
            with tempfile.TemporaryFile(dir=directory) as log:
                with patch.object(dockerlib.subprocess, "run", run_producer):
                    assert ops.full_logs(log)
                size = log.tell()
                log.seek(0)
                with tempfile.TemporaryFile(dir=directory) as response:
                    shutil.copyfileobj(log, response, length=256 * 1024)
                return size

        size_before, old_metrics = measure(old_export)
        size_after, new_metrics = measure(new_export)
        # The old sh wrapper stripped the trailing newline. The new export preserves it.
        assert size_after == size_before + 1
        results["full_log_export"] = {
            "size_mib": size_after / 1024**2,
            "before": old_metrics,
            "after": new_metrics,
        }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
