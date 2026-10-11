"""S3 export ordering, failure preservation and real SDK transfer behavior."""

import hashlib
import io
import json
import os
import tarfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from concurrent.futures import ThreadPoolExecutor

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

import backups3
import backupverify
import config
import i18n
import ops
import payloads
from test_api import api as api  # noqa: F401


@pytest.fixture
def backup_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "Server").mkdir(parents=True)
    (data / "Server/test.ini").write_text("Mods=\n", encoding="utf-8")
    (data / "Maps").mkdir()
    (data / "Maps/map.bin").write_bytes(b"world")
    cfg = dict(config.CFG)
    cfg.update(
        data_dir=str(data),
        backup_dir=str(tmp_path / "backups"),
        dashboard_dir=str(tmp_path / "panel"),
        backup_s3_enabled=True,
        backup_s3_bucket="test-backups",
        backup_s3_prefix="server-one",
        backup_s3_endpoint="",
        backup_s3_region="us-east-1",
        backup_s3_addressing_style="path",
    )
    monkeypatch.setattr(config, "CFG", cfg)
    monkeypatch.setattr(ops, "is_running", lambda: False)
    monkeypatch.setattr(ops, "_set_phase", Mock())
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(
        ops,
        "get_settings",
        lambda: {"backup": {"maxBackups": 1}, "watchdog": {"gracePeriodMin": 5}},
    )

    def create(bdir, ddir):
        dest = Path(bdir) / "pz-backup-20261010-030000.tar.gz"
        with tarfile.open(dest, "w:gz") as archive:
            archive.add(ddir, arcname=".")
        return dest.name, str(dest), dest.stat().st_size

    monkeypatch.setattr(ops, "_create_backup_archive", create)
    return tmp_path, cfg


@pytest.fixture
def s3_client(monkeypatch):
    backups3.invalidate()

    class Client:
        stored = b""
        keys = []
        upload_error = None
        read_error = None
        corrupt = False
        wrong_size = False
        missing_checksum = False
        pages = []
        body = None
        modified = datetime(2026, 10, 10, 3, tzinfo=timezone.utc)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def close(self):
            pass

        def upload_file(self, path, bucket, key, **kwargs):
            if self.upload_error:
                raise self.upload_error
            self.stored = Path(path).read_bytes()
            self.keys.append(key)
            assert (
                kwargs["ExtraArgs"]["Metadata"]["sha256"] == hashlib.sha256(self.stored).hexdigest()
            )
            kwargs["Callback"](len(self.stored))

        def get_object(self, **kwargs):
            if self.read_error:
                raise self.read_error
            stored = b"x" * len(self.stored) if self.corrupt else self.stored
            self.body = io.BytesIO(stored)
            return {
                "ContentLength": len(stored) + int(self.wrong_size),
                "ETag": '"' + hashlib.md5(self.stored).hexdigest() + '"',
                "LastModified": self.modified,
                "Body": self.body,
                "Metadata": {}
                if self.missing_checksum
                else {"sha256": hashlib.sha256(self.stored).hexdigest()},
            }

        def get_paginator(self, operation):
            return Mock(paginate=Mock(return_value=self.pages))

        def delete_object(self, **kwargs):
            if self.upload_error:
                raise self.upload_error
            self.deleted = kwargs

    client = Client()
    monkeypatch.setattr(backups3.boto3, "client", Mock(return_value=client))
    return client


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize(
    "error", [NoCredentialsError(), PartialCredentialsError(provider="env", cred_var="secret_key")]
)
def test_missing_s3_credentials_return_json_without_breaking_overview(
    api, backup_env, s3_client, monkeypatch, language, error
):
    paginator = Mock(side_effect=error)
    monkeypatch.setattr(s3_client, "get_paginator", paginator)
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: False)
    monkeypatch.setattr(ops, "container_state", lambda: None)
    monkeypatch.setattr(ops, "local_digest_cached", lambda **kwargs: None)
    headers = {"x-pz-language": language}
    status, overview = api("GET", "/api/overview", headers=headers)
    assert status == 200 and overview["ok"] and overview["backupsCount"] is None
    paginator.assert_not_called()
    for _ in range(2):
        status, catalog = api("GET", "/api/backups", headers=headers)
        assert status == 200 and not catalog["ok"]
        assert "AWS_SECRET_ACCESS_KEY" in catalog["error"]
        assert type(error).__name__ in catalog["error"]
        assert (
            "Check AWS_ACCESS_KEY_ID" if language == "en" else "Проверьте AWS_ACCESS_KEY_ID"
        ) in catalog["error"]
    assert paginator.call_count == 1
    token = i18n.LANGUAGE.set(language)
    try:
        frame = payloads.StreamCache().frame("backups", 10).decode()
    finally:
        i18n.LANGUAGE.reset(token)
    assert json.loads(frame.split("data: ", 1)[1])["error"] == catalog["error"]
    status, overview = api("GET", "/api/overview", headers=headers)
    assert status == 200 and overview["ok"] and overview["backupsCount"] is None
    assert paginator.call_count == 1


