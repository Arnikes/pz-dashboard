"""Pending drafts survive verified game serialization without rewriting game files."""

import json

import pytest

from test_configeditor import (
    INI,
    LUA,
    RESET_COMMENT,
    change,
    editor,
    env as env,
    saved_verification,
    workshop,
)
from configformats import LuaTable, literal_table
from configprofiles import merge_verified_sandbox


def verify_saved(env, monkeypatch, automatic, desired, serialized):
    data, request = saved_verification(env, monkeypatch)
    pending = change(texts={"sandbox": desired})
    (data / "Server/world_SandboxVars.lua").write_bytes(serialized.encode())
    before = editor.read_profile("world.ini")
    request.update(draftRevision=pending["draftRevision"], currentRevision=editor.revision(before))
    if automatic:
        editor.auto_verify_running()
        result = editor.draft("world.ini")
    else:
        result = editor.verify_running(request)
    assert result["state"]["status"] == "applied"
    assert result["changed"] and not result["conflict"]
    assert result["state"]["savedRevision"] == result["currentRevision"]
    assert editor.read_profile("world.ini") == before
    assert "topsecret" not in json.dumps(result)
    assert "hidden-token" not in json.dumps(result)
    return result


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("serialization", ["numeric", "formatting"])
@pytest.mark.parametrize("edit", ["value", "addition", "removal", "comment"])
def test_pending_sandbox_survives_verified_serialization(
    env, monkeypatch, automatic, serialization, edit
):
    desired = {
        "value": LUA.replace("Zombies = 4", "Zombies = 3"),
        "addition": LuaTable(LUA).edit({"Mod.Pending": 7}),
        "removal": LuaTable(LUA).edit({}, ["Mod.Count"]),
        "comment": LUA.replace("-- keep", "-- pending source comment"),
    }[edit]
    serialized = LUA.replace("Zombies = 4", "Zombies = 4.0")
    if serialization == "formatting":
        serialized = (
            'SandboxVars={VERSION=5,Mod={Password="hidden-token",Count=2,Enabled=true},'
            "Zombies=4.0}\n"
        )
    result = verify_saved(env, monkeypatch, automatic, desired, serialized)
    table = LuaTable(result["texts"]["sandbox"])
    if edit == "value":
        assert table.values[("Zombies",)]["value"] == 3
    elif edit == "addition":
        assert table.values[("Mod", "Pending")]["value"] == 7
    elif edit == "removal":
        assert ("Mod", "Count") not in table.values
    else:
        assert "-- pending source comment" in result["texts"]["sandbox"]


@pytest.mark.parametrize("automatic", [False, True])
def test_verified_defaults_join_pending_source_without_losing_comments(env, monkeypatch, automatic):
    data, _ = env
    media = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    media.mkdir()
    (media / "sandbox-options.txt").write_text(
        "option Mod.Generated { type=integer, default=8, }", encoding="utf-8"
    )
    desired = LuaTable(LUA.replace("-- keep", "-- pending source comment")).edit(
        {"Zombies": 3}, ["Mod.Count"]
    )
    serialized = LuaTable(LUA).edit({"Mod.Generated": 8}).replace("Zombies = 4", "Zombies = 4.0")
    result = verify_saved(env, monkeypatch, automatic, desired, serialized)
    table = LuaTable(result["texts"]["sandbox"])
    assert table.values[("Mod", "Generated")]["value"] == 8
    assert table.values[("Zombies",)]["value"] == 3
    assert ("Mod", "Count") not in table.values
    assert "-- pending source comment" in result["texts"]["sandbox"]


@pytest.mark.parametrize(
    "desired,expected",
    [
        ("SandboxVars={VERSION=5,Zombies=3,Mod={}}", {("Mod", "Generated"): 8}),
        ("SandboxVars={VERSION=5,Zombies=3}", {}),
        ("SandboxVars={VERSION=5,Zombies=3,Mod=false}", {}),
        ("SandboxVars={VERSION=5,Zombies=3,Mod={Generated=9}}", {("Mod", "Generated"): 9}),
        ("SandboxVars={VERSION=5,Zombies=3,Mod={Generated={}}}", {}),
    ],
)
def test_verified_defaults_respect_explicit_pending_table_shape(desired, expected):
    base = "SandboxVars={VERSION=5,Zombies=4,Mod={Count=2}}"
    current = "SandboxVars = {VERSION=5,Zombies=4.0,Mod={Count=2,Generated=8}}"
    merged = LuaTable(merge_verified_sandbox(base, desired, current))
    assert {
        path: rec["value"] for path, rec in merged.values.items() if path == ("Mod", "Generated")
    } == expected
    assert ("Mod", "Count") not in merged.values
    if desired.endswith("Mod=false}"):
        assert merged.values[("Mod",)]["value"] is False
    if "Generated={}" in desired:
        assert ("Mod", "Generated") in merged.tables
    if desired.endswith("Zombies=3}"):
        assert ("Mod",) not in merged.tables


