"""Configuration safety and B42 Workshop behavior without Docker/Steam access."""

import json
import itertools
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import config  # noqa: E402
import configeditor as editor  # noqa: E402
import ops  # noqa: E402
import workshop  # noqa: E402
from configformats import (
    FormatError,
    LuaTable,
    SECRET,
    edit_ini,
    ini_entries,
    mask_ini,
    mask_lua,
    restore_raw_literals,
)  # noqa: E402

INI = "# keep comment\r\nPublicName=Сервер\r\nPassword=topsecret\r\nMods=\\library;\\plugin\r\nWorkshopItems=111\r\nMap=Muldraugh, KY\r\nUnknown=preserve\r\n"
LUA = 'SandboxVars = {\n -- keep\n VERSION = 5,\n Zombies = 4,\n Mod = { Enabled = true, Count = 2, Password = "hidden-token", },\n}\n'


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "Server").mkdir(parents=True)
    (data / "Server" / "world.ini").write_bytes(INI.encode())
    (data / "Server" / "world_SandboxVars.lua").write_text(LUA, encoding="utf-8")
    monkeypatch.setitem(config.CFG, "data_dir", str(data))
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path / "dashboard"))
    ctx = {
        "activeFile": "world.ini",
        "version": "42.15.1",
        "versionKnown": True,
        "owners": {},
        "dockerAvailable": True,
        "generatesSettings": False,
        "mountsKnown": True,
    }
    monkeypatch.setattr(editor, "context", lambda refresh=False: ctx)
    monkeypatch.setattr(ops, "is_running", lambda: False)
    monkeypatch.setattr(ops, "op_busy", lambda: False)
    monkeypatch.setattr(ops, "container_state", lambda: {"startedAt": "start-1", "running": False})
    monkeypatch.setattr(ops, "_ws_titles", lambda ids: {"111": "Package name differs"})
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "_set_phase", Mock())
    monkeypatch.setattr(ops, "run_backup_job", Mock())
    monkeypatch.setattr(workshop, "container_files", lambda items: {})
    monkeypatch.setattr(workshop, "download_manifest", lambda items: None)
    monkeypatch.setattr(workshop, "validate_items", Mock())
    monkeypatch.setattr(workshop, "vanilla_translations", lambda version, allow_container=False: {})
    base = data / "steamapps/workshop/content/108600/111/mods"
    for folder, mid in (("DifferentFolder", "library"), ("PluginFolder", "plugin")):
        (base / folder / "42").mkdir(parents=True)
        (base / folder / "42" / "mod.info").write_text(
            f"id={mid}\nname=Name of {mid}\n" + ("require=\\library\n" if mid == "plugin" else ""),
            encoding="utf-8",
        )
    workshop.invalidate()
    yield data, ctx
    workshop.invalidate()


def change(**kwargs):
    current = editor.draft("world.ini")
    return editor.patch({"file": "world.ini", "draftRevision": current["draftRevision"], **kwargs})


def test_ini_lossless_and_secret_masking():
    result = edit_ini(INI, {"PublicName": "Другой"})
    assert result == INI.replace("Сервер", "Другой")
    masked = mask_ini(INI)
    assert "topsecret" not in masked and SECRET in masked
    assert "Unknown=preserve\r\n" in masked
    assert ini_entries("mods=one\nWORKSHOPITEMS=1\n")["Mods"]["value"] == "one"


def test_ini_continuation_only_replaces_target():
    text = "# comment\r\nMods=one;\r\n  two;\r\n  three\r\nPublicName=hi\r\n"
    assert edit_ini(text, {"Mods": "\\chosen"}) == "# comment\r\nMods=\\chosen\r\nPublicName=hi\r\n"


@pytest.mark.parametrize("text", ["X=1\nX=2\n", "Mods=a\nmods=b\n"])
def test_duplicate_ini_is_rejected(text):
    with pytest.raises(FormatError):
        ini_entries(text)


def test_literal_lua_preserves_comments_tables_and_version():
    table = LuaTable(LUA)
    changed = table.edit({"Mod.Count": 9, "Mod.Added": "Привет", "NewMod.Options.Factor": 1.5})
    parsed = LuaTable(changed)
    assert parsed.values[("Mod", "Count")]["value"] == 9
    assert parsed.values[("NewMod", "Options", "Factor")]["value"] == 1.5
    assert " -- keep" in changed and "VERSION = 5" in changed
    removed = LuaTable(changed).edit({}, ["Mod.Count"])
    assert ("Mod", "Count") not in LuaTable(removed).values
    assert "hidden-token" not in mask_lua(LUA)


@pytest.mark.parametrize(
    "text",
    [
        "SandboxVars = { A = os.execute('bad') }",
        "SandboxVars = { A = 1 + 2 }",
        "SandboxVars = {}; print('bad')",
        "SandboxVars = { A = 1, A = 2 }",
        "SandboxVars={X=1e999}",
    ],
)
def test_lua_never_executes_expressions(text):
    with pytest.raises(FormatError):
        LuaTable(text)


def test_lua_long_strings_decimal_escape_and_add_after_last():
    table = LuaTable('SandboxVars={VERSION=5,Name=[=[line\ntext]=],Other="\\065"}')
    assert table.values[("Other",)]["value"] == "A"
    changed = table.edit({"Control": "\x01", "Added": True})
    assert LuaTable(changed).values[("Control",)]["value"] == "\x01"


def test_draft_is_persistent_and_does_not_write_server(env):
    data, _ = env
    current = change(ini={"PublicName": "Draft name"}, sandbox={"Mod.Count": 10})
    assert current["changed"] is True
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    assert editor.draft("world.ini")["draftRevision"] == current["draftRevision"]
    encoded = json.dumps(current, ensure_ascii=False)
    assert "topsecret" not in encoded and "hidden-token" not in encoded


def test_source_secret_markers_preserve_actual_values(env):
    current = editor.draft("world.ini")
    new = editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "texts": {
                "ini": current["texts"]["ini"].replace("Сервер", "New"),
                "sandbox": current["texts"]["sandbox"],
            },
        }
    )
    saved = editor.load_json(editor.state_dir("world.ini") / "draft.json")
    assert "Password=topsecret" in saved["texts"]["ini"]
    assert "hidden-token" in saved["texts"]["sandbox"]
    assert "topsecret" not in json.dumps(new)


def test_external_and_other_tab_changes_are_conflicts(env):
    data, _ = env
    first = editor.draft("world.ini")
    change(ini={"PublicName": "First"})
    with pytest.raises(editor.EditorError, match="другой вкладкой"):
        editor.patch(
            {
                "file": "world.ini",
                "draftRevision": first["draftRevision"],
                "ini": {"PublicName": "Second"},
            }
        )
    (data / "Server/world.ini").write_bytes(INI.replace("Сервер", "External").encode())
    assert editor.draft("world.ini")["conflict"]
    assert not editor.validate("world.ini")["valid"]
    fresh = change(discard=True)
    assert not fresh["conflict"] and not fresh["changed"]