def test_s3_catalog_recovers_after_failed_cache_expires(backup_env, s3_client, monkeypatch):
    _, cfg = backup_env
    clock = [10.0]
    monkeypatch.setattr(backups3.time, "monotonic", lambda: clock[0])
    paginator = Mock(side_effect=[NoCredentialsError(), Mock(paginate=Mock(return_value=[]))])
    monkeypatch.setattr(s3_client, "get_paginator", paginator)
    for _ in range(2):
        with pytest.raises(ops.OpsError, match="AWS_SECRET_ACCESS_KEY"):
            backups3.list_archives(cfg)
    assert paginator.call_count == 1 and ops.count_backups() is None
    clock[0] += 30
    assert backups3.list_archives(cfg) == []
    assert paginator.call_count == 2 and ops.count_backups() == 0
    clock[0] += 30
    assert ops.count_backups() is None
    assert paginator.call_count == 2


def test_s3_count_does_not_wait_for_slow_catalog_refresh(backup_env, s3_client, monkeypatch):
    _, cfg = backup_env
    entered, release = threading.Event(), threading.Event()

    def paginate(**kwargs):
        entered.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(s3_client, "get_paginator", lambda operation: Mock(paginate=paginate))
    with ThreadPoolExecutor(max_workers=2) as executor:
        refresh = executor.submit(backups3.list_archives, cfg)
        try:
            assert entered.wait(2)
            assert executor.submit(ops.count_backups).result(timeout=2) is None
        finally:
            release.set()
        assert refresh.result(timeout=2) == []
    assert ops.count_backups() == 0


def test_s3_count_merges_fresh_local_files_without_duplicate_names(backup_env, s3_client):
    _, cfg = backup_env
    name = "pz-backup-20261010-030000-" + "a" * 32 + ".tar.gz"
    s3_client.pages = [
        {
            "Contents": [
                {"Key": "server-one/" + name, "Size": 7, "LastModified": datetime.now(timezone.utc)}
            ]
        }
    ]
    backups3.list_archives(cfg)
    local = Path(cfg["backup_dir"])
    local.mkdir()
    (local / name).write_bytes(b"archive")
    assert ops.count_backups() == 1
    extra = local / "retained-local.tar.gz"
    extra.write_bytes(b"retained")
    assert ops.count_backups() == 2
    extra.unlink()
    assert ops.count_backups() == 1


@pytest.mark.parametrize("trigger", ["manual", "scheduled", "update", "config"])
def test_export_verifies_before_upload_and_deletes_after_readback(
    backup_env, s3_client, monkeypatch, trigger
):
    root, cfg = backup_env
    order = []
    verify = ops.verify_backup
    upload = s3_client.upload_file
    read = s3_client.get_object
    remove = backups3.os.remove

    def verified(name):
        result = verify(name)
        assert result["hasServerIni"] and result["hasMapData"]
        order.append("verified")
        return result

    def uploaded(*args, **kwargs):
        assert order == ["verified"]
        order.append("uploaded")
        return upload(*args, **kwargs)

    def readback(**kwargs):
        assert list((root / "backups").glob("*.tar.gz"))
        order.append("readback")
        return read(**kwargs)

    def removed(path):
        assert s3_client.body.closed
        order.append("deleted")
        remove(path)

    monkeypatch.setattr(ops, "verify_backup", verified)
    monkeypatch.setattr(s3_client, "upload_file", uploaded)
    monkeypatch.setattr(s3_client, "get_object", readback)
    monkeypatch.setattr(backups3.os, "remove", removed)
    result = ops.run_backup_job(trigger, False)
    assert order == ["verified", "uploaded", "readback", "deleted"]
    assert result["storage"] == "s3"
    assert result["path"].startswith("s3://test-backups/server-one/")
    assert not list((root / "backups").glob("*.tar.gz"))
    assert not list((root / "panel").glob("verify-tmp-*"))
    (record,) = ops.get_backup_journal()
    assert record["path"] == result["path"] and record["status"] == "success"
    assert record["trigger"] == trigger and record["name"] == result["name"]


