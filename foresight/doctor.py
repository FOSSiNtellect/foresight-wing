"""First-run check: is everything Foresight Wing needs on this PC? Used by the Home / Settings setup card (/api/setup)
and by the one-step setup script (tools/setup.py), which can fix what is missing.

Every check is local: Ollama on 127.0.0.1, the Hugging Face cache on disk, the audio devices, the data folder.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

from .config import settings

OLLAMA_DOWNLOAD = "https://ollama.com/download/windows"
INSTALLED = bool(getattr(sys, "frozen", False))  # ForesightWing.exe: speech models inside, "Set up now" for the rest
MODEL_GB = {"gemma4:e2b-it-qat": 4.3, "embeddinggemma-2:740m": 1.3}  # download sizes, for the button's label
OLLAMA_SETUP_GB = 1.0


def parakeet_repo() -> str:
    return f"istupakov/{settings.parakeet_model.removeprefix('nemo-')}-onnx"


def whisper_repo(model: str | None = None) -> str:
    model = model or settings.whisper_model
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(model, model)
    except Exception:
        return f"Systran/faster-whisper-{model}"


def cached(repo: str, pattern: str = "*") -> bool:
    """Whether the files of a Hugging Face model are in the local cache. (Looked up on disk: the loaders fetch only the
    files they need, which huggingface_hub would call an incomplete snapshot.)"""
    try:
        from huggingface_hub import constants
        root = Path(constants.HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}" / "snapshots"
    except Exception:
        return False
    return root.is_dir() and any(root.glob(f"*/{pattern}"))


def _check(id: str, label: str, ok: bool, detail: str = "", fix: str = "", required: bool = True) -> dict:
    return {"id": id, "label": label, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix, "required": required}


def run(*, devices: bool = True) -> dict:
    from . import llm, provision, vault

    checks = []
    st = llm.status()
    steps = provision.plan()
    button = "Press Set up now."
    checks.append(_check("ollama", "Ollama is running", st.get("ok"),
                         f"version {st.get('version')}" if st.get("ok") else st.get("error", ""),
                         button if INSTALLED else f"Install Ollama from {OLLAMA_DOWNLOAD} and start it, then check again."))
    for key, model, what in (("gen", settings.gen_model, "Gemma 4 (answers, notes, briefings)"),
                             ("embed", settings.embed_model, "EmbeddingGemma 2 (search)")):
        ready = st.get(f"{key}_ready", False)
        checks.append(_check(f"{key}_model", f"{what}: {model}", ready, "downloaded" if ready else "not downloaded",
                             button if INSTALLED else f"ollama pull {model}"))
    bundled = "Reinstall Foresight Wing: speech recognition ships inside it."
    quant = settings.parakeet_quantization
    parakeet = cached(parakeet_repo(), f"*.{quant}.onnx" if quant else "*.onnx")
    checks.append(_check("speech_en", f"Speech recognition (English and 24 more): {settings.asr_label}", parakeet,
                         ("included with Foresight Wing" if INSTALLED else "in the local model cache") if parakeet else f"{parakeet_repo()} is not downloaded",
                         bundled if INSTALLED else "Run setup.bat to download it (about 670 MB).",
                         required=settings.asr_engine != "whisper"))
    hindi = cached(whisper_repo(), "model.bin")
    checks.append(_check("speech_hi", f"Hindi / Hinglish live preview: Whisper {settings.whisper_model}", hindi,
                         ("included with Foresight Wing" if INSTALLED else "in the local model cache") if hindi else f"{whisper_repo()} is not downloaded",
                         bundled if INSTALLED else
                         "Run setup.bat to download it (about 480 MB). Only needed for Hindi / Hinglish meetings.",
                         required=settings.asr_engine == "whisper"))
    if devices:
        from .asr import list_devices
        dev = list_devices()
        checks.append(_check("audio", "Microphone and speakers", "error" not in dev,
                             f"mic: {dev.get('microphone')} · speakers: {dev.get('speaker')}" if "error" not in dev
                             else dev["error"], "Connect a microphone or headset, then check again.", required=False))
    detail = str(settings.data_dir)
    try:  # a file of its own, so two checks at once (e.g. a page reload) do not trip over each other
        with tempfile.TemporaryFile(dir=settings.data_dir, prefix=".write-test-") as probe:
            probe.write(b"ok")
        writable = True
    except OSError as exc:
        writable, detail = False, f"{settings.data_dir}: {exc.strerror or exc}"
    checks.append(_check("data", "Data folder", writable, detail, "Free up space or fix the folder's permissions."))
    if vault.available():
        try:
            vault.key()
            locked = True
        except Exception as exc:
            locked, detail = False, str(exc)
        checks.append(_check("vault", "Files encrypted at rest", locked,
                             "key locked to this Windows account" if locked else detail,
                             "The key in files.key belongs to another Windows account or PC."))
    missing = [c for c in checks if c["required"] and not c["ok"]]
    download = sum(OLLAMA_SETUP_GB if s == "install_ollama" else MODEL_GB.get(s.split(":", 1)[1], 2.0)
                   for s in steps if s != "start_ollama")
    return {"ok": not missing, "checks": checks, "missing": [c["id"] for c in missing], "installed": INSTALLED,
            "plan": steps, "download_gb": round(download, 1),
            "packages": {name: importlib.util.find_spec(name) is not None for name in ("pystray", "PIL", "cryptography")},
            "python": sys.version.split()[0]}
