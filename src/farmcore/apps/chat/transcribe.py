"""Local speech-to-text: voice notes are transcribed on this host and never leave it.

`WhisperTranscriber` runs faster-whisper on CPU (int8), loading `settings.whisper_model` on first
use. No vocabulary is injected into decoding: on silence the old prompt could be emitted as
speech, inventing parcels/products. VAD removes silence and each window decodes independently.
The hints argument remains compatible with callers; name resolution belongs to the extractor.
Noise that survives VAD can still decode as a stock caption ("Thanks for watching!"): a segment
Whisper itself rates as probable non-speech is dropped, and so is a whole transcript that is one
of the well-known caption hallucinations. Real speech that merely contains such words is kept.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from farmcore.apps.chat.extractor.base import fold
from farmcore.settings import Settings

# Whisper models take 16 kHz mono input.
WHISPER_SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    seconds: float  # audio duration


class Transcriber(Protocol):
    def transcribe(self, path: Path, hints: Sequence[str]) -> Transcript: ...


# The words the regex fallback and the model key on, in both demo languages.
SPRAY_VOCABULARY = (
    "copper, sulphur, cuivre, soufre, Bouillie bordelaise, 2 kg/ha, 1,5 L/ha, kilos, litres, "
    "this morning, yesterday, ce matin, hier"
)


def initial_prompt(hints: Sequence[str]) -> str:
    words = [h.strip() for h in hints if h.strip()]
    names = (" " + ", ".join(words) + ".") if words else ""
    return f"Vineyard spray log: {SPRAY_VOCABULARY}.{names}"


# Whisper's own non-speech heuristic (openai/whisper `transcribe`: no_speech_threshold 0.6 with
# logprob_threshold -1.0).
NO_SPEECH_PROB = 0.6
LOW_LOGPROB = -1.0
# Stock captions Whisper emits on silence or noise, matched on the whole folded transcript.
HALLUCINATIONS = frozenset(
    {
        "thanks for watching",
        "thank you for watching",
        "thank you",
        "thanks",
        "you",
        "bye",
        "sous titres realises par la communaute d amara org",
        "sous titrage st 501",
        "merci d avoir regarde cette video",
        "merci d avoir regarde",
        "merci",
    }
)


def _words(text: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in fold(text)).split())


def clean(segments: Sequence[Any]) -> str:
    """The spoken text: probable non-speech segments and stock caption hallucinations removed."""
    kept = [
        s.text.strip()
        for s in segments
        if not (
            getattr(s, "no_speech_prob", 0.0) > NO_SPEECH_PROB
            and getattr(s, "avg_logprob", 0.0) < LOW_LOGPROB
        )
    ]
    text = " ".join(t for t in kept if t).strip()
    return "" if _words(text) in HALLUCINATIONS else text


class WhisperTranscriber:
    DEVICE = "cpu"
    COMPUTE_TYPE = "int8"

    def __init__(self, settings: Settings, *, local_files_only: bool = False) -> None:
        self.model_name = settings.whisper_model
        self._local_files_only = local_files_only
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.model_name,
                    device=self.DEVICE,
                    compute_type=self.COMPUTE_TYPE,
                    local_files_only=self._local_files_only,
                )
            return self._model

    def transcribe(self, path: Path, hints: Sequence[str]) -> Transcript:
        audio = decode_audio(path)
        segments, info = self._load().transcribe(
            audio, condition_on_previous_text=False, vad_filter=True
        )
        text = clean(list(segments))
        return Transcript(text=text, language=info.language, seconds=float(info.duration))


def decode_audio(path: Path) -> Any:
    """Any audio file (Telegram voice is OGG/Opus) → mono 16 kHz float32 samples.

    faster-whisper 1.2.1's own `decode_audio` passes `metadata_errors` to `av.open`, which PyAV
    19 removed (TypeError), so the file is decoded here without it.
    """
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=WHISPER_SAMPLE_RATE)
    chunks: list[Any] = []
    with av.open(str(path), mode="r") as container:
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


class FakeTranscriber:
    def __init__(self, transcript: Transcript | None = None) -> None:
        self.transcript = transcript or Transcript(text="", language="en", seconds=0.0)
        self.calls: list[tuple[Path, list[str]]] = []

    def transcribe(self, path: Path, hints: Sequence[str]) -> Transcript:
        self.calls.append((path, list(hints)))
        return self.transcript
