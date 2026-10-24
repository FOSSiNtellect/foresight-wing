"""ForesightWingSetup.exe: installs Foresight Wing for the current user. No admin rights, no account, nothing at Windows
sign-in. Double-click it; it puts the app in %LOCALAPPDATA%\\Programs\\ForesightWing, adds Foresight Wing to the Start menu and
to Settings → Apps (for uninstalling), and opens it.

  ForesightWingSetup.exe [--target DIR] [--no-shortcut] [--no-launch] [--quiet]   (the options are for testing)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from foresight import __version__, install


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Foresight Wing")
    parser.add_argument("--target", type=Path, default=None, help="install folder (default: your Programs folder)")
    parser.add_argument("--no-shortcut", action="store_true", help="no Start-menu or Settings → Apps entry")
    parser.add_argument("--no-launch", action="store_true", help="do not open Foresight Wing afterwards")
    parser.add_argument("--quiet", action="store_true", help="no message boxes")
    args = parser.parse_args()
    try:
        import pyi_splash  # the "Installing Foresight Wing…" picture shown while the files unpack
        pyi_splash.close()
    except ImportError:
        pass

    def say(text: str, flags: int = install.MB_OK | install.MB_ICONINFO) -> None:
        if not args.quiet:
            install.message(text, flags)

    target = (args.target or install.default_target()).resolve()
    if install.running():
        say("Foresight Wing is open. Quit it from its icon in the notification area (right-click → Quit), then run this "
            "installer again.", install.MB_OK | install.MB_ICONWARN)
        return 1
    try:
        install.place(Path(sys._MEIPASS) / "payload" / "ForesightWing", target)
        if not args.no_shortcut:
            install.make_shortcut(install.start_menu_link(), target / install.EXE)
            install.register(target, __version__)
    except Exception as exc:
        say(f"Foresight Wing could not be installed:\n{exc}", install.MB_OK | install.MB_ICONWARN)
        return 1
    if not args.no_launch:
        subprocess.Popen([str(target / install.EXE)], cwd=target, close_fds=True)
    say("Foresight Wing is installed.\n\nIt opens in its own window, and its icon sits in the notification area. Next time, "
        "open it from the Start menu. The first time, press “Set up now” to download Ollama and the Gemma models.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
