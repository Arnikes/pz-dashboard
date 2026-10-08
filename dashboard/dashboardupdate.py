"""Update the console through a detached helper that survives its replacement."""

import json
from pathlib import Path
import tempfile
import time
import uuid

import config
import dockerlib
from errors import OpsError

_CHECK = {}
_CONTAINER = {"at": 0, "name": None, "value": None}


def inspect(name):
    code, out, _ = dockerlib.sh(["docker", "inspect", name], timeout=30)
    if code:
        return None
    try:
        return json.loads(out)[0]
    except (ValueError, IndexError, TypeError):
        return None


def container_cached():
    name = config.CFG["dashboard_container"]
    if _CONTAINER["name"] != name or time.monotonic() - _CONTAINER["at"] > 60:
        _CONTAINER.update(at=time.monotonic(), name=name, value=inspect(name))
        _CONTAINER["digest"] = (
            dockerlib.image_digests((_CONTAINER["value"] or {}).get("Image"))
            if _CONTAINER["value"]
            else None
        )
    return _CONTAINER["value"]


def state():
    container = container_cached()
    image = (container or {}).get("Config", {}).get("Image")
    local = (container or {}).get("Image")
    labels = (container or {}).get("Config", {}).get("Labels") or {}
    reason = ""
    if not container:
        reason = "Контейнер пульта не найден. Проверьте PZ_DASHBOARD_CONTAINER."
    elif labels.get("com.docker.compose.service") != config.CFG["dashboard_service"]:
        reason = "Сервис пульта не совпадает с Compose. Проверьте PZ_DASHBOARD_SERVICE."
    elif not config.CFG["compose_file"]:
        reason = "Для обновления пульта задайте COMPOSE_FILE и подключите файлы Compose."
    elif not image or "@" in image or not _CONTAINER.get("digest"):
        reason = "Используйте опубликованный образ пульта с тегом в Compose вместо локальной сборки или digest."
    check = _CHECK if _CHECK.get("image") == image and _CHECK.get("local") == local else {}
    return {**check, "image": image, "local": local, "supported": not reason, "note": reason}


def check():
    current = state()
    if not current["supported"]:
        raise OpsError(current["note"])
    code, out, _ = dockerlib.sh(
        ["docker", "manifest", "inspect", "--verbose", current["image"]], timeout=25
    )
    result = {**current, "at": time.time(), "remote": None, "available": None, "error": None}
    try:
        if code:
            raise ValueError
        manifests = json.loads(out)
        if not isinstance(manifests, list):
            manifests = [manifests]
        code, image_out, _ = dockerlib.sh(
            ["docker", "image", "inspect", current["local"]], timeout=30
        )
        if code:
            raise ValueError
        image = json.loads(image_out)[0]
        for manifest in manifests:
            platform = manifest.get("Descriptor", {}).get("platform", {})
            if platform and any(
                platform.get(key, "") != image.get(field, "")
                for key, field in (
                    ("os", "Os"),
                    ("architecture", "Architecture"),
                    ("variant", "Variant"),
                )
            ):
                continue
            schema = manifest.get("SchemaV2Manifest") or manifest.get("OCIManifest") or {}
            digest = schema.get("config", {}).get("digest")
            if digest:
                result.update(remote=digest, available=digest != current["local"])
                break
        if not result["remote"]:
            raise ValueError
    except (ValueError, IndexError, TypeError, AttributeError):
        result["error"] = "Не удалось проверить образ пульта. Проверьте реестр и доступ к образу."
    _CHECK.clear()
    _CHECK.update(result)
    return result


