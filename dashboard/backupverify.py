"""Persistent verification receipts, bound to an archive's storage and identity."""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import fileio

_LOCK = threading.Lock()


def local_identity(path):
    stat = os.stat(path)
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime_ns,
        "ctime": stat.st_ctime_ns,
        "inode": stat.st_ino,
        "device": stat.st_dev,
    }


def s3_identity(obj):
    """Compare GET and LIST metadata at their shared timestamp precision.

    GET's HTTP Last-Modified header has whole seconds, while LIST's XML may
    include fractions. ETag and size still detect content changes within a second.
    """
    modified = obj.get("LastModified")
    if not obj.get("ETag") or not isinstance(modified, datetime):
        return None
    return {
        "size": obj.get("Size", obj.get("ContentLength")),
        "etag": obj["ETag"],
        "mtime": modified.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


def _key(name, storage, cfg):
    scope = (
        [
            cfg.get("backup_s3_endpoint") or "aws",
            cfg["backup_s3_bucket"],
            cfg.get("backup_s3_region") or "us-east-1",
            cfg.get("backup_s3_prefix", "pz-backups").strip("/"),
        ]
        if storage == "s3"
        else [os.path.realpath(cfg["backup_dir"])]
    )
    return hashlib.sha256(json.dumps([storage, scope, name]).encode()).hexdigest()


def _path(cfg):
    return Path(cfg["dashboard_dir"]) / "backup-verifications.json"


def receipts(cfg):
    """Missing or damaged receipts never confer verified status."""
    with _LOCK:
        try:
            saved = json.loads(_path(cfg).read_text(encoding="utf-8"))
            return saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            return {}


def lookup(name, storage, identity, cfg, saved):
    receipt = saved.get(_key(name, storage, cfg))
    if identity is None or not isinstance(receipt, dict) or receipt.get("identity") != identity:
        return None
    result = receipt.get("result")
    if (
        not isinstance(result, dict)
        or type(result.get("files")) is not int
        or result["files"] <= 0
        or type(result.get("hasServerIni")) is not bool
        or type(result.get("hasMapData")) is not bool
        or not isinstance(result.get("verifiedAt"), str)
    ):
        return None
    try:
        datetime.fromisoformat(result["verifiedAt"])
    except ValueError:
        return None
    return dict(result)


def remember(name, storage, identity, result, cfg):
    if identity is None:
        return
    fields = ("files", "totalSize", "hasServerIni", "hasMapData")
    verified = {key: result[key] for key in fields}
    verified["verifiedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _change(cfg, _key(name, storage, cfg), {"identity": identity, "result": verified})


def forget(name, storage, cfg):
    _change(cfg, _key(name, storage, cfg), None)


def _change(cfg, key, receipt):
    with _LOCK:
        try:
            saved = json.loads(_path(cfg).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        if receipt is None:
            if key not in saved:
                return
            del saved[key]
        else:
            saved[key] = receipt
        fileio.atomic_write(_path(cfg), json.dumps(saved, ensure_ascii=False).encode(), mode=0o600)
