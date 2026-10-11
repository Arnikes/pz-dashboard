#!/usr/bin/env python3
"""Конфигурация пульта PZ. Все параметры задаются переменными окружения
(см. .env.example в корне проекта)."""

import os


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


IMAGE = os.getenv("PZ_IMAGE", "indifferentbroccoli/projectzomboid-server-docker:latest")
_REPO, _TAG = IMAGE.rsplit(":", 1) if ":" in IMAGE.rsplit("/", 1)[-1] else (IMAGE, "latest")

CFG = {
    "port": _int("PORT", 8080),
    "pz_container": os.getenv("PZ_CONTAINER", "pzserver"),
    "pz_service": os.getenv("PZ_SERVICE", "pzserver"),
    "dashboard_container": os.getenv("PZ_DASHBOARD_CONTAINER", "pz-dashboard"),
    "dashboard_service": os.getenv("PZ_DASHBOARD_SERVICE", "pz-dashboard"),
    "pz_image": IMAGE,
    "image_repo": _REPO,
    "image_tag": _TAG,
    "rcon_host": os.getenv("RCON_HOST", "pzserver"),
    "rcon_port": _int("RCON_PORT", 27015),
    "rcon_password": os.getenv("RCON_PASSWORD", ""),
    "compose_file": os.getenv("COMPOSE_FILE", ""),  # путь в контейнере, можно :склейка
    "compose_project": os.getenv("COMPOSE_PROJECT_NAME", ""),
    "data_dir": os.getenv("DATA_DIR", "/data"),  # смонтированные данные PZ
    "backup_dir": os.getenv("BACKUP_DIR", "/backups"),
    "backup_s3_enabled": os.getenv("BACKUP_S3_ENABLED", "false").lower() in ("true", "1"),
    "backup_s3_bucket": os.getenv("BACKUP_S3_BUCKET", "").strip(),
    "backup_s3_endpoint": os.getenv("BACKUP_S3_ENDPOINT_URL", "").strip(),
    "backup_s3_region": os.getenv("BACKUP_S3_REGION", "us-east-1").strip(),
    "backup_s3_prefix": os.getenv("BACKUP_S3_PREFIX", "pz-backups").strip().strip("/"),
    "backup_s3_addressing_style": os.getenv("BACKUP_S3_ADDRESSING_STYLE", "auto").strip(),
    "dashboard_dir": os.getenv("DASHBOARD_DIR", "/dashboard-data"),
    "server_name": os.getenv("PZ_SERVER_NAME", "Project Zomboid"),
    "log_lines": _int("LOG_LINES", 250),
}

CFG["events_file"] = os.path.join(CFG["dashboard_dir"], "events.jsonl")
CFG["settings_file"] = os.path.join(CFG["dashboard_dir"], "settings.json")
