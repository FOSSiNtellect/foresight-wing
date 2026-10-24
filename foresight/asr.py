"""Audio capture (microphone + WASAPI system loopback) and local speech-to-text.

A multiplexed speech stream: two independent sources, each cut into
utterances by a voice-activity detector, transcribed by one on-device model
(NVIDIA Parakeet TDT 0.6B v3 by default, faster-whisper as an alternative).
"""
from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from .config import settings

log = logging.getLogger(__name__)

SR = 16000
FRAME = 480                 # 30 ms
PRE_ROLL_FRAMES = 10        # keep 300 ms before speech onset
END_SILENCE_FRAMES = 23     # ~700 ms of silence closes an utterance
MAX_UTTERANCE_S = 15.0
MIN_UTTERANCE_S = 0.6

# Whisper's well-known phantom outputs on silence or music.
_HALLUCINATIONS = re.compile(
    r"^(thanks? (you )?(so much )?for watching|subtitles? by|please subscribe|thank you\.?|you|\.+|bye\.?)$", re.I)


# ---------------------------------------------------------------- models

def decode_audio(path: Path) -> np.ndarray:
    """Decode any audio/video file to 16 kHz mono float32 (PyAV, shipped with faster-whisper)."""
    from faster_whisper.audio import decode_audio as _decode

    return _decode(str(path), sampling_rate=SR)


class ParakeetEngine:
    """NVIDIA Parakeet TDT 0.6B v3 through onnx-asr: fast on CPU, punctuated, 25 European languages."""

    def __init__(self) -> None:
        import onnx_asr

        log.info("loading %s (%s) on CPU", settings.parakeet_model, settings.parakeet_quantization or "fp32")
        self.model = onnx_asr.load_model(settings.parakeet_model, quantization=settings.parakeet_quantization,
                                         providers=["CPUExecutionProvider"])
        self.lock = threading.Lock()
        # ONNX Runtime optimises lazily; one dummy pass keeps the first real utterance from lagging.
        self.model.recognize(np.random.default_rng(0).normal(0, 0.01, SR).astype(np.float32), sample_rate=SR)

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> str:
        # Parakeet has no prompt conditioning; `prompt` is accepted for interface parity.
        if len(audio) < SR * 0.3:
            return ""
        with self.lock:
            text = self.model.recognize(audio.astype(np.float32, copy=False), sample_rate=SR)
        return (text or "").strip()

    def transcribe_file(self, path: Path) -> Iterator[tuple[float, float, str]]:
        # The same VAD as live capture cuts the recording into utterances of at most 15 s.
        audio = decode_audio(path)
        segmenter = Segmenter("Recording")
        step = SR * 10
        for i in range(0, len(audio), step):
            for utt in segmenter.feed(audio[i:i + step]):
                yield from self._emit(utt)
        tail = segmenter.flush()
        if tail:
            yield from self._emit(tail)

    def _emit(self, utt: "Utterance") -> Iterator[tuple[float, float, str]]:
        text = self.transcribe(utt.audio)
        if text:
            yield utt.t_start, utt.t_start + len(utt.audio) / SR, text


class Transcriber:
    """The process-wide speech engine selected by FORESIGHT_ASR_ENGINE (default: parakeet)."""

    _instance: "ParakeetEngine | WhisperEngine | None" = None
    _instance_lock = threading.Lock()

    _hindi: "WhisperEngine | None" = None

    @classmethod
    def get(cls) -> "ParakeetEngine | WhisperEngine":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = WhisperEngine() if settings.asr_engine == "whisper" else ParakeetEngine()
            return cls._instance

    @classmethod
    def for_language(cls, language: str = "en") -> "ParakeetEngine | WhisperEngine":
        """The engine for a meeting's language. English uses the configured engine (Parakeet by default).
        Hindi / Hinglish ("hi") uses Whisper forced to Hindi, because Parakeet v3 covers European languages only;
        Gemma re-transcribes those meetings afterwards (repass.py)."""
        if language != "hi":
            return cls.get()
        with cls._instance_lock:
            if cls._hindi is None:
                cls._hindi = WhisperEngine(language="hi")
            return cls._hindi


