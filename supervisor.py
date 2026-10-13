"""The supervisor: runs ``app.py`` as a child process and keeps it on the latest commit of ``main``.

``start.sh`` / ``start.bat`` start this file, not ``app.py``. Every ``update_check_min`` minutes
(``config.json``, default 60, ``0`` — off) it asks GitHub for the SHA of ``main``; a new one is
downloaded, copied over the code (``config.json``, ``data/``, ``runtime/`` are not in the repository
and stay), the requirements are reinstalled if they changed, and the child is restarted. A child that
dies within ``PROBATION_SEC`` of the update gets the old files back, and that SHA is not tried again.
A child that dies on its own is restarted after a growing pause.

⚠️ Standard library only and no ``monitor.*`` imports: a broken update must not break the code that
rolls it back.
⚠️ The restart is a CLEAN stop (SIGINT / CTRL_BREAK → uvicorn → the lifespan marks ``clean_exit``)
and takes seconds, below ``min_outage_sec``, so it leaves no «не работал» gap in the journal.
⚠️ A new ``supervisor.py`` is copied too, but the running one keeps its old code until the next manual
start: restarting itself would leave nothing to roll back a bad supervisor.
⚠️ A folder with ``.git`` (a developer's clone) is never overwritten: updates are off there.
"""

from __future__ import annotations

import filecmp
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from logging.handlers import RotatingFileHandler
from typing import Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(ROOT, "config.json")
RUNTIME_DIR = os.path.join(ROOT, "runtime")
UPDATE_DIR = os.path.join(RUNTIME_DIR, "update")
STATE_PATH = os.path.join(RUNTIME_DIR, "update.json")

REPO = "Anton-Shvetsov/internet-stability"
# Overridable for a test against a local server.
SHA_URL = os.environ.get("MONITOR_UPDATE_SHA_URL") or f"https://api.github.com/repos/{REPO}/commits/main"
ZIP_URL = os.environ.get("MONITOR_UPDATE_ZIP_URL") or f"https://github.com/{REPO}/archive/{{sha}}.zip"

#: Top-level names an update never touches: the user's settings, history and Python.
KEEP = {"config.json", "data", "runtime", ".git"}

DEFAULT_CHECK_MIN = 60
MIN_CHECK_MIN, MAX_CHECK_MIN = 10, 1440
#: The first check after the start: soon, but not while the child is still starting.
FIRST_CHECK_SEC = 60
HTTP_TIMEOUT_SEC = 60
#: How long the child gets to shut down cleanly before it is killed.
STOP_TIMEOUT_SEC = 20
#: A child alive this long after an update counts as working.
PROBATION_SEC = 20
#: A child that dies this soon after the very first start (port busy, not installed) ends the supervisor.
EARLY_EXIT_SEC = 10
RESTART_PAUSE_SEC, RESTART_PAUSE_MAX_SEC = 10, 300

WINDOWS = os.name == "nt"

logger = logging.getLogger("supervisor")


