"""Gemma's second pass over Hindi / Hinglish meetings.

No engine on a typical CPU is both live and accurate for Hindi: Parakeet does not support it, and Whisper `small`
keeps up but makes mistakes. So a Hindi / Hinglish meeting is transcribed live by Whisper, and afterwards Gemma 4
listens to every line again (FLEURS Hindi WER 10.1% for E2B vs ~28% for Whisper large-v3; see
docs/multimodal-memory-design.md) and writes it in Latin-letter Hinglish, the way people type their questions.

The old wording of each changed line is kept in segment_versions. The meeting's audio is saved for this pass even when
"Keep meeting audio" is off, and is deleted as soon as the pass is done in that case.

Live transcription may not keep up on a slow CPU, and it is not waited for at the end of a Hindi meeting. So the pass
first runs the same voice detector over the saved audio and adds a line for any speech that has none (fill_gaps).
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time

import numpy as np

from . import db, events, listen, llm, media

log = logging.getLogger(__name__)

PAD_S = 0.2              # a little audio around each line, so first and last syllables are not clipped
MAX_GROWTH = 4           # an answer this many times longer than the line (+ slack) is treated as a hallucination
MAX_GROWTH_SLACK = 60
CHARS_PER_S = 40         # ...unless it still fits this many characters per second of audio (fast speech is ~15-20)
GAP_COVERED = 0.5        # speech counts as transcribed when existing lines cover this share of it
PLACEHOLDER = "…"        # a line added by fill_gaps, waiting for Gemma
MIN_GAP_SPEECH_S = 1.0   # a gap needs this much detected speech to become a line
MIN_LINE_SPEECH_S = 0.3  # a live line with less is text written over music or noise, and is removed
BUSY_WAIT_S = 15
_REFUSAL = re.compile(r"^(i'?m sorry|sorry|i cannot|i can't|as an ai|there is no (speech|audio))", re.I)
# Gemma sometimes answers a clip like a chatbot instead of transcribing it (seen on a jingle: "नमस्ते! मैं आपकी कैसे मदद
# कर सकता हूँ?"). Short replies like these are not speech.
_ASSISTANT = re.compile(r"how (can|may) i (help|assist)|(मैं|main) (आपकी|aapki) (कैसे|क्या|kaise|kya) (मदद|madad)|"
                        r"as an ai|language model|no (speech|audio) (is|was)? ?(detected|present|heard)|"
                        r"i (can'?t|cannot|am unable to) (hear|transcribe|understand)|"
                        r"\b(i am|i'm|main|mai|मैं) (an?|ek|एक) (ai|एआई)\b|\bai (assistant|model|hoon|hu)\b|"
                        + listen.NO_SPEECH, re.I)
_LABEL = re.compile(r"^\s*(language|transcript(ion)?|text)\s*:\s*", re.I)  # "Language: …" before the words
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")  # the Devanagari block, U+0900 to U+097F
_PREAMBLE = re.compile(r"^(here is|here's|sure|okay,|transliteration|summary)", re.I)
LATIN_PROMPT = ("Write this Hindi / Hinglish transcript in Latin letters, the way people type Hinglish in chat. "
                "Keep every word and keep English words as they are. Reply with the text only.")

_queue: "queue.Queue[int]" = queue.Queue()
_queued: set[int] = set()
_lock = threading.Lock()
_started = False


class _Paused(Exception):
    pass


def prepare(meeting_id: int, *, temp_audio: bool) -> None:
    """Register a meeting for the pass (called when it starts); it runs once the meeting is done."""
    db.execute("INSERT INTO transcript_passes(meeting_id, status, temp_audio, updated_at) VALUES (?,?,?,?) "
               "ON CONFLICT(meeting_id) DO UPDATE SET status = excluded.status, temp_audio = excluded.temp_audio, "
               "updated_at = excluded.updated_at", (meeting_id, "waiting", int(temp_audio), db.now()))


def status(meeting_id: int) -> dict | None:
    return db.query_one("SELECT status, done, total, changed, temp_audio, error FROM transcript_passes WHERE meeting_id = ?",
                        (meeting_id,))


def enqueue(meeting_id: int) -> None:
    db.execute("UPDATE transcript_passes SET status = 'queued', updated_at = ? WHERE meeting_id = ? AND status != 'done'",
               (db.now(), meeting_id))
    with _lock:
        if meeting_id in _queued:
            return
        _queued.add(meeting_id)
    _queue.put(meeting_id)
    events.publish("transcript_pass", meeting_id=meeting_id, status="queued")


def acceptable(old: str, new: str, seconds: float = 0.0) -> bool:
    """Use Gemma's wording unless it is empty, a refusal, or far longer than the line and its audio could hold
    (a hallucination)."""
    new = new.strip()
    limit = max(MAX_GROWTH * len(old) + MAX_GROWTH_SLACK, CHARS_PER_S * seconds)
    chatbot = len(new) < 160 and _ASSISTANT.search(new)
    return bool(new) and not _REFUSAL.match(new) and not chatbot and len(new) <= limit


def devanagari_share(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha() or _DEVANAGARI.match(ch)]
    return sum(1 for ch in letters if _DEVANAGARI.match(ch)) / len(letters) if letters else 0.0


def in_latin_letters(text: str) -> str:
    """Gemma is asked for Latin-letter Hinglish but sometimes answers in Devanagari. Transliterate those lines (a
    cheap text-only call), so a transcript reads one way and matches how questions are typed."""
    if devanagari_share(text) < 0.3:
        return text
    latin = " ".join(llm.chat([{"role": "system", "content": LATIN_PROMPT}, {"role": "user", "content": text}],
                              task="transcript_pass", options={"temperature": 0, "num_predict": 400}).split())
    # Use it only if it really is Latin and keeps the words (a transliteration, not a summary or an answer).
    words, latin_words = len(text.split()), len(latin.split())
    if (latin and devanagari_share(latin) < 0.1 and 0.7 * words <= latin_words <= 1.3 * words + 1
            and not _PREAMBLE.match(latin)):
        return latin
    return text


def speech_seconds(pcm: np.ndarray) -> float:
    """Seconds of speech in 16 kHz float32 audio, by the Silero detector bundled with faster-whisper (no download).
    Unlike the loudness-based detector used live, it tells speech from music: Gemma, given a jingle, answers like a
    chatbot ("Main ek AI hoon") instead of transcribing, so clips without speech are never sent to it."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except ImportError:  # detector unavailable: treat everything as speech, as before
        return len(pcm) / 16000
    return sum(t["end"] - t["start"] for t in get_speech_timestamps(pcm, VadOptions())) / 16000


