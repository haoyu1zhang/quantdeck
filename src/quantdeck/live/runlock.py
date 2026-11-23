from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunLock:
    """Pid file that stops two processes from trading the same run at once.

    Created with O_EXCL so two starts racing each other can't both win. A lock
    left behind by a crashed process is detected and replaced.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if _alive(pid) and pid != os.getpid():
                    raise AlreadyRunning(f"{self.path.parent.name} is already running (pid {pid})") from None
                self.path.unlink(missing_ok=True)  # stale
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self._held = True
            return
        raise AlreadyRunning(f"could not acquire {self.path}")

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
