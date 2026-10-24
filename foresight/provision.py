"""One-click setup from inside the app ("Set up now" on the first-run card), for the installed ForesightWing.exe, which
has no setup script: install and start Ollama, then pull the two models.

Nothing here runs until the user presses the button. Ollama's installer comes from ollama.com and only runs if
Windows confirms it is signed by Ollama Inc.; it installs for the current user (no admin). The models come through
Ollama itself. Progress is kept in one place (status()) for the page to show.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from . import llm
from .config import settings

log = logging.getLogger(__name__)

OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_SIGNER = "O=Ollama Inc."
SILENT = ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_lock = threading.Lock()
_state: dict = {"running": False, "step": "", "done": 0, "total": 0, "error": None, "finished": False}


def status() -> dict:
    with _lock:
        return dict(_state)


def _set(**changes) -> None:
    with _lock:
        _state.update(changes)


# ---------------------------------------------------------------- Ollama

def ollama_folder() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"


def ollama_exe() -> Path | None:
    found = shutil.which("ollama")
    if found:
        return Path(found)
    exe = ollama_folder() / "ollama.exe"
    return exe if exe.is_file() else None


def _wait_for_ollama(seconds: int = 60) -> bool:
    for _ in range(seconds):
        if llm.status().get("ok"):
            return True
        time.sleep(1)
    return False


def start_ollama() -> bool:
    """Start the installed Ollama (its tray app, which runs the server; else the server alone, hidden)."""
    if llm.status().get("ok"):
        return True
    app = ollama_folder() / "ollama app.exe"
    exe = ollama_exe()
    if app.is_file():
        subprocess.Popen([str(app)], creationflags=_NO_WINDOW, close_fds=True)
    elif exe:
        subprocess.Popen([str(exe), "serve"], creationflags=_NO_WINDOW, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    else:
        return False
    return _wait_for_ollama()


def signed_by_ollama(path: Path) -> bool:
    """Windows' own Authenticode check: a valid signature whose signer is Ollama Inc."""
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
              "@{status = [string]$s.Status; subject = [string]$s.SignerCertificate.Subject} | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             timeout=60, creationflags=_NO_WINDOW).stdout
        sig = json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return sig.get("status") == "Valid" and OLLAMA_SIGNER in (sig.get("subject") or "")


def _download(url: str, dest: Path, step: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        _set(step=step, done=0, total=total)
        with open(part, "wb") as f:
            for block in resp.iter_bytes(1 << 20):
                f.write(block)
                _set(done=status()["done"] + len(block))
    os.replace(part, dest)


def install_ollama() -> None:
    setup = settings.data_dir / "downloads" / "OllamaSetup.exe"
    _download(OLLAMA_SETUP_URL, setup, "Downloading Ollama from ollama.com")
    _set(step="Checking Ollama's signature", done=0, total=0)
    if not signed_by_ollama(setup):
        setup.unlink(missing_ok=True)
        raise RuntimeError("the downloaded Ollama installer is not signed by Ollama Inc., so it was not run")
    _set(step="Installing Ollama")
    result = subprocess.run([str(setup), *SILENT], timeout=900, creationflags=_NO_WINDOW)
    setup.unlink(missing_ok=True)
    if result.returncode != 0 or not ollama_exe():
        raise RuntimeError(f"Ollama's installer stopped (code {result.returncode})")


def pull(model: str) -> None:
    """ollama pull, with its progress."""
    _set(step=f"Downloading {model}", done=0, total=0)
    with httpx.stream("POST", f"{settings.ollama_url}/api/pull", json={"model": model, "stream": True},
                      timeout=httpx.Timeout(60, read=None)) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("error"):
                raise RuntimeError(msg["error"])
            if msg.get("total"):
                _set(done=int(msg.get("completed") or 0), total=int(msg["total"]))


# ---------------------------------------------------------------- the button

def plan() -> list[str]:
    """What "Set up now" would do on this PC, in order."""
    st = llm.status()
    steps = []
    if not st.get("ok"):
        steps.append("start_ollama" if ollama_exe() else "install_ollama")
    for key, model in (("gen", settings.gen_model), ("embed", settings.embed_model)):
        if not st.get(f"{key}_ready"):
            steps.append(f"pull:{model}")
    return steps


def _run(steps: list[str]) -> None:
    try:
        for step in steps:
            if step == "install_ollama":
                install_ollama()
            if step in ("install_ollama", "start_ollama"):
                _set(step="Starting Ollama", done=0, total=0)
                if not start_ollama():
                    raise RuntimeError("Ollama did not start; open it from the Start menu, then check again")
            if step.startswith("pull:"):
                pull(step.split(":", 1)[1])
        _set(step="Ready", finished=True)
    except Exception as exc:
        log.exception("setup failed")
        _set(error=str(exc)[:300], finished=True)
    finally:
        _set(running=False)
        llm.check_media()  # the models' abilities, now that they are there


def start() -> dict:
    """Run every step this PC still needs, in the background. Returns the state (already running: unchanged)."""
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(running=True, step="Starting", done=0, total=0, error=None, finished=False)
    threading.Thread(target=_run, args=(plan(),), name="setup", daemon=True).start()
    return status()