def fill_gaps(meeting_id: int, channels: dict[str, int]) -> int:
    """Add an empty line for speech in the saved audio that has no transcript line yet: the live backlog left at
    the end of the meeting, lines live transcription dropped, or anything lost when the app stopped."""
    from .asr import SR, Segmenter  # the voice detector only (numpy); no speech model is loaded

    added = 0
    for channel, media_id in channels.items():
        row = media.playable(media_id)
        if not row:
            continue
        pcm = media.decode_pcm16(row["path"]).astype(np.float32) / 32768
        detector = Segmenter(channel)
        utterances = []
        for i in range(0, len(pcm), SR * 10):
            utterances += detector.feed(pcm[i:i + SR * 10])
        tail = detector.flush()
        if tail:
            utterances.append(tail)
        lines = db.query("SELECT t_start, t_end FROM segments WHERE meeting_id = ? AND speaker = ?", (meeting_id, channel))
        for u in utterances:
            t0, t1 = u.t_start, u.t_start + len(u.audio) / SR
            covered = sum(max(0.0, min(t1, ln["t_end"]) - max(t0, ln["t_start"])) for ln in lines)
            if covered < GAP_COVERED * (t1 - t0) and speech_seconds(u.audio) >= MIN_GAP_SPEECH_S:
                db.execute("INSERT INTO segments(meeting_id, t_start, t_end, speaker, text) VALUES (?,?,?,?,?)",
                           (meeting_id, t0, t1, channel, PLACEHOLDER))
                lines.append({"t_start": t0, "t_end": t1})
                added += 1
    return added


def _recording_other(meeting_id: int) -> bool:
    from .meetings import manager
    return manager.active_id is not None and manager.active_id != meeting_id


