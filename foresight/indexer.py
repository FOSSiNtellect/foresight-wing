"""Knowledge ingestion: discover → parse → chunk → embed → store, on one background worker."""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import db, events, images, llm, memos, parsers, retrieval, runtime, vault
from .config import settings

log = logging.getLogger(__name__)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "site-packages", "$recycle.bin",
             "appdata", ".cache", "dist", "build", ".idea", ".vscode"}
MAX_FILES_PER_FOLDER = 5000
MAX_URL_BYTES = 8 * 1024 * 1024
RESCAN_SECONDS = 600
MEETING_POLL_SECONDS = 20

_queue: "queue.Queue[int]" = queue.Queue()
_pending: set[int] = set()
_pending_lock = threading.Lock()
_deferred: set[int] = set()  # pictures and audio files waiting for the meeting being recorded to end
_started = False


# ---------------------------------------------------------------- chunking

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Paragraph-aware chunks of roughly `size` characters with a sentence-aligned overlap."""
    size = size or settings.chunk_chars
    overlap = settings.chunk_overlap if overlap is None else overlap
    text = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []

    pieces: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        for sent in _SENTENCE_END.split(para):
            while len(sent) > size:  # pathological run-on text: hard split
                pieces.append(sent[:size])
                sent = sent[size:]
            if sent.strip():
                pieces.append(sent.strip())

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= size or not current:
            current = candidate
            continue
        chunks.append(current)
        tail = current[-overlap:] if overlap else ""
        if tail:
            cut = _SENTENCE_END.search(tail)
            tail = tail[cut.end():] if cut else tail
        current = f"{tail}\n\n{piece}".strip() if tail else piece
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------- source registration

def _upsert_source(uri: str, title: str, kind: str, **extra) -> int:
    row = db.query_one("SELECT id FROM sources WHERE uri = ?", (uri,))
    with db.tx() as conn:
        if row:
            sets = ", ".join(f"{k} = ?" for k in extra)
            conn.execute(
                f"UPDATE sources SET title = ?, status = 'queued', error = NULL{', ' + sets if sets else ''} WHERE id = ?",
                (title, *extra.values(), row["id"]),
            )
            return row["id"]
        cols = ["uri", "title", "kind", "added_at", *extra.keys()]
        cur = conn.execute(
            f"INSERT INTO sources({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (uri, title, kind, db.now(), *extra.values()),
        )
        return cur.lastrowid


def enqueue(source_id: int) -> None:
    with _pending_lock:
        if source_id in _pending:
            return
        _pending.add(source_id)
    _queue.put(source_id)
    events.publish("source", id=source_id, status="queued")


def add_file(path: str | Path, *, kind: str = "file", folder_id: int | None = None, title: str | None = None) -> int:
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    if not parsers.is_supported(p):
        raise parsers.ParseError(f"unsupported file type {p.suffix}")
    extra = {"mtime": p.stat().st_mtime, "mime": p.suffix.lower().lstrip(".")}
    if folder_id is not None:
        extra["folder_id"] = folder_id
    sid = _upsert_source(str(p), title or p.name, kind, **extra)
    enqueue(sid)
    return sid


def add_url(url: str) -> int:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("only http(s) URLs can be added")
    sid = _upsert_source(url.strip(), parsed.netloc + parsed.path, "url", mime="html")
    enqueue(sid)
    return sid


def add_meeting_text(meeting_id: int, which: str, title: str) -> int:
    """Index a meeting transcript or its enhanced notes as a knowledge source."""
    sid = _upsert_source(f"meeting:{meeting_id}:{which}", title, which, meeting_id=meeting_id, mime="md")
    enqueue(sid)
    return sid


def add_folder(path: str) -> dict:
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"')))).resolve()
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    existing = db.query_one("SELECT id FROM folders WHERE path = ?", (str(p),))
    folder_id = existing["id"] if existing else db.execute(
        "INSERT INTO folders(path, added_at) VALUES (?, ?)", (str(p), db.now()))
    queued = scan_folder(folder_id)
    return {"id": folder_id, "path": str(p), "queued": queued}


def scan_folder(folder_id: int) -> int:
    folder = db.query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))
    if not folder:
        return 0
    root = Path(folder["path"])
    known = {r["uri"]: r for r in db.query(
        "SELECT id, uri, mtime, status FROM sources WHERE folder_id = ?", (folder_id,))}
    seen: set[str] = set()
    queued = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if name.startswith(("~$", ".")) or not parsers.is_supported(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if parsers.is_image(path) and (stat.st_size < parsers.MIN_FOLDER_IMAGE_BYTES
                                           or not runtime.get("index_pictures")):
                continue
            if parsers.is_audio(path) and (stat.st_size < parsers.MIN_FOLDER_AUDIO_BYTES
                                           or not runtime.get("index_audio_files")):
                continue
            count += 1
            if count > MAX_FILES_PER_FOLDER:
                break
            uri = str(path.resolve())
            seen.add(uri)
            prev = known.get(uri)
            mtime = stat.st_mtime
            if prev and prev["status"] == "ready" and prev["mtime"] and abs(prev["mtime"] - mtime) < 1:
                continue
            try:
                add_file(path, folder_id=folder_id)
                queued += 1
            except (OSError, parsers.ParseError) as exc:
                log.warning("skip %s: %s", path, exc)
    removed = [r["id"] for uri, r in known.items() if uri not in seen]
    if removed:
        with db.tx() as conn:
            conn.executemany("DELETE FROM sources WHERE id = ?", [(i,) for i in removed])
        images.sweep()
        retrieval.invalidate()
    db.execute("UPDATE folders SET scanned_at = ? WHERE id = ?", (db.now(), folder_id))
    return queued


def remove_source(source_id: int) -> None:
    db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    images.sweep()
    retrieval.invalidate()


def remove_folder(folder_id: int) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM sources WHERE folder_id = ?", (folder_id,))
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    images.sweep()
    retrieval.invalidate()


# ---------------------------------------------------------------- loading text

def _fetch_url(url: str) -> tuple[str, str]:
    headers = {"User-Agent": "ForesightWing/0.1 (+local knowledge import)"}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=30) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").lower()
        body = b""
        for part in resp.iter_bytes():
            body += part
            if len(body) > MAX_URL_BYTES:
                raise parsers.ParseError("page is larger than 8 MB")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        tmp = settings.upload_dir / f"url_{hashlib.sha1(url.encode()).hexdigest()[:12]}.pdf"
        vault.write_bytes(tmp, body)
        return Path(urlparse(url).path).name or url, parsers.extract(tmp)
    if "html" in ctype or body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        title, text = parsers.html_to_text(body)
        return title or url, text
    return url, body.decode(resp.encoding or "utf-8", "replace")


def _meeting_text(uri: str) -> tuple[str, str]:
    _, mid, which = uri.split(":")
    meeting = db.query_one("SELECT * FROM meetings WHERE id = ?", (int(mid),))
    if not meeting:
        raise parsers.ParseError("meeting no longer exists")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meeting["started_at"]))
    if which == "transcript":
        segs = db.query("SELECT speaker, text, t_start FROM segments WHERE meeting_id = ? ORDER BY t_start", (int(mid),))
        body = "\n".join(f"[{int(s['t_start'] // 60):02d}:{int(s['t_start'] % 60):02d}] {s['speaker']}: {s['text']}" for s in segs)
        return f"{meeting['title']} — transcript", f"Meeting: {meeting['title']} ({when})\n\n{body}"
    notes = meeting["enhanced"] or meeting["shorthand"] or ""
    return f"{meeting['title']} — notes", f"Meeting notes: {meeting['title']} ({when})\n\n{notes}"


def _load(source: dict) -> tuple[str, str]:
    kind, uri = source["kind"], source["uri"]
    if kind == "url":
        return _fetch_url(uri)
    if kind in ("transcript", "notes"):
        return _meeting_text(uri)
    return source["title"], parsers.extract(Path(uri))


# ---------------------------------------------------------------- worker

def _process(source_id: int) -> None:
    source = db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
    if not source:
        return
    db.execute("UPDATE sources SET status = 'indexing' WHERE id = ?", (source_id,))
    events.publish("source", id=source_id, status="indexing", title=source["title"])
    try:
        path = Path(source["uri"]) if source["kind"] in ("file", "upload") else None
        picture = path if path and parsers.is_image(path) else None
        recording = path if path and parsers.is_audio(path) else None
        if picture or recording:  # the file's bytes say whether it changed; Gemma looks (or we transcribe) only if it did
            digest = _file_digest(path)
            title, text = source["title"], ""
        else:
            title, text = _load(source)
            text = (text or "").strip()
            if not text:
                raise parsers.ParseError("no extractable text (scanned PDF or empty file?)")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest == source["sha256"] and source["n_chunks"]:
            db.execute("UPDATE sources SET status = 'ready', title = ? WHERE id = ?", (title or source["title"], source_id))
            events.publish("source", id=source_id, status="ready", title=title)
            return
        if picture:
            description = images.describe(picture)
            text = f"Image: {title}\n\n{description or '(Gemma could not describe this picture)'}"
        if recording:
            lines, duration = memos.transcribe(recording)
            text = memos.as_text(title, lines, duration)
        chunks = chunk_text(text)
        vectors = llm.embed(chunks, title=title)
        with db.tx() as conn:
            conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
            conn.executemany(
                "INSERT INTO chunks(source_id, ord, text, embedding, dim) VALUES (?,?,?,?,?)",
                [(source_id, i, c, vectors[i].tobytes(), int(vectors.shape[1])) for i, c in enumerate(chunks)],
            )
            conn.execute(
                "UPDATE sources SET status='ready', error=NULL, title=?, sha256=?, n_chunks=?, n_chars=?, "
                "indexed_at=?, summary=NULL WHERE id=?",
                (title or source["title"], digest, len(chunks), len(text), db.now(), source_id),
            )
        if picture:
            images.attach(source_id, picture, description)
        if recording:
            memos.attach(source_id, recording, lines, duration)
        retrieval.invalidate()
        events.publish("source", id=source_id, status="ready", title=title, n_chunks=len(chunks))
    except Exception as exc:
        log.warning("indexing %s failed: %s", source["uri"], exc)
        db.execute("UPDATE sources SET status = 'error', error = ? WHERE id = ?", (str(exc)[:500], source_id))
        events.publish("source", id=source_id, status="error", error=str(exc)[:200])


def _recording() -> bool:
    from .meetings import manager
    return manager.active_id is not None


def _file_digest(path: Path) -> str:
    """Of the plain bytes, so encrypting a file in place does not make it look changed."""
    h = hashlib.sha256()
    with vault.open_read(path) as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _is_heavy(source_id: int) -> bool:
    """A picture (Gemma describes it) or an audio file (speech recognition): work that competes with a live meeting."""
    row = db.query_one("SELECT kind, uri FROM sources WHERE id = ?", (source_id,))
    if not row or row["kind"] not in ("file", "upload"):
        return False
    return parsers.is_image(Path(row["uri"])) or parsers.is_audio(Path(row["uri"]))


def _after_meeting() -> None:
    """Put pictures and audio files that waited for a meeting back in the queue once it ends."""
    while _recording():
        time.sleep(MEETING_POLL_SECONDS)
    with _pending_lock:
        waiting = list(_deferred)
        _deferred.clear()
    for source_id in waiting:
        enqueue(source_id)


def _wait_for_meeting_end() -> None:
    threading.Thread(target=_after_meeting, name="pictures-after-meeting", daemon=True).start()


def _handle(source_id: int) -> None:
    if _recording() and _is_heavy(source_id):
        # Describing a picture holds Gemma, and transcribing a file the CPU: during a meeting both belong to it.
        with _pending_lock:
            first = not _deferred
            _deferred.add(source_id)
        if first:
            _wait_for_meeting_end()
        return
    _process(source_id)


def _worker() -> None:
    while True:
        source_id = _queue.get()
        with _pending_lock:
            _pending.discard(source_id)
        try:
            _handle(source_id)
        except Exception:
            log.exception("indexer crashed on source %s", source_id)


def rescan_all() -> None:
    for folder in db.query("SELECT id FROM folders"):
        try:
            scan_folder(folder["id"])
        except Exception:
            log.exception("rescan failed for folder %s", folder["id"])


def _rescanner() -> None:
    while True:
        time.sleep(RESCAN_SECONDS)
        rescan_all()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    # Anything interrupted mid-index last time goes back on the queue.
    for row in db.query("SELECT id FROM sources WHERE status IN ('queued', 'indexing')"):
        enqueue(row["id"])
    threading.Thread(target=_worker, name="indexer", daemon=True).start()
    threading.Thread(target=_rescanner, name="rescanner", daemon=True).start()


def queue_depth() -> int:
    return _queue.qsize()
