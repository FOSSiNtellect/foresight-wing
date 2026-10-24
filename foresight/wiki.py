"""Second-brain wiki pages: synthesize everything under one semantic label and flag contradictions."""
from __future__ import annotations

from typing import Iterator

from . import db, llm

SOURCE_BUDGET_CHARS = 6000
MAX_SOURCES = 12

SUMMARY_PROMPT = """Summarize this document as dense factual bullets (max 12).
Preserve every figure, amount, date, deadline, owner and decision exactly as written. No commentary."""

WIKI_PROMPT = """You maintain a private wiki for the user. Using the numbered source summaries, write a wiki page in Markdown:
# {label}
## Overview
## Key facts & figures
## Timeline & decisions
## Conflicts & inconsistencies
Compare the sources carefully. List every place where they disagree (different numbers, dates, owners, scope or decisions), citing both sides like [1] vs [3]. If they agree, write "No conflicts found."
## Open questions
Cite sources with [n] throughout. Do not invent information."""


def _source_text(source_id: int) -> str:
    chunks = db.query("SELECT text FROM chunks WHERE source_id = ? ORDER BY ord", (source_id,))
    if not chunks:
        return ""
    total = sum(len(c["text"]) for c in chunks)
    if total <= SOURCE_BUDGET_CHARS:
        return "\n\n".join(c["text"] for c in chunks)
    # Sample evenly across a long document instead of only reading its start.
    step = max(1, len(chunks) * 1200 // SOURCE_BUDGET_CHARS)
    picked, used = [], 0
    for c in chunks[::step]:
        if used + len(c["text"]) > SOURCE_BUDGET_CHARS:
            break
        picked.append(c["text"])
        used += len(c["text"])
    return "\n\n[...]\n\n".join(picked)


def _summary(source: dict) -> str:
    if source["summary"]:
        return source["summary"]
    text = _source_text(source["id"])
    summary = llm.chat([{"role": "system", "content": SUMMARY_PROMPT},
                        {"role": "user", "content": f"Title: {source['title']}\n\n{text}"}],
                       task="wiki_map", options={"num_predict": 500}).strip()
    db.execute("UPDATE sources SET summary = ? WHERE id = ?", (summary, source["id"]))
    return summary


def wiki_stream(label: str) -> Iterator[dict]:
    sources = db.query(
        "SELECT s.* FROM sources s JOIN source_labels sl ON sl.source_id = s.id JOIN labels l ON l.id = sl.label_id "
        "WHERE l.name = ? AND s.status = 'ready' ORDER BY s.added_at LIMIT ?", (label, MAX_SOURCES))
    if not sources:
        yield {"type": "delta", "text": f"No indexed sources carry the label **{label}** yet."}
        yield {"type": "done", "citations": []}
        return
    citations, blocks = [], []
    for n, src in enumerate(sources, 1):
        yield {"type": "progress", "step": f"Reading {src['title']} ({n}/{len(sources)})"}
        summary = _summary(src)
        blocks.append(f"[{n}] {src['title']}\n{summary}")
        citations.append({"n": n, "source_id": src["id"], "title": src["title"], "uri": src["uri"],
                          "kind": src["kind"], "snippet": summary[:400]})
    yield {"type": "start", "citations": citations}
    yield {"type": "progress", "step": "Writing wiki page"}
    for delta in llm.chat_stream([
        {"role": "system", "content": WIKI_PROMPT.format(label=label)},
        {"role": "user", "content": "\n\n---\n\n".join(blocks)},
    ], task="wiki", options={"num_predict": 1500}):
        yield {"type": "delta", "text": delta}
    yield {"type": "done", "citations": citations}