def test_explicit_rebase_keeps_new_reset_id_secrets_and_pending_settings(env):
    data, _ = env
    path = data / "Server/world.ini"
    original = INI + "ResetID=4742151\r\n"
    path.write_bytes(original.encode())
    current = change(ini={"PublicName": "My draft"})
    external = original.replace("ResetID=4742151", "ResetID=1701740").replace(
        "Password=topsecret", "Password=external-secret"
    )
    path.write_bytes(external.encode())
    result = editor.validate("world.ini")
    assert not result["valid"] and result["rebaseAvailable"]
    assert "external-secret" not in json.dumps(result)
    rebased = editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "currentRevision": result["currentRevision"],
            "rebase": True,
        }
    )
    assert not rebased["conflict"] and rebased["changed"]
    assert "ResetID=1701740\r\n" in rebased["texts"]["ini"]
    assert "PublicName=My draft" in rebased["texts"]["ini"]
    assert path.read_bytes() == external.encode()
    saved = editor.load_json(editor.state_dir("world.ini") / "draft.json")
    assert "Password=external-secret" in saved["texts"]["ini"]
    assert editor.validate("world.ini")["valid"]


def test_conflicted_draft_can_be_edited_without_writing_server_files(env):
    data, _ = env
    current = editor.draft("world.ini")
    path = data / "Server/world.ini"
    external = INI + "ResetID=1701740\r\n"
    path.write_bytes(external.encode())
    result = editor.patch(
        {"file": "world.ini", "draftRevision": current["draftRevision"], "ini": {"PVP": False}}
    )
    assert result["changed"] and result["conflict"]
    assert path.read_bytes() == external.encode()
    assert not editor.validate("world.ini")["valid"]


def test_clean_conflicted_draft_has_explicit_refresh_with_no_file_write(env):
    data, _ = env
    current = editor.draft("world.ini")
    path = data / "Server/world.ini"
    external = INI.replace("Сервер", "Changed on disk")
    path.write_bytes(external.encode())
    result = editor.validate("world.ini")
    assert result["rebaseAvailable"] and not any(result["rebaseDiff"].values())
    refreshed = editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "currentRevision": result["currentRevision"],
            "rebase": True,
        }
    )
    assert not refreshed["conflict"] and not refreshed["changed"]
    assert "PublicName=Changed on disk" in refreshed["texts"]["ini"]
    assert path.read_bytes() == external.encode()


def test_rebase_rejects_overlapping_edits_and_stale_disk_or_draft_revision(env):
    data, _ = env
    current = change(ini={"Password": "draft-secret"})
    path = data / "Server/world.ini"
    path.write_bytes(INI.replace("topsecret", "other-secret").encode())
    result = editor.validate("world.ini")
    assert not result["rebaseAvailable"] and result["rebaseError"]
    assert "other-secret" not in json.dumps(result) and "draft-secret" not in json.dumps(result)
    payload = {
        "file": "world.ini",
        "draftRevision": current["draftRevision"],
        "currentRevision": result["currentRevision"],
        "rebase": True,
    }
    original_draft = (editor.state_dir("world.ini") / "draft.json").read_bytes()
    with pytest.raises(editor.EditorError, match="Обе версии"):
        editor.patch(payload)
    assert (editor.state_dir("world.ini") / "draft.json").read_bytes() == original_draft
    with pytest.raises(editor.EditorError, match="снова изменились"):
        editor.patch({**payload, "currentRevision": "stale"})
    with pytest.raises(editor.EditorError, match="другой вкладкой"):
        editor.patch({**payload, "draftRevision": "stale"})


@pytest.mark.parametrize(
    "base,pending,current,expected",
    [
        ("a\r\nb\r\nc\r\n", "A\r\nb\r\nc\r\n", "a\r\nb\r\nC\r\n", "A\r\nb\r\nC\r\n"),
        ("a\nb\nc\n", "a\nextra\nb\nc\n", "a\nb\nC\n", "a\nextra\nb\nC\n"),
        ("a\nb\nc\n", "a\nc\n", "a\nb\nC\n", "a\nC\n"),
        ("a\nb\nc\n", "A\nb\nc\n", "A\nb\nC\n", "A\nb\nC\n"),
    ],
)
def test_rebase_line_merge_preserves_insertions_deletions_and_newlines(
    base, pending, current, expected
):
    assert editor.merge_source(base, pending, current) == expected


def test_rebase_is_atomic_across_ini_and_sandbox_conflicts(env):
    data, _ = env
    current = change(ini={"PublicName": "Draft"}, sandbox={"Mod.Count": 3})
    (data / "Server/world.ini").write_bytes((INI + "ResetID=1701740\r\n").encode())
    (data / "Server/world_SandboxVars.lua").write_text(
        LUA.replace("Count = 2", "Count = 4"), encoding="utf-8"
    )
    result = editor.validate("world.ini")
    assert not result["rebaseAvailable"] and result["rebaseError"].startswith("sandbox:")
    before = (editor.state_dir("world.ini") / "draft.json").read_bytes()
    with pytest.raises(editor.EditorError, match="sandbox:"):
        editor.patch(
            {
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "currentRevision": result["currentRevision"],
                "rebase": True,
            }
        )
    assert (editor.state_dir("world.ini") / "draft.json").read_bytes() == before


def test_profiles_never_choose_first_when_ambiguous(env):
    data, ctx = env
    (data / "Server/another.ini").write_bytes(INI.encode())
    ctx["activeFile"] = None
    with pytest.raises(editor.EditorError, match="Выберите профиль"):
        editor.draft()
    assert editor.draft("another.ini")["file"] == "another.ini"


@pytest.mark.parametrize(
    "file",
    [
        "../world.ini",
        "..\\world.ini",
        "C:\\world.ini",
        ".ini",
        "world.lua",
        "world.ini/another.ini",
    ],
)
def test_profile_path_boundaries(env, file):
    with pytest.raises(editor.EditorError):
        editor.draft(file)


def test_saving_requires_stop_and_keeps_comments_secrets(env, monkeypatch):
    data, _ = env
    draft = change(ini={"PublicName": "Written"})
    request = {"file": "world.ini", "draftRevision": draft["draftRevision"], "restart": False}
    monkeypatch.setattr(ops, "is_running", lambda: True)
    with pytest.raises(editor.EditorError, match="остановите"):
        editor.queue(request)
    monkeypatch.setattr(ops, "is_running", lambda: False)
    editor.run(request)
    assert (data / "Server/world.ini").read_bytes() == INI.replace("Сервер", "Written").encode()
    assert editor.draft("world.ini")["status"] == "saved"
    assert editor.history("world.ini")["items"]


