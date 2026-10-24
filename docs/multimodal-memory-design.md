# Multimodal memory: design

Status (2026-10-18): decisions are in section 18. **Built:**

- P0 on English: capability check, `tools/eval_media.py`, results in section 2.
- P1: opt-in audio storage, playback synced to the transcript, retention setting.
- P2: audio moments in search (`media_index.py`, `retrieval.py`), at weight 0.5, with ▶ on citations.
- P3: Gemma listens (`listen.py`). "Ask about this moment" (✦ on a transcript line, or "Ask" next to a cited moment),
  "Listen to cited audio" in Chat (up to two clips of 30 s or less), and "Re-transcribe with Gemma" on a line. A
  re-transcription is a suggestion: saving it updates the line, the audio windows' text, and the transcript source.

- P4: pictures (`images.py`), built 2026-10-18; see 6.4 for what changed from the plan and what was measured.
- P5: voice memos (`memos.py`), built 2026-10-18; see 6.5.

**Next:** P0 on real Hinglish audio, to set the speech engine and the audio weight (`FORESIGHT_MEDIA_WEIGHT`).

First real runs of P3 (synthetic English voice, so indicative only):

- "Ask about this moment" on the line Parakeet wrote as "Donna's approval" produced the correct answer, "Dana Kim".
- With "Listen to cited audio" on, Chat also named Dana Kim. A vendor document says the same, so audio alone can't take
  the credit.
- Gemma's re-transcription of that line was not better ("down as approval"). That is why it stays a suggestion the
  user accepts or keeps.

As built, P2 differs from sections 6–7 in a few details:

- Indexing progress lives in a separate `media_index` table, so the P1 `media` table needs no migration.
- Live Assistance searches text only (`with_media=False`), because its threshold is calibrated on text-to-text similarity.
- Search results can be filtered with `/api/search?modality=all|text|audio`.

The first real run shows why P3 matters. The audio moment correctly pointed to where "Dana's approval" was said, but
Parakeet had transcribed the name as "Donna", and Gemma, reading the transcript, repeated the mistake.

## 1. Why

Google's launch post for EmbeddingGemma 2 describes Google AI Edge Foresight as "mapping text, audio, and visual data into a single
vector space", with cross-modal retrieval that finds "images, documents, transcripts, and notes using natural language".

Foresight Wing was text-only before this design:

- Meeting audio is transcribed and then thrown away. Live meetings keep no audio at all.
- Images are not indexed.
- EmbeddingGemma 2 is used only for text, although the installed `embeddinggemma-2:740m` also embeds audio and images.

**Goal:** one index in EmbeddingGemma 2's shared 768-dimension space that holds text chunks, audio moments and images. Any
typed question can find any of them. A hit can be played or shown, and Gemma 4 can listen to or look at it when a question
needs that.

**Non-goals for now:** video, speaker identification beyond You / Others, cloud anything.

The Skilltitute explainer video ("Google's AI Edge Foresight: An AI Meeting Assistant That Runs Locally?", 3:45) is a
non-technical overview. It confirms the user-facing features (shorthand enhanced from the transcript, search across past
conversations and files, offline on-device processing, a caution that local does not automatically mean private) but says
nothing about internals, so it does not change this design.

## 2. What was measured on this PC (2026-10-18)

Ollama 0.40.5, `embeddinggemma-2:740m`, `gemma4:e2b-it-qat`, 8 CPU cores, 4 GB GPU. Clips were synthesized with Windows'
offline voice (clean speech, 6–8 s), so this is a smoke test, not a benchmark.

| Probe | Result |
|---|---|
| Text embedding (`/api/embed`) | 768 dimensions |
| Audio embedding, `input: [{"audio": "<base64 WAV>"}]` | Works. Warm: 1.1–1.4 s per 6–8 s clip (about 0.2× real time). Cold load: about 20 s. Raw float arrays are rejected (`input.audio must be a string`) |
| Audio via the `images` field of `/api/embed` | **Silently ignored**: it embeds the empty string. Both clips came back identical |
| Text query vs audio clip | Budget question: 0.83 to the budget clip vs 0.61 to the other. Printing question: 0.81 vs 0.55. Unrelated question: about 0.49 to both. The same sentence as text vs as audio: 0.885 |
| Image embedding, `input: [{"image": "<base64 PNG>"}]` | Works. Warm: **11–18 s** per 1000×562 chart. Cold: about 70 s. Two similar charts separated weakly (0.76 vs 0.70) |
| Gemma 4 E2B listening (`/api/chat`, WAV bytes in `images`) | 1.7 s for a 6.1 s clip (warm). Accurate. About 25 prompt tokens per second of audio. Ollama splits clips longer than 30 s (v0.33.3 release notes) |
| Parakeet TDT 0.6B v3 int8 (current engine) | 1.05 s for 6.1 s, 1.61 s for 7.7 s. Like Gemma, it writes "fifty two thousand dollars" as "$52,000" |
| Gemma 4 E2B vision | Works (47 s cold) |
| Memory held by Ollama | Gemma 4 E2B 3.68 GiB, EmbeddingGemma 2 0.35 GiB (both partly on the GPU) |

