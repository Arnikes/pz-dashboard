#!/usr/bin/env python3
"""Persist and validate Codex Gauntlet Loop runs using only the standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
VERDICTS = ("ours", "bar", "tie", "invalid")
GATE_STATUSES = ("pass", "fail", "na")
FINISH_STATUSES = ("won", "stopped", "blocked")


class GauntletError(RuntimeError):
    """Raised for an invalid or unsafe ledger operation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str, limit: int = 44) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "run")[:limit].rstrip("-")


def workspace_key(root: Path) -> str:
    normalized = os.path.normcase(str(root.resolve()))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{slugify(root.name, 28)}-{digest}"


def default_state_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return codex_home / "gauntlet-loop" / "runs"


def validate_run_id(run_id: str) -> str:
    run_id = run_id.lower()
    if not RUN_ID_RE.fullmatch(run_id):
        raise GauntletError(
            "run id must be 1-96 lowercase letters, digits, dots, underscores, or hyphens "
            "and cannot contain path separators"
        )
    return run_id


def hash_path(path: Path) -> str:
    path = path.resolve()
    if not path.exists():
        raise GauntletError(f"artifact does not exist: {path}")
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    if not path.is_dir():
        raise GauntletError(f"artifact must be a regular file or directory: {path}")
    digest.update(b"directory\0")
    files = sorted((item for item in path.rglob("*") if item.is_file()), key=lambda p: p.as_posix())
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def artifact_record(value: str) -> dict[str, str]:
    path = Path(value).expanduser().resolve()
    return {"path": str(path), "sha256": hash_path(path)}