def test_transaction_rolls_back_both_files_after_partial_failure(env, monkeypatch):
    data, _ = env
    original = editor.read_profile("world.ini")
    real = editor.atomic
    failed = False

    def fail_sandbox(path, value, mode=0o600, owner=None):
        nonlocal failed
        if path == data / "Server/world_SandboxVars.lua" and not failed:
            failed = True
            raise OSError("disk failed")
        return real(path, value, mode, owner=owner)

    monkeypatch.setattr(editor, "atomic", fail_sandbox)
    with pytest.raises(editor.EditorError, match="восстановлена"):
        editor.commit(
            "world.ini",
            {"ini": INI.replace("Сервер", "bad"), "sandbox": LUA.replace("Count = 2", "Count = 9")},
            "test",
        )
    assert editor.read_profile("world.ini") == original
    assert not (editor.state_dir("world.ini") / "transaction.json").exists()


def test_crash_journal_recovers_before_loading_draft(env):
    data, _ = env
    root = editor.state_dir("world.ini")
    backup = root / "history/20260101-120000-12345678"
    backup.mkdir(parents=True)
    (backup / "ini").write_bytes(INI.encode())
    (backup / "sandbox").write_bytes(LUA.encode())
    editor.save_json(
        root / "transaction.json",
        {"historyId": backup.name, "modes": {"ini": 0o600, "sandbox": 0o600}},
    )
    (data / "Server/world.ini").write_text("broken", encoding="utf-8")
    assert editor.draft("world.ini")["texts"]["ini"] == mask_ini(INI)


def test_process_crash_after_first_replace_retains_durable_recovery_marker(env, monkeypatch):
    data, _ = env
    before = editor.read_profile("world.ini")
    root = editor.state_dir("world.ini")
    events = []
    crashed = False

    class Crash(BaseException):
        pass

    def sync(path):
        nonlocal crashed
        events.append(path)
        if path == data / "Server" and not crashed:
            crashed = True
            transaction = editor.load_json(root / "transaction.json")
            backup = root / "history" / transaction["historyId"]
            # Both snapshots and the journal were synced before replacing any PZ file.
            assert root in events and backup in events
            assert (backup / "ini").read_bytes() == INI.encode()
            assert transaction["owners"]["ini"]
            raise Crash()

    monkeypatch.setattr(editor, "sync_directory", sync)
    with pytest.raises(Crash):
        editor.commit(
            "world.ini", {"ini": INI.replace("Сервер", "changed"), "sandbox": LUA}, "Crash"
        )
    assert (root / "transaction.json").exists()
    assert "changed" in editor.read_profile("world.ini")["ini"]
    editor.draft("world.ini")
    assert editor.read_profile("world.ini") == before
    assert not (root / "transaction.json").exists()
    assert events[-1] == root


def test_directory_sync_failure_after_journal_unlink_restores_original_pair(env, monkeypatch):
    before = editor.read_profile("world.ini")
    root = editor.state_dir("world.ini")
    failed = False
    journal_seen = False

    def sync(path):
        nonlocal failed, journal_seen
        if path == root and (root / "transaction.json").exists():
            journal_seen = True
        # Initial applying/history entries are allowed; fail once at commit finalization.
        if (
            path == root
            and not failed
            and journal_seen
            and not (root / "transaction.json").exists()
        ):
            failed = True
            raise OSError("Directory sync failed")

    monkeypatch.setattr(editor, "sync_directory", sync)
    with pytest.raises(editor.EditorError, match="восстановлена"):
        editor.commit(
            "world.ini", {"ini": INI.replace("Сервер", "changed"), "sandbox": LUA}, "Sync failure"
        )
    assert failed and editor.read_profile("world.ini") == before
    assert not (root / "transaction.json").exists()


def test_failed_rollback_never_automatically_starts_partial_configuration(env, monkeypatch):
    data, _ = env
    before = editor.read_profile("world.ini")
    current = change(ini={"PublicName": "New"}, sandbox={"Zombies": 3})
    running = [True]
    failed = False
    monkeypatch.setattr(ops, "is_running", lambda: running[0])
    monkeypatch.setattr(ops, "container_state", lambda: {"running": running[0]})

    def stop():
        running[0] = False
        return "stopped"

    def sync(path):
        nonlocal failed
        if path == data / "Server":
            if editor.read_profile("world.ini")["sandbox"] != before["sandbox"]:
                failed = True
            if failed:
                raise OSError("Persistent disk failure")

    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(editor, "sync_directory", sync)
    start = Mock()
    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    with pytest.raises(OSError, match="Persistent disk failure"):
        editor.run(
            {
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "restart": True,
                "warnSeconds": 0,
            }
        )
    assert (editor.state_dir("world.ini") / "transaction.json").exists()
    assert not running[0]
    start.assert_not_called()
    # Once the disk is available again, the retained marker restores both originals.
    monkeypatch.setattr(editor, "sync_directory", lambda path: None)
    editor.recover("world.ini")
    assert editor.read_profile("world.ini") == before


def test_env_owned_invalid_numeric_and_version_are_blocked(env):
    _, ctx = env
    ctx["owners"]["PublicName"] = "PUBLIC_NAME"
    change(
        ini={"PublicName": "Wrong", "MaxPlayers": 0},
        texts={"sandbox": LUA.replace("VERSION = 5", "VERSION = 9")},
    )
    result = editor.validate("world.ini")
    assert not result["valid"]
    assert {e.get("key") for e in result["errors"]} >= {"VERSION", "PublicName", "MaxPlayers"}
    assert "topsecret" not in json.dumps(result)


def test_b42_index_uses_id_and_effective_version_only():
    files = {
        "111/mods/Folder/mod.info": "id=b41\n",
        "111/mods/Folder/42/mod.info": "id=base\n",
        "111/mods/Folder/42.14/mod.info": "id=chosen\nname=Different\n",
        "111/mods/Folder/42.20/mod.info": "id=future\n",
        "111/mods/Folder/common/media/sandbox-options.txt": "option Mod.Count { type = integer, min = 1, max = 10, default = 2, }",
        "111/mods/Other/42/mod.info": "id=second\n",
        "222/mods/Bad/42/mod.info": "name=NotAnID\nmodID=wrong\n",
    }
    index = workshop.build_index(files, ["111", "222"], "42.15.1")
    assert [r["modId"] for r in index["111"]] == ["chosen", "second"]
    assert index["111"][0]["options"][0]["max"] == 10
    assert index["222"][0]["modId"] == ""


