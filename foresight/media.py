"""Stored meeting audio: live capture written to Ogg Opus, imported recordings, playback, retention and clean-up.

Audio is kept only when the user turns on "Keep meeting audio" (off by default). Transcripts never depend on it.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from . import db, events, runtime, vault
from .config import settings

log = logging.getLogger(__name__)

MEDIA_DIR = settings.data_dir / "media"
RATE = 16000
OPUS_BITRATE = 32000          # about 14 MB per hour per channel
BACKLOG_BLOCKS = 600          # if the encoder falls this far behind, later audio is recorded as silence (a gap marker)
SWEEP_SECONDS = 6 * 3600
MIME = {".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".mp4": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".webm": "audio/webm"}

_started = False
_listeners: list[Callable[[], None]] = []


def on_change(listener: Callable[[], None]) -> None:
    """Called whenever new audio is ready (the audio indexer subscribes)."""
    _listeners.append(listener)


def _changed() -> None:
    for listener in list(_listeners):
        try:
            listener()
        except Exception:
            log.exception("media listener failed")


def _expiry(created: float) -> float | None:
    days = int(runtime.get("audio_retention_days") or 0)
    return created + days * 86400 if days > 0 else None


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _unlink(path: str | Path) -> None:
    """Delete a file Foresight Wing created. Never touches anything outside the data folder."""
    p = Path(path)
    if not _norm(p).startswith(_norm(settings.data_dir) + os.sep):
        log.warning("refusing to delete %s: outside the data folder", p)
        return
    try:
        p.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not delete %s: %s", p, exc)


@contextmanager
def _container(path: str | Path):
    """A PyAV container for a stored file, decrypted on the fly (in memory) when Foresight Wing encrypted it."""
    import av

    if vault.is_encrypted(path):
        with vault.Reader(path) as f, av.open(f) as container:
            yield container
    else:
        with av.open(str(path)) as container:
            yield container


def probe_duration(path: str | Path) -> float | None:
    import av

    try:
        with _container(path) as container:
            if container.duration:
                return float(container.duration / av.time_base)
            stream = container.streams.audio[0]
            return float(stream.duration * stream.time_base) if stream.duration else None
    except Exception:
        return None


def decode_pcm16(path: str | Path) -> np.ndarray:
    """Any audio file → 16 kHz mono int16 samples (half the memory of float32 for long meetings)."""
    import av

    chunks: list[np.ndarray] = []
    with _container(path) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(container.streams.audio[0]):
            chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    return np.concatenate(chunks).astype(np.int16, copy=False) if chunks else np.zeros(0, dtype=np.int16)


def decode_slice(path: str | Path, t0: float, t1: float) -> np.ndarray:
    """Samples from t0 to t1 seconds as 16 kHz mono int16, decoding only that part of the file."""
    import av

    t0 = max(0.0, t0)
    chunks: list[np.ndarray] = []
    base: float | None = None
    got = 0
    with _container(path) as container:
        stream = container.streams.audio[0]
        if t0 > 2 and stream.time_base:
            try:
                container.seek(int((t0 - 2) / stream.time_base), stream=stream)  # lands on or before the start
            except av.error.FFmpegError:
                pass  # not seekable: decode from the beginning
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(stream):
            if base is None:
                base = max(0.0, float(frame.time)) if frame.time is not None else 0.0
            for f in resampler.resample(frame):
                chunks.append(f.to_ndarray().reshape(-1))
                got += chunks[-1].shape[0]
            if base + got / RATE >= t1:
                break
    if base is None or not chunks:
        return np.zeros(0, dtype=np.int16)
    pcm = np.concatenate(chunks)
    start = max(0, int(round((t0 - base) * RATE)))
    return pcm[start:max(start, int(round((t1 - base) * RATE)))].astype(np.int16, copy=False)


# ---------------------------------------------------------------- live capture → Ogg Opus

class _ChannelWriter:
    """Encodes one capture channel on its own thread, so capture never waits for the encoder."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.samples = 0
        self.error: str | None = None
        self._q: "queue.Queue[np.ndarray | int | None]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"audio-{path.parent.name}-{path.stem}", daemon=True)
        self._thread.start()

    def put(self, block: np.ndarray) -> None:
        if self._q.qsize() >= BACKLOG_BLOCKS:
            self._q.put_nowait(len(block))  # remember the gap, not the audio, so memory stays bounded
        else:
            self._q.put_nowait(np.array(block, dtype=np.float32, copy=True).reshape(-1))

    def close(self, timeout: float = 60) -> None:
        self._q.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        import av

        container = out = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)  # a sweep may have removed the still-empty folder
            out = vault.open_write(self.path)  # encrypted as it is written: plain audio never reaches the disk
            container = av.open(out, mode="w", format="ogg")
            stream = container.add_stream("libopus", rate=RATE)
            stream.layout = "mono"
            stream.bit_rate = OPUS_BITRATE
            while True:
                item = self._q.get()
                if item is None:
                    break
                # A gap is written as silence so the file stays aligned with the transcript's timestamps.
                pcm = np.zeros(item, dtype=np.float32) if isinstance(item, int) else item
                frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="flt", layout="mono")
                frame.sample_rate = RATE
                for packet in stream.encode(frame):
                    container.mux(packet)
                self.samples += len(pcm)
            for packet in stream.encode(None):
                container.mux(packet)
        except Exception as exc:
            self.error = str(exc)[:300]
            log.exception("saving audio to %s failed", self.path)
            while self._q.get() is not None:  # drain, so close() returns
                pass
        finally:
            if container is not None:
                container.close()
            if out is not None:
                out.close()


