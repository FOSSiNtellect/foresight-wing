"""Foresight Wing in a window of its own, like any installed app: Microsoft Edge (part of Windows) in app mode, with no
tabs or address bar and its own taskbar entry. It uses a separate profile in the data folder, so it never mixes with
your own Edge windows or tabs, and it runs no extensions and syncs nothing. Where Edge is missing, the default browser
opens the page instead.
"""
from __future__ import annotations

import os
import subprocess
import webbrowser
from pathlib import Path

WINDOW_SIZE = "1320,880"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def edge() -> Path | None:
    """msedge.exe, from the App Paths registration or its usual install folders."""
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                    found = Path(winreg.QueryValue(key, None))
                if found.is_file():
                    return found
            except OSError:
                continue
    except ImportError:  # not Windows
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        candidate = Path(base or "") / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if base and candidate.is_file():
            return candidate
    return None


def command(exe: Path, url: str, profile: Path) -> list[str]:
    # Edge signs even a new profile in with the Windows account and syncs that account's extensions into it. Extensions
    # can read the page (transcripts, notes, answers), so the window runs none and syncs nothing.
    return [str(exe), f"--app={url}", f"--user-data-dir={profile}", f"--window-size={WINDOW_SIZE}", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-sync"]


def open_window(url: str, profile: Path) -> bool:
    """Open Foresight Wing's window (or bring it back). True if it is an app window, False if a browser tab opened."""
    exe = edge()
    if not exe:
        webbrowser.open(url)
        return False
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(command(exe, url, profile), creationflags=_NO_WINDOW, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
