"""Live Assistance: notice questions in the meeting transcript and answer them from local knowledge."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time

from . import db, events, llm, retrieval, runtime

log = logging.getLogger(__name__)

_INTERROGATIVE = re.compile(
    r"^(what|what's|whats|how|why|when|where|who|whose|which|can|could|do|does|did|is|are|was|were|will|would|"
    r"should|have|has|any|anyone|remind me|tell me|do you know|does anyone know)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

ANSWER_PROMPT = """You help the user during a live meeting. Someone just asked a question. Answer it using ONLY the numbered context from the user's own files.
Reply in at most 3 short sentences or bullets, include the key number/name/date, and cite blocks like [1].
If the context does not answer the question, reply with exactly: NO_ANSWER"""


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2}


def candidate_questions(text: str) -> list[str]:
    """Cheap first-stage detector; the retrieval score and the model's NO_ANSWER act as the second stage."""
    found = []
    for sent in _SENTENCE.split(text.strip()):
        s = sent.strip().strip('"')
        words = s.split()
        if len(words) < 4 or len(words) > 40:
            continue
        if s.endswith("?") or (_INTERROGATIVE.match(s) and not s.endswith(".")):
            found.append(s)
    return found


class LiveAssistant:
    def __init__(self) -> None:
        self.jobs: "queue.Queue[tuple[int, str, str]]" = queue.Queue(maxsize=4)
        self.recent: list[tuple[float, set[str]]] = []
        self._thread = threading.Thread(target=self._loop, name="live-assistant", daemon=True)
        self._thread.start()

    def _is_duplicate(self, question: str) -> bool:
        now = time.time()
        self.recent = [(t, toks) for t, toks in self.recent if now - t < 300]
        toks = _tokens(question)
        for _, prev in self.recent:
            if toks and len(toks & prev) / len(toks | prev) > 0.6:
                return True
        self.recent.append((now, toks))
        return False

    def on_segment(self, meeting_id: int, speaker: str, text: str) -> None:
        if not runtime.get("live_assist"):
            return
        if speaker == "You" and not runtime.get("answer_own_questions"):
            return
        for question in candidate_questions(text):
            if self._is_duplicate(question):
                continue
            events.publish("question", meeting_id=meeting_id, speaker=speaker, question=question)
            try:
                self.jobs.put_nowait((meeting_id, speaker, question))
            except queue.Full:
                log.info("live assistant busy; skipped: %s", question)

    def _loop(self) -> None:
        while True:
            meeting_id, speaker, question = self.jobs.get()
            try:
                self._answer(meeting_id, speaker, question)
            except Exception:
                log.exception("live answer failed")

    def _answer(self, meeting_id: int, speaker: str, question: str) -> None:
        # Text only: the relevance threshold below is calibrated on text-to-text similarity, and audio moments
        # (text-to-audio) score on a different scale.
        hits = retrieval.search(question, k=4, exclude_meeting=meeting_id, with_media=False)
        min_score = float(runtime.get("live_min_score"))
        if not hits or max(h.similarity for h in hits) < min_score:
            events.publish("card_skip", meeting_id=meeting_id, question=question, reason="no relevant sources")
            return
        context = retrieval.format_context(hits, max_chars=5000)
        answer = llm.chat([
            {"role": "system", "content": ANSWER_PROMPT},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion ({speaker}): {question}"},
        ], task="live_assist", options={"num_predict": 220}).strip()
        if not answer or "NO_ANSWER" in answer:
            events.publish("card_skip", meeting_id=meeting_id, question=question, reason="not in sources")
            return
        citations = [h.as_citation(i) for i, h in enumerate(hits, 1)]
        insight_id = db.execute(
            "INSERT INTO insights(meeting_id, question, answer, citations, created_at) VALUES (?,?,?,?,?)",
            (meeting_id, question, answer, db.dumps(citations), db.now()))
        events.publish("card", id=insight_id, meeting_id=meeting_id, question=question, answer=answer,
                       citations=citations, created_at=db.now())


_assistant: LiveAssistant | None = None


def get() -> LiveAssistant:
    global _assistant
    if _assistant is None:
        _assistant = LiveAssistant()
    return _assistant
