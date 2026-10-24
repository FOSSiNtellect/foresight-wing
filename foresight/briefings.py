"""Daily Briefings: before each upcoming calendar event, the related knowledge sources, similar past
meetings and a 3-bullet "what to know" summary from Gemma. Cached per event until the event changes."""
from __future__ import annotations

import difflib
import logging
import queue
import re
import threading
import time

from . import db, events, llm, retrieval, runtime

log = logging.getLogger(__name__)

HORIZON_SECONDS = 24 * 3600
K_SOURCES = 5
MAX_PAST_MEETINGS = 3
RETRY_SECONDS = 120

SYSTEM_PROMPT = """You prepare a short briefing for a meeting the user is about to have.
You get the calendar event and numbered context blocks from the user's own documents and past meeting notes.

Write exactly 3 Markdown bullets ("- ...") with the most useful things to know or prepare: decisions, numbers, dates, owners, open questions, and conflicts between sources.
Cite the block each fact came from, e.g. [2]. Only use facts from the context. If the context says little about this meeting, say so in a bullet.
No heading, preamble or closing line."""

# Words that say nothing about what a meeting is about, ignored when matching titles.
_GENERIC = {"the", "and", "for", "with", "about", "meeting", "call", "sync", "weekly", "daily", "monthly",
            "check", "catch", "catchup", "untitled", "chat", "discussion"}
_LINKS = re.compile(r"https?://\S+|\S+@\S+\.\w+")

_queue: "queue.Queue[int]" = queue.Queue()
_pending: set[int] = set()
_errors: dict[int, tuple[float, str]] = {}
_lock = threading.Lock()
_started = False


# ---------------------------------------------------------------- inputs

def _person(name: str) -> str:
    """'dana.kim@example.com' → 'dana kim' (attendees without a display name)."""
    return re.sub(r"[._\-]+", " ", name.split("@", 1)[0]).strip() if "@" in name else name


def _event(event_id: int) -> dict | None:
    ev = db.query_one("SELECT e.*, c.name AS calendar FROM events e JOIN calendars c ON c.id = e.calendar_id "
                      "WHERE e.id = ?", (event_id,))
    if ev:
        ev["attendees"] = db.loads(ev["attendees"], [])
    return ev


def query_text(ev: dict) -> str:
    """Retrieval query: title + description (minus meeting links and e-mail addresses) + attendee names."""
    description = re.sub(r"\s+", " ", _LINKS.sub(" ", ev.get("description") or "")).strip()[:600]
    people = " ".join(_person(a) for a in ev.get("attendees") or [])
    return " ".join(part for part in (ev["title"], description, people) if part)


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and not w.isdigit() and w not in _GENERIC}


def title_similarity(a: str, b: str) -> float:
    wa, wb = _title_words(a), _title_words(b)
    overlap = len(wa & wb) / len(wa | wb) if wa and wb else 0.0
    ratio = difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()
    return max(overlap, ratio if ratio >= 0.8 else 0.0)


def similar_meetings(title: str, *, before: float, limit: int = MAX_PAST_MEETINGS) -> list[dict]:
    """Recorded meetings whose titles look like this event's, most similar (then most recent) first."""
    rows = db.query("SELECT id, title, started_at, summary, enhanced, shorthand FROM meetings "
                    "WHERE status = 'done' AND started_at < ? ORDER BY started_at DESC LIMIT 300", (before,))
    scored = []
    for r in rows:
        score = title_similarity(title, r["title"])
        if score >= 0.5:
            scored.append((score, r))
    scored.sort(key=lambda s: (round(s[0], 2), s[1]["started_at"]), reverse=True)
    return [dict(r, similarity=round(score, 3)) for score, r in scored[:limit]]


def _meeting_notes(m: dict, max_chars: int = 1200) -> str:
    notes = (m.get("summary") or m.get("enhanced") or m.get("shorthand") or "").strip()
    if not notes:
        segs = db.query("SELECT speaker, text FROM segments WHERE meeting_id = ? ORDER BY t_start, id LIMIT 60", (m["id"],))
        notes = "\n".join(f"{s['speaker']}: {s['text']}" for s in segs)
    return notes[:max_chars] or "(no notes or transcript)"


# ---------------------------------------------------------------- generation

def _bullets(text: str) -> str:
    items = [re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line.strip()) for line in text.splitlines()
             if re.match(r"^\s*(?:[-*•]|\d+[.)])\s+\S", line)]
    return "\n".join(f"- {item}" for item in items[:3]) if items else text.strip()


def _cached(ev: dict) -> dict | None:
    row = db.query_one("SELECT summary, citations, created_at FROM briefings WHERE event_id = ? AND fingerprint = ?",
                       (ev["id"], ev["fingerprint"]))
    if row:
        row["citations"] = db.loads(row["citations"], [])
    return row


