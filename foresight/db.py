"""SQLite storage: knowledge sources, chunk vectors (float32 BLOBs) + FTS5, meetings, chat, usage."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    uri         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- file | url | upload | transcript | notes
    mime        TEXT,
    sha256      TEXT,
    mtime       REAL,
    folder_id   INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    meeting_id  INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued | indexing | ready | error
    error       TEXT,
    n_chunks    INTEGER NOT NULL DEFAULT 0,
    n_chars     INTEGER NOT NULL DEFAULT 0,
    summary     TEXT,
    added_at    REAL NOT NULL,
    indexed_at  REAL
);

CREATE TABLE IF NOT EXISTS folders (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,
    added_at  REAL NOT NULL,
    scanned_at REAL
);

CREATE TABLE IF NOT EXISTS labels (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS source_labels (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    label_id  INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (source_id, label_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    embedding  BLOB,
    dim        INTEGER
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS meetings (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    status       TEXT NOT NULL DEFAULT 'recording',  -- recording | done | importing
    shorthand    TEXT NOT NULL DEFAULT '',
    enhanced     TEXT,
    summary      TEXT,
    action_items TEXT,                               -- JSON list of strings
    language     TEXT NOT NULL DEFAULT 'en'          -- en | hi (Hindi / Hinglish): picks the speech engine
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    t_start     REAL NOT NULL,
    t_end       REAL NOT NULL,
    speaker     TEXT NOT NULL,                       -- You | Others | Recording
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_meeting ON segments(meeting_id, t_start);

CREATE TABLE IF NOT EXISTS insights (
    id          INTEGER PRIMARY KEY,
    meeting_id  INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    citations   TEXT NOT NULL,                       -- JSON
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY,
    conv_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    citations   TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_conv ON chat_messages(conv_id, id);

CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY,
    day               TEXT NOT NULL,
    task              TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_day ON usage(day);

CREATE TABLE IF NOT EXISTS calendars (
    id         INTEGER PRIMARY KEY,
    uri        TEXT NOT NULL UNIQUE,             -- local .ics path or http(s) subscription URL
    name       TEXT NOT NULL,
    added_at   REAL NOT NULL,
    synced_at  REAL,
    error      TEXT,
    n_events   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    uid         TEXT NOT NULL,                   -- iCalendar UID, plus the instance start for recurring events
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    attendees   TEXT NOT NULL DEFAULT '[]',      -- JSON list of names (or e-mail addresses)
    starts_at   REAL NOT NULL,
    ends_at     REAL NOT NULL,
    all_day     INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,                   -- changes whenever the event does
    UNIQUE (calendar_id, uid)
);
CREATE INDEX IF NOT EXISTS events_start ON events(starts_at);

CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    meeting_id  INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                   -- recording (meeting audio); audio | image in later phases
    channel     TEXT,                            -- You | Others | Recording
    path        TEXT NOT NULL,
    owned       INTEGER NOT NULL,                -- 1 = Foresight Wing created the file and may delete it
    mime        TEXT,
    duration_s  REAL,
    bytes       INTEGER,
    status      TEXT NOT NULL,                   -- recording | ready | expired
    error       TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL                             -- audio retention; NULL = keep
);
CREATE INDEX IF NOT EXISTS media_meeting ON media(meeting_id);

CREATE TABLE IF NOT EXISTS media_items (               -- searchable moments: EmbeddingGemma 2 vectors of the audio itself
    id         INTEGER PRIMARY KEY,
    media_id   INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    t_start    REAL,                             -- seconds into the file (NULL for images, later)
    t_end      REAL,
    text       TEXT NOT NULL DEFAULT '',         -- transcript of the window: Gemma's context and the hover snippet
    embedding  BLOB NOT NULL,                    -- float32, L2-normalised
    dim        INTEGER NOT NULL,
    model      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_items_media ON media_items(media_id, t_start);

CREATE TABLE IF NOT EXISTS media_index (               -- indexing progress per file, so work resumes after a restart
    media_id    INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    status      TEXT NOT NULL,                   -- indexing | done | error
    done_until  REAL NOT NULL DEFAULT 0,         -- windows ending at or before this second are stored
    error       TEXT,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_passes (         -- Gemma's second pass over a Hindi / Hinglish meeting
    meeting_id  INTEGER PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,                   -- queued | running | done | error
    done        INTEGER NOT NULL DEFAULT 0,      -- lines processed
    total       INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,      -- lines Gemma reworded
    temp_audio  INTEGER NOT NULL DEFAULT 0,      -- 1: audio kept only for this pass, deleted when it is done
    error       TEXT,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS segment_versions (          -- earlier wordings of transcript lines, so changes can be undone
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,                   -- the wording that was replaced
    replaced_by TEXT NOT NULL,                   -- gemma (transcript pass) | edit (the user)
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS segment_versions_seg ON segment_versions(segment_id);

CREATE TABLE IF NOT EXISTS briefings (
    event_id    INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,                   -- events.fingerprint the briefing was written for
    summary     TEXT NOT NULL,
    citations   TEXT NOT NULL,                   -- JSON: knowledge hits and past meetings
    created_at  REAL NOT NULL
);
"""

_write_lock = threading.RLock()
_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    global _initialized
    with _write_lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table first shipped: CREATE TABLE IF NOT EXISTS leaves existing tables alone."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(meetings)")}
    if "language" not in columns:
        conn.execute("ALTER TABLE meetings ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Serialized write transaction (SQLite allows one writer at a time anyway)."""
    if not _initialized:
        init()
    with _write_lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def query(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    if not _initialized:
        init()
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | dict = ()) -> int:
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def now() -> float:
    return time.time()


def record_usage(task: str, prompt_tokens: int, completion_tokens: int) -> None:
    execute(
        "INSERT INTO usage(day, task, prompt_tokens, completion_tokens) VALUES (?,?,?,?)",
        (time.strftime("%Y-%m-%d"), task, int(prompt_tokens or 0), int(completion_tokens or 0)),
    )


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default
