"""Bounded retries for transient Windows file-sharing failures."""

import os
import time


def replace(source, target):
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if (
                os.name != "nt"
                or getattr(error, "winerror", None) not in (5, 32, 33)
                or attempt == 4
            ):
                raise
            time.sleep(0.025 * 2**attempt)
