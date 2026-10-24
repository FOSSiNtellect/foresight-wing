"""Hybrid retrieval: EmbeddingGemma cosine similarity + SQLite FTS5 BM25, fused with RRF.

Media join the fusion as two more ranked lists, in the same space as text: audio moments (EmbeddingGemma 2 vectors of
stored meeting audio; see media_index.py), weighted by settings.media_weight, and pictures (images.py), weighted by
settings.image_weight but only above settings.image_min_similarity, so the closest picture does not join every
answer. Lists are fused by rank, not raw score, because text-to-audio and text-to-image cosines sit on a different
scale than text-to-text ones.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

import numpy as np

from . import db, llm
from .config import settings

log = logging.getLogger(__name__)

RRF_K = 60
LAST_LINE_S = 15.0  # a transcript line lasts at most this long (the VAD's utterance limit)
_TIMESTAMP = re.compile(r"^\[(\d+):(\d{2})\]", re.M)

_cache_lock = threading.Lock()
_cache: dict | None = None        # text chunks: {"ids", "sources", "matrix"}
_media_cache: dict | None = None  # audio moments and pictures: {"ids", "sources", "meetings", "pictures", "matrix"}


@dataclass
class Hit:
    chunk_id: int       # 0 for an audio moment
    source_id: int
    title: str
    uri: str
    kind: str
    text: str
    score: float        # fused RRF score (ranking)
    similarity: float   # cosine similarity in [-1, 1] (thresholding)
    media: dict | None = None  # audio {"media_id", "channel", "t_start", "t_end", "url"}: the moment can be played;
                               # picture {"media_id", "kind": "image", "url", "thumb"}

    def as_citation(self, n: int) -> dict:
        citation = {"n": n, "source_id": self.source_id, "title": self.title, "uri": self.uri,
                    "kind": self.kind, "snippet": self.text[:400], "similarity": round(self.similarity, 3)}
        if self.media:
            citation["media"] = self.media
        return citation


def invalidate() -> None:
    global _cache, _media_cache
    with _cache_lock:
        _cache = None
        _media_cache = None


def _stack(rows: list[dict]) -> tuple[list[dict], np.ndarray]:
    """Rows of the most common dimension (vectors left over from another model are ignored) and their matrix."""
    if not rows:
        return rows, np.zeros((0, 0), dtype=np.float32)
    dim = max(set(r["dim"] for r in rows), key=lambda d: sum(1 for r in rows if r["dim"] == d))
    rows = [r for r in rows if r["dim"] == dim]
    return rows, np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32).reshape(len(rows), dim)


def _load_matrix() -> dict:
    global _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        rows, matrix = _stack(db.query(
            "SELECT c.id, c.source_id, c.embedding, c.dim FROM chunks c "
            "JOIN sources s ON s.id = c.source_id WHERE s.status = 'ready' AND c.embedding IS NOT NULL"))
        _cache = {
            "ids": np.array([r["id"] for r in rows], dtype=np.int64),
            "sources": np.array([r["source_id"] for r in rows], dtype=np.int64),
            "matrix": matrix,
        }
        return _cache


def _load_media() -> dict:
    """Audio moments and pictures. An audio moment counts as part of its meeting's transcript source and a picture as
    its own source, so label filters apply to them too."""
    global _media_cache
    with _cache_lock:
        if _media_cache is not None:
            return _media_cache
        rows, matrix = _stack(db.query(
            "SELECT i.id, i.embedding, i.dim, m.meeting_id, m.kind, COALESCE(m.source_id, "
            "(SELECT s.id FROM sources s WHERE s.meeting_id = m.meeting_id AND s.kind = 'transcript' LIMIT 1)) AS source_id "
            "FROM media_items i JOIN media m ON m.id = i.media_id WHERE i.model = ?", (settings.embed_model,)))
        _media_cache = {
            "ids": np.array([r["id"] for r in rows], dtype=np.int64),
            "sources": np.array([r["source_id"] or -1 for r in rows], dtype=np.int64),
            "meetings": np.array([r["meeting_id"] or -1 for r in rows], dtype=np.int64),
            "pictures": np.array([r["kind"] == "image" for r in rows], dtype=bool),
            "matrix": matrix,
        }
        return _media_cache


def _label_source_ids(label: str | None) -> set[int] | None:
    if not label:
        return None
    rows = db.query(
        "SELECT sl.source_id FROM source_labels sl JOIN labels l ON l.id = sl.label_id WHERE l.name = ?", (label,))
    return {r["source_id"] for r in rows}


def _fts_query(text: str) -> str:
    words = [w for w in re.findall(r"[\w']+", text.lower()) if len(w) > 2][:24]
    return " OR ".join(f'"{w}"' for w in words)


def _ranked(scores: np.ndarray, mask: np.ndarray, ids: np.ndarray, pool: int) -> tuple[dict[int, int], dict[int, float]]:
    scores = np.where(mask, scores, -np.inf)
    ranks: dict[int, int] = {}
    sims: dict[int, float] = {}
    for rank, idx in enumerate(np.argsort(-scores)[:pool]):
        if not np.isfinite(scores[idx]):
            break
        ranks[int(ids[idx])] = rank
        sims[int(ids[idx])] = float(scores[idx])
    return ranks, sims


def _fmt(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def _chunk_span(text: str) -> tuple[float, float] | None:
    """Time range of a transcript chunk, from its "[mm:ss] Speaker: text" lines."""
    times = [int(m) * 60 + int(s) for m, s in _TIMESTAMP.findall(text)]
    return (min(times), max(times) + LAST_LINE_S) if times else None


def _merge_moments(hits: list[Hit]) -> list[Hit]:
    """One card per moment: an audio moment inside a transcript chunk that is also a result becomes that chunk's
    play button, and a picture found by its look joins its description's result as a thumbnail. The pair takes the
    better of the two positions."""
    out: list[Hit] = []
    taken: set[int] = set()
    for i, h in enumerate(hits):
        if i in taken:
            continue
        taken.add(i)
        if h.kind not in ("audio", "image") or not h.source_id:
            out.append(h)
            continue
        partner = None
        for j, t in enumerate(hits):
            if t.source_id != h.source_id or t.media is not None or t.kind in ("audio", "image"):
                continue
            if h.kind == "image":
                partner = j
                break
            span = _chunk_span(t.text)  # a meeting transcript or a voice memo
            if span and h.media["t_start"] < span[1] and h.media["t_end"] > span[0]:
                partner = j
                break
        if partner is None:
            out.append(h)
            continue
        t = hits[partner]
        t.media, t.score = h.media, t.score + h.score
        if partner not in taken:
            taken.add(partner)
            out.append(t)
    return out


def search(query: str, k: int = 6, *, label: str | None = None, exclude_meeting: int | None = None,
           query_vec: np.ndarray | None = None, with_media: bool = True,
           meeting_ids: set[int] | None = None) -> list[Hit]:
    """meeting_ids: only those meetings' transcripts and notes (and their audio moments), e.g. "last Tuesday's meeting"."""
    query = query.strip()
    if not query:
        return []
    allowed = _label_source_ids(label)
    if meeting_ids is not None:
        ids = tuple(meeting_ids)
        in_meetings = {r["id"] for r in db.query(
            f"SELECT id FROM sources WHERE meeting_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
        allowed = in_meetings if allowed is None else allowed & in_meetings
    if allowed is not None and not allowed:
        return []
    excluded: set[int] = set()
    if exclude_meeting is not None:
        excluded = {r["id"] for r in db.query("SELECT id FROM sources WHERE meeting_id = ?", (exclude_meeting,))}

    cache = _load_matrix()
    moments = _load_media() if with_media else None
    pool = max(k * 4, 24)
    qv = query_vec

    def vec() -> np.ndarray:
        nonlocal qv
        if qv is None:
            qv = llm.embed([query], query=True)[0]
        return qv

    vec_rank: dict[int, int] = {}
    sims: dict[int, float] = {}
    if cache["matrix"].size and vec().shape[0] == cache["matrix"].shape[1]:
        mask = np.ones(len(cache["ids"]), dtype=bool)
        if allowed is not None:
            mask &= np.isin(cache["sources"], list(allowed))
        if excluded:
            mask &= ~np.isin(cache["sources"], list(excluded))
        vec_rank, sims = _ranked(cache["matrix"] @ vec(), mask, cache["ids"], pool)

    media_rank: dict[int, int] = {}
    picture_rank: dict[int, int] = {}
    media_sims: dict[int, float] = {}
    if moments is not None and moments["matrix"].size and vec().shape[0] == moments["matrix"].shape[1]:
        mask = np.ones(len(moments["ids"]), dtype=bool)
        if allowed is not None:
            mask &= np.isin(moments["sources"], list(allowed))
        if exclude_meeting is not None:
            mask &= moments["meetings"] != exclude_meeting
        scores = moments["matrix"] @ vec()
        media_rank, media_sims = _ranked(scores, mask & ~moments["pictures"], moments["ids"], pool)
        picture_rank, picture_sims = _ranked(
            scores, mask & moments["pictures"] & (scores >= settings.image_min_similarity), moments["ids"], pool)
        media_sims.update(picture_sims)

    fts_rank: dict[int, int] = {}
    fts = _fts_query(query)
    if fts:
        try:
            rows = db.query(
                "SELECT c.id, c.source_id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                "JOIN sources s ON s.id = c.source_id WHERE chunks_fts MATCH ? AND s.status = 'ready' "
                "ORDER BY bm25(chunks_fts) LIMIT ?", (fts, pool))
        except Exception:
            rows = []
        rank = 0
        for r in rows:
            if (allowed is not None and r["source_id"] not in allowed) or r["source_id"] in excluded:
                continue
            fts_rank[r["id"]] = rank
            rank += 1

    fused: dict[tuple[str, int], float] = {}
    for ranks, kind, weight in ((vec_rank, "c", 1.0), (fts_rank, "c", 1.0), (media_rank, "m", settings.media_weight),
                                (picture_rank, "m", settings.image_weight)):
        for item, rank in ranks.items():
            fused[(kind, item)] = fused.get((kind, item), 0.0) + weight / (RRF_K + rank)
    best = sorted(fused, key=fused.get, reverse=True)[: k * 3]
    if not best:
        return []

    chunk_ids = [i for kind, i in best if kind == "c"]
    item_ids = [i for kind, i in best if kind == "m"]
    chunks = {r["id"]: r for r in db.query(
        f"SELECT c.id, c.source_id, c.text, s.title, s.uri, s.kind FROM chunks c "
        f"JOIN sources s ON s.id = c.source_id WHERE c.id IN ({','.join('?' * len(chunk_ids))})", tuple(chunk_ids))} \
        if chunk_ids else {}
    items = {r["id"]: r for r in db.query(
        f"SELECT i.id, i.media_id, i.t_start, i.t_end, i.text, m.kind AS media_kind, m.channel, m.status, m.meeting_id, "
        f"mt.title AS meeting_title, src.title AS source_title, src.uri AS source_uri, COALESCE(m.source_id, "
        f"(SELECT s.id FROM sources s WHERE s.meeting_id = m.meeting_id AND s.kind = 'transcript' LIMIT 1)) AS source_id "
        f"FROM media_items i JOIN media m ON m.id = i.media_id LEFT JOIN meetings mt ON mt.id = m.meeting_id "
        f"LEFT JOIN sources src ON src.id = m.source_id "
        f"WHERE i.id IN ({','.join('?' * len(item_ids))})", tuple(item_ids))} if item_ids else {}

    candidates: list[Hit] = []
    for key in best:
        kind, item = key
        if kind == "c" and item in chunks:
            r = chunks[item]
            candidates.append(Hit(item, r["source_id"], r["title"], r["uri"], r["kind"], r["text"], fused[key], sims.get(item, 0.0)))
        elif kind == "m" and item in items and items[item]["media_kind"] == "image":
            r = items[item]
            media = {"media_id": r["media_id"], "kind": "image", "url": f"/api/media/{r['media_id']}",
                     "thumb": f"/api/media/{r['media_id']}/thumb"}
            candidates.append(Hit(0, r["source_id"] or 0, r["source_title"] or "Picture", r["source_uri"] or "", "image",
                                  r["text"] or "(picture)", fused[key], media_sims.get(item, 0.0), media))
        elif kind == "m" and item in items:
            r = items[item]
            media = {"media_id": r["media_id"], "channel": r["channel"], "t_start": r["t_start"], "t_end": r["t_end"],
                     "url": f"/api/media/{r['media_id']}" if r["status"] == "ready" else None}
            name = r["meeting_title"] or r["source_title"] or "Recording"
            where = f"meeting:{r['meeting_id']}" if r["meeting_id"] else (r["source_uri"] or "")
            candidates.append(Hit(0, r["source_id"] or 0, f"{name} — audio {_fmt(r['t_start'])}",
                                  f"{where}#t={r['t_start']:.1f}", "audio",
                                  r["text"] or "(audio without a transcript)", fused[key], media_sims.get(item, 0.0), media))

    hits: list[Hit] = []
    per_source: dict = {}
    for h in _merge_moments(candidates):
        # Keep results diverse: at most 3 results from one source (an audio moment counts toward its meeting).
        source = h.source_id or ("media", h.media["media_id"] if h.media else 0)
        if per_source.get(source, 0) >= 3:
            continue
        per_source[source] = per_source.get(source, 0) + 1
        hits.append(h)
        if len(hits) >= k:
            break
    _attach_media(hits)
    return hits


def _attach_media(hits: list[Hit]) -> None:
    """A result from a picture's description shows the picture, and a passage of a voice memo plays from its first
    line, even when the picture's look or the memo's sound was not itself a match."""
    plain = {h.source_id for h in hits if h.media is None and h.source_id}
    if not plain:
        return
    ids = tuple(plain)
    files = {(r["source_id"], r["kind"]): r["id"] for r in db.query(
        f"SELECT id, source_id, kind FROM media WHERE kind IN ('image', 'audio') AND status = 'ready' "
        f"AND source_id IN ({','.join('?' * len(ids))})", ids)}
    for h in hits:
        if h.media is not None:
            continue
        if (h.source_id, "image") in files:
            m = files[(h.source_id, "image")]
            h.media = {"media_id": m, "kind": "image", "url": f"/api/media/{m}", "thumb": f"/api/media/{m}/thumb"}
        elif (h.source_id, "audio") in files and (span := _chunk_span(h.text)):
            m = files[(h.source_id, "audio")]
            h.media = {"media_id": m, "channel": "Memo", "t_start": float(span[0]), "t_end": float(span[1]),
                       "url": f"/api/media/{m}"}


RERANK_PROMPT = """You rank search results for a question. Score every numbered passage for how well it helps answer it:
3 = answers the question directly, 2 = closely related, 1 = loosely related, 0 = unrelated.
Return JSON: {"scores": [{"id": <passage number>, "score": <0-3>}, ...]} with one entry per passage."""
_RERANK_SCHEMA = {"type": "object", "required": ["scores"], "properties": {"scores": {"type": "array", "items": {
    "type": "object", "required": ["id", "score"],
    "properties": {"id": {"type": "integer"}, "score": {"type": "integer", "minimum": 0, "maximum": 3}}}}}}


def rerank(question: str, hits: list[Hit], k: int) -> list[Hit]:
    """Re-order fused search results with Gemma reading each passage against the question (one call), keep the best k.

    Passages Gemma scores 0 are dropped when enough relevant ones remain. On any failure the fused order stands."""
    if len(hits) <= 1:
        return hits[:k]
    blocks = "\n\n".join(f"[{n}] {h.title}\n{h.text.strip()[:500]}" for n, h in enumerate(hits, 1))
    try:
        out = llm.chat_json([{"role": "system", "content": RERANK_PROMPT},
                             {"role": "user", "content": f"Question: {question}\n\nPassages:\n{blocks}"}],
                            task="rerank", schema=_RERANK_SCHEMA, options={"num_predict": 400})
        scores = {int(s["id"]): int(s["score"]) for s in out.get("scores", []) if isinstance(s, dict) and "id" in s}
    except Exception:
        log.warning("rerank failed; keeping the search order", exc_info=True)
        return hits[:k]
    if not scores:
        return hits[:k]
    order = sorted(range(len(hits)), key=lambda i: (-scores.get(i + 1, 1), i))  # unscored passages count as 1
    relevant = [hits[i] for i in order if scores.get(i + 1, 1) > 0]
    return (relevant if len(relevant) >= min(3, k) else [hits[i] for i in order])[:k]


def format_context(hits: list[Hit], max_chars: int = 9000) -> str:
    blocks, used = [], 0
    for n, h in enumerate(hits, 1):
        block = f"[{n}] {h.title}\n{h.text.strip()}"
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)]
        if not block:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n---\n\n".join(blocks)