Correction to an earlier answer in chat: Parakeet is not more word-for-word than Gemma. Both turn spoken numbers into
digits. On speed, the single-clip probe made the two look close, but over the 119 s English set (P0 results below) Parakeet
ran at 6.9× real time and Gemma at 1.9×. Gemma also runs one generation at a time (`llm._gen_lock`), so transcribing with it
would make Live Assistance wait (section 9).

### P0 results, English (2026-10-18)

`tools/eval_media.py` on 16 synthetic English clips (119 s, Windows voices) and 18 typed questions, several of them
paraphrased ("print shop" for "printing vendor", "retreat" for "offsite"):

| Speech engine | Word error rate | Speed |
|---|---|---|
| Parakeet TDT 0.6B v3 int8 | 5.6% | 6.9× real time |
| Whisper `small` (CPU int8) | 5.5% | 1.0× real time |
| Gemma 4 E2B | 9.1% | 1.9× real time |

| Search | Recall@1 | MRR |
|---|---|---|
| Audio only (question → spoken clip, no transcript) | 94% | 0.963 |
| Transcript vectors only (Parakeet) | 100% | 1.000 |
| The app today: transcript vectors + keywords (Parakeet) | 94% | 0.956 |
| The app + audio, weight 0.5 / 1.0 (Parakeet) | 94% | 0.958 / 0.958 |
| The app + audio, weight 1.0 (Gemma transcripts) | 94% | 0.972 |

What it means:

- Audio-only search already works well across modalities.
- On clean English, transcripts are near-perfect, so audio adds almost nothing. That was expected. The deciding test is
  real Hinglish audio, still to come.
- Keyword search pulls one answer down from first place on this set. Worth checking on real data before tuning weights.

### Hindi smoke test (2026-10-18; not the P0 result)

Two short clips the user supplied (4.2 s and 2.7 s at 24 kHz, likely text-to-speech, pure Hindi):

| Engine | Output | Time for 6.9 s of audio |
|---|---|---|
| Parakeet TDT 0.6B v3 | Unusable romanization ("Mirab Hai Add School Nahi…") | 0.9 s |
| Whisper `small` (auto or `hi`) | Devanagari, 2–4 word errors per sentence | 6.7 s |
| Whisper `large-v3-turbo` (`hi`) | Devanagari, both sentences exact | 27 s |
| Gemma 4 E2B | Latin-letter Hinglish, nearly exact (one tense slip) | 22.8 s |

How well four questions (two English, two typed Hinglish in Latin letters) separated the right clip from the other one,
by cosine margin:

- Devanagari transcripts: +0.20 to +0.26. Search works across scripts.
- Gemma's Latin transcripts: +0.19 to +0.28.
- The audio itself: +0.16 to +0.24.
- Parakeet's transcripts: +0.02 to +0.16.

FTS5 keywords cannot match across scripts, so for Devanagari transcripts the vectors carry the search.

Tentative reading, to be confirmed on real meeting audio:

- Parakeet must not transcribe Hindi or Hinglish.
- On this CPU no engine is both accurate and live for Hindi:
  - Whisper `small` keeps up but makes mistakes.
  - `large-v3-turbo` and Gemma are accurate but about 3–4× slower than real time.
- A likely policy: Whisper `small` live for Hindi/Hinglish meetings, then a background re-transcription with
  `large-v3-turbo` after the meeting. Audio search covers the rough live transcript in the meantime, which argues for
  keeping the audio weight at 0.5 or higher for these meetings.
- The weight itself cannot be tuned from two clips.

### Published accuracy (web check, 2026-10-18)

