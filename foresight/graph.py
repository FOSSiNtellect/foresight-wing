"""Knowledge graph: sources as nodes, linked to their most similar sources and to their labels.

A source's position in meaning is the mean of its chunk vectors (EmbeddingGemma 2). Each source links to its few
nearest neighbours that are clearly more alike than usual for this collection, so the graph shows clusters of related
files, pages and meetings without becoming a hairball. Labels are hub nodes joined to their sources.
"""
from __future__ import annotations

import numpy as np

from . import db, retrieval

MIN_SIMILARITY = 0.5    # never link sources less alike than this
SPREAD = 0.5            # ...or than the collection's average plus this many standard deviations. Whole documents
                        # score 0.6-0.8 against almost anything, so only pairs well above the norm count as related.
NEIGHBOURS = 3          # links per source, at most (before de-duplication)


def build(*, limit: int = 300, label: str | None = None, min_similarity: float = MIN_SIMILARITY,
          neighbours: int = NEIGHBOURS) -> dict:
    """{"nodes", "edges", "threshold"} for the `limit` most recently added ready sources (optionally one label).
    `threshold` is the similarity a pair needed to be linked."""
    sql = ("SELECT s.id, s.title, s.kind, s.meeting_id, s.n_chunks FROM sources s WHERE s.status = 'ready'")
    params: tuple = ()
    if label:
        sql += (" AND s.id IN (SELECT sl.source_id FROM source_labels sl JOIN labels l ON l.id = sl.label_id "
                "WHERE l.name = ?)")
        params = (label,)
    sources = db.query(sql + " ORDER BY s.added_at DESC LIMIT ?", (*params, limit))
    if not sources:
        return {"nodes": [], "edges": [], "threshold": min_similarity}

    index = {s["id"]: i for i, s in enumerate(sources)}
    cache = retrieval._load_matrix()
    vectors = np.zeros((len(sources), cache["matrix"].shape[1] if cache["matrix"].size else 1), dtype=np.float32)
    if cache["matrix"].size:
        rows = np.flatnonzero(np.isin(cache["sources"], list(index)))
        positions = np.array([index[int(s)] for s in cache["sources"][rows]], dtype=np.int64)
        np.add.at(vectors, positions, cache["matrix"][rows])
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    has_vector = norms[:, 0] > 0
    vectors = vectors / np.where(norms > 0, norms, 1.0)

    edges: dict[tuple, dict] = {}
    threshold = min_similarity
    if has_vector.sum() > 1:
        sims = vectors @ vectors.T
        pairs = sims[np.ix_(has_vector, has_vector)][np.triu_indices(int(has_vector.sum()), 1)]
        threshold = max(min_similarity, float(pairs.mean() + SPREAD * pairs.std()))
        np.fill_diagonal(sims, -1.0)
        sims[~has_vector, :] = -1.0
        sims[:, ~has_vector] = -1.0
        for i in range(len(sources)):
            for j in np.argsort(-sims[i])[:neighbours]:
                if sims[i, j] < threshold:
                    break
                a, b = sorted((sources[i]["id"], sources[int(j)]["id"]))
                edges[(a, b)] = {"a": a, "b": b, "kind": "similar", "weight": round(float(sims[i, j]), 3)}

    ids = tuple(index)
    pairs = db.query(f"SELECT sl.source_id, l.name FROM source_labels sl JOIN labels l ON l.id = sl.label_id "
                     f"WHERE sl.source_id IN ({','.join('?' * len(ids))})", ids)
    labels_of: dict[int, list[str]] = {}
    for p in pairs:
        labels_of.setdefault(p["source_id"], []).append(p["name"])
        key = (f"label:{p['name']}", p["source_id"])
        edges[key] = {"a": f"label:{p['name']}", "b": p["source_id"], "kind": "label", "weight": 1.0}

    nodes = [{"id": s["id"], "title": s["title"], "kind": s["kind"], "meeting_id": s["meeting_id"],
              "chunks": s["n_chunks"], "labels": sorted(labels_of.get(s["id"], []))} for s in sources]
    nodes += [{"id": f"label:{name}", "title": name, "kind": "label"} for name in sorted({p["name"] for p in pairs})]
    return {"nodes": nodes, "edges": list(edges.values()), "threshold": round(threshold, 3)}