def generate(event_id: int, *, force: bool = False) -> dict | None:
    """Build (or return the cached) briefing for one event. One retrieval pass and at most one Gemma call."""
    ev = _event(event_id)
    if not ev:
        return None
    if not force and (hit := _cached(ev)):
        return hit

    hits = retrieval.search(query_text(ev), k=K_SOURCES * 2 if runtime.get("rerank") else K_SOURCES)
    if runtime.get("rerank") and len(hits) > 1:
        hits = retrieval.rerank(f"What should I know before the meeting '{ev['title']}'?", hits, K_SOURCES)
    citations = [h.as_citation(n) for n, h in enumerate(hits, 1)]
    blocks = [f"[{n}] {h.title}\n{h.text.strip()[:1500]}" for n, h in enumerate(hits, 1)]
    for m in similar_meetings(ev["title"], before=ev["starts_at"]):
        n = len(citations) + 1
        when = time.strftime("%a %Y-%m-%d", time.localtime(m["started_at"]))
        notes = _meeting_notes(m)
        citations.append({"n": n, "source_id": f"meeting-{m['id']}", "meeting_id": m["id"], "kind": "meeting",
                          "title": f"{m['title']} ({when})", "uri": f"meeting:{m['id']}", "snippet": notes[:400],
                          "similarity": m["similarity"]})
        blocks.append(f"[{n}] Past meeting: {m['title']} ({when})\n{notes}")

    summary = ""
    if blocks:  # with nothing to go on, skip the model rather than have it guess
        when = time.strftime("%A %Y-%m-%d %H:%M", time.localtime(ev["starts_at"]))
        details = [f"Meeting: {ev['title']}", f"When: {when}"]
        if ev["attendees"]:
            details.append("Attendees: " + ", ".join(_person(a) for a in ev["attendees"][:15]))
        if ev["location"]:
            details.append(f"Location: {ev['location']}")
        if ev["description"]:
            details.append("Description: " + ev["description"][:1200])
        context = "\n\n---\n\n".join(blocks)
        text = llm.chat([{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": "\n".join(details) + f"\n\nContext:\n{context}"}],
                        task="briefing", options={"num_predict": 400})
        summary = _bullets(text)

    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
            return None  # the event disappeared from the calendar while Gemma was writing
        conn.execute(
            "INSERT INTO briefings(event_id, fingerprint, summary, citations, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET fingerprint=excluded.fingerprint, summary=excluded.summary, "
            "citations=excluded.citations, created_at=excluded.created_at",
            (event_id, ev["fingerprint"], summary, db.dumps(citations), db.now()))
    return {"summary": summary, "citations": citations, "created_at": db.now()}


# ---------------------------------------------------------------- listing + background worker

def upcoming(*, now: float | None = None, enqueue_missing: bool = True) -> list[dict]:
    """Timed events that have not ended and start within 24 hours, each with its briefing status."""
    now = time.time() if now is None else now
    rows = db.query(
        "SELECT e.id, e.title, e.location, e.attendees, e.starts_at, e.ends_at, e.fingerprint, "
        "c.name AS calendar, b.fingerprint AS b_fingerprint, b.summary, b.citations, b.created_at AS b_created "
        "FROM events e JOIN calendars c ON c.id = e.calendar_id LEFT JOIN briefings b ON b.event_id = e.id "
        "WHERE e.all_day = 0 AND e.ends_at > ? AND e.starts_at < ? ORDER BY e.starts_at, e.id LIMIT 25",
        (now, now + HORIZON_SECONDS))
    out = []
    for r in rows:
        item = {k: r[k] for k in ("id", "title", "location", "starts_at", "ends_at", "calendar")}
        item["attendees"] = db.loads(r["attendees"], [])
        item["briefing"] = None
        err = _errors.get(r["id"])
        if r["b_fingerprint"] == r["fingerprint"]:
            item["status"] = "ready"
            item["briefing"] = {"summary": r["summary"], "citations": db.loads(r["citations"], []),
                                "created_at": r["b_created"]}
        elif err and time.time() - err[0] < RETRY_SECONDS:
            item["status"], item["error"] = "error", err[1]
        else:
            item["status"] = "pending"
            if enqueue_missing:
                enqueue(r["id"])
        out.append(item)
    return out


def enqueue(event_id: int) -> None:
    with _lock:
        if event_id in _pending:
            return
        _pending.add(event_id)
    _queue.put(event_id)


def refresh(event_id: int) -> None:
    """Throw away the cached briefing (e.g. after adding new documents) and write a new one."""
    db.execute("DELETE FROM briefings WHERE event_id = ?", (event_id,))
    _errors.pop(event_id, None)
    enqueue(event_id)


def prewarm() -> None:
    """After a calendar sync, prepare briefings for the next 24 hours, unless a meeting is being recorded
    (live assistance needs Gemma more)."""
    from .meetings import manager
    if manager.active_id is None:
        upcoming()


def _worker() -> None:
    while True:
        event_id = _queue.get()
        with _lock:
            _pending.discard(event_id)
        try:
            if generate(event_id) is not None:
                _errors.pop(event_id, None)
                events.publish("briefing", event_id=event_id, status="ready")
        except Exception as exc:
            log.warning("briefing for event %s failed: %s", event_id, exc)
            _errors[event_id] = (time.time(), str(exc)[:300])
            events.publish("briefing", event_id=event_id, status="error", error=str(exc)[:200])


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, name="briefings", daemon=True).start()
