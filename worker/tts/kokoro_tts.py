"""Kokoro local TTS - the default provider.

Runs entirely on this machine with no account, no key and no usage cost, which
is what makes it the default. Model weights live in the local HuggingFace
cache, so synthesis works with no network at all once they are present.

Sentences are synthesised individually and joined with a short pause, so the
exact start/end of every sentence is known by construction rather than
inferred afterwards.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from ..log import get as get_logger
from . import Segment, VoiceResult

log = get_logger("tts.kokoro")

SAMPLE_RATE = 24000
#: Silence inserted between sentences, which also gives the mixer a natural
#: place to breathe and keeps sentences from running together.
SENTENCE_GAP_S = 0.28

from .catalog import KOKORO_VOICES as VOICES, kokoro_available, lang_for_voice

_VOICE_IDS = {v["id"] for v in VOICES}
_pipelines: dict[str, object] = {}


def is_available() -> bool:
    return kokoro_available()


def _pipeline(lang_code: str):
    """Cache one pipeline per language; construction dominates short jobs."""
    if lang_code not in _pipelines:
        # Weights come from the local HF cache; do not reach for the network.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from kokoro import KPipeline
        log.info("loading Kokoro pipeline (lang=%s)", lang_code)
        _pipelines[lang_code] = KPipeline(lang_code=lang_code,
                                          repo_id="hexgrad/Kokoro-82M")
    return _pipelines[lang_code]


def _to_numpy(audio) -> np.ndarray:
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    return np.asarray(audio, dtype=np.float32).reshape(-1)


def synthesize(sentences: Sequence[str], out_path: Path, *,
               voice: str = "af_heart", cfg=None,
               on_progress: Optional[Callable[[float, str], None]] = None
               ) -> VoiceResult:
    if not is_available():
        raise RuntimeError(
            "kokoro is not installed. Install it with: pip install kokoro")
    if voice not in _VOICE_IDS:
        log.warning("voice %r is not in the known set; passing it through", voice)

    import soundfile as sf

    speed = float(cfg.get("tts.speed", 1.0)) if cfg else 1.0
    pipe = _pipeline(lang_for_voice(voice))

    gap = np.zeros(int(SAMPLE_RATE * SENTENCE_GAP_S), dtype=np.float32)
    pieces: list[np.ndarray] = []
    segments: list[Segment] = []
    cursor = 0.0

    for i, sentence in enumerate(sentences):
        chunks = [_to_numpy(res.audio) for res in
                  pipe(sentence, voice=voice, speed=speed)]
        if not chunks:
            log.warning("sentence %d produced no audio: %r", i, sentence[:60])
            continue
        audio = np.concatenate(chunks)
        start = cursor
        end = start + len(audio) / SAMPLE_RATE
        segments.append(Segment(index=i, text=sentence, start=start, end=end))
        pieces.append(audio)

        if i < len(sentences) - 1:
            pieces.append(gap)
            cursor = end + SENTENCE_GAP_S
        else:
            cursor = end

        if on_progress:
            on_progress(0.1 + 0.7 * (i + 1) / max(1, len(sentences)),
                        f"synthesised sentence {i + 1}/{len(sentences)}")

    if not pieces:
        raise RuntimeError("Kokoro produced no audio for this narration")

    full = np.concatenate(pieces)
    peak = float(np.max(np.abs(full))) if full.size else 0.0
    if peak > 0:
        # Normalise to a consistent headroom so the mixer's ducking maths and
        # the music bed behave the same regardless of voice or sentence.
        full = full * (0.89 / peak)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), full, SAMPLE_RATE)
    duration = len(full) / SAMPLE_RATE
    log.info("kokoro: %d sentence(s), %.2fs -> %s",
             len(segments), duration, out_path.name)

    return VoiceResult(
        audio_path=out_path,
        sample_rate=SAMPLE_RATE,
        duration_s=duration,
        provider="kokoro",
        voice=voice,
        segments=segments,
    )
