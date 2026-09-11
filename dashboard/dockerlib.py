#!/usr/bin/env python3
"""Тонкая обёртка над docker CLI (контейнер получает /var/run/docker.sock).

Все команды выполняются списком аргументов (shell=False), параметры
проходят валидацию в ops.py.
"""

import re
import subprocess

SIZE_RE = re.compile(r"^([\d.]+)\s*([kKmMgG]?)(i?)([bB])$")


def sh(args, timeout=120, merge_stderr=False):
    """Выполнить команду, вернуть (code, stdout, stderr)."""
    try:
        p = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "docker CLI не найден в контейнере"
    except subprocess.TimeoutExpired:
        return -1, "", f"таймаут команды (>{timeout} с)"


def docker_version():
    return sh(["docker", "version", "--format", "{{.Server.Version}}"])[0] == 0


def compose_version():
    return sh(["docker", "compose", "version", "--short"])[0] == 0


def _compose_prefix(cfg):
    args = ["docker", "compose"]
    if cfg["compose_project"]:
        args += ["-p", cfg["compose_project"]]
    if cfg["compose_file"]:
        for f in cfg["compose_file"].split(":"):
            args += ["-f", f]
    return args


def inspect_container(name):
    """(status, running, started_at, image) или None, если контейнер не найден."""
    code, out, err = sh(
        [
            "docker",
            "inspect",
            name,
            "--format",
            "{{.State.Status}}|{{.State.Running}}|{{.State.StartedAt}}|{{.Config.Image}}",
        ],
        timeout=30,
    )
    if code != 0 or not out:
        return None
    parts = out.split("|")
    if len(parts) < 4:
        return None
    return {
        "status": parts[0],
        "running": parts[1] == "true",
        "startedAt": parts[2],
        "image": parts[3],
    }


def image_digests(image):
    """Первый RepoDigest образа (sha256:...) или None.

    Важно: RepoDigests хранится в виде repo@sha256:... — парсим хеш после @."""
    code, out, err = sh(
        [
            "docker",
            "image",
            "inspect",
            image,
            "--format",
            "{{range .RepoDigests}}{{println .}}{{end}}",
        ],
        timeout=30,
    )
    if code != 0 or not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if "@sha256:" in line:
            return line.split("@", 1)[1]
        if line.startswith("sha256:"):
            return line
    return None


def container_logs(name, tail=250):
    code, out, err = sh(
        ["docker", "logs", "--tail", str(tail), "--timestamps", name],
        timeout=30,
        merge_stderr=True,
    )
    if code != 0:
        return None, err or out
    return out, None


def parse_bytes(raw):
    """'1.2kB', '123MiB', '1.9GiB' → байты."""
    raw = (raw or "").strip()
    m = SIZE_RE.match(raw)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2)
    iec = m.group(3) == "i"
    if not unit:
        return int(num)
    if iec:
        return int(num * {"k": 1024, "m": 1024**2, "g": 1024**3}[unit.lower()])
    return int(num * {"k": 1e3, "m": 1e6, "g": 1e9}[unit.lower()])


def container_stats(name):
    """{cpuPct, memUsed, memLimit, memPct, netIn, netOut, pids} или None."""
    code, out, err = sh(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.PIDs}}",
            name,
        ],
        timeout=30,
    )
    if code != 0 or not out:
        return None
    try:
        cpu, mem_usage, mem_pct, net_io, pids = out.split("|")
        mem_used_raw, mem_limit_raw = [x.strip() for x in mem_usage.split("/")]
        net_in_raw, net_out_raw = [x.strip() for x in net_io.split("/")]
        return {
            "cpuPct": float(cpu.replace("%", "") or 0),
            "memUsed": parse_bytes(mem_used_raw),
            "memLimit": parse_bytes(mem_limit_raw),
            "memPct": float(mem_pct.replace("%", "") or 0),
            "netIn": parse_bytes(net_in_raw),
            "netOut": parse_bytes(net_out_raw),
            "pids": int(pids or 0),
        }
    except (ValueError, IndexError):
        return None


def container_start(name):
    return sh(["docker", "start", name], timeout=120)


def container_stop(name, seconds=180):
    return sh(["docker", "stop", "-t", str(seconds), name], timeout=seconds + 60)


def container_exec(name, command, timeout=60):
    """docker exec sh -c … — чтение данных внутри контейнера (find/sed)."""
    return sh(["docker", "exec", name, "sh", "-c", command], timeout=timeout)


def get_restart_policy(name):
    """Имя политики рестарта ("no", "always", "unless-stopped", "on-failure") или None."""
    code, out, err = sh(
        [
            "docker",
            "inspect",
            name,
            "--format",
            "{{.HostConfig.RestartPolicy.Name}}",
        ],
        timeout=30,
    )
    if code != 0:
        return None
    return (out or "").strip() or "no"


def set_restart_policy(name, policy):
    """docker update --restart=… (не запускает контейнер, меняет только политику)."""
    code, out, err = sh(["docker", "update", "--restart=" + policy, name], timeout=30)
    return code == 0


def image_pull(image, timeout=1500):
    return sh(["docker", "pull", image], timeout=timeout)


def compose_up(cfg, timeout=600):
    """Пересоздать сервис из compose (подхватывает новый образ)."""
    return sh(_compose_prefix(cfg) + ["up", "-d", cfg["pz_service"]], timeout=timeout)


def compose_pull(cfg, timeout=1500):
    return sh(_compose_prefix(cfg) + ["pull", cfg["pz_service"]], timeout=timeout)
