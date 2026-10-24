"""Meeting sessions: live capture, shorthand notes, Auto-Enhance (map-reduce) and action items."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Iterator

from . import assistant, db, events, indexer, llm, media, repass, runtime
from .asr import LiveCapture, Transcriber

log = logging.getLogger(__name__)

DEFAULT_TITLE = "Untitled meeting"
LANGUAGES = {"en": "English", "hi": "Hindi / Hinglish"}


def _language(value: str | None) -> str:
    if (value or "en") not in LANGUAGES:
        raise ValueError(f"unknown meeting language {value!r}; use one of: {', '.join(LANGUAGES)}")
    return value or "en"
MAP_WINDOW_CHARS = 6000

MAP_PROMPT = """Extract dense factual notes from this part of a meeting transcript.
Use short bullets. Keep every name, number, date, decision, open question and commitment (who will do what, by when).
Do not add anything that is not in the transcript."""

ENHANCE_PROMPT = """You turn a user's quick shorthand from a meeting into polished, trustworthy meeting notes.

You get the user's shorthand and the meeting transcript (or condensed transcript notes).
Write Markdown with exactly these sections:
## Summary
2-4 sentences on what the meeting was about and what was concluded.
## Notes
Follow the structure and order of the user's shorthand. Expand each shorthand point into clear prose or bullets, filling in specifics from the transcript (names, numbers, dates, reasons). Add a few important points from the transcript the user did not write down.
## Decisions
Bullets, or "None recorded."
## Action items
Checklist lines formatted as "- [ ] Owner — task (due date if mentioned)". Use "Unassigned" when nobody was named.

