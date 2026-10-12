"""The monitor's files under ``data/`` (the history of THIS machine).

* ``incidents.json`` — ``{"incidents": [...]}``: the outages and the gaps, pruned to ``RETENTION_DAYS``;
* ``state.json``     — the run's bookkeeping: ``observed_from``, ``alive_at``, ``clean_exit``, the last
  probe per target.

⚠️ Every writer swallows and logs its error: on Windows ``os.replace`` fails now and then while an
antivirus or a backup is reading the same file, and the monitor's loop must not die of it.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from monitor import core
from monitor.paths import DATA_DIR

logger = logging.getLogger("monitor")

RETENTION_DAYS = 400

_cache: dict = {}   # path -> (mtime, data)


def incidents_path() -> str:
    return os.path.join(DATA_DIR, "incidents.json")


def state_path() -> str:
    return os.path.join(DATA_DIR, "state.json")


def write_json(path: str, data) -> None:
    """Atomic write (temp file + ``os.replace``): a crash mid-write leaves the old file whole."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _save(path: str, data) -> bool:
    try:
        write_json(path, data)
    except Exception:
        logger.exception("write failed: %s", path)
        return False
    _cache.pop(path, None)
    return True


# --- the incident journal ---------------------------------------------------------------------------

def _shape(data) -> dict:
    if not isinstance(data, dict):
        data = {}
    return {"incidents": [i for i in (data.get("incidents") or []) if isinstance(i, dict)]}


def _read_journal() -> tuple[dict, bool]:
    """→ (journal, writable).

    ⚠️ Strict: reading any failure as «empty» would let the next write replace months of history
    with one incident.
    * the file does not parse → it is moved aside (``…corrupt-<ts>.json``, nothing is lost) and the
      journal starts empty;
    * the file cannot be READ right now → ``writable=False``: the caller skips its write rather than
      overwrite what it could not see.
    """
    path = incidents_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"incidents": []}, not os.path.exists(path)
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return _shape(hit[1]), True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        aside = f"{path[:-5]}.corrupt-{stamp}.json"
        try:
            os.replace(path, aside)
            logger.error("%s did not parse — moved aside to %s", path, aside)
        except OSError:
            logger.exception("could not move aside the damaged %s", path)
            return {"incidents": []}, False
        _cache.pop(path, None)
        return {"incidents": []}, True
    except OSError:
        logger.warning("%s is not readable right now — the write is skipped", path)
        return {"incidents": []}, False
    _cache[path] = (mtime, data)
    return _shape(data), True


def load_journal() -> dict:
    """For the readers (the page): whatever could be read."""
    return _read_journal()[0]


def _write_journal(journal: dict) -> bool:
    floor = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    def keep(i):
        end = core.parse_ts(i.get("end"))
        return end is None or end >= floor

    return _save(incidents_path(), {"incidents": [i for i in journal["incidents"] if keep(i)]})


def add_incident(incident: dict) -> str:
    """Append one incident (a copy, with a fresh ``id`` unless it has one). Returns the id — even when
    the write had to be skipped (the next update of the same id then finds nothing, which is harmless)."""
    inc = dict(incident)
    inc.setdefault("id", uuid.uuid4().hex[:12])
    journal, ok = _read_journal()
    if ok:
        _write_journal({"incidents": journal["incidents"] + [inc]})
    return inc["id"]


def update_incident(incident_id: str, **fields) -> bool:
    journal, ok = _read_journal()
    if not ok:
        return False
    found = False
    incidents = []
    for i in journal["incidents"]:
        if i.get("id") == incident_id:
            i = {**i, **fields}
            found = True
        incidents.append(i)
    if found:
        _write_journal({"incidents": incidents})
    return found


def close_open_incidents() -> int:
    """At start: an incident still open belonged to a run that died mid-outage — close it at the last
    moment it was seen. Returns how many were closed."""
    journal, ok = _read_journal()
    if not ok:
        return 0
    closed = 0
    incidents = []
    for i in journal["incidents"]:
        if not i.get("end"):
            i = {**i, "end": i.get("last_seen") or i.get("start")}
            closed += 1
        incidents.append(i)
    if closed:
        _write_journal({"incidents": incidents})
    return closed


# --- the run state ----------------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> bool:
    return _save(state_path(), state)
