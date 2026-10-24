"""HTTP + WebSocket API and static UI. Binds to localhost only."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (asr, briefings, calendars, db, doctor, events, graph, images, indexer, listen, llm, media, media_index,
               meetings, memos, parsers, provision, rag, repass, retrieval, runtime, vault, wiki)
from .config import settings

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".mp4", ".aac", ".opus"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    events.bind_loop(asyncio.get_running_loop())
    indexer.start()
    briefings.start()
    calendars.start(on_sync=briefings.prewarm)
    media.start()
    media_index.start()
    repass.start()
    yield
    if meetings.manager.active_id is not None:
        await asyncio.to_thread(meetings.manager.stop)


app = FastAPI(title="Foresight Wing", lifespan=lifespan)


# ---------------------------------------------------------------- local-only guard

_ALLOWED_HOSTS = {f"127.0.0.1:{settings.port}", f"localhost:{settings.port}", "127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    if host not in _ALLOWED_HOSTS:  # blocks DNS-rebinding pages from reaching the API
        return PlainTextResponse("forbidden host", status_code=403)
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        # A custom header forces a CORS preflight, which we never grant, so other sites cannot POST here.
        if request.headers.get("x-foresight") != "1":
            return PlainTextResponse("missing X-Foresight header", status_code=403)
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        # Revalidate the UI on every load (cheap on localhost), so an updated app.js is never replaced by a cached one.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def _ndjson(gen: Iterator[dict]) -> StreamingResponse:
    def body():
        try:
            for item in gen:
                yield json.dumps(item, ensure_ascii=False) + "\n"
        except Exception as exc:
            log.exception("stream failed")
            yield json.dumps({"type": "error", "error": str(exc)}) + "\n"
    return StreamingResponse(body(), media_type="application/x-ndjson")


# ---------------------------------------------------------------- status & settings

@app.get("/api/setup")
def setup_check():
    """First-run check (doctor.py): what is ready on this PC, and how to fix what is not."""
    return doctor.run()


@app.post("/api/setup/run")
def setup_run():
    """"Set up now": install or start Ollama and pull the models (downloads, so only when the user presses it)."""
    return provision.start()


@app.get("/api/setup/progress")
def setup_progress():
    return provision.status()


@app.get("/api/status")
def status():
    counts = db.query_one(
        "SELECT (SELECT COUNT(*) FROM sources) AS sources, (SELECT COUNT(*) FROM sources WHERE status='ready') AS ready, "
        "(SELECT COUNT(*) FROM sources WHERE status='error') AS errors, (SELECT COUNT(*) FROM chunks) AS chunks, "
        "(SELECT COUNT(*) FROM meetings) AS meetings")
    return {"llm": llm.status(), "counts": counts, "queue": indexer.queue_depth(),
            "meeting": meetings.manager.status(), "prefs": runtime.all_values(),
            "models": {"gen": settings.gen_model, "embed": settings.embed_model, "asr": settings.asr_label},
            "media": {"caps": _media_caps(), **media.usage()},
            "data_dir": str(settings.data_dir)}


_media_check = threading.Lock()


def _media_caps() -> dict:
    """Audio/vision support of the installed models. The first status call starts the check in the background."""
    caps = llm.media_status()
    if not caps.get("checked") and _media_check.acquire(blocking=False):
        def run():
            try:
                llm.check_media()
            finally:
                _media_check.release()
        threading.Thread(target=run, name="media-check", daemon=True).start()
    return caps


@app.get("/api/audio-devices")
def audio_devices():
    return asr.list_devices()


@app.put("/api/settings")
def put_settings(changes: dict):
    values = runtime.update(changes)
    if "audio_retention_days" in changes:
        media.apply_retention()  # a shorter retention deletes older audio right away (the UI asks first)
    if "index_pictures" in changes or "index_audio_files" in changes:  # add or drop those files now, not at the next rescan
        threading.Thread(target=indexer.rescan_all, name="rescan", daemon=True).start()
    return values


@app.get("/api/stats")
def stats():
    days = db.query("SELECT day, SUM(prompt_tokens) AS prompt, SUM(completion_tokens) AS completion, COUNT(*) AS calls "
                    "FROM usage WHERE day >= date('now', '-6 days') GROUP BY day ORDER BY day")
    by_task = db.query("SELECT task, SUM(prompt_tokens + completion_tokens) AS tokens, COUNT(*) AS calls "
                       "FROM usage GROUP BY task ORDER BY tokens DESC")
    return {"days": days, "by_task": by_task}


# ---------------------------------------------------------------- knowledge

def _with_labels(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    ids = tuple(r["id"] for r in rows)
    pairs = db.query(f"SELECT sl.source_id, l.name FROM source_labels sl JOIN labels l ON l.id = sl.label_id "
                     f"WHERE sl.source_id IN ({','.join('?' * len(ids))})", ids)
    by_source: dict[int, list[str]] = {}
    for p in pairs:
        by_source.setdefault(p["source_id"], []).append(p["name"])
    for r in rows:
        r["labels"] = sorted(by_source.get(r["id"], []))
    return rows


@app.get("/api/sources")
def list_sources(label: str | None = None):
    sql = ("SELECT s.id, s.uri, s.title, s.kind, s.mime, s.status, s.error, s.n_chunks, s.n_chars, s.added_at, "
           "s.indexed_at, s.folder_id, s.meeting_id FROM sources s")
    params: tuple = ()
    if label:
        sql += " JOIN source_labels sl ON sl.source_id = s.id JOIN labels l ON l.id = sl.label_id WHERE l.name = ?"
        params = (label,)
    return _with_labels(db.query(sql + " ORDER BY s.added_at DESC LIMIT 2000", params))


class FolderIn(BaseModel):
    path: str


class UrlIn(BaseModel):
    url: str


class LabelIn(BaseModel):
    name: str


@app.post("/api/sources/folder")
def add_folder(body: FolderIn):
    try:
        return indexer.add_folder(body.path)
    except (NotADirectoryError, FileNotFoundError) as exc:
        raise HTTPException(400, f"Not a folder: {exc}")


@app.post("/api/sources/url")
def add_url(body: UrlIn):
    try:
        return {"id": indexer.add_url(body.url)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", Path(name).name)[:120] or "file"


@app.post("/api/sources/upload")
async def upload(files: list[UploadFile] = File(...)):
    added, rejected = [], []
    for f in files:
        name = _safe_name(f.filename or "file")
        target = settings.upload_dir / f"{int(time.time() * 1000)}_{name}"
        if not parsers.is_supported(target):
            rejected.append({"name": name, "error": "unsupported type"})
            continue
        vault.write_bytes(target, await f.read())  # encrypted at rest (vault.py)
        try:
            added.append(indexer.add_file(target, kind="upload", title=name))
        except Exception as exc:
            rejected.append({"name": name, "error": str(exc)})
    return {"added": added, "rejected": rejected}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    indexer.remove_source(source_id)
    return {"ok": True}


@app.post("/api/sources/{source_id}/reindex")
def reindex(source_id: int):
    db.execute("UPDATE sources SET sha256 = NULL, status = 'queued' WHERE id = ?", (source_id,))
    indexer.enqueue(source_id)
    return {"ok": True}


@app.get("/api/sources/{source_id}/text")
def source_text(source_id: int):
    src = db.query_one("SELECT id, title, uri, kind FROM sources WHERE id = ?", (source_id,))
    if not src:
        raise HTTPException(404)
    src["chunks"] = [c["text"] for c in db.query("SELECT text FROM chunks WHERE source_id = ? ORDER BY ord", (source_id,))]
    src["picture"] = images.for_source(source_id)
    src["audio"] = memos.for_source(source_id)
    return src


@app.get("/api/folders")
def folders():
    return db.query("SELECT f.*, (SELECT COUNT(*) FROM sources s WHERE s.folder_id = f.id) AS n_sources FROM folders f")


@app.post("/api/folders/{folder_id}/rescan")
def rescan(folder_id: int):
    return {"queued": indexer.scan_folder(folder_id)}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int):
    indexer.remove_folder(folder_id)
    return {"ok": True}


@app.get("/api/labels")
def labels():
    return db.query("SELECT l.name, COUNT(sl.source_id) AS n FROM labels l LEFT JOIN source_labels sl ON sl.label_id = l.id "
                    "GROUP BY l.id ORDER BY l.name")


@app.post("/api/sources/{source_id}/labels")
def add_label(source_id: int, body: LabelIn):
    name = body.name.strip()[:60]
    if not name:
        raise HTTPException(400, "empty label")
    with db.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO labels(name) VALUES (?)", (name,))
        label_id = conn.execute("SELECT id FROM labels WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO source_labels(source_id, label_id) VALUES (?,?)", (source_id, label_id))
    return {"ok": True}


@app.delete("/api/sources/{source_id}/labels/{name}")
def remove_label(source_id: int, name: str):
    with db.tx() as conn:
        conn.execute("DELETE FROM source_labels WHERE source_id = ? AND label_id = (SELECT id FROM labels WHERE name = ?)",
                     (source_id, name))
        conn.execute("DELETE FROM labels WHERE id NOT IN (SELECT label_id FROM source_labels)")
    return {"ok": True}


@app.get("/api/search")
def search(q: str, label: str | None = None, k: int = 10, modality: str = "all"):
    """modality: all | text | audio (results with a playable moment) | image (results that are pictures)."""
    hits = retrieval.search(q, k=min(max(k, 1), 30), label=label, with_media=modality != "text")
    if modality in ("audio", "image"):
        hits = [h for h in hits if h.media and (h.media.get("kind") == "image") == (modality == "image")]
    return [h.as_citation(i) | {"score": round(h.score, 4)} for i, h in enumerate(hits, 1)]


@app.get("/api/graph")
def knowledge_graph(label: str | None = None, limit: int = 300):
    """Sources linked to their most similar sources and to their labels, for the Sources graph view."""
    return graph.build(limit=min(max(limit, 1), 500), label=label or None)


class WikiIn(BaseModel):
    label: str


@app.post("/api/wiki")
def make_wiki(body: WikiIn):
    return _ndjson(wiki.wiki_stream(body.label))


# ---------------------------------------------------------------- chat

class MomentIn(BaseModel):
    media_id: int
    t_start: float
    t_end: float


class ChatIn(BaseModel):
    question: str
    conv_id: str | None = None
    label: str | None = None
    meeting_id: int | None = None
    listen: bool = False            # let Gemma hear cited audio moments
    focus: MomentIn | None = None   # a moment the user picked ("Ask about this moment")


@app.post("/api/chat")
def chat(body: ChatIn):
    if not body.question.strip():
        raise HTTPException(400, "empty question")
    return _ndjson(rag.answer_stream(body.question.strip(), body.conv_id, body.label or None, body.meeting_id,
                                     listen_audio=body.listen, focus=body.focus.model_dump() if body.focus else None))


class SegmentIn(BaseModel):
    text: str


@app.post("/api/segments/{segment_id}/retranscribe")
def retranscribe(segment_id: int):
    """Gemma's hearing of one transcript line, as a suggestion (nothing is saved)."""
    try:
        return listen.retranscribe(segment_id)
    except KeyError:
        raise HTTPException(404)
    except LookupError as exc:
        raise HTTPException(409, str(exc))


