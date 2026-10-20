"""Source drafts preserve original bytes and reject unresolved secret markers."""

import json

import pytest

import configeditor as editor
from configformats import (
    FormatError,
    LuaTable,
    SECRET,
    literal,
    mask_lua,
    preserve_newlines,
    raw_literals,
    restore_raw_literals,
)
from configprofiles import profile_diff
from test_configeditor import env as env


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("final_newline", [False, True])
def test_appending_after_an_unterminated_line_keeps_lines_separate(newline, final_newline):
    original = f"# retain{newline}PublicName=Server"
    edited = "# retain\nPublicName=Server\nMaxPlayers=32" + ("\n" if final_newline else "")
    expected = f"# retain{newline}PublicName=Server{newline}MaxPlayers=32"
    assert preserve_newlines(original, edited) == expected + (newline if final_newline else "")


def test_source_save_appends_ini_key_without_changing_previous_value(env):
    data, _ = env
    ini = data / "Server/world.ini"
    ini.write_bytes(b"PublicName=Server")
    current = editor.draft("world.ini")
    result = editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "texts": {"ini": "PublicName=Server\nMaxPlayers=32\n"},
        }
    )
    fields = {field["key"]: field["value"] for field in result["fields"]}
    assert fields["PublicName"] == "Server"
    assert fields["MaxPlayers"] == "32"
    assert ini.read_bytes() == b"PublicName=Server"


@pytest.mark.parametrize(
    "raw",
    ['"\\208\\159"', "'\\065\\066'", "[=[\nsecret\r\nvalue]=]", "0x2A", "2.500", "true", "false"],
)
def test_masked_lua_source_restores_exact_secret_literal(env, raw):
    data, _ = env
    path = data / "Server/world_SandboxVars.lua"
    original = f"SandboxVars={{VERSION=5, Mod={{Password={raw}}}, Count=1}}\n"
    path.write_bytes(original.encode())
    current = editor.draft("world.ini")
    result = editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "texts": {"sandbox": current["texts"]["sandbox"].replace("Count=1", "Count=2")},
        }
    )
    saved = editor.load_json(editor.state_dir("world.ini") / "draft.json")
    assert saved["texts"]["sandbox"] == original.replace("Count=1", "Count=2")
    assert path.read_bytes() == original.encode()
    assert LuaTable(result["texts"]["sandbox"]).values[("Mod", "Password")]["value"] == SECRET


def test_literal_to_expression_source_checks_restored_source_before_saving(env, monkeypatch):
    data, _ = env
    path = data / "Server/world_SandboxVars.lua"
    original = 'SandboxVars={VERSION=5, Password="\\208\\159", SecretCode=0x2A, Count=1}\n'
    path.write_bytes(original.encode())
    current = editor.draft("world.ini")
    submitted = current["texts"]["sandbox"].replace("Count=1", "Count=math.floor(4.2)")
    checked = []
    monkeypatch.setattr(editor, "syntax_check", checked.append)
    result = editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "texts": {"sandbox": submitted},
        }
    )
    expected = original.replace("Count=1", "Count=math.floor(4.2)")
    saved = editor.load_json(editor.state_dir("world.ini") / "draft.json")
    assert checked == [expected]
    assert saved["texts"]["sandbox"] == expected
    assert result["sandboxDiagnostic"] and not result["sandboxFields"]
    assert "\\208\\159" not in json.dumps(result)
    assert path.read_bytes() == original.encode()


def test_rejected_expression_source_keeps_persisted_draft_unchanged(env, monkeypatch):
    current = editor.draft("world.ini")
    path = editor.state_dir("world.ini") / "draft.json"
    before = path.read_bytes()

    def reject(text):
        raise editor.EditorError("invalid Lua")

    monkeypatch.setattr(editor, "syntax_check", reject)
    with pytest.raises(editor.EditorError, match="invalid Lua"):
        editor.patch(
            {
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "texts": {
                    "sandbox": current["texts"]["sandbox"].replace("Zombies = 4", "Zombies = 1 +")
                },
            }
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("placeholder", [SECRET, "__PZ_RAW_LITERAL_unknown__"])
def test_unknown_lua_marker_cannot_be_persisted(env, monkeypatch, placeholder):
    current = editor.draft("world.ini")
    path = editor.state_dir("world.ini") / "draft.json"
    before = path.read_bytes()
    checked = []
    monkeypatch.setattr(editor, "syntax_check", checked.append)
    with pytest.raises(FormatError):
        editor.patch(
            {
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "texts": {
                    "sandbox": f'SandboxVars={{VERSION=5, NewSecret="{placeholder}", Count=1+2}}'
                },
            }
        )
    assert path.read_bytes() == before
    assert not checked


def test_legacy_generic_secret_marker_still_restores_literal_bytes(env):
    data, _ = env
    original = 'SandboxVars={VERSION=5, Password="\\208\\159", Count=1}\n'
    (data / "Server/world_SandboxVars.lua").write_bytes(original.encode())
    current = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "texts": {"sandbox": f'SandboxVars={{VERSION=5, Password="{SECRET}", Count=2}}\n'},
        }
    )
    saved = editor.load_json(editor.state_dir("world.ini") / "draft.json")
    assert saved["texts"]["sandbox"] == original.replace("Count=1", "Count=2")


