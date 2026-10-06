# Application screenshots

README images are generated from the real static UI and the real configuration editor
against isolated local fixtures. The fictional server is **Riverside Co-op**;
player names, metrics, history, archives, Workshop IDs, and mod metadata are synthetic.
Workshop IDs 111, 222, and 333 are fixture identifiers, not package recommendations.

Desktop captures use 1440 × 900; the mobile viewport is 390 × 844.
Desktop images capture the full page; mobile captures the visible viewport.
The interface is English, fonts are bundled locally,
the clock starts at 2026-10-06 18:00 UTC, and animations are disabled.
No game server, Docker socket, Steam download, or production credentials are used.

## Regenerate

Set up the development environment from [CONTRIBUTING.md](../../CONTRIBUTING.md).
From the repository root with the environment activated:

PowerShell:

```powershell
$env:PZ_README_SCREENSHOTS = '1'
python -m pytest -q tests/browser/test_readme_screenshots.py
Remove-Item Env:PZ_README_SCREENSHOTS
```

Bash:

```bash
PZ_README_SCREENSHOTS=1 python -m pytest -q tests/browser/test_readme_screenshots.py
```

The opt-in capture overwrites the six PNG files here. The ordinary test suite skips it.
Inspect the resulting images before committing them. Operating-system font rendering
and timezones may produce small visual differences.

| Image | Workspace |
| --- | --- |
| `overview.png` | Server status, metrics, updates, and activity |
| `settings.png` | INI configuration form |
| `mods.png` | Fictional Workshop packages and ModIDs |
| `backups.png` | Schedule, archive list, and journal |
| `players.png` | Fictional online players and activity history |
| `mobile.png` | Overview with mobile navigation |
