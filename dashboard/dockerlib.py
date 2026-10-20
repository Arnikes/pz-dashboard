#!/usr/bin/env python3
"""Тонкая обёртка над docker CLI (контейнер получает /var/run/docker.sock).

Все команды выполняются списком аргументов (shell=False), параметры
проходят валидацию в ops.py.
"""

import re
import json
import posixpath
import queue
import subprocess
import threading
import time

SIZE_RE = re.compile(r"^([\d.]+)\s*([kKmMgG]?)(i?)([bB])$")
STARTUP_LOG_BYTES_LIMIT = 64_000_000


def normalize_image(image):
    if "@" not in image and ":" not in image.rsplit("/", 1)[-1]:
        return image + ":latest"
    return image


def host_bind_source(source, mounts):
    """Translate paths visible inside the panel into the Docker host's paths."""
    for mount in sorted(mounts, key=lambda item: len(item["Destination"]), reverse=True):
        target = mount["Destination"].rstrip("/")
        if mount["Type"] == "bind" and (source == target or source.startswith(target + "/")):
            return mount["Source"].rstrip("/") + source[len(target) :]
    return source


def preserve_mounts(container, model, service):
    """Validate both declared and removed persistent mounts before recreation.

    File-backed Compose configs and secrets also appear as binds in inspect.
    Implicit image volumes get an anonymous declaration so Compose inherits the
    existing data even when the next image removes its VOLUME instruction.
    """
    definition = model["services"][service]
    expected = {volume["target"]: volume for volume in definition.get("volumes", [])}
    for kind, prefix in (("configs", "/"), ("secrets", "/run/secrets/")):
        for reference in definition.get(kind, []):
            source = reference["source"]
            target = posixpath.join(prefix, reference.get("target") or source)
            record = model.get(kind, {}).get(source, {})
            # Inline/environment-backed files have generated host paths. Their
            # declared targets are retained without treating them as world storage.
            expected.setdefault(
                target,
                {
                    "type": "bind",
                    "source": record.get("file"),
                    "read_only": True,
                    "generated": not record.get("file"),
                },
            )
    mounts = {mount["Destination"]: mount for mount in container.get("Mounts", [])}
    for target, live in mounts.items():
        if live["Type"] not in ("bind", "volume") or target in expected:
            continue
        name = live.get("Name", "")
        if (
            live["Type"] != "volume"
            or target not in (container.get("Config", {}).get("Volumes") or {})
            or not re.fullmatch(r"[0-9a-f]{64}", name)
        ):
            return False
        volume = {"type": "volume", "target": target, "read_only": not live.get("RW", True)}
        definition.setdefault("volumes", []).append(volume)
        expected[target] = volume
    for target, volume in expected.items():
        kind = volume.get("type", "volume")
        live = mounts.get(target)
        if kind not in ("bind", "volume"):
            if live and live["Type"] in ("bind", "volume"):
                return False
            continue
        if (
            not live
            or live["Type"] != kind
            or live.get("RW", True) != (not volume.get("read_only", False))
        ):
            return False
        source = volume.get("source")
        if kind == "bind":
            if not volume.get("generated") and posixpath.normpath(source) != posixpath.normpath(
                live["Source"]
            ):
                return False
        elif source:
            name = model.get("volumes", {}).get(source, {}).get("name")
            if not name or name != live.get("Name"):
                return False
        if kind == "volume":
            configured = next(
                (
                    mount
                    for mount in (container.get("HostConfig", {}).get("Mounts") or [])
                    if mount.get("Target") == target
                ),
                {},
            )
            if (volume.get("volume") or {}).get("subpath", "") != (
                configured.get("VolumeOptions") or {}
            ).get("Subpath", ""):
                return False
    return True