def source_record(value: str) -> dict[str, str | None]:
    if re.match(r"^https?://", value, flags=re.IGNORECASE):
        return {"locator": value, "kind": "url", "sha256": None}
    candidate = Path(value).expanduser()
    if candidate.exists():
        resolved = candidate.resolve()
        return {"locator": str(resolved), "kind": "path", "sha256": hash_path(resolved)}
    raise GauntletError(f"bar source must be an existing local path or an HTTP(S) URL: {value}")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(encoded, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_jsonl(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GauntletError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GauntletError(f"expected a JSON object in {path}")
    return value


def read_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GauntletError(f"invalid history JSON at {path}:{number}: {exc}") from exc
        if not isinstance(value, dict):
            raise GauntletError(f"history entry at {path}:{number} is not an object")
        records.append(value)
    return records


def resolve_run(value: str) -> tuple[Path, Path, dict[str, Any]]:
    candidate = Path(value).expanduser().resolve()
    state_path = candidate / "state.json" if candidate.is_dir() else candidate
    if state_path.name != "state.json" or not state_path.is_file():
        raise GauntletError(f"run must point to a run directory or state.json: {candidate}")
    state = read_json(state_path)
    if state.get("schema_version") != SCHEMA_VERSION:
        raise GauntletError(f"unsupported ledger schema in {state_path}")
    if state.get("run_id") != state_path.parent.name:
        raise GauntletError(f"run id does not match its directory in {state_path}")
    return state_path.parent, state_path, state


def parse_gate_results(values: Iterable[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise GauntletError(f"gate result must use NAME=pass|fail|na: {value}")
        name, status = value.rsplit("=", 1)
        name = name.strip()
        status = status.strip().lower()
        if not name or status not in GATE_STATUSES:
            raise GauntletError(f"invalid gate result: {value}")
        if name in results:
            raise GauntletError(f"duplicate gate result: {name}")
        results[name] = status
    return results


def md(value: Any, limit: int = 220) -> str:
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_progress(run_dir: Path, state: dict[str, Any]) -> None:
    history = read_history(run_dir / "history.jsonl")
    lines = [
        f"# Gauntlet Loop: {state['run_id']}",
        "",
        f"- Status: **{state['status']}**",
        f"- Updated: {state['updated_at']}",
        f"- Workspace: `{md(state['workspace'], 500)}`",
        f"- Goal: {md(state['goal'], 500)}",
        f"- Bar: {md(state['bar'], 500)}",
        f"- Judge question: {md(state['judge_question'], 500)}",
        "",
        "## Bar sources and provenance",
        "",
    ]
    for source in state.get("bar_sources", []):
        suffix = f" (`sha256:{source['sha256'][:12]}…`)" if source.get("sha256") else ""
        lines.append(f"- {md(source['locator'], 500)}{suffix}")
    if not state.get("bar_sources"):
        lines.append("- None recorded")

    lines.extend(["", "## Deterministic gates", ""])
    if state.get("gates"):
        lines.extend(f"- {md(gate, 500)}" for gate in state["gates"])
    else:
        lines.append("- None declared")

    lines.extend(["", "## Pieces", "", "| Piece | Status |", "|---|---|"])
    for piece in state.get("pieces", []):
        lines.append(f"| {md(piece['name'])} | {md(piece['status'])} |")

    lines.extend(
        [
            "",
            "## Iteration history",
            "",
            "| Round | Scope | Verdict | Confidence | Change | Biggest gap | Next |",
            "|---:|---|---|---|---|---|---|",
        ]
    )
    for record in history:
        lines.append(
            "| {round} | {scope} | {verdict} | {confidence} | {change} | {gap} | {next_step} |".format(
                round=record.get("round", ""),
                scope=md(record.get("scope")),
                verdict=md(record.get("verdict")),
                confidence=md(record.get("confidence")),
                change=md(record.get("change")),
                gap=md(record.get("gap")),
                next_step=md(record.get("next")),
            )
        )

    latest = state.get("latest_round")
    if latest:
        lines.extend(["", "## Latest evidence", ""])
        for item in latest.get("evidence", []):
            lines.append(f"- {md(item, 1000)}")
        lines.extend(["", "### Latest gate results", ""])
        if latest.get("gates"):
            for name, status in latest["gates"].items():
                lines.append(f"- {md(name)}: **{md(status)}**")
        else:
            lines.append("- None recorded")
        lines.extend(["", "### Latest artifacts", ""])
        lines.append(f"- Candidate: `{md(latest['candidate']['path'], 1000)}`")
        lines.append(f"- Candidate SHA-256: `{latest['candidate']['sha256']}`")
        lines.append(f"- Bar capture: `{md(latest['bar_artifact']['path'], 1000)}`")
        lines.append(f"- Bar SHA-256: `{latest['bar_artifact']['sha256']}`")
        for artifact in latest.get("artifacts", []):
            lines.append(f"- Supporting: `{md(artifact, 1000)}`")

    if state.get("finish_reason"):
        lines.extend(["", "## Finish reason", "", md(state["finish_reason"], 2000)])
    lines.extend(["", "_Generated from `state.json` and `history.jsonl`; do not hand-edit this page._", ""])
    (run_dir / "progress.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def latest_artifacts_current(state: dict[str, Any]) -> dict[str, bool | None]:
    latest = state.get("latest_round")
    if not latest:
        return {"candidate": None, "bar": None}
    result: dict[str, bool | None] = {}
    for key, output_key in (("candidate", "candidate"), ("bar_artifact", "bar")):
        record = latest.get(key)
        if not record:
            result[output_key] = None
            continue
        try:
            result[output_key] = hash_path(Path(record["path"])) == record["sha256"]
        except GauntletError:
            result[output_key] = False
    return result


def frozen_sources_current(state: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for source in state.get("bar_sources", []):
        if source.get("kind") != "path":
            continue
        try:
            current = hash_path(Path(source["locator"])) == source.get("sha256")
        except GauntletError:
            current = False
        checks.append({"path": source["locator"], "current": current})
    return checks


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def command_init(args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise GauntletError(f"workspace root is not a directory: {root}")
    goal = args.goal.strip()
    bar = args.bar.strip()
    judge_question = args.judge_question.strip()
    if not goal or not bar or not judge_question:
        raise GauntletError("goal, bar, and judge question must be non-empty")
    state_root = Path(args.state_root).expanduser().resolve() if args.state_root else default_state_root().resolve()
    run_id = validate_run_id(
        args.run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}-{slugify(goal)}"
    )
    run_dir = state_root / workspace_key(root) / run_id
    if run_dir.exists():
        raise GauntletError(f"run already exists: {run_dir}")
    bar_sources = [source_record(value) for value in args.bar_source]
    if not any(source["kind"] == "path" for source in bar_sources):
        raise GauntletError("at least one --bar-source must be an existing local frozen capture; a URL alone is provenance")
    run_dir.mkdir(parents=True)
    now = utc_now()
    gates = list(dict.fromkeys(gate.strip() for gate in args.gate if gate.strip()))
    pieces = list(dict.fromkeys(piece.strip() for piece in args.piece if piece.strip()))
    if not any(piece.casefold() == "overall" for piece in pieces):
        pieces.append("overall")
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workspace": str(root),
        "workspace_key": workspace_key(root),
        "status": "active",
        "goal": goal,
        "bar": bar,
        "bar_sources": bar_sources,
        "judge_question": judge_question,
        "gates": gates,
        "pieces": [{"name": piece, "status": "pending"} for piece in pieces],
        "round_count": 0,
        "latest_round": None,
        "events": [{"at": now, "type": "initialized", "reason": "Run created"}],
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "finish_reason": None,
    }
    atomic_write_json(run_dir / "state.json", state)
    (run_dir / "history.jsonl").touch()
    render_progress(run_dir, state)
    emit(
        {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "state_path": str(run_dir / "state.json"),
            "progress_path": str(run_dir / "progress.md"),
        }
    )


def command_record(args: argparse.Namespace) -> None:
    run_dir, state_path, state = resolve_run(args.run)
    if state.get("status") != "active":
        raise GauntletError(f"cannot record into a {state.get('status')} run; resume it first when applicable")
    gates = parse_gate_results(args.gate)
    declared = set(state.get("gates", []))
    unknown = set(gates) - declared
    if unknown:
        raise GauntletError(f"gate results were not declared at initialization: {sorted(unknown)}")
    missing = declared - set(gates)
    if missing:
        raise GauntletError(f"missing gate results: {sorted(missing)}")
    source_checks = frozen_sources_current(state)
    if any(not check["current"] for check in source_checks):
        raise GauntletError(f"a frozen local bar source changed or disappeared: {source_checks}")
    history = read_history(run_dir / "history.jsonl")
    record = {
        "round": len(history) + 1,
        "at": utc_now(),
        "scope": args.scope.strip(),
        "verdict": args.verdict,
        "confidence": args.confidence,
        "change": args.change.strip(),
        "gap": args.gap.strip(),
        "evidence": [item.strip() for item in args.evidence if item.strip()],
        "next": args.next_step.strip(),
        "gates": gates,
        "candidate": artifact_record(args.candidate),
        "bar_artifact": artifact_record(args.bar_artifact),
        "artifacts": [str(Path(item).expanduser().resolve()) for item in args.artifact],
    }
    if not record["scope"] or not record["change"] or not record["gap"] or not record["evidence"]:
        raise GauntletError("scope, change, gap, and at least one evidence item are required")
    append_jsonl(run_dir / "history.jsonl", record)
    state["round_count"] = record["round"]
    state["latest_round"] = record
    state["updated_at"] = record["at"]
    for piece in state.get("pieces", []):
        if piece["name"].casefold() == record["scope"].casefold():
            piece["status"] = "passed" if (
                record["verdict"] == "ours"
                and record["confidence"] in ("medium", "high")
                and all(status == "pass" for status in gates.values())
            ) else "active"
    atomic_write_json(state_path, state)
    render_progress(run_dir, state)
    emit({"recorded_round": record["round"], "run_dir": str(run_dir), "progress_path": str(run_dir / "progress.md")})


def command_status(args: argparse.Namespace) -> None:
    run_dir, _, state = resolve_run(args.run)
    emit(
        {
            "run_dir": str(run_dir),
            "progress_path": str(run_dir / "progress.md"),
            "latest_artifacts_current": latest_artifacts_current(state),
            "frozen_sources_current": frozen_sources_current(state),
            "state": state,
        }
    )


def command_list(args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser().resolve()
    state_root = Path(args.state_root).expanduser().resolve() if args.state_root else default_state_root().resolve()
    workspace_dir = state_root / workspace_key(root)
    runs: list[dict[str, Any]] = []
    if workspace_dir.is_dir():
        for state_path in sorted(workspace_dir.glob("*/state.json"), reverse=True):
            state = read_json(state_path)
            runs.append(
                {
                    "run_id": state.get("run_id"),
                    "status": state.get("status"),
                    "goal": state.get("goal"),
                    "updated_at": state.get("updated_at"),
                    "run_dir": str(state_path.parent),
                }
            )
    emit({"workspace": str(root), "runs": runs})


def command_finish(args: argparse.Namespace) -> None:
    run_dir, state_path, state = resolve_run(args.run)
    if state.get("status") != "active":
        raise GauntletError(f"run is already {state.get('status')}")
    reason = args.reason.strip()
    if not reason:
        raise GauntletError("an evidence-backed finish reason is required")
    if args.status == "won":
        latest = state.get("latest_round")
        if not latest:
            raise GauntletError("cannot win without a recorded critic round")
        if latest.get("scope", "").casefold() != "overall":
            raise GauntletError("cannot win without a latest scope of overall")
        if latest.get("verdict") != "ours":
            raise GauntletError("cannot win unless the latest overall verdict is ours")
        if latest.get("confidence") not in ("medium", "high"):
            raise GauntletError("cannot win on a low-confidence verdict")
        current = latest_artifacts_current(state)
        if current != {"candidate": True, "bar": True}:
            raise GauntletError(f"judged artifacts changed or disappeared: {current}")
        source_checks = frozen_sources_current(state)
        if any(not check["current"] for check in source_checks):
            raise GauntletError(f"a frozen local bar source changed or disappeared: {source_checks}")
        declared = set(state.get("gates", []))
        gates = latest.get("gates", {})
        if set(gates) != declared or any(gates[name] != "pass" for name in declared):
            raise GauntletError("cannot win until every declared gate is present and passing")
    now = utc_now()
    state["status"] = args.status
    state["updated_at"] = now
    state["finished_at"] = now
    state["finish_reason"] = reason
    state.setdefault("events", []).append({"at": now, "type": args.status, "reason": reason})
    atomic_write_json(state_path, state)
    render_progress(run_dir, state)
    emit({"status": args.status, "run_dir": str(run_dir), "progress_path": str(run_dir / "progress.md")})


def command_resume(args: argparse.Namespace) -> None:
    run_dir, state_path, state = resolve_run(args.run)
    if state.get("status") not in ("stopped", "blocked"):
        raise GauntletError(f"only stopped or blocked runs can resume; current status is {state.get('status')}")
    reason = args.reason.strip()
    if not reason:
        raise GauntletError("a resume reason is required")
    now = utc_now()
    drift = latest_artifacts_current(state)
    source_checks = frozen_sources_current(state)
    state["status"] = "active"
    state["updated_at"] = now
    state["finished_at"] = None
    state["finish_reason"] = None
    state.setdefault("events", []).append(
        {"at": now, "type": "resumed", "reason": reason, "artifact_check": drift, "source_check": source_checks}
    )
    atomic_write_json(state_path, state)
    render_progress(run_dir, state)
    emit({"status": "active", "artifact_check": drift, "source_check": source_checks, "run_dir": str(run_dir)})


def command_self_test(_: argparse.Namespace) -> None:
    script = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(prefix="gauntlet state test ") as temporary:
        base = Path(temporary)
        workspace = base / "workspace with spaces"
        state_root = base / "state"
        workspace.mkdir()
        ours = workspace / "ours.txt"
        bar = workspace / "bar.txt"
        ours.write_text("draft one", encoding="utf-8")
        bar.write_text("reference", encoding="utf-8")
        if hash_path(ours) != hashlib.sha256(ours.read_bytes()).hexdigest():
            raise GauntletError("self-test file digest does not match standard SHA-256")

        def run(*arguments: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [sys.executable, str(script), *arguments],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != expect:
                raise GauntletError(
                    f"self-test command returned {result.returncode}, expected {expect}: {arguments}\n"
                    f"stdout={result.stdout}\nstderr={result.stderr}"
                )
            return result

        invalid = run(
            "init", "--root", str(workspace), "--state-root", str(state_root), "--run-id", "../escape",
            "--goal", "test", "--bar", "bar", "--bar-source", str(bar), "--judge-question", "A or B?", expect=2,
        )
        if "path separators" not in invalid.stderr:
            raise GauntletError("self-test did not reject traversal-like run id")

        missing_source = run(
            "init", "--root", str(workspace), "--state-root", str(state_root), "--run-id", "bad-source",
            "--goal", "test", "--bar", "bar", "--bar-source", str(workspace / "missing.txt"),
            "--judge-question", "A or B?", expect=2,
        )
        if "existing local path" not in missing_source.stderr:
            raise GauntletError("self-test did not reject a non-fetchable bar source")

        url_only = run(
            "init", "--root", str(workspace), "--state-root", str(state_root), "--run-id", "url-only",
            "--goal", "test", "--bar", "bar", "--bar-source", "https://example.com/reference",
            "--judge-question", "A or B?", expect=2,
        )
        if "local frozen capture" not in url_only.stderr:
            raise GauntletError("self-test did not reject a URL-only mutable bar")

        initialized = json.loads(
            run(
                "init", "--root", str(workspace), "--state-root", str(state_root), "--run-id", "test-run",
                "--goal", "test goal", "--bar", "test bar", "--bar-source", str(bar),
                "--bar-source", "https://example.com/reference",
                "--judge-question", "Which is better?", "--gate", "tests", "--piece", "overall",
            ).stdout
        )
        run_dir = initialized["run_dir"]
        run(
            "record", "--run", run_dir, "--scope", "overall", "--verdict", "bar", "--confidence", "high",
            "--change", "baseline", "--gap", "ours is weaker", "--evidence", "direct comparison",
            "--next", "improve", "--gate", "tests=pass", "--candidate", str(ours), "--bar-artifact", str(bar),
        )
        run("finish", "--run", run_dir, "--status", "won", "--reason", "should fail", expect=2)
        run(
            "record", "--run", run_dir, "--scope", "overall", "--verdict", "ours", "--confidence", "high",
            "--change", "revision", "--gap", "none", "--evidence", "direct comparison",
            "--next", "verify", "--gate", "tests=fail", "--candidate", str(ours), "--bar-artifact", str(bar),
        )
        run("finish", "--run", run_dir, "--status", "won", "--reason", "should fail gates", expect=2)
        run(
            "record", "--run", run_dir, "--scope", "overall", "--verdict", "ours", "--confidence", "high",
            "--change", "revision two", "--gap", "none", "--evidence", "direct comparison",
            "--next", "finish", "--gate", "tests=pass", "--candidate", str(ours), "--bar-artifact", str(bar),
        )
        ours.write_text("changed after judgment", encoding="utf-8")
        run("finish", "--run", run_dir, "--status", "won", "--reason", "should fail drift", expect=2)
        run(
            "record", "--run", run_dir, "--scope", "overall", "--verdict", "ours", "--confidence", "high",
            "--change", "reevaluated", "--gap", "none", "--evidence", "fresh direct comparison",
            "--next", "finish", "--gate", "tests=pass", "--candidate", str(ours), "--bar-artifact", str(bar),
        )
        bar.write_text("changed frozen reference", encoding="utf-8")
        run(
            "record", "--run", run_dir, "--scope", "overall", "--verdict", "ours", "--confidence", "high",
            "--change", "should reject bar drift", "--gap", "none", "--evidence", "invalid comparison",
            "--next", "restore", "--gate", "tests=pass", "--candidate", str(ours), "--bar-artifact", str(bar),
            expect=2,
        )
        run("finish", "--run", run_dir, "--status", "won", "--reason", "should fail bar drift", expect=2)
        bar.write_text("reference", encoding="utf-8")
        run("finish", "--run", run_dir, "--status", "won", "--reason", "ours won and tests passed")
        status = json.loads(run("status", "--run", run_dir).stdout)
        if (
            status["state"]["status"] != "won"
            or status["latest_artifacts_current"] != {"candidate": True, "bar": True}
            or any(not check["current"] for check in status["frozen_sources_current"])
        ):
            raise GauntletError("self-test final status was not a valid stable win")

        second = json.loads(
            run(
                "init", "--root", str(workspace), "--state-root", str(state_root), "--run-id", "resume-run",
                "--goal", "resume goal", "--bar", "test bar", "--bar-source", str(bar),
                "--judge-question", "Which is better?",
            ).stdout
        )
        run("finish", "--run", second["run_dir"], "--status", "stopped", "--reason", "user stopped")
        resumed = json.loads(
            run("resume", "--run", second["run_dir"], "--reason", "user requested continuation").stdout
        )
        if resumed["status"] != "active":
            raise GauntletError("self-test failed to resume a stopped run")
    emit({"ok": True, "tests": 13})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persist and validate a Codex Gauntlet Loop run.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create an isolated run ledger.")
    init.add_argument("--root", required=True, help="Workspace root used to isolate runs.")
    init.add_argument("--state-root", help="Override the default CODEX_HOME state root.")
    init.add_argument("--run-id", help="Optional stable run id.")
    init.add_argument("--goal", required=True)
    init.add_argument("--bar", required=True)
    init.add_argument(
        "--bar-source",
        action="append",
        required=True,
        help="Existing local frozen capture, or an HTTP(S) URL recorded only as provenance; repeatable. At least one local capture is required.",
    )
    init.add_argument("--judge-question", required=True)
    init.add_argument("--gate", action="append", default=[], help="Deterministic gate name; repeatable.")
    init.add_argument("--piece", action="append", default=[], help="Judgeable piece name; repeatable.")
    init.set_defaults(handler=command_init)

    record = subparsers.add_parser("record", help="Append one decoded critic round.")
    record.add_argument("--run", required=True)
    record.add_argument("--scope", required=True)
    record.add_argument("--verdict", choices=VERDICTS, required=True, help="Decoded verdict relative to ours.")
    record.add_argument("--confidence", choices=("low", "medium", "high"), required=True)
    record.add_argument("--change", required=True)
    record.add_argument("--gap", required=True)
    record.add_argument("--evidence", action="append", required=True)
    record.add_argument("--next", dest="next_step", default="")
    record.add_argument("--gate", action="append", default=[], help="NAME=pass|fail|na; repeatable.")
    record.add_argument("--candidate", required=True, help="Current local candidate file or directory.")
    record.add_argument("--bar-artifact", required=True, help="Frozen local bar file or directory used by the critic.")
    record.add_argument("--artifact", action="append", default=[], help="Supporting path; repeatable.")
    record.set_defaults(handler=command_record)

    status = subparsers.add_parser("status", help="Read state and verify the latest artifact hashes.")
    status.add_argument("--run", required=True)
    status.set_defaults(handler=command_status)

    list_parser = subparsers.add_parser("list", help="List runs isolated to a workspace.")
    list_parser.add_argument("--root", required=True)
    list_parser.add_argument("--state-root", help="Override the default CODEX_HOME state root.")
    list_parser.set_defaults(handler=command_list)

    finish = subparsers.add_parser("finish", help="Finish a run with enforced win preconditions.")
    finish.add_argument("--run", required=True)
    finish.add_argument("--status", choices=FINISH_STATUSES, required=True)
    finish.add_argument("--reason", required=True)
    finish.set_defaults(handler=command_finish)

    resume = subparsers.add_parser("resume", help="Resume a stopped or blocked run.")
    resume.add_argument("--run", required=True)
    resume.add_argument("--reason", required=True)
    resume.set_defaults(handler=command_resume)

    self_test = subparsers.add_parser("self-test", help="Run isolated end-to-end ledger checks.")
    self_test.set_defaults(handler=command_self_test)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except GauntletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
