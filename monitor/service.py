"""The monitor's loop and the page's data.

Every ``probe_interval_sec``: two groups of HTTPS probes (the internet / the VPN-only resources), the
sleep detector and the «alive» stamp the next start judges the gap by.

⚠️ The probes go DIRECTLY (``trust_env=False``): the VPN is a system client on the machine, so a direct
request sees exactly what the machine's programs see. A probe is an anonymous ``HEAD`` to a website.
⚠️ A group is alive when ANY of its targets answered. So a «VPN-only» target that in fact opens
without the VPN keeps the group alive forever and VPN outages go unseen: the page shows the last probe
of every target, and the targets are checked once by switching the VPN off.
⚠️ No keep-alive: a probe over a pooled connection does no new TLS handshake, so it would keep
answering «reachable» after DPI (or a dead tunnel) started refusing NEW connections.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from monitor import config, core, store

logger = logging.getLogger("monitor")

PROBE_TIMEOUT_SEC = 8.0
#: The whole group is bounded too: a DNS lookup runs in a thread httpx cannot interrupt, and a hung
#: resolver must not stretch a tick.
GROUP_TIMEOUT_SEC = PROBE_TIMEOUT_SEC + 4.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short(url: str) -> str:
    return url.split("://", 1)[-1].rstrip("/")


def _classify(exc: Exception) -> str:
    """A probe error in one word for the «причина» line."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    text = str(exc).lower()
    if any(k in text for k in ("name or service", "nodename", "getaddrinfo", "name resolution",
                               "no address associated", "11001", "temporary failure in name")):
        return "dns"
    if any(k in text for k in ("ssl", "certificate", "tls")):
        return "tls"
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    return type(exc).__name__


async def _probe(client: httpx.AsyncClient, url: str) -> dict:
    t0 = time.monotonic()
    try:
        await client.head(url)
        return {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "error": ""}
    except Exception as e:  # any HTTP answer is «reachable»; only a failure to get one lands here
        return {"ok": False, "ms": int((time.monotonic() - t0) * 1000), "error": _classify(e)}


