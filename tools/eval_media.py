"""P0 evaluation for multimodal memory (docs/multimodal-memory-design.md): does searching the audio itself help?

Compares, on one set of clips and questions:
  * transcript search the way the app works today (EmbeddingGemma 2 text vectors + FTS5 keywords, fused),
  * audio-only search (EmbeddingGemma 2 audio vectors, typed question → spoken clip),
  * both fused, with the audio list at several weights,
and the word error rate of each speech engine when reference transcripts are known.

    python tools/eval_media.py --make-english C:\\evals\\english   # build an English set with Windows' offline voice
    python tools/eval_media.py C:\\evals\\english                  # run it (Parakeet transcripts)
    python tools/eval_media.py C:\\evals\\english --engines parakeet,whisper,gemma

Engines: parakeet, gemma, and whisper[:model][@language], e.g. whisper:large-v3-turbo@hi or whisper@en
(Whisper weights must already be on disk; nothing is downloaded).

A dataset is a folder with audio files and dataset.json:

    {"clips": [{"id": "budget", "file": "budget.wav", "reference": "optional exact transcript"}],
     "questions": [{"q": "How much is the budget?", "relevant": ["budget"]},
                   {"q": "Who owns the vendor list?", "at": {"file": "meeting.m4a", "t": 83.5}}]}

Long recordings work too: list them under "recordings" instead of "clips", optionally with the full "reference"
transcript (or "reference_file": a .txt next to it), and ask questions with "at": {"file", "t"[, "until"]} in seconds.
Recordings are cut into 20-second windows; a question counts as answered when a window overlapping its time (±2 s)
is found. Word error rates are skipped where an engine writes in another script than the reference (Devanagari vs
Latin letters), since word-by-word comparison would be meaningless there.

Everything runs locally against Ollama. The app's database is not touched (a temporary data folder is used).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("FORESIGHT_DATA_DIR", tempfile.mkdtemp(prefix="foresight-eval-"))  # keep usage rows out of the app DB
os.environ.setdefault("HF_HUB_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from foresight import llm  # noqa: E402
from foresight.retrieval import RRF_K, _fts_query  # noqa: E402

WINDOW_S = 20.0
GEMMA_ASR_PROMPT = ("Transcribe this audio verbatim, in the language it is spoken. Write Hindi or Hinglish speech in Latin "
                    "letters as it sounds. Reply with the transcript only.")

ENGLISH = {
    "clips": {
        "atlas-budget": "After Tuesday's finance review, the Atlas marketing budget was raised to $52,000, and the extra money goes to a second wave of paid social in October.",
        "helios-budget": "The Helios rebrand budget stays at $30,000. We are not adding anything until the customer survey comes back.",
        "launch-date": "Because of the app store review backlog, the public launch moves from September 15 to October 1.",
        "design-review": "Marco will run the design review on September 3, and he wants the final mockups two days before.",
        "vendor-quotes": "Priya needs final quotes from the two shortlisted vendors by Friday, and anything above $2,000 needs Dana's approval.",
        "printing": "The printing vendor needs fifteen business days of lead time, so the brochures have to be ordered this week.",
        "hiring": "We agreed to hire two more support engineers in Bangalore before the end of the quarter.",
        "outage": "The login outage last night lasted forty minutes and was caused by an expired certificate on the gateway.",
        "offsite": "The team offsite is moving to Goa in the second week of December, and Sara is collecting dietary preferences.",
        "churn": "Customer churn went up to four percent last month, mostly from small teams on the monthly plan.",
        "security": "Before the audit, every laptop must have disk encryption turned on and the old VPN client removed.",
        "pricing": "Sales wants to test a cheaper annual plan at eighty dollars a seat for teams under ten people.",
        "roadmap": "Offline mode is the top request from field staff, so it moves ahead of the dark theme on the roadmap.",
        "event-venue": "Contoso Events quoted $11,800 for the launch venue, including the livestream.",
        "data-retention": "Legal asked us to delete raw call recordings after thirty days and keep only the transcripts.",
        "onboarding": "New hires will get a buddy for their first month, and the checklist moves into the wiki.",
    },
    "questions": [
        ("How much is the Atlas marketing budget now?", "atlas-budget"),
        ("Is the Helios rebrand getting more money?", "helios-budget"),
        ("When is the public launch?", "launch-date"),
        ("Who is running the design review?", "design-review"),
        ("What is the deadline for the vendor quotes?", "vendor-quotes"),
        ("How much notice does the print shop need?", "printing"),
        ("Are we growing the support team?", "hiring"),
        ("What caused the sign-in problem yesterday?", "outage"),
        ("Where is the company retreat this year?", "offsite"),
        ("Why are customers leaving?", "churn"),
        ("What do we need to do with our computers before the audit?", "security"),
        ("Is there a discount for small teams?", "pricing"),
        ("What feature do people in the field want most?", "roadmap"),
        ("How much does the venue cost?", "event-venue"),
        ("How long do we keep call recordings?", "data-retention"),
        ("How do new employees get help when they join?", "onboarding"),
        ("Who approves big vendor contracts?", "vendor-quotes"),
        ("Which campaign gets the extra money in October?", "atlas-budget"),
    ],
}


# ---------------------------------------------------------------- dataset

def make_english(out: Path) -> None:
    """Speak the English set with Windows' built-in voices (offline), alternating voices, 16 kHz mono."""
    out.mkdir(parents=True, exist_ok=True)
    items = [{"id": cid, "file": f"{cid}.wav", "text": text} for cid, text in ENGLISH["clips"].items()]
    spec = out / "_tts.json"
    spec.write_text(json.dumps(items), encoding="utf-8")
    script = f"""
Add-Type -AssemblyName System.Speech
$items = Get-Content -Raw -Encoding UTF8 '{str(spec).replace("'", "''")}' | ConvertFrom-Json
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$probe = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = @($probe.GetInstalledVoices() | Where-Object {{ $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en-*' }} | ForEach-Object {{ $_.VoiceInfo.Name }})
$i = 0
foreach ($it in $items) {{
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  if ($voices.Count -gt 0) {{ $s.SelectVoice($voices[$i % $voices.Count]) }}
  $s.SetOutputToWaveFile((Join-Path '{str(out).replace("'", "''")}' $it.file), $fmt)
  $s.Speak($it.text); $s.Dispose(); $i++
}}
"""
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True)
    spec.unlink()
    dataset = {"clips": [{"id": it["id"], "file": it["file"], "reference": it["text"]} for it in items],
               "questions": [{"q": q, "relevant": [cid]} for q, cid in ENGLISH["questions"]]}
    (out / "dataset.json").write_text(json.dumps(dataset, indent=2), encoding="utf-8")
    print(f"Wrote {len(items)} clips and {len(dataset['questions'])} questions to {out}")