class WhisperEngine:
    """faster-whisper fallback (FORESIGHT_ASR_ENGINE=whisper)."""

    def __init__(self, language: str | None = None) -> None:
        from faster_whisper import WhisperModel

        self.language = language or settings.whisper_language  # None: detect per utterance
        log.info("loading whisper model %s (%s/%s, language %s)", settings.whisper_model, settings.whisper_device,
                 settings.whisper_compute, self.language or "auto")
        self.model = WhisperModel(settings.whisper_model, device=settings.whisper_device,
                                  compute_type=settings.whisper_compute, local_files_only=settings.whisper_local_only)
        self.lock = threading.Lock()

    @staticmethod
    def _keep(seg) -> bool:
        text = seg.text.strip()
        if not text or _HALLUCINATIONS.match(text):
            return False
        return not (seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0)

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> str:
        with self.lock:
            segments, _ = self.model.transcribe(
                audio, language=self.language, beam_size=1, vad_filter=True,
                condition_on_previous_text=False, initial_prompt=prompt or None,
                # One decoding per utterance. faster-whisper's default retries at up to 6 temperatures when output looks
                # bad, which on noisy audio (music under speech) made live Hindi transcription ~10x slower than real time.
                temperature=0.0)
            return " ".join(s.text.strip() for s in segments if self._keep(s)).strip()

    def transcribe_file(self, path: Path) -> Iterator[tuple[float, float, str]]:
        with self.lock:
            segments, _ = self.model.transcribe(
                str(path), language=self.language, beam_size=1, vad_filter=True)
            for s in segments:
                if self._keep(s):
                    yield s.start, s.end, s.text.strip()


# ---------------------------------------------------------------- voice activity

@dataclass
class Utterance:
    speaker: str
    t_start: float
    audio: np.ndarray


class Segmenter:
    """Adaptive-energy VAD that turns a continuous stream into utterances."""

    def __init__(self, speaker: str) -> None:
        self.speaker = speaker
        self.noise = 0.003
        self.buf = np.zeros(0, dtype=np.float32)
        self.pre: list[np.ndarray] = []
        self.speech: list[np.ndarray] = []
        self.silent = 0
        self.voiced = 0
        self.samples_seen = 0
        self.start_sample = 0
        self.level = 0.0

    def feed(self, audio: np.ndarray) -> list[Utterance]:
        out: list[Utterance] = []
        self.buf = np.concatenate([self.buf, audio.astype(np.float32, copy=False)])
        while len(self.buf) >= FRAME:
            frame, self.buf = self.buf[:FRAME], self.buf[FRAME:]
            self.samples_seen += FRAME
            rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
            self.level = 0.8 * self.level + 0.2 * rms
            threshold = max(0.006, self.noise * 3.0)
            loud = rms > threshold
            if not self.speech:
                self.noise = 0.995 * self.noise + 0.005 * min(rms, 0.05) if not loud else self.noise
                self.pre.append(frame)
                if len(self.pre) > PRE_ROLL_FRAMES:
                    self.pre.pop(0)
                self.voiced = self.voiced + 1 if loud else 0
                if self.voiced >= 3:
                    self.speech = list(self.pre)
                    self.start_sample = self.samples_seen - FRAME * len(self.pre)
                    self.pre, self.silent = [], 0
                continue
            self.speech.append(frame)
            self.silent = 0 if loud else self.silent + 1
            duration = len(self.speech) * FRAME / SR
            if self.silent >= END_SILENCE_FRAMES or duration >= MAX_UTTERANCE_S:
                utt = self._close()
                if utt:
                    out.append(utt)
        return out

    def _close(self) -> Utterance | None:
        audio = np.concatenate(self.speech) if self.speech else np.zeros(0, dtype=np.float32)
        t0 = self.start_sample / SR
        self.speech, self.silent, self.voiced = [], 0, 0
        if len(audio) / SR < MIN_UTTERANCE_S:
            return None
        return Utterance(self.speaker, t0, audio)

    def flush(self) -> Utterance | None:
        return self._close() if self.speech else None


# ---------------------------------------------------------------- live capture

_soundcard_lock = threading.Lock()


def _com_init() -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED; harmless if already set


def _soundcard():
    """Return the soundcard module with COM initialised on the calling thread.

    The import must come first: soundcard's import calls CoInitializeEx itself and treats
    S_FALSE ("already initialised on this thread") as fatal."""
    with _soundcard_lock:
        import soundcard
    _com_init()
    warnings.filterwarnings("ignore", module="soundcard")
    return soundcard