@app.put("/api/segments/{segment_id}")
def update_segment(segment_id: int, body: SegmentIn):
    try:
        return listen.update_segment(segment_id, body.text)
    except KeyError:
        raise HTTPException(404)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/chats")
def chats():
    return rag.conversations()


@app.get("/api/chats/{conv_id}")
def chat_history(conv_id: str):
    return rag.history(conv_id, limit=200)


# ---------------------------------------------------------------- meetings

class StartIn(BaseModel):
    title: str | None = None
    mic: bool = True
    system: bool = True
    language: str = "en"  # en | hi (Hindi / Hinglish)


class MeetingPatch(BaseModel):
    title: str | None = None
    shorthand: str | None = None


@app.get("/api/meetings")
def list_meetings():
    return meetings.list_meetings()


@app.post("/api/meetings/start")
async def start_meeting(body: StartIn):
    try:
        meeting_id = await asyncio.to_thread(meetings.manager.start, body.title, mic=body.mic, system=body.system,
                                             language=body.language)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.exception("could not start capture")
        raise HTTPException(500, f"Could not start audio capture: {exc}")
    return {"id": meeting_id}


@app.post("/api/meetings/stop")
async def stop_meeting():
    return {"id": await asyncio.to_thread(meetings.manager.stop)}


@app.post("/api/meetings/import")
async def import_recording(file: UploadFile = File(...), title: str = Form(""), language: str = Form("en")):
    name = _safe_name(file.filename or "recording.wav")
    if Path(name).suffix.lower() not in AUDIO_EXTS:
        raise HTTPException(400, f"unsupported audio type; use one of {', '.join(sorted(AUDIO_EXTS))}")
    if language not in meetings.LANGUAGES:
        raise HTTPException(400, f"unknown meeting language; use one of {', '.join(meetings.LANGUAGES)}")
    target = settings.recordings_dir / f"{int(time.time() * 1000)}_{name}"
    vault.write_bytes(target, await file.read())  # encrypted at rest; transcribed from memory
    return {"id": meetings.manager.import_recording(target, title or Path(name).stem, language)}


