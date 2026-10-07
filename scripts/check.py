"""Run the same quality checks locally and in CI, from any working directory."""

import argparse
import errno
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def worker_count(value):
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Workers must be a non-negative integer.") from None
    if count < 0:
        raise argparse.ArgumentTypeError("Workers must be a non-negative integer.")
    return count


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=worker_count,
        default=min(4, os.cpu_count() or 1),
        help="Parallel pytest processes (default: CPU count, capped at 4; 0 runs serially).",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Record initial browser traces (slower); otherwise replay failures with tracing.",
    )
    return parser.parse_args(argv)


def diagnose_failures(workers, cache_dir, output_dir):
    """Retain replay traces even if a failure does not reproduce; never change the gate."""
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests",
        "-n",
        str(workers),
        "--dist=worksteal",
        "--lf",
        "--last-failed-no-failures=none",
        "--tracing=on",
        "--screenshot=on",
        "-o",
        f"cache_dir={cache_dir}",
        f"--output={output_dir}/diagnostic",
        f"--junitxml={output_dir}/diagnostic/results.xml",
    ]
    print("+ Diagnostic replay: " + " ".join(command), flush=True)
    try:
        subprocess.run(command, cwd=ROOT, check=False)
    except OSError as error:
        print(f"Diagnostic replay could not start: {error}", flush=True)


@contextmanager
def test_run_lock():
    """Queue full suites in one checkout, while each suite uses parallel workers."""
    with (ROOT / ".tmp-pytest" / "check.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        if os.name == "nt":
            import msvcrt

            def acquire():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        waiting = False
        while True:
            try:
                acquire()
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if not waiting:
                    print("Another check is running tests in this checkout; waiting...", flush=True)
                    waiting = True
                time.sleep(0.5)
        try:
            yield
        finally:
            release()


def run_test_suite(command, args, cache_dir, output_dir):
    with test_run_lock():
        try:
            subprocess.run(command, cwd=ROOT, check=True)
        except subprocess.CalledProcessError:
            if not args.trace:
                # Record traces only for the failed cases, keeping the original
                # screenshots and exit status even when a replay passes.
                diagnose_failures(args.workers, cache_dir, output_dir)
            raise


def main():
    args = parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Python 3.12, matching dashboard/Dockerfile.")
    # Keep pytest's numbered temporary directories local to this checkout.
    temp_root = ROOT / ".tmp-pytest"
    temp_root.mkdir(exist_ok=True)
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(temp_root))
    # A second check must not replace this run's cached failures or artifacts.
    with tempfile.TemporaryDirectory(prefix="check-", dir=temp_root) as cache_dir:
        run_checks(args, cache_dir)


def run_checks(args, cache_dir):
    output_dir = f"test-results/{Path(cache_dir).name}"
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
        ["node", "--check", "dashboard/static/alerts.js"],
        ["node", "--check", "dashboard/static/sw.js"],
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests",
            "-n",
            str(args.workers),
            "--dist=worksteal",
            "--durations=20",
            "--tracing=retain-on-failure" if args.trace else "--tracing=off",
            "--screenshot=only-on-failure",
            "-o",
            f"cache_dir={cache_dir}",
            f"--output={output_dir}",
        ],
    ]
    for command in commands:
        print("+ " + " ".join(command), flush=True)
        started = time.perf_counter()
        try:
            if command is commands[-1]:
                run_test_suite(command, args, cache_dir, output_dir)
            else:
                subprocess.run(command, cwd=ROOT, check=True)
        except FileNotFoundError:
            raise SystemExit(f"Required executable not found: {command[0]}") from None
        except subprocess.CalledProcessError as error:
            raise SystemExit(error.returncode) from None
        print(f"Completed in {time.perf_counter() - started:.2f}s", flush=True)


if __name__ == "__main__":
    main()