Word error rates on Hindi and Hinglish speech, lower is better. They were measured by different parties with
different text normalisation, so compare within a column rather than across sources.

| Engine | FLEURS Hindi | Common Voice Hindi | CoSHE-500 (conversational Hinglish) | CS-FLEURS hi-en (read Hinglish) | Source |
|---|---|---|---|---|---|
| Gemma 4 E2B (installed) | **10.1** | — | — | — | Gemma 4 technical report, Table 7 |
| Gemma 4 E4B | 8.6 | — | — | — | same |
| Whisper large-v3 | 27.5 | 30.8 | 29.7 | 33.9 | whisper-hinglish-preview page |
| whisper-hinglish-preview (large-v3 fine-tune, Apache-2.0) | 12.6 | 12.9 | 13.7 | 10.2 | same |
| Tara (large-v3 variant, Apache-2.0) | 10.5 | 12.5 | 14.4 | 8.4 | Trelis/tara model card |
| Parakeet TDT 0.6B v3 | not supported (25 European languages) | | | | NVIDIA model card |

- Whisper `large-v3-turbo` has no published Hindi figure. One model directory ranks it well behind the leaders on
  Hindi (2.4× the best error rate), and it is a distilled version of large-v3. Expect it to be no better than large-v3's
  ~28%, and Whisper `small` worse.
- Code-switching raises error rates by a further 30–50% relative to single-language speech (HiACC paper).
- The smoke test above, where `large-v3-turbo` was exact, used two clean text-to-speech sentences, which are far
  easier than real meetings.

What this changes in the plan:

- The accurate pass for Hindi/Hinglish should be **Gemma 4 E2B**, not Whisper. It is already installed, scored 10.1%
  vs ~28% for Whisper large-v3 on FLEURS Hindi, and writes Hinglish in Latin letters when asked, which is how people
  type their questions.
- The fine-tuned Hinglish Whisper models (Tara, whisper-hinglish-preview) are the strongest open options for
  conversational code-switching. But they are large-v3-sized, so on this CPU they would run well below real time.
  They would also need a download and a conversion, so they are a later option.
- Live transcription for Hindi stays the weak spot on this PC. This is where audio search matters.
- The user's real recording (P0) still decides it: these are benchmark numbers, not this team's voices, microphones
  or vocabulary.

### Built: Hindi / Hinglish meetings (2026-10-18)

- **Language per meeting.** `meetings.language` is `en` or `hi`. It is chosen in the start controls (remembered as
  `meeting_language`), used for imports, and added to older databases by `db._migrate`.
- **Live engine** (`asr.Transcriber.for_language`): English uses Parakeet. Hindi / Hinglish uses Whisper `small`
  forced to Hindi, decoding each utterance once (`temperature=0.0`).
- **Gemma's pass** (`repass.py`): after a Hindi meeting it fills gaps from the saved audio, then re-transcribes every
  line with Gemma 4 E2B in Latin-letter Hinglish. Lines that come back in Devanagari get a text-only transliteration.
  Empty, refusal, chatbot-style or runaway answers keep the live line. Replaced wording goes to `segment_versions`.
- **Audio** is kept for the pass and deleted afterwards unless *Keep meeting audio* is on.
- **Stopping** a Hindi meeting does not wait for the live backlog (`LiveCapture.stop(transcribe_backlog=False)`); the
  pass picks that speech up from the saved audio.