AT_TOLERANCE_S = 2.0


def _seconds(value: float | int | str) -> float:
    """83.5, "83.5", "1:23.5" or "0:01:23" → seconds."""
    if isinstance(value, str) and ":" in value:
        total = 0.0
        for part in value.split(":"):
            total = total * 60 + float(part)
        return total
    return float(value)


def load(folder: Path) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Clips (each with 16 kHz audio), questions with the set of clip ids that answer them, and the reference
    transcripts of whole recordings (file → text)."""
    from foresight.asr import decode_audio

    spec = json.loads((folder / "dataset.json").read_text(encoding="utf-8"))
    clips = []
    for c in spec.get("clips", []):
        clips.append({**c, "pcm": decode_audio(folder / c["file"])})
    references: dict[str, str] = {}
    for rec in spec.get("recordings", []):
        if rec.get("reference_file"):
            references[rec["file"]] = (folder / rec["reference_file"]).read_text(encoding="utf-8")
        elif rec.get("reference"):
            references[rec["file"]] = rec["reference"]
        pcm = decode_audio(folder / rec["file"])
        step = int(WINDOW_S * llm.AUDIO_RATE)
        for start in range(0, max(len(pcm), 1), step):
            clips.append({"id": f"{rec['file']}@{start // llm.AUDIO_RATE}", "file": rec["file"],
                          "t0": start / llm.AUDIO_RATE, "t1": min(len(pcm), start + step) / llm.AUDIO_RATE,
                          "pcm": pcm[start:start + step]})
    questions = []
    for q in spec["questions"]:
        relevant = set(q.get("relevant", []))
        if "at" in q:
            at = q["at"]
            lo, hi = _seconds(at["t"]) - AT_TOLERANCE_S, _seconds(at.get("until", at["t"])) + AT_TOLERANCE_S
            relevant |= {c["id"] for c in clips if c.get("file") == at["file"] and "t0" in c and c["t0"] < hi and c["t1"] > lo}
        if relevant:
            questions.append({"q": q["q"], "relevant": relevant})
        else:
            print(f"skipped (no audio at that time): {q['q']!r}")
    return clips, questions, references


# ---------------------------------------------------------------- speech engines

def transcribe(engine: str, clips: list[dict]) -> tuple[list[str], float]:
    """engine: parakeet | gemma | whisper[:model][@language] (language: hi, en, auto…)."""
    from foresight import asr
    from foresight.config import settings

    name, _, language = engine.partition("@")
    name, _, model_name = name.partition(":")
    texts: list[str] = []
    started = time.time()
    if name == "gemma":
        import base64
        for c in clips:
            msg = {"role": "user", "content": GEMMA_ASR_PROMPT,
                   "images": [base64.b64encode(llm.wav_bytes(c["pcm"])).decode("ascii")]}
            texts.append(llm.chat([msg], task="eval_asr", options={"temperature": 0, "num_predict": 400}).strip())
    elif name == "whisper":
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name or settings.whisper_model, device=settings.whisper_device,
                             compute_type=settings.whisper_compute, local_files_only=True)
        lang = None if language == "auto" else (language or settings.whisper_language)  # None = detect per clip
        started = time.time()  # do not count model loading
        for c in clips:
            segments, _ = model.transcribe(c["pcm"], language=lang, beam_size=1, vad_filter=True,
                                           condition_on_previous_text=False)
            texts.append(" ".join(s.text.strip() for s in segments if asr.WhisperEngine._keep(s)).strip())
    elif name == "parakeet":
        model = asr.ParakeetEngine()
        started = time.time()
        texts = [model.transcribe(c["pcm"]) for c in clips]
    else:
        raise ValueError(f"unknown engine {engine!r}")
    return texts, time.time() - started


_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def devanagari_share(text: str) -> float:
    """Share of letters written in Devanagari (0 = all Latin letters)."""
    letters = [ch for ch in text if ch.isalpha() or _DEVANAGARI.match(ch)]
    return sum(1 for ch in letters if _DEVANAGARI.match(ch)) / len(letters) if letters else 0.0


def same_script(a: str, b: str) -> bool:
    return abs(devanagari_share(a) - devanagari_share(b)) < 0.3


def _words(text: str) -> list[str]:
    text = re.sub(r"(?<=\d),(?=\d)", "", text.lower())
    return re.sub(r"[.,!?;:\"“”‘’()\[\]{}…$%–—।॥-]", " ", text).split()


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = _words(reference), _words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[len(hyp)] / max(1, len(ref))


# ---------------------------------------------------------------- ranking

def rank_by_vectors(query_vecs: np.ndarray, doc_vecs: np.ndarray) -> list[list[int]]:
    return [list(np.argsort(-(doc_vecs @ q))) for q in query_vecs]


def rank_by_keywords(questions: list[dict], texts: list[str]) -> list[list[int]]:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='porter unicode61')")
    conn.executemany("INSERT INTO t(rowid, text) VALUES (?, ?)", [(i, t) for i, t in enumerate(texts)])
    out = []
    for q in questions:
        fts = _fts_query(q["q"])
        rows = conn.execute("SELECT rowid FROM t WHERE t MATCH ? ORDER BY bm25(t)", (fts,)).fetchall() if fts else []
        out.append([r[0] for r in rows])
    return out


def fuse(rankings: list[tuple[list[list[int]], float]], n_questions: int) -> list[list[int]]:
    """Weighted reciprocal-rank fusion, the same formula retrieval.py uses (weights default to 1 there)."""
    fused = []
    for qi in range(n_questions):
        scores: dict[int, float] = {}
        for ranking, weight in rankings:
            for rank, doc in enumerate(ranking[qi]):
                scores[doc] = scores.get(doc, 0.0) + weight / (RRF_K + rank)
        fused.append(sorted(scores, key=scores.get, reverse=True))
    return fused


def metrics(ranked: list[list[int]], questions: list[dict], clips: list[dict]) -> dict:
    r1 = r5 = mrr = 0.0
    for order, q in zip(ranked, questions):
        hits = [i for i, doc in enumerate(order) if clips[doc]["id"] in q["relevant"]]
        if hits:
            r1 += hits[0] == 0
            r5 += hits[0] < 5
            mrr += 1 / (hits[0] + 1)
    n = max(1, len(questions))
    return {"recall@1": r1 / n, "recall@5": r5 / n, "mrr": mrr / n}


# ---------------------------------------------------------------- main

def engine_wer(clips: list[dict], texts: list[str], references: dict[str, str]) -> dict:
    """Mean word error rate over clips with a reference and whole recordings with one. Pairs written in different
    scripts are left out and counted instead."""
    pairs = [(c["reference"], t) for c, t in zip(clips, texts) if c.get("reference")]
    for file, reference in references.items():
        windows = sorted((c["t0"], t) for c, t in zip(clips, texts) if c.get("file") == file and "t0" in c)
        pairs.append((reference, " ".join(t for _, t in windows)))
    comparable = [(r, h) for r, h in pairs if same_script(r, h)]
    out = {"wer": round(float(np.mean([wer(r, h) for r, h in comparable])), 3) if comparable else None}
    if len(comparable) < len(pairs):
        out["note"] = f"{len(pairs) - len(comparable)} of {len(pairs)} transcripts are in another script than the reference"
    return out


def rerank_rankings(ranked: list[list[int]], questions: list[dict], texts: list[str], clips: list[dict],
                    top: int = 8) -> list[list[int]]:
    """Gemma re-ranks the top candidates of each question, as the app does before answering (retrieval.rerank)."""
    from foresight import retrieval

    out = []
    for order, q in zip(ranked, questions):
        head = order[:top]
        hits = [retrieval.Hit(i, i, clips[i]["id"], "", "transcript", texts[i], 0.0, 0.0) for i in head]
        out.append([h.chunk_id for h in retrieval.rerank(q["q"], hits, len(hits))] + [i for i in order if i not in head])
    return out


def evaluate(folder: Path, engines: list[str], rerank: bool = False) -> dict:
    clips, questions, references = load(folder)
    print(f"{len(clips)} clips, {len(questions)} questions, {sum(len(c['pcm']) for c in clips) / llm.AUDIO_RATE:.0f} s of audio")
    report: dict = {"dataset": str(folder), "clips": len(clips), "questions": len(questions), "engines": {}, "search": {}}

    transcripts: dict[str, list[str]] = {}
    audio_s = sum(len(c["pcm"]) for c in clips) / llm.AUDIO_RATE
    for engine in engines:
        try:
            texts, seconds = transcribe(engine, clips)
        except Exception as exc:  # e.g. Whisper weights not downloaded; the other engines still run
            report["engines"][engine] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            continue
        transcripts[engine] = texts
        report["engines"][engine] = {"seconds": round(seconds, 1), "speed_x_realtime": round(audio_s / max(seconds, 1e-6), 1),
                                     "devanagari": round(devanagari_share(" ".join(texts)), 2),
                                     **engine_wer(clips, texts, references)}
        safe_name = re.sub(r"[^\w.@-]+", "_", engine)
        (folder / f"transcript.{safe_name}.txt").write_text(  # to read each engine's mistakes
            "\n".join(f"[{c['id']}] {t}" for c, t in zip(clips, texts)), encoding="utf-8")

    started = time.time()
    audio_vecs = llm.embed_audio([llm.wav_bytes(c["pcm"]) for c in clips])
    report["audio_embedding_seconds"] = round(time.time() - started, 1)
    query_vecs = llm.embed([q["q"] for q in questions], query=True)
    by_audio = rank_by_vectors(query_vecs, audio_vecs)
    report["search"]["audio only"] = metrics(by_audio, questions, clips)

    for engine, texts in transcripts.items():
        by_text = rank_by_vectors(query_vecs, llm.embed(texts))
        by_words = rank_by_keywords(questions, texts)
        n = len(questions)
        rows = {
            "transcript vectors": by_text,
            "keywords": by_words,
            "app today (vectors + keywords)": fuse([(by_text, 1.0), (by_words, 1.0)], n),
        }
        for w in (0.25, 0.5, 1.0):
            rows[f"app + audio x{w}"] = fuse([(by_text, 1.0), (by_words, 1.0), (by_audio, w)], n)
        if rerank and engine == next(iter(transcripts)):  # one Gemma call per question: first engine only
            started = time.time()
            rows["app today + Gemma rerank"] = rerank_rankings(rows["app today (vectors + keywords)"], questions, texts, clips)
            report["rerank_seconds_per_question"] = round((time.time() - started) / max(1, n), 1)
        for name, ranked in rows.items():
            report["search"][f"{engine}: {name}"] = metrics(ranked, questions, clips)
    return report


def print_report(report: dict) -> None:
    if report["engines"]:
        print("\nSpeech engines")
        print("| Engine | Word error rate | Speed (x real time) | Devanagari share |\n|---|---|---|---|")
        for name, e in report["engines"].items():
            if "error" in e:
                print(f"| {name} | failed: {e['error']} | | |")
                continue
            rate = "n/a" if e["wer"] is None else format(e["wer"], ".1%")
            if e.get("note"):
                rate += f" ({e['note']})"
            print(f"| {name} | {rate} | {e['speed_x_realtime']} | {e['devanagari']:.0%} |")
    print(f"\nSearch ({report['questions']} questions over {report['clips']} clips; "
          f"audio embedding took {report['audio_embedding_seconds']} s)")
    print("| Method | Recall@1 | Recall@5 | MRR |\n|---|---|---|---|")
    for name, m in report["search"].items():
        print(f"| {name} | {m['recall@1']:.0%} | {m['recall@5']:.0%} | {m['mrr']:.3f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dataset", nargs="?", type=Path, help="folder with dataset.json and audio files")
    parser.add_argument("--make-english", type=Path, metavar="FOLDER", help="write the English clip set here and exit")
    parser.add_argument("--engines", default="parakeet", help="comma-separated: parakeet, whisper, gemma")
    parser.add_argument("--rerank", action="store_true", help="also measure Gemma re-ranking (one call per question)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Devanagari would crash a cp1252 Windows console
    if args.make_english:
        make_english(args.make_english)
        return
    if not args.dataset:
        parser.error("give a dataset folder, or --make-english FOLDER")
    caps = llm.check_media()
    if not caps["audio_embed"]:
        sys.exit(f"Audio embeddings are not available: {caps['error']}")
    report = evaluate(args.dataset, [e.strip() for e in args.engines.split(",") if e.strip()], rerank=args.rerank)
    print_report(report)
    out = args.dataset / "results.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
