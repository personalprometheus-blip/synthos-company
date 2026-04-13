"""
company_lock.py — Write-Access Protection for company.db
=========================================================
Lightweight file-based lock that prevents concurrent writes to company.db.
Replaces the Timekeeper slot system with a simple fcntl lock.

Usage by agents:
    from utils.company_lock import company_write_lock

    with company_write_lock("Auditor"):
        db.post_suggestion(...)

    # Or non-blocking check:
    with company_write_lock("Scoop", timeout=5) as acquired:
        if acquired:
            db.send_mail(...)
        else:
            print("DB busy, will retry")

The lock file lives at data/.company_write.lock on pi4b.
Uses fcntl.LOCK_EX for process-safe atomic locking (same pattern as
retail_scheduler.py SessionLock on pi5).
"""

import os
import time
import fcntl
import logging
from pathlib import Path
from contextlib import contextmanager

log = logging.getLogger('company_lock')

_BASE_DIR = Path(__file__).resolve().parent.parent  # synthos-company/
_LOCK_DIR = _BASE_DIR / 'data'
_LOCK_FILE = _LOCK_DIR / '.company_write.lock'


class CompanyWriteLock:
    """
    Process-safe file lock for company.db writes.
    Uses fcntl — works across all Python processes on the same filesystem.
    """

    def __init__(self, agent_name: str = 'unknown', timeout: int = 30):
        self.agent_name = agent_name
        self.timeout = timeout
        self._fd = None
        self._acquired = False

    def acquire(self) -> bool:
        """Try to acquire the write lock. Returns True if acquired."""
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self._fd = open(_LOCK_FILE, 'w')
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd.write(f"{self.agent_name}\n{os.getpid()}\n{time.time()}\n")
                self._fd.flush()
                self._acquired = True
                log.debug(f"[{self.agent_name}] Write lock acquired")
                return True
            except BlockingIOError:
                time.sleep(0.5)
        log.warning(f"[{self.agent_name}] Could not acquire write lock after {self.timeout}s")
        self._fd.close()
        self._fd = None
        return False

    def release(self):
        """Release the write lock."""
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None
            self._acquired = False
            log.debug(f"[{self.agent_name}] Write lock released")

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    @property
    def is_acquired(self):
        return self._acquired


@contextmanager
def company_write_lock(agent_name: str = 'unknown', timeout: int = 30):
    """
    Context manager for company.db write access.

    Usage:
        with company_write_lock("Auditor") as lock:
            if lock.is_acquired:
                db.post_suggestion(...)
            else:
                print("Busy, skipping")

    Or if you want it to block until acquired (default 30s timeout):
        with company_write_lock("Scoop"):
            db.write_something()
    """
    lock = CompanyWriteLock(agent_name, timeout)
    try:
        lock.acquire()
        yield lock
    finally:
        lock.release()


def check_lock_status():
    """Return info about who holds the lock, if anyone."""
    if not _LOCK_FILE.exists():
        return None
    try:
        content = _LOCK_FILE.read_text().strip().split('\n')
        if len(content) >= 3:
            agent = content[0]
            pid = int(content[1])
            lock_time = float(content[2])
            age = int(time.time() - lock_time)
            # Check if the PID is still alive
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
            return {
                'agent': agent,
                'pid': pid,
                'age_secs': age,
                'alive': alive,
            }
    except Exception:
        pass
    return None
