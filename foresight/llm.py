"""Local model access through Ollama: Gemma 4 for generation, EmbeddingGemma 2 for vectors."""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Iterator

import httpx
import numpy as np

from . import db
from .config import settings

log = logging.getLogger(__name__)

# EmbeddingGemma is trained with task prefixes; queries and documents use different ones.
QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: {title} | text: "

_client = httpx.Client(base_url=settings.ollama_url, timeout=httpx.Timeout(600.0, connect=5.0))
# One generation at a time keeps a 4 GB GPU / 16 GB RAM machine responsive.
_gen_lock = threading.Lock()
_think_supported: bool | None = None


class LLMError(RuntimeError):
    pass


def status() -> dict:
    try:
        version = _client.get("/api/version", timeout=3).json().get("version")
        names = {m["name"] for m in _client.get("/api/tags", timeout=5).json().get("models", [])}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": f"Ollama not reachable at {settings.ollama_url}: {exc}"}
    return {
        "ok": True,
        "version": version,
        "gen_model": settings.gen_model,
        "gen_ready": settings.gen_model in names,
        "embed_model": settings.embed_model,
        "embed_ready": settings.embed_model in names,
    }


def embed(texts: list[str], *, query: bool = False, title: str = "none", batch: int = 16) -> np.ndarray:
    """Return L2-normalised float32 embeddings, shape (len(texts), dim)."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    prefix = QUERY_PREFIX if query else DOC_PREFIX.format(title=(title or "none").replace("|", " ")[:120])
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = [prefix + t for t in texts[i:i + batch]]
        resp = _client.post("/api/embed", json={
            "model": settings.embed_model, "input": part, "keep_alive": settings.keep_alive, "truncate": True,
        })
        if resp.status_code != 200:
            raise LLMError(f"embedding failed ({resp.status_code}): {resp.text[:300]}")
        out.extend(resp.json()["embeddings"])
    return _unit_rows(out)


def _unit_rows(rows: list[list[float]]) -> np.ndarray:
    arr = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


# ---------------------------------------------------------------- audio (EmbeddingGemma 2 shares one space for text and audio)

AUDIO_RATE = 16000  # EmbeddingGemma 2 and Gemma 4 both expect 16 kHz mono


def wav_bytes(pcm: np.ndarray, rate: int = AUDIO_RATE) -> bytes:
    """Mono float32 samples in [-1, 1] → 16-bit PCM WAV file bytes (what Ollama accepts for audio)."""
    data = (np.clip(np.asarray(pcm, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2").tobytes()
    header = b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
    header += (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + rate.to_bytes(4, "little")
    header += (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    return header + b"data" + len(data).to_bytes(4, "little") + data


def embed_audio(wavs: list[bytes], *, batch: int = 4) -> np.ndarray:
    """L2-normalised EmbeddingGemma 2 vectors for WAV clips (16 kHz mono, up to ~5 minutes each).

    Ollama takes audio as {"audio": <base64 WAV>} inputs. Do not use the `images` field here: /api/embed
    ignores it silently and returns the vector of an empty string."""
    out: list[list[float]] = []
    for i in range(0, len(wavs), batch):
        part = [{"audio": base64.b64encode(w).decode("ascii")} for w in wavs[i:i + batch]]
        resp = _client.post("/api/embed", json={"model": settings.embed_model, "input": part,
                                                "keep_alive": settings.keep_alive})
        if resp.status_code != 200:
            raise LLMError(f"audio embedding failed ({resp.status_code}): {resp.text[:300]}")
        out.extend(resp.json()["embeddings"])
    return _unit_rows(out)


def embed_images(images: list[bytes], *, batch: int = 4) -> np.ndarray:
    """L2-normalised EmbeddingGemma 2 vectors for pictures (PNG or JPEG bytes), in the same space as text."""
    out: list[list[float]] = []
    for i in range(0, len(images), batch):
        part = [{"image": base64.b64encode(b).decode("ascii")} for b in images[i:i + batch]]
        resp = _client.post("/api/embed", json={"model": settings.embed_model, "input": part,
                                                "keep_alive": settings.keep_alive})
        if resp.status_code != 200:
            raise LLMError(f"image embedding failed ({resp.status_code}): {resp.text[:300]}")
        out.extend(resp.json()["embeddings"])
    return _unit_rows(out)


def _test_pictures() -> list[bytes]:
    """Two clearly different small PNGs: a red square and black stripes on white."""
    import io

    from PIL import Image, ImageDraw

    red = Image.new("RGB", (64, 64), (220, 30, 30))
    stripes = Image.new("RGB", (64, 64), "white")
    draw = ImageDraw.Draw(stripes)
    for x in range(0, 64, 8):
        draw.rectangle([x, 0, x + 3, 63], fill="black")
    out = []
    for im in (red, stripes):
        buf = io.BytesIO()
        im.save(buf, "PNG")
        out.append(buf.getvalue())
    return out


_media: dict = {"checked": False}
_media_lock = threading.Lock()


def media_status() -> dict:
    return dict(_media)


def check_media() -> dict:
    """What the installed models can do with audio and images, verified with real audio and image embeddings.

    A request shape Ollama does not understand can fail silently (it embeds the empty string instead),
    so two clearly different synthetic clips (and pictures) must come back as different vectors."""
    with _media_lock:
        result = {"checked": True, "checked_at": time.time(), "audio_embed": False, "image_embed": False,
                  "gen_audio": False, "gen_vision": False, "error": None}
        try:
            embed_caps = set(_client.post("/api/show", json={"model": settings.embed_model}, timeout=10).json()
                             .get("capabilities") or [])
            gen_caps = set(_client.post("/api/show", json={"model": settings.gen_model}, timeout=10).json()
                           .get("capabilities") or [])
            result["gen_audio"], result["gen_vision"] = "audio" in gen_caps, "vision" in gen_caps
            if "audio" in embed_caps:
                t = np.arange(AUDIO_RATE) / AUDIO_RATE
                tone = 0.3 * np.sin(2 * np.pi * 440 * t)
                noise = np.random.default_rng(0).normal(0, 0.1, AUDIO_RATE)
                a, b = embed_audio([wav_bytes(tone), wav_bytes(noise)])
                result["audio_embed"] = bool(float(a @ b) < 0.995)
                if not result["audio_embed"]:
                    result["error"] = "different audio clips came back identical; this Ollama may not accept audio input"
            else:
                result["error"] = f"{settings.embed_model} does not support audio"
            if "vision" in embed_caps:  # pictures (images.py); a failure here leaves the audio result standing
                try:
                    a, b = embed_images(_test_pictures())
                    result["image_embed"] = bool(float(a @ b) < 0.995)
                except (httpx.HTTPError, LLMError, ValueError, KeyError) as exc:
                    result["error"] = result["error"] or f"image embedding: {str(exc)[:250]}"
        except (httpx.HTTPError, LLMError, ValueError, KeyError) as exc:
            result["error"] = str(exc)[:300]
        _media.clear()
        _media.update(result)
        return dict(result)


STYLE_RULE = "Write amounts as plain text (e.g. $52,000). Never use LaTeX or math notation."


def _chat_payload(messages: list[dict], stream: bool, options: dict | None, fmt: str | dict | None) -> dict:
    if fmt is None and messages and messages[0]["role"] == "system" and STYLE_RULE not in messages[0]["content"]:
        messages = [{**messages[0], "content": messages[0]["content"] + "\n" + STYLE_RULE}, *messages[1:]]
    payload: dict = {
        "model": settings.gen_model,
        "messages": messages,
        "stream": stream,
        "keep_alive": settings.keep_alive,
        "options": {**settings.gen_options, **(options or {})},
    }
    if _think_supported is not False:
        payload["think"] = False  # thinking off: notes and answers do not need it, and it is faster
    if fmt is not None:
        payload["format"] = fmt
    return payload


def chat_stream(messages: list[dict], *, task: str = "chat", options: dict | None = None,
                fmt: str | dict | None = None) -> Iterator[str]:
    """Yield text deltas from Gemma. Records token usage when the stream ends."""
    global _think_supported
    with _gen_lock:
        for attempt in range(2):
            payload = _chat_payload(messages, True, options, fmt)
            with _client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "replace")
                    if "think" in body.lower() and attempt == 0 and "think" in payload:
                        _think_supported = False
                        continue
                    raise LLMError(f"generation failed ({resp.status_code}): {body[:300]}")
                if "think" in payload:
                    _think_supported = True
                for line in resp.iter_lines():
                    if not line:
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise LLMError(msg["error"])
                    delta = (msg.get("message") or {}).get("content") or ""
                    if delta:
                        yield delta
                    if msg.get("done"):
                        try:
                            db.record_usage(task, msg.get("prompt_eval_count", 0), msg.get("eval_count", 0))
                        except Exception:  # usage stats must never break generation
                            log.exception("usage logging failed")
                return


def chat(messages: list[dict], *, task: str = "chat", options: dict | None = None,
         fmt: str | dict | None = None) -> str:
    return "".join(chat_stream(messages, task=task, options=options, fmt=fmt))


def chat_json(messages: list[dict], *, task: str, schema: dict | None = None, options: dict | None = None) -> dict:
    text = chat(messages, task=task, options={"temperature": 0.1, **(options or {})}, fmt=schema or "json")
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
        log.warning("model returned non-JSON output for %s: %r", task, text[:200])
        return {}
