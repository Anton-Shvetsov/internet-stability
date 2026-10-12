"""The DECISIONS of the monitor — pure, no I/O, no clock of its own (every function takes ``now``).

Two kinds of outage (``kind``), each a ``{start, end}`` interval in aware UTC (stored as ISO ``…Z``):

* ``net`` — the internet group of probes did not answer at all;
* ``vpn`` — the internet answered, the VPN-only group did not.

A third kind, ``gap``, is not an outage: the monitor itself was not running (``sub``: ``stop`` — a
clean stop, ``crash`` — the window was closed / the machine was switched off, ``sleep`` — the machine
slept, ``unknown`` — a gap too long to trust). Nothing is known about the internet then, so a gap is
shown grey and is neither downtime nor observed time.

⚠️ Durations are combined by UNION, never by sum: a VPN outage never overlaps an internet one by
construction, but a sum is still the wrong habit to start.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

KINDS = ("net", "vpn")

#: A gap since the last «alive» stamp longer than this is not trusted: it is what copying an old
#: ``data/`` folder to another device (or a machine left off for a month) looks like.
MAX_GAP_SEC = 30 * 24 * 3600


# --- time helpers -----------------------------------------------------------------------------------

def parse_ts(value) -> Optional[datetime]:
    """An ISO string / datetime → aware UTC, or ``None``. A naive value is UTC (our storage rule)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """Aware UTC → ``YYYY-MM-DDTHH:MM:SSZ`` (seconds are enough for outages)."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


# --- the probe tracker ------------------------------------------------------------------------------

class ProbeTracker:
    """One group of probes → incidents.

    An outage starts at the first failed probe, is CONFIRMED once it has lasted ``min_outage_sec`` and
    ends at the first successful probe. One that never got confirmed is forgotten — a blink of the
    network is not an incident.

    ``feed`` returns a list of events: ``{"type": "confirmed", "start", "detail"}`` (persist it open),
    ``{"type": "update", "start", "last_seen", "detail"}`` (still down — refresh ``last_seen``),
    ``{"type": "closed", "start", "end", "detail"}``.
    """

    def __init__(self, min_outage_sec: int):
        self.min_outage_sec = max(0, int(min_outage_sec))
        self.first_fail: Optional[datetime] = None
        self.last_fail: Optional[datetime] = None
        self.detail = ""
        self.confirmed = False

    @property
    def down(self) -> bool:
        """Failing right now (confirmed or not)."""
        return self.first_fail is not None

    def _reset(self) -> None:
        self.first_fail = self.last_fail = None
        self.detail = ""
        self.confirmed = False

    def feed(self, ok: bool, ts: datetime, detail: str = "") -> list[dict]:
        if ok:
            if self.first_fail is None:
                return []
            out = []
            if self.confirmed:
                out.append({"type": "closed", "start": self.first_fail, "end": ts, "detail": self.detail})
            self._reset()
            return out
        if self.first_fail is None:
            self.first_fail = ts
        self.last_fail = ts
        if detail:
            self.detail = detail
        if not self.confirmed:
            if (ts - self.first_fail).total_seconds() >= self.min_outage_sec:
                self.confirmed = True
                return [{"type": "confirmed", "start": self.first_fail, "last_seen": ts,
                         "detail": self.detail}]
            return []
        return [{"type": "update", "start": self.first_fail, "last_seen": ts, "detail": self.detail}]

    def cut(self, ts: datetime) -> list[dict]:
        """End whatever is open AT ``ts`` without a successful probe: a wider outage took over (the
        internet went, so the VPN group's silence means nothing any more) or the machine slept. A
        pending failure is forgotten — the time it would have needed to confirm belongs elsewhere."""
        if self.first_fail is None:
            return []
        out = []
        if self.confirmed:
            end = max(ts, self.first_fail)
            out.append({"type": "closed", "start": self.first_fail, "end": end, "detail": self.detail})
        self._reset()
        return out


# --- when the monitor itself was not running --------------------------------------------------------

def start_gap(state: dict, now: datetime, min_outage_sec: int) -> Optional[dict]:
    """The gap before this start, judged from the previous run's state file.

    ``stop`` — the previous run ran its clean exit; ``crash`` — it did not (the console window closed,
    a power cut, a reboot); ``unknown`` — a gap longer than ``MAX_GAP_SEC``. ``None`` — no previous
    run, or a gap below the threshold (a quick restart).
    """
    alive = parse_ts((state or {}).get("alive_at"))
    if alive is None or now <= alive:
        return None
    gap = (now - alive).total_seconds()
    if gap < max(0, int(min_outage_sec)):
        return None
    if gap > MAX_GAP_SEC:
        sub = "unknown"
    elif state.get("clean_exit"):
        sub = "stop"
    else:
        sub = "crash"
    return {"kind": "gap", "sub": sub, "start": iso(alive), "end": iso(now), "detail": ""}


def tick_gap(prev_mono: Optional[float], prev_wall: Optional[datetime], now_mono: float,
             now_wall: datetime, expected_sec: float, min_outage_sec: int) -> Optional[dict]:
    """The process did not run between two ticks (a sleeping machine, a frozen console).

    Both clocks: the monotonic one ignores an NTP step but does not advance while the machine is
    suspended on Linux/macOS; the wall clock is the only witness of a sleep. The excess over the
    expected pause must reach the threshold."""
    if prev_mono is None or prev_wall is None:
        return None
    gap = max(now_mono - prev_mono, (now_wall - prev_wall).total_seconds())
    if gap - max(0.0, float(expected_sec)) < max(0, int(min_outage_sec)):
        return None
    start = now_wall - timedelta(seconds=gap)
    return {"kind": "gap", "sub": "sleep", "start": iso(start), "end": iso(now_wall), "detail": ""}


# --- interval arithmetic ----------------------------------------------------------------------------

Interval = tuple  # (start: datetime, end: datetime)


def union(intervals: Iterable[Interval]) -> list[Interval]:
    items = sorted((s, e) for s, e in intervals if e > s)
    out: list[list] = []
    for s, e in items:
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def measure(intervals: Iterable[Interval]) -> float:
    return sum((e - s).total_seconds() for s, e in union(intervals))


def subtract(interval: Interval, holes: list[Interval]) -> list[Interval]:
    """``interval`` minus a UNIONED list of holes."""
    s, e = interval
    out = []
    cur = s
    for hs, he in holes:
        if he <= cur or hs >= e:
            continue
        if hs > cur:
            out.append((cur, min(hs, e)))
        cur = max(cur, he)
        if cur >= e:
            break
    if cur < e:
        out.append((cur, e))
    return out


def subtract_all(intervals: Iterable[Interval], holes: Iterable[Interval]) -> list[Interval]:
    """A list of intervals minus a list of holes (both unioned here)."""
    holes = union(holes)
    out: list[Interval] = []
    for iv in union(intervals):
        out += subtract(iv, holes)
    return out


def clip(interval: Interval, lo: datetime, hi: datetime) -> Optional[Interval]:
    s, e = max(interval[0], lo), min(interval[1], hi)
    return (s, e) if e > s else None


def local_day_pieces(s: datetime, e: datetime, offset_min: int):
    """Split [s, e) at LOCAL midnights of a fixed UTC offset → [(local date, from_sec, to_sec)], the
    seconds counted from that day's local midnight (a fixed offset ⇒ every day is 86 400 s)."""
    off = timedelta(minutes=int(offset_min))
    out = []
    cur = s
    while cur < e:
        local = cur + off
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        nxt = midnight + timedelta(days=1) - off
        end = min(e, nxt)
        frm = (local - midnight).total_seconds()
        out.append((local.date(), frm, frm + (end - cur).total_seconds()))
        cur = end
    return out


def local_day_slices(s: datetime, e: datetime, offset_min: int):
    """Split [s, e) at LOCAL midnights of a fixed UTC offset → [(local date, seconds)]."""
    return [(d, to - frm) for d, frm, to in local_day_pieces(s, e, offset_min)]


#: The per-day timeline colours each moment by its most senior state only: while the monitor did not
#: run nothing is known (grey), without the internet the VPN is not even probed.
LAYER_ORDER = ("gap", "net", "vpn")


# --- the summary for the page -----------------------------------------------------------------------

def summarize(incidents: list[dict], *, observed_from, since: datetime, until: datetime,
              tz_offset_min: int = 0) -> dict:
    """Everything the page draws for the period [since, until].

    * the observed time starts at ``observed_from`` (the monitor's first run — nothing before it is
      known) and excludes every ``gap``;
    * the VPN is observed only while the internet is up (it is not probed otherwise);
    * the total downtime is the union of the internet and VPN outages;
    * ``daily`` is a 0–24 h timeline per local day, each moment coloured by its most senior state
      (``LAYER_ORDER``); ``since_last_down`` is the clean stretch since the last outage.
    """
    obs = parse_ts(observed_from) or until
    lo = max(since, obs)
    hi = until
    if hi <= lo:
        lo = hi

    rows = []
    for inc in incidents:
        s = parse_ts(inc.get("start"))
        e = parse_ts(inc.get("end")) or until  # still open: the probe keeps failing until now
        if s is None or e <= s:
            continue
        rows.append({"inc": inc, "s": s, "e": e, "kind": inc.get("kind"), "sub": inc.get("sub") or ""})

    gaps = union((r["s"], r["e"]) for r in rows if r["kind"] == "gap")
    gaps_in = [c for c in (clip(g, lo, hi) for g in gaps) if c]

    per_kind: dict[str, list[Interval]] = {k: [] for k in KINDS}
    listed = []
    for r in sorted(rows, key=lambda r: r["s"], reverse=True):
        if not clip((r["s"], r["e"]), since, hi):
            continue
        kind = r["kind"]
        counted = kind in KINDS
        effective = 0.0
        if counted:
            pieces = subtract((r["s"], r["e"]), gaps)
            in_period = [c for c in (clip(p, lo, hi) for p in pieces) if c]
            per_kind[kind] += in_period
            effective = measure(in_period)
        inc = r["inc"]
        listed.append({
            "id": inc.get("id"), "kind": kind, "sub": r["sub"],
            "start": iso(r["s"]), "end": iso(parse_ts(inc.get("end"))) if inc.get("end") else None,
            "open": not inc.get("end"),
            "duration_sec": int((r["e"] - r["s"]).total_seconds()),
            "effective_sec": int(effective),
            "counted": counted and effective > 0,
            "detail": inc.get("detail") or "",
        })

    span = max(0.0, (hi - lo).total_seconds())
    observed = span - measure(gaps_in)
    net_down = measure(per_kind["net"])
    vpn_down = measure(per_kind["vpn"])
    down_total = measure(per_kind["net"] + per_kind["vpn"])

    def pct(down: float, total: float) -> Optional[float]:
        if total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - down / total)))

    # The per-day timeline: every kind minus the senior ones (LAYER_ORDER), so each moment has one
    # colour. Positions are seconds since the LOCAL midnight; `span_*` is the observed part of the day
    # (the first day starts at the observation / the period, today ends now).
    layer_src = {"gap": gaps_in, "net": per_kind["net"], "vpn": per_kind["vpn"]}
    layers: dict[str, list[Interval]] = {}
    senior: list[Interval] = []
    for key in LAYER_ORDER:
        layers[key] = subtract_all(layer_src[key], senior)
        senior += layers[key]
    days: dict = {}
    if hi > lo:
        for d, frm, to in local_day_pieces(lo, hi, tz_offset_min):
            days[d] = {k: 0.0 for k in LAYER_ORDER} | {"span_from": frm, "span_to": to, "segments": []}
    for key, items in layers.items():
        for s, e in items:
            for d, frm, to in local_day_pieces(s, e, tz_offset_min):
                if d in days:
                    days[d][key] += to - frm
                    days[d]["segments"].append({"kind": key, "from": int(round(frm)), "to": int(round(to))})
    daily = [{"date": d.isoformat(), **{k: int(vals[k]) for k in LAYER_ORDER},
              "span_from": int(round(vals["span_from"])), "span_to": int(round(vals["span_to"])),
              "segments": sorted(vals["segments"], key=lambda g: g["from"])}
             for d, vals in sorted(days.items(), reverse=True)]

    # «Без падений»: since the end of the LAST outage over the whole journal (not the period: a clean
    # streak can be longer than it), its part outside the gaps; an outage open right now ⇒ None.
    # A gap is not an outage and does not reset it.
    last_end = None
    open_now = False
    for r in rows:
        if r["kind"] not in KINDS:
            continue
        pieces = subtract((r["s"], r["e"]), gaps)
        if not pieces:
            continue
        if not r["inc"].get("end"):
            open_now = True
        end = max(e for _s, e in pieces)
        if last_end is None or end > last_end:
            last_end = end
    since_last_down = None
    if not open_now:
        if last_end is not None:
            since_last_down = {"end": iso(last_end), "never": False,
                               "sec": int(max(0.0, (until - last_end).total_seconds()))}
        elif parse_ts(observed_from):
            since_last_down = {"end": iso(obs), "never": True,
                               "sec": int(max(0.0, (until - obs).total_seconds()))}

    counted_rows = [x for x in listed if x["counted"]]
    return {
        "observed_from": iso(obs) if parse_ts(observed_from) else None,
        "uptime": {
            "all": pct(down_total, observed),
            "net": pct(net_down, observed),
            "vpn": pct(vpn_down, observed - net_down),
        },
        "count": len(counted_rows),
        "down_sec": int(down_total),
        "longest_sec": max((x["effective_sec"] for x in counted_rows), default=0),
        "gap_sec": int(measure(gaps_in)),
        "incidents": listed,
        "daily": daily,
        "since_last_down": since_last_down,
    }
