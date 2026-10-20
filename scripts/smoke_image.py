"""Check the built panel image using an isolated container on the Docker daemon."""

import argparse
import base64
import secrets
import subprocess
import time


def smoke_image(image, *, attempts=30):
    # Generate Fernet-compatible material without starting a second container.
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    container = subprocess.check_output(
        [
            "docker",
            "create",
            "-e",
            "PZ_ADMIN_LOGIN=ci-admin",
            "-e",
            f"PZ_ADMIN_PASSWORD={secrets.token_urlsafe(24)}",
            "-e",
            f"PZ_AUTH_KEY={key}",
            "-e",
            "RCON_PASSWORD=ci-only-dummy",
            image,
        ],
        text=True,
    ).strip()
    if not container:
        raise RuntimeError("Docker did not return a container ID.")
    try:
        subprocess.run(["docker", "start", container], check=True)
        for attempt in range(attempts):
            result = subprocess.run(
                ["docker", "exec", container, "python", "healthcheck.py"], check=False
            )
            if result.returncode == 0:
                print(f"Container health OK (attempt {attempt + 1}).", flush=True)
                return
            if attempt + 1 < attempts:
                time.sleep(1)
        raise RuntimeError(f"Container health failed after {attempts} attempts.")
    finally:
        # Cleanup only this run's container, including after a failed start.
        # Diagnostics must never mask the original smoke-test failure.
        for command in (["docker", "logs", container], ["docker", "rm", "-f", container]):
            try:
                subprocess.run(command, check=False)
            except OSError as error:
                print(f"Container cleanup command failed: {error}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="The exact image tag or ID produced by the build.")
    args = parser.parse_args()
    smoke_image(args.image)


if __name__ == "__main__":
    main()
