"""Malformed optional metadata must not interrupt Workshop discovery."""

import io
import json
from collections import OrderedDict
from pathlib import Path

import pytest

import config
import i18n
import workshop


DEEP_JSON = "[" * 12_000 + "0" + "]" * 12_000
FILES = {
    "111/mods/Example/42/mod.info": "id=example\nname=Example\nversionMin=42.0\n",
    "111/mods/Example/42/media/sandbox-options.txt": (
        "option Example.Mode { type=enum, numValues=2, default=1, translation=Mode, }"
    ),
    "111/mods/Broken/42/mod.info": "name=Missing identifier\n",
}


@pytest.fixture
def metadata_env(tmp_path, monkeypatch):
    monkeypatch.setitem(config.CFG, "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path / "dashboard"))
    monkeypatch.setenv("SERVER_FILES_DIR", str(tmp_path / "server-files"))
    monkeypatch.setattr(workshop, "_CACHE", OrderedDict())
    monkeypatch.setattr(workshop, "_TRANSLATIONS", OrderedDict())
    monkeypatch.setattr(workshop, "local_files", lambda items: dict(FILES))
    monkeypatch.setattr(workshop, "container_files", lambda items: {})
    token = i18n.LANGUAGE.set("ru")
    yield tmp_path
    i18n.LANGUAGE.reset(token)


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "null",
        '{"response": null}',
        '{"response": []}',
        '{"response": "untrusted-response-body"}',
        '{"response": untrusted-response-body}',
        DEEP_JSON,
    ],
    ids=["array", "null", "null-response", "array-response", "text", "invalid-json", "deep"],
)
def test_invalid_steam_envelope_is_a_controlled_resolve_error(monkeypatch, body):
    monkeypatch.setattr(
        workshop.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(body.encode())
    )
    with pytest.raises(ValueError) as error:
        workshop.resolve("111")
    assert str(error.value) == "Steam вернул некорректные метаданные item"
    assert "untrusted-response-body" not in str(error.value)


def test_valid_steam_envelopes_still_resolve_a_package(monkeypatch):
    def response(request, **kwargs):
        key = (
            "collectiondetails"
            if "GetCollectionDetails" in request.full_url
            else "publishedfiledetails"
        )
        record = {
            "publishedfileid": "111",
            "result": 9 if key == "collectiondetails" else 1,
            "consumer_app_id": 108600,
            "title": "Example",
        }
        return io.BytesIO(json.dumps({"response": {key: [record]}}).encode())

    monkeypatch.setattr(workshop.urllib.request, "urlopen", response)
    assert workshop.resolve("111") == [{"workshopId": "111", "title": "Example"}]


def test_deep_mod_translation_keeps_metadata_options_and_valid_translation(metadata_env):
    files = {
        **FILES,
        "111/mods/Example/42/media/lua/shared/Translate/EN/Sandbox.json": (
            '{"Sandbox": {"Mode": "Mode from English"}}'
        ),
        "111/mods/Example/42/media/lua/shared/Translate/RU/Sandbox.json": DEEP_JSON,
    }
    index = workshop.build_index(files, ["111"], "42.20.4")
    record = next(rec for rec in index["111"] if rec["modId"] == "example")
    assert record["name"] == "Example"
    assert record["versionMin"] == "42.0"
    assert record["options"][0]["label"] == "Mode from English"
    assert len(record["options"][0]["choices"]) == 2


def test_deep_vanilla_translation_cache_is_ignored_and_can_recover(metadata_env):
    path = Path(config.CFG["data_dir"]) / "media/lua/shared/Translate/RU/Sandbox.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"Mode": "Installed mode"}', encoding="utf-8")
    assert workshop.vanilla_translations("42.20.4") == {"Sandbox_Mode": "Installed mode"}
    cache_path = next((Path(config.CFG["dashboard_dir"]) / "game-metadata").glob("*.json"))
    cache_path.write_text(DEEP_JSON, encoding="utf-8")
    path.unlink()
    workshop._TRANSLATIONS.clear()
    assert workshop.vanilla_translations("42.20.4") == {}
    assert cache_path.read_text(encoding="utf-8") == DEEP_JSON
    path.write_text('{"Mode": "Recovered mode"}', encoding="utf-8")
    workshop._TRANSLATIONS.clear()
    assert workshop.vanilla_translations("42.20.4") == {"Sandbox_Mode": "Recovered mode"}


