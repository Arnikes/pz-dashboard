"""Lossless parsing must preserve comments and reject conflicting field edits."""

import pytest

from configformats import FormatError, LuaTable, edit_ini, ini_entries


@pytest.mark.parametrize("line", ["  # keep comment", "  ; keep comment", "  "])
def test_ini_continuations_stop_before_comments_and_blank_lines(line):
    original = f"Mods=one;\r\n{line}\r\nPublicName=hi\r\n"
    assert ini_entries(original)["Mods"]["value"] == "one;"
    assert edit_ini(original, {"Mods": "two"}) == original.replace("one;", "two")


@pytest.mark.parametrize(
    "key, alias", [("Mods", "mods"), ("WorkshopItems", "WORKSHOPITEMS"), ("Password", "password")]
)
def test_ini_edits_use_the_same_canonical_keys_as_the_parser(key, alias):
    original = f"{key}=old\n"
    edited = edit_ini(original, {alias: "new"})
    assert edited == f"{key}=new\n"
    assert ini_entries(edited)[key]["value"] == "new"


def test_ini_edits_reject_duplicate_aliases():
    with pytest.raises(FormatError):
        edit_ini("Mods=old\n", {"Mods": "first", "mods": "second"})


@pytest.mark.parametrize("changes", [{"New.Child": 1, "New": 2}, {"New": 2, "New.Child": 1}])
def test_lua_additions_reject_parent_and_child_field_conflicts(changes):
    with pytest.raises(FormatError):
        LuaTable("SandboxVars={}").edit(changes)