@pytest.mark.parametrize("broken", ["gzip", "crc", "unsafe", "empty"])
def test_invalid_archive_never_contacts_s3(backup_env, s3_client, monkeypatch, broken):
    root, cfg = backup_env
    original = ops._create_backup_archive

    def create(bdir, ddir):
        name, path, size = original(bdir, ddir)
        if broken == "gzip":
            Path(path).write_bytes(b"broken")
        elif broken == "crc":
            blob = bytearray(Path(path).read_bytes())
            blob[-8] ^= 255
            Path(path).write_bytes(blob)
        else:
            with tarfile.open(path, "w:gz") as archive:
                if broken == "unsafe":
                    member = tarfile.TarInfo("../escape")
                    member.size = 4
                    archive.addfile(member, io.BytesIO(b"evil"))
        return name, path, Path(path).stat().st_size

    monkeypatch.setattr(ops, "_create_backup_archive", create)
    with pytest.raises(ops.OpsErrorReported):
        ops.run_backup_job("scheduled", False)
    backups3.boto3.client.assert_not_called()
    assert len(list((root / "backups").glob("*.tar.gz"))) == 1
    (record,) = ops.get_backup_journal()
    assert record["status"] == "error" and record["name"]
    assert Path(record["path"]).exists()
    assert not (root / "escape").exists()


@pytest.mark.parametrize(
    "failure", ["upload", "credentials", "read", "checksum", "metadata", "size", "delete"]
)
def test_export_failures_keep_current_and_previous_archives(
    backup_env, s3_client, monkeypatch, failure
):
    root, cfg = backup_env
    previous = root / "backups/previous.tar.gz"
    previous.parent.mkdir()
    previous.write_bytes(b"previous failed export")
    secret_error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "secret-access-token"}}, "PutObject"
    )
    if failure == "upload":
        s3_client.upload_error = secret_error
    elif failure == "credentials":
        s3_client.upload_error = NoCredentialsError()
    elif failure == "read":
        s3_client.read_error = secret_error
    elif failure == "checksum":
        s3_client.corrupt = True
    elif failure == "metadata":
        s3_client.missing_checksum = True
    elif failure == "size":
        s3_client.wrong_size = True
    else:
        monkeypatch.setattr(backups3.os, "remove", Mock(side_effect=PermissionError("locked")))
    with pytest.raises(ops.OpsErrorReported) as caught:
        ops.run_backup_job("scheduled", False)
    assert "secret-access-token" not in str(caught.value)
    assert len(list(previous.parent.glob("*.tar.gz"))) == 2
    assert previous.read_bytes() == b"previous failed export"
    (record,) = ops.get_backup_journal()
    assert record["status"] == "error" and Path(record["path"]).exists()
    assert "secret-access-token" not in json.dumps(record)
    errors = [call for call in ops.log_event.call_args_list if call.args[0] == "error"]
    assert len(errors) == 1
    if s3_client.body:
        assert s3_client.body.closed


