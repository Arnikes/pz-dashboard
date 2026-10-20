"""Count retention after verified S3 exports, with isolated storage and failures."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

import backups3
import backupverify
import config
import i18n
import ops
from test_backups3 import backup_env as backup_env  # noqa: F401
from test_backups3 import s3_client as s3_client  # noqa: F401


@pytest.fixture
def retention_catalog(backup_env, s3_client, monkeypatch):
    _, cfg = backup_env
    objects, deleted, events, listings = {}, [], [], []

    def add(name, modified):
        key = backups3._prefix(cfg) + name
        objects[key] = {
            "Key": key,
            "Size": 100,
            "LastModified": modified,
            "ETag": '"fixture-etag"',
        }
        return key

    for index in range(13):
        add(
            f"pz-backup-20261001-030000-{index:032x}.tar.gz",
            datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=index),
        )
    upload, read, delete = s3_client.upload_file, s3_client.get_object, s3_client.delete_object

    def uploaded(path, bucket, key, **kwargs):
        upload(path, bucket, key, **kwargs)
        events.append("upload")
        objects[key] = {
            "Key": key,
            "Size": Path(path).stat().st_size,
            "LastModified": s3_client.modified,
            "ETag": '"new-export"',
        }

    def readback(**kwargs):
        events.append("readback")
        return read(**kwargs)

    def pages(**kwargs):
        events.append("list")
        listings.append(kwargs)
        entries = [obj for key, obj in objects.items() if key.startswith(kwargs["Prefix"])]
        # Reverse chronology and paginate so retention must inspect every page.
        entries.reverse()
        return [{"Contents": entries[i : i + 4]} for i in range(0, len(entries), 4)]

    def removed(**kwargs):
        assert s3_client.body.closed
        assert not list(Path(cfg["backup_dir"]).glob("pz-backup-20261010-*.tar.gz"))
        delete(**kwargs)
        events.append("delete")
        deleted.append(kwargs)
        del objects[kwargs["Key"]]

    monkeypatch.setattr(s3_client, "upload_file", uploaded)
    monkeypatch.setattr(s3_client, "get_object", readback)
    monkeypatch.setattr(s3_client, "get_paginator", lambda operation: Mock(paginate=pages))
    monkeypatch.setattr(s3_client, "delete_object", removed)
    monkeypatch.setattr(ops, "get_settings", lambda: {"backup": {"maxBackups": 10}})
    return SimpleNamespace(
        objects=objects, deleted=deleted, events=events, listings=listings, add=add
    )


@pytest.mark.parametrize("trigger", ["scheduled", "manual", "update", "config"])
def test_export_rotates_s3_to_limit_and_preserves_local_fallbacks(
    backup_env, s3_client, retention_catalog, trigger
):
    root, cfg = backup_env
    catalog = retention_catalog
    oldest = list(catalog.objects)[:4]
    previous = root / "backups/previous.tar.gz"
    previous.parent.mkdir()
    previous.write_bytes(b"failed export must remain available")
    verification = {"files": 1, "totalSize": 100, "hasServerIni": True, "hasMapData": True}
    for key, obj in catalog.objects.items():
        backupverify.remember(
            key.rsplit("/", 1)[-1], "s3", backupverify.s3_identity(obj), verification, cfg
        )
    assert len(backups3.list_archives(cfg)) == 13  # Prime the pre-export cache.
    catalog.events.clear()

    result = ops.run_backup_job(trigger, False)

    assert len(catalog.objects) == 10
    assert {item["Key"] for item in catalog.deleted} == set(oldest)
    assert s3_client.keys[-1] in catalog.objects
    assert previous.read_bytes() == b"failed export must remain available"
    assert catalog.events[:3] == ["upload", "readback", "list"]
    assert len(backups3.list_archives(cfg)) == 10  # Deletion invalidated the catalog.
    assert len(backupverify.receipts(cfg)) == 11  # Ten S3 receipts and the local receipt.
    (record,) = ops.get_backup_journal()
    assert record["status"] == "success" and record["path"] == result["path"]
    ops.log_event.assert_any_call("backup-delete", "Удалено старых бэкапов: 4")


@pytest.mark.parametrize("prefix", ["server-one", ""])
def test_retention_excludes_unrelated_and_nested_objects(backup_env, retention_catalog, prefix):
    _, cfg = backup_env
    catalog = retention_catalog
    if not prefix:
        cfg["backup_s3_prefix"] = ""
        catalog.objects.clear()
        for index in range(13):
            catalog.add(
                f"pz-backup-20261001-030000-{index:032x}.tar.gz",
                datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=index),
            )
    valid_name = "pz-backup-20261001-030000-" + "a" * 32 + ".tar.gz"
    protected = ["notes.txt", "nested/" + valid_name, "../" + valid_name]
    protected_keys = {
        catalog.add(name, datetime(2026, 1, 1, tzinfo=timezone.utc)) for name in protected
    }
    outside = "other-server/" + valid_name
    catalog.objects[outside] = dict(catalog.objects[next(iter(protected_keys))], Key=outside)

    ops.run_backup_job("scheduled", False)

    assert protected_keys | {outside} <= catalog.objects.keys()
    assert len(backups3.list_archives(cfg)) == 10
    assert all(item["Bucket"] == cfg["backup_s3_bucket"] for item in catalog.deleted)


def test_zero_retention_does_not_list_or_delete(backup_env, retention_catalog, monkeypatch):
    monkeypatch.setattr(ops, "get_settings", lambda: {"backup": {"maxBackups": 0}})
    ops.run_backup_job("scheduled", False)
    assert len(retention_catalog.objects) == 14
    assert not retention_catalog.listings and not retention_catalog.deleted


@pytest.mark.parametrize("listed", [True, False])
def test_new_export_survives_equal_timestamps_and_delayed_listing(
    backup_env, s3_client, retention_catalog, monkeypatch, listed
):
    _, cfg = backup_env
    catalog = retention_catalog
    for obj in catalog.objects.values():
        obj["LastModified"] = s3_client.modified
    original_pages = s3_client.get_paginator

    # Reverse insertion order would put the current export last in the deletion slice.
    def paginator(operation):
        paginator = original_pages(operation)
        pages = paginator.paginate

        def ordered(**kwargs):
            rows = [row for page in pages(**kwargs) for row in page["Contents"]]
            current = set(s3_client.keys)
            rows.sort(key=lambda row: row["Key"] in current)
            return [{"Contents": [row for row in rows if listed or row["Key"] not in current]}]

        paginator.paginate = ordered
        return paginator

    monkeypatch.setattr(s3_client, "get_paginator", paginator)
    monkeypatch.setattr(ops, "get_settings", lambda: {"backup": {"maxBackups": 1}})
    result = ops.run_backup_job("scheduled", False)
    assert list(catalog.objects) == [backups3._key(result["name"], cfg)]
    assert len(catalog.deleted) == 13


@pytest.mark.parametrize("failure", ["upload", "readback", "checksum", "local-delete"])
def test_failed_export_never_starts_retention(
    backup_env, s3_client, retention_catalog, monkeypatch, failure
):
    if failure == "upload":
        s3_client.upload_error = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
    elif failure == "readback":
        s3_client.read_error = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    elif failure == "checksum":
        s3_client.corrupt = True
    else:
        monkeypatch.setattr(backups3.os, "remove", Mock(side_effect=PermissionError("locked")))
    with pytest.raises(ops.OpsErrorReported):
        ops.run_backup_job("scheduled", False)
    assert not retention_catalog.listings and not retention_catalog.deleted
    assert list(Path(backup_env[1]["backup_dir"]).glob("*.tar.gz"))


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("failure", ["list", "delete"])
def test_retention_errors_are_visible_and_preserve_saved_export(
    backup_env, s3_client, retention_catalog, monkeypatch, failure, language
):
    _, cfg = backup_env
    operation = "ListObjectsV2" if failure == "list" else "DeleteObject"
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "secret-access-token"}}, operation
    )
    if failure == "list":
        monkeypatch.setattr(s3_client, "get_paginator", Mock(side_effect=error))
    else:
        monkeypatch.setattr(s3_client, "delete_object", Mock(side_effect=error))
    with pytest.raises(ops.OpsErrorReported) as caught:
        ops.run_backup_job("scheduled", False)
    assert len(retention_catalog.objects) == 14 and not retention_catalog.deleted
    (record,) = ops.get_backup_journal()
    assert record["status"] == "error"
    assert record["name"] == s3_client.keys[-1].rsplit("/", 1)[-1]
    assert record["path"] == f"s3://{cfg['backup_s3_bucket']}/{s3_client.keys[-1]}"
    assert s3_client.keys[-1] in retention_catalog.objects
    message = i18n.translate(str(caught.value), locale=language)
    assert "secret-access-token" not in message
    if language == "en":
        assert "failed" in message or "unavailable" in message
    else:
        assert "не удалось" in message or "недоступен" in message
    assert len([call for call in ops.log_event.call_args_list if call.args[0] == "error"]) == 1


def test_retention_stops_after_partial_deletion_failure(
    backup_env, s3_client, retention_catalog, monkeypatch
):
    remove = s3_client.delete_object
    attempts = []

    def fail_second(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 2:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")
        remove(**kwargs)

    monkeypatch.setattr(s3_client, "delete_object", fail_second)
    with pytest.raises(ops.OpsErrorReported):
        ops.run_backup_job("scheduled", False)
    assert len(attempts) == 2 and len(retention_catalog.deleted) == 1
    assert len(retention_catalog.objects) == 13
    assert s3_client.keys[-1] in retention_catalog.objects


def test_retention_uses_export_destination_snapshot(backup_env, retention_catalog, monkeypatch):
    _, cfg = backup_env
    replacement = dict(cfg, backup_s3_bucket="another-bucket", backup_s3_prefix="other-server")

    def phase(message, *args, **kwargs):
        if message == "Удаление локальной копии бэкапа":
            monkeypatch.setattr(config, "CFG", replacement)

    monkeypatch.setattr(ops, "_set_phase", phase)
    ops.run_backup_job("scheduled", False)
    assert len(retention_catalog.objects) == 10
    assert retention_catalog.listings == [{"Bucket": "test-backups", "Prefix": "server-one/"}]
    assert {item["Bucket"] for item in retention_catalog.deleted} == {"test-backups"}
    assert all(item["Key"].startswith("server-one/") for item in retention_catalog.deleted)


def test_retention_receipt_cleanup_failure_is_a_warning(backup_env, retention_catalog, monkeypatch):
    forget = backupverify.forget

    def fail_remote(name, storage, cfg):
        if storage == "s3":
            raise PermissionError("private-path")
        forget(name, storage, cfg)

    monkeypatch.setattr(backupverify, "forget", fail_remote)
    ops.run_backup_job("scheduled", False)
    assert len(retention_catalog.objects) == 10
    assert ops.get_backup_journal()[0]["status"] == "success"
    ops.log_event.assert_any_call("warn", "Бэкап удалён, но результат проверки удалить не удалось")
    assert "private-path" not in str(ops.log_event.call_args_list)