**First live test** (Jatin Sapru's cricket squads review on YouTube, captured as system audio, 3.5 minutes):

- Whisper `small` fell about 10× behind real time: 2 lines in 4 minutes, with a queue of 15 utterances. On music under
  overlapping voices, faster-whisper's default temperature retries re-decode each 15 s utterance up to 6 times, and its
  output was garbage: mixed scripts, Cyrillic, repetition loops ("तो बबब विन्त सीरीज…").
- Stopping took 121 s, the backlog limit, and the old pass ran on the 4 lines that existed then.
- Re-run with gap filling on the saved 312 s: 18 lines (1 filled gap), 14 rewritten by Gemma in 87 s (about 3×
  faster than real time). The result was readable Hinglish with the right names and terms (Bhuvneshwar Kumar,
  Bengaluru, T20, Ishan Kishan, strike rate, IDFC First Bank).
- Two problems led to filters:
  - Gemma answered one jingle like a chatbot ("नमस्ते! मैं आपकी कैसे मदद कर सकता हूँ?").
  - Gemma wrote 4 of 18 lines in Devanagari.

**Fixes after the first test:**

- **Lines in Devanagari** get a text-only transliteration into Latin letters. It is accepted only when the word count
  stays within ±30% and the reply has no preamble.
- **Chatbot-style replies, `NO_SPEECH` and "Language:" labels** are filtered out. Text filters alone kept missing new
  phrasings, for example "Namaste. Main ek AI hoon." and "नमस्ते मैं आपको ट्रांसक्राइब कर रहा हूँ।".
- **A speech check comes first** (`repass.speech_seconds`, the Silero detector bundled with faster-whisper, about
  30 ms per 15 s). It tells speech from music, which the loudness-based live detector cannot:

  | Clip | Length | Speech detected |
  |---|---|---|
  | Jingle gap | 5.5 s | 0.00 s |
  | Speech | 15 s | 15 s |
  | English ad over music | 15 s | 15 s |
  | Intro jingle with announcer | 15 s | 5.7 s |

  Gaps need 1 s of speech to become a line. Live lines with under 0.3 s of speech are removed as text written over
  music. Clips without speech are never sent to Gemma.

**Re-test** (same show, 2.5 minutes, system audio):

| | First test | Re-test |
|---|---|---|
| Live lines during playback | 2 in 230 s | 10 in 161 s |
| Largest backlog | 15 utterances | 1 |
| Lag once warm | — | 7–9 s |
| Time to stop | 121 s | 4 s |

- The pass took about 70 s for 188 s of audio and produced 12 lines, all in Latin letters, with the jingle
  hallucination gone.
- One name over music was still misheard: "Jatin Sapru" became "Language tan sapluk".
- Live Whisper text on this TV-style audio (music under overlapping voices) remains poor. So Live Assistance in
  Hindi meetings will catch fewer questions until a better live engine fits on this CPU. The final transcript is
  Gemma's.

## 3. Principles

1. **The transcript stays the text backbone.** Gemma answers from text, keyword search needs text, and exact numbers and
   names live in text. Media vectors are an extra way to *find* things, plus a pointer back to the original media.
2. **One vector space, one index.** Every indexable unit gets a 768-dimension vector in the same matrix. A question is embedded
   once and compared with everything.
3. **Keep the original media**, so a hit can be played or shown, and passed to Gemma when a question is about *how*
   something sounded or looked.
4. **Privacy first.** Keeping meeting audio is opt-in, has a retention period, and is deleted with its meeting. No new network
   calls.
5. **The live path does not change.** Capture → voice detection → Parakeet → Live Assistance stays as it is. All media
   embedding runs in the background and pauses while a meeting is recording.
6. **Measure before switching on by default.** An evaluation script decides how much weight audio hits get.

## 4. Overview

```
live (unchanged)   mic / loopback ─► Segmenter (VAD) ─► Parakeet ─► segments ─► Live Assistance
                          │
                          └─ opt-in: AudioStore, one Ogg Opus file per channel ─► media row

background         MediaIndexer (one worker, paused while recording)
                     ├─ audio:  speech windows of ~20–30 s, cut at utterance boundaries ─► embed_audio ─► media_items
                     ├─ images: downscaled image ─► embed_image ─► media_items (+ thumbnail)
                     └─ text:   indexer.py as today ─► chunks (+ FTS5)

question ─► embed once ─► VectorIndex (chunks + media_items, 768-d) ─┐
         └─► FTS5 over text chunks ──────────────────────────────────┴─► weighted RRF ─► hits

hits ─► context: text as text · audio as its transcript (+ the clip when Gemma should listen) · image as the image (Gemma vision)
```

## 5. Data model

New tables in `db.py`. Text chunks stay in `chunks`, unchanged, so the text path and its FTS triggers carry no migration risk.

```sql
CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER REFERENCES sources(id) ON DELETE CASCADE,   -- knowledge source it belongs to
    meeting_id  INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,               -- recording | audio | image
    channel     TEXT,                        -- You | Others | Recording (meeting audio only)
    path        TEXT NOT NULL,
    owned       INTEGER NOT NULL,            -- 1 = Foresight Wing created the file and may delete it; 0 = the user's file
    mime        TEXT,
    duration_s  REAL,
    width       INTEGER,
    height      INTEGER,
    bytes       INTEGER,
    sha256      TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued | indexing | ready | error | expired
    error       TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL                         -- retention: the file is deleted after this; vectors and transcript stay
);

CREATE TABLE IF NOT EXISTS media_items (
    id         INTEGER PRIMARY KEY,
    media_id   INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    t_start    REAL,                         -- audio window in seconds; NULL for images
    t_end      REAL,
    text       TEXT NOT NULL DEFAULT '',     -- transcript of the window (Gemma context, display); '' if none
    embedding  BLOB NOT NULL,                -- float32, L2-normalised
    dim        INTEGER NOT NULL,
    model      TEXT NOT NULL                 -- embedding model, so a model change can trigger re-indexing
);
CREATE INDEX IF NOT EXISTS media_items_media ON media_items(media_id, t_start);
```

Files live under `data_dir/media/` (`meeting-<id>/<channel>.ogg`, `thumbs/<media_id>.webp`). Rows cascade, but SQLite cannot
delete files, so `media.purge()` deletes owned files explicitly. A sweep at startup removes owned files with no row. The
sweep also closes an existing gap: an imported recording stays in `recordings/` after its meeting is deleted.

## 6. Pipelines

### 6.1 Live meeting audio (opt-in)

- **Tap:** `LiveCapture._capture` already reads 16 kHz mono blocks for the Segmenter. It also hands each block to an
  `on_audio(channel, block)` callback, which must never block capture. `asr.py` is being reworked in another session, so this
  one hook is the only change there.
- **Writer:** `AudioStore` runs one thread per channel with a queue. It encodes Ogg Opus at 32 kbps (PyAV 18 is installed and
  has `libopus`), about **14 MB per hour per channel**. If the queue overflows, it drops blocks and records a gap rather than
  stalling capture.
- **Timeline:** segment times already count samples from the start of each channel, so the transcript and the stored audio
  line up exactly.
- **Crash safety:** Ogg is written page by page, so a crash keeps everything up to the last page. The transcript does not
  depend on the audio file.
- The whole stream is stored, not just speech, so playback works anywhere on the timeline. Opus silence is cheap.

### 6.2 Imported recordings

These are already saved in `recordings/`. Each becomes a `media` row (`kind=recording`, `channel=Recording`, `owned=1`). The
original file is played directly, since every format in `AUDIO_EXTS` plays in Chromium.

### 6.3 Audio moments (MediaIndexer)

- **Input:** the decoded media (16 kHz mono float32) plus the meeting's segments.
- **Windows:** consecutive segments on one channel are grouped into windows of about 20 s (at most 30 s), cut at utterance
  boundaries and overlapping by one utterance. Silence is never embedded. Audio with no transcript (music, or audio files
  indexed without speech recognition) uses fixed 20 s windows with a 5 s hop, skipping quiet windows.
- **Window text:** the transcript lines inside the window.
- **Embedding:** encode to PCM16 WAV → base64 → `/api/embed` with `[{"audio": ...}]`, 4 windows per request → normalise.
- **Cost:** about 0.2× the audio length (measured), so an hour of speech takes about 12 minutes after the meeting. Resumable:
  it continues from the last stored `t_end`.

### 6.4 Images

**Built (2026-10-18).** What changed from the plan below, and why:

- **Gemma describes every picture** (what it shows, then any text copied out). Warm, it takes about 2 s per picture
  on this PC (26 s for the first, cold call), not the 11–18 s first measured, so the caption is on by default. It makes
  pictures findable by words and gives Gemma text context. It copied a receipt's lines exactly; on a 4-bar chart it
  said five bars, so Gemma also looks at the picture itself when it is cited (up to two per answer).
- **Folders are indexed by default**, with guards instead of a per-folder opt-in: files under 12 KB (icons, UI art) are
  skipped, Settings → *Index pictures in watched folders* turns it off, and pictures wait while a meeting records (a
  description holds Gemma, which live answers need). Uploads are always indexed.
- **Model input:** 1024 px JPEG on the long side. **Thumbnail:** 320 px JPEG in `thumbs/` (not `media/`, whose sweep
  removes files no row points at). The picture itself stays where it is; Foresight Wing never deletes it.
- **Search:** the picture's EmbeddingGemma 2 vector is a fourth ranked list, at full weight but only from cosine 0.65 up.
  Without a floor, rank fusion would put the closest picture in every answer; at the audio weight (0.5), a picture found
  only by its look would rank below every text chunk. Measured text-to-image cosines: matching descriptions 0.68–0.79
  on real screenshots and simple charts, unrelated ones 0.50–0.67 (`FORESIGHT_IMAGE_MIN_SIMILARITY`). A result from a
  picture's description shows the picture as well.
- Not done: slide and PDF page images (they need a page renderer).

The plan, as written before building:

- **Sources:** image files (png, jpg, jpeg, webp) in folders and uploads. Folder indexing is opt-in per folder, because of cost.
  Slide and PDF page images come later: they need a page renderer that is not installed.
- **Before embedding:** downscale to at most 896 px on the long side. Measure whether this cuts the 11–18 s per image. Store a
  320 px WebP thumbnail.
- **Text:** the file name. An optional Gemma caption, off by default (one slow call per image), would make images findable by
  keyword and give text context.
- **Cost guard:** at about 15 s per image, 1,000 images take about 4 hours. Run in the background, pause during meetings, show
  progress.

### 6.5 Audio files in Knowledge (voice memos)

**Built (2026-10-18).** An audio file in a watched folder or an upload is transcribed with the engine for the default
meeting language (Parakeet; Whisper for Hindi / Hinglish) into `[mm:ss]` lines, the same format as meeting transcripts,
so a cited passage plays from its first line. Its sound is cut into ~20 s windows along those lines (fixed 20 s windows,
first 30 minutes, when no speech is recognised) and embedded by EmbeddingGemma 2. The media row points at the user's
own file (`owned = 0`): it is played from there and never copied, changed or deleted. Unchanged files are not
transcribed again. Like pictures, folders are on by default with a setting to turn it off, files under 48 KB are
skipped, and files wait while a meeting records (live speech recognition needs the CPU).

First real run (Windows TTS voice, 19 s memo): indexed in 20 s, and it was the top result for three different questions
about it. Parakeet wrote "four thousand euros" as "€400"; Gemma, asked to transcribe the clip, heard it right. With
*Listen to cited audio* on, Gemma E2B still repeated the transcript's €400, so the listen prompt does not reliably make it
trust the audio over the text. Not done: Gemma's re-transcription pass for Hindi / Hinglish memos (meetings have it).

The plan, as written before building:

Opt-in per folder. Decode → Parakeet `transcribe_file` (timestamped text, stored as a text source) → audio windows (6.3).
Files with no speech still get windows, so "piano intro" can find them.

## 7. Retrieval

- **VectorIndex:** `chunks` and `media_items` are loaded into one float32 matrix, with parallel arrays for row kind, row id,
  source id and media id. Each vector takes 3 KB, so 100k vectors take about 300 MB. If that grows, cut vectors to 256
  dimensions (Google: about 95% of quality kept on speech and image retrieval).
- **Query:** one embedding with the existing `task: search result | query:` prefix.
- **Ranking:** three ranked lists (text vectors, FTS5 keywords, media vectors), merged with weighted reciprocal-rank fusion.
  Weights start at text 1, keywords 1, media 0.5, then get tuned with the evaluation script. Ranks are fused rather than raw
  scores, because cross-modal cosines sit on a different scale (about 0.8 for a match and 0.5 for unrelated audio, here).
- **One card per moment:** when an audio window overlaps a transcript chunk of the same meeting, the audio becomes a
  `media` attachment on that hit instead of a second hit. The existing cap of 3 hits per source still applies.
- **Hit:** gains `modality` and `media: {id, kind, channel, t_start, t_end, thumb}`, and `as_citation()` passes them to the UI.
- **Thresholds:** Live Assistance keeps its similarity threshold for text hits. Media hits get their own `media_min_score`.
- **Filters:** labels work as now, through `source_id`. `/api/search` gains `modality=all|text|audio|image`.

## 8. Answering with media (Gemma listens and looks)

- **Default:** Gemma gets text only. Audio hits contribute their transcript, image hits their file name or caption.
- **Listen and look:** used from a moment's "Ask about this" action, or a "Use audio and images" toggle in Chat. It attaches up
  to 2 clips (30 s or less each) and 2 downscaled images to the call, as WAV or image bytes in `images` (both measured). A 30 s
  clip costs about 750 tokens, which fits the 8,192-token window.
- It shares the single Gemma lock. It is not used in Live Assistance, for latency.

## 9. Speech recognition engine

Gemma is similar in speed on short clips, but as the live engine it would hold the Gemma lock for every utterance, and Live
Assistance answers would queue behind it. With stored audio, a **"Re-transcribe with Gemma"** action on a segment or moment
gives a second opinion.

**Meetings are in English, Hindi and Hinglish (decision 3).** Parakeet TDT 0.6B v3 covers 25 European languages, not Hindi, so
it cannot be the only live engine:

- **Engines to compare in P0**, on a few minutes of real Hinglish meeting audio (recorded with consent): Parakeet, Whisper
  `small` and `large-v3-turbo` (already supported through `FORESIGHT_ASR_ENGINE=whisper`), and Gemma 4 E2B. Measure a word
  error rate on a hand-checked transcript, plus speed.
- **Likely outcome:** a language choice per meeting (default in Settings, changeable when starting a meeting). English goes to
  Parakeet. Hindi and Hinglish go to whichever engine wins P0, or Whisper as the fallback.
- **Audio search matters more here.** If transcription of Hinglish is weak, audio vectors can still match an English question
  to Hindi speech. P0 measures this cross-lingual case explicitly.
- `asr.py` belongs to the Parakeet session, so the engine choice is built there or coordinated with it.

## 10. API

| Route | Purpose |
|---|---|
| `GET /api/media/{id}` | Stream a stored file for playback. Starlette 1.6 `FileResponse` handles `Range`. Only files registered in `media` are served (no path input, so no path traversal) |
| `GET /api/media/{id}/thumb` | Image thumbnail |
| `GET /api/search?q=&modality=` | Hits now include audio moments and images |
| `POST /api/chat` | Adds `media: bool` and `focus: {media_id, t_start, t_end}` |
| `GET /api/meetings/{id}` | Adds the meeting's media, for the player |
| `DELETE /api/media?kind=recording` | Delete all stored meeting audio |
| `POST /api/media/{id}/reindex` | Re-embed one item |

New preferences in `runtime.py`: `keep_meeting_audio`, `audio_retention_days`, `media_search`. Per-folder switches for audio
files and images are new `folders` columns. Every route that changes something keeps the `X-Foresight` header and Host checks.

## 11. UI

- **Meeting page:** a player bar for each channel. Clicking a transcript line seeks there, and the line playing is highlighted.
- **Citations:** ▶ chips play an audio moment inline from `t_start` to `t_end`. Image chips show a thumbnail on hover.
- **Knowledge:** search results grouped by type. Audio and image sources appear in the table with status. A folder dialog
  offers "Include audio files" and "Include images".
- **Settings:** an "Audio and media memory" card with the keep-audio switch, retention, disk used, "Delete all stored audio",
  and whether the installed models support audio and vision.
- **Home:** no change. Briefings benefit automatically, because they use the same search.

## 12. Background work and resources

- **One media worker**, in priority order: meeting and imported audio, then Knowledge audio, then images. It pauses while a
  meeting is recording.
- **Ollama:** both models stay loaded (3.68 + 0.35 GiB measured), and embeddings use the same `keep_alive` as generation.
- **Startup check:** `/api/show` must list `audio` and `vision` for both models. The check embeds two different short clips and
  requires **different** vectors, which catches the silent "empty string" failure seen with the wrong request shape. If it
  fails, media features turn off with a message, and text features keep working.
- **Model changes:** `media_items.model` records the model, rows with other dimensions are skipped (as for text), and a
  re-index action rebuilds them.

## 13. Privacy and retention

- `keep_meeting_audio` is **off by default**. When on, the meeting page shows that audio is being saved on this PC, next to the
  existing reminder to tell participants.
- **Retention is the user's choice (decision 4):** Settings offers 7 days, 30 days, 90 days or Forever, with 30 days as the
  starting value. `expires_at` is set from it when a file is stored, and changing it updates existing files. A daily sweep
  deletes audio files past `expires_at`. The transcript and vectors stay, so search still finds the moment, and the player
  says the audio has expired.
- Deleting a meeting deletes its files, including imported recordings.
- Everything stays in `data_dir`. No new network requests.

## 14. Code layout

| File | Change |
|---|---|
| `foresight/media.py` (new) | `media` rows, paths, `AudioStore` (Ogg Opus via PyAV), WAV encoding, decoding slices, thumbnails, retention and orphan sweeps |
| `foresight/media_index.py` (new) | `MediaIndexer` worker: windows, embedding calls, `media_items`, progress events |
| `foresight/llm.py` | `embed_audio()`, `embed_images()`, `chat(..., media=[...])`, `capabilities()` |
| `foresight/retrieval.py` | `VectorIndex` over both tables, weighted RRF, `Hit.media` |
| `foresight/asr.py` | One `on_audio` hook in `LiveCapture` (coordinate with the Parakeet session) |
| `foresight/meetings.py` | Start and stop `AudioStore`; deleting a meeting purges its media |
| `foresight/server.py`, `db.py`, `runtime.py` | Routes, tables, preferences |
| `static/app.js`, `style.css` | Player, media chips, Settings card |
| `tests/test_core.py`, `tools/eval_media.py` | Tests and the evaluation script |

## 15. Phases (each one can ship on its own)

| Phase | Delivers | New model calls |
|---|---|---|
| **P0** Evaluation and capability check | `tools/eval_media.py`: questions against known clips (English and Hinglish), recall@5 and MRR for text-only vs fused search. Speech-engine comparison on Hinglish (section 9). Startup capability check | measurement only |
| **P1** Keep audio and play it back | Opt-in `AudioStore`, `media` rows, meeting player, retention, deletion fixes | none |
| **P2** Audio moments in search | `MediaIndexer` audio windows, `VectorIndex` fusion, ▶ citations, `modality` filter | audio embeddings |
| **P3** Gemma listens | "Ask about this moment", Chat media toggle, "Re-transcribe with Gemma" | Gemma audio |
| **P4** Images | Image sources, thumbnails, image embeddings, Gemma vision context | image embeddings, Gemma vision |
| **P5** Voice memos in Knowledge | Audio files in folders and uploads | speech recognition + audio embeddings |

## 16. Testing

- **Offline (`pytest`, faked embeddings):**
  - window builder boundaries and overlap
  - WAV encoding round trip
  - `AudioStore` writes a playable Ogg of the right length
  - fusion and one-card-per-moment merging, using a fake audio embedding built from a sidecar transcript so cross-modal
    matching works in tests
  - retention deletes only owned files
  - meeting deletion removes its media
  - the media route answers `206` to a `Range` request
  - the API guards
- **Integration (`-m ollama`, skipped when Ollama is not running):** today's probes as tests, covering the request shapes,
  "different clips give different vectors", cross-modal ordering, and Gemma listening.
- **Quality:** `tools/eval_media.py` on real recordings, before media weights become default.

## 17. Risks

1. **Undocumented request shape.** `{"audio": ...}` in `/api/embed` was verified on Ollama 0.40.5 only. A wrong shape fails
   silently. Mitigation: one function in `llm.py`, the startup check, the integration test, and a minimum Ollama version.
2. **Image cost.** 11–18 s per image here. Mitigation: opt-in, downscaling, background work.
3. **Audio might not beat the transcript.** Audio search may add little for clear English speech. P0 measures it, and audio
   starts at half weight.
4. **Keeping audio changes the privacy story.** Mitigation: off by default, retention, delete with the meeting, visible
   indicator.
5. **Parallel work.** `asr.py` belongs to the Parakeet session. Only the `on_audio` hook touches it.

## 18. Decisions (2026-10-18)

1. **Keep meeting audio: off by default.** Users switch it on in Settings.
2. **First build: P0–P2.** Evaluation and capability check, keeping audio with playback, audio moments in search.
   P3–P5 come later.
3. **Languages: English, Hindi and Hinglish.** P0 compares speech engines on real Hinglish audio, and live transcription gets a
   per-meeting language choice (section 9).
4. **Retention: a user setting** (7 / 30 / 90 days / Forever, starting at 30 days; section 13).

Needed before P0 can measure Hinglish: a few minutes of real Hinglish meeting audio, recorded with everyone's
consent, plus 10–20 questions you would ask about it.

## Sources

- Google AI Edge, "Bring multimodal semantic search to the edge with EmbeddingGemma 2"
- EmbeddingGemma 2 developer guide and model card
- Ollama v0.33.3 release notes on Gemma 4 audio: <https://prismix.dev/news/063e7b21df0d>
- Skilltitute, "Google's AI Edge Foresight: An AI Meeting Assistant That Runs Locally?":
  <https://youtu.be/AuraLVqfULA>
- Measurements: probe scripts run on the development PC on 2026-10-18 (not included)