async def _probe_group(client: httpx.AsyncClient, urls: list[str]) -> tuple[bool, str, dict]:
    """→ (alive, detail of the failures, {url: result})."""
    try:
        results = await asyncio.wait_for(asyncio.gather(*(_probe(client, u) for u in urls)),
                                         timeout=GROUP_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        results = [{"ok": False, "ms": int(GROUP_TIMEOUT_SEC * 1000), "error": "timeout"} for _ in urls]
    by_url = dict(zip(urls, results))
    alive = any(r["ok"] for r in results)
    detail = "; ".join(f"{_short(u)}: {r['error']}" for u, r in by_url.items() if not r["ok"])
    return alive, detail, by_url


class Monitor:
    def __init__(self):
        cfg = config.load()
        self.net = core.ProbeTracker(cfg["min_outage_sec"])
        self.vpn = core.ProbeTracker(cfg["min_outage_sec"])
        self.open_ids: dict[str, Optional[str]] = {"net": None, "vpn": None}
        self.state: dict = {}
        self.prev_mono: Optional[float] = None
        self.prev_wall: Optional[datetime] = None
        self.last_tick: Optional[datetime] = None

    # --- start / stop -------------------------------------------------------------------------------
    def start(self) -> None:
        """Judge the previous run, then reset the exit flag AT ONCE — a crash of THIS run must not
        inherit the previous run's «clean»."""
        prev = store.load_state()
        now = _now()
        store.close_open_incidents()
        gap = core.start_gap(prev, now, config.load()["min_outage_sec"])
        if gap:
            store.add_incident(gap)
            logger.warning("the monitor was not running %s → %s (%s)", gap["start"], gap["end"], gap["sub"])
        self.state = {
            "observed_from": prev.get("observed_from") or core.iso(now),
            "alive_at": core.iso(now),
            "clean_exit": False,
            "probes": prev.get("probes") or {},
        }
        store.save_state(self.state)

    def mark_clean_exit(self) -> None:
        now = _now()
        self._apply("net", self.net.cut(now))
        self._apply("vpn", self.vpn.cut(now))
        self.state["clean_exit"] = True
        self.state["alive_at"] = core.iso(now)
        store.save_state(self.state)

    # --- events → the journal -----------------------------------------------------------------------
    def _apply(self, kind: str, events: list[dict]) -> None:
        for ev in events:
            if ev["type"] == "confirmed":
                self.open_ids[kind] = store.add_incident({
                    "kind": kind, "sub": "", "start": core.iso(ev["start"]), "end": None,
                    "last_seen": core.iso(ev.get("last_seen") or ev["start"]),
                    "detail": ev.get("detail") or ""})
                logger.warning("%s outage since %s (%s)", kind, core.iso(ev["start"]), ev.get("detail") or "")
                if kind == "net":
                    # The internet went: the VPN group's silence means nothing any more.
                    self._apply("vpn", self.vpn.cut(ev["start"]))
            elif ev["type"] == "update" and self.open_ids.get(kind):
                store.update_incident(self.open_ids[kind], last_seen=core.iso(ev["last_seen"]),
                                      detail=ev.get("detail") or "")
            elif ev["type"] == "closed" and self.open_ids.get(kind):
                store.update_incident(self.open_ids[kind], end=core.iso(ev["end"]),
                                      last_seen=core.iso(ev["end"]), detail=ev.get("detail") or "")
                logger.warning("%s outage over (%s → %s)", kind, core.iso(ev["start"]), core.iso(ev["end"]))
                self.open_ids[kind] = None

    # --- one tick -----------------------------------------------------------------------------------
    async def tick(self, client: httpx.AsyncClient) -> None:
        try:
            await self._tick(client)
        finally:
            # ⚠️ The sleep detector's anchor moves even when the tick failed: a tick that raised every
            # time would otherwise leave the anchor behind and report a fresh «sleep» on every tick.
            self.prev_mono, self.prev_wall = time.monotonic(), _now()

    async def _tick(self, client: httpx.AsyncClient) -> None:
        cfg = config.load()
        self.net.min_outage_sec = self.vpn.min_outage_sec = cfg["min_outage_sec"]
        now_mono, now = time.monotonic(), _now()
        # The expected pause is the interval at the previous wait, not a freshly saved one.
        expected = self.state.get("interval_sec") or cfg["probe_interval_sec"]
        gap = core.tick_gap(self.prev_mono, self.prev_wall, now_mono, now, expected, cfg["min_outage_sec"])
        if gap:
            store.add_incident(gap)
            logger.warning("the process did not run %s → %s (sleep/freeze)", gap["start"], gap["end"])
            # A failure seen before the sleep must not be confirmed by the sleep's length.
            start = core.parse_ts(gap["start"])
            self._apply("net", self.net.cut(start))
            self._apply("vpn", self.vpn.cut(start))

        ts = _now()
        net_ok, net_detail, net_res = await _probe_group(client, cfg["net_targets"])
        self._apply("net", self.net.feed(net_ok, ts, net_detail))
        vpn_res = {}
        if net_ok:
            vpn_ok, vpn_detail, vpn_res = await _probe_group(client, cfg["vpn_targets"])
            self._apply("vpn", self.vpn.feed(vpn_ok, ts, vpn_detail))
        # While the internet is down (even unconfirmed) the VPN group is not fed: its silence would
        # only repeat the internet's.

        stamp = core.iso(_now())
        probes = {}
        for group, res in (("net", net_res), ("vpn", vpn_res)):
            for url, r in res.items():
                probes[url] = {**r, "group": group, "at": stamp}
        # A VPN target skipped this tick keeps its previous result (marked by its own `at`).
        old = self.state.get("probes") or {}
        for url, r in old.items():
            if url not in probes and url in cfg["vpn_targets"]:
                probes[url] = r
        self.state["probes"] = probes
        self.state["alive_at"] = stamp
        self.state["interval_sec"] = cfg["probe_interval_sec"]
        store.save_state(self.state)
        self.last_tick = _now()


_monitor: Optional[Monitor] = None
_wake: Optional[asyncio.Event] = None


def wake() -> None:
    """Probe now (after a settings save) instead of waiting for the interval."""
    if _wake is not None:
        _wake.set()


async def run_loop(stop_event: asyncio.Event) -> None:
    global _monitor, _wake
    m = Monitor()
    try:
        m.start()
    except Exception:
        logger.exception("start failed")
    _monitor = m
    _wake = asyncio.Event()
    client = httpx.AsyncClient(timeout=PROBE_TIMEOUT_SEC, follow_redirects=False, trust_env=False,
                               limits=httpx.Limits(max_keepalive_connections=0))
    clean = False
    try:
        while not stop_event.is_set():
            try:
                await m.tick(client)
            except Exception:
                logger.exception("tick failed")
            _wake.clear()
            waiters = [asyncio.ensure_future(stop_event.wait()), asyncio.ensure_future(_wake.wait())]
            try:
                await asyncio.wait(waiters, timeout=config.load()["probe_interval_sec"],
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
        clean = True
    except asyncio.CancelledError:
        clean = True
        raise
    finally:
        # Only a stop or a cancel is a clean exit. If THIS loop died of a bug, the gap reads as
        # «не работал» rather than «остановлен» — wrong too, but loud, and logged.
        _monitor = None
        _wake = None
        if clean:
            try:
                m.mark_clean_exit()
            except Exception:
                logger.exception("exit mark failed")
        else:
            logger.error("the monitor loop stopped unexpectedly — outages are not recorded")
        try:
            await client.aclose()
        except BaseException:
            pass


# --- the page's data --------------------------------------------------------------------------------

def stats(days: int, tz_offset_min: int) -> dict:
    now = _now()
    cfg = config.load()
    journal = store.load_journal()
    state = store.load_state()
    out = core.summarize(journal["incidents"], observed_from=state.get("observed_from"),
                         since=now - timedelta(days=int(days)), until=now, tz_offset_min=tz_offset_min)
    probes = state.get("probes") or {}
    out["probes"] = [
        {"group": group, "targets": [{"url": _short(u), **(probes.get(u) or {})} for u in urls]}
        for group, urls in (("net", cfg["net_targets"]), ("vpn", cfg["vpn_targets"]))
    ]
    m = _monitor
    live = {"running": m is not None, "last_tick": core.iso(m.last_tick) if m else None}
    if m is not None and m.last_tick is not None:
        for key, tracker in (("net", m.net), ("vpn", m.vpn)):
            live[key] = {"down": tracker.down, "confirmed": tracker.confirmed,
                         "since": core.iso(tracker.first_fail)}
        if m.net.down:
            live["vpn"] = None  # not probed while the internet is down
    out["live"] = live
    out["host"] = socket.gethostname()
    out["now"] = core.iso(now)
    out["interval_sec"] = cfg["probe_interval_sec"]
    out["min_outage_sec"] = cfg["min_outage_sec"]
    return out