def test_secret_source_markers_are_stable_when_the_secret_changes():
    first = 'SandboxVars={Mod={Password="first-secret"}, Count=1}'
    second = first.replace("first-secret", "second-secret")
    assert mask_lua(first) == mask_lua(second)
    assert LuaTable(mask_lua(first)).values[("Mod", "Password")]["value"] != "first-secret"


@pytest.mark.parametrize(
    "body",
    [
        f'Password="{SECRET}", Password="{SECRET}"',
        f'Mod={{NewPassword="{SECRET}"}}',
        f'Mod={{Password="{SECRET}" .. "changed"}}',
        f'Mod=(function() return {{Password="{SECRET}"}} end)()',
        "Count=1, Count=2",
    ],
)
def test_duplicate_or_misplaced_markers_are_rejected_before_syntax_check(env, monkeypatch, body):
    current = editor.draft("world.ini")
    path = editor.state_dir("world.ini") / "draft.json"
    before = path.read_bytes()
    checked = []
    monkeypatch.setattr(editor, "syntax_check", checked.append)
    with pytest.raises(FormatError):
        editor.patch(
            {
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "texts": {"sandbox": f"SandboxVars={{VERSION=5,{body}}}"},
            }
        )
    assert not checked
    assert path.read_bytes() == before


def test_expression_before_secret_fields_does_not_change_their_table_paths(env, monkeypatch):
    current = editor.draft("world.ini")
    submitted = current["texts"]["sandbox"].replace(
        "Zombies = 4",
        "Zombies = (function() if true then return math.floor(4.2) end; return 4 end)()",
    )
    checked = []
    monkeypatch.setattr(editor, "syntax_check", checked.append)
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "texts": {"sandbox": submitted},
        }
    )
    assert len(checked) == 1
    assert 'Password = "hidden-token"' in checked[0]
    assert SECRET not in checked[0]


@pytest.mark.parametrize("value", ["1e999", "9" * 400])
def test_nonfinite_literal_cannot_fall_back_to_expression_syntax_check(env, monkeypatch, value):
    current = editor.draft("world.ini")
    path = editor.state_dir("world.ini") / "draft.json"
    before = path.read_bytes()
    checked = []
    monkeypatch.setattr(editor, "syntax_check", checked.append)
    with pytest.raises(FormatError, match="Неконечное"):
        editor.patch(
            {
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "texts": {"sandbox": f"SandboxVars={{VERSION=5, Count={value}}}"},
            }
        )
    assert not checked
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "escaped, expected",
    [
        (r"\208\159", "П"),
        (r"\240\159\154\128", "🚀"),
        (r"prefix\208\159-\65\66tail", "prefixП-ABtail"),
        (r"\000\208\159\010", "\0П\n"),
        (r"\208\1590", "П0"),
        (r"\\208\\159", r"\208\159"),
    ],
)
def test_decimal_lua_escape_runs_decode_valid_utf8(escaped, expected):
    source = f'SandboxVars={{Text="{escaped}"}}'
    assert LuaTable(source).values[("Text",)]["value"] == expected


@pytest.mark.parametrize(
    "escaped, expected",
    [
        (r"\255", "\xff"),
        (r"\208", "\xd0"),
        (r"\192\175", "\xc0\xaf"),
        (r"\237\160\128", "\xed\xa0\x80"),
        (r"\255\208\159", "\xff\xd0\x9f"),
    ],
)
def test_non_utf8_decimal_escape_runs_keep_legacy_values_safe_for_json(escaped, expected):
    source = f'SandboxVars={{Text="{escaped}"}}'
    value = LuaTable(source).values[("Text",)]["value"]
    assert value == expected
    assert json.loads(json.dumps(value, ensure_ascii=False).encode("utf-8")) == expected


@pytest.mark.parametrize("escaped", [r"\256", r"\65\999"])
def test_out_of_range_decimal_lua_escapes_remain_invalid(escaped):
    with pytest.raises(FormatError, match="escape"):
        LuaTable(f'SandboxVars={{Text="{escaped}"}}')


