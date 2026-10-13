"""``config.json``: the server address (read at start) and the probe settings (edited on the page,
read by the monitor on every tick, so a save applies without a restart).

A missing file is written with the defaults on the first start; a broken or partial one falls back
to the default per field.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

from monitor.paths import CONFIG_PATH
from monitor.store import write_json

logger = logging.getLogger("monitor")

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8058,
    "probe_interval_sec": 60,
    "min_outage_sec": 180,
    "net_targets": ["https://ya.ru", "https://1.1.1.1"],
    "vpn_targets": ["https://telegram.org", "https://www.instagram.com"],
    "speedtest_enabled": False,
    "speedtest_interval_min": 60,
}

#: The keys the page may change; ``host``/``port`` are edited in the file (they need a restart).
EDITABLE = ("probe_interval_sec", "min_outage_sec", "net_targets", "vpn_targets",
            "speedtest_enabled", "speedtest_interval_min")
MAX_TARGETS = 10


class ConfigError(ValueError):
    """A value the page sent is unusable; the text is shown to the user as is."""


def _int(value, lo: int, hi: int, what: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what}: нужно целое число")
    if not lo <= out <= hi:
        raise ConfigError(f"{what}: допустимо от {lo} до {hi}")
    return out


def _bool(value, what: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1) or str(value).strip().lower() in ("true", "false", "0", "1"):
        return str(value).strip().lower() in ("true", "1")
    raise ConfigError(f"{what}: нужно да или нет")


def _targets(value, what: str) -> list[str]:
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{what}: нужен список адресов")
    out = []
    for raw in value:
        url = str(raw).strip()
        if not url:
            continue
        if "://" not in url:
            url = "https://" + url
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigError(f"{what}: «{raw}» — не адрес сайта")
        if url not in out:
            out.append(url)
    if not out:
        raise ConfigError(f"{what}: нужен хотя бы один адрес")
    if len(out) > MAX_TARGETS:
        raise ConfigError(f"{what}: не больше {MAX_TARGETS} адресов")
    return out


def validate(data: dict) -> dict:
    """The editable fields of ``data`` → normalised values; raises ``ConfigError``."""
    out = {}
    if "probe_interval_sec" in data:
        out["probe_interval_sec"] = _int(data["probe_interval_sec"], 10, 3600, "Интервал проверки")
    if "min_outage_sec" in data:
        out["min_outage_sec"] = _int(data["min_outage_sec"], 0, 86400, "Порог падения")
    if "net_targets" in data:
        out["net_targets"] = _targets(data["net_targets"], "Цели интернета")
    if "vpn_targets" in data:
        out["vpn_targets"] = _targets(data["vpn_targets"], "Цели VPN")
    if "speedtest_enabled" in data:
        out["speedtest_enabled"] = _bool(data["speedtest_enabled"], "Замер скорости")
    if "speedtest_interval_min" in data:
        out["speedtest_interval_min"] = _int(data["speedtest_interval_min"], 15, 1440,
                                             "Интервал замера скорости")
    return out


def _read_file() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.exception("config.json is unreadable — using the defaults")
        return {}
    return data if isinstance(data, dict) else {}


def load() -> dict:
    """The whole config, every field valid (a bad field → its default)."""
    raw = _read_file()
    cfg = dict(DEFAULTS)
    host = raw.get("host")
    if isinstance(host, str) and host.strip():
        cfg["host"] = host.strip()
    try:
        port = int(raw.get("port", DEFAULTS["port"]))
        if 1 <= port <= 65535:
            cfg["port"] = port
    except (TypeError, ValueError):
        pass
    for key in EDITABLE:
        if key in raw:
            try:
                cfg.update(validate({key: raw[key]}))
            except ConfigError as e:
                logger.warning("config.json: %s — the default is used", e)
    return cfg


def ensure_file() -> None:
    """First start: write the defaults so ``host``/``port`` are visible in the file."""
    if not os.path.exists(CONFIG_PATH):
        try:
            write_json(CONFIG_PATH, DEFAULTS)
        except OSError:
            logger.exception("could not write %s", CONFIG_PATH)


def save(changes: dict) -> dict:
    """Validate the page's changes and merge them into the file (other keys stay). → the new config."""
    clean = validate(changes)
    raw = _read_file()
    raw.update(clean)
    write_json(CONFIG_PATH, raw)
    return load()


def public_view(cfg: dict) -> dict:
    return {k: cfg[k] for k in EDITABLE} | {"host": cfg["host"], "port": cfg["port"]}