def run(meeting_id: int) -> dict | None:
    """Re-transcribe every line of one meeting with Gemma. Resumable: lines already done are skipped."""
    row = status(meeting_id)
    if not row or row["status"] == "done":
        return row
    segments = db.query("SELECT id, meeting_id, t_start, t_end, speaker, text FROM segments WHERE meeting_id = ? "
                        "ORDER BY t_start, id", (meeting_id,))
    channels = {r["channel"]: r["id"] for r in db.query(
        "SELECT id, channel FROM media WHERE meeting_id = ? AND kind = 'recording' AND status = 'ready'", (meeting_id,))}
    done, changed = row["done"], row["changed"]
    if done == 0:  # first run only: on a resume the gaps are already filled, and new lines would shift the order
        filled = fill_gaps(meeting_id, channels)
        if filled:
            segments = db.query("SELECT id, meeting_id, t_start, t_end, speaker, text FROM segments WHERE meeting_id = ? "
                                "ORDER BY t_start, id", (meeting_id,))
            events.publish("transcript_pass", meeting_id=meeting_id, status="running", done=0, total=len(segments),
                           filled=filled)
    db.execute("UPDATE transcript_passes SET status = 'running', total = ?, error = NULL, updated_at = ? WHERE meeting_id = ?",
               (len(segments), db.now(), meeting_id))
    removed = 0
    for i in range(done, len(segments)):
        if _recording_other(meeting_id):
            raise _Paused()
        seg = segments[i]
        media_id = channels.get(seg["speaker"])
        pcm = listen.clip_pcm(media_id, seg["t_start"] - PAD_S, seg["t_end"] + PAD_S) if media_id else None
        if pcm is not None and speech_seconds(pcm) < MIN_LINE_SPEECH_S:
            if seg["text"] != PLACEHOLDER:  # live transcription wrote text over music or noise
                db.execute("DELETE FROM segments WHERE id = ?", (seg["id"],))
                removed += 1
            pcm = None
        if pcm is not None:
            text = llm.chat([{"role": "user", "content": listen.TRANSCRIBE_PROMPT,
                              "images": [listen.as_image_field(llm.wav_bytes(pcm))]}],
                            task="transcript_pass", options={"temperature": 0, "num_predict": 300}).strip()
            text = _LABEL.sub("", " ".join(text.split()))
            old = "" if seg["text"] == PLACEHOLDER else seg["text"]
            if acceptable(old, text, seg["t_end"] - seg["t_start"]):
                text = in_latin_letters(text)
            if acceptable(old, text, seg["t_end"] - seg["t_start"]) and text != seg["text"]:
                if old:
                    listen.replace_line(seg, text, replaced_by="gemma")
                else:  # a gap: nothing to keep
                    db.execute("UPDATE segments SET text = ? WHERE id = ?", (text, seg["id"]))
                    events.publish("segment_update", meeting_id=meeting_id, id=seg["id"], text=text)
                changed += 1
        db.execute("UPDATE transcript_passes SET done = ?, changed = ?, updated_at = ? WHERE meeting_id = ?",
                   (i + 1, changed, db.now(), meeting_id))
        events.publish("transcript_pass", meeting_id=meeting_id, status="running", done=i + 1, total=len(segments))

    # Gaps Gemma heard nothing in (music, noise) are not speech after all.
    db.execute("DELETE FROM segments WHERE meeting_id = ? AND text = ?", (meeting_id, PLACEHOLDER))
    listen.refresh_windows(meeting_id)
    listen.reindex_transcript(meeting_id)
    db.execute("UPDATE transcript_passes SET status = 'done', updated_at = ? WHERE meeting_id = ?", (db.now(), meeting_id))
    if row["temp_audio"]:
        media.purge_meeting(meeting_id)  # kept only for this pass
    events.publish("transcript_pass", meeting_id=meeting_id, status="done", changed=changed, total=len(segments),
                   removed=removed)
    return status(meeting_id)


def _worker() -> None:
    while True:
        meeting_id = _queue.get()
        while _recording_other(meeting_id):  # live transcription and Live Assistance come first
            time.sleep(BUSY_WAIT_S)
        with _lock:
            _queued.discard(meeting_id)
        try:
            run(meeting_id)
        except _Paused:
            enqueue(meeting_id)
        except Exception as exc:
            log.warning("transcript pass for meeting %s failed: %s", meeting_id, exc)
            db.execute("UPDATE transcript_passes SET status = 'error', error = ?, updated_at = ? WHERE meeting_id = ?",
                       (str(exc)[:300], db.now(), meeting_id))
            events.publish("transcript_pass", meeting_id=meeting_id, status="error", error=str(exc)[:200])


def retry(meeting_id: int) -> None:
    db.execute("UPDATE transcript_passes SET status = 'queued', error = NULL WHERE meeting_id = ?", (meeting_id,))
    enqueue(meeting_id)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, name="transcript-pass", daemon=True).start()
    # Passes interrupted by a restart pick up where they stopped.
    for r in db.query("SELECT t.meeting_id FROM transcript_passes t JOIN meetings m ON m.id = t.meeting_id "
                      "WHERE t.status IN ('queued', 'running') AND m.status = 'done'"):
        enqueue(r["meeting_id"])