def test_one_item_multiple_ids_no_position_mapping(env):
    state = editor.mod_state("world.ini")
    assert state["paired"] is False
    assert state["workshop"][0]["mods"] == ["library", "plugin"]
    assert state["workshop"][0]["title"] == "Package name differs"
    change(mods={"selected": ["library"]})
    state = editor.mod_response("world.ini", draft_mode=True)
    assert state["mods"] == ["library"] and len(state["workshop"]) == 1
    assert editor.validate("world.ini")["valid"]


def test_dependencies_and_unknown_new_mod_are_blocked(env):
    change(mods={"selected": ["plugin", "not-a-folder-name"]})
    errors = editor.validate("world.ini")["errors"]
    assert {p["code"] for p in errors} >= {"dependency", "unknown"}


def test_unknown_existing_mod_survives_as_warning(env):
    data, _ = env
    (data / "Server/world.ini").write_bytes(INI.replace("\\plugin", "\\unknown").encode())
    editor.draft("world.ini")
    result = editor.validate("world.ini")
    assert result["valid"] and any(p.get("code") == "unknown" for p in result["warnings"])


@pytest.mark.parametrize("defect", ["dependency", "order", "version", "incompatible"])
def test_existing_mod_defects_allow_settings_but_block_composition_edits(env, defect):
    data, _ = env
    info = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/mod.info"
    extra = {
        "dependency": "require=missing\n",
        "order": "loadModBefore=library\n",
        "version": "versionMin=42.99\n",
        "incompatible": "incompatible=library\n",
    }[defect]
    info.write_text("id=plugin\n" + extra, encoding="utf-8")
    current = change(ini={"PublicName": "Changed setting"})
    result = editor.validate("world.ini")
    assert result["valid"] and not result["modChanges"]
    assert any(p.get("code") == defect and p.get("existing") for p in result["warnings"])
    assert info.exists() and current["changed"]
    change(mods={"maps": ["Explicit map change"]})
    result = editor.validate("world.ini")
    assert not result["valid"] and result["modChanges"]
    assert any(p.get("code") == defect for p in result["errors"])


