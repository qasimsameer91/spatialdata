"""Text-to-speech with a pluggable provider, mirroring the video-workflow-saas
provider-selector pattern.

Providers
---------
``kokoro``  local, free, unlimited. The default, and the only one that needs no
            account of any kind.
``gcloud``  Google Cloud TTS free tier (Chirp3 HD voices). Optional; used only
            when explicitly selected and credentials are already present.
``ai33``    AI33 / OpenSpeaker. PAID, credit-based, optional. Large voice
            library and native word timings; needs AI33_API_KEY in .env.

Narration is synthesised **per sentence** and concatenated, which gives exact
segment boundaries for the beats timeline without having to infer them. Word
level timestamps come from Whisper forced alignment (`align.py`) when the
provider does not return them natively - Kokoro does not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..log import get as get_logger

log = get_logger("tts")

ProgressFn = Callable[[float, str], None]

# Sentence splitter that keeps abbreviations and decimals intact.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Prof|Inc|Ltd|vs|etc|e\.g|i\.e)\.$",
                     re.IGNORECASE)


@dataclass
class Word:
    word: str
    start: float
    end: float

    def as_dict(self) -> dict:
        return {"word": self.word, "start": round(self.start, 3),
                "end": round(self.end, 3)}


@dataclass
class Segment:
    """One narration sentence with its position in the finished audio."""

    index: int
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {"index": self.index, "text": self.text,
                "start": round(self.start, 3), "end": round(self.end, 3),
                "words": [w.as_dict() for w in self.words]}


@dataclass
class VoiceResult:
    audio_path: Path
    sample_rate: int
    duration_s: float
    provider: str
    voice: str
    segments: list[Segment] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    aligned_with: str = ""
    #: Credits spent, for paid providers. None for free ones.
    credits: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "audio": str(self.audio_path),
            "credits": self.credits,
            "sample_rate": self.sample_rate,
            "duration_s": round(self.duration_s, 3),
            "provider": self.provider,
            "voice": self.voice,
            "aligned_with": self.aligned_with,
            "segments": [s.as_dict() for s in self.segments],
            "words": [w.as_dict() for w in self.words],
        }


def split_sentences(text: str) -> list[str]:
    """Split narration into sentences for per-segment synthesis."""
    text = " ".join(text.split())
    if not text:
        return []
    parts: list[str] = []
    for chunk in _SENTENCE_END.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Re-join a split that landed on a known abbreviation.
        if parts and _ABBREV.search(parts[-1]):
            parts[-1] = f"{parts[-1]} {chunk}"
        else:
            parts.append(chunk)
    return parts


def available_providers() -> dict[str, bool]:
    """Which providers can actually run right now.

    Resolved through `catalog`, which checks for the modules without importing
    them, so the hosted dashboard can answer this with none of the render-only
    dependencies installed.
    """
    from .catalog import available_providers as _available
    return _available()


def list_voices(provider: str = "kokoro") -> list[dict]:
    from .catalog import VOICES_BY_PROVIDER
    try:
        return VOICES_BY_PROVIDER[provider]
    except KeyError:
        raise ValueError(f"unknown TTS provider {provider!r}; "
                         f"expected one of {sorted(VOICES_BY_PROVIDER)}") from None


def synthesize(text: str, out_path: Path, *, provider: str = "kokoro",
               voice: str = "af_heart", cfg=None,
               on_progress: Optional[ProgressFn] = None) -> VoiceResult:
    """Synthesise narration and return audio plus word-level timings."""
    text = (text or "").strip()
    if not text:
        raise ValueError("narration text is empty")

    sentences = split_sentences(text)
    log.info("synthesising %d sentence(s) with %s/%s",
             len(sentences), provider, voice)

    if provider == "kokoro":
        from . import kokoro_tts
        result = kokoro_tts.synthesize(sentences, Path(out_path), voice=voice,
                                       cfg=cfg, on_progress=on_progress)
    elif provider == "gcloud":
        from . import gcloud_tts
        result = gcloud_tts.synthesize(sentences, Path(out_path), voice=voice,
                                       cfg=cfg, on_progress=on_progress)
    elif provider == "ai33":
        from . import ai33_tts
        result = ai33_tts.synthesize(sentences, Path(out_path), voice=voice,
                                     cfg=cfg, on_progress=on_progress)
    else:
        raise ValueError(
            f"unknown TTS provider {provider!r}; "
            "expected 'kokoro', 'gcloud' or 'ai33'")

    # Word timings: use the provider's own when it has them, else force-align.
    if not result.words:
        from . import align
        mode = (cfg.get("tts.align", "whisper") if cfg else "whisper")
        if mode == "whisper":
            if on_progress:
                on_progress(0.85, "aligning words with Whisper")
            try:
                align.attach_word_timings(
                    result, model_size=(cfg.get("tts.whisper_model", "base.en")
                                        if cfg else "base.en"))
            except Exception as exc:
                log.warning("Whisper alignment unavailable (%s); "
                            "falling back to proportional timings", exc)
                align.attach_estimated_timings(result)
        else:
            align.attach_estimated_timings(result)

    if on_progress:
        on_progress(1.0, f"voiceover ready ({result.duration_s:.1f}s)")
    return result
