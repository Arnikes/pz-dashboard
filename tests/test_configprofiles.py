"""Review diffs must show settings changes without revealing either side's secrets."""

import pytest

from configprofiles import profile_diff


@pytest.mark.parametrize(
    "sandbox, displayed",
    [
        ('SandboxVars = { Token = "old-lua", Count = 1 }\r\n', ("Count = 1", "Count = 2")),
        (
            'SandboxVars = { Token = "old-lua" .. tostring(2), Count = 1 }\r\n',
            ('Count = "__PZ_RAW_LITERAL_', "tostring("),
        ),
    ],
)
def test_profile_diff_masks_both_sides_and_preserves_review_details(sandbox, displayed):
    before = {
        "ini": "# preserved\r\nPassword=old-ini\r\nPublicName=Before\r\n",
        "sandbox": sandbox,
    }
    after = {
        "ini": before["ini"].replace("old-ini", "new-ini").replace("Before", "After"),
        "sandbox": sandbox.replace("old-lua", "new-lua").replace("Count = 1", "Count = 2"),
    }
    diffs = profile_diff(before, after, "Original", "Edited")
    combined = "".join(diffs.values())
    assert all(secret not in combined for secret in ("old-ini", "new-ini", "old-lua", "new-lua"))
    assert "-PublicName=Before\r\n+PublicName=After\r\n" in diffs["ini"]
    assert all(value in diffs["sandbox"] for value in displayed)
    assert all(diff.startswith("--- Original\n+++ Edited\n") for diff in diffs.values())
    assert before["sandbox"] == sandbox


def test_profile_diff_handles_empty_sandbox_and_secret_only_edits():
    before = {"ini": "Password=old\n", "sandbox": ""}
    after = {"ini": "Password=new\n", "sandbox": ""}
    assert profile_diff(before, after, "Before", "After") == {"ini": "", "sandbox": ""}
