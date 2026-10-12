"""Internet stability monitor: probes the internet and the VPN, keeps a journal of outages and serves
a page with it. Run: ``python app.py`` (address and port in ``config.json``, default 0.0.0.0:8058)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
import sys
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from monitor import config, service
from monitor.paths import DATA_DIR, STATIC_DIR

DAYS_CHOICES = (1, 7, 30, 90, 365)

logger = logging.getLogger("monitor")


def _setup_logging() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(os.path.join(DATA_DIR, "monitor.log"), maxBytes=2_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    logger.setLevel(logging.INFO)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = asyncio.Event()
    task = asyncio.create_task(service.run_loop(stop), name="monitor")
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=15)
        except BaseException:
            pass


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/stats")
async def api_stats(days: int = 30, tz: int = 0):
    days = days if days in DAYS_CHOICES else 30
    tz = max(-14 * 60, min(14 * 60, tz))
    return service.stats(days, tz)


@app.get("/api/config")
async def api_config():
    return config.public_view(config.load())


@app.post("/api/config")
async def api_config_save(request: Request):
    # JSON only: a plain HTML form on a foreign site cannot send application/json without a CORS
    # preflight this server never approves, so a page the user happens to open cannot change the
    # settings from their browser.
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"error": "нужен JSON"}, status_code=415)
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"error": "битый JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "нужен объект"}, status_code=400)
    try:
        cfg = config.save(data)
    except config.ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except OSError:
        logger.exception("config save failed")
        return JSONResponse({"error": "не удалось записать config.json"}, status_code=500)
    logger.info("settings saved: %s", {k: data[k] for k in data if k in config.EDITABLE})
    service.wake()
    return config.public_view(cfg)


#: Where another device can reach this one: the private LAN ranges and Tailscale's 100.64/10.
_REACHABLE_NETS = [ipaddress.ip_network(n) for n in
                   ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]


def _lan_address() -> str:
    """The address of the default route, for the start banner — or ``""``.

    A UDP «connect» sends nothing, it only picks the interface. Under a VPN that is the tunnel's
    address (198.18.x and the like), useless to another device, so only a private / Tailscale one is
    shown. ⚠️ Not ``getaddrinfo(gethostname())``: on macOS it can hang for seconds on mDNS."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            addr = s.getsockname()[0]
    except OSError:
        return ""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return ""
    return addr if any(ip in net for net in _REACHABLE_NETS) else ""


def _bind(host: str, port: int) -> socket.socket:
    """Take the port BEFORE the monitor starts.

    ⚠️ uvicorn runs the lifespan (the monitor's start, which judges and rewrites the state file) before
    it binds, so a second copy started by mistake would write a false «не работал» gap into the journal
    and only then fail on the busy port. Binding here makes the second copy exit having touched nothing.
    SO_REUSEADDR only off Windows: there it lets two processes share one port."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def main() -> None:
    _setup_logging()
    config.ensure_file()
    cfg = config.load()
    host, port = cfg["host"], cfg["port"]
    try:
        sock = _bind(host, port)
    except OSError as e:
        print(f"Port {port} on {host} is busy ({e}). The monitor is probably already running: "
              f"open http://127.0.0.1:{port}", flush=True)
        sys.exit(1)
    print(f"Internet stability monitor: http://127.0.0.1:{port}", flush=True)
    if host in ("0.0.0.0", "::"):
        addr = _lan_address()
        print(f"  from other devices: http://{addr or '<this computer IP>'}:{port}", flush=True)
    print("Stop: Ctrl+C", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
