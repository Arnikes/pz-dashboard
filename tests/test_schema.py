"""Schemas follow installed profile metadata, not guessed bounds from another build."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_configeditor import env as profile_env, editor, change, LUA  # noqa: F401
import config
import configschema
import workshop
from configformats import (
    FormatError,
    LuaTable,
    mask_lua,
    version_marker,
    mask_ini,
    restore_ini_secrets,
    edit_ini,
    preserve_newlines,
    restore_raw_literals,
)

pytestmark = pytest.mark.usefixtures("profile_env")
NATIVE_READER = workshop.vanilla_translations


def test_generated_profile_schema_is_versioned_and_attached_to_correct_field():
    ini = "# Min: 1 Max: 200\r\nMaxPlayers=120\r\n# Keep secret out\r\nPassword=secret\r\n"
    sandbox = """SandboxVars = {
 VERSION = 6,
 -- Min: -1.00 Max: 50.25 Default: 0.00
 ClayLakeChance = 2.5,
 ZombieLore = {
  -- 1 = Sprinters
  -- 2 = Fast shamblers
  -- 5 = A build-specific choice
  Speed = 5,
 },
 -- Min: 0 Max: 9
 Mod = { First = 4, Second = 200 },
}
"""
    schema = configschema.catalog(ini, sandbox, "42.20.4")
    assert schema["ini"]["MaxPlayers"] == {"min": 1, "max": 200, "type": "integer"}
    assert "Password" not in schema["ini"]
    assert schema["sandbox"]["ClayLakeChance"]["max"] == 50.25
    speed = configschema.field("ZombieLore.Speed", 5, sandbox=True, schema=schema)
    assert speed["type"] == "enum" and speed["choices"][-1]["value"] == 5
    assert speed["schemaVersion"] == "42.20.4"
    assert not schema["sandbox"]["Mod.Second"]
    assert configschema.catalog(ini, sandbox, "42.20.5")["id"] != schema["id"]
    assert not configschema.catalog(ini, sandbox, "41.78")["sandbox"]


def test_stock_boolean_and_nested_fields_are_not_orphaned_mod_options():
    assert configschema.field("AnimalMetaPredator", True, sandbox=True)["stock"]
    assert configschema.field("ZombieConfig.RallyGroupSize", 20, sandbox=True)["stock"]
    assert (
        configschema.field("Map.AllowWorldMap", True, sandbox=True)["group"] == "Карта и интерфейс"
    )
    assert not configschema.field("AbsentMod.Setting", 4, sandbox=True).get("stock")
    assert "вне каталога" not in configschema.field("StartYear", 1, sandbox=True)["hint"]


@pytest.mark.parametrize(
    "key,kind",
    [
        ("StartYear", "world"),
        ("StartMonth", "world"),
        ("StartDay", "world"),
        ("StartTime", "world"),
        ("StarterKit", "character"),
        ("CarSpawnRate", "areas"),
        ("InitialGas", "vehicles"),
        ("ChanceHasGas", "vehicles"),
    ],
)
def test_applicability_retains_installed_description_and_does_not_change_validation(key, kind):
    original_hint = "Описание установленной версии игры"
    field = configschema.field(
        key,
        True if key == "StarterKit" else 2,
        sandbox=True,
        translations={
            f"Sandbox_{key}": "Переведённое имя",
            f"Sandbox_{key}_tooltip": original_hint,
        },
    )
    assert field["applicationScope"]["kind"] == kind
    assert field["hint"] == original_hint and field["label"] == "Переведённое имя"
    if key == "StarterKit":
        assert field["type"] == "boolean" and not field.get("newWorld")
    field["applicationScope"]["hint"] = "Changed by a caller"
    assert (
        configschema.field(key, 2, sandbox=True)["applicationScope"]["hint"]
        != "Changed by a caller"
    )


def test_applicability_does_not_guess_scope_of_runtime_or_mod_settings():
    for key in (
        "DayLength",
        "LootRespawn",
        "CarGasConsumption",
        "Mod.StartYear",
        "UnknownCarOption",
    ):
        assert "applicationScope" not in configschema.field(key, 2, sandbox=True)
    custom = configschema.field("StarterKit", True, sandbox=True, custom={"label": "Custom"})
    assert "applicationScope" not in custom
    assert "applicationScope" not in configschema.field("StartYear", "1993")


def test_stock_strong_enum_serializes_numeric_index_not_text():
    # StrongEnumSandboxOption extends EnumSandboxOption / IntegerConfigOption.
    for key in ("InjurySeverity", "DamageToPlayerFromHitByACar"):
        field = configschema.field(key, 2, sandbox=True)
        assert field["stock"] and field["type"] == "enum"
        errors = []
        editor.check_field(field, 2, errors)
        assert not errors
        editor.check_field(field, "Minor", errors)
        assert errors
        text = LuaTable("SandboxVars={" + key + "=1}").edit({key: 2})
        assert LuaTable(text).values[(key,)]["value"] == 2


def test_draft_cannot_change_comments_to_bypass_original_schema():
    path = Path(config.CFG["data_dir"]) / "Server/world_SandboxVars.lua"
    text = "SandboxVars={\n VERSION=5,\n -- Min: 0 Max: 10 Default: 1\n ClayLakeChance=1.0,\n}\n"
    path.write_text(text, encoding="utf-8")
    original = editor.draft("world.ini")
    updated = text.replace("Max: 10", "Max: 100").replace("Chance=1.0", "Chance=20.0")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": original["draftRevision"],
            "texts": {"sandbox": updated},
        }
    )
    result = editor.validate("world.ini")
    assert not result["valid"] and any(e.get("key") == "ClayLakeChance" for e in result["errors"])


def test_original_out_of_catalog_value_is_preserved_with_warning():
    path = Path(config.CFG["data_dir"]) / "Server/world_SandboxVars.lua"
    path.write_text(LUA.replace("Zombies = 4", "Zombies = 99"), encoding="utf-8")
    change(ini={"PublicName": "A different field"})
    result = editor.validate("world.ini")
    assert result["valid"] and any(e.get("key") == "Zombies" for e in result["warnings"])


def test_fixing_unsupported_lua_to_literal_table_needs_no_compiler(monkeypatch):
    path = Path(config.CFG["data_dir"]) / "Server/world_SandboxVars.lua"
    path.write_text("SandboxVars={VERSION=5, Zombies=2+2}", encoding="utf-8")
    current = editor.draft("world.ini")
    checker = Mock(side_effect=AssertionError("Literal data should not require a compiler"))
    monkeypatch.setattr(editor, "syntax_check", checker)
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "texts": {"sandbox": "SandboxVars={VERSION=5, Zombies=4}"},
        }
    )
    assert editor.validate("world.ini")["valid"]
    assert not editor.draft("world.ini")["sandboxDiagnostic"]
    checker.assert_not_called()


def test_installed_vanilla_translations_are_version_scoped_and_cached_offline():
    path = Path(config.CFG["data_dir"]) / "media/lua/shared/Translate/RU/Sandbox.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"Sandbox": {"AnimalMetaPredator": "Хищники", "AnimalMetaPredator_tooltip": "Описание"}}
        ),
        encoding="utf-8",
    )
    translated = NATIVE_READER("42.20.4")
    field = configschema.field("AnimalMetaPredator", True, sandbox=True, translations=translated)
    assert field["label"] == "Хищники" and field["hint"] == "Описание"
    path.unlink()
    workshop._TRANSLATIONS.clear()
    assert NATIVE_READER("42.20.4")["Sandbox_AnimalMetaPredator"] == "Хищники"
    assert NATIVE_READER("42.20.5") == {}


def test_mod_json_translations_and_enum_value_translation():
    files = {
        "111/mods/Folder/42/mod.info": "id=example\n",
        "111/mods/Folder/42/media/sandbox-options.txt": "option Example.Mode { type=enum, numValues=2, default=1, page=ExamplePage, translation=ModeName, valueTranslation=ModeValues, }\noption Example.Text { type=string, default=, page=ExamplePage, }",
        "111/mods/Folder/common/media/lua/shared/Translate/RU/Sandbox.json": json.dumps(
            {
                "Sandbox_ExamplePage": "Страница",
                "Sandbox_ModeName": "Режим",
                "Sandbox_ModeValues_option1": "Первый",
                "Sandbox_ModeValues_option2": "Второй",
            }
        ),
    }
    options = workshop.build_index(files, ["111"], "42.20.4")["111"][0]["options"]
    assert options[0]["group"] == "Страница" and options[0]["label"] == "Режим"
    assert options[0]["choices"][1] == {"value": 2, "label": "Второй"}
    assert options[0]["custom"] and options[0]["valueTranslation"] == "ModeValues"
    assert options[1]["default"] == ""


@pytest.mark.parametrize(
    "key,kind",
    [
        ("MultiplierConfig.Aiming", "double"),
        ("MultiplierConfig.Blacksmith", "double"),
        ("ZombieConfig.ZombiesCountBeforeDelete", "integer"),
    ],
)
def test_serialized_stock_names_are_distinct_from_java_field_names(key, kind):
    field = configschema.field(key, 2, sandbox=True)
    assert field["stock"] and field["type"] == kind
    assert not configschema.field("MultiplierConfig.UnconfirmedModSkill", 2, sandbox=True).get(
        "stock"
    )


def test_modpack_never_exports_or_imports_stock_lua_aliases():
    path = Path(config.CFG["data_dir"]) / "Server/world_SandboxVars.lua"
    path.write_text(
        "SandboxVars={VERSION=5, MultiplierConfig={Aiming=2.0}, ZombieConfig={ZombiesCountBeforeDelete=300}, Mod={Count=2}}",
        encoding="utf-8",
    )
    current = editor.draft("world.ini")
    pack = editor.modpack({"file": "world.ini"})["pack"]
    assert pack["sandbox"] == {"Mod.Count": 2}
    for key in ("MultiplierConfig.Aiming", "ZombieConfig.ZombiesCountBeforeDelete"):
        with pytest.raises(editor.EditorError, match="штатные параметры мира"):
            editor.modpack(
                {
                    "file": "world.ini",
                    "draftRevision": current["draftRevision"],
                    "pack": {**pack, "sandbox": {key: 1}},
                }
            )


@pytest.mark.parametrize(
    "text",
    [
        "SandboxVars={end=5}",
        "SandboxVars={[computed]=5}",
        "SandboxVars={[5]=5}",
        'SandboxVars={Text="one\ntwo"}',
        "SandboxVars={[[broken]=5}",
    ],
)
def test_invalid_or_computed_lua_is_not_accepted_as_literal_data(text):
    with pytest.raises(FormatError):
        LuaTable(text)


def test_lua_strings_hex_and_service_version_are_read_without_execution():
    text = '-- VERSION=999\nSandboxVars={VERSION=6, Text=[=[\nfirst\nsecond]=], Escaped="first\\\r\nsecond", Hex=0xFF, Mod={VERSION=3}, Note="VERSION=100"}'
    table = LuaTable(text)
    assert table.values[("Text",)]["value"] == "first\nsecond"
    assert table.values[("Escaped",)]["value"] == "first\nsecond"
    assert table.values[("Hex",)]["value"] == 255
    assert version_marker(text) == "6"
    assert version_marker('SandboxVars={["VERSION"]=6, Zombies=2+2}') == "6"
    assert "DEADBEEF" not in mask_lua("SandboxVars={VERSION=6, Password=0xDEADBEEF + 1}")


def test_changing_a_version_comment_does_not_change_service_field():
    path = Path(config.CFG["data_dir"]) / "Server/world_SandboxVars.lua"
    original = "-- VERSION=99 is just documentation\nSandboxVars={VERSION=5, Zombies=4}"
    path.write_text(original, encoding="utf-8")
    change(texts={"sandbox": original.replace("VERSION=99", "VERSION=100")})
    assert editor.validate("world.ini")["valid"]
    change(texts={"sandbox": original.replace("VERSION=5", "VERSION=6")})
    assert any(e.get("key") == "VERSION" for e in editor.validate("world.ini")["errors"])


def test_future_content_cannot_supply_compatible_b42_ids():
    branch = "42.99"
    files = {f"111/mods/Folder/{branch}/mod.info": "id=real-id\nname=Future mod"}
    record = workshop.build_index(files, ["111"], "42.20.4")["111"][0]
    assert record["modId"] == "real-id" and record["compatible"] is False
    issues = workshop.problems({"111": [record]}, ["real-id"], "42.20.4")
    assert any(p["severity"] == "error" and p["code"] == "version" for p in issues)


def test_common_only_metadata_is_valid_b42_content():
    files = {
        "111/mods/Folder/common/mod.info": "id=real-id\nname=Common mod",
        "111/mods/Folder/common/media/sandbox-options.txt": "option Common.Value {type=integer, default=2,}",
    }
    record = workshop.build_index(files, ["111"], "42.21.0")["111"][0]
    assert record["modId"] == "real-id" and record["compatible"] is True
    assert record["branch"] == "common" and record["options"][0]["default"] == 2
    assert not workshop.problems({"111": [record]}, ["real-id"], "42.21.0")


def test_versioned_content_inherits_common_metadata_and_overrides_options():
    files = {
        "111/mods/Folder/common/mod.info": "id=real-id\nrequire=library",
        "111/mods/Folder/common/media/sandbox-options.txt": "option Common.Value {type=integer, default=2,}",
        "111/mods/Folder/42.20/media/sandbox-options.txt": "option Common.Value {type=integer, default=3,}",
        "111/mods/Folder/42.99/media/sandbox-options.txt": "option Common.Value {type=integer, default=99,}",
    }
    record = workshop.build_index(files, ["111"], "42.21.0")["111"][0]
    assert record["compatible"] is True and record["branch"] == "42.20"
    assert record["path"].endswith("common/mod.info")
    assert record["require"] == ["library"] and record["options"][0]["default"] == 3


def test_invalid_metadata_is_visible_even_without_selected_mod_ids():
    index = workshop.build_index(
        {"111/mods/Folder/42/mod.info": "name=Missing id"}, ["111"], "42.20.4"
    )
    assert any(
        p["workshopId"] == "111" and p["code"] == "metadata"
        for p in workshop.problems(index, [], "42.20.4")
    )


def test_existing_unknown_ini_keys_and_their_secrets_round_trip_losslessly():
    text = (
        "# comment\r\nPlugin.Password=private\r\nСвой параметр=значение\r\nUnknown.Key=preserve\r\n"
    )
    masked = mask_ini(text)
    assert "private" not in masked
    assert restore_ini_secrets(masked, text) == text
    assert edit_ini(text, {"Свой параметр": "новое"}) == text.replace("значение", "новое")
    assert edit_ini(text, {"Unknown.Key": "new"}) == text.replace("preserve", "new")
    with pytest.raises(FormatError):
        edit_ini(text, {"Injected\nKey": "new"})


def test_source_edit_retains_mixed_line_endings_and_new_line_convention():
    original = "# comment\r\nOne=1\nTwo=2\r\nThree=3\r\n"
    browser = "# comment\nOne=1\nTwo=changed\nAdded=4\nThree=3\n"
    assert (
        preserve_newlines(original, browser)
        == "# comment\r\nOne=1\nTwo=changed\r\nAdded=4\r\nThree=3\r\n"
    )
    assert preserve_newlines(original, original.replace("\r\n", "\n")) == original


@pytest.mark.parametrize(
    "literal", ['"private\nvalue"', '"unterminated-private', "[=[unfinished-private"]
)
def test_invalid_string_literals_stay_private_and_restore_exactly(literal):
    original = "SandboxVars={VERSION=5, Password=" + literal
    masked = mask_lua(original)
    assert "private" not in masked
    assert restore_raw_literals(masked, original) == original


def test_unsupported_source_cannot_remove_service_version_by_emptying_file(monkeypatch):
    path = Path(config.CFG["data_dir"]) / "Server/world_SandboxVars.lua"
    path.write_text("SandboxVars={VERSION=5, Zombies=2+2}", encoding="utf-8")
    monkeypatch.setattr(editor, "syntax_check", lambda text: None)
    change(texts={"sandbox": ""})
    assert any(e.get("key") == "VERSION" for e in editor.validate("world.ini")["errors"])