@pytest.mark.parametrize("automatic", [False, True])
def test_startup_verifies_utf8_escape_reserialization_without_revealing_secrets(
    env, monkeypatch, automatic
):
    data, _ = env
    path = data / "Server/world_SandboxVars.lua"
    expected = 'SandboxVars={VERSION=5, Password="\\208\\159", Count=1}\n'
    actual = expected.replace('"\\208\\159"', '"П"')
    path.write_bytes(expected.encode())
    initial = editor.draft("world.ini")
    editor.save_json(
        editor.state_dir("world.ini") / "state.json",
        {
            "status": "saved",
            "savedRevision": initial["baseRevision"],
            "snapshots": {"saved": editor.read_profile("world.ini")},
        },
    )
    path.write_bytes(actual.encode())
    monkeypatch.setattr(
        editor.ops, "container_state", lambda: {"running": True, "startedAt": "utf8-start"}
    )
    monkeypatch.setattr(editor.ops, "rcon", lambda *args, **kwargs: "Players connected (0):")
    if automatic:
        editor.auto_verify_running()
        result = editor.draft("world.ini")
    else:
        result = editor.verify_running(
            {
                "file": "world.ini",
                "draftRevision": initial["draftRevision"],
                "currentRevision": editor.revision(editor.read_profile("world.ini")),
            }
        )
    assert result["status"] == "applied"
    assert result["state"]["savedRevision"] == result["currentRevision"]
    assert LuaTable(result["texts"]["sandbox"]).values[("Password",)]["value"] == SECRET
    assert path.read_bytes() == actual.encode()


def test_raw_secret_marker_cannot_be_moved_into_a_public_literal_field(env, monkeypatch):
    data, _ = env
    original = 'SandboxVars={VERSION=5,Password="raw-secret"..tostring(2)}\n'
    path = data / "Server/world_SandboxVars.lua"
    path.write_bytes(original.encode())
    current = editor.draft("world.ini")
    marker = next(rec[2] for rec in raw_literals(original) if rec[3] == '"raw-secret"')
    draft_path = editor.state_dir("world.ini") / "draft.json"
    before = draft_path.read_bytes()
    checked = []
    monkeypatch.setattr(editor, "syntax_check", checked.append)
    with pytest.raises(FormatError):
        editor.patch(
            {
                "file": "world.ini",
                "draftRevision": current["draftRevision"],
                "texts": {"sandbox": f"SandboxVars={{VERSION=5,PublicName={literal(marker)}}}\n"},
            }
        )
    assert draft_path.read_bytes() == before
    assert path.read_bytes() == original.encode()
    assert not checked
    assert "raw-secret" not in json.dumps(editor.draft("world.ini"))


@pytest.mark.parametrize(
    "attack", ["duplicate", "embedded", "different-expression", "different-slot"]
)
def test_raw_literal_markers_reject_changed_ownership_or_expression_context(attack):
    original = 'SandboxVars={VERSION=5,Password="raw-secret"..tostring(2)}'
    masked = mask_lua(original)
    secret, number = [literal(rec[2]) for rec in raw_literals(original)[1:]]
    submitted = {
        "duplicate": masked[:-1] + f",PublicName={secret}}}",
        "embedded": masked.replace(secret, literal("prefix-" + raw_literals(original)[1][2])),
        "different-expression": masked.replace(f"{secret}..tostring({number})", secret),
        "different-slot": masked.replace(
            f"{secret}..tostring({number})", f"{number}..tostring({secret})"
        ),
    }[attack]
    with pytest.raises(FormatError):
        restore_raw_literals(submitted, original)


@pytest.mark.parametrize("edit", ["comment", "explicit-value", "new-field"])
def test_raw_source_retains_markers_during_safe_local_edits(edit):
    original = (
        'SandboxVars={VERSION=5,Password="raw-secret"..tostring(2),Mod={Count=math.floor(4.2)}}\n'
    )
    masked = mask_lua(original)
    if edit == "comment":
        submitted, expected = masked + "-- retained\n", original + "-- retained\n"
    elif edit == "explicit-value":
        marker = next(rec[2] for rec in raw_literals(original) if rec[3] == "4.2")
        submitted = masked.replace(literal(marker), "6.5")
        expected = original.replace("4.2", "6.5")
    else:
        submitted = masked.replace("Password=", 'PublicName="safe",Password=')
        expected = original.replace("Password=", 'PublicName="safe",Password=')
    assert restore_raw_literals(submitted, original) == expected


