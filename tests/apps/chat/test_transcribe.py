"""Transcriber seam: the fake covers callers; one slow test runs real faster-whisper on a cached
model and a synthetic sentence (macOS `say`)."""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from farmcore.apps.chat.transcribe import (
    SPRAY_VOCABULARY,
    FakeTranscriber,
    Transcript,
    WhisperTranscriber,
    initial_prompt,
)
from farmcore.settings import Settings

# Tried in order; the first one already in the local Hugging Face cache is used.
CANDIDATE_MODELS = ("small", "base", "base.en", "tiny", "tiny.en")


def test_fake_returns_its_transcript_and_records_calls(tmp_path):
    audio = tmp_path / "note.ogg"
    fake = FakeTranscriber(Transcript(text="Sprayed P1", language="en", seconds=2.5))

    result = fake.transcribe(audio, ["P1 Le Plateau"])

    assert result == Transcript(text="Sprayed P1", language="en", seconds=2.5)
    assert fake.calls == [(audio, ["P1 Le Plateau"])]


def test_initial_prompt_carries_spray_vocabulary_next_to_the_hints():
    # Brand hints alone bias Whisper towards the brand ("sulphur" heard as "Sulfo"), so the
    # generic substance and time words are always in the prompt too.
    prompt = initial_prompt(["P5 Les Granges", "MICROTHIOL SPECIAL DISPERSS"])

    assert "Les Granges" in prompt and "MICROTHIOL SPECIAL DISPERSS" in prompt
    vocabulary = ("copper", "sulphur", "cuivre", "soufre", "Bouillie bordelaise", "kg/ha")
    for word in (*vocabulary, "this morning", "ce matin"):
        assert word in SPRAY_VOCABULARY
        assert word in prompt


def test_initial_prompt_without_hints_is_still_the_vocabulary():
    prompt = initial_prompt(["", "  "])

    assert "sulphur" in prompt and "soufre" in prompt


def test_whisper_does_not_load_the_model_until_first_use():
    # A model name that cannot exist: constructing must not touch disk or network.
    transcriber = WhisperTranscriber(Settings(_env_file=None, whisper_model="no-such-model"))
    assert transcriber.model_name == "no-such-model"


def _cached_model() -> str | None:
    from faster_whisper.utils import download_model

    wanted = os.environ.get("FARMCORE_WHISPER_TEST_MODEL")
    for name in (wanted,) if wanted else CANDIDATE_MODELS:
        try:
            download_model(name, local_files_only=True)
        except Exception:
            continue
        return name
    return None


@pytest.mark.slow
def test_silence_cannot_turn_the_farms_vocabulary_into_a_spray(tmp_path):
    model = _cached_model()
    if model is None:
        pytest.skip("no faster-whisper model in the local cache")
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000 * 8)
    transcriber = WhisperTranscriber(
        Settings(_env_file=None, whisper_model=model), local_files_only=True
    )

    result = transcriber.transcribe(
        path,
        [
            "Le Tertre",
            "Les Granges",
            "Les Graves",
            "Bel-Air",
            "BOUILLIE BORDELAISE RSR DISPERSS NC",
            "PAJO",
        ],
    )

    assert result.text == "", result.text


@pytest.mark.slow
def test_whisper_transcribes_a_synthetic_sentence(tmp_path: Path):
    model = _cached_model()
    if model is None:
        pytest.skip("no faster-whisper model in the local cache")
    if shutil.which("say") is None:
        pytest.skip("macOS `say` not available to synthesise speech")
    audio = tmp_path / "note.wav"
    subprocess.run(
        ["say", "-o", str(audio), "--data-format=LEI16@16000", "Sprayed P1 with copper, two kilos"],
        check=True,
    )
    transcriber = WhisperTranscriber(
        Settings(_env_file=None, whisper_model=model), local_files_only=True
    )

    result = transcriber.transcribe(audio, ["P1 Le Plateau", "copper"])

    assert "copper" in result.text.casefold()
    assert result.language == "en"
    assert result.seconds > 0


class _Segment:
    def __init__(self, text, no_speech_prob=0.0, avg_logprob=-0.2):
        self.text, self.no_speech_prob, self.avg_logprob = text, no_speech_prob, avg_logprob


class _Info:
    language, duration = "en", 3.0


class _FakeModel:
    def __init__(self, segments):
        self.segments = segments

    def transcribe(self, audio, **kwargs):
        return iter(self.segments), _Info()


def _with_segments(monkeypatch, segments):
    from farmcore.apps.chat import transcribe as module

    monkeypatch.setattr(module, "decode_audio", lambda path: b"")
    transcriber = WhisperTranscriber(Settings(_env_file=None, whisper_model="unused"))
    transcriber._model = _FakeModel(segments)
    return transcriber


@pytest.mark.parametrize(
    "phrase",
    [
        "Thanks for watching!",
        "Thank you for watching.",
        "Thank you.",
        "Sous-titres réalisés par la communauté d'Amara.org",
        "Merci d'avoir regardé cette vidéo !",
    ],
)
def test_known_silence_hallucinations_are_dropped(tmp_path, monkeypatch, phrase):
    transcriber = _with_segments(monkeypatch, [_Segment(phrase)])
    assert transcriber.transcribe(tmp_path / "a.ogg", []).text == ""


def test_segments_whisper_marks_as_no_speech_are_dropped(tmp_path, monkeypatch):
    transcriber = _with_segments(
        monkeypatch,
        [
            _Segment("Sprayed P5 with copper, 2 kilos."),
            _Segment("Bouillie bordelaise", no_speech_prob=0.9, avg_logprob=-1.4),
        ],
    )
    assert transcriber.transcribe(tmp_path / "a.ogg", []).text == (
        "Sprayed P5 with copper, 2 kilos."
    )


def test_real_speech_mentioning_thanks_is_kept(tmp_path, monkeypatch):
    transcriber = _with_segments(monkeypatch, [_Segment("Thanks, I sprayed P6 this morning.")])
    assert transcriber.transcribe(tmp_path / "a.ogg", []).text == (
        "Thanks, I sprayed P6 this morning."
    )
