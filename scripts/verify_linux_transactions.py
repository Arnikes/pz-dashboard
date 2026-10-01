"""Exercise the shipped transaction writer on disposable Linux files, without Docker access.

Run as UID 1000 inside a disposable panel-image container with /verification on
tmpfs. The Docker/RCON boundary is replaced by an explicitly stopped fixture;
filesystem writes, permissions, fsync, process death and recovery are real.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path.cwd()))

import config  # noqa: E402
import configeditor as editor  # noqa: E402
import ops  # noqa: E402

FILE = "verify.ini"
INI = "# Preserve comment\r\nPublicName=Before\r\nUnknown=keep\r\n"
SANDBOX = (
    "SandboxVars = {\n -- Preserve comment\n VERSION = 6, Zombies = 4, Mod = { Count = 2 },\n}\n"
)
TARGET = {
    "ini": INI.replace("Before", "After"),
    "sandbox": SANDBOX.replace("Count = 2", "Count = 9"),
}


def stopped_fixture(root):
    # No connection to an engine is possible in the documented --network none,
    # socket-free container. Scope all writes strictly to this temporary root.
    assert root.resolve().is_relative_to(Path("/verification").resolve())
    config.CFG.update(data_dir=str(root / "data"), dashboard_dir=str(root / "panel"))
    editor.confirmed_container = lambda: {"running": False}
    editor.context = lambda refresh=False: {"mountsKnown": True, "dataWritable": True}
    ops.is_running = lambda: False
    ops.op_busy = lambda: False


def pair(root):
    stopped_fixture(root)
    server = root / "data/Server"
    server.mkdir(parents=True)
    ini, sandbox = server / FILE, server / "verify_SandboxVars.lua"
    ini.write_bytes(INI.encode("utf-8-sig"))
    sandbox.write_bytes(SANDBOX.encode())
    ini.chmod(0o640)
    sandbox.chmod(0o600)
    return ini, sandbox


def attributes(path):
    stat = path.stat()
    return stat.st_uid, stat.st_gid, stat.st_mode & 0o777


def check_original(paths, contents, owners):
    assert [path.read_bytes() for path in paths] == contents, "Original bytes were not restored"
    assert [attributes(path) for path in paths] == owners, "Ownership or permissions changed"
    assert not (editor.state_dir(FILE) / "transaction.json").exists(), (
        "Recovery journal was not cleared"
    )


def permission_failure(root, persistent=False):
    paths = ini, sandbox = pair(root)
    contents = [path.read_bytes() for path in paths]
    owners = [attributes(path) for path in paths]
    real_atomic = editor.atomic
    first_written = False
    denied = False

    def fault(path, data, mode=0o600, owner=None):
        nonlocal first_written, denied
        if path == ini and data == TARGET["ini"].encode("utf-8-sig") and not first_written:
            real_atomic(path, data, mode, owner=owner)
            assert ini.read_bytes() != contents[0] and sandbox.read_bytes() == contents[1]
            first_written = True
            ini.parent.chmod(0o500)
            return
        if path == sandbox and first_written and not denied:
            try:
                real_atomic(path, data, mode, owner=owner)
            except PermissionError:
                denied = True
                if not persistent:
                    ini.parent.chmod(0o700)
                raise
            raise AssertionError("Kernel did not reject the second file's write")
        real_atomic(path, data, mode, owner=owner)

    editor.atomic = fault
    try:
        try:
            editor.commit(FILE, TARGET, "Disposable Linux permission fault")
        except PermissionError:
            assert persistent, "Rollback unexpectedly failed"
        except editor.EditorError as error:
            assert not persistent and error.status == 500
        else:
            raise AssertionError("Failed write was reported as successful")
        assert first_written and denied
        journal = editor.state_dir(FILE) / "transaction.json"
        if persistent:
            assert journal.exists(), "Failed recovery lost its durable journal"
            assert ini.read_bytes() != contents[0] and sandbox.read_bytes() == contents[1]
            ini.parent.chmod(0o700)
            editor.atomic = real_atomic
            editor.recover(FILE)
        check_original(paths, contents, owners)
    finally:
        ini.parent.chmod(0o700)
        editor.atomic = real_atomic
    return {
        "secondWriteDenied": True,
        "originalBytesRestored": True,
        "permissionsRestored": True,
        "uid": owners[0][0],
    }


def process_crash(root):
    paths = ini, _ = pair(root)
    contents = [path.read_bytes() for path in paths]
    owners = [attributes(path) for path in paths]
    child = """
import os, sys
from pathlib import Path
import config, configeditor as editor, ops
root = Path(sys.argv[1])
assert root.resolve().is_relative_to(Path('/verification').resolve())
config.CFG.update(data_dir=str(root/'data'), dashboard_dir=str(root/'panel'))
editor.confirmed_container = lambda: {'running': False}
editor.context = lambda refresh=False: {'mountsKnown': True, 'dataWritable': True}
ops.is_running = lambda: False
ops.op_busy = lambda: False
real_atomic = editor.atomic
ini = root/'data/Server/verify.ini'
def crash_after_replace(path, data, mode=0o600, owner=None):
    real_atomic(path, data, mode, owner=owner)
    if path == ini:
        os._exit(73)
editor.atomic = crash_after_replace
before = editor.read_profile('verify.ini')
editor.commit('verify.ini', {'ini': before['ini'].replace('Before','After'), 'sandbox': before['sandbox'].replace('Count = 2','Count = 9')}, 'Disposable process death')
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(root)], capture_output=True, timeout=30
    )
    assert result.returncode == 73, "Writer did not die at the intended checkpoint"
    journal = editor.state_dir(FILE) / "transaction.json"
    assert journal.exists() and ini.read_bytes() != contents[0]
    record = editor.load_json(journal)
    folder = journal.parent / "history" / record["historyId"]
    assert (folder / "ini").read_bytes() == contents[0]
    assert (folder / "sandbox").read_bytes() == contents[1]
    editor.recover(FILE)
    check_original(paths, contents, owners)
    return {
        "exitCode": 73,
        "durableJournalRetained": True,
        "originalBytesRestored": True,
        "permissionsRestored": True,
    }


def main():
    if os.name != "posix" or not hasattr(os, "getuid") or os.getuid() != 1000:
        raise SystemExit(
            "Run this verifier as UID 1000 in the isolated Linux container described in docs/acceptance-b42.md"
        )
    with tempfile.TemporaryDirectory(prefix="pz-transactions-", dir="/verification") as temporary:
        root = Path(temporary)
        results = {
            "permissionFailure": permission_failure(root / "permission"),
            "persistentRollbackFailure": permission_failure(root / "persistent", persistent=True),
            "processCrash": process_crash(root / "crash"),
        }
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
