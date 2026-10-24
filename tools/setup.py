"""One-step setup for Foresight Wing (run it through setup.bat).

  python tools/setup.py          check, then fix what is missing, asking before each install or download
  python tools/setup.py --yes    the same, answering yes to everything
  python tools/setup.py --check  only report what is ready (changes nothing)

Steps: Python packages → Ollama → Gemma 4 and EmbeddingGemma 2 → speech weights (Parakeet; Whisper for Hindi) →
optional Start-menu shortcut (tray icon). There is no account and nothing is added to Windows sign-in: Foresight Wing
starts only when you open it.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
ASSUME_YES = False


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask(question: str, default: bool = True) -> bool:
    if ASSUME_YES:
        say(f"{question} yes")
        return True
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer.startswith("y")


def run(cmd: list[str], **kw) -> bool:
    say("  > " + " ".join(cmd))
    return subprocess.run(cmd, **kw).returncode == 0


# ---------------------------------------------------------------- steps

MODULES = ("fastapi", "uvicorn", "httpx", "numpy", "av", "onnx_asr", "faster_whisper", "soundcard", "pystray", "PIL",
           "cryptography", "docx", "openpyxl", "pypdf", "bs4", "lxml")


def missing_packages() -> list[str]:
    return [m for m in MODULES if importlib.util.find_spec(m) is None]


def packages() -> bool:
    if sys.version_info < (3, 10):
        say(f"Python {sys.version.split()[0]} is too old: install Python 3.10 or newer, then run setup.bat again.")
        return False
    missing = missing_packages()
    if not missing:
        say("✓ Python packages are installed")
        return True
    say(f"Python packages missing: {', '.join(missing)}")
    if not ask("Install them with pip (downloads from the Python Package Index)?"):
        return False
    return run([sys.executable, "-m", "pip", "install", "-r", str(APP / "requirements.txt")])


def _ollama_exe() -> str | None:
    found = shutil.which("ollama")
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return found or (str(default) if default.is_file() else None)


def ollama() -> bool:
    from foresight import doctor, llm

    if llm.status().get("ok"):
        say("✓ Ollama is running")
        return True
    exe = _ollama_exe()
    if not exe:
        say("Ollama is not installed. It runs Gemma on this PC.")
        if shutil.which("winget") and ask("Install it with winget (Ollama.Ollama, about 1 GB)?"):
            run(["winget", "install", "--id", "Ollama.Ollama", "-e", "--accept-package-agreements"])
            exe = _ollama_exe()
        if not exe:
            say(f"Install Ollama from {doctor.OLLAMA_DOWNLOAD}, then run setup.bat again.")
            return False
    say("Starting Ollama…")
    subprocess.Popen([exe, "serve"], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                     getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        if llm.status().get("ok"):
            say("✓ Ollama is running")
            return True
    say("Ollama did not start. Open the Ollama app from the Start menu, then run setup.bat again.")
    return False


def models() -> bool:
    from foresight import llm
    from foresight.config import settings

    st, ok = llm.status(), True
    for key, model, size in (("gen", settings.gen_model, "about 4 GB"), ("embed", settings.embed_model, "about 1.5 GB")):
        if st.get(f"{key}_ready"):
            say(f"✓ {model} is downloaded")
        elif ask(f"Download {model} with Ollama ({size})?"):
            ok &= run([_ollama_exe() or "ollama", "pull", model])
        else:
            ok = False
    return ok


def speech() -> bool:
    from foresight import doctor
    from foresight.config import settings

    env = {**os.environ, "HF_HUB_OFFLINE": "0"}  # the app itself never downloads; setup may, when you agree
    ok = True
    quant = settings.parakeet_quantization
    if doctor.cached(doctor.parakeet_repo(), f"*.{quant}.onnx" if quant else "*.onnx"):
        say(f"✓ Speech recognition ({settings.asr_label}) is downloaded")
    elif ask(f"Download speech recognition {doctor.parakeet_repo()} from Hugging Face (about 670 MB)?"):
        ok &= run([sys.executable, "-c", "import onnx_asr, sys; onnx_asr.load_model(sys.argv[1], quantization=sys.argv[2] or None)",
                   settings.parakeet_model, quant or ""], env=env)
    else:
        ok = False
    if doctor.cached(doctor.whisper_repo(), "model.bin"):
        say(f"✓ Hindi / Hinglish preview (Whisper {settings.whisper_model}) is downloaded")
    elif ask(f"Download Whisper {settings.whisper_model} for Hindi / Hinglish meetings (about 480 MB)?", default=False):
        run([sys.executable, "-c", "import faster_whisper, sys; faster_whisper.download_model(sys.argv[1])",
             settings.whisper_model], env=env)
    return ok


def shortcut() -> None:
    """A Start-menu entry that opens Foresight Wing in the notification area. Not in Startup: it never starts by itself."""
    programs = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    link = programs / "Foresight Wing.lnk"
    if link.exists() or not programs.is_dir():
        return
    if not ask("Add Foresight Wing to the Start menu (opens it with a tray icon)?"):
        return
    from foresight.config import settings
    import run as launcher

    icon = settings.data_dir / "foresight.ico"
    launcher.tray_image(256).save(icon, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
              f"$s.TargetPath = '{pythonw if pythonw.is_file() else sys.executable}'; "
              f"$s.Arguments = '\"{APP / 'run.py'}\" --tray'; $s.WorkingDirectory = '{APP}'; "
              f"$s.IconLocation = '{icon}'; $s.Description = 'Foresight Wing'; $s.Save()")
    if run(["powershell", "-NoProfile", "-Command", script]):
        say(f"✓ Start menu: {link.stem}")


def report() -> bool:
    missing = missing_packages()
    if missing:
        say(f" ✕ Python packages for {sys.executable}: missing {', '.join(missing)}")
        say("     → run setup.bat (it installs them with pip)")
        return False
    from foresight import doctor

    result = doctor.run()
    say("")
    for c in result["checks"]:
        mark = "✓" if c["ok"] else ("✕" if c["required"] else "!")
        say(f" {mark} {c['label']}" + (f"  ({c['detail']})" if c["detail"] else ""))
        if c["fix"]:
            say(f"     → {c['fix']}")
    say("")
    say("Ready." if result["ok"] else "Not ready yet: see the ✕ items above.")
    return result["ok"]


def main() -> int:
    global ASSUME_YES
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="answer yes to every install and download")
    parser.add_argument("--check", action="store_true", help="only report what is ready")
    args = parser.parse_args()
    ASSUME_YES = args.yes
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    say("Foresight Wing: setup\n")
    if args.check:
        return 0 if report() else 1
    if not packages():
        return 1
    if ollama():
        models()
    speech()
    shortcut()
    ready = report()
    if ready and ask("Open Foresight Wing now?"):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pythonw if pythonw.is_file() else sys.executable), str(APP / "run.py"), "--tray"], cwd=APP)
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
