"""Per-user install and uninstall of ForesightWing.exe (used by ForesightWingSetup.exe, and by "ForesightWing.exe --uninstall",
which Settings → Apps runs). No admin rights and nothing at Windows sign-in: the app goes to
%LOCALAPPDATA%\\Programs\\ForesightWing, plus a Start-menu entry and an entry in Settings → Apps.

Standard library only, so the installer stays small. Your data (%LOCALAPPDATA%\\ForesightWin) lives apart from the app,
so reinstalling or updating keeps it.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

APP_NAME = "Foresight Wing"
EXE = "ForesightWing.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ForesightWin"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MB_OK, MB_YESNO, MB_ICONINFO, MB_ICONWARN, MB_DEFBUTTON2, IDYES = 0x0, 0x4, 0x40, 0x30, 0x100, 6


def default_target() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "ForesightWing"


def data_folder() -> Path:
    return Path(os.environ.get("FORESIGHT_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ForesightWin")


def start_menu_link() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"


def message(text: str, flags: int = MB_OK | MB_ICONINFO) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)


def running(port: int = 8765) -> bool:
    """Whether Foresight Wing is open (its files cannot be replaced while it runs)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.5) as resp:
            return resp.status == 200
    except OSError:
        return False


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def make_shortcut(link: Path, exe: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(link)}); "
              f"$s.TargetPath = {_ps_quote(exe)}; $s.WorkingDirectory = {_ps_quote(exe.parent)}; "
              f"$s.IconLocation = {_ps_quote(exe)}; $s.Description = '{APP_NAME}'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, creationflags=_NO_WINDOW, timeout=60)


def register(target: Path, version: str) -> None:
    """The entry in Settings → Apps (per user), whose Uninstall runs ForesightWing.exe --uninstall."""
    import winreg

    size_kb = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) // 1024
    exe = target / EXE
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", version), ("Publisher", "FOSSiNtellect"),
                            ("DisplayIcon", str(exe)), ("InstallLocation", str(target)),
                            ("UninstallString", f'"{exe}" --uninstall')):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, value in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", size_kb)):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def unregister() -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def ours(folder: Path) -> bool:
    """A folder the installer may replace: empty, or holding a previous Foresight Wing install."""
    return not folder.exists() or (folder.is_dir() and (not any(folder.iterdir()) or (folder / EXE).is_file()))


def place(payload: Path, target: Path) -> None:
    """Put the app files in place, replacing an older install (never anything else)."""
    if not ours(target):
        raise RuntimeError(f"{target} holds other files; choose another folder")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(payload), str(target))  # same drive: a rename, instant
    except OSError:
        shutil.copytree(payload, target)


def _remove_after_exit(target: Path, pid: int) -> list[str]:
    """PowerShell that waits for process `pid` to exit, then deletes `target`. A fixed delay is not enough: the
    uninstaller's "was removed" box keeps the exe running, and Windows will not delete the files it has loaded."""
    folder = _ps_quote(target)
    script = (f"Wait-Process -Id {pid} -ErrorAction SilentlyContinue; "
              f"foreach ($i in 1..20) {{ Remove-Item -LiteralPath {folder} -Recurse -Force -ErrorAction SilentlyContinue; "
              f"if (-not (Test-Path -LiteralPath {folder})) {{ break }}; Start-Sleep -Milliseconds 500 }}")
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def uninstall(target: Path, *, remove_data: bool) -> None:
    """Remove the Start-menu entry, the Settings → Apps entry and the app folder (after this process exits)."""
    start_menu_link().unlink(missing_ok=True)
    unregister()
    if remove_data:
        shutil.rmtree(data_folder(), ignore_errors=True)
    if ours(target):  # the running exe cannot delete itself: a hidden PowerShell does it once this process has exited
        subprocess.Popen(_remove_after_exit(target, os.getpid()), creationflags=_NO_WINDOW, close_fds=True)


def uninstall_interactive(target: Path) -> int:
    if message(f"Remove {APP_NAME} from this PC?", MB_YESNO | MB_ICONWARN) != IDYES:
        return 1
    if running():
        message("Foresight Wing is open. Quit it from its icon in the notification area (right-click → Quit), then try again.",
                MB_OK | MB_ICONWARN)
        return 1
    keep = message(f"Also delete your Foresight Wing data (meetings, notes, indexed sources, kept audio) in\n{data_folder()}?\n\n"
                   "Choose No to keep it for a later reinstall.", MB_YESNO | MB_ICONWARN | MB_DEFBUTTON2) != IDYES
    uninstall(target, remove_data=not keep)
    message(f"{APP_NAME} was removed." + (" Your data was kept." if keep else ""))
    return 0
