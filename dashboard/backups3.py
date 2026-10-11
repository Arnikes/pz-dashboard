"""Optional S3 export with read-back confirmation before local deletion."""

import hashlib
import os
import re
import time
import threading
import uuid
from contextlib import closing
from urllib.parse import urlsplit

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from boto3.exceptions import S3UploadFailedError

from errors import OpsError

ARCHIVE_NAME = re.compile(r"pz-backup-\d{8}-\d{6}(?:-\d+)?-[0-9a-f]{32}\.tar\.gz")
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _client(cfg):
    return closing(
        boto3.client(
            "s3",
            endpoint_url=cfg.get("backup_s3_endpoint") or None,
            region_name=cfg.get("backup_s3_region") or "us-east-1",
            config=Config(
                connect_timeout=10,
                read_timeout=60,
                retries={"mode": "standard", "total_max_attempts": 3},
                s3={"addressing_style": cfg.get("backup_s3_addressing_style", "auto")},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
    )


def _prefix(cfg):
    prefix = cfg.get("backup_s3_prefix", "pz-backups").strip("/")
    return prefix + "/" if prefix else ""


def invalidate():
    with _CACHE_LOCK:
        _CACHE.clear()


def list_archives(cfg):
    """Enumerate only this panel's archive namespace, caching monitoring reads."""
    validate(cfg)
    signature = tuple(
        cfg.get(key)
        for key in (
            "backup_s3_bucket",
            "backup_s3_prefix",
            "backup_s3_endpoint",
            "backup_s3_region",
            "backup_s3_addressing_style",
        )
    )
    with _CACHE_LOCK:
        cached = _CACHE.get(signature)
        if cached and time.monotonic() - cached[0] < 30:
            return [dict(item) for item in cached[1]]
        items = []
        prefix = _prefix(cfg)
        try:
            with _client(cfg) as client:
                for page in client.get_paginator("list_objects_v2").paginate(
                    Bucket=cfg["backup_s3_bucket"],
                    Prefix=prefix,
                ):
                    for obj in page.get("Contents", []):
                        name = obj["Key"][len(prefix) :]
                        if not obj["Key"].startswith(prefix) or not ARCHIVE_NAME.fullmatch(name):
                            continue
                        items.append(
                            {
                                "name": name,
                                "size": obj["Size"],
                                "mtime": obj["LastModified"].isoformat(),
                                "storage": "s3",
                            }
                        )
        except (BotoCoreError, ClientError, OSError, ValueError) as error:
            raise OpsError(f"S3: список бэкапов недоступен ({type(error).__name__})") from error
        items.sort(key=lambda item: item["mtime"], reverse=True)
        _CACHE[signature] = time.monotonic(), items
        return [dict(item) for item in items]


def _key(name, cfg):
    validate(cfg)
    if not ARCHIVE_NAME.fullmatch(name):
        raise OpsError("Некорректное имя бэкапа S3")
    return _prefix(cfg) + name


def download(name, dest, cfg, phase):
    """Download privately; size and stored SHA-256 must match before returning."""
    key = _key(name, cfg)
    phase("Скачивание бэкапа из S3", name)
    deadline = time.monotonic() + 2400
    try:
        with _client(cfg) as client:
            response = client.get_object(Bucket=cfg["backup_s3_bucket"], Key=key)
            digest = hashlib.sha256()
            received = 0
            with closing(response["Body"]) as body:
                expected = response.get("Metadata", {}).get("sha256", "")
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise OpsError("S3: у бэкапа отсутствует контрольная сумма SHA-256")
                with open(dest, "xb") as archive:
                    while chunk := body.read(1024 * 1024):
                        if time.monotonic() >= deadline:
                            raise OpsError("S3: скачивание не уложилось в таймаут (2400 с)")
                        archive.write(chunk)
                        received += len(chunk)
                        digest.update(chunk)
            if time.monotonic() >= deadline:
                raise OpsError("S3: скачивание не уложилось в таймаут (2400 с)")
            if received != response["ContentLength"] or digest.hexdigest() != expected:
                raise OpsError("S3: скачанный бэкап не прошёл проверку SHA-256")
    except (BotoCoreError, ClientError, OSError, ValueError) as error:
        raise OpsError(f"S3: скачивание бэкапа не удалось ({type(error).__name__})") from error


def delete(name, cfg):
    key = _key(name, cfg)
    try:
        with _client(cfg) as client:
            client.delete_object(Bucket=cfg["backup_s3_bucket"], Key=key)
    except (BotoCoreError, ClientError, OSError, ValueError) as error:
        raise OpsError(f"S3: удаление бэкапа не удалось ({type(error).__name__})") from error
    invalidate()


def validate(cfg):
    """Reject incomplete configuration before saving/stopping the game server."""
    if not cfg.get("backup_s3_enabled", False):
        return
    bucket = cfg.get("backup_s3_bucket", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise OpsError("S3: задайте корректный BACKUP_S3_BUCKET")
    if cfg.get("backup_s3_addressing_style", "auto") not in ("auto", "path", "virtual"):
        raise OpsError("S3: BACKUP_S3_ADDRESSING_STYLE должен быть auto, path или virtual")
    endpoint = cfg.get("backup_s3_endpoint", "")
    if endpoint:
        try:
            parsed = urlsplit(endpoint)
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise OpsError("S3: BACKUP_S3_ENDPOINT_URL должен быть URL без пароля и параметров")


def upload_verified(path, cfg, phase):
    """Upload a previously verified archive; leave it intact on every failure.

    UUID keys prevent overwriting earlier exports after the local file is removed.
    Read-back uses bounded memory and checks actual bytes, including multipart files.
    Credentials use the SDK chain and never enter settings, results or diagnostics.
    """
    validate(cfg)
    bucket = cfg["backup_s3_bucket"]
    name = os.path.basename(path)
    object_name = f"{name[:-7]}-{uuid.uuid4().hex}.tar.gz"
    key = "/".join(filter(None, (cfg.get("backup_s3_prefix", "pz-backups"), object_name)))
    location = f"s3://{bucket}/{key}"
    deadline = time.monotonic() + 2400

    def check_deadline(_bytes=0):
        if time.monotonic() >= deadline:
            raise OpsError("S3: загрузка и проверка не уложились в таймаут (2400 с)")

    try:
        digest = hashlib.sha256()
        with open(path, "rb") as archive:
            while chunk := archive.read(1024 * 1024):
                check_deadline()
                digest.update(chunk)
        size = os.path.getsize(path)
        with _client(cfg) as client:
            phase("Выгрузка бэкапа в S3", name)
            client.upload_file(
                path,
                bucket,
                key,
                ExtraArgs={
                    "ContentType": "application/gzip",
                    "Metadata": {"sha256": digest.hexdigest()},
                },
                Callback=check_deadline,
                Config=TransferConfig(use_threads=False, preferred_transfer_client="classic"),
            )
            check_deadline()
            phase("Проверка загруженного объекта S3", name)
            response = client.get_object(Bucket=bucket, Key=key)
            remote_digest = hashlib.sha256()
            received = 0
            with closing(response["Body"]) as body:
                if response["ContentLength"] != size:
                    raise OpsError(
                        "S3: размер загруженного бэкапа не совпадает, локальная копия сохранена"
                    )
                while chunk := body.read(1024 * 1024):
                    check_deadline()
                    received += len(chunk)
                    remote_digest.update(chunk)
            check_deadline()
            if received != size or remote_digest.digest() != digest.digest():
                raise OpsError(
                    "S3: контрольная сумма бэкапа не совпадает, локальная копия сохранена"
                )
    except (BotoCoreError, ClientError, S3UploadFailedError, OSError, ValueError) as error:
        # SDK errors may include endpoint URLs or credentials. Report only the type.
        raise OpsError(
            f"S3: выгрузка не завершена, локальная копия сохранена ({type(error).__name__})"
        ) from error
    invalidate()
    phase("Удаление локальной копии бэкапа", name)
    try:
        os.remove(path)
    except OSError as error:
        raise OpsError(
            "S3: бэкап загружен и проверен, но локальную копию удалить не удалось"
        ) from error
    return location
