"""Pull the official Python base image with a bounded registry fallback."""

import argparse
import subprocess

PYTHON_BASE_IMAGES = (
    "docker.io/library/python:3.12-alpine",
    "public.ecr.aws/docker/library/python:3.12-alpine",
)


def prepare_image_base(tag, *, timeout=120):
    failures = []
    for image in PYTHON_BASE_IMAGES:
        print(f"Pulling official Python base image: {image}", flush=True)
        try:
            result = subprocess.run(["docker", "pull", image], check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            reason = f"timed out after {timeout} seconds"
        else:
            if result.returncode == 0:
                # Retagging errors are local Docker failures, not registry failures.
                subprocess.run(["docker", "tag", image, tag], check=True)
                print(f"Prepared local base image {tag} from {image}.", flush=True)
                return image
            reason = f"exited with code {result.returncode}"
        failures.append(f"{image}: {reason}")
        print(f"Base image pull {reason}; checking the next registry if available.", flush=True)
    raise RuntimeError(
        "Cannot prepare the Python base image from any configured registry. "
        "Check registry availability, network access, and pull quotas before retrying. "
        + "; ".join(failures)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="The local base image tag used by the following Docker build.")
    args = parser.parse_args()
    prepare_image_base(args.tag)


if __name__ == "__main__":
    main()
