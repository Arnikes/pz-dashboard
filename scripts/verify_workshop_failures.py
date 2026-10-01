"""Verify offline Steam and corrupted copies of actual B42 Workshop metadata.

Run in an isolated, --network none panel-image container with the acceptance
server-files volume mounted read-only at /server-files. No files are written.
"""

import json
from pathlib import Path
import sys
import urllib.error

sys.path.insert(0, str(Path.cwd()))

import workshop  # noqa: E402

ITEM = "2983905789"
VERSION = "42.21.0"
MOD_ID = "WanderingZombies"


def main():
    try:
        workshop.resolve(ITEM)
    except urllib.error.URLError as error:
        offline_error = type(error).__name__
    else:
        raise SystemExit(
            "Expected unavailable Steam; run in the documented --network none container"
        )
    original = workshop.local_files([ITEM])
    index = workshop.build_index(original, [ITEM], VERSION)
    record = next((r for r in index[ITEM] if r.get("modId") == MOD_ID), None)
    if not record or record.get("branch") != "42.18":
        raise SystemExit(
            "Expected the acceptance package downloaded from Steam; metadata/version changed"
        )
    corrupt = original.copy()
    corrupt[record["path"]] = "name=Damaged metadata without id\n"
    damaged = workshop.build_index(corrupt, [ITEM], VERSION)
    available = {r.get("modId") for r in damaged[ITEM]}
    assert MOD_ID not in available and "wandering-zombies" not in available
    issues = workshop.problems(damaged, [MOD_ID], VERSION)
    assert any(p["code"] == "metadata" for p in issues)
    assert any(p["code"] == "unknown" and p["modId"] == MOD_ID for p in issues)
    assert workshop.local_files([ITEM]) == original, "Read-only source metadata changed"
    restored = workshop.build_index(original, [ITEM], VERSION)
    assert MOD_ID in {r.get("modId") for r in restored[ITEM]}
    print(
        json.dumps(
            {
                "steamOffline": {"error": offline_error, "resolverReturnedCandidates": False},
                "corruptMetadata": {
                    "workshopId": ITEM,
                    "modId": MOD_ID,
                    "reportedProblems": sorted({p["code"] for p in issues}),
                    "folderNotUsedAsId": True,
                },
                "recovery": {"originalMetadataUntouched": True, "providerRestored": True},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