@app.post("/api/meetings/{meeting_id}/transcript-pass")
def retry_transcript_pass(meeting_id: int):
    """Run Gemma's pass over a Hindi / Hinglish meeting again after it failed (it resumes where it stopped)."""
    if not repass.status(meeting_id):
        raise HTTPException(404, "this meeting has no transcript pass")
    repass.retry(meeting_id)
    return {"ok": True}


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: int):
    m = meetings.get(meeting_id)
    if not m:
        raise HTTPException(404)
    return m


@app.put("/api/meetings/{meeting_id}")
def patch_meeting(meeting_id: int, body: MeetingPatch):
    meetings.update(meeting_id, title=body.title, shorthand=body.shorthand)
    return {"ok": True}


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: int):
    meetings.delete(meeting_id)
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/enhance")
def enhance(meeting_id: int):
    if not db.query_one("SELECT id FROM meetings WHERE id = ?", (meeting_id,)):
        raise HTTPException(404)
    return _ndjson(meetings.enhance_stream(meeting_id))


@app.get("/api/meetings/{meeting_id}/export")
def export(meeting_id: int):
    try:
        text = meetings.export_markdown(meeting_id)
    except KeyError:
        raise HTTPException(404)
    filename = re.sub(r"[^\w\- ]+", "_", text.splitlines()[0].lstrip("# "))[:60] or "meeting"
    return PlainTextResponse(text, media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{filename}.md"'})


# ---------------------------------------------------------------- stored audio

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def _send_file(path: str, mime: str, request: Request):
    """A stored file, with byte ranges (so the player can seek). Encrypted files are decrypted as they stream."""
    if not vault.is_encrypted(path):
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-store"}, content_disposition_type="inline")
    reader = vault.Reader(path)
    size, start, end, status = reader.size, 0, reader.size - 1, 200
    match = _RANGE.match(request.headers.get("range", "").strip())
    if match and (match.group(1) or match.group(2)):
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:  # "bytes=-500": the last 500 bytes
            start = max(0, size - int(match.group(2)))
        if start > end or start >= size:
            reader.close()
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206

    def body():
        try:
            reader.seek(start)
            left = end - start + 1
            while left > 0:
                block = reader.read(min(256 * 1024, left))
                if not block:
                    break
                left -= len(block)
                yield block
        finally:
            reader.close()

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store",
               "Content-Disposition": "inline"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=status, media_type=mime, headers=headers)


