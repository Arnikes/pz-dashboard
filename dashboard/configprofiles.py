"""Pure profile revisions, conflict merging and recognized PZ startup changes."""

import difflib
import hashlib
import json
import re

from configformats import (
    edit_ini,
    ini_entries,
    literal_table,
    mask_ini,
    mask_lua,
    preserve_newlines,
)
from errors import EditorError


def revision(texts):
    return hashlib.sha256(
        json.dumps(texts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def profile_diff(before, after, fromfile, tofile):
    """Build reviewable INI/Lua diffs without exposing secrets from either side."""
    return {
        kind: "".join(
            difflib.unified_diff(
                mask(before[kind]).splitlines(keepends=True),
                mask(after[kind]).splitlines(keepends=True),
                fromfile=fromfile,
                tofile=tofile,
            )
        )
        for kind, mask in (("ini", mask_ini), ("sandbox", mask_lua))
    }


def merge_source(base, desired, current):
    """Merge disjoint line edits without evaluating Lua or rewriting secrets."""
    if desired == base or desired == current:
        return current
    if current == base:
        return desired
    lines = base.splitlines(keepends=True)

    def edits(text):
        target = text.splitlines(keepends=True)
        return [
            (i1, i2, target[j1:j2])
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                None, lines, target, autojunk=False
            ).get_opcodes()
            if tag != "equal"
        ]

    working, pending = edits(current), edits(desired)
    combined = working.copy()
    for change in pending:
        if change in working:
            continue
        start, end, _ = change
        for left, right, _ in working:
            # Insertions at a replacement boundary are deliberately ambiguous.
            overlap = (
                left <= start <= right
                if start == end
                else start <= left <= end
                if left == right
                else max(start, left) < min(end, right)
            )
            if overlap:
                raise EditorError(
                    f"Обе версии изменяют строки {start + 1}–{max(start + 1, end)}. Объедините исходник вручную",
                    409,
                )
        combined.append(change)
    for start, end, replacement in sorted(combined, key=lambda edit: edit[:2], reverse=True):
        lines[start:end] = replacement
    return "".join(lines)


def merge_profile(saved, current):
    merged = {}
    for kind in ("ini", "sandbox"):
        try:
            merged[kind] = merge_source(saved["base"][kind], saved["texts"][kind], current[kind])
        except EditorError as error:
            raise EditorError(f"{kind}: {error}", 409) from None
    return merged


def issue_identity(issue):
    return tuple(issue.get(key) for key in ("code", "modId", "dependency", "message"))


def preserve_existing_issues(issues, previous, mod_changes):
    """Existing composition defects must not block unrelated setting edits."""
    known = {issue_identity(issue) for issue in previous}
    return [
        {
            **issue,
            "severity": "warning",
            "existing": True,
        }
        if not mod_changes and issue["severity"] == "error" and issue_identity(issue) in known
        else issue
        for issue in issues
    ]


PZ_RESET_COMMENT = re.compile(
    r"^(# Reset ID determines if the server has undergone a soft-reset\. "
    r"If this number does match the client, the client must create a new character\. "
    r"Used in conjunction with PlayerServerID\. It is strongly advised that you backup "
    r"these IDs somewhere Min: 0 Max: 2147483647 Default: )\d+(?=\r?\nResetID=\d+\r?$)",
    re.M,
)


def startup_profile_matches(expected, actual, allow_runtime_reset=False):
    """Accept only identified PZ startup metadata, never general disk changes.

    CRLF/LF serialization does not change INI settings. B42 also generates
    ResetID after loading a world without z_outfits.bin.
    Callers may accept it only when ResetID was not a requested edit and PZ/RCON
    readiness was checked. Optimistic concurrency stays byte-exact everywhere.
    """
    if expected == actual:
        return True
    expected_ini = expected["ini"].replace("\r\n", "\n")
    actual_ini = actual["ini"].replace("\r\n", "\n")
    if allow_runtime_reset:
        old = ini_entries(expected_ini).get("ResetID", {}).get("value")
        new = ini_entries(actual_ini).get("ResetID", {}).get("value")
        if old and new and new.isdigit() and 0 <= int(new) < 100_000_000:
            actual_ini = edit_ini(actual_ini, {"ResetID": old})
    return expected["sandbox"] == actual["sandbox"] and PZ_RESET_COMMENT.sub(
        r"\g<1><generated>", expected_ini
    ) == PZ_RESET_COMMENT.sub(r"\g<1><generated>", actual_ini)


def adopt_startup_comment(text, actual):
    generated = PZ_RESET_COMMENT.search(actual)
    return PZ_RESET_COMMENT.sub(lambda _: generated[0], text) if generated else text


def adopt_startup_ini(text, expected, actual):
    text = adopt_startup_comment(text, actual)
    before = ini_entries(expected).get("ResetID", {}).get("value")
    pending = ini_entries(text).get("ResetID", {}).get("value")
    current = ini_entries(actual).get("ResetID", {}).get("value")
    text = (
        edit_ini(text, {"ResetID": current}) if before and pending == before and current else text
    )
    return preserve_newlines(actual, text)


def merge_verified_profile(saved, current):
    """Merge pending edits across accepted startup INI newline serialization."""
    # Align endings before the line-based merge, so changing every CRLF to LF
    # cannot turn a disjoint setting edit into an overlapping source conflict.
    aligned = {
        **current,
        "ini": preserve_newlines(saved["base"]["ini"], current["ini"]),
    }
    merged = merge_profile(saved, aligned)
    merged["ini"] = preserve_newlines(current["ini"], merged["ini"])
    return merged


def same_lua_value(left, right):
    # Lua numbers may serialize as 2.0 or 2; true is never the number 1.
    return left == right and (
        type(left) is type(right) or type(left) in (int, float) and type(right) in (int, float)
    )


def startup_sandbox_defaults(expected, actual, discovered, selected):
    """Accept PZ's literal serialization and declared selected-mod defaults only."""
    if expected == actual:
        return {}
    before, after = literal_table(expected), literal_table(actual)
    if not before or not after or not before.values.keys() <= after.values.keys():
        return None

    if any(
        not same_lua_value(rec["value"], after.values[path]["value"])
        for path, rec in before.values.items()
    ):
        return None
    defaults, ambiguous = {}, set()
    for records in discovered.values():
        for rec in records:
            if rec.get("modId") not in selected or rec.get("metadataStale"):
                continue
            for option in rec.get("options", []):
                if "default" not in option:
                    continue
                path = tuple(option["key"].split("."))
                if path in defaults and not same_lua_value(defaults[path], option["default"]):
                    ambiguous.add(path)
                defaults[path] = option["default"]
    added = after.values.keys() - before.values.keys()
    if any(
        path not in defaults
        or path in ambiguous
        or not same_lua_value(after.values[path]["value"], defaults[path])
        for path in added
    ):
        return None
    tables = before.tables.keys() | {path[:i] for path in added for i in range(1, len(path))}
    if after.tables.keys() != tables:
        return None
    return {".".join(path): after.values[path]["value"] for path in added}


def remember_mod_order(memory, selected):
    """Reorder active slots while keeping positions of disabled IDs."""
    memory = list(dict.fromkeys(memory))
    known = set(memory).intersection(selected)
    ordered = iter(mid for mid in selected if mid in known)
    return [next(ordered) if mid in known else mid for mid in memory] + [
        mid for mid in selected if mid not in memory
    ]