def test_expression_source_keeps_conservative_conflict():
    desired = LUA.replace("Zombies = 4", "Zombies = 3 + 1")
    with pytest.raises(editor.EditorError, match="Обе версии"):
        merge_verified_sandbox(LUA, desired, LUA.replace("Zombies = 4", "Zombies = 4.0"))
    assert merge_verified_sandbox(LUA, desired, LUA) == desired
    assert literal_table(desired) is None


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("pending", ["reset", "comment", "other"])
def test_pending_ini_edits_survive_accepted_generated_reset(env, monkeypatch, automatic, pending):
    data, request = saved_verification(env, monkeypatch)
    desired = INI + RESET_COMMENT
    if pending == "reset":
        desired = desired.replace("ResetID=471224", "ResetID=999")
    elif pending == "comment":
        desired = desired.replace("Default: 123456", "Default: 777")
    else:
        desired = desired.replace("PublicName=Сервер", "PublicName=Pending")
    changed = change(texts={"ini": desired})
    before = editor.read_profile("world.ini")
    request.update(draftRevision=changed["draftRevision"], currentRevision=editor.revision(before))
    if automatic:
        editor.auto_verify_running()
        result = editor.draft("world.ini")
    else:
        result = editor.verify_running(request)
    assert result["state"]["status"] == "applied"
    assert result["changed"] and not result["conflict"]
    assert editor.read_profile("world.ini") == before
    if pending == "reset":
        assert "ResetID=999" in result["texts"]["ini"]
    else:
        assert "ResetID=2748676" in result["texts"]["ini"]
    assert ("Default: 777" if pending == "comment" else "Default: 654321") in result["texts"]["ini"]
    if pending == "other":
        assert "PublicName=Pending" in result["texts"]["ini"]


def test_startup_merge_does_not_relax_general_lua_value_conflicts():
    base = "SandboxVars={VERSION=5,Zombies=4}"
    with pytest.raises(editor.EditorError, match="Обе версии"):
        merge_verified_sandbox(base, base.replace("4", "3"), base.replace("4", "5"))


@pytest.mark.parametrize("layout", ["compact", "multiline"])
@pytest.mark.parametrize("shape", ["removed", "scalar", "default-table", "override"])
def test_preparation_defaults_preserve_pending_table_shape(env, monkeypatch, shape, layout):
    data, _ = env
    path = data / "Server/world_SandboxVars.lua"
    media = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    media.mkdir()
    (media / "sandbox-options.txt").write_text(
        "option Mod.Generated { type=integer, default=8, }", encoding="utf-8"
    )
    mod_line = next(line for line in LUA.splitlines(True) if " Mod = {" in line)
    mod_block = (
        mod_line
        if layout == "compact"
        else ' Mod = {\n  Enabled = true,\n  Count = 2,\n  Password = "hidden-token",\n },\n'
    )
    original = LUA.replace(mod_line, mod_block)
    path.write_bytes(original.encode())
    replacement = {
        "removed": "",
        "scalar": " Mod = false,\n",
        "default-table": mod_line.replace(" },", " Generated = {}, },"),
        "override": mod_line.replace(" },", " Generated = 9, },"),
    }[shape]
    desired = original.replace(mod_block, replacement).replace("Zombies = 4", "Zombies = 3")
    pending = change(texts={"sandbox": desired}, mods={"items": ["111", "222"]})
    native_scan = workshop.scan
    monkeypatch.setattr(
        workshop,
        "scan",
        lambda *args, **kwargs: {**native_scan(*args, **kwargs), "222": [{"modId": "new"}]},
    )

    def start(name):
        path.write_bytes(
            LuaTable(path.read_text(encoding="utf-8")).edit({"Mod.Generated": 8}).encode()
        )
        return 0, "", ""

    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    editor.run(
        {"file": "world.ini", "draftRevision": pending["draftRevision"], "restart": True},
        prepare=True,
    )
    result = editor.draft("world.ini")
    assert result["state"]["status"] == "select-mods"
    assert result["changed"] and not result["conflict"]
    physical = LuaTable(path.read_text(encoding="utf-8"))
    draft = LuaTable(result["texts"]["sandbox"])
    assert physical.values[("Mod", "Generated")]["value"] == 8
    assert physical.values[("Zombies",)]["value"] == 4
    assert draft.values[("Zombies",)]["value"] == 3
    if shape == "override":
        assert draft.values[("Mod", "Generated")]["value"] == 9
    else:
        assert ("Mod", "Generated") not in draft.values
    if shape == "removed":
        assert ("Mod",) not in draft.tables
    elif shape == "scalar":
        assert draft.values[("Mod",)]["value"] is False
    elif shape == "default-table":
        assert ("Mod", "Generated") in draft.tables
