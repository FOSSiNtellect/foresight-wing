"""Runtime settings. Every value can be overridden with a FORESIGHT_* environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FORESIGHT_{name}", default)


def _default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "ForesightWin"


@dataclass(frozen=True)
class Settings:
    host: str = _env("HOST", "127.0.0.1")
    port: int = int(_env("PORT", "8765"))
    data_dir: Path = Path(_env("DATA_DIR", str(_default_data_dir())))

    ollama_url: str = _env("OLLAMA_URL", "http://127.0.0.1:11434")
    # Gemma 4 E2B fits machines with < 15 GB RAM; E4B needs about 15 GB.
    gen_model: str = _env("GEN_MODEL", "gemma4:e2b-it-qat")
    embed_model: str = _env("EMBED_MODEL", "embeddinggemma-2:740m")
    # Conservative sampling defaults for grounded answers.
    gen_options: dict = field(default_factory=lambda: {
        "temperature": 0.4,
        "top_k": 40,
        "top_p": 0.9,
        "repeat_penalty": 1.05,
        "num_ctx": int(_env("NUM_CTX", "8192")),
    })
    keep_alive: str = _env("KEEP_ALIVE", "30m")

    # Speech-to-text engine: "parakeet" (NVIDIA Parakeet TDT 0.6B v3 via ONNX) or "whisper" (faster-whisper).
    asr_engine: str = _env("ASR_ENGINE", "parakeet").lower()
    parakeet_model: str = _env("PARAKEET_MODEL", "nemo-parakeet-tdt-0.6b-v3")
    parakeet_quantization: str | None = _env("PARAKEET_QUANTIZATION", "int8") or None

    whisper_model: str = _env("WHISPER_MODEL", "small")
    whisper_device: str = _env("WHISPER_DEVICE", "cpu")
    whisper_compute: str = _env("WHISPER_COMPUTE", "int8")
    whisper_language: str | None = _env("WHISPER_LANGUAGE", "") or None
    # Only use speech weights already on disk so transcription never reaches the network.
    whisper_local_only: bool = _env("WHISPER_LOCAL_ONLY", "1") == "1"

    @property
    def asr_label(self) -> str:
        if self.asr_engine == "whisper":
            return f"whisper {self.whisper_model}"
        name = self.parakeet_model.removeprefix("nemo-")
        return f"{name} ({self.parakeet_quantization})" if self.parakeet_quantization else name

    chunk_chars: int = int(_env("CHUNK_CHARS", "1200"))
    chunk_overlap: int = int(_env("CHUNK_OVERLAP", "200"))
    retrieve_k: int = int(_env("RETRIEVE_K", "6"))
    # Weight of audio moments in search fusion (text vectors and keywords weigh 1). Tune with tools/eval_media.py.
    media_weight: float = float(_env("MEDIA_WEIGHT", "0.5"))
    encrypt_files: bool = _env("ENCRYPT_FILES", "1") != "0"  # audio, uploads and thumbnails Foresight Wing keeps (vault.py)
    # Pictures found by their look rank like text, but only from this cosine up: on real screenshots, matching
    # descriptions scored 0.68-0.79 against the picture and unrelated ones 0.50-0.67.
    image_weight: float = float(_env("IMAGE_WEIGHT", "1.0"))
    image_min_similarity: float = float(_env("IMAGE_MIN_SIMILARITY", "0.65"))
    # Minimum cosine similarity before Live Assistance shows a proactive card.
    live_min_score: float = float(_env("LIVE_MIN_SCORE", "0.30"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "foresight.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"


settings = Settings()
for _d in (settings.data_dir, settings.upload_dir, settings.recordings_dir):
    _d.mkdir(parents=True, exist_ok=True)