def test_local_mode_keeps_archive_and_retention_without_s3(backup_env, s3_client, monkeypatch):
    root, cfg = backup_env
    cfg["backup_s3_enabled"] = False
    cfg["backup_s3_bucket"] = ""
    verify = Mock(side_effect=AssertionError("Local backups do not require verification"))
    monkeypatch.setattr(ops, "verify_backup", verify)
    prune = Mock(return_value=0)
    monkeypatch.setattr(ops, "_prune_backups", prune)
    result = ops.run_backup_job("manual", False)
    assert result["storage"] == "local" and Path(result["path"]).exists()
    verify.assert_not_called()
    prune.assert_called_once_with(1)
    backups3.boto3.client.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("backup_s3_bucket", ""),
        ("backup_s3_bucket", "Bad bucket!"),
        ("backup_s3_endpoint", "https://user:secret@example.test"),
        ("backup_s3_endpoint", "https://example.test/?token=secret"),
        ("backup_s3_endpoint", "https://example.test:bad"),
        ("backup_s3_endpoint", "ftp://example.test"),
        ("backup_s3_addressing_style", "invalid"),
    ],
)
def test_bad_configuration_fails_before_server_operations(backup_env, monkeypatch, field, value):
    root, cfg = backup_env
    cfg[field] = value
    running = Mock()
    monkeypatch.setattr(ops, "is_running", running)
    with pytest.raises(ops.OpsErrorReported):
        ops.run_backup_job("manual", True)
    running.assert_not_called()
    assert not (root / "backups").exists()


def test_success_does_not_prune_failed_exports_and_object_keys_are_unique(backup_env, s3_client):
    root, cfg = backup_env
    previous = root / "backups/previous.tar.gz"
    previous.parent.mkdir()
    previous.write_bytes(b"keep")
    first = ops.run_backup_job("manual", False)
    second = ops.run_backup_job("manual", False)
    assert first["name"] != second["name"]
    assert first["path"] != second["path"]
    assert s3_client.keys[0] != s3_client.keys[1]
    assert previous.read_bytes() == b"keep"


def test_stopped_backup_restarts_before_export(backup_env, s3_client, monkeypatch):
    order = []
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", lambda *args: None)
    monkeypatch.setattr(ops, "graceful_stop", lambda *args: "stopped")
    monkeypatch.setattr(ops, "_start_container", lambda: (order.append("restarted") or 0, "", ""))
    monkeypatch.setattr(ops, "wait_until_running", lambda *args: True)
    monkeypatch.setattr(ops, "wait_until_ready", lambda: True)
    original = ops.verify_backup

    def verify(name):
        assert order == ["restarted"]
        return original(name)

    monkeypatch.setattr(ops, "verify_backup", verify)
    ops.run_backup_job("manual", True)
    assert s3_client.keys


def test_deadline_retains_local_archive(backup_env, s3_client, monkeypatch):
    root, cfg = backup_env
    ticks = iter([0, 2401])
    monkeypatch.setattr(backups3.time, "monotonic", lambda: next(ticks))
    path = root / "pz-backup-20261010-030000.tar.gz"
    path.write_bytes(b"bytes")
    with pytest.raises(ops.OpsError, match="2400"):
        backups3.upload_verified(str(path), cfg, Mock())
    assert path.exists() and not s3_client.keys


