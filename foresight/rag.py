"""Conversational retrieval over the local knowledge hub, with numbered citations."""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import replace
from typing import Iterator

from . import dates, db, images, listen, llm, repass, retrieval, runtime
from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Foresight Wing, a private assistant running entirely on the user's own computer.
You answer questions using the user's personal knowledge sources (documents, web pages, meeting transcripts and notes) provided as numbered context blocks.

Rules:
- Base every factual statement on the context. Cite the block it came from with its number in square brackets, e.g. [2]. Combine citations like [1][3] when needed.
- If the context does not contain the answer, say plainly that you could not find it in the user's sources, then (optionally) offer general knowledge clearly marked as such.
- When sources disagree (different numbers, dates or decisions), point out the conflict and cite both sides.
- A block marked [T] (when present) is the transcript of the meeting happening right now; cite it as [T].
- Be concise. Prefer short paragraphs or bullet lists in Markdown. Never invent sources.
Today is {today}."""


def _recent_meetings_note() -> str:
    rows = db.query("SELECT title, started_at FROM meetings WHERE status = 'done' ORDER BY started_at DESC LIMIT 8")
    if not rows:
        return ""
    lines = [f"- {time.strftime('%a %Y-%m-%d %H:%M', time.localtime(r['started_at']))}: {r['title']}" for r in rows]
    return "\n\nRecent meetings recorded by the user:\n" + "\n".join(lines)


def history(conv_id: str, limit: int = 50) -> list[dict]:
    rows = db.query(
        "SELECT role, content, citations, created_at FROM chat_messages WHERE conv_id = ? ORDER BY id DESC LIMIT ?",
        (conv_id, limit))
    for r in rows:
        r["citations"] = db.loads(r["citations"], [])
    return list(reversed(rows))


def conversations(limit: int = 30) -> list[dict]:
    return db.query(
        "SELECT conv_id, MIN(created_at) AS started_at, "
        "(SELECT content FROM chat_messages m2 WHERE m2.conv_id = m.conv_id AND role = 'user' ORDER BY id LIMIT 1) AS first "
        "FROM chat_messages m GROUP BY conv_id ORDER BY MAX(id) DESC LIMIT ?", (limit,))


LISTEN_NOTE = ("\n\nAudio clips are attached, in this order: {order}. Each one is the recording behind that numbered block. "
               "Listen to them to check names, numbers and how things were said. Where what you hear differs from the "
               "block's transcript, trust the audio and say what differs.")
# Asked together with the question, Gemma E2B tends to repeat a block's transcript even where the audio says otherwise
# (a memo saying "four thousand euros", transcribed "€400", was answered "€400"). Transcribing each clip on its own
# first gets it right, and those words then replace the transcript lines the clip covers. (Given both, in a separate
# section or side by side in the block, E2B still went with the transcript about half the time.) The answer is written
# from text: sending the clips again would double the audio Gemma processes. If hearing fails, the clips go with the
# question as before.
HEAR_PROMPT = ("Transcribe this audio word for word, in the language it is spoken (Hindi or Hinglish in Latin letters, "
               "as it sounds). Write numbers, amounts and dates in words, exactly as they are said. Reply with the "
               f"transcript only. If there is no speech, only music or noise, reply exactly {listen.NO_SPEECH}.")
HEARD_NOTE = ("\n\nIn blocks {order}, the lines marked 'Heard in the recording' were listened to word by word: they "
              "replace the speech-recognition transcript there, which can get names, numbers and amounts wrong. "
              "Answer from what was heard.")
_STAMP = re.compile(r"^\[(\d+):(\d{2})\]")


def _fmt(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def with_heard(text: str, span: tuple[float, float], words: str) -> str:
    """A block with the transcript lines its clip covers ("[mm:ss] …" lines starting inside the span, and their
    continuation lines) replaced by what Gemma heard there. Without timestamps, what was heard is the passage."""
    t0, t1 = span
    heard_line = f"[{_fmt(t0)}–{_fmt(t1)}] Heard in the recording, word for word: {words}"
    out, inserted, dropping = [], False, False
    for line in text.splitlines():
        stamp = _STAMP.match(line.strip())
        if stamp:
            t = int(stamp.group(1)) * 60 + int(stamp.group(2))
            dropping = t0 - 0.5 <= t < t1
        if dropping:
            if not inserted:
                out.append(heard_line)
                inserted = True
            continue
        out.append(line)
    return "\n".join(out) if inserted else heard_line
MAX_CLIPS = 2
LOOK_NOTE = ("\n\nPictures are attached, in this order: {order}. Each one is the image behind that numbered block. "
             "Look at them to answer; where a picture shows more than its block's description, trust the picture.")
MAX_PICTURES = 2


def _clips(citations: list[dict], focus_n: int | None, listen_audio: bool) -> list[tuple[int, dict]]:
    """(block number, moment) pairs Gemma will hear: the picked moment first, then cited moments if asked to listen."""
    picked = [(c["n"], c["media"]) for c in citations if c["n"] == focus_n]
    if listen_audio:
        for c in citations:
            m = c.get("media")
            if (len(picked) < MAX_CLIPS and m and m.get("url") and m.get("kind") != "image"
                    and all(m != p for _, p in picked)):
                picked.append((c["n"], m))
    return picked


def _hear(clips: list[tuple[int, str, float]]) -> dict[int, str]:
    """{block number: what Gemma hears in that clip}, each clip transcribed on its own. Clips without speech, failed
    calls and chatbot-style replies are left out."""
    heard: dict[int, str] = {}
    for n, clip, seconds in clips:
        try:
            text = llm.chat([{"role": "user", "content": HEAR_PROMPT, "images": [clip]}], task="hear",
                            options={"temperature": 0, "num_predict": 400}).strip()
        except Exception:
            log.warning("could not transcribe clip [%s] for the answer", n, exc_info=True)
            continue
        if text and listen.NO_SPEECH not in text and repass.acceptable("", text, seconds):
            heard[n] = text
    return heard


def answer_stream(question: str, conv_id: str | None = None, label: str | None = None,
                  meeting_id: int | None = None, *, listen_audio: bool = False,
                  focus: dict | None = None) -> Iterator[dict]:
    """Answer with citations. `focus` ({media_id, t_start, t_end}) is a moment the user picked: Gemma hears it.
    `listen_audio` also lets Gemma hear up to two cited audio moments (slower; catches transcription mistakes)."""
    conv_id = conv_id or uuid.uuid4().hex
    past = history(conv_id, limit=8)

    retrieval_query = question
    prev_user = next((m["content"] for m in reversed(past) if m["role"] == "user"), None)
    if prev_user and len(question.split()) < 8:  # short follow-ups ("and the budget?") need the earlier topic
        retrieval_query = f"{prev_user}\n{question}"
    rerank = bool(runtime.get("rerank"))
    k = settings.retrieve_k * 2 if rerank else settings.retrieve_k
    # "What did we decide in last Tuesday's meeting?": search only the meetings recorded then (all sources if none match).
    window = dates.parse_window(question)
    in_window = db.query("SELECT id, title, started_at FROM meetings WHERE started_at >= ? AND started_at < ? "
                         "ORDER BY started_at", (window[0], window[1])) if window else []
    hits = retrieval.search(retrieval_query, k=k, label=label, exclude_meeting=meeting_id,
                            meeting_ids={m["id"] for m in in_window}) if in_window else []
    if not hits:
        hits = retrieval.search(retrieval_query, k=k, label=label, exclude_meeting=meeting_id)
    if rerank and len(hits) > 1:  # Gemma reads the candidates against the question and keeps the best
        hits = retrieval.rerank(question, hits, settings.retrieve_k)
    citations = [h.as_citation(i) for i, h in enumerate(hits, 1)]

    focus_n = None
    picked = listen.moment(int(focus["media_id"]), float(focus["t_start"]), float(focus["t_end"])) if focus else None
    if picked:
        focus_n = len(citations) + 1
        citations.append({"n": focus_n, "source_id": f"moment-{picked['media']['media_id']}", "title": picked["title"],
                          "uri": f"media:{picked['media']['media_id']}#t={picked['media']['t_start']:.1f}", "kind": "audio",
                          "snippet": picked["text"][:400], "similarity": 0, "media": picked["media"]})
    clips, heard, seconds, spans = [], [], [], {}
    for n, m in _clips(citations, focus_n, listen_audio):
        wav = listen.clip_wav(m["media_id"], m["t_start"], m["t_end"])
        if wav:
            clips.append(listen.as_image_field(wav))
            heard.append(n)
            seconds.append(min(m["t_end"] - m["t_start"], listen.CLIP_MAX_S))
            spans[n] = (m["t_start"], m["t_start"] + seconds[-1])
    pictures, looked = [], []  # Gemma looks at cited pictures (the description alone can miss what matters)
    for c in citations:
        m = c.get("media") or {}
        if m.get("kind") == "image" and len(pictures) < MAX_PICTURES and c["n"] not in looked:
            b64 = images.for_gemma(m["media_id"])
            if b64:
                pictures.append(b64)
                looked.append(c["n"])
    yield {"type": "start", "conv_id": conv_id, "citations": citations, "listened": heard, "looked": looked,
           "window": {"label": window[2], "meetings": len(in_window)} if window else None}

    words = _hear(list(zip(heard, clips, seconds))) if clips else {}
    if words:
        yield {"type": "heard", "texts": {str(n): text for n, text in words.items()}}

    def block(n: int, text: str) -> str:
        return with_heard(text, spans[n], words[n]) if n in words else text

    shown = [replace(h, text=block(n, h.text)) for n, h in enumerate(hits, 1)]
    context = retrieval.format_context(shown) if hits else "(no matching sources were found)"
    if picked:
        context += (f"\n\n---\n\n[{focus_n}] {picked['title']} (the moment the user is asking about)\n"
                    + block(focus_n, picked["text"] or "(no transcript for this part)"))
    if window:
        listed = "\n".join(f"- {time.strftime('%a %d %b %H:%M', time.localtime(m['started_at']))}: {m['title']}"
                           for m in in_window) or "(no meetings were recorded then)"
        context += f"\n\n---\n\nThe question refers to {window[2]}. Meetings recorded then:\n{listed}"

    if meeting_id is not None:
        from .meetings import transcript_text
        live = transcript_text(meeting_id)[-3500:]
        if live:
            context += f"\n\n---\n\n[T] Transcript of the current meeting (most recent part)\n{live}"
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(today=time.strftime("%A %Y-%m-%d")) + _recent_meetings_note()}]
    for m in past[-6:]:
        messages.append({"role": m["role"], "content": m["content"]})
    user = {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
    if clips:
        user["content"] += (HEARD_NOTE if words else LISTEN_NOTE).format(order=", ".join(f"[{n}]" for n in heard))
    if words:
        clips = []  # already heard: the answer works from the words
    if pictures:  # after any audio clips, in the same field
        user["content"] += LOOK_NOTE.format(order=", ".join(f"[{n}]" for n in looked))
    if clips or pictures:  # Ollama takes Gemma 4's audio input as WAV bytes in `images`, like pictures
        user["images"] = clips + pictures
    messages.append(user)

    db.execute("INSERT INTO chat_messages(conv_id, role, content, created_at) VALUES (?,?,?,?)",
               (conv_id, "user", question, db.now()))
    answer = []
    try:
        for delta in llm.chat_stream(messages, task="chat"):
            answer.append(delta)
            yield {"type": "delta", "text": delta}
    finally:
        text = "".join(answer).strip()
        if text:
            db.execute("INSERT INTO chat_messages(conv_id, role, content, citations, created_at) VALUES (?,?,?,?,?)",
                       (conv_id, "assistant", text, db.dumps(citations), db.now()))
    yield {"type": "done", "conv_id": conv_id}
