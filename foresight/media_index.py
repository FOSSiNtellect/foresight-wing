"""Audio moments for search (multimodal memory, phase P2).

Stored meeting audio is cut into windows of about 20 s along the transcript, and each window is embedded with
EmbeddingGemma 2 straight from the audio, in the same vector space as text. A typed question can then find the moment
it was spoken, even where the transcript got words wrong. Runs on one background thread after audio is saved, and waits
while a meeting is being recorded so live transcription and Live Assistance keep the CPU.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from . import db, events, llm, media, retrieval
from .config import settings

log = logging.getLogger(__name__)

TARGET_S = 20.0     # aim for about 20 s of speech per window
MAX_S = 30.0        # never longer: finer windows point closer to the moment
MAX_GAP_S = 5.0     # a longer pause starts a new window
MIN_S = 0.5         # shorter slices are skipped
BATCH = 4           # windows per embedding request
POLL_S = 60
RETRY_ERRORS_S = 3600
RECHECK_CAPS_S = 600

_wake = threading.Event()
_started = False


class _Paused(Exception):
    """A meeting started recording: stop between batches and resume later."""


@dataclass
class Window:
    t_start: float
    t_end: float
    text: str


def build_windows(segments: list[dict]) -> list[Window]:
    """Group consecutive transcript lines of one channel into windows of about TARGET_S seconds.

    Consecutive windows share one line, so a sentence on a boundary is whole in at least one of them,
    except across a pause longer than MAX_GAP_S, where a new window starts cleanly."""
    segs = sorted(segments, key=lambda s: (s["t_start"], s["t_end"]))
    windows: list[Window] = []
    i = 0
    while i < len(segs):
        j = i
        while j + 1 < len(segs):
            nxt = segs[j + 1]
            if (nxt["t_start"] - segs[j]["t_end"] > MAX_GAP_S or nxt["t_end"] - segs[i]["t_start"] > MAX_S
                    or segs[j]["t_end"] - segs[i]["t_start"] >= TARGET_S):
                break
            j += 1
        windows.append(Window(segs[i]["t_start"], segs[j]["t_end"], " ".join(s["text"].strip() for s in segs[i:j + 1])))
        if j + 1 >= len(segs):
            break
        paused = segs[j + 1]["t_start"] - segs[j]["t_end"] > MAX_GAP_S
        i = j if j > i and not paused else j + 1
    return windows


def _recording() -> bool:
    from .meetings import manager
    return manager.active_id is not None


def _set_state(media_id: int, status: str, done_until: float, error: str | None = None) -> None:
    db.execute("INSERT INTO media_index(media_id, model, status, done_until, error, updated_at) VALUES (?,?,?,?,?,?) "
               "ON CONFLICT(media_id) DO UPDATE SET model=excluded.model, status=excluded.status, "
               "done_until=excluded.done_until, error=excluded.error, updated_at=excluded.updated_at",
               (media_id, settings.embed_model, status, done_until, error, db.now()))


def index_media(media_id: int) -> int:
    """Embed the windows of one stored recording that are not stored yet. Returns how many were added."""
    m = db.query_one("SELECT * FROM media WHERE id = ?", (media_id,))
    if not m or m["status"] != "ready" or m["kind"] != "recording":
        return 0
    state = db.query_one("SELECT * FROM media_index WHERE media_id = ?", (media_id,))
    if state and state["model"] != settings.embed_model:  # another embedding model: its vectors do not mix
        with db.tx() as conn:
            conn.execute("DELETE FROM media_items WHERE media_id = ?", (media_id,))
            conn.execute("DELETE FROM media_index WHERE media_id = ?", (media_id,))
        state = None
    done_until = state["done_until"] if state else 0.0
    segments = db.query("SELECT t_start, t_end, text FROM segments WHERE meeting_id = ? AND speaker = ? ORDER BY t_start, id",
                        (m["meeting_id"], m["channel"]))
    windows = [w for w in build_windows(segments) if w.t_end > done_until + 1e-6]
    if not windows:
        _set_state(media_id, "done", done_until)
        return 0

    _set_state(media_id, "indexing", done_until)
    pcm = media.decode_pcm16(m["path"])
    rate = media.RATE
    added = 0
    for k in range(0, len(windows), BATCH):
        if _recording():
            raise _Paused()
        part = [w for w in windows[k:k + BATCH]
                if (min(len(pcm), int(w.t_end * rate)) - int(w.t_start * rate)) >= MIN_S * rate]
        vectors = llm.embed_audio([llm.wav_bytes(pcm[int(w.t_start * rate):int(w.t_end * rate)].astype(np.float32) / 32768)
                                   for w in part]) if part else None
        last = windows[min(k + BATCH, len(windows)) - 1].t_end
        with db.tx() as conn:  # vectors and progress together, so a crash never stores a window twice
            if part:
                conn.executemany(
                    "INSERT INTO media_items(media_id, t_start, t_end, text, embedding, dim, model) VALUES (?,?,?,?,?,?,?)",
                    [(media_id, w.t_start, w.t_end, w.text, vectors[n].tobytes(), int(vectors.shape[1]), settings.embed_model)
                     for n, w in enumerate(part)])
            conn.execute("UPDATE media_index SET done_until = ?, updated_at = ? WHERE media_id = ?", (last, db.now(), media_id))
        added += len(part)
        retrieval.invalidate()
        events.publish("media_index", media_id=media_id, meeting_id=m["meeting_id"],
                       done=min(k + BATCH, len(windows)), total=len(windows))
    _set_state(media_id, "done", windows[-1].t_end)
    return added


def pending() -> list[int]:
    """Stored recordings whose audio moments are missing, incomplete, or from another embedding model."""
    return [r["id"] for r in db.query(
        "SELECT m.id FROM media m LEFT JOIN media_index x ON x.media_id = m.id "
        "WHERE m.kind = 'recording' AND m.status = 'ready' "
        "AND NOT EXISTS (SELECT 1 FROM transcript_passes t WHERE t.meeting_id = m.meeting_id AND t.temp_audio = 1) "
        "AND (x.media_id IS NULL OR x.model != ? OR x.status = 'indexing' OR (x.status = 'error' AND x.updated_at < ?)) "
        "ORDER BY m.created_at", (settings.embed_model, time.time() - RETRY_ERRORS_S))]


def _audio_supported() -> bool:
    caps = llm.media_status()
    if not caps.get("checked") or (not caps.get("audio_embed") and time.time() - caps.get("checked_at", 0) > RECHECK_CAPS_S):
        caps = llm.check_media()  # e.g. Ollama was not running when the app started
    return bool(caps.get("audio_embed"))


def run_once() -> int:
    """Index everything pending (unless a meeting is recording). Returns the number of windows added."""
    if _recording() or not pending() or not _audio_supported():
        return 0
    added = 0
    for media_id in pending():
        try:
            added += index_media(media_id)
        except _Paused:
            break
        except Exception as exc:
            log.warning("audio indexing of media %s failed: %s", media_id, exc)
            state = db.query_one("SELECT done_until FROM media_index WHERE media_id = ?", (media_id,))
            _set_state(media_id, "error", state["done_until"] if state else 0.0, str(exc)[:300])  # keep progress
    if added:
        events.publish("media", status="indexed", moments=added)
    return added


def notify() -> None:
    _wake.set()


def _worker() -> None:
    while True:
        _wake.wait(POLL_S)
        _wake.clear()
        try:
            run_once()
        except Exception:
            log.exception("audio indexing failed")


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    media.on_change(notify)
    threading.Thread(target=_worker, name="media-index", daemon=True).start()
    notify()  # catch up on anything saved while the app was closed
