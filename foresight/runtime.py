"""User-adjustable preferences persisted as JSON in the data directory."""
from __future__ import annotations

import json
import threading
from typing import Any

from .config import settings

DEFAULTS: dict[str, Any] = {
    "live_assist": True,              # answer questions heard during meetings
    "answer_own_questions": False,    # also react to questions picked up on the microphone
    "live_min_score": settings.live_min_score,
    "capture_mic": True,
    "capture_system": True,
    "keep_meeting_audio": False,      # save meeting audio for playback (opt-in)
    "audio_retention_days": 30,       # 7 | 30 | 90 | 0 = keep until deleted
    "meeting_language": "en",         # default for new meetings: en | hi (Hindi / Hinglish)
    "rerank": True,                   # Gemma re-ranks sources before chat answers and briefings
    "index_pictures": True,           # Gemma describes pictures in watched folders (uploads are always indexed)
    "index_audio_files": True,        # transcribe voice memos and other audio in watched folders (uploads always)
}

_path = settings.data_dir / "preferences.json"
_lock = threading.Lock()
_values: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _values
    if _values is None:
        try:
            stored = json.loads(_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        _values = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    return _values


def get(key: str) -> Any:
    with _lock:
        return _load()[key]


def all_values() -> dict[str, Any]:
    with _lock:
        return dict(_load())


def update(changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        values = _load()
        for key, value in changes.items():
            if key not in DEFAULTS:
                continue
            values[key] = type(DEFAULTS[key])(value)
        _path.write_text(json.dumps(values, indent=2), encoding="utf-8")
        return dict(values)