class AudioStore:
    """Saves one meeting's live capture: one Ogg Opus file per channel, sample-aligned with the transcript.

    Rows are created up front (status 'recording') so the orphan sweep never deletes a file being written."""

    def __init__(self, meeting_id: int, channels: list[str]) -> None:
        self.meeting_id = meeting_id
        folder = MEDIA_DIR / f"meeting-{meeting_id}"
        folder.mkdir(parents=True, exist_ok=True)
        now = db.now()
        self.ids: dict[str, int] = {}
        self.writers: dict[str, _ChannelWriter] = {}
        for channel in channels:
            path = folder / f"{channel.lower()}.ogg"
            self.ids[channel] = db.execute(
                "INSERT INTO media(meeting_id, kind, channel, path, owned, mime, status, created_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (meeting_id, "recording", channel, str(path), 1, "audio/ogg", "recording", now, _expiry(now)))
            self.writers[channel] = _ChannelWriter(path)

    def feed(self, channel: str, block: np.ndarray) -> None:
        writer = self.writers.get(channel)
        if writer is not None:
            writer.put(block)

    def close(self) -> None:
        for channel, writer in self.writers.items():
            writer.close()
            _finalize(self.ids[channel], writer.samples / RATE if writer.samples else 0.0, writer.error)
        events.publish("media", meeting_id=self.meeting_id, status="ready")
        _changed()

    def discard(self) -> None:
        for writer in self.writers.values():
            writer.close()
        purge_meeting(self.meeting_id)


def _finalize(media_id: int, duration: float | None, error: str | None = None) -> None:
    row = db.query_one("SELECT * FROM media WHERE id = ?", (media_id,))
    if not row:
        return
    path = Path(row["path"])
    if not duration or not path.is_file():  # e.g. that audio source failed to start: nothing worth keeping
        _purge_rows([row])
        return
    db.execute("UPDATE media SET status = 'ready', duration_s = ?, bytes = ?, error = ? WHERE id = ?",
               (round(duration, 3), path.stat().st_size, error, media_id))


# ---------------------------------------------------------------- imported recordings

def adopt_import(meeting_id: int, path: str | Path, *, keep: bool | None = None) -> int | None:
    """After an imported recording is transcribed: keep it for playback if the user keeps meeting audio (or the
    caller still needs it, `keep=True`), otherwise delete Foresight Wing's copy (the user's original file is untouched)."""
    path = Path(path)
    if not (runtime.get("keep_meeting_audio") if keep is None else keep):
        _unlink(path)
        return None
    if not path.is_file():
        return None
    vault.encrypt_file(path)  # normally already written encrypted (server.py)
    now = db.now()
    media_id = db.execute(
        "INSERT INTO media(meeting_id, kind, channel, path, owned, mime, duration_s, bytes, status, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (meeting_id, "recording", "Recording", str(path), 1, MIME.get(path.suffix.lower(), "application/octet-stream"),
         probe_duration(path), path.stat().st_size, "ready", now, _expiry(now)))
    events.publish("media", meeting_id=meeting_id, status="ready")
    _changed()
    return media_id


def transcribe(engine, path: str | Path) -> Iterator[tuple[float, float, str]]:
    """engine.transcribe_file for a stored file. An encrypted one is decrypted in memory, never to disk, and cut into
    utterances by the same VAD as live capture."""
    if not vault.is_encrypted(path):
        yield from engine.transcribe_file(Path(path))
        return
    from .asr import SR, Segmenter

    audio = decode_pcm16(path).astype(np.float32) / 32768
    segmenter = Segmenter("Recording")

    def emit(utt):
        text = engine.transcribe(utt.audio)
        return (utt.t_start, utt.t_start + len(utt.audio) / SR, text) if text else None

    for i in range(0, len(audio), SR * 10):
        for utt in segmenter.feed(audio[i:i + SR * 10]):
            if line := emit(utt):
                yield line
    tail = segmenter.flush()
    if tail and (line := emit(tail)):
        yield line