def offline_cache(monkeypatch):
    index = workshop.scan(["111"], "42.20.4", refresh=True)
    directory = Path(config.CFG["dashboard_dir"]) / "workshop-index"
    path = next(directory.glob("*.json"))
    monkeypatch.setattr(workshop, "local_files", lambda items: {})
    workshop.invalidate()
    return index, path


@pytest.mark.parametrize(
    "damage",
    [
        "root",
        "records",
        "record",
        "identity",
        "dependency",
        "dependency-entry",
        "maps",
        "options",
        "option",
        "option-key",
        "option-choices",
        "version",
        "deep",
    ],
)
def test_invalid_offline_cache_keeps_fresh_index_and_disk(metadata_env, monkeypatch, damage):
    index, path = offline_cache(monkeypatch)
    record = index["111"][0]
    if damage == "root":
        index = []
    elif damage == "records":
        index["111"] = None
    elif damage == "record":
        index["111"].append(None)
    elif damage == "identity":
        record["modId"] = []
    elif damage == "dependency":
        record["require"] = "other"
    elif damage == "dependency-entry":
        record["require"] = [{}]
    elif damage == "maps":
        record["maps"] = [None]
    elif damage == "options":
        record["options"] = None
    elif damage == "option":
        record["options"] = [None]
    elif damage == "option-key":
        record["options"][0]["key"] = []
    elif damage == "option-choices":
        record["options"][0]["choices"] = [{}]
    elif damage == "version":
        del record["versionMin"]
    body = DEEP_JSON if damage == "deep" else json.dumps(index)
    path.write_text(body, encoding="utf-8")

    assert workshop.scan(["111"], "42.20.4") == {"111": []}
    assert path.read_text(encoding="utf-8") == body
    assert (
        workshop.problems(workshop.scan(["111"], "42.20.4"), ["example"], "42.20.4")[0]["code"]
        == "unknown"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("options", None),
        ("options", [None]),
        ("options", [{"key": [], "type": "string"}]),
        ("maps", [None]),
    ],
    ids=["options-container", "option-record", "option-key", "maps"],
)
def test_invalid_error_record_metadata_rejects_whole_cache(metadata_env, monkeypatch, field, value):
    index, path = offline_cache(monkeypatch)
    record = next(rec for rec in index["111"] if rec.get("error"))
    index["111"].append(dict(record))  # Retain a valid missing-id neighbor.
    record[field] = value
    body = json.dumps(index)
    path.write_text(body, encoding="utf-8")

    assert workshop.scan(["111"], "42.20.4") == {"111": []}
    assert path.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("error_metadata", [False, True])
def test_valid_offline_cache_retains_error_records_and_extensions(
    metadata_env, monkeypatch, error_metadata
):
    index, path = offline_cache(monkeypatch)
    record = index["111"][0]
    record["versionMin"] = "custom release 42.0 preview"
    record["extension"] = {"future": [1, 2]}
    record["options"][0]["type"] = "future-option-type"
    if error_metadata:
        error = next(rec for rec in index["111"] if rec.get("error"))
        error.update(options=[{"key": "Future.Option", "type": "future-type"}], maps=["Map"])
    body = json.dumps(index)
    path.write_text(body, encoding="utf-8")

    restored = workshop.scan(["111"], "42.20.4")
    assert restored == {"111": [{**entry, "metadataStale": True} for entry in index["111"]]}
    assert path.read_text(encoding="utf-8") == body
    assert {p["code"] for p in workshop.problems(restored, ["example"], "42.20.4")} == {
        "metadata",
        "cached",
    }