@pytest.mark.parametrize("multipart", [False, True])
def test_real_sdk_upload_and_readback_on_loopback(tmp_path, monkeypatch, multipart):
    """Exercise signing, request bodies and multipart completion without external S3."""
    objects, parts, methods = {}, {}, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, data=b"", **headers):
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            parsed = urlsplit(self.path)
            body = self.rfile.read(int(self.headers["Content-Length"]))
            methods.append("PUT")
            query = parse_qs(parsed.query)
            if "uploadId" in query:
                parts[int(query["partNumber"][0])] = body
            else:
                objects[parsed.path] = body
            self.respond(ETag='"test-etag"')

        def do_POST(self):
            parsed = urlsplit(self.path)
            methods.append("POST")
            if parsed.query == "uploads":
                self.respond(
                    b"<InitiateMultipartUploadResult><UploadId>test-upload</UploadId></InitiateMultipartUploadResult>"
                )
            else:
                self.rfile.read(int(self.headers["Content-Length"]))
                objects[parsed.path] = b"".join(parts[key] for key in sorted(parts))
                self.respond(
                    b'<CompleteMultipartUploadResult><ETag>"completed"</ETag></CompleteMultipartUploadResult>'
                )

        def do_GET(self):
            methods.append("GET")
            content = objects[urlsplit(self.path).path]
            self.respond(
                content,
                **{
                    "x-amz-meta-sha256": hashlib.sha256(content).hexdigest(),
                    "ETag": '"test-object"',
                    "Last-Modified": "Sat, 10 Oct 2026 03:00:00 GMT",
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fictional-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fictional-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "fictional-session")
    path = tmp_path / "pz-backup-20261010-030000.tar.gz"
    content = os.urandom(9 * 1024 * 1024) if multipart else b"verified archive bytes"
    path.write_bytes(content)
    cfg = {
        "backup_s3_enabled": True,
        "backup_s3_bucket": "test-backups",
        "backup_s3_prefix": "",
        "backup_s3_endpoint": f"http://127.0.0.1:{server.server_port}",
        "backup_s3_addressing_style": "path",
        "dashboard_dir": str(tmp_path / "panel"),
    }
    try:
        verification = {
            "files": 1,
            "totalSize": len(content),
            "hasServerIni": True,
            "hasMapData": True,
            "identity": backupverify.local_identity(path),
        }
        location = backups3.upload_verified(str(path), cfg, Mock(), verification=verification)
        assert location.startswith("s3://test-backups/pz-backup-20261010-030000-")
        assert list(objects.values()) == [content]
        assert not path.exists()
        assert methods[-1] == "GET"
        assert bool(parts) is multipart
        downloaded = tmp_path / "downloaded.tar.gz"
        # Fetch the actual exported object through the same streaming SDK path as restore.
        remote_name = location.rsplit("/", 1)[-1]
        identity = backups3.download(remote_name, str(downloaded), cfg, Mock())
        assert downloaded.read_bytes() == content
        assert backupverify.lookup(remote_name, "s3", identity, cfg, backupverify.receipts(cfg))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_s3_errors_and_phases_have_english_presentation():
    token = i18n.LANGUAGE.set("en")
    try:
        result = i18n.present(
            {
                "phase": "Выгрузка бэкапа в S3",
                "error": "Бэкап (по расписанию) не удался: S3: выгрузка не завершена, локальная копия сохранена (ClientError)",
            }
        )
        assert result["phase"] == "Uploading backup to S3"
        assert "local copy retained" in result["error"]
        assert "по расписанию" not in result["error"]
    finally:
        i18n.LANGUAGE.reset(token)


def test_restore_downloads_and_checks_s3_before_stopping_or_replacing_world(
    backup_env, s3_client, monkeypatch
):
    root, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    ini = root / "data/Server/test.ini"
    ini.write_text("changed world", encoding="utf-8")
    order = []
    get_object = s3_client.get_object
    extract = ops._extract_backup

    def fetch(**kwargs):
        order.append("download")
        assert kwargs["Key"] == s3_client.keys[0]
        return get_object(**kwargs)

    def verify(path, dest):
        assert Path(path).is_file()
        extract(path, dest)
        order.append("verified")

    def stop(*args):
        assert order == ["download", "verified"]
        assert ini.read_text(encoding="utf-8") == "changed world"
        order.append("stopped")
        return "stopped"

    monkeypatch.setattr(s3_client, "get_object", fetch)
    monkeypatch.setattr(ops, "_extract_backup", verify)
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "rcon_warn_broadcast", Mock())
    monkeypatch.setattr(ops, "graceful_stop", stop)
    monkeypatch.setattr(ops, "container_state", lambda: {"running": False})
    monkeypatch.setattr(ops, "_start_container", lambda: (0, "", ""))
    monkeypatch.setattr(ops, "wait_until_running", lambda *args: True)
    monkeypatch.setattr(ops, "_require_ready", lambda: order.append("ready"))
    ops._do_restore(exported["name"])
    assert order == ["download", "verified", "stopped", "ready"]
    assert ini.read_text(encoding="utf-8") == "Mods=\n"
    assert (root / "data/Maps/map.bin").read_bytes() == b"world"
    assert not list((root / "backups").iterdir())
    assert not list((root / "panel").glob("restore-tmp-*"))
    assert s3_client.stored and s3_client.body.closed