def _path():
    return Path(config.CFG["dashboard_dir"]) / "dashboard-update.json"


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        json.dump(value, f)
        temporary = Path(f.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def operation():
    try:
        result = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if result.get("active") and time.time() - result.get("at", 0) > 1200:
        result.update(
            active=False,
            ok=False,
            message="Обновление пульта не подтверждено. Проверьте контейнер и журнал Docker.",
        )
    return result


def prepare(container):
    """Freeze interpolation and map Compose's container paths to host bind paths."""
    labels = container["Config"].get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    service = config.CFG["dashboard_service"]
    if not project or service == config.CFG["pz_service"]:
        raise OpsError("Не удалось сопоставить контейнер пульта с отдельным сервисом Compose.")
    cfg = {**config.CFG, "compose_project": project}
    code, out, _ = dockerlib.sh(
        dockerlib._compose_prefix(cfg) + ["config", "--format", "json"], timeout=30
    )
    if code:
        raise OpsError(
            "Не удалось прочитать Compose. Проверьте файлы, .env и переменные окружения."
        )
    try:
        model = json.loads(out)
        definition = model["services"][service]
        if definition.get("image") != container["Config"]["Image"]:
            raise OpsError(
                "Образ пульта в Compose отличается от запущенного. Примените конфигурацию на хосте."
            )
        if (
            definition.get("container_name", config.CFG["dashboard_container"])
            != config.CFG["dashboard_container"]
        ):
            raise OpsError("Имя контейнера пульта в Compose отличается от запущенного.")
        mounts = sorted(
            container.get("Mounts", []), key=lambda item: len(item["Destination"]), reverse=True
        )
        for definition in model["services"].values():
            for volume in definition.get("volumes", []):
                if volume.get("type") != "bind":
                    continue
                source = volume["source"]
                for mount in mounts:
                    target = mount["Destination"].rstrip("/")
                    if mount["Type"] == "bind" and (
                        source == target or source.startswith(target + "/")
                    ):
                        volume["source"] = mount["Source"].rstrip("/") + source[len(target) :]
                        break
                else:
                    # Compose can already contain an absolute host path. The
                    # console service's existing binds must match live mounts.
                    live = next((m for m in mounts if m["Destination"] == volume["target"]), None)
                    if definition is model["services"][service] and (
                        not live or source != live["Source"]
                    ):
                        raise OpsError(
                            "Не удалось сопоставить пути Compose с хостом. Проверьте подключения пульта."
                        )
        return model
    except (ValueError, KeyError, TypeError) as error:
        raise OpsError(
            "Не удалось сопоставить контейнер пульта с отдельным сервисом Compose."
        ) from error


def apply(phase):
    current = state()
    if not current["supported"]:
        raise OpsError(current["note"])
    if not dockerlib.compose_version():
        raise OpsError("Недоступен плагин docker compose в контейнере пульта")
    container = inspect(config.CFG["dashboard_container"])
    if not container or container["Image"] != current["local"]:
        raise OpsError("Контейнер пульта изменился. Повторите проверку.")
    model = prepare(container)
    if not any(
        m["RW"]
        and (
            config.CFG["dashboard_dir"] == m["Destination"]
            or config.CFG["dashboard_dir"].startswith(m["Destination"].rstrip("/") + "/")
        )
        for m in container.get("Mounts", [])
    ):
        raise OpsError(
            "Для обновления пульта подключите постоянный каталог DASHBOARD_DIR с правом записи."
        )
    phase("Скачивание нового образа", current["image"])
    code, _, _ = dockerlib.image_pull(current["image"])
    if code:
        raise OpsError("Не удалось скачать образ пульта. Проверьте реестр и доступ к образу.")
    code, out, _ = dockerlib.sh(
        ["docker", "image", "inspect", current["image"], "--format", "{{.Id}}"], timeout=30
    )
    if code or not out.startswith("sha256:"):
        raise OpsError("Не удалось подтвердить скачанный образ пульта.")
    if out == current["local"]:
        phase("Готово", "Образ пульта уже актуален")
        return
    snapshot = _path().with_name(f"dashboard-compose-{uuid.uuid4().hex}.json")
    _write(snapshot, model)
    record = {
        "id": uuid.uuid4().hex,
        "op": "apply-dashboard-update",
        "active": True,
        "at": time.time(),
        "startedAt": _now(),
        "phase": "Пересоздание контейнера пульта",
        "message": "Пульт перезапускается. Соединение восстановится автоматически.",
    }
    _write(_path(), record)
    args = [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--network",
        "none",
        "--volumes-from",
        container["Id"],
        "--env",
        f"DASHBOARD_DIR={config.CFG['dashboard_dir']}",
        "--entrypoint",
        "python3",
        current["local"],
        "/app/dashboardupdate.py",
        str(snapshot),
        config.CFG["dashboard_container"],
        config.CFG["dashboard_service"],
        model["name"],
        out,
    ]
    code, _, _ = dockerlib.sh(args, timeout=30)
    if code:
        snapshot.unlink(missing_ok=True)
        record.update(
            active=False,
            ok=False,
            message="Не удалось запустить обновление пульта.",
            finishedAt=_now(),
        )
        _write(_path(), record)
        raise OpsError(record["message"])
    phase(record["phase"], record["message"])
    return "handoff"


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_helper(snapshot, container_name, service, project, image_id):
    record = operation()
    try:
        # Give the initiating HTTP request and operation stream time to settle.
        time.sleep(2)
        code, _, _ = dockerlib.sh(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(snapshot),
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                service,
            ],
            timeout=600,
        )
        if code:
            raise OpsError("Обновление пульта не удалось. Проверьте контейнер и журнал Docker.")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            container = inspect(container_name)
            status = (container or {}).get("State", {})
            if (
                container
                and container["Image"] == image_id
                and status.get("Running")
                and status.get("Health", {}).get("Status", "healthy") == "healthy"
            ):
                record.update(ok=True, message="Контейнер пульта обновлён и запущен")
                break
            time.sleep(3)
        else:
            raise OpsError(
                "Обновление пульта не подтверждено. Проверьте контейнер и журнал Docker."
            )
    except Exception as error:  # noqa: BLE001 — persist failure across replacement
        record.update(ok=False, message=str(error))
    finally:
        record.update(active=False, finishedAt=_now())
        _write(_path(), record)
        snapshot.unlink(missing_ok=True)
    import ops

    ops.log_event("update" if record["ok"] else "error", record["message"])


if __name__ == "__main__":
    import sys

    run_helper(Path(sys.argv[1]), *sys.argv[2:])