def test_existing_mod_dependency_is_preserved_as_warning_after_restart(env, monkeypatch):
    data, _ = env
    info = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/mod.info"
    info.write_text("id=plugin\nrequire=missing\n", encoding="utf-8")
    current = change(ini={"PublicName": "Setting only"})
    monkeypatch.setattr(editor.dockerlib, "container_start", lambda name: (0, "", ""))
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    editor.run({"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True})
    result = editor.draft("world.ini")
    assert result["status"] == "applied"
    assert any(
        p["code"] == "dependency" and p["severity"] == "warning" and p["existing"]
        for p in result["state"]["verificationProblems"]
    )


def test_dependency_cycle_ambiguity_and_incompatibility():
    def mod(mid, **extra):
        return {
            "modId": mid,
            "require": [],
            "incompatible": [],
            "before": [],
            "after": [],
            "versionMin": "",
            "versionMax": "",
            **extra,
        }

    index = {"1": [mod("a", require=["b"], incompatible=["b"])], "2": [mod("b", require=["a"])]}
    assert {p["code"] for p in workshop.problems(index, ["a", "b"], "42.15")} >= {
        "cycle",
        "incompatible",
    }
    index["2"].append(mod("a"))
    assert any(p["code"] == "ambiguous" for p in workshop.problems(index, ["a"], "42.15"))


def test_empty_scan_is_cached(monkeypatch):
    workshop.invalidate()
    monkeypatch.setattr(workshop, "local_files", lambda items: {})
    lookup = Mock(return_value={})
    monkeypatch.setattr(workshop, "container_files", lookup)
    assert workshop.scan(["123"], "42.15") == {"123": []}
    workshop.scan(["123"], "42.15")
    lookup.assert_called_once()
    workshop.scan(["123"], "42.15", refresh=True)
    assert lookup.call_count == 2


def test_first_restart_downloads_only_items_preserving_unrelated_edits(env, monkeypatch):
    data, _ = env
    draft = change(
        ini={"PublicName": "Future name"}, sandbox={"Mod.Count": 9}, mods={"items": ["111", "222"]}
    )
    running = [True]
    monkeypatch.setattr(ops, "is_running", lambda: running[0])

    def stop():
        running[0] = False
        return "stopped"

    def start(name):
        base = data / "steamapps/workshop/content/108600/222/mods/New/42"
        base.mkdir(parents=True)
        (base / "mod.info").write_text("id=new-id\nname=New\n", encoding="utf-8")
        running[0] = True
        return 0, "", ""

    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    editor.run(
        {"file": "world.ini", "draftRevision": draft["draftRevision"], "warnSeconds": 0},
        prepare=True,
    )
    disk = editor.read_profile("world.ini")
    assert "PublicName=Сервер" in disk["ini"]
    assert "WorkshopItems=111;222" in disk["ini"]
    assert "Mods=\\library;\\plugin" in disk["ini"] and "Count = 2" in disk["sandbox"]
    current = editor.draft("world.ini")
    assert not current["conflict"] and current["changed"]
    assert current["state"]["installation"]["stage"] == "select-mods"
    assert "Future name" in current["texts"]["ini"] and "Count = 9" in current["texts"]["sandbox"]
    ops.run_backup_job.assert_called_once_with("manual", False)


def test_prepare_preview_only_validates_and_shows_stage_one_changes(env):
    current = change(
        ini={"PublicName": "Later name"},
        sandbox={"Zombies": 99},
        mods={"items": ["111", "222"], "selected": ["library", "unknown-new"]},
    )
    assert not editor.validate("world.ini")["valid"]
    preview = editor.validate("world.ini", prepare=True, draft_revision=current["draftRevision"])
    assert preview["valid"] and preview["prepare"] and preview["modChanges"]
    assert "+WorkshopItems=111;222" in preview["diff"]["ini"]
    assert "Later name" not in preview["diff"]["ini"] and not preview["diff"]["sandbox"]
    assert preview["draftRevision"] == current["draftRevision"]


def test_review_rejects_draft_changed_by_another_tab(env):
    old = editor.draft("world.ini")
    change(ini={"PublicName": "Another tab"})
    with pytest.raises(editor.EditorError) as failure:
        editor.validate("world.ini", draft_revision=old["draftRevision"])
    assert failure.value.status == 409


def test_backup_failure_never_writes_mod_changes(env, monkeypatch):
    data, _ = env
    draft = change(mods={"selected": ["library"]})
    monkeypatch.setattr(ops, "run_backup_job", Mock(side_effect=ops.OpsError("backup failed")))
    with pytest.raises(ops.OpsError, match="backup failed"):
        editor.run({"file": "world.ini", "draftRevision": draft["draftRevision"], "restart": False})
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    assert editor.draft("world.ini")["state"]["status"] == "error"


def test_new_sandbox_file_inherits_server_ini_permissions_and_owner(env, monkeypatch):
    data, _ = env
    ini = data / "Server/world.ini"
    ini.chmod(0o640)
    expected = ini.stat()
    (data / "Server/world_SandboxVars.lua").unlink()
    calls = []
    native_chown = getattr(editor.os, "chown", None)

    def chown(path, uid, gid):
        calls.append((Path(path).parent, uid, gid))
        if native_chown:
            native_chown(path, uid, gid)

    monkeypatch.setattr(editor.os, "chown", chown, raising=False)
    current = change(texts={"sandbox": "SandboxVars={VERSION=5,Zombies=4}\n"})
    editor.run({"file": "world.ini", "draftRevision": current["draftRevision"]})
    result = (data / "Server/world_SandboxVars.lua").stat()
    assert result.st_mode & 0o777 == expected.st_mode & 0o777
    assert (data / "Server", expected.st_uid, expected.st_gid) in calls


def test_not_active_cannot_restart(env):
    _, ctx = env
    current = editor.draft("world.ini")
    ctx["activeFile"] = None
    with pytest.raises(editor.EditorError, match="профиль"):
        editor.queue(
            {"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True}
        )


def test_other_profile_can_be_saved_without_active_profile_environment_ownership(env):
    _, ctx = env
    ctx.update(activeFile="other.ini", generatesSettings=True, owners={"PublicName": "PUBLIC_NAME"})
    current = change(ini={"PublicName": "Other profile name"})
    assert not current["canApply"] and current["canWrite"]
    assert next(rec for rec in current["fields"] if rec["key"] == "PublicName")["owner"] is None
    assert editor.validate("world.ini")["valid"]
    editor.run({"file": "world.ini", "draftRevision": current["draftRevision"]})
    assert "PublicName=Other profile name" in editor.read_profile("world.ini")["ini"]


def test_known_mount_mismatch_blocks_operations_before_writing(env):
    _, ctx = env
    current = change(ini={"PublicName": "Draft"})
    ctx.update(mountsKnown=False, dataWritable=False, dataDiagnostic="Wrong data volume")
    assert not editor.draft("world.ini")["canWrite"]
    with pytest.raises(editor.EditorError, match="Wrong data volume"):
        editor.queue({"file": "world.ini", "draftRevision": current["draftRevision"]})
    with pytest.raises(editor.EditorError, match="Wrong data volume"):
        editor.commit("world.ini", editor.read_profile("world.ini"), "Test")
    assert "PublicName=Сервер" in editor.read_profile("world.ini")["ini"]


def test_export_never_contains_secret_options(env):
    pack = editor.modpack({"file": "world.ini"})["pack"]
    assert pack["selected"] == ["library", "plugin"]
    assert "hidden-token" not in json.dumps(pack) and "Mod.Password" not in pack["sandbox"]


@pytest.mark.parametrize(
    "value",
    [
        "http://evil.com/?id=1",
        "https://evil.com/?id=1",
        "https://steamcommunity.com/a?id=1",
        "bad",
        123,
    ],
)
def test_workshop_url_is_restricted(value):
    with pytest.raises(ValueError):
        workshop.resolve(value)


def test_steam_collection_items_are_verified(monkeypatch):
    def steam(method, ids):
        if method == "GetCollectionDetails":
            return {"collectiondetails": [{"result": 1, "children": [{"publishedfileid": "2"}]}]}
        return {
            "publishedfiledetails": [
                {
                    "publishedfileid": ids[0],
                    "result": 1,
                    "consumer_app_id": 108600,
                    "file_type": 2 if ids[0] == "1" else 0,
                    "title": "Title",
                }
            ]
        }

    monkeypatch.setattr(workshop, "steam_call", steam)
    assert workshop.resolve("https://steamcommunity.com/sharedfiles/filedetails/?id=1") == [
        {"workshopId": "2", "title": "Title"}
    ]


def test_legacy_toggle_requires_actual_boolean_and_stop(env, monkeypatch):
    with pytest.raises(editor.EditorError, match="boolean"):
        editor.legacy_toggle({"file": "world.ini", "workshopId": "111", "enable": "false"})
    monkeypatch.setattr(ops, "is_running", lambda: True)
    with pytest.raises(editor.EditorError) as failure:
        editor.legacy_toggle({"file": "world.ini", "workshopId": "111", "enable": False})
    assert failure.value.status == 409


def test_raw_lua_literals_masked_and_restored_without_execution(env, monkeypatch):
    data, _ = env
    text = 'SandboxVars={VERSION=5, Password="computed-secret" .. tostring(2)}\n'
    (data / "Server/world_SandboxVars.lua").write_bytes(text.encode("utf-8"))
    masked = mask_lua(text)
    assert "computed-secret" not in masked and restore_raw_literals(masked, text) == text
    current = editor.draft("world.ini")
    assert current["sandboxDiagnostic"] and not current["sandboxFields"]
    checker = Mock()
    monkeypatch.setattr(editor, "syntax_check", checker)
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "texts": {"sandbox": masked + "-- changed\n"},
        }
    )
    checker.assert_called_once_with(text + "-- changed\n")
    saved = editor.load_json(editor.state_dir("world.ini") / "draft.json")
    assert saved["texts"]["sandbox"] == text + "-- changed\n"


def test_lua_syntax_check_uses_parse_only_and_removes_temporary_file(monkeypatch):
    monkeypatch.setattr(editor.shutil, "which", lambda command: "luac")
    invoked = []

    def check(args, **kwargs):
        invoked.append(args)
        assert args[:2] == ["luac", "-p"]
        assert Path(args[2]).read_text(encoding="utf-8") == "SandboxVars={}"
        return Mock(returncode=0)

    monkeypatch.setattr(editor.subprocess, "run", check)
    editor.syntax_check("SandboxVars={}")
    assert not Path(invoked[0][2]).exists()


def test_readiness_requires_a_pz_players_response_not_just_open_rcon(env, monkeypatch):
    monkeypatch.setattr(ops, "is_running", lambda: True)
    command = Mock(side_effect=["", "Unknown command", "Players connected (0):\n"])
    monkeypatch.setattr(ops, "rcon", command)
    monkeypatch.setattr(editor.time, "monotonic", lambda: next(ticks))
    ticks = itertools.count()
    monkeypatch.setattr(editor.time, "sleep", Mock())
    editor.wait_ready(timeout=10)
    assert command.call_count == 3


def test_readiness_timeout_is_reported_without_restarting_container(env, monkeypatch):
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "rcon", Mock(return_value="Unknown command"))
    ticks = itertools.count()
    monkeypatch.setattr(editor.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(editor.time, "sleep", Mock())
    start = Mock()
    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    with pytest.raises(editor.EditorError, match="PZ/RCON не готов"):
        editor.wait_ready(timeout=2)
    start.assert_not_called()


@pytest.mark.parametrize("version", ["41.78.16", "42.20.1"])
def test_game_version_is_rechecked_after_restart(env, monkeypatch, version):
    data, ctx = env
    current = change(ini={"PublicName": "New name"})
    running = [True]
    monkeypatch.setattr(ops, "is_running", lambda: running[0])

    def stop():
        running[0] = False
        return "stopped"

    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    starts = []

    def start(name):
        starts.append(name)
        running[0] = True
        ctx.update(version=version, versionKnown=version.startswith("42."))
        variant = data / "steamapps/workshop/content/108600/111/mods/DifferentFolder/42.20"
        variant.mkdir()
        (variant / "mod.info").write_text("id=library-updated\n", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    message = "версия B42" if version.startswith("41.") else "Steam-содержимое изменилось"
    with pytest.raises(editor.EditorError, match=message):
        editor.run(
            {
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "restart": True,
                "warnSeconds": 0,
            }
        )
    state = editor.draft("world.ini")["state"]
    assert state["status"] == "error" and not state.get("appliedRevision")
    assert len(starts) == 1 and "New name" in editor.read_profile("world.ini")["ini"]


def test_unknown_docker_never_allows_write_or_recovery(env, monkeypatch):
    data, _ = env
    draft = change(ini={"PublicName": "Draft"})
    monkeypatch.setattr(ops, "container_state", lambda: None)
    with pytest.raises(editor.EditorError, match="Docker"):
        editor.queue({"file": "world.ini", "draftRevision": draft["draftRevision"]})
    with pytest.raises(editor.EditorError, match="Docker"):
        editor.commit("world.ini", editor.read_profile("world.ini"), "Test")
    assert (data / "Server/world.ini").read_bytes() == INI.encode()


def test_mod_id_duplicates_are_checked_after_normalization(env):
    with pytest.raises(editor.EditorError, match="повторы"):
        change(mods={"selected": ["library", "\\library"]})


def test_disabled_id_position_survives_save_and_next_enable(env):
    current = change(mods={"selected": ["plugin"], "preserveOrder": True})
    assert "Mods=\\plugin\r\n" in current["texts"]["ini"]
    restored = change(mods={"selected": ["plugin", "library"], "preserveOrder": True})
    assert "Mods=\\library;\\plugin\r\n" in restored["texts"]["ini"]
    current = change(mods={"selected": ["library"], "preserveOrder": True})
    editor.run({"file": "world.ini", "draftRevision": current["draftRevision"]})
    saved = editor.load_json(editor.state_dir("world.ini") / "draft.json")
    assert saved["modOrder"] == ["library", "plugin"]
    restored = change(mods={"selected": ["library", "plugin"], "preserveOrder": True})
    assert "Mods=\\library;\\plugin\r\n" in restored["texts"]["ini"]


def test_explicit_order_changes_update_remembered_positions(env):
    change(mods={"selected": ["library", "plugin", "third"]})
    change(mods={"selected": ["library", "third"], "preserveOrder": True})
    change(mods={"selected": ["third", "library"]})
    restored = change(mods={"selected": ["third", "library", "plugin"], "preserveOrder": True})
    assert "Mods=\\third;\\plugin;\\library\r\n" in restored["texts"]["ini"]


def test_legacy_disabled_subset_keeps_item_and_original_order(env, monkeypatch):
    current = change(mods={"selected": ["library"]})
    editor.run({"file": "world.ini", "draftRevision": current["draftRevision"]})
    monkeypatch.setattr(ops, "start_op", lambda name, work: work())
    editor.legacy_toggle({"file": "world.ini", "workshopId": "111", "enable": False})
    assert editor.mod_state("world.ini")["mods"] == []
    assert len(editor.mod_state("world.ini")["workshop"]) == 1
    editor.legacy_toggle({"file": "world.ini", "workshopId": "111", "enable": True})
    assert editor.mod_state("world.ini")["mods"] == ["library"]


def test_exact_patch_version_is_used():
    files = {
        f"111/mods/Folder/{branch}/mod.info": f"id={branch}"
        for branch in ("42", "42.15", "42.15.2")
    }
    assert workshop.build_index(files, ["111"], "42.15.1")["111"][0]["modId"] == "42.15"
    assert workshop.version_tuple("42.15") == workshop.version_tuple("42.15.0")
    record = {
        "modId": "a",
        "require": [],
        "incompatible": [],
        "before": [],
        "after": [],
        "versionMin": "",
        "versionMax": "41.78",
    }
    assert any(p["code"] == "version" for p in workshop.problems({"1": [record]}, ["a"], "42.15"))


def test_backup_external_change_is_not_overwritten(env, monkeypatch):
    data, _ = env
    draft = change(mods={"selected": ["library"]})
    path = data / "Server/world.ini"
    monkeypatch.setattr(
        ops,
        "run_backup_job",
        lambda *args: path.write_bytes(INI.replace("Сервер", "External").encode()),
    )
    with pytest.raises(editor.EditorError, match="бэкапа"):
        editor.run({"file": "world.ini", "draftRevision": draft["draftRevision"]})
    assert "External" in path.read_text(encoding="utf-8")
    assert not list((editor.state_dir("world.ini") / "history").glob("*"))


def test_history_restore_rebases_against_external_files(env):
    data, _ = env
    draft = change(ini={"PublicName": "Saved"})
    editor.run({"file": "world.ini", "draftRevision": draft["draftRevision"]})
    history = editor.history("world.ini")["items"][0]
    (data / "Server/world.ini").write_bytes(INI.replace("Сервер", "External").encode())
    current = editor.draft("world.ini")
    restored = editor.restore_history(
        {"file": "world.ini", "draftRevision": current["draftRevision"], "historyId": history["id"]}
    )
    assert not restored["conflict"] and restored["changed"]
    assert "Сервер" in restored["texts"]["ini"]


def test_history_restore_updates_positions_for_subsequent_individual_toggle(env):
    current = change(mods={"selected": ["plugin", "library"]})
    editor.run({"file": "world.ini", "draftRevision": current["draftRevision"]})
    hid = editor.history("world.ini")["items"][0]["id"]
    current = editor.draft("world.ini")
    editor.restore_history(
        {"file": "world.ini", "draftRevision": current["draftRevision"], "historyId": hid}
    )
    change(mods={"selected": ["plugin"], "preserveOrder": True})
    restored = change(mods={"selected": ["plugin", "library"], "preserveOrder": True})
    assert "Mods=\\library;\\plugin\r\n" in restored["texts"]["ini"]


def test_start_overwriting_configuration_is_not_success(env, monkeypatch):
    data, _ = env
    draft = change(ini={"PublicName": "Draft"})

    def start(name):
        (data / "Server/world.ini").write_bytes(INI.replace("Сервер", "Image override").encode())
        return 0, "", ""

    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    with pytest.raises(editor.EditorError, match="при запуске"):
        editor.run({"file": "world.ini", "draftRevision": draft["draftRevision"], "restart": True})
    state = editor.load_json(editor.state_dir("world.ini") / "state.json")
    assert state["status"] == "error" and not state.get("appliedRevision")


RESET_COMMENT = (
    "# Reset ID determines if the server has undergone a soft-reset. "
    "If this number does match the client, the client must create a new character. "
    "Used in conjunction with PlayerServerID. It is strongly advised that you backup "
    "these IDs somewhere Min: 0 Max: 2147483647 Default: 123456\r\nResetID=471224\r\n"
)


@pytest.mark.parametrize("prepare", [False, True])
def test_pz_random_default_comment_is_adopted_after_verified_start(env, monkeypatch, prepare):
    data, _ = env
    path = data / "Server/world.ini"
    path.write_bytes((INI + RESET_COMMENT).encode())
    current = change(ini={"PublicName": "Deferred"}, mods={"items": ["111", "222"]})
    if prepare:
        monkeypatch.setattr(workshop, "scan", lambda *args, **kwargs: {"222": [{"modId": "new"}]})
        monkeypatch.setattr(workshop, "problems", lambda *args: [])

    def start(name):
        path.write_bytes(path.read_bytes().replace(b"Default: 123456", b"Default: 654321"))
        return 0, "", ""

    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    editor.run(
        {"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True},
        prepare=prepare,
    )
    result = editor.draft("world.ini")
    assert not result["conflict"]
    assert result["state"]["savedRevision"] == result["currentRevision"]
    assert result["state"]["appliedRevision"] == result["currentRevision"]
    assert "Default: 654321" in result["texts"]["ini"]
    assert "ResetID=471224" in result["texts"]["ini"]
    assert ("PublicName=Deferred" in path.read_text(encoding="utf-8")) is not prepare
    assert result["changed"] is prepare
    assert "PublicName=Deferred" in result["texts"]["ini"]


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace("ResetID=471224", "ResetID=999"),
        lambda text: text.replace("topsecret", "another-secret"),
        lambda text: text.replace("# keep comment", "# user changed comment"),
        lambda text: text.replace("Default: 123456", "Default: arbitrary"),
    ],
)
def test_startup_comment_exception_does_not_accept_other_changes(change):
    expected = {"ini": INI + RESET_COMMENT, "sandbox": LUA}
    actual = {
        **expected,
        "ini": change(expected["ini"]).replace("Default: 123456", "Default: 987654"),
    }
    assert not editor.startup_profile_matches(expected, actual)
    assert not editor.startup_profile_matches(expected, {**expected, "sandbox": LUA + "-- changed"})


@pytest.mark.parametrize("prepare", [False, True])
def test_startup_mod_defaults_rebase_draft_without_changing_deferred_values(
    env, monkeypatch, prepare
):
    data, _ = env
    path = data / "Server/world_SandboxVars.lua"
    media = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    media.mkdir()
    (media / "sandbox-options.txt").write_text(
        "option Mod.Added { type=boolean, default=true, page=Example, }",
        encoding="utf-8",
    )
    current = change(sandbox={"Zombies": 3, "Mod.Added": False}, mods={"items": ["111", "222"]})
    native_scan = workshop.scan
    if prepare:
        monkeypatch.setattr(
            workshop, "scan", lambda *a, **kw: {**native_scan(*a, **kw), "222": [{"modId": "new"}]}
        )

    def start(name):
        text = path.read_text(encoding="utf-8")
        table = LuaTable(text)
        if ("Mod", "Added") not in table.values:
            text = table.edit({"Mod.Added": True})
        path.write_text(text.replace(" -- keep", " -- generated by PZ"), encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    editor.run(
        {"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True},
        prepare=prepare,
    )
    result = editor.draft("world.ini")
    assert not result["conflict"] and result["changed"] is prepare
    assert result["currentRevision"] == result["state"]["appliedRevision"]
    assert any(p["code"] == "serialization" for p in result["state"]["verificationProblems"])
    pending = LuaTable(result["texts"]["sandbox"])
    physical = LuaTable(path.read_text(encoding="utf-8"))
    assert pending.values[("Zombies",)]["value"] == 3
    assert pending.values[("Mod", "Added")]["value"] is False
    assert physical.values[("Zombies",)]["value"] == (4 if prepare else 3)
    assert physical.values[("Mod", "Added")]["value"] is prepare


@pytest.mark.parametrize(
    "failure",
    [
        "value",
        "secret",
        "version",
        "unknown",
        "wrong-default",
        "unselected",
        "empty-table",
        "stale",
        "boolean-number",
    ],
)
def test_startup_sandbox_serialization_never_accepts_unknown_or_changed_data(failure):
    expected = "SandboxVars={VERSION=5, Zombies=4, Empty={}, Mod={Password='hidden', Enabled=true}}"
    actual = expected.replace("}}", ", Added=true}}")
    discovered = {"111": [{"modId": "plugin", "options": [{"key": "Mod.Added", "default": True}]}]}
    selected = ["plugin"]
    if failure == "value":
        actual = actual.replace("Zombies=4", "Zombies=3")
    elif failure == "secret":
        actual = actual.replace("hidden", "changed")
    elif failure == "version":
        actual = actual.replace("VERSION=5", "VERSION=6")
    elif failure == "unknown":
        actual = actual.replace("Added=true", "Unknown=true")
    elif failure == "wrong-default":
        actual = actual.replace("Added=true", "Added=false")
    elif failure == "unselected":
        selected = []
    elif failure == "empty-table":
        actual = actual.replace("Empty={}, ", "")
    elif failure == "stale":
        discovered["111"][0]["metadataStale"] = True
    else:
        actual = actual.replace("Enabled=true", "Enabled=1")
    assert editor.startup_sandbox_defaults(expected, actual, discovered, selected) is None


@pytest.mark.parametrize("failure", ["dependency", "disappeared"])
def test_workshop_changes_during_start_are_rechecked(env, monkeypatch, failure):
    data, _ = env
    current = change(ini={"PublicName": "Draft"})
    info = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/mod.info"

    def start(name):
        state = editor.load_json(editor.state_dir("world.ini") / "state.json")
        assert state["status"] == "applying"
        if failure == "dependency":
            info.write_text("id=plugin\nrequire=new-required\n", encoding="utf-8")
        else:
            info.unlink()
        return 0, "", ""

    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    with pytest.raises(editor.EditorError, match="Steam-содержимое изменилось"):
        editor.run(
            {"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True}
        )
    after = editor.draft("world.ini")
    assert after["status"] == "error" and not after["conflict"] and not after["changed"]
    assert after["state"]["verificationProblems"] and not after["state"].get("appliedRevision")
    history = after["state"]["historyId"]
    restored = editor.restore_history(
        {"file": "world.ini", "historyId": history, "draftRevision": after["draftRevision"]}
    )
    assert restored["changed"] and "PublicName=Сервер" in restored["texts"]["ini"]


def test_start_failure_keeps_rebased_draft_and_world_backup(env, monkeypatch):
    current = change(mods={"selected": ["library"]})
    monkeypatch.setattr(ops, "run_backup_job", Mock(return_value={"name": "world.tar.gz"}))
    monkeypatch.setattr(editor.dockerlib, "container_start", lambda name: (1, "", "failed"))
    with pytest.raises(editor.EditorError, match="запустить"):
        editor.run(
            {"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True}
        )
    after = editor.draft("world.ini")
    assert after["status"] == "error" and not after["conflict"]
    assert after["state"]["worldBackup"] == "world.tar.gz"
    assert after["state"]["operationCompletedAt"]
    assert after["state"]["historyId"]


def test_preserved_unknown_ids_are_not_falsely_confirmed(env, monkeypatch):
    data, _ = env
    (data / "Server/world.ini").write_bytes(INI.replace("\\plugin", "\\unresolved").encode())
    current = change(ini={"PublicName": "Draft"})
    monkeypatch.setattr(editor.dockerlib, "container_start", lambda name: (0, "", ""))
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    editor.run({"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True})
    after = editor.draft("world.ini")
    assert after["status"] == "unconfirmed" and not after["state"].get("appliedRevision")
    assert after["state"]["verificationProblems"][0]["code"] == "unknown"


def test_updated_sandbox_constraints_are_checked_after_start(env, monkeypatch):
    data, _ = env
    path = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    path.mkdir()
    option = path / "sandbox-options.txt"
    option.write_text(
        "option Mod.Count { type=integer, min=1, max=10, default=2, }", encoding="utf-8"
    )
    current = change(ini={"PublicName": "Draft"})

    def start(name):
        option.write_text(
            "option Mod.Count { type=integer, min=1, max=1, default=1, }", encoding="utf-8"
        )
        return 0, "", ""

    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    with pytest.raises(editor.EditorError, match="Steam-содержимое изменилось"):
        editor.run(
            {"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True}
        )
    after = editor.draft("world.ini")
    assert after["state"]["verificationProblems"][-1]["key"] == "Mod.Count"
    assert not after["state"].get("appliedRevision") and not after["conflict"]


def test_download_stage_verifies_preserved_active_ids_in_union_of_items(env, monkeypatch):
    data, _ = env
    current = change(ini={"PublicName": "Later name"}, mods={"items": ["222"], "selected": []})

    def start(name):
        path = data / "steamapps/workshop/content/108600/222/mods/Duplicate/42"
        path.mkdir(parents=True)
        (path / "mod.info").write_text("id=plugin\nname=Different provider\n", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(editor.dockerlib, "container_start", start)
    monkeypatch.setattr(editor, "wait_ready", lambda: None)
    with pytest.raises(editor.EditorError, match="несколько поставщиков"):
        editor.run({"file": "world.ini", "draftRevision": current["draftRevision"]}, prepare=True)
    after = editor.draft("world.ini")
    assert after["state"]["installation"]["stage"] == "error"
    assert after["changed"] and not after["conflict"]
    assert "Later name" in after["texts"]["ini"]
    assert "PublicName=Сервер" in editor.read_profile("world.ini")["ini"]


def test_failed_prewrite_operation_status_is_visible_with_draft(env, monkeypatch):
    current = change(mods={"selected": ["library"]})
    monkeypatch.setattr(ops, "run_backup_job", Mock(side_effect=ops.OpsError("Backup failed")))
    with pytest.raises(ops.OpsError):
        editor.run({"file": "world.ini", "draftRevision": current["draftRevision"]})
    after = editor.draft("world.ini")
    assert after["changed"] and after["status"] == "error"
    assert after["state"]["error"] == "Backup failed"


def test_modpack_contains_only_mod_settings_and_never_overwrites_world(env):
    pack = editor.modpack({"file": "world.ini"})["pack"]
    assert "Zombies" not in pack["sandbox"] and pack["sandbox"]["Mod.Count"] == 2
    pack["sandbox"]["Zombies"] = 1
    current = editor.draft("world.ini")
    with pytest.raises(editor.EditorError, match="штатные параметры мира"):
        editor.modpack(
            {"file": "world.ini", "draftRevision": current["draftRevision"], "pack": pack}
        )


def test_steam_manifest_and_corrupt_option_are_data_only():
    parsed = workshop.manifest_items(
        '"AppWorkshop" { "appid" "108600" "WorkshopItemsInstalled" { "111" { "manifest" "99" } } }'
    )
    assert parsed["111"]["manifest"] == "99"
    assert (
        workshop.options("option Mod.Choice { type=enum, numValues=bad, }", {})[0]["choices"] == []
    )


def test_legacy_toggle_uses_shared_operation_slot(env, monkeypatch):
    start = Mock()
    monkeypatch.setattr(ops, "start_op", start)
    result = editor.legacy_toggle({"file": "world.ini", "workshopId": "111", "enable": False})
    assert result["operation"] == "apply-config"
    ops.run_backup_job.assert_not_called()
    start.call_args.args[1]()
    ops.run_backup_job.assert_called_once()
    assert "WorkshopItems=111\r\n" in editor.read_profile("world.ini")["ini"]


def test_modpack_import_checks_packages_in_one_batch(env):
    draft = editor.draft("world.ini")
    pack = editor.modpack({"file": "world.ini"})["pack"]
    result = editor.modpack(
        {"file": "world.ini", "draftRevision": draft["draftRevision"], "pack": pack}
    )
    assert result["file"] == "world.ini"
    workshop.validate_items.assert_called_once_with(["111"])


def test_live_transaction_cannot_be_recovered_by_background_read(env, monkeypatch):
    root = editor.state_dir("world.ini")
    editor.save_json(root / "transaction.json", {"historyId": "unfinished", "modes": {}})
    monkeypatch.setattr(ops, "op_busy", lambda: True)
    with pytest.raises(editor.EditorError, match="Идёт запись"):
        editor.draft("world.ini")
    assert (root / "transaction.json").exists()


def test_unknown_version_blocks_changed_configuration(env):
    _, ctx = env
    change(ini={"PublicName": "Changed"})
    ctx.update(version="", versionKnown=False)
    assert any(
        "Версия B42 неизвестна" in e["message"] for e in editor.validate("world.ini")["errors"]
    )
