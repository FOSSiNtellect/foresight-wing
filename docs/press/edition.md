<!--
  THE COPY DESK: every word printed in the README, in plain Markdown.

  docs/press/press.py sets this as newspaper pages (docs/readme/*.svg) and also
  pastes it, as is, into the README's plain-text edition. Page markers:

    {{name}}             a special page from edition.toml: front, newsreel, architecture, pipelines, classifieds, colophon
    # TITLE | subtitle   a section banner
    ## Headline          a new article page; a "> " line right after it is the deck
    ### / ####           headings inside the article

  Inside an article: paragraphs, "- " lists (two levels), "1. " lists, tables and ``` code.
  **bold**, *italic*, `code` and [links](url) work inside text. Tables and long code run the full width.
-->

{{front}}

# THE NEWSREEL | Thirty-two seconds with the real app, on a real PC

{{newsreel}}

# BEFORE YOU BEGIN | Everything you need, and nothing you don’t

## The checklist
> What Foresight Wing needs on your PC, from Windows itself to the last speech weight

### To use it

| You need | Details |
|---|---|
| **Windows 10 or 11, 64-bit** | Developed and tested on Windows 11. System audio is captured through WASAPI loopback and the files key is protected by Windows DPAPI, so Windows is required. |
| **A 64-bit (x64) CPU** | Everything runs on the CPU, and no GPU is needed. Ollama uses a supported GPU on its own if you have one. |
| **8 GB of RAM or more** | 16 GB is comfortable. Gemma 4 E2B (a 4.3 GB download) stays loaded for 30 minutes after it was last used (`FORESIGHT_KEEP_ALIVE`). The larger `gemma4:e4b-it-qat` wants 16 GB free. |
| **About 10 GB of free disk** | The installed app (1.5 GB, speech models included), Gemma 4 E2B (4.3 GB), EmbeddingGemma 2 (1.3 GB), Ollama itself, and room for your data. Kept meeting audio adds about 14 MB per hour per source. |
| **Ollama** | Serves the two Gemma models on `127.0.0.1:11434`. *Set up now* inside the app installs it for you, only if Windows confirms the installer is signed by Ollama Inc., or get it from [ollama.com](https://ollama.com/download/windows). Developed with Ollama 0.40.10; older versions may lack Gemma 4’s audio input. |
| **Microsoft Edge** | Ships with Windows. The app opens in Edge’s app mode, with its own profile and with extensions and sync turned off. Without Edge, your default browser opens it instead. |
| **A microphone and an output device** | The microphone records your side (“You”); the speakers or headset you hear the call on are recorded through loopback (“Others”). Either one can be switched off. |
| **Internet, once** | To download Ollama and the two models, about 6.6 GB in all. After that everything works offline. |
| **An account** | None. There is no sign-in, no API key and no subscription. |

### To run from source

Everything above, plus:

| You need | Details |
|---|---|
| **Python 3.10 or newer** | Developed and tested on Python 3.13. `setup.bat` checks it and says how to install it, from [python.org](https://www.python.org/downloads/windows/) or with `winget install --id Python.Python.3.12 -e`. |
| **Git** | To clone this repository. |
| **The Python packages** | From `requirements.txt`, installed by `setup.bat` or `pip install -r requirements.txt`. Web server: `fastapi`, `uvicorn`, `python-multipart`, `httpx`. Speech and audio: `onnx-asr[cpu,hub]`, `faster-whisper`, `soundcard`, `av`, `numpy`. Documents: `pypdf`, `python-docx`, `openpyxl`, `beautifulsoup4`, `lxml`. The app: `Pillow`, `cryptography`, `pystray`, `tzdata`. |
| **The two Ollama models** | `ollama pull gemma4:e2b-it-qat` (about 4.3 GB) and `ollama pull embeddinggemma-2:740m` (about 1.3 GB). `setup.bat` offers to pull them. |
| **The speech weights** | From Hugging Face into your local cache: `istupakov/parakeet-tdt-0.6b-v3-onnx` (about 670 MB; only the int8 files are used) and, for Hindi / Hinglish meetings, `Systran/faster-whisper-small` (about 480 MB). `setup.bat` offers to download them. The app itself never downloads them (`run.py` sets `HF_HUB_OFFLINE=1`). |
| **pytest** | Only to run the tests: `pip install pytest`. |

### To build the installer

Everything for running from source, plus:

| You need | Details |
|---|---|
| **PyInstaller** | `pip install pyinstaller`. Built and tested with PyInstaller 6.21. |
| **The speech weights in your cache** | The build copies them into the app, so the installed app needs no download for speech. |
| **About 4 GB free in `%LOCALAPPDATA%`** | The build writes to `%LOCALAPPDATA%\ForesightBuild`: the app folder (1.46 GB), the installer (1.11 GB) and work files. It stays outside the project folder so a synced folder such as OneDrive does not upload it. |
| **4 to 8 minutes** | On the development PC a full build took 4 to 6 minutes. |

# THE FEATURES DESK | What it does, and where to find it

## At the meeting
> It hears both sides of the call, answers what is asked, and writes the notes afterwards

#### Shorthand into polished notes
Scribble shorthand during the meeting, then press *Auto-enhance* (Meeting → Notes). A map-reduce over long transcripts turns your shorthand and the transcript into a Summary, Decisions and Action items.

#### Calls and in-person meetings
It captures the **microphone** (“You”) and the **system audio** through WASAPI loopback (“Others”), or both.

#### English and Hindi / Hinglish
Pick the language when you start or import a meeting. English uses Parakeet live. Hindi / Hinglish uses Whisper `small` live as a preview; afterwards Gemma 4 listens to the saved audio again, rewrites every line in Latin-letter Hinglish and adds any speech the live pass missed. Replaced wording is kept, so changes can be undone.

#### Live Assistance
A question detector watches the transcript, searches your files and shows a proactive card with citations, while the meeting is still going.

#### Floating live answers
*Float* on a recording meeting opens a small window that stays on top of your call, with the live answers, the last two lines said and an Ask box (Document Picture-in-Picture in Chrome and Edge).

#### Meeting import
Import an audio recording (wav, mp3, m4a and more) to transcribe it into a meeting.

#### Daily Briefings
Home → *Upcoming*: for every event in the next 24 hours, the most relevant sources and similar past meetings, plus a three-bullet “what to know” from Gemma, cited and cached until the event changes. *Start this meeting* opens it with the event’s title.

#### Calendars without a sign-in
Settings → *Calendars*: local `.ics` files or subscription links (`https://` or `webcal://`), re-synced every 15 minutes. Daily and weekly repeats, exceptions, moved instances and time zones are expanded with the standard library.

#### Replay what was said
Settings → *Audio memory*, off by default, keeps meeting audio as Ogg Opus (about 14 MB per hour per source). Click a transcript line to hear it. It is kept for 7, 30 or 90 days, or until you delete it.

#### Gemma listens
Gemma 4 hears kept audio when you ask it to: ask about one transcript line or a cited moment, or tick *Listen to cited audio* in Chat. It first transcribes each clip on its own, so speech-recognition slips such as “€400” for “four thousand euros” never reach the answer, and it can suggest a better transcription for any line.

## In your knowledge
> Point it at your files, and every answer comes back with its sources

#### Folders, documents and web pages
Knowledge → local folders (re-scanned every 10 minutes), uploads (PDF, DOCX, PPTX, XLSX, ODT, RTF, EPUB, HTML, MD, TXT, CSV, JSON) and web pages saved once and indexed locally.

#### Answers with citations
Chat across all your knowledge or one label. Gemma re-ranks the best candidates against the question before answering, on by default, for about 6 seconds more per answer.

#### Date questions
“What did we decide in last Tuesday’s meeting?” searches only the meetings recorded then. It understands today, yesterday, this morning, weekdays, last week or month, “3 days ago”, dates like 12 October and the Hinglish forms (*kal*, *parso*, *pichle hafte*).

#### The document graph
Sources → *Graph*: every source is a dot, joined to the sources whose content is most alike, with labels as hubs. Click a dot for its closest neighbours, or find one by name.

#### Labels into a wiki
Tag sources with a label, then *Build wiki page*. It lists the contradictions it finds, such as a $40k versus $52k budget.

#### Search the audio itself
Kept audio is cut into moments of about 20 seconds and embedded by EmbeddingGemma 2 straight from the sound. A question finds the moment it was said, even past a transcription error, and its citation plays just that moment.

#### Pictures
Photos, screenshots, charts and scanned receipts in folders and uploads. Gemma 4 describes each one and copies any text in it, and EmbeddingGemma 2 embeds the picture itself, so either words or a description of its look find it. Gemma looks at up to two cited pictures when it answers.

#### Voice memos
Audio files in folders and uploads are transcribed locally into timestamped lines and embedded from the sound. A citation plays the moment straight from your own file, which is never copied or changed.

#### Encrypted at rest
Meeting audio, recordings, uploads and thumbnails are AES-256-GCM encrypted with a key that only your Windows account can unwrap.

#### Token stats
Home shows an on-device usage chart: how many tokens each task used, with no cloud calls.

#### Not included
Cloud sign-in, Drive or calendar account sync (calendars come from `.ics` files or subscription links instead), and telemetry.

# GETTING YOUR COPY | Build the installer, or run it from source

## Two ways in
> There is no prebuilt download yet: run it straight from source, or build the one-file installer yourself

### Run it from source

Clone the repository:

```
git clone https://github.com/FOSSiNtellect/foresight-wing.git
```

Then, in the `foresight-wing` folder, double-click `setup.bat`. It checks what is ready and fixes what is missing, asking before every install or download:

1. Python packages, with `pip install -r requirements.txt`. Python 3.10 or newer must already be installed; `setup.bat` says how.
2. Ollama: it installs it with winget if you agree, or points to the download page, and starts it.
3. The models `gemma4:e2b-it-qat` and `embeddinggemma-2:740m`, through `ollama pull`.
4. The speech weights from Hugging Face: Parakeet TDT 0.6B v3 and, for Hindi / Hinglish meetings, Whisper small.
5. Optionally, a Start-menu entry that opens Foresight Wing with a tray icon.

`setup.bat --check` only reports; `setup.bat --yes` answers yes to everything. Then start it from the Start-menu entry, or with:

```
python run.py --tray
```

It opens `http://127.0.0.1:8765/` in its own window and puts the Foresight Wing orb in the notification area (*Open Foresight Wing*, *Quit*). Without `--tray` it runs in the console instead. Your data lives in `%LOCALAPPDATA%\ForesightWin`.

If something is missing, such as Ollama stopped or a model not pulled, Home shows *Finish setting up* with the fix for each item.

### Build the installer

With the source set up as above, run:

```
python packaging/build.py
```

It writes `ForesightWingSetup.exe`, about 1.1 GB, to `%LOCALAPPDATA%\ForesightBuild\dist`. Copy it to any Windows 10 or 11 PC and run it:

- It installs Foresight Wing for that Windows account only, with no admin rights, in `%LOCALAPPDATA%\Programs\ForesightWing`, with a Start-menu entry and an entry in Settings → Apps.
- It opens Foresight Wing in its own window. Closing the window keeps it in the notification area, so a recording is never cut off; right-click its icon and choose Quit to stop it.
- Speech recognition ships inside. The first time, Home shows *Set up now*: it installs Ollama from ollama.com, only if Windows confirms the installer is signed by Ollama Inc., then pulls the two models, about 6.6 GB, once.
- To update, quit Foresight Wing and run a newer installer; your data stays. To uninstall, use Settings → Apps → Foresight Wing, which asks whether to delete your data too.

## The settings
> Every setting can be changed with a FORESIGHT_ environment variable

| Variable | Default | What it does |
|---|---|---|
| `FORESIGHT_GEN_MODEL` | `gemma4:e2b-it-qat` | Use `gemma4:e4b-it-qat` with 16 GB of free RAM or more |
| `FORESIGHT_EMBED_MODEL` | `embeddinggemma-2:740m` | `embeddinggemma-2:270m` is lighter (re-index after changing) |
| `FORESIGHT_ASR_ENGINE` | `parakeet` | `whisper` switches to faster-whisper |
| `FORESIGHT_PARAKEET_MODEL` | `nemo-parakeet-tdt-0.6b-v3` | `nemo-parakeet-tdt-0.6b-v2` is English-only (download its ONNX first) |
| `FORESIGHT_PARAKEET_QUANTIZATION` | `int8` | Empty means fp32: slower, marginally more accurate |
| `FORESIGHT_WHISPER_MODEL` | `small` | Whisper engine only: `base.en` is faster, `large-v3-turbo` more accurate |
| `FORESIGHT_WHISPER_DEVICE` | `cpu` | Whisper engine only: `cuda` if cuBLAS and cuDNN are installed |
| `FORESIGHT_MEDIA_WEIGHT` | `0.5` | Weight of audio moments in search |
| `FORESIGHT_IMAGE_WEIGHT` | `1.0` | Weight of pictures found by their look |
| `FORESIGHT_IMAGE_MIN_SIMILARITY` | `0.65` | The similarity a picture needs to count at all |
| `FORESIGHT_ENCRYPT_FILES` | `1` | `0` keeps new audio, uploads and thumbnails unencrypted |
| `FORESIGHT_NUM_CTX` | `8192` | Gemma’s context window |
| `FORESIGHT_KEEP_ALIVE` | `30m` | How long Ollama keeps Gemma loaded after the last request |
| `FORESIGHT_PORT` | `8765` | The local port of the app |
| `FORESIGHT_DATA_DIR` | `%LOCALAPPDATA%\ForesightWin` | Where your data lives |

# INSIDE THE PRESS ROOM | How a meeting becomes notes, and a question becomes a cited answer

{{architecture}}

{{pipelines}}

## Three layers, five models
> One Python process on one PC, a browser window to show it, and models that never leave the machine

Foresight Wing is a private meeting companion and second brain. Everything runs on one Windows PC: on-device models, one local web server on `127.0.0.1`, and one SQLite database. Nothing is sent anywhere unless you add a web link or a calendar link yourself, or press *Set up now* to download Ollama and the models.

### The three layers

1. **The window.** The UI is a dependency-free web app (`foresight/static/`) with no build step and no CDN, shown in Microsoft Edge’s app mode: no tabs, no address bar, its own profile, and extensions and sync turned off.
2. **The app.** One Python process (`run.py`: FastAPI and uvicorn on `127.0.0.1:8765`) runs the server and a handful of background workers, and pushes live events to the window over one WebSocket, `/ws`. Long answers stream as NDJSON. Installed, it is `ForesightWing.exe` with a notification-area icon.
3. **The models.** Ollama serves Gemma 4 and EmbeddingGemma 2 on `127.0.0.1:11434`. ONNX Runtime and CTranslate2 run the speech models inside the app, on the CPU.

### The models

| Role | Model | Size | Runs in | Measured on the development PC |
|---|---|---|---|---|
| Speech to text, English and 24 more European languages | NVIDIA Parakeet TDT 0.6B v3, int8 ONNX | 670 MB | ONNX Runtime, CPU | 6.9× faster than real time; 5.6% word errors on the English test set |
| Hindi / Hinglish live preview | Whisper small (faster-whisper, int8) | 480 MB | CTranslate2, CPU | keeps up live |
| Meaning of text, sound and pictures, in one 768-dimension space | EmbeddingGemma 2 (740M) | 1.3 GB | Ollama | 1.1 to 1.4 s per 6 to 8 s audio clip, warm |
| Answers, notes, re-ranking, listening and looking | Gemma 4 E2B (QAT) | 4.3 GB | Ollama | answers that first listen to cited clips: 9 to 19 s |
| Speech or not, the gate for the Hindi pass | Silero VAD (ships with faster-whisper) | 2 MB | ONNX Runtime | stops jingles and music being “transcribed” |

**Why two speech engines, and Gemma too.** Parakeet is fast and accurate for English but has no Hindi. Whisper small gives a quick live preview in Hindi, and after the meeting Gemma 4 re-transcribes every line from the saved audio. Gemma also re-checks single clips when it answers.

**Why EmbeddingGemma 2 matters.** It embeds text, audio and images into the same vector space. A typed question can find a moment in a recording by its *sound*, or a picture by how it *looks*, even when the transcript or the description missed it.

## The workers and the archive
> What runs in the background, and where every byte of your data is kept

### The process and its workers

| Worker | File | What it does |
|---|---|---|
| Indexer queue | `indexer.py` | Parses, chunks and embeds sources one at a time. Pictures and audio files wait while a meeting records. |
| Folder rescanner | `indexer.py` | Rescans watched folders every 10 minutes, adding new or changed files and removing deleted ones. |
| Live capture | `asr.py` | One thread per audio source captures 16 kHz mono blocks; a transcription thread drains the queue. |
| Audio indexer | `media_index.py` | Cuts kept audio into moments of about 20 s and embeds them. It pauses during meetings and resumes after a restart. |
| Hindi pass | `repass.py` | After a Hindi / Hinglish meeting, Gemma re-transcribes every line from the saved audio. |
| Briefings | `briefings.py`, `calendars.py` | Syncs `.ics` calendars every 15 minutes and pre-writes a cited briefing for each upcoming event. |
| Media sweep | `media.py` | Every 6 hours applies audio retention, removes orphaned files, and encrypts files saved before encryption existed. |
| Setup job | `provision.py` | Only when you press *Set up now*: installs Ollama and pulls the models, with progress. |

### Where your data lives

```
%LOCALAPPDATA%\ForesightWin\   all of your data (the app itself is in %LOCALAPPDATA%\Programs\ForesightWing)
  foresight.db (+ -wal)    SQLite: everything below; not encrypted
  files.key                the files key, wrapped by Windows DPAPI for your account
  media\meeting-N\*.ogg    kept meeting audio (Ogg Opus, 32 kbit/s), encrypted
  recordings\              imported recordings, encrypted
  uploads\                 uploaded files and saved web PDFs, encrypted
  thumbs\                  picture thumbnails (320 px), encrypted
  window\                  the app window's own Edge profile
  preferences.json         settings
  foresight.log            the log, when started without a console
```

### The SQLite tables

- **Knowledge:** `sources` (one row per document, page, picture, memo, transcript or notes), `chunks` (about 1,200 characters each, 200 overlapping, with a float32 vector), `chunks_fts` (an FTS5 BM25 index), `folders`, `labels`, `source_labels`.
- **Meetings:** `meetings` (with a language, en or hi), `segments` (transcript lines with timestamps and speaker You / Others), `insights` (live answer cards), `segment_versions` (undo for edited or Gemma-replaced lines), `transcript_passes` (Hindi pass progress).
- **Media:** `media` (recordings, kept audio, pictures and memo files, with owner and retention), `media_items` (audio moments and picture vectors, each tagged with its embedding model), `media_index` (resumable progress).
- **Chat, calendar and usage:** `chat_messages`, `calendars`, `events`, `briefings`, and `usage`, the token counts per task shown on Home.

## From the first word to the final notes
> How a meeting, a Hinglish call, a document and a voice memo each find their way into memory

### A live meeting

1. **Capture.** `LiveCapture` opens the microphone (“You”) and WASAPI loopback of the speakers (“Others”) with `soundcard`, and feeds 16 kHz blocks to an energy-based voice activity detector (`Segmenter`).
2. **Transcribe.** Each utterance, cut at a pause or at 15 s, goes to the meeting’s engine: Parakeet for English, Whisper (Hindi forced, `temperature=0`) for Hindi / Hinglish. Lines are saved and pushed to the window as they arrive.
3. **Live answers.** `assistant.py` watches the transcript for questions, searches your knowledge and lets Gemma answer from what it found. If nothing is relevant enough (`live_min_score`, 0.30) it stays quiet; otherwise the answer appears as a cited card.
4. **Audio, if you opt in.** With *Keep meeting audio* on, each source is encoded to Ogg Opus on its own thread. PyAV writes straight into the encrypting writer, so plain audio never touches the disk, and gaps are written as silence so audio and transcript stay aligned.
5. **Afterwards.** The transcript and your shorthand become sources, indexed like documents. *Auto-enhance* turns them into polished notes with a map-reduce over long transcripts.

### Hindi / Hinglish: Gemma’s second pass

Live Whisper text on noisy audio is rough, so `repass.py` rewrites the meeting afterwards:

1. **Fill the gaps.** It finds speech the live detector missed and adds lines for it.
2. **Drop noise.** Lines with less than 0.3 s of real speech, by Silero VAD, are removed, which stops jingles being “transcribed”.
3. **Re-transcribe.** For every line, Gemma 4 hears that clip and writes it in Latin-letter Hinglish.
4. **Filter.** Replies too long for the audio, chatbot-like replies and `NO_SPEECH` are rejected; Devanagari output is transliterated with a cheap text-only call.
5. **Keep undo.** Replaced wording goes into `segment_versions`. Audio kept only for this pass is deleted afterwards.

### Documents, web pages, pictures and memos

- **Documents.** PDF, DOCX, PPTX, XLSX, ODT, RTF, EPUB, HTML, Markdown and text are parsed to text, split into paragraph-aware chunks and embedded with EmbeddingGemma 2. Text that has not changed (same SHA-256) is never embedded again.
- **Web pages.** Fetched once, only for links you add, then indexed locally like documents.
- **Pictures** (`images.py`). Gemma 4 describes the picture and copies any text in it; that becomes the searchable text. EmbeddingGemma 2 also embeds the picture itself, and a thumbnail is kept. Pictures under 12 KB in watched folders are skipped as icons.
- **Voice memos** (`memos.py`). They are transcribed into `[mm:ss]` lines like meeting transcripts, and cut into moments that are embedded from the sound. The file stays where you keep it, and the app never copies or deletes it.

### Audio moments

`media_index.py` groups transcript lines into windows of about 20 s, at most 30 s, starting a new one after a pause of more than 5 s. It embeds the audio of each window with EmbeddingGemma 2 and stores the vector with that window’s text, so a search result can play exactly that moment.

## How an answer is found
> Four ranked lists, one fusion, one re-rank, and a Gemma that checks what it heard

### Search: four ranked lists, fused

For each question, `retrieval.search` builds up to four ranked lists:

| List | Scored by | Weight |
|---|---|---|
| Text vectors | cosine of the question’s vector with every chunk, in one matrix product over a cached float32 matrix | 1.0 |
| Keywords | SQLite FTS5 BM25 | 1.0 |
| Audio moments | cosine with the moments’ sound vectors | 0.5 |
| Pictures | cosine with the pictures’ vectors, only from 0.65 up | 1.0 |

The lists are fused with Reciprocal Rank Fusion, `weight / (60 + rank)`. Fusing by rank matters, because text-to-audio and text-to-image cosines sit on a different scale than text-to-text ones. The 0.65 picture floor comes from real screenshots: matching descriptions scored 0.68 to 0.79, unrelated ones 0.50 to 0.67, and without it the closest picture would join every answer.

- **Merging.** An audio moment inside a transcript chunk becomes that chunk’s play button, and a picture found by its look joins its description.
- **Diversity.** At most three results come from one source.
- **Filters.** Labels restrict the search to tagged sources. So do dates: “last Tuesday’s meeting”, *kal* or “12 October” become a time window (`dates.py`), and only the meetings recorded then are searched.
- **Re-ranking**, on by default. Gemma reads the top candidates against the question in one JSON call and scores each from 0 to 3. On the English test set it lifted recall@1 from 94% to 100% (MRR 0.956 to 1.000), for about 6 s per question. Live Assistance skips it for speed.

### The answer

`rag.answer_stream` sends Gemma 4 a system prompt (cite every fact as [n], say so when the sources don’t, flag conflicts) and the numbered blocks, then streams the reply.

- **Follow-ups** such as “and the budget?” are searched together with the previous question.
- **Listening.** With *Listen to cited audio*, or after you pick a moment, Gemma first transcribes each cited clip on its own, with numbers and amounts as said, and its words replace the transcript lines the clip covers. Asked together with the question, Gemma E2B tended to repeat the transcript’s mistake.
- **Looking.** Gemma looks at up to two cited pictures, because a description can miss what matters.
- **Live meetings.** The current meeting’s transcript is added as block [T].
- **Showing its work.** The window shows “Listening to the recordings…”, then “Heard in [1]: …”, then the answer with its source chips.

### More on the same pieces

- **Labels into a wiki.** A label’s sources are summarized, and cached, into a wiki page that calls out contradictions.
- **Daily briefings.** For each event in the next 24 hours, the most relevant sources, similar past meetings and a three-bullet “what to know”, cached until the event changes.
- **The document graph.** Each source sits at the mean of its chunk vectors. Links join sources clearly more alike than usual for the collection: above the mean plus half a standard deviation, and never below 0.5. Whole documents score 0.6 to 0.8 against almost anything, so a fixed cut-off would link everything.

## Locks, keys and the only doors out
> What stays on the PC, what is encrypted, and the three requests that ever leave it

- **Local only.** The server binds to `127.0.0.1`. Requests with a foreign `Host` header are refused, which blocks DNS rebinding, and WebSocket origins are checked. Every changing API call needs an `X-Foresight: 1` header, which forces a CORS preflight that is never granted, so other websites cannot post to it.
- **No accounts, no telemetry.** The only outbound requests are web links you add, calendar subscription links you add, and *Set up now*, only when you press it.
- **The app window** runs Edge with its own profile and with extensions and sync turned off. Edge signs even a new profile in with your Windows account and syncs that account’s extensions, which could otherwise read your transcripts and notes.
- **Files at rest** (`vault.py`):
  - **Encryption:** AES-256-GCM in 64 KiB chunks. Each chunk’s nonce is a random file prefix plus the chunk number, and its additional data marks the last chunk, so chunks cannot be reordered, swapped between files or silently cut off.
  - **Reading:** decryption happens in memory. PyAV decodes audio through a seekable decrypting reader, and the player’s byte-range requests are decrypted on the fly.
  - **The key:** random, made on first run and wrapped by Windows DPAPI for your account (`files.key`). There is no password, and only your Windows account on this PC can unwrap it; a copy of the data folder on another PC cannot be opened.
  - **Not encrypted:** the database (transcripts, notes, chunks and vectors) and the files in your own folders, which the app only reads.
- **Audio retention.** Keeping meeting audio is off by default. When it is on, audio is kept for 7, 30 or 90 days, or until you delete it; transcripts stay.
- **Installer safety.** *Set up now* downloads Ollama’s installer from ollama.com and runs it only if Windows’ Authenticode check says it is validly signed by Ollama Inc.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## From source to installer
> How the app is packed, where it lands, a map of the code and the honest limits

### Packaging

`python packaging/build.py` takes 4 to 8 minutes:

1. **Speech models.** It copies Parakeet’s int8 weights and Whisper small into a bundled Hugging Face cache folder (1.16 GB), skipping 3 GB of fp32 weights the app never loads. The app reads them offline (`HF_HUB_OFFLINE=1`).
2. **ForesightWing.exe.** PyInstaller builds a folder app, which starts fast. It leaves out `torch`, `transformers`, `pyarrow` and about 1 GB of CUDA libraries, because speech runs on the CPU. Result: 1.46 GB.
3. **ForesightWingSetup.exe.** PyInstaller wraps that folder into one file (1.11 GB) that shows a splash picture while it unpacks.

Installing takes well under a minute and needs no admin rights. Uninstalling runs `ForesightWing.exe --uninstall`, which asks whether to keep your data and removes the app folder once the app has exited. Nothing is added to Windows sign-in.

### The code map

```
run.py            start: server, tray icon, app window, single instance, --uninstall
foresight/
  server.py       FastAPI routes, WebSocket, security middleware, byte-range media
  config.py       settings (FORESIGHT_* variables)       runtime.py    preferences.json
  db.py           SQLite schema and migrations            events.py     WebSocket event bus
  llm.py          Ollama: chat, JSON, text / audio / image embeddings
  asr.py          capture, voice activity detection, Parakeet and Whisper
  meetings.py     sessions, import, Auto-enhance, action items, export
  assistant.py    question detection -> live cards
  indexer.py      discovery, parsing, chunking, embedding queue
  parsers.py      document formats      images.py  pictures      memos.py  audio files
  retrieval.py    hybrid search, RRF, merging, re-ranking
  rag.py          answers: citations, dates, listening, looking
  dates.py        "last Tuesday", "kal", "12 October" -> time windows
  media.py        kept audio: encoding, decoding, retention, sweep
  media_index.py  audio moments         listen.py  clips for Gemma
  repass.py       Gemma's Hindi / Hinglish pass
  briefings.py    calendars.py    wiki.py    graph.py
  vault.py        encryption at rest    doctor.py  first-run check
  provision.py    "Set up now"          install.py install / uninstall
  window.py       the app window (Edge app mode, no extensions)
  static/         index.html, app.js, style.css: the whole UI, no build step
packaging/        build.py, foresight.spec, installer.spec, installer.py
tools/            setup.py (setup from source), eval_media.py (accuracy)
tests/            50 offline tests
docs/             design notes; press/ prints this README
```

### Known limits

- **Windows only, for now.** The core is plain Python, but the installer, the files key (DPAPI), system-audio capture (WASAPI) and *Set up now* are Windows-specific; Linux and macOS are untested.
- **Hindi live text** is a rough preview until Gemma’s pass, and Hindi voice memos don’t get that pass yet.
- **Gemma mishears too.** Its own hearing can slip (“offsite” heard as “north site”); the answer shows what was heard so you can spot it.
- **The database is not encrypted.** Only the files the app keeps are.
- **Set up now** has not yet been tested on a PC without Ollama.

# LETTERS & CLASSIFIEDS | Tests, contributions, security and the fine print

## Tests, letters and the fine print
> How it is checked, how to help, and whose work it builds on

### Tests

```
python -m pytest tests -q
```

The 50 tests run offline: embeddings are faked with a bag-of-words hash, Gemma is faked in each test that needs it (a guard fails any test that reaches the real model), and speech engines and audio devices are faked. They cover search, answers, meetings, the Hindi pass, calendars and briefings, encryption and its tamper checks, the security headers, and installing and uninstalling.

`tools/eval_media.py` measures whether searching the audio itself helps and compares speech engines; it needs Ollama. Results so far are in [docs/multimodal-memory-design.md](docs/multimodal-memory-design.md).

`samples/` holds three fictional “Project Atlas” documents with a deliberate budget and date conflict, and `atlas_calendar.ics` with recurring Atlas meetings. Add the `samples` folder under Knowledge and the calendar under Settings → Calendars, then open Home.

### Contributing

Issues and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Security problems go to [SECURITY.md](SECURITY.md), not a public issue. This README is printed by `docs/press/press.py` from `docs/press/edition.md` and `docs/press/edition.toml`, in the same type as [The FOSSiNtellect](https://github.com/FOSSiNtellect); edit those, then run the press.

### Background

Foresight Wing is an independent, clean-room project inspired by Google AI Edge Foresight, an experimental local AI note-taker for macOS. It brings the same idea to Windows. It contains no Google code, assets or prompts, and it is not affiliated with or endorsed by Google.

### License

[MIT](LICENSE), © 2026 FOSSiNtellect. Gemma, EmbeddingGemma, Parakeet and Whisper are third-party models with their own licenses and terms; they are not in this repository, and the installer bundles the Parakeet and Whisper weights under their own licenses. The pages are set in Old Standard TT and Caveat, both under the SIL Open Font License (`docs/press/fonts/`).

{{classifieds}}

{{colophon}}