def registry_digests(image, image_id, descriptor=None):
    """Compare the same platform and digest kind through Docker's registry client."""
    descriptor = descriptor or {}
    local = descriptor.get("digest") or image_id
    code, out, _ = sh(["docker", "manifest", "inspect", "--verbose", image], timeout=25)
    if code:
        raise ValueError("Registry manifest unavailable")
    manifests = json.loads(out)
    if not isinstance(manifests, list):
        manifests = [manifests]
    code, out, _ = sh(["docker", "image", "inspect", image_id], timeout=30)
    if code:
        raise ValueError("Installed image unavailable")
    installed = json.loads(out)[0]
    if installed.get("Descriptor") and not descriptor.get("digest"):
        raise ValueError("Running platform manifest unavailable")
    platform = descriptor.get("platform") or {
        key: installed.get(field, "")
        for key, field in (("os", "Os"), ("architecture", "Architecture"), ("variant", "Variant"))
    }
    if not platform.get("os") or not platform.get("architecture"):
        raise ValueError("Installed platform unavailable")
    for manifest in manifests:
        remote_descriptor = manifest.get("Descriptor") or {}
        remote_platform = remote_descriptor.get("platform") or {}
        if remote_platform and any(
            remote_platform.get(key, "") != platform.get(key, "")
            for key in ("os", "architecture", "variant")
        ):
            continue
        schema = manifest.get("SchemaV2Manifest") or manifest.get("OCIManifest") or {}
        remote = (
            remote_descriptor.get("digest")
            if descriptor.get("digest")
            else schema.get("config", {}).get("digest")
        )
        if remote and local:
            return local, remote
    raise ValueError("Matching platform manifest unavailable")


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
            "{{.State.Status}}|{{.State.Running}}|{{.State.StartedAt}}|{{.Config.Image}}|{{.Image}}",
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
        "imageId": parts[4] if len(parts) > 4 else None,
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


def container_logs(name, tail=250, since=None, until=None):
    args = ["docker", "logs", "--tail", str(tail), "--timestamps"]
    if since:
        args += ["--since", since]
    if until:
        args += ["--until", until]
    args.append(name)
    code, out, err = sh(
        args,
        timeout=30,
        merge_stderr=True,
    )
    if code != 0:
        return None, err or out
    return out, None


def container_logs_to_file(name, destination):
    """Capture both log streams without buffering the full container log in RAM."""
    try:
        result = subprocess.run(
            ["docker", "logs", name],
            stdout=destination,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def parse_startup_version(line):
    # PZ emits this header before loading mods. A mod title or player message
    # mentioning a version must not override the game version.
    match = re.search(
        r"^(?:\d{4}-\d\d-\d\dT\S+\s+)?LOG\s*:\s*General\b[^>\r\n]*>\s*"
        r"version\s*=\s*(\d{2}\.\d+(?:\.\d+)?)\b",
        line,
        re.I,
    )
    return match[1] if match else None


def _startup_version_attempt(args, deadline, output_limit):
    """Read one Docker log window; only clean EOF allows a larger attempt."""
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None, 0, False
    messages = queue.Queue(maxsize=16)
    stopped = threading.Event()
    read_failed = threading.Event()

    def send(line):
        while not stopped.is_set():
            try:
                messages.put(line, timeout=0.1)
                return
            except queue.Full:
                continue

    def read():
        try:
            while not stopped.is_set():
                line = process.stdout.readline(65536)
                send(line or None)
                if not line:
                    break
        except (OSError, ValueError):
            read_failed.set()
            send(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    size, version, complete = 0, None, False
    try:
        while (remaining := deadline - time.monotonic()) > 0 and size < output_limit:
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                if read_failed.is_set():
                    break
                try:
                    code = process.poll()
                    if code is None:
                        code = process.wait(timeout=max(0, deadline - time.monotonic()))
                    complete = code == 0
                except (OSError, subprocess.TimeoutExpired):
                    pass
                break
            size += len(line)
            if size > output_limit:
                break
            version = parse_startup_version(line)
            if version:
                break
    finally:
        stopped.set()
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass  # The docker log reader may have exited since poll().
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        reader.join(timeout=2)
        if not reader.is_alive():
            process.stdout.close()
    return version, size, complete and not reader.is_alive()


def container_startup_version(name, started_at, timeout=15):
    """Read a current-launch header with one time and output budget for all windows."""
    if not started_at or started_at.startswith("0001-"):
        return None
    deadline = time.monotonic() + timeout
    output_left = STARTUP_LOG_BYTES_LIMIT
    # With json-file journals, Docker applies --since after decoding old history.
    # A finite --tail seeks backwards first, avoiding that delay.
    # Expand when recent windows omit the header, retaining the full-log fallback.
    for tail in (100_000, 1_000_000, None):
        if time.monotonic() >= deadline or output_left <= 0:
            break
        args = ["docker", "logs"]
        if tail is not None:
            args += ["--tail", str(tail)]
        args += ["--since", started_at, "--timestamps", name]
        version, size, complete = _startup_version_attempt(args, deadline, output_left)
        output_left -= size
        if version:
            return version
        if not complete:
            break
    return None


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