def test_raw_string_key_markers_restore_only_their_original_key_role():
    original = 'SandboxVars={VERSION=5,["Password"]="raw-secret"..tostring(2)}'
    masked = mask_lua(original)
    key = literal(next(rec[2] for rec in raw_literals(original) if rec[3] == '"Password"'))
    assert (
        restore_raw_literals(masked + " -- key retained", original) == original + " -- key retained"
    )
    assert restore_raw_literals(masked.replace(key, '"Password"'), original) == original
    moved = masked.replace(key, '"Password"')[:-1] + f",PublicName={key}}}"
    with pytest.raises(FormatError):
        restore_raw_literals(moved, original)


def test_changing_a_masked_key_cannot_make_its_hidden_value_public():
    original = 'SandboxVars={VERSION=5,["Password.X"]="raw-secret"}'
    masked = mask_lua(original)
    key = literal(next(rec[2] for rec in raw_literals(original) if rec[3] == '"Password.X"'))
    with pytest.raises(FormatError):
        restore_raw_literals(masked.replace(key, '"PublicName"'), original)


def test_ambiguous_raw_context_preserves_comments_and_rejects_changed_retained_context():
    original = 'SandboxVars=configure({Password="raw-secret"},2)'
    masked = mask_lua(original)
    number = literal(next(rec[2] for rec in raw_literals(original) if rec[3] == "2"))
    assert restore_raw_literals(masked + " -- retained", original) == original + " -- retained"
    with pytest.raises(FormatError):
        restore_raw_literals(masked.replace(number, "3"), original)
    assert restore_raw_literals('SandboxVars=configure({Password="replacement"},3)', original) == (
        'SandboxVars=configure({Password="replacement"},3)'
    )


def test_raw_source_markers_do_not_fingerprint_hidden_literal_values():
    first = 'SandboxVars={Password="first-secret"..tostring(2)}'
    second = first.replace("first-secret", "second-secret")
    assert mask_lua(first) == mask_lua(second)


def test_retained_raw_markers_cannot_swap_between_nested_fields():
    original = 'SandboxVars={A={Password="first"..tostring(1)},B={Password="second"..tostring(2)}}'
    masked = mask_lua(original)
    first, second = [
        literal(rec[2]) for rec in raw_literals(original) if rec[3] in ('"first"', '"second"')
    ]
    swapped = (
        masked.replace(first, '"temporary"').replace(second, first).replace('"temporary"', second)
    )
    with pytest.raises(FormatError):
        restore_raw_literals(swapped, original)


@pytest.mark.parametrize("suffix", ["000000000000", "ffffffffffff"])
def test_legacy_raw_marker_suffix_is_not_a_secret_authentication_digest(suffix):
    original = 'SandboxVars={Password="raw-secret"..tostring(2)}'
    masked = mask_lua(original)
    marker = raw_literals(original)[0][2]
    legacy = f"__PZ_RAW_LITERAL_0_{suffix}__"
    assert restore_raw_literals(masked.replace(marker, legacy), original) == original


@pytest.mark.parametrize(
    "before, after, hidden",
    [
        (
            'SandboxVars={Password="old-secret"..tostring(2)}\n',
            'SandboxVars={Password="new-secret"..tostring(2)}\n',
            ("old-secret", "new-secret"),
        ),
        (
            'SandboxVars={Password="unchanged"..tostring(2),Count=1}\n',
            'SandboxVars={Password="unchanged"..tostring(2),Count=3}\n',
            ("unchanged",),
        ),
        (
            'SandboxVars={["A.Name"]="first",["B.Name"]="second"}\n',
            'SandboxVars={["B.Name"]="second",["A.Name"]="first"}\n',
            ("A.Name", "B.Name", "first", "second"),
        ),
        (
            'SandboxVars={Password="retained"..tostring(2)}\n',
            'SandboxVars={NewName="inserted",Password="retained"..tostring(2)}\n',
            ("inserted", "retained"),
        ),
    ],
)
def test_raw_review_shows_literal_edits_without_fingerprinting_or_exposing_values(
    before, after, hidden
):
    original = {"ini": "", "sandbox": before}
    desired = {"ini": "", "sandbox": after}
    diff = profile_diff(original, desired, "Before", "After")["sandbox"]
    assert diff
    assert "_changed_before__" in diff
    assert "_changed_after__" in diff
    assert all(value not in diff for value in hidden)
    assert original["sandbox"] == before and desired["sandbox"] == after


def test_raw_review_annotations_do_not_change_the_editable_source_markers():
    before = 'SandboxVars={Password="first-secret"..tostring(2)}'
    after = before.replace("first-secret", "second-secret")
    assert mask_lua(before) == mask_lua(after)
    assert "_changed_" not in mask_lua(before)
    assert profile_diff(
        {"ini": "", "sandbox": before}, {"ini": "", "sandbox": before}, "A", "B"
    ) == {
        "ini": "",
        "sandbox": "",
    }
