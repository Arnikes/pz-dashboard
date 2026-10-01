"""Public Steam responses omit file_type; collections need their own endpoint."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import workshop  # noqa: E402


@pytest.fixture
def steam(monkeypatch):
    calls = []
    collections = {"1": ["3", "2"]}
    published = {}

    def request(method, ids):
        calls.append((method, list(ids)))
        if method == "GetCollectionDetails":
            return {
                "collectiondetails": [
                    {
                        "publishedfileid": wid,
                        "result": 1 if wid in collections else 9,
                        **(
                            {"children": [{"publishedfileid": child} for child in collections[wid]]}
                            if wid in collections
                            else {}
                        ),
                    }
                    for wid in reversed(ids)
                ]
            }
        return {
            "publishedfiledetails": [
                {
                    "publishedfileid": wid,
                    "result": 1,
                    "consumer_app_id": 108600,
                    "title": f"Package {wid}",
                    **published.get(wid, {}),
                }
                for wid in reversed(ids)
            ]
        }

    monkeypatch.setattr(workshop, "steam_call", request)
    return calls, collections, published, request


def test_collection_without_file_type_keeps_source_and_child_order(steam):
    result = workshop.resolve("1", with_source=True)
    assert result == {
        "source": {"kind": "collection", "workshopId": "1", "title": "Package 1"},
        "items": [
            {"workshopId": "3", "title": "Package 3"},
            {"workshopId": "2", "title": "Package 2"},
        ],
    }


def test_single_item_is_not_a_collection(steam):
    assert workshop.resolve("2", with_source=True) == {
        "source": {"kind": "item", "workshopId": "2", "title": "Package 2"},
        "items": [{"workshopId": "2", "title": "Package 2"}],
    }
    workshop.validate_items(["2", "3"])


@pytest.mark.parametrize("children", [[], ["2"], ["2", "2"]])
def test_empty_or_single_item_collection_remains_a_collection(steam, children):
    steam[1]["1"] = children
    result = workshop.resolve("1", with_source=True)
    assert result["source"]["kind"] == "collection"
    assert len(result["items"]) == len(set(children))


def test_nested_collection_cannot_be_added_as_a_package(steam):
    steam[1]["2"] = ["4"]
    with pytest.raises(ValueError, match="вложенные коллекции"):
        workshop.resolve("1")
    with pytest.raises(ValueError, match="коллекцию"):
        workshop.validate_items(["2"])


@pytest.mark.parametrize("bad", [{"result": 9}, {"consumer_app_id": 440}, {"banned": True}])
def test_unavailable_or_wrong_game_child_is_rejected(steam, bad):
    steam[2]["2"] = bad
    with pytest.raises(ValueError, match="недоступен"):
        workshop.resolve("1")
    with pytest.raises(ValueError, match="недоступен"):
        workshop.validate_items(["2"])


@pytest.mark.parametrize("method", ["GetPublishedFileDetails", "GetCollectionDetails"])
@pytest.mark.parametrize("failure", ["missing", "duplicate", "another-id", "error"])
def test_incomplete_or_failed_steam_response_is_rejected(steam, monkeypatch, method, failure):
    original = steam[3]

    def request(name, ids):
        result = original(name, ids)
        if name == method:
            key = (
                "publishedfiledetails" if name == "GetPublishedFileDetails" else "collectiondetails"
            )
            if failure == "missing":
                result[key] = []
            elif failure == "duplicate":
                result[key].append(result[key][0])
            elif failure == "another-id":
                result[key][0]["publishedfileid"] = "999"
            else:
                result[key][0]["result"] = 2
        return result

    monkeypatch.setattr(workshop, "steam_call", request)
    with pytest.raises(ValueError):
        workshop.resolve("1")
    with pytest.raises(ValueError):
        workshop.validate_items(["2"])


def test_large_collection_checks_all_packages_in_batches(steam):
    ids = [str(i) for i in range(1000, 1300)]
    steam[1]["1"] = ids
    assert [r["workshopId"] for r in workshop.resolve("1")] == ids
    assert all(len(batch) <= 100 for _, batch in steam[0])
    assert len(steam[0]) == 8  # Two root calls and three batches per endpoint.
    steam[1]["1"].append("1300")
    with pytest.raises(ValueError, match="до 300"):
        workshop.resolve("1")


@pytest.mark.parametrize("child", ["", "bad", "1" * 21])
def test_invalid_child_id_is_rejected(steam, child):
    steam[1]["1"] = [child]
    with pytest.raises(ValueError, match="некорректный Workshop ID"):
        workshop.resolve("1")
