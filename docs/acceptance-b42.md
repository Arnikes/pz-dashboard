# Isolated B42 acceptance testing

Use the separate acceptance stack to exercise real PZ, RCON, Steam, and configuration
files. Isolated backend/browser fixtures do not replace this integration check.
The acceptance stack creates its own project, containers, credentials, and four volumes.

## Recorded validation scope

Previous local acceptance used **B42 42.21.0** and covered server/RCON readiness,
active profile and version detection, configuration application and revision verification,
Workshop preparation/activation, collection metadata resolution, and configuration recovery.
Startup-generated `ResetID` comments and SandboxVars serialization were also examined.
The server returned to its Steam-enabled configuration with a clean draft and
confirmed applied state.

**Gameplay effects of Sandbox settings in existing/new world regions were not verified.**
A connected game-client acceptance pass remains outstanding; readiness and file verification
do not prove in-game effects. This is a record of earlier acceptance, not a claim that the
current `latest` image or Steam branch always reproduces that exact version.
Capture the actual image digest and PZ version for each run.

## Isolation

`docker-compose.acceptance.yml` uses project `pz-console-acceptance`,
containers `pz-console-acceptance-server` and `pz-console-acceptance-panel`,
and separate named volumes. The panel is at **http://127.0.0.1:18081**.
Game ports bind to loopback; RCON stays inside the Compose network.
Restart policies are disabled. No production data or passwords belong in this stack.

The game image and volume paths follow the
[upstream server image](https://github.com/indifferentbroccoli/projectzomboid-server-docker).
`SERVER_BRANCH=unstable` requests B42; verify the actual version after startup.
`UPDATE_ON_START=false` preserves installed files between stages but still permits
the initial installation. The 2 GiB test heap is for a small test world, not a
production sizing recommendation. Increase resources for heavier fixtures.

## Launch

Check `docker context show` before running: use your local test engine.
Generate separate credentials in the ignored temporary directory.

PowerShell, from the repository root after development setup:

```powershell
New-Item -ItemType Directory -Path .tmp-b42-acceptance -Force | Out-Null
if (-not (Test-Path -LiteralPath .tmp-b42-acceptance/secrets.env)) {
    $acceptanceKey = .\.venv\Scripts\python.exe -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
    Set-Content -LiteralPath .tmp-b42-acceptance/secrets.env -Encoding ascii -Value @(
        "ACCEPTANCE_ADMIN_PASSWORD=$([guid]::NewGuid().ToString('N'))"
        "ACCEPTANCE_RCON_PASSWORD=$([guid]::NewGuid().ToString('N'))"
        "ACCEPTANCE_PANEL_PASSWORD=$([guid]::NewGuid().ToString('N'))"
        "ACCEPTANCE_AUTH_KEY=$acceptanceKey"
    )
}
docker compose --env-file .tmp-b42-acceptance/secrets.env -f docker-compose.acceptance.yml up -d --build
```

The first run downloads game files from Steam. Inspect logs, wait for RCON readiness,
then sign in using the generated panel credentials. Preserve the secret file between runs.

## Acceptance scenarios

1. Confirm the actual B42 version, profile, image digest, and writable shared mounts.
2. Change an ordinary INI setting and a Sandbox value. Review the masked diff,
   apply with restart, and confirm readiness and resulting file revisions.
3. Make an external edit to the INI. Confirm that application is blocked, then review
   a non-overlapping rebase. Check that overlapping edits are rejected.
4. Add a compatible Workshop package with several ModIDs. Prepare it through the
   first restart, inspect fresh metadata, select IDs/dependencies, then apply the
   second restart. Other pending settings must remain in the draft during preparation.
5. Exercise missing/incompatible IDs, broken dependencies, and a metadata change
   after validation. Confirm that failure does not falsely report successful application.
6. Verify player-warning behavior, world-backup choices, watchdog grace, and operation errors.
7. Restore configuration history into a draft, review it, then apply separately.
8. Create and verify a world archive. Rehearse restore only on this isolated instance.
9. With a capable game client, compare Sandbox effects in existing and newly generated
   regions and on newly created characters. Record this separately from file validation.

Acceptance helpers in `scripts/` include `verify_running_config.py`,
`verify_workshop_failures.py`, and `verify_linux_transactions.py`.
Read each script's required environment before running it. They are not screenshot tools.

## Stop

```powershell
docker compose --env-file .tmp-b42-acceptance/secrets.env -f docker-compose.acceptance.yml down
```

This preserves the test volumes. Add `--volumes` only when intentionally discarding
the acceptance world and its panel state. Never run that cleanup against a production project.
