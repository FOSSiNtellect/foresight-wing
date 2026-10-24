"""Gemma listens (multimodal memory, phase P3): clips of stored meeting audio for questions, and re-transcription.

Gemma 4 E2B accepts audio in Ollama's /api/chat as WAV bytes in a message's `images` list (verified on Ollama 0.40.5;
clips over 30 s are split by Ollama, so clips are kept at 30 s or less here).
"""
from __future__ import annotations

import base64
import logging

import numpy as np

from . import db, events, indexer, llm, media, retrieval

log = logging.getLogger(__name__)

CLIP_MAX_S = 30.0
MIN_CLIP_S = 0.3
NO_SPEECH = "NO_SPEECH"
TRANSCRIBE_PROMPT = ("Transcribe this audio verbatim, in the language it is spoken. Write Hindi or Hinglish speech in "
                     "Latin letters as it sounds. Reply with the transcript only. If there is no speech, only music or "
                     f"noise, reply exactly {NO_SPEECH}.")


def _fmt(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def clip_pcm(media_id: int, t0: float, t1: float) -> np.ndarray | None:
    """Samples of [t0, t1] (at most 30 s) of a stored recording as float32, or None if the audio is gone or too short."""
    row = media.playable(media_id)
    if not row:
        return None
    t0 = max(0.0, t0)
    pcm = media.decode_slice(row["path"], t0, min(t1, t0 + CLIP_MAX_S))
    if len(pcm) < MIN_CLIP_S * media.RATE:
        return None
    return pcm.astype(np.float32) / 32768


def clip_wav(media_id: int, t0: float, t1: float) -> bytes | None:
    """WAV bytes of [t0, t1] (at most 30 s) of a stored recording, or None if the audio is gone or too short."""
    pcm = clip_pcm(media_id, t0, t1)
    return llm.wav_bytes(pcm) if pcm is not None else None


def as_image_field(wav: bytes) -> str:
    return base64.b64encode(wav).decode("ascii")


def moment(media_id: int, t0: float, t1: float) -> dict | None:
    """A moment the user picked: its title, the transcript of that span, and how to play it."""
    row = db.query_one("SELECT m.id, m.meeting_id, m.source_id, m.channel, m.status, mt.title, s.title AS source_title "
                       "FROM media m LEFT JOIN meetings mt ON mt.id = m.meeting_id "
                       "LEFT JOIN sources s ON s.id = m.source_id WHERE m.id = ?", (media_id,))
    if not row or row["status"] != "ready":
        return None
    t0, t1 = max(0.0, t0), min(t1, max(0.0, t0) + CLIP_MAX_S)
    if row["meeting_id"]:
        lines = [r["text"] for r in db.query(
            "SELECT text FROM segments WHERE meeting_id = ? AND speaker = ? AND t_end > ? AND t_start < ? "
            "ORDER BY t_start, id", (row["meeting_id"], row["channel"], t0, t1))]
    else:  # a voice memo: its own timestamped transcript
        from .memos import lines_between
        lines = lines_between(row["source_id"], t0, t1) if row["source_id"] else []
    return {"title": f"{row['title'] or row['source_title'] or 'Recording'} — audio {_fmt(t0)}–{_fmt(t1)}",
            "text": " ".join(lines),
            "media": {"media_id": media_id, "channel": row["channel"], "t_start": t0, "t_end": t1,
                      "url": f"/api/media/{media_id}"}}


def _segment_media(segment: dict) -> int | None:
    row = db.query_one("SELECT id FROM media WHERE meeting_id = ? AND channel = ? AND kind = 'recording' AND status = 'ready'",
                       (segment["meeting_id"], segment["speaker"]))
    return row["id"] if row else None


def retranscribe(segment_id: int) -> dict:
    """Gemma's own hearing of one transcript line, as a second opinion. Nothing is changed until the user accepts it."""
    seg = db.query_one("SELECT * FROM segments WHERE id = ?", (segment_id,))
    if not seg:
        raise KeyError(segment_id)
    media_id = _segment_media(seg)
    wav = clip_wav(media_id, seg["t_start"] - 0.2, seg["t_end"] + 0.2) if media_id else None
    if not wav:
        raise LookupError("this line's audio is not stored (turn on Keep meeting audio in Settings)")
    text = llm.chat([{"role": "user", "content": TRANSCRIBE_PROMPT, "images": [as_image_field(wav)]}],
                    task="retranscribe", options={"temperature": 0, "num_predict": 300}).strip()
    return {"id": segment_id, "original": seg["text"], "text": text}


def update_segment(segment_id: int, text: str) -> dict:
    """Replace one transcript line, then refresh what was built from it: the audio windows' text and the
    meeting's transcript source (re-indexed in the background). The audio vectors stay: the sound did not change."""
    text = " ".join(text.split())[:2000]
    if not text:
        raise ValueError("empty text")
    seg = db.query_one("SELECT * FROM segments WHERE id = ?", (segment_id,))
    if not seg:
        raise KeyError(segment_id)
    replace_line(seg, text, replaced_by="edit")
    refresh_windows(seg["meeting_id"], seg["speaker"], seg["t_start"], seg["t_end"])
    reindex_transcript(seg["meeting_id"])
    return {"id": segment_id, "text": text}


def replace_line(seg: dict, text: str, *, replaced_by: str) -> None:
    """Change one transcript line, keeping the old wording in segment_versions so it can be restored."""
    with db.tx() as conn:
        conn.execute("INSERT INTO segment_versions(segment_id, text, replaced_by, created_at) VALUES (?,?,?,?)",
                     (seg["id"], seg["text"], replaced_by, db.now()))
        conn.execute("UPDATE segments SET text = ? WHERE id = ?", (text, seg["id"]))
    events.publish("segment_update", meeting_id=seg["meeting_id"], id=seg["id"], text=text)


def refresh_windows(meeting_id: int, channel: str | None = None, t0: float | None = None, t1: float | None = None) -> None:
    """Rebuild the text of audio windows (media_items) from the current transcript lines.
    The audio vectors stay as they are: the sound did not change."""
    sql = ("SELECT i.id, i.t_start, i.t_end, m.channel FROM media_items i JOIN media m ON m.id = i.media_id "
           "WHERE m.meeting_id = ?")
    params: list = [meeting_id]
    if channel is not None:
        sql += " AND m.channel = ?"
        params.append(channel)
    if t0 is not None and t1 is not None:  # only windows that contain [t0, t1]
        sql += " AND i.t_start <= ? AND i.t_end >= ?"
        params += [t0 + 1e-6, t1 - 1e-6]
    for w in db.query(sql, tuple(params)):
        lines = db.query("SELECT text FROM segments WHERE meeting_id = ? AND speaker = ? AND t_start >= ? AND t_end <= ? "
                         "ORDER BY t_start, id", (meeting_id, w["channel"], w["t_start"] - 1e-6, w["t_end"] + 1e-6))
        db.execute("UPDATE media_items SET text = ? WHERE id = ?", (" ".join(r["text"] for r in lines), w["id"]))
    retrieval.invalidate()


def reindex_transcript(meeting_id: int) -> None:
    """Re-embed the meeting's transcript source (in the background) after its lines changed."""
    source = db.query_one("SELECT title FROM sources WHERE uri = ?", (f"meeting:{meeting_id}:transcript",))
    if source:
        indexer.add_meeting_text(meeting_id, "transcript", source["title"])