def encrypt_existing() -> int:
    """Encrypt audio, uploads and thumbnails saved before encryption was on (skipping files being recorded)."""
    from . import images

    recording = {r["path"] for r in db.query("SELECT path FROM media WHERE status = 'recording'")}
    return vault.encrypt_existing([MEDIA_DIR, settings.recordings_dir, settings.upload_dir, images.THUMB_DIR], recording)


# ---------------------------------------------------------------- queries

def for_meeting(meeting_id: int) -> list[dict]:
    rows = db.query("SELECT id, channel, status, duration_s, mime, bytes, created_at, expires_at FROM media "
                    "WHERE meeting_id = ? AND kind = 'recording' ORDER BY channel DESC", (meeting_id,))
    for r in rows:
        r["url"] = f"/api/media/{r['id']}" if r["status"] == "ready" else None
    return rows


def playable(media_id: int) -> dict | None:
    row = db.query_one("SELECT id, path, mime, status FROM media WHERE id = ?", (media_id,))
    if not row or row["status"] != "ready" or not Path(row["path"]).is_file():
        return None
    return row


def usage() -> dict:
    row = db.query_one("SELECT COUNT(*) AS files, COALESCE(SUM(bytes), 0) AS bytes, "
                       "(SELECT COUNT(*) FROM media_items i JOIN media r ON r.id = i.media_id "
                       "WHERE r.kind = 'recording') AS moments FROM media "
                       "WHERE kind = 'recording' AND status = 'ready'")
    return {"files": row["files"], "bytes": row["bytes"], "moments": row["moments"]}


# ---------------------------------------------------------------- deletion, retention, clean-up

def _purge_rows(rows: list[dict]) -> None:
    for r in rows:
        if r["owned"]:
            _unlink(r["path"])
            folder = Path(r["path"]).parent
            if _norm(folder).startswith(_norm(MEDIA_DIR) + os.sep):  # a meeting's folder, once empty
                try:
                    folder.rmdir()
                except OSError:
                    pass
    with db.tx() as conn:
        conn.executemany("DELETE FROM media WHERE id = ?", [(r["id"],) for r in rows])


def purge_meeting(meeting_id: int) -> None:
    _purge_rows(db.query("SELECT * FROM media WHERE meeting_id = ?", (meeting_id,)))


def delete_all_recordings() -> int:
    """'Delete all stored audio' in Settings. A meeting being recorded right now keeps its files."""
    rows = db.query("SELECT * FROM media WHERE kind = 'recording' AND status != 'recording'")
    _purge_rows(rows)
    events.publish("media", status="deleted", count=len(rows))
    return len(rows)


def apply_retention() -> int:
    """Recompute expiry dates after the retention setting changes, then delete what is now past it."""
    days = int(runtime.get("audio_retention_days") or 0)
    db.execute("UPDATE media SET expires_at = CASE WHEN ? > 0 THEN created_at + ? * 86400 ELSE NULL END "
               "WHERE kind = 'recording'", (days, days))
    return sweep()


def sweep() -> int:
    """Delete expired audio (transcripts stay) and files in the media folder that no row knows about."""
    expired = db.query("SELECT * FROM media WHERE status = 'ready' AND expires_at IS NOT NULL AND expires_at < ?",
                       (time.time(),))
    for r in expired:
        if r["owned"]:
            _unlink(r["path"])
        db.execute("UPDATE media SET status = 'expired', bytes = 0 WHERE id = ?", (r["id"],))
    known = {_norm(r["path"]) for r in db.query("SELECT path FROM media")}
    orphans = 0
    if MEDIA_DIR.is_dir():
        for p in MEDIA_DIR.rglob("*"):
            if p.is_file() and _norm(p) not in known:
                _unlink(p)
                orphans += 1
        for d in sorted((d for d in MEDIA_DIR.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    from . import images  # thumbnails of pictures no longer indexed
    orphans += images.sweep()
    if expired or orphans:
        log.info("media sweep: %d expired, %d orphaned files removed", len(expired), orphans)
        events.publish("media", status="swept", expired=len(expired))
    return len(expired) + orphans


def _recover() -> None:
    """Files left in 'recording' by a crash are still playable up to the last written page."""
    for r in db.query("SELECT id, path FROM media WHERE status = 'recording'"):
        _finalize(r["id"], probe_duration(r["path"]), "recording was interrupted")


def _sweeper() -> None:
    while True:
        try:
            sweep()
        except Exception:
            log.exception("media sweep failed")
        time.sleep(SWEEP_SECONDS)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    _recover()
    threading.Thread(target=_sweeper, name="media-sweep", daemon=True).start()
    threading.Thread(target=encrypt_existing, name="encrypt-existing", daemon=True).start()