Never invent facts that are not in the shorthand or transcript."""

TITLE_PROMPT = "Give this meeting a short descriptive title (max 7 words). Reply with the title only, no quotes."


def _fmt_ts(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def transcript_text(meeting_id: int) -> str:
    segs = db.query("SELECT t_start, speaker, text FROM segments WHERE meeting_id = ? ORDER BY t_start, id", (meeting_id,))
    return "\n".join(f"[{_fmt_ts(s['t_start'])}] {s['speaker']}: {s['text']}" for s in segs)


def get(meeting_id: int) -> dict | None:
    m = db.query_one("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not m:
        return None
    m["action_items"] = db.loads(m["action_items"], [])
    m["segments"] = db.query("SELECT * FROM segments WHERE meeting_id = ? ORDER BY t_start, id", (meeting_id,))
    m["insights"] = [dict(i, citations=db.loads(i["citations"], [])) for i in
                     db.query("SELECT * FROM insights WHERE meeting_id = ? ORDER BY id", (meeting_id,))]
    m["media"] = media.for_meeting(meeting_id)
    m["transcript_pass"] = repass.status(meeting_id)
    return m


def list_meetings(limit: int = 50) -> list[dict]:
    return db.query(
        "SELECT m.id, m.title, m.started_at, m.ended_at, m.status, m.enhanced IS NOT NULL AS has_notes, "
        "(SELECT COUNT(*) FROM segments s WHERE s.meeting_id = m.id) AS n_segments "
        "FROM meetings m ORDER BY m.started_at DESC LIMIT ?", (limit,))


class MeetingManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active_id: int | None = None
        self.capture: LiveCapture | None = None
        self.store: media.AudioStore | None = None
        self.started: float = 0.0

    def status(self) -> dict:
        if self.active_id is None:
            return {"active": False}
        return {"active": True, "meeting_id": self.active_id, "started_at": self.started,
                "sources": self.capture.sources if self.capture else [],
                "errors": self.capture.errors if self.capture else {},
                "saving_audio": self.store is not None,
                "language": self.capture.language if self.capture else "en"}

    def start(self, title: str | None = None, *, mic: bool = True, system: bool = True, language: str = "en") -> int:
        with self.lock:
            if self.active_id is not None:
                return self.active_id
            if not (mic or system):
                raise ValueError("enable the microphone, system audio, or both")
            language = _language(language)
            self.started = time.time()
            meeting_id = db.execute("INSERT INTO meetings(title, started_at, status, language) VALUES (?,?,?,?)",
                                    ((title or "").strip() or DEFAULT_TITLE, self.started, "recording", language))
            capture = LiveCapture(lambda *a: self._on_segment(meeting_id, *a), mic=mic, system=system, language=language,
                                  on_level=lambda lv: events.publish("levels", meeting_id=meeting_id, **lv))
            keep = bool(runtime.get("keep_meeting_audio"))
            # Hindi / Hinglish meetings always save audio: Gemma re-transcribes it afterwards (repass.py), and
            # the audio is deleted then unless the user keeps meeting audio.
            store = media.AudioStore(meeting_id, capture.sources) if keep or language == "hi" else None
            if language == "hi":
                repass.prepare(meeting_id, temp_audio=not keep)
            if store:
                capture.on_audio = store.feed
            try:
                capture.start()
            except Exception:
                if store:
                    store.discard()
                db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
                raise
            self.active_id, self.capture, self.store = meeting_id, capture, store
        events.publish("meeting", id=meeting_id, status="recording")
        return meeting_id

    def _on_segment(self, meeting_id: int, speaker: str, t0: float, t1: float, text: str) -> None:
        seg_id = db.execute("INSERT INTO segments(meeting_id, t_start, t_end, speaker, text) VALUES (?,?,?,?,?)",
                            (meeting_id, t0, t1, speaker, text))
        events.publish("segment", meeting_id=meeting_id, id=seg_id, t_start=t0, t_end=t1, speaker=speaker, text=text)
        assistant.get().on_segment(meeting_id, speaker, text)

    def stop(self) -> int | None:
        with self.lock:
            meeting_id, capture, store = self.active_id, self.capture, self.store
            if meeting_id is None:
                return None
            self.active_id, self.capture, self.store = None, None, None
        events.publish("meeting", id=meeting_id, status="finishing")
        if capture:
            # A Hindi / Hinglish meeting does not wait for live transcription to catch up: Gemma's pass
            # (repass.fill_gaps) transcribes whatever was left from the saved audio.
            dropped = capture.stop(transcribe_backlog=capture.language != "hi")
            if dropped:
                log.info("meeting %s: %d utterances left for Gemma's pass", meeting_id, dropped)
        if store:
            store.close()  # after capture.stop(): every captured block has been handed over
        self._finish(meeting_id)
        return meeting_id

    def _finish(self, meeting_id: int) -> None:
        db.execute("UPDATE meetings SET status = 'done', ended_at = ? WHERE id = ?", (time.time(), meeting_id))
        events.publish("meeting", id=meeting_id, status="done")
        threading.Thread(target=self._post_process, args=(meeting_id,), daemon=True).start()
        if repass.status(meeting_id):
            repass.enqueue(meeting_id)  # Gemma's second pass over a Hindi / Hinglish meeting

    def _post_process(self, meeting_id: int) -> None:
        text = transcript_text(meeting_id)
        if not text.strip():
            return
        meeting = db.query_one("SELECT title FROM meetings WHERE id = ?", (meeting_id,))
        title = meeting["title"]
        if title == DEFAULT_TITLE:
            try:
                title = llm.chat([{"role": "system", "content": TITLE_PROMPT},
                                  {"role": "user", "content": text[:4000]}],
                                 task="title", options={"num_predict": 24}).strip().strip('"#* ')[:80] or title
                db.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, meeting_id))
                events.publish("meeting", id=meeting_id, status="done", title=title)
            except Exception:
                log.exception("title generation failed")
        indexer.add_meeting_text(meeting_id, "transcript", f"{title} — transcript")

    def import_recording(self, path: Path, title: str | None = None, language: str = "en") -> int:
        language = _language(language)
        meeting_id = db.execute("INSERT INTO meetings(title, started_at, status, language) VALUES (?,?,?,?)",
                                ((title or "").strip() or Path(path).stem, time.time(), "importing", language))
        keep = bool(runtime.get("keep_meeting_audio"))
        if language == "hi":
            repass.prepare(meeting_id, temp_audio=not keep)

        def run() -> None:
            try:
                for t0, t1, text in media.transcribe(Transcriber.for_language(language), path):
                    self._on_segment(meeting_id, "Recording", t0, t1, text)
            except Exception as exc:
                log.exception("import failed")
                events.publish("meeting", id=meeting_id, status="error", error=str(exc))
            # Kept for playback if the user keeps meeting audio; a Hindi / Hinglish recording also until Gemma's pass.
            media.adopt_import(meeting_id, path, keep=keep or language == "hi")
            self._finish(meeting_id)

        threading.Thread(target=run, name="import-recording", daemon=True).start()
        events.publish("meeting", id=meeting_id, status="importing")
        return meeting_id


manager = MeetingManager()


def update(meeting_id: int, *, title: str | None = None, shorthand: str | None = None) -> None:
    if title is not None:
        db.execute("UPDATE meetings SET title = ? WHERE id = ?", (title.strip() or DEFAULT_TITLE, meeting_id))
    if shorthand is not None:
        db.execute("UPDATE meetings SET shorthand = ? WHERE id = ?", (shorthand, meeting_id))


def delete(meeting_id: int) -> None:
    if manager.active_id == meeting_id:
        manager.stop()
    media.purge_meeting(meeting_id)  # files first: the rows go with the meeting, the files would not
    db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    from . import retrieval
    retrieval.invalidate()


def _windows(text: str, size: int) -> list[str]:
    lines, out, cur = text.splitlines(), [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def _parse_action_items(markdown: str) -> list[str]:
    section = re.split(r"^##\s*Action items\s*$", markdown, flags=re.I | re.M)
    if len(section) < 2:
        return []
    body = re.split(r"^##\s", section[1], flags=re.M)[0]
    items = []
    for line in body.splitlines():
        m = re.match(r"^\s*[-*]\s*(\[[ xX]\]\s*)?(.+)$", line)
        if m and "none recorded" not in m.group(2).lower():
            items.append(m.group(2).strip())
    return items


def enhance_stream(meeting_id: int) -> Iterator[dict]:
    meeting = db.query_one("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    if not meeting:
        raise KeyError(meeting_id)
    transcript = transcript_text(meeting_id)
    shorthand = (meeting["shorthand"] or "").strip() or "(the user took no notes; build the notes from the transcript)"
    if not transcript.strip():
        transcript = "(no transcript was captured)"

    material = transcript
    if len(transcript) > MAP_WINDOW_CHARS:
        windows = _windows(transcript, MAP_WINDOW_CHARS)
        partial = []
        for i, window in enumerate(windows, 1):
            yield {"type": "progress", "step": f"Reading transcript part {i} of {len(windows)}"}
            partial.append(llm.chat([{"role": "system", "content": MAP_PROMPT},
                                     {"role": "user", "content": window}], task="enhance_map"))
        material = "\n\n".join(f"Part {i}:\n{p}" for i, p in enumerate(partial, 1))

    yield {"type": "progress", "step": "Writing enhanced notes"}
    out = []
    for delta in llm.chat_stream([
        {"role": "system", "content": ENHANCE_PROMPT},
        {"role": "user", "content": f"Meeting title: {meeting['title']}\n\nUser shorthand:\n{shorthand}\n\n"
                                    f"Transcript:\n{material}"},
    ], task="enhance", options={"num_predict": 1500}):
        out.append(delta)
        yield {"type": "delta", "text": delta}
    enhanced = "".join(out).strip()
    items = _parse_action_items(enhanced)
    summary = ""
    m = re.search(r"^##\s*Summary\s*$(.+?)(^##\s|\Z)", enhanced, flags=re.I | re.M | re.S)
    if m:
        summary = m.group(1).strip()
    db.execute("UPDATE meetings SET enhanced = ?, summary = ?, action_items = ? WHERE id = ?",
               (enhanced, summary, db.dumps(items), meeting_id))
    indexer.add_meeting_text(meeting_id, "notes", f"{meeting['title']} — notes")
    yield {"type": "done", "action_items": items}


def export_markdown(meeting_id: int) -> str:
    m = get(meeting_id)
    if not m:
        raise KeyError(meeting_id)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["started_at"]))
    parts = [f"# {m['title']}", f"*{when}*", ""]
    if m["enhanced"]:
        parts += [m["enhanced"], ""]
    elif m["shorthand"]:
        parts += ["## My notes", m["shorthand"], ""]
    if m["insights"]:
        parts.append("## Live answers")
        for i in m["insights"]:
            parts.append(f"- **{i['question']}** — {i['answer']}")
        parts.append("")
    parts += ["## Transcript", transcript_text(meeting_id) or "_(empty)_"]
    return "\n".join(parts)
