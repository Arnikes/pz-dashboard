"""Exercise live verification on the fixed, isolated local B42 acceptance server.

The game server is never stopped or restarted. A temporary panel draft is restored
after verification. Docker labels and the local endpoint are checked first.
"""

import argparse
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:18081"
PROFILE = "pz-acceptance.ini"
SERVER = "pz-console-acceptance-server"
PANEL = "pz-console-acceptance-panel"


def docker(*args):
    result = subprocess.run(
        ["docker", "--context", "desktop-linux", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=40,
        check=True,
    )
    return result.stdout.strip()


def api(path, body=None, expected=200):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as reply:
            status, data = reply.status, json.load(reply)
    except urllib.error.HTTPError as error:
        status, data = error.code, json.load(error)
    if status != expected:
        raise RuntimeError(f"{path}: HTTP {status}; {data.get('error', '')}")
    return data


def state():
    return json.loads(docker("inspect", "--format", "{{json .State}}", SERVER))


def hashes():
    return docker(
        "exec",
        PANEL,
        "sha256sum",
        "/data/Server/" + PROFILE,
        "/data/Server/" + PROFILE.replace(".ini", "_SandboxVars.lua"),
    ).splitlines()


def draft():
    return api("/api/config-draft?file=" + PROFILE)


def guard():
    context = json.loads(docker("context", "inspect", "desktop-linux"))[0]
    if not context["Endpoints"]["docker"]["Host"].startswith("npipe://"):
        raise RuntimeError("Only the local Windows Docker Desktop engine is accepted")
    for name in (SERVER, PANEL):
        labels = json.loads(docker("inspect", "--format", "{{json .Config.Labels}}", name))
        if (
            labels.get("pz-console.acceptance") != "true"
            or labels.get("com.docker.compose.project") != "pz-console-acceptance"
        ):
            raise RuntimeError("Expected the isolated acceptance containers")
    profiles = api("/api/server-configs")
    if (
        api("/api/overview")["container"] != SERVER
        or profiles["activeFile"] != PROFILE
        or not profiles["versionKnown"]
        or not profiles["version"].startswith("42.")
        or not profiles["mountsKnown"]
    ):
        raise RuntimeError("Expected the confirmed, mounted B42 acceptance profile")
    if api("/api/ops")["active"] or not state()["Running"]:
        raise RuntimeError("Wait for a running acceptance server without active operations")
    mods = api("/api/mods?file=" + PROFILE + "&draft=1")
    if "WanderingZombies" not in mods["mods"] or not any(
        option.get("key") == "WanderingZombies.TryStopVirtual" and option.get("type") == "boolean"
        for packet in mods["workshop"]
        for rec in packet["available"]
        if rec.get("modId") == "WanderingZombies"
        for option in rec.get("options", [])
    ):
        raise RuntimeError(
            "Expected the accepted WanderingZombies package and its boolean declaration"
        )
    return profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    profiles = guard()
    original = draft()
    if original["changed"] or original["conflict"]:
        raise RuntimeError("Do not replace an existing draft or unresolved conflict")
    before_state, before_hashes = state(), hashes()
    name = next(field["value"] for field in original["fields"] if field["key"] == "PublicName")
    owned_revision = None
    try:
        pending = api(
            "/api/config-draft",
            {
                "file": PROFILE,
                "draftRevision": original["draftRevision"],
                "ini": {"PublicName": name + " · verification"},
            },
        )
        owned_revision = pending["draftRevision"]
        request = {
            "file": PROFILE,
            "draftRevision": owned_revision,
            "currentRevision": pending["currentRevision"],
        }
        api("/api/config-verify", {**request, "draftRevision": "stale"}, expected=409)
        api("/api/config-verify", {**request, "currentRevision": "stale"}, expected=409)
        verified = api("/api/config-verify", request)
        owned_revision = verified["draftRevision"]
        if (
            not verified["changed"]
            or verified["conflict"]
            or verified["status"] != "draft"
            or verified["state"]["status"] != "applied"
            or verified["state"]["appliedRevision"] != verified["currentRevision"]
            or not verified["state"].get("verifiedAt")
            or not any(
                field["key"] == "PublicName" and field["value"] == name + " · verification"
                for field in verified["fields"]
            )
        ):
            raise RuntimeError(
                "Verification did not preserve the pending draft and confirm the saved revision"
            )
        repeated = api(
            "/api/config-verify",
            {
                **request,
                "draftRevision": owned_revision,
                "currentRevision": verified["currentRevision"],
            },
        )
        owned_revision = repeated["draftRevision"]
        if (
            repeated["state"]["status"] != "applied"
            or not repeated["changed"]
            or repeated["texts"] != verified["texts"]
        ):
            raise RuntimeError("Repeated verification lost confirmation or changed the draft")
        api(
            "/api/action",
            {
                "op": "apply-config",
                "file": PROFILE,
                "draftRevision": owned_revision,
                "restart": False,
            },
            expected=409,
        )
        wrong_type = api(
            "/api/config-draft",
            {
                "file": PROFILE,
                "draftRevision": owned_revision,
                "sandbox": {"WanderingZombies.TryStopVirtual": 0},
            },
        )
        owned_revision = wrong_type["draftRevision"]
        validation = api("/api/config-validate", {"file": PROFILE, "draftRevision": owned_revision})
        if validation["valid"] or not any(
            p.get("key") == "WanderingZombies.TryStopVirtual" for p in validation["errors"]
        ):
            raise RuntimeError("A numeric value was accepted for a Lua boolean setting")
        if hashes() != before_hashes or state()["StartedAt"] != before_state["StartedAt"]:
            raise RuntimeError("Verification changed game files or restarted the server")
    finally:
        if owned_revision:
            current = draft()
            if current["draftRevision"] != owned_revision:
                raise RuntimeError("Another editor changed the draft; automatic cleanup refused")
            api(
                "/api/config-draft",
                {"file": PROFILE, "draftRevision": owned_revision, "discard": True},
            )
    final = draft()
    if final["changed"] or final["conflict"] or hashes() != before_hashes:
        raise RuntimeError("The original clean draft and game files were not preserved")
    report = {
        "server": SERVER,
        "profile": PROFILE,
        "version": profiles["version"],
        "image": docker("inspect", "--format", "{{.Image}}", PANEL),
        "pendingSettingPreserved": True,
        "repeatedVerification": True,
        "staleDraftRejected": True,
        "staleFilesRejected": True,
        "saveWhileRunningRejected": True,
        "wrongLuaTypeRejected": True,
        "gameFilesUnchanged": True,
        "serverStartUnchanged": True,
        "draftCleanAfterProbe": True,
        "status": final["status"],
    }
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
