"""Pictures in the knowledge hub: photos, screenshots, charts, scanned receipts.

Gemma 4 describes each image and copies any text in it, so words find it like any other source. EmbeddingGemma 2
also embeds the picture itself, in the same space as text, so a description of what it shows finds it even when
Gemma's words missed it (the vector joins search as a media item, like an audio moment). A small thumbnail shows it
in citations, and Gemma looks at cited images when it answers (rag.py).
"""
from __future__ import annotations

import base64
import io
import logging
import os
from pathlib import Path

from PIL import Image, ImageOps

from . import db, llm, vault
from .config import settings

log = logging.getLogger(__name__)

THUMB_DIR = settings.data_dir / "thumbs"
THUMB_PX = 320     # longest side of a citation thumbnail
MODEL_PX = 1024    # longest side sent to Gemma and EmbeddingGemma (larger photos only cost time)
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
        ".bmp": "image/bmp"}

DESCRIBE_PROMPT = ("Describe this image so it can be found by search later. First say what it is and what it shows "
                   "(objects, people, places, charts with their values, diagrams). Then copy any readable text exactly. "
                   "Plain sentences, no preamble, at most 150 words.")


def _rgb(path: str | Path, longest: int) -> Image.Image:
    """The image upright (camera EXIF rotation), flattened onto white, no larger than `longest` pixels."""
    with vault.open_read(path) as f, Image.open(f) as im:
        im.seek(0)  # first frame of an animation
        im = ImageOps.exif_transpose(im)
        im.thumbnail((longest, longest))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, "white")
            flat.paste(im, mask=im.getchannel("A"))
            return flat
        return im.convert("RGB")


def model_bytes(path: str | Path) -> bytes:
    """JPEG of the image at model size."""
    out = io.BytesIO()
    _rgb(path, MODEL_PX).save(out, "JPEG", quality=90)
    return out.getvalue()


def describe(path: str | Path) -> str:
    """Gemma 4's description of the picture, with any text in it copied out."""
    b64 = base64.b64encode(model_bytes(path)).decode("ascii")
    text = llm.chat([{"role": "user", "content": DESCRIBE_PROMPT, "images": [b64]}], task="describe_image",
                    options={"temperature": 0.2, "num_predict": 400})
    return text.strip()


def thumb_path(media_id: int) -> Path:
    return THUMB_DIR / f"{media_id}.jpg"


def _make_thumbnail(path: str | Path, media_id: int) -> None:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = io.BytesIO()
    _rgb(path, THUMB_PX).save(out, "JPEG", quality=82)
    vault.write_bytes(thumb_path(media_id), out.getvalue())


def attach(source_id: int, path: str | Path, description: str) -> int:
    """Link an indexed image source to its picture: media row, thumbnail and (when the model can) image vector."""
    p = Path(path)
    row = db.query_one("SELECT id FROM media WHERE source_id = ? AND kind = 'image'", (source_id,))
    values = (str(p), MIME.get(p.suffix.lower(), "application/octet-stream"), p.stat().st_size)
    if row:
        media_id = row["id"]
        db.execute("UPDATE media SET path = ?, mime = ?, bytes = ?, status = 'ready', error = NULL WHERE id = ?",
                   (*values, media_id))
    else:  # owned = 0: the picture is the user's own file (or their upload); Foresight Wing never deletes it
        media_id = db.execute("INSERT INTO media(source_id, kind, path, owned, mime, bytes, status, created_at) "
                              "VALUES (?, 'image', ?, 0, ?, ?, 'ready', ?)", (source_id, *values, db.now()))
    _make_thumbnail(p, media_id)
    try:
        vector = llm.embed_images([model_bytes(p)])[0]
    except Exception as exc:  # e.g. an older Ollama without image input: the description still finds it
        log.warning("image embedding of %s failed: %s", p.name, exc)
        db.execute("DELETE FROM media_items WHERE media_id = ?", (media_id,))
        return media_id
    with db.tx() as conn:
        conn.execute("DELETE FROM media_items WHERE media_id = ?", (media_id,))
        conn.execute("INSERT INTO media_items(media_id, t_start, t_end, text, embedding, dim, model) "
                     "VALUES (?, NULL, NULL, ?, ?, ?, ?)",
                     (media_id, description[:1000], vector.tobytes(), int(vector.shape[0]), settings.embed_model))
    return media_id


def for_source(source_id: int) -> dict | None:
    row = db.query_one("SELECT id FROM media WHERE source_id = ? AND kind = 'image' AND status = 'ready'", (source_id,))
    return {"media_id": row["id"], "kind": "image", "url": f"/api/media/{row['id']}",
            "thumb": f"/api/media/{row['id']}/thumb"} if row else None


def for_gemma(media_id: int) -> str | None:
    """Base64 JPEG of a stored picture for Gemma's `images` field, or None if the file is gone."""
    row = db.query_one("SELECT path FROM media WHERE id = ? AND kind = 'image' AND status = 'ready'", (media_id,))
    try:
        return base64.b64encode(model_bytes(row["path"])).decode("ascii") if row else None
    except OSError:
        return None


def sweep() -> int:
    """Delete thumbnails whose picture is no longer indexed (its source was removed)."""
    if not THUMB_DIR.is_dir():
        return 0
    known = {r["id"] for r in db.query("SELECT id FROM media WHERE kind = 'image'")}
    removed = 0
    for p in THUMB_DIR.iterdir():
        if not (p.suffix == ".jpg" and p.stem.isdigit() and int(p.stem) in known):  # incl. ".part" left by a crash
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    return removed
