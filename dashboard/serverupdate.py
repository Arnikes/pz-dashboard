"""Validate a game image update before stopping the server."""

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import config
import dockerlib
import dashboardupdate
from errors import OpsError


def image_id(image):
    code, out, _ = dockerlib.sh(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], timeout=30
    )
    if code or not out.startswith("sha256:"):
        raise OpsError("Не удалось подтвердить скачанный образ сервера")
    return out


def prepare():
    container = dashboardupdate.inspect(config.CFG["pz_container"])
    if not container:
        raise OpsError("Контейнер сервера не найден")
    labels = container.get("Config", {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    service = config.CFG["pz_service"]
    if not project or labels.get("com.docker.compose.service") != service:
        raise OpsError("Контейнер сервера не соответствует сервису Compose")
    if not config.CFG["compose_file"]:
        raise OpsError("Для обновления сервера задайте COMPOSE_FILE и подключите файлы Compose")
    code, out, _ = dockerlib.sh(
        dockerlib._compose_prefix({**config.CFG, "compose_project": project})
        + ["config", "--format", "json"],
        timeout=30,
    )
    if code:
        raise OpsError(
            "Не удалось прочитать Compose. Проверьте файлы, .env и переменные окружения."
        )
    try:
        model = json.loads(out)
        definition = model["services"][service]
        image = definition["image"]
        running_image = container["Config"]["Image"]
        # Docker's implicit latest tag is equivalent to an explicit one.
        if dockerlib.normalize_image(image) != dockerlib.normalize_image(running_image):
            raise OpsError(
                "Образ сервера в Compose отличается от запущенного. Проверьте .env и переменные окружения."
            )
        if "@" in image or image.startswith("sha256:") or definition.get("build"):
            raise OpsError(
                "Для обновления сервера используйте опубликованный образ с тегом без build или digest"
            )
        if (
            definition.get("container_name", config.CFG["pz_container"])
            != config.CFG["pz_container"]
        ):
            raise OpsError("Имя контейнера сервера в Compose отличается от запущенного")
        panel = dashboardupdate.inspect(config.CFG["dashboard_container"]) or {}
        panel_mounts = sorted(
            panel.get("Mounts", []), key=lambda m: len(m["Destination"]), reverse=True
        )
        server_mounts = {m["Destination"]: m for m in container.get("Mounts", [])}
        for volume in definition.get("volumes", []):
            live = server_mounts.get(volume["target"])
            if volume.get("type", "volume") == "volume":
                source = volume.get("source")
                name = model.get("volumes", {}).get(source, {}).get("name") if source else None
                if not live or live["Type"] != "volume" or (source and name != live.get("Name")):
                    raise OpsError(
                        "Пути данных сервера в Compose не совпадают с подключениями контейнера"
                    )
                continue
            if volume.get("type") != "bind":
                continue
            source = volume["source"]
            for mount in panel_mounts:
                target = mount["Destination"].rstrip("/")
                if mount["Type"] == "bind" and (
                    source == target or source.startswith(target + "/")
                ):
                    source = mount["Source"].rstrip("/") + source[len(target) :]
                    break
            if not live or live["Type"] != "bind" or source != live["Source"]:
                raise OpsError(
                    "Пути данных сервера в Compose не совпадают с подключениями контейнера"
                )
            volume["source"] = source
        model["name"] = project
        return container, model
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise OpsError("Не удалось сопоставить контейнер сервера с Compose") from error


@contextmanager
def compose_snapshot(model):
    # Freeze interpolation (including literal dollars in secrets) and host paths.
    with tempfile.TemporaryDirectory(prefix="server-compose-") as directory:
        snapshot = Path(directory) / "compose.json"
        snapshot.write_text(json.dumps(model).replace("$", "$$"), encoding="utf-8")
        yield snapshot


def recreate(model, snapshot=None):
    if snapshot is None:
        with compose_snapshot(model) as snapshot:
            return recreate(model, snapshot)
    else:
        return dockerlib.sh(
            [
                "docker",
                "compose",
                "-p",
                model["name"],
                "-f",
                str(snapshot),
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                config.CFG["pz_service"],
            ],
            timeout=600,
        )