@app.get("/api/media/{media_id}")
def get_media(media_id: int, request: Request):
    """Stream a stored recording (Range requests supported, so the player can seek), or show an indexed picture."""
    row = media.playable(media_id)
    if not row:
        raise HTTPException(404, "not available (not kept, expired or deleted)")
    return _send_file(row["path"], row["mime"] or "application/octet-stream", request)


@app.get("/api/media/{media_id}/thumb")
def get_thumbnail(media_id: int):
    path = images.thumb_path(media_id)
    if not path.is_file():
        raise HTTPException(404, "no thumbnail")
    return Response(vault.read_bytes(path), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.delete("/api/media")
def delete_media(kind: str = "recording"):
    if kind != "recording":
        raise HTTPException(400, "only stored meeting audio can be deleted here")
    return {"deleted": media.delete_all_recordings()}


@app.post("/api/media/check")
def check_media():
    return llm.check_media()


# ---------------------------------------------------------------- calendars & daily briefings

class CalendarIn(BaseModel):
    uri: str


@app.get("/api/calendars")
def list_calendars():
    return calendars.list_calendars()


@app.post("/api/calendars")
def add_calendar(body: CalendarIn):
    try:
        return calendars.add_calendar(body.uri)
    except calendars.CalendarError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/calendars/{calendar_id}/sync")
def sync_calendar(calendar_id: int):
    try:
        return calendars.sync(calendar_id)
    except KeyError:
        raise HTTPException(404)


@app.delete("/api/calendars/{calendar_id}")
def delete_calendar(calendar_id: int):
    calendars.remove_calendar(calendar_id)
    return {"ok": True}


@app.get("/api/briefings")
def list_briefings():
    """Events in the next 24 h with their briefings; missing ones are written in the background (WS 'briefing')."""
    n_calendars = db.query_one("SELECT COUNT(*) AS n FROM calendars")["n"]
    return {"calendars": n_calendars, "events": briefings.upcoming()}


@app.post("/api/briefings/{event_id}/refresh")
def refresh_briefing(event_id: int):
    if not db.query_one("SELECT id FROM events WHERE id = ?", (event_id,)):
        raise HTTPException(404)
    briefings.refresh(event_id)
    return {"ok": True}


# ---------------------------------------------------------------- live events

@app.websocket("/ws")
async def ws(socket: WebSocket):
    origin = socket.headers.get("origin", "")
    if origin and origin.split("//", 1)[-1] not in _ALLOWED_HOSTS:
        await socket.close(code=4403)
        return
    await socket.accept()
    q = events.subscribe()
    try:
        while True:
            await socket.send_text(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        events.unsubscribe(q)


# ---------------------------------------------------------------- UI

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(llm.LLMError)
async def llm_error(_: Request, exc: llm.LLMError):
    return JSONResponse({"detail": str(exc)}, status_code=503)