def list_devices() -> dict:
    try:
        sc = _soundcard()
        return {
            "microphone": sc.default_microphone().name,
            "speaker": sc.default_speaker().name,
            "microphones": [m.name for m in sc.all_microphones()],
        }
    except Exception as exc:
        return {"error": str(exc)}


SegmentCallback = Callable[[str, float, float, str], None]


class LiveCapture:
    """Records the selected sources and emits (speaker, t_start, t_end, text)."""

    def __init__(self, on_segment: SegmentCallback, *, mic: bool = True, system: bool = True,
                 on_level: Callable[[dict], None] | None = None,
                 on_audio: Callable[[str, np.ndarray], None] | None = None, language: str = "en") -> None:
        self.on_segment = on_segment
        self.on_level = on_level
        self.on_audio = on_audio  # receives every captured block (media.AudioStore), so saved audio matches transcript times
        self.language = language  # "en" or "hi" (Hindi / Hinglish): picks the engine, see Transcriber.for_language
        self.sources = [s for s, on in (("You", mic), ("Others", system)) if on]
        self.stop_event = threading.Event()
        self.utterances: "queue.Queue[Utterance | None]" = queue.Queue()
        self.segmenters = {s: Segmenter(s) for s in self.sources}
        self.threads: list[threading.Thread] = []
        self.errors: dict[str, str] = {}
        self.last_text: dict[str, str] = {}
        self.t0 = 0.0

    @property
    def backlog(self) -> int:
        return self.utterances.qsize()

    def start(self) -> None:
        Transcriber.for_language(self.language)  # load the model up front so the first utterance isn't delayed
        self.t0 = time.time()
        for speaker in self.sources:
            t = threading.Thread(target=self._capture, args=(speaker,), name=f"capture-{speaker}", daemon=True)
            t.start()
            self.threads.append(t)
        worker = threading.Thread(target=self._transcribe_loop, name="transcribe", daemon=True)
        worker.start()
        self.threads.append(worker)
        if self.on_level:
            threading.Thread(target=self._level_loop, name="levels", daemon=True).start()

    def stop(self, *, transcribe_backlog: bool = True) -> int:
        """Stop capturing. Speech still queued is transcribed first (for up to 2 minutes), unless transcribe_backlog
        is False: then it is dropped, for a later pass over the saved audio (repass.py) to pick up.
        Returns how many queued utterances were dropped."""
        self.stop_event.set()
        for t in self.threads[:-1]:
            t.join(timeout=3)
        for seg in self.segmenters.values():
            utt = seg.flush()
            if utt:
                self.utterances.put(utt)
        dropped = 0
        while not transcribe_backlog:
            try:
                dropped += self.utterances.get_nowait() is not None
            except queue.Empty:
                break
        self.utterances.put(None)
        if self.threads:
            self.threads[-1].join(timeout=120)  # let queued speech (or the utterance in progress) finish transcribing
        return dropped

    def _capture(self, speaker: str) -> None:
        try:
            sc = _soundcard()
            if speaker == "You":
                device = sc.default_microphone()
            else:
                device = sc.get_microphone(id=str(sc.default_speaker().name), include_loopback=True)
            segmenter = self.segmenters[speaker]
            with device.recorder(samplerate=SR, channels=1, blocksize=FRAME * 4) as rec:
                while not self.stop_event.is_set():
                    data = rec.record(numframes=FRAME * 8)
                    mono = data[:, 0] if data.ndim == 2 else data
                    if self.on_audio is not None:
                        self.on_audio(speaker, mono)
                    for utt in segmenter.feed(mono):
                        self.utterances.put(utt)
        except Exception as exc:
            log.exception("%s capture failed", speaker)
            self.errors[speaker] = str(exc)

    def _transcribe_loop(self) -> None:
        model = Transcriber.for_language(self.language)
        while True:
            utt = self.utterances.get()
            if utt is None:
                return
            try:
                text = model.transcribe(utt.audio, prompt=self.last_text.get(utt.speaker, "")[-200:])
            except Exception:
                log.exception("transcription failed")
                continue
            if text:
                self.last_text[utt.speaker] = (self.last_text.get(utt.speaker, "") + " " + text)[-400:]
                self.on_segment(utt.speaker, utt.t_start, utt.t_start + len(utt.audio) / SR, text)

    def _level_loop(self) -> None:
        while not self.stop_event.wait(0.25):
            self.on_level({s: round(min(1.0, seg.level * 12), 3) for s, seg in self.segmenters.items()} |
                          {"backlog": self.backlog, "errors": self.errors})
