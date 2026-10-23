#!/usr/bin/env python3
"""One-click Windows installer for JAV FANZA OneClick."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import os
import shutil
import sys
import tempfile
from pathlib import Path


VERSION = "1.12.0"
PLUGIN_ID = "JAVFanzaOneClick"
PAYLOAD_FILES = (
    "JAVFanzaOneClick.yml",
    "jav_fanza_oneclick.exe",
    "jav_fanza_oneclick.js",
    "README_KO.md",
)


def message_box(text: str, title: str, error: bool = False) -> None:
    if os.environ.get("JAV_FANZA_ONECLICK_SILENT") == "1":
        return
    flags = 0x00000010 if error else 0x00000040
    ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def bundled_plugin_dir() -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / "payload" / PLUGIN_ID


def install_target() -> Path:
    test_override = os.environ.get("JAV_FANZA_ONECLICK_INSTALL_ROOT")
    if test_override:
        return Path(test_override).resolve()
    user_profile = os.environ.get("USERPROFILE")
    profile = Path(user_profile) if user_profile else Path.home()
    return profile / ".stash" / "plugins" / PLUGIN_ID


@contextmanager
def installation_lock(target: Path):
    # OS locks are released after a crash; a leftover lock file cannot block updates.
    with (target / ".oneclick-install.lock").open("a+b") as stream:
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
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("다른 설치 작업이 진행 중입니다. 해당 작업이 끝난 뒤 다시 실행하세요.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def install() -> Path:
    source = bundled_plugin_dir()
    missing = [name for name in PAYLOAD_FILES if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError("설치 파일 내부 구성 요소 누락: " + ", ".join(missing))

    target = install_target()
    target.mkdir(parents=True, exist_ok=True)
    with installation_lock(target):
        return install_staged(source, target)


def install_staged(source: Path, target: Path) -> Path:
    staging = None
    temporary_files = []
    keep_backup = False
    try:
        staging = Path(tempfile.mkdtemp(prefix=".oneclick-update-", dir=target.parent))
        # Stage and back up everything first. The manifest is committed last.
        order = [name for name in PAYLOAD_FILES if name != "JAVFanzaOneClick.yml"] + ["JAVFanzaOneClick.yml"]
        backups = {}
        for name in order:
            staged = staging / (name + ".new")
            temporary_files.append(staged)
            shutil.copy2(source / name, staged)
            destination = target / name
            if destination.exists():
                if not destination.is_file() or destination.is_symlink():
                    raise RuntimeError(f"기존 플러그인 경로를 확인하세요: {destination}")
                previous = staging / (name + ".previous")
                temporary_files.append(previous)
                shutil.copy2(destination, previous)
                backups[name] = previous
        committed = []
        try:
            for name in order:
                os.replace(staging / (name + ".new"), target / name)
                committed.append(name)
        except OSError as exc:
            failures = []
            for name in reversed(committed):
                try:
                    if name in backups:
                        os.replace(backups[name], target / name)
                    else:
                        (target / name).unlink()
                except OSError as restore_error:
                    failures.append(f"{name}: {restore_error}")
            if failures:
                keep_backup = True
                raise RuntimeError(f"업데이트와 복원에 실패했습니다. 이전 파일을 보존한 위치: {staging}\n" + "\n".join(failures)) from exc
            raise RuntimeError("업데이트하지 못해 기존 버전으로 복원했습니다. 진행 중인 플러그인 작업을 종료한 뒤 다시 설치하세요.") from exc
    finally:
        if staging and not keep_backup:
            for temporary in temporary_files:
                if temporary.exists():
                    temporary.unlink()
            staging.rmdir()
    return target


def main() -> int:
    try:
        target = install()
    except Exception as exc:
        message_box(
            f"설치하지 못했습니다.\n\n{exc}",
            f"JAV FANZA OneClick {VERSION} 설치 오류",
            error=True,
        )
        return 1

    message_box(
        "설치가 완료되었습니다.\n\n"
        f"설치 위치:\n{target}\n\n"
        "Stash가 실행 중이면 Settings → Plugins → Reload Plugins를 누르고 화면을 새로고침하세요.\n"
        "Stash를 새로 실행하는 경우에는 자동으로 불러옵니다.",
        f"JAV FANZA OneClick {VERSION}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
