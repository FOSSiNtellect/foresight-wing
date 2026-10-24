"""Start Foresight Wing:  python run.py  [--no-browser] [--port 8765] [--tray]

Foresight Wing opens in a window of its own (Edge's app mode: no tabs or address bar; see foresight/window.py).
--tray keeps Foresight Wing in the notification area (Open Foresight Wing / Quit) instead of a console window; the Start-menu
shortcut that setup.bat can create uses it with pythonw. Closing the window leaves Foresight Wing running there, so a
meeting being recorded is never cut off. Foresight Wing starts only when you open it: nothing is added to Windows sign-in,
and there is no account to sign in to.

Built as ForesightWing.exe (packaging/build.py), it always runs in the tray, reads its speech models from the folder it
ships with, and "ForesightWing.exe --uninstall" removes it (Settings → Apps runs that).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
FROZEN = bool(getattr(sys, "frozen", False))


def _running(url: str) -> bool:
    """Whether Foresight Wing already answers on this port (a second launch then just opens it)."""
    import httpx

    try:
        return httpx.get(url + "api/status", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


LOG_MAX_BYTES = 5 * 1024 * 1024


def console_or_log(data_dir: Path) -> None:
    """Started from the Start menu or Explorer (ForesightWing.exe, or pythonw) there is no console: sys.stdout and
    sys.stderr are None, which breaks whatever writes to them (uvicorn's log setup asks stdout.isatty() and crashes).
    Send both to foresight.log in the data folder instead, keeping one older log of up to 5 MB."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = Path(data_dir) / "foresight.log"
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
    except OSError:
        pass
    log = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def tray_image(size: int = 64):
    """The Foresight Wing orb (as in the app's favicon): blue, lit from the top left."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    stops = [(0.0, (11, 87, 208)), (0.55, (76, 141, 246)), (1.0, (211, 227, 253))]  # rim → highlight
    steps = size // 2
    for i in range(steps):  # concentric discs, shrinking toward the highlight
        t = i / (steps - 1)
        (t0, c0), (t1, c1) = (stops[0], stops[1]) if t <= stops[1][0] else (stops[1], stops[2])
        u = (t - t0) / (t1 - t0)
        color = tuple(round(c0[k] + (c1[k] - c0[k]) * u) for k in range(3))
        r = (size / 2 - 2) * (1 - i / steps)
        cx = size / 2 - (size * 0.15) * (i / steps)
        cy = size / 2 - (size * 0.18) * (i / steps)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color, 255))
    return img


def _tray(url: str, server, show) -> None:
    import pystray

    def quit_app(icon, _item) -> None:
        server.should_exit = True
        icon.stop()

    icon = pystray.Icon("ForesightWing", tray_image(), "Foresight Wing", menu=pystray.Menu(
        pystray.MenuItem("Open Foresight Wing", lambda _icon, _item: show(), default=True),
        pystray.MenuItem("Quit", quit_app)))
    icon.run()  # until Quit


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-first meeting companion and knowledge hub")
    parser.add_argument("--port", type=int, default=None, help="port to listen on (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the UI in a browser")
    parser.add_argument("--tray", action="store_true", help="run in the notification area (Open / Quit)")
    parser.add_argument("--uninstall", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.uninstall:
        from foresight import install
        sys.exit(install.uninstall_interactive(Path(sys.executable).parent if FROZEN else install.default_target()))
    if args.port:
        os.environ["FORESIGHT_PORT"] = str(args.port)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # speech weights come from the local cache only
    if FROZEN:  # the speech models ship inside ForesightWing.exe's folder, in the Hugging Face cache layout
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "models"
        if bundled.is_dir():
            os.environ.setdefault("HF_HUB_CACHE", str(bundled))
        args.tray = True

    from foresight.config import settings
    from foresight.window import open_window

    console_or_log(settings.data_dir)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{settings.host}:{settings.port}/"

    def show() -> None:
        open_window(url, settings.data_dir / "window")

    if _running(url):
        if not args.no_browser:
            show()
        print(f"Foresight Wing is already running: {url}")
        return

    import uvicorn

    from foresight.server import app

    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning"))
    if not args.no_browser:
        threading.Timer(1.5, show).start()
    print(f"\n  Foresight Wing: {url}\n  data: {settings.data_dir}\n", flush=True)
    if not args.tray:
        server.run()
        return
    thread = threading.Thread(target=server.run, name="server", daemon=True)
    thread.start()
    _tray(url, server, show)
    server.should_exit = True
    thread.join(15)


if __name__ == "__main__":
    main()
