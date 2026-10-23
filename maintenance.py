"""Shared operation locks, lazy backups and bounded temporary-record cleanup."""
from contextlib import contextmanager
import hashlib
import os
import time
from pathlib import Path

from performer_names import read_json, write_json


@contextmanager
def operation_lock(root, name):
    folder = Path(root) / "operation_locks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(name.encode()).hexdigest() + ".lock")
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("같은 작업이 이미 실행 중입니다. Tasks에서 완료 여부를 확인하세요.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Backup:
    def __init__(self, client):
        self.client = client
        self.done = False
        self.receipt = None

    def ensure(self):
        if self.done:
            return
        result = self.client.graphql("mutation OneClickMaintenanceBackup($input: BackupDatabaseInput!) { backupDatabase(input: $input) }",
                                     {"input": {"download": False, "includeBlobs": True}}, timeout=300)
        if "backupDatabase" not in result or (result["backupDatabase"] is not None and
                (not isinstance(result["backupDatabase"], str) or not result["backupDatabase"].strip())):
            raise RuntimeError("전체 백업 완료를 확인하지 못해 변경하지 않았습니다.")
        self.done, self.receipt = True, result["backupDatabase"]


def clean_previews(root):
    """Only expired preview tickets, never image restoration or merge history."""
    folder = Path(root) / "image_previews"
    if not folder.is_dir() or folder.is_symlink() or getattr(folder, "is_junction", lambda: False)():
        return 0
    checkpoint = Path(root) / "preview_cleanup.json"
    if time.time() - read_json(checkpoint).get("time", 0) < 86400:
        return 0
    removed = 0
    for p in folder.glob("*.json"):
        if p.is_symlink() or not p.is_file() or len(p.stem) != 32 or any(c not in "0123456789abcdef" for c in p.stem):
            continue
        item = read_json(p)
        if item.get("stage") in {"candidates", "preview", "name"} and isinstance(item.get("time"), (int, float)) and time.time() - item["time"] > 86400:
            p.unlink()
            removed += 1
    write_json(checkpoint, {"time": time.time(), "removed": removed})
    return removed
