"""Nonblocking project writer locks, released by the OS when an owner dies."""
from __future__ import annotations

import functools
import json
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path

_held = threading.local()
DELEGATE_TOKEN = "SPEECHKIT_DELEGATED_LOCK_TOKEN"


class ProjectBusyError(ValueError):
    pass


def lock_token(root) -> str:
    return getattr(_held, "projects", {}).get(str(Path(root).resolve()), "")


def _acquire(file):
    if os.name == "nt":
        import msvcrt
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(file):
    if os.name == "nt":
        import msvcrt
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def project_lock(root):
    root = Path(root).resolve()
    key = str(root)
    owned = getattr(_held, "projects", {})
    if key in owned:
        yield
        return
    folder = root / "work" / ".speechkit_narrate"
    folder.mkdir(parents=True, exist_ok=True)
    lock_path, owner_path = folder / "writer.lock", folder / "writer.json"
    # This inode is deliberately never removed; deleting a lock file can create two owners.
    with lock_path.open("a+b") as file:
        if file.tell() == 0:
            file.write(b" ")
            file.flush()
        try:
            _acquire(file)
        except OSError:
            try:
                owner = json.loads(owner_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                owner = {}
            token = os.environ.get(DELEGATE_TOKEN)
            if token and secrets.compare_digest(token, owner.get("token", "")):
                # Only the coordinator's explicit delegated child receives this token.
                _held.projects = {**owned, key: token}
                try:
                    yield
                finally:
                    _held.projects = owned
                return
            raise ProjectBusyError(f"project {root} is busy with writer PID {owner.get('pid', 'unknown')}; "
                                   "wait for that run to finish") from None
        token = secrets.token_hex(32)
        try:
            owner_path.write_text(json.dumps({"pid": os.getpid(), "token": token}) + "\n", encoding="utf-8")
            _held.projects = {**owned, key: token}
            yield
        finally:
            _held.projects = owned
            _release(file)


def locked_project(function):
    @functools.wraps(function)
    def wrapper(project, *args, **kwargs):
        if kwargs.get("dry_run"):
            return function(project, *args, **kwargs)
        with project_lock(project.root):
            return function(project, *args, **kwargs)
    return wrapper