@pytest.mark.parametrize(
    "failure", ["network", "size", "oversize", "checksum", "metadata", "gzip", "unsafe"]
)
def test_failed_s3_restore_never_stops_server_or_touches_world(
    backup_env, s3_client, monkeypatch, failure
):
    root, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    sentinel = root / "data/Server/test.ini"
    sentinel.write_text("live world", encoding="utf-8")
    if failure == "network":
        s3_client.read_error = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "secret"}}, "GetObject"
        )
    elif failure == "size":
        s3_client.wrong_size = True
    elif failure == "oversize":
        s3_client.wrong_size = -1
    elif failure == "checksum":
        s3_client.corrupt = True
    elif failure == "metadata":
        s3_client.missing_checksum = True
    elif failure == "gzip":
        s3_client.stored = b"not gzip"
    else:
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w:gz") as archive:
            member = tarfile.TarInfo("../escape")
            member.size = 4
            archive.addfile(member, io.BytesIO(b"evil"))
        s3_client.stored = blob.getvalue()
    prepare = Mock()
    monkeypatch.setattr(ops, "_restore_prepared", prepare)
    with pytest.raises(ops.OpsError) as caught:
        ops._do_restore(exported["name"])
    assert "secret" not in str(caught.value)
    prepare.assert_not_called()
    assert sentinel.read_text(encoding="utf-8") == "live world"
    assert not list((root / "backups").iterdir())
    assert not list((root / "panel").glob("restore-tmp-*"))
    assert not (root / "escape").exists()


def test_s3_download_cleanup_on_restore_failure_and_standalone_verification(
    backup_env, s3_client, monkeypatch
):
    root, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    verified = ops.verify_backup(exported["name"])
    assert verified["hasServerIni"] and verified["hasMapData"]
    assert not list((root / "backups").iterdir())
    monkeypatch.setattr(ops, "_restore_prepared", Mock(side_effect=ops.OpsError("restore failed")))
    with pytest.raises(ops.OpsError, match="restore failed"):
        ops._do_restore(exported["name"])
    assert not list((root / "backups").iterdir())


def test_s3_catalog_pagination_namespace_cache_and_delete(backup_env, s3_client):
    root, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    name = exported["name"]
    obj = {"Key": "server-one/" + name, "Size": 123, "LastModified": datetime.now(timezone.utc)}
    s3_client.pages = [
        {"Contents": [obj]},
        {
            "Contents": [
                dict(obj, Key="server-one/nested/" + name),
                dict(obj, Key="other/" + name),
                dict(obj, Key="server-one/unrelated.tar.gz"),
            ]
        },
    ]
    items = ops.list_backups()
    assert len(items) == ops.count_backups() == 1
    assert items[0]["name"] == name and items[0]["storage"] == "s3"
    items[0]["name"] = "mutated"
    assert ops.list_backups()[0]["name"] == name
    ops.delete_backup(name)
    assert s3_client.deleted == {"Bucket": "test-backups", "Key": "server-one/" + name}
    s3_client.pages = []
    assert ops.list_backups() == []


@pytest.mark.parametrize(
    "name", ["../evil.tar.gz", "nested/evil.tar.gz", "s3://other/evil.tar.gz", "evil.tar.gz"]
)
def test_restore_rejects_names_outside_owned_prefix_without_network(backup_env, s3_client, name):
    with pytest.raises(ops.OpsError):
        ops._do_restore(name)
    backups3.boto3.client.assert_not_called()


