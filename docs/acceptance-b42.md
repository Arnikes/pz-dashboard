# Isolated B42 acceptance testing

## Recorded validation scope

An earlier local run on B42 **42.21.0** covered startup and RCON readiness,
profile and version detection, configuration application and recovery, Workshop
preparation and activation, and collection resolution. It also checked
startup-generated `ResetID` and SandboxVars serialization. Gameplay effects of
Sandbox settings still need a connected game-client test. Record the actual
PZ version and image digest for each new run.

## Isolation

`docker-compose.acceptance.yml` uses the project `pz-console-acceptance` with its
own credentials, containers, and four volumes. The panel listens at
`http://127.0.0.1:18081`. Game ports bind to loopback and RCON stays inside the
Compose network. The stack disables automatic container restarts. Use a local
test Docker context and keep production data and credentials outside it.

The supplied game image requests the `unstable` Steam branch.
`UPDATE_ON_START=false` preserves the installed files between runs and allows
the initial download. Verify the version in the startup logs.

## Launch

Complete the [development setup](../CONTRIBUTING.md#setup-and-checks), then
run these commands in PowerShell from the repository root.

```powershell
docker context show
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

Wait for the Steam download and RCON readiness. Sign in as
`acceptance-panel-admin` with `ACCEPTANCE_PANEL_PASSWORD`. Preserve the secret
file between runs.

## Acceptance scenarios

1. Confirm the image digest, B42 version, active profile, and shared writable
   data mount. Change one INI setting and one Sandbox value. Review the masked
   diff, apply with restart, and verify the written revisions and game readiness.
2. Start from the clean draft left by step 1. Change a non-secret INI field in
   the browser and wait for the draft to save. With the editor still open,
   change a different INI field on disk. Confirm that **View changes** shows
   the external edit. Apply the reviewed draft and verify that the file on
   disk contains the draft's values for both fields. Confirm that profile
   history retained the externally edited version present before the write.
   Separately, exercise the [strict API](config-editor.md#editor-api) with an
   external edit. Confirm the conflict, rebase non-overlapping edits, and
   verify that overlapping edits require manual resolution.
3. Add a compatible Workshop package with several ModIDs. Use the first restart
   to download it while retaining pending unrelated edits. Choose IDs and
   dependencies, apply with the second restart, and confirm fresh metadata.
   Check that missing or incompatible IDs, broken dependencies, or metadata
   changed after validation prevent a false success.
4. Check warning countdowns, backup options, watchdog grace, and failure
   reporting. Restore configuration history to a draft and apply it separately.
   Create, verify, and restore a world archive in this isolated instance.
5. With a game client, verify Sandbox effects in existing and new regions
   and on newly created characters. Record these results separately from file
   and startup validation.

### Acceptance helpers

`scripts/verify_running_config.py` targets the named acceptance stack on the
local Windows Docker Desktop `desktop-linux` context. It requires the
WanderingZombies fixture and its expected Sandbox declaration.
`scripts/verify_workshop_failures.py` needs the acceptance server-files volume
mounted read-only at `/server-files` in a panel image container started with
`--network none`. Both scripts check their prerequisites.

Run `scripts/verify_linux_transactions.py` in a disposable panel image
container as UID 1000, with no network or Docker socket and a writable
`/verification` tmpfs. Run these commands in PowerShell.

```powershell
docker build -t pz-console-transaction-check ./dashboard
$transactionScript = (Resolve-Path scripts/verify_linux_transactions.py).Path
docker run --rm --network none --user 1000:1000 --workdir /app --tmpfs /verification:rw,uid=1000,gid=1000,mode=0700 --mount "type=bind,src=$transactionScript,dst=/verify_linux_transactions.py,readonly" --entrypoint python pz-console-transaction-check /verify_linux_transactions.py
```

## Stop

```powershell
docker compose --env-file .tmp-b42-acceptance/secrets.env -f docker-compose.acceptance.yml down
```

This preserves the test volumes. Add `--volumes` only when intentionally
discarding the acceptance world and panel state.