def _setup_logging() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [supervisor] %(message)s")
    file_handler = RotatingFileHandler(os.path.join(DATA_DIR, "supervisor.log"), maxBytes=1_000_000,
                                       backupCount=2, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    logger.setLevel(logging.INFO)


def check_interval_min() -> int:
    """``update_check_min`` from ``config.json``: 0 — off, otherwise clamped to 10…1440."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            value = int(json.load(f).get("update_check_min", DEFAULT_CHECK_MIN))
    except FileNotFoundError:
        return DEFAULT_CHECK_MIN
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_CHECK_MIN
    if value <= 0:
        return 0
    return max(MIN_CHECK_MIN, min(MAX_CHECK_MIN, value))


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def _get(url: str, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "internet-stability-updater"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        return resp.read()


def latest_sha() -> str:
    sha = _get(SHA_URL, "application/vnd.github.sha").decode("ascii", "replace").strip()
    if not (7 <= len(sha) <= 64 and all(c in "0123456789abcdef" for c in sha)):
        raise ValueError(f"unexpected answer instead of a SHA: {sha[:80]!r}")
    return sha


def download(sha: str, into: str) -> str:
    """The archive of ``sha`` unpacked under ``into`` → the project folder in it (the one with app.py)."""
    archive = os.path.join(into, "project.zip")
    with open(archive, "wb") as f:
        f.write(_get(ZIP_URL.format(sha=sha), "application/zip"))
    with zipfile.ZipFile(archive) as z:
        z.extractall(os.path.join(into, "project"))
    base = os.path.join(into, "project")
    for name in os.listdir(base):  # GitHub's archive holds a single <repo>-<sha>/ folder
        if os.path.isfile(os.path.join(base, name, "app.py")):
            return os.path.join(base, name)
    raise ValueError("the archive has no app.py")


def project_files(src: str) -> list[str]:
    """The relative paths of the files in ``src`` an update copies."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in KEEP]
            filenames = [f for f in filenames if f not in KEEP]
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            out.append(os.path.normpath(os.path.join(rel_dir, name)))
    return sorted(out)


def _copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    if not WINDOWS and dst.endswith((".sh", ".command")):
        os.chmod(dst, 0o755)  # the zip does not carry the executable bit


class Supervisor:
    def __init__(self):
        self.child: Optional[subprocess.Popen] = None
        self.started = 0.0
        self.state = load_state()

    # --- the child ----------------------------------------------------------------------------------
    def start_child(self) -> None:
        env = dict(os.environ)
        sha = self.state.get("installed")
        if sha:
            env["MONITOR_VERSION"] = sha[:7]
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}
        self.child = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")], cwd=ROOT, env=env,
                                      **kwargs)
        self.started = time.monotonic()
        logger.info("monitor started (pid %s, version %s)", self.child.pid, sha[:7] if sha else "unknown")

    def stop_child(self, signal_it: bool = True) -> None:
        """A clean stop: the signal uvicorn shuts down on, then a kill if it hangs.

        ``signal_it=False`` — the child already got the signal (Ctrl+C reaches the whole process group
        on macOS / Linux) and must not get a second one: uvicorn's second SIGINT skips the lifespan's
        shutdown, so the clean exit would not be marked."""
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        if signal_it:
            try:
                child.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
            except OSError:
                pass
        try:
            child.wait(timeout=STOP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            logger.warning("monitor did not stop in %s s, killing it", STOP_TIMEOUT_SEC)
            child.kill()
            child.wait()

    # --- updates ------------------------------------------------------------------------------------
    def check_update(self) -> None:
        sha = latest_sha()
        if sha == self.state.get("installed") or sha in self.state.get("bad", []):
            return
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)
        os.makedirs(UPDATE_DIR)
        try:
            src = download(sha, tempfile.mkdtemp(dir=UPDATE_DIR))
            self.apply(sha, src)
        finally:
            shutil.rmtree(UPDATE_DIR, ignore_errors=True)

    def apply(self, sha: str, src: str) -> None:
        changed = [f for f in project_files(src)
                   if not (os.path.isfile(os.path.join(ROOT, f))
                           and filecmp.cmp(os.path.join(src, f), os.path.join(ROOT, f), shallow=False))]
        if not changed:
            # The code already is this commit (a fresh install): only remember it, no restart.
            self._installed(sha)
            logger.info("code is up to date with %s", sha[:7])
            return
        logger.info("updating to %s: %s", sha[:7], ", ".join(changed))
        backup = os.path.join(UPDATE_DIR, "backup")
        old = self.state.get("installed")
        added = []
        self.stop_child()
        for f in changed:
            if os.path.isfile(os.path.join(ROOT, f)):
                _copy(os.path.join(ROOT, f), os.path.join(backup, f))
            else:
                added.append(f)
        try:
            for f in changed:
                _copy(os.path.join(src, f), os.path.join(ROOT, f))
            if "requirements.txt" in changed:
                self.pip_install()
            self._installed(sha)
            self.start_child()
            if self._survives(PROBATION_SEC):
                logger.info("updated to %s", sha[:7])
                if "supervisor.py" in changed:
                    logger.info("the new supervisor.py takes effect on the next start of the monitor")
                return
            logger.error("the monitor exited right after the update to %s", sha[:7])
        except Exception:
            logger.exception("update to %s failed", sha[:7])
        self.rollback(sha, old, backup, changed, added)

    def rollback(self, sha: str, old: Optional[str], backup: str, changed: list[str],
                 added: list[str]) -> None:
        logger.warning("rolling back %s: it will not be installed again", sha[:7])
        self.stop_child()
        for f in changed:
            if f in added:
                try:
                    os.remove(os.path.join(ROOT, f))
                except OSError:
                    pass
            else:
                _copy(os.path.join(backup, f), os.path.join(ROOT, f))
        self.state["installed"] = old
        self.state["bad"] = (self.state.get("bad", []) + [sha])[-20:]
        save_state(self.state)
        if "requirements.txt" in changed:
            try:
                self.pip_install()
            except Exception:
                logger.exception("restoring the old requirements failed")
        self.start_child()

    def _installed(self, sha: str) -> None:
        self.state["installed"] = sha
        save_state(self.state)

    def _survives(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.child is None or self.child.poll() is not None:
                return False
            time.sleep(0.5)
        return True

    @staticmethod
    def pip_install() -> None:
        logger.info("installing the requirements")
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade",
                        "-r", os.path.join(ROOT, "requirements.txt")], cwd=ROOT, check=True)

    # --- the loop -----------------------------------------------------------------------------------
    def run(self) -> int:
        updates = not os.path.exists(os.path.join(ROOT, ".git"))
        if not updates:
            logger.info("a git clone (.git found): automatic updates are off here")
        self.start_child()
        first_run = True
        pause = RESTART_PAUSE_SEC
        next_check = time.monotonic() + FIRST_CHECK_SEC
        while True:
            rc = self.child.poll() if self.child else None
            if rc is not None:
                lived = time.monotonic() - self.started
                self.child = None
                if first_run and lived < EARLY_EXIT_SEC:
                    return rc
                pause = RESTART_PAUSE_SEC if lived > RESTART_PAUSE_MAX_SEC else pause
                logger.error("monitor exited with code %s, restarting in %s s", rc, pause)
                time.sleep(pause)
                pause = min(pause * 2, RESTART_PAUSE_MAX_SEC)
                self.start_child()
                continue
            if first_run and time.monotonic() - self.started >= EARLY_EXIT_SEC:
                first_run = False
            if updates and time.monotonic() >= next_check:
                interval = check_interval_min()
                if interval:
                    try:
                        self.check_update()
                    except Exception as e:
                        logger.warning("update check failed: %s", e)
                # Off: look at the setting again in a minute, so switching it on needs no restart.
                next_check = time.monotonic() + (interval or 1) * 60
            time.sleep(1)


class _Terminate(Exception):
    pass


def _on_sigterm(_signum, _frame):
    raise _Terminate()


def main() -> None:
    _setup_logging()
    if not WINDOWS:
        signal.signal(signal.SIGTERM, _on_sigterm)
    sup = Supervisor()
    rc = 0
    try:
        rc = sup.run()
    except KeyboardInterrupt:
        # macOS / Linux: the child got the same Ctrl+C. Windows: it is in its own group and did not.
        sup.stop_child(signal_it=WINDOWS)
    except _Terminate:
        sup.stop_child()
    except BaseException:
        logger.exception("supervisor failed")
        sup.stop_child()
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