def test_browser_download_uses_temporary_s3_copy_and_cleans_up(backup_env, s3_client, monkeypatch):
    import app

    root, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    handler = object.__new__(app.Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler._send_error_json = Mock()
    ops._set_phase.reset_mock()
    handler._serve_backup(exported["name"])
    handler.send_response.assert_called_once_with(200)
    assert handler.wfile.getvalue() == s3_client.stored
    assert not list((root / "backups").iterdir())
    ops._set_phase.assert_not_called()


def test_interrupted_stream_is_cleaned_up_before_restore(backup_env, s3_client, monkeypatch):
    root, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    content = s3_client.stored

    class InterruptedBody(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise OSError("stream interrupted")
            return super().read(20)

    body = InterruptedBody(content)
    monkeypatch.setattr(
        s3_client,
        "get_object",
        lambda **kwargs: {
            "Body": body,
            "ContentLength": len(content),
            "Metadata": {"sha256": hashlib.sha256(content).hexdigest()},
        },
    )
    prepare = Mock()
    monkeypatch.setattr(ops, "_restore_prepared", prepare)
    with pytest.raises(ops.OpsError, match="OSError"):
        ops._do_restore(exported["name"])
    prepare.assert_not_called()
    assert body.closed and not list((root / "backups").iterdir())


def test_delete_failure_retains_remote_catalog_entry(backup_env, s3_client):
    exported = ops.run_backup_job("manual", False)
    s3_client.upload_error = NoCredentialsError()
    with pytest.raises(ops.OpsError, match="NoCredentialsError"):
        ops.delete_backup(exported["name"])
    assert not hasattr(s3_client, "deleted")


def remote_catalog(s3_client, name, cfg):
    response = s3_client.get_object()
    response["Body"].close()
    s3_client.pages = [
        {
            "Contents": [
                {
                    "Key": cfg["backup_s3_prefix"].strip("/") + "/" + name,
                    "Size": response["ContentLength"],
                    "ETag": response["ETag"],
                    "LastModified": response["LastModified"],
                }
            ]
        }
    ]
    backups3.invalidate()


def test_export_receipt_survives_cache_reset_and_does_not_require_another_download(
    backup_env, s3_client, monkeypatch
):
    root, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    remote_catalog(s3_client, exported["name"], cfg)
    monkeypatch.setattr(s3_client, "get_object", Mock(side_effect=AssertionError("no readback")))
    receipt = ops.list_backups()[0]["verification"]
    assert receipt["files"] == 2 and receipt["hasMapData"] and receipt["hasServerIni"]
    assert receipt["verifiedAt"]
    assert (root / "panel/backup-verifications.json").is_file()
    backups3.invalidate()
    assert ops.list_backups()[0]["verification"] == receipt


@pytest.mark.parametrize("change", ["etag", "size", "mtime", "endpoint", "region", "missing-etag"])
def test_remote_receipt_does_not_trust_changed_objects_or_storage(backup_env, s3_client, change):
    _, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    remote_catalog(s3_client, exported["name"], cfg)
    assert ops.list_backups()[0]["verification"]
    obj = s3_client.pages[0]["Contents"][0]
    if change == "etag":
        obj["ETag"] = '"changed"'
    elif change == "size":
        obj["Size"] += 1
    elif change == "mtime":
        obj["LastModified"] = datetime(2026, 10, 11, tzinfo=timezone.utc)
    elif change == "missing-etag":
        del obj["ETag"]
    else:
        cfg["backup_s3_" + change] = (
            "https://other.example.test" if change == "endpoint" else "other"
        )
    backups3.invalidate()
    assert ops.list_backups()[0]["verification"] is None


def test_old_remote_backup_can_be_checked_once_and_failed_recheck_revokes_receipt(
    backup_env, s3_client
):
    _, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    backupverify.forget(exported["name"], "s3", cfg)
    remote_catalog(s3_client, exported["name"], cfg)
    assert ops.list_backups()[0]["verification"] is None
    ops.verify_backup(exported["name"])
    assert ops.list_backups()[0]["verification"]
    s3_client.corrupt = True
    with pytest.raises(ops.OpsError, match="SHA-256"):
        ops.verify_backup(exported["name"])
    assert ops.list_backups()[0]["verification"] is None


def test_local_receipt_survives_history_and_is_revoked_after_same_size_replacement(
    backup_env, s3_client
):
    _, cfg = backup_env
    cfg["backup_s3_enabled"] = False
    exported = ops.run_backup_job("manual", False)
    assert ops.list_backups()[0]["verification"] is None
    ops.verify_backup(exported["name"])
    assert ops.list_backups()[0]["verification"]["files"] == 2
    original = Path(exported["path"])
    stat = original.stat()
    replacement = original.with_suffix(".replacement")
    replacement.write_bytes(b"x" * stat.st_size)
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(replacement, original)
    assert ops.list_backups()[0]["verification"] is None


@pytest.mark.parametrize("damage", ["broken json", "[]", "{}"])
def test_missing_or_damaged_receipts_do_not_break_catalog(backup_env, s3_client, damage):
    root, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    remote_catalog(s3_client, exported["name"], cfg)
    (root / "panel/backup-verifications.json").write_text(damage, encoding="utf-8")
    assert ops.list_backups()[0]["verification"] is None


def test_receipt_write_failure_retains_local_backup(backup_env, s3_client, monkeypatch):
    root, cfg = backup_env
    remember = backupverify.remember

    def fail_remote(name, storage, *args):
        if storage == "s3":
            raise OSError("fictional private path")
        remember(name, storage, *args)

    monkeypatch.setattr(backupverify, "remember", fail_remote)
    with pytest.raises(ops.OpsErrorReported, match="локальная копия сохранена") as caught:
        ops.run_backup_job("manual", False)
    assert "fictional private path" not in str(caught.value)
    assert len(list((root / "backups").glob("*.tar.gz"))) == 1
    remote_catalog(s3_client, s3_client.keys[0].rsplit("/", 1)[-1], cfg)
    items = ops.list_backups()
    assert next(item for item in items if item["storage"] == "local")["verification"]
    assert next(item for item in items if item["storage"] == "s3")["verification"] is None


def test_changed_local_archive_is_not_exported_as_verified(backup_env, s3_client, monkeypatch):
    root, cfg = backup_env
    original = ops.verify_backup

    def changed(name):
        result = original(name)
        (root / "backups" / name).write_bytes(b"changed after archive verification")
        return result

    monkeypatch.setattr(ops, "verify_backup", changed)
    with pytest.raises(ops.OpsErrorReported, match="изменился"):
        ops.run_backup_job("manual", False)
    assert not s3_client.keys and list((root / "backups").glob("*.tar.gz"))


@pytest.mark.parametrize("prefix", ["/server-one/", "server-one", ""])
def test_upload_and_download_use_the_same_normalized_prefix(backup_env, s3_client, prefix):
    _, cfg = backup_env
    cfg["backup_s3_prefix"] = prefix
    exported = ops.run_backup_job("manual", False)
    assert s3_client.keys == [backups3._key(exported["name"], cfg)]
    assert ops.verify_backup(exported["name"])["hasMapData"]


@pytest.mark.parametrize("language", ["en", "ru"])
def test_s3_outage_keeps_retained_local_archives_accessible(
    backup_env, s3_client, monkeypatch, language
):
    root, cfg = backup_env
    local = root / "backups/retained.tar.gz"
    local.parent.mkdir()
    local.write_bytes(b"retained archive")
    monkeypatch.setattr(s3_client, "get_paginator", Mock(side_effect=NoCredentialsError()))
    monkeypatch.setattr(ops, "auto_backup_state", lambda: {})
    payload = payloads.backups_payload()
    assert payload["ok"] and payload["items"][0]["name"] == local.name
    assert "AWS_SECRET_ACCESS_KEY" in payload["storageError"]
    token = i18n.LANGUAGE.set(language)
    try:
        presented = i18n.present(payload)
    finally:
        i18n.LANGUAGE.reset(token)
    assert (
        "Check AWS_ACCESS_KEY_ID" if language == "en" else "Проверьте AWS_ACCESS_KEY_ID"
    ) in presented["storageError"]


def test_catalog_identity_cannot_be_mutated_through_returned_snapshots(backup_env, s3_client):
    _, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    remote_catalog(s3_client, exported["name"], cfg)
    catalog = backups3.list_archives(cfg)
    original = dict(catalog[0]["identity"])
    catalog[0]["identity"]["etag"] = '"changed"'
    snapshot = backups3.cached_archives(cfg)
    assert snapshot[0]["identity"] == original
    snapshot[0]["identity"]["etag"] = '"changed-again"'
    assert backups3.list_archives(cfg)[0]["identity"] == original


def test_receipt_cleanup_failure_does_not_report_deleted_archive_as_failed(
    backup_env, s3_client, monkeypatch
):
    _, cfg = backup_env
    exported = ops.run_backup_job("manual", False)
    monkeypatch.setattr(backupverify, "forget", Mock(side_effect=PermissionError("private path")))
    ops.delete_backup(exported["name"])
    assert s3_client.deleted["Key"] == s3_client.keys[0]
    ops.log_event.assert_any_call("backup-delete", f"Бэкап удалён: {exported['name']}")
    assert "private path" not in str(ops.log_event.call_args_list)
