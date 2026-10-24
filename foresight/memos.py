"""Voice memos and other audio files in the knowledge hub.

An audio file in a watched folder or an upload is transcribed on this PC (the engine for the default meeting language:
Parakeet, or Whisper for Hindi / Hinglish) into timestamped lines, which are indexed like any document. Its sound is
also cut into ~20 s moments embedded by EmbeddingGemma 2 (as for kept meeting audio, media_index.py), so a question
can find the moment even where the transcript is wrong, and citations get a ▶ that plays it from the user's own file.
Foresight Wing never copies, changes or deletes the file.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from . import db, llm, media, media_index, runtime
from .config import settings

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 500 * 1024 * 1024
SILENT_WINDOW_S = 20.0    # a file with no recognised speech is still cut into moments, so its sound can be found
MAX_SILENT_WINDOWS = 90   # (the first 30 minutes)
_LINE = re.compile(r"^\[(\d+):(\d{2})\] (.+)$", re.M)


def _fmt(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def transcribe(path: Path) -> tuple[list[dict], float | None]:
    """Timestamped lines [{"t_start", "t_end", "text"}] and the duration in seconds."""
    from .asr import Transcriber

    if path.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError(f"audio file is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    engine = Transcriber.for_language(runtime.get("meeting_language") or "en")
    lines = [{"t_start": float(t0), "t_end": float(t1), "text": text.strip()}
             for t0, t1, text in media.transcribe(engine, path) if text.strip()]
    return lines, media.probe_duration(path)


def as_text(title: str, lines: list[dict], duration: float | None) -> str:
    head = f"Voice memo: {title}" + (f" ({_fmt(duration)})" if duration else "")
    body = "\n".join(f"[{_fmt(s['t_start'])}] {s['text']}" for s in lines) or "(no speech recognised)"
    return f"{head}\n\n{body}"


def _windows(lines: list[dict], duration: float | None) -> list[media_index.Window]:
    if lines:
        return media_index.build_windows(lines)
    end = min(duration or 0.0, SILENT_WINDOW_S * MAX_SILENT_WINDOWS)
    return [media_index.Window(t, min(t + SILENT_WINDOW_S, end), "") for t in np.arange(0.0, end, SILENT_WINDOW_S)
            if min(t + SILENT_WINDOW_S, end) - t >= media_index.MIN_S]


def attach(source_id: int, path: Path, lines: list[dict], duration: float | None) -> int:
    """Link an indexed audio file to its sound: media row (playback) and audio moments (search by sound)."""
    row = db.query_one("SELECT id FROM media WHERE source_id = ? AND kind = 'audio'", (source_id,))
    values = (str(path), media.MIME.get(path.suffix.lower(), "application/octet-stream"), duration, path.stat().st_size)
    if row:
        media_id = row["id"]
        db.execute("UPDATE media SET path = ?, mime = ?, duration_s = ?, bytes = ?, status = 'ready', error = NULL "
                   "WHERE id = ?", (*values, media_id))
    else:  # owned = 0: the user's own file (or their upload); Foresight Wing never deletes it
        media_id = db.execute("INSERT INTO media(source_id, kind, channel, path, owned, mime, duration_s, bytes, status, "
                              "created_at) VALUES (?, 'audio', 'Memo', ?, 0, ?, ?, ?, 'ready', ?)",
                              (source_id, *values, db.now()))
    db.execute("DELETE FROM media_items WHERE media_id = ?", (media_id,))
    windows = _windows(lines, duration)
    if not windows:
        return media_id
    try:
        pcm = media.decode_pcm16(path)
        rate = media.RATE
        for k in range(0, len(windows), media_index.BATCH):
            part = [w for w in windows[k:k + media_index.BATCH]
                    if (min(len(pcm), int(w.t_end * rate)) - int(w.t_start * rate)) >= media_index.MIN_S * rate]
            if not part:
                continue
            vectors = llm.embed_audio([llm.wav_bytes(pcm[int(w.t_start * rate):int(w.t_end * rate)].astype(np.float32) / 32768)
                                       for w in part])
            with db.tx() as conn:
                conn.executemany(
                    "INSERT INTO media_items(media_id, t_start, t_end, text, embedding, dim, model) VALUES (?,?,?,?,?,?,?)",
                    [(media_id, w.t_start, w.t_end, w.text, vectors[n].tobytes(), int(vectors.shape[1]), settings.embed_model)
                     for n, w in enumerate(part)])
    except Exception as exc:  # e.g. an Ollama without audio input: the transcript still finds the memo
        log.warning("audio moments of %s failed: %s", path.name, exc)
    return media_id


def for_source(source_id: int) -> dict | None:
    row = db.query_one("SELECT id, duration_s FROM media WHERE source_id = ? AND kind = 'audio' AND status = 'ready'",
                       (source_id,))
    return {"media_id": row["id"], "url": f"/api/media/{row['id']}", "duration_s": row["duration_s"]} if row else None


def lines_between(source_id: int, t0: float, t1: float) -> list[str]:
    """The memo's transcript lines that start between t0 (less a line's length) and t1."""
    seen, out = set(), []
    for chunk in db.query("SELECT text FROM chunks WHERE source_id = ? ORDER BY ord", (source_id,)):
        for m, s, text in _LINE.findall(chunk["text"]):
            t = int(m) * 60 + int(s)
            if (t, text) not in seen and t0 - 15 < t < t1:
                seen.add((t, text))
                out.append(text)
    return out
