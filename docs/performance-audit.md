# CPU and memory audit

The October 10, 2026 audit reviewed monitoring collectors, browser polling,
background schedulers, notifications, filesystem scans and backup validation.
Graphify queries guided source inspection; changes were verified against the
actual implementations and isolated tests.

## Changes

- `workshop.local_files` walks directory entries and constructs `Path` objects
  only for the six supported metadata filenames. It resolves the Workshop item
  root once, skips directory links, retains containment checks and the 512,000-byte
  metadata limit, and preserves UTF-8 BOM handling. It still traverses all real
  directories because supported metadata can be nested at arbitrary depths.
- `ops.overview` counts backup entries directly instead of formatting and sorting
  full rows every three seconds. The count remains fresh: creates, deletes and
  target-directory changes are visible on the next collection. `list_backups`
  uses `scandir` and cached entry metadata for the full list; retention still
  sorts by nanosecond modification time.
- Backup verification checks the entire gzip stream in 64 KiB reads before safe
  tar extraction. This removes an external `tar -tzf` process and its unbounded
  filename output. CRC, truncation and invalid deflate errors become operation
  errors before stopping the game server. A monotonic 900-second deadline is
  checked between blocks. English/Russian timeout messages were updated together.

The CRC regression exposed an existing Windows failure: the installed `tar`
accepted a valid tar payload with a damaged gzip trailer. The new validation
rejects that archive before stopping or modifying the running world. Safe-member
filtering, numeric owner preservation and staged restoration remain in place.

## Existing controls reviewed

The SSE cache already shares collection across subscribers and EN/RU, and caches
localized frames. Browser polling already prevents overlapping requests, stops
requests in hidden tabs, and fetches logs only in the visible console. Metadata
caches, event history and notification queues have entry limits. The notification
worker blocks on its queue; schedulers sleep between iterations. These mechanisms
were retained. Direct control checks still read current container state.

## Reproduce measurements

Use Python 3.12 with the checkout's pinned virtual environment:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_filesystem.py --baseline ed6e6d51292b338c6eafbd20993d75aa61de1f98
```

The script creates temporary synthetic data, compiles only the baseline functions
from the specified trusted local Git revision, and checks equal scan/list/count
results. Scenarios contain 10,000 mod assets with 20 metadata files, 2,000 backup
entries, and a gzip archive with 20,000 tar members. Fixture creation is outside
the measurement. Timing uses the median of five accumulated runs; peak Python
allocations are measured separately with `tracemalloc`.

CPU timing covers the Python process only: historical archive validation also
uses a child process, whose CPU is excluded. Compare validation wall time and
Python allocations rather than treating its parent CPU timing as total CPU.

## Measured results

The final measurement ran after the repository test suite finished, on Windows
with Python 3.12.10. Values below compare the baseline with this implementation.

| Scenario | Python CPU, ms (before → after) | Wall time, ms (before → after) | Peak Python allocations, KiB (before → after) |
| --- | --- | --- | --- |
| Workshop: 10,000 assets, 20 metadata files | 78.125 → 33.203 | 78.213 → 34.565 | 222.2 → 44.1 |
| Overview count: 2,000 backups | 56.250 → 2.066 | 58.293 → 2.114 | 985.9 → 2.2 |
| Full list: 2,000 backups | 59.375 → 12.500 | 60.319 → 12.197 | 986.0 → 986.8 |
| Gzip validation: 20,000 tar members | Not comparable: baseline has child CPU | 173.975 → 21.369 | 2,378.9 → 442.2 |

Workshop scanning used **57.5% less Python CPU** and **80.1% less peak Python
memory**. Counting backups used **96.3% less Python CPU** and **99.8% less peak
Python memory**. The full list used **78.9% less Python CPU**, with essentially
the same memory because it still returns all rows. The gzip validation stage was
**8.1 times faster by wall time**, with **81.4% less peak Python memory**. These
percentages describe only the named synthetic operations.

## Limits

These are synthetic panel measurements, not production CPU percentages or total
process RSS. Results depend on filesystem, cache state and archive contents.
Full tar extraction still retains member metadata, and safe restoration still
copies staged world data. Compression, extraction, Docker/RCON work and the game
server require real deployment profiling to quantify their combined load.

The gzip deadline is cooperative between reads; a blocked filesystem read cannot
be interrupted by that deadline. Extraction remains a separate subsequent pass,
as it was before this audit.

## Validation

The final repository gate used Python 3.12.10 from `.venv` and Node.js 24.21.0.
Repository-wide Ruff formatting, lint and format checks passed. Dependency
consistency, generated EN/RU catalogs and all JavaScript syntax checks passed.
`python scripts/check.py` passed with **1,549 tests passed and 4 skipped**, including
the Chromium suite. The new directory/file symlink regression was skipped because
this Windows account cannot create symlinks; its non-link metadata checks passed.

The resource tests cover fresh counts after create/delete, metadata filename and
size filtering, BOM decoding, fixed-size validation reads, deadlines, stream
cleanup and refusing corrupt gzip/unsafe tar archives before server shutdown.
The local code graph was updated with `graphify update .` using AST extraction
without model API calls. `git diff --check` passed.
