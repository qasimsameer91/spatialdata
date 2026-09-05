"""Google Cloud TTS (Chirp3 HD) - optional secondary provider.

Selected from the same dropdown as Kokoro, mirroring the provider-selector
pattern in the video-workflow-saas app. It is **not** the default: it needs a
Google Cloud project and credentials, and while the free tier is generous
(1 million characters/month for standard voices, 100k for Chirp3 HD) it is
still an account-gated service. Kokoro stays the zero-setup default.

Credentials are read the standard way, from GOOGLE_APPLICATION_CREDENTIALS or
an explicitly configured service-account JSON path. Nothing is ever sent
anywhere unless the operator picks this provider.

Chirp3 HD does not return word timings, so alignment falls through to Whisper
exactly as it does for Kokoro.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..log import get as get_logger
from . import Segment, VoiceResult

log = get_logger("tts.gcloud")

SAMPLE_RATE = 24000
SENTENCE_GAP_S = 0.28

from .catalog import GCLOUD_VOICES as VOICES, gcloud_available


def is_available() -> bool:
    """True only when both the client library and credentials are present."""
    return gcloud_available()


def _client(cfg):
    from google.cloud import texttospeech

    creds_path = (cfg.get("tts.gcloud_credentials") if cfg else None)
    if creds_path and Path(creds_path).is_file():
        return texttospeech.TextToSpeechClient.from_service_account_file(creds_path)
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise RuntimeError(
            "Google Cloud TTS selected but no credentials found. Set "
            "GOOGLE_APPLICATION_CREDENTIALS, or set tts.gcloud_credentials in "
            "config/local.json, or switch the provider back to 'kokoro'."
        )
    return texttospeech.TextToSpeechClient()


def synthesize(sentences: Sequence[str], out_path: Path, *,
               voice: str = "en-US-Chirp3-HD-Charon", cfg=None,
               on_progress: Optional[Callable[[float, str], None]] = None
               ) -> VoiceResult:
    try:
        from google.cloud import texttospeech
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-texttospeech is not installed. Install it with: "
            "pip install google-cloud-texttospeech, or use the free local "
            "'kokoro' provider instead."
        ) from exc

    import numpy as np
    import soundfile as sf

    client = _client(cfg)
    language = "-".join(voice.split("-")[:2]) or "en-US"
    speed = float(cfg.get("tts.speed", 1.0)) if cfg else 1.0

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        speaking_rate=speed,
    )
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language, name=voice)

    gap = np.zeros(int(SAMPLE_RATE * SENTENCE_GAP_S), dtype=np.float32)
    pieces: list[np.ndarray] = []
    segments: list[Segment] = []
    cursor = 0.0

    for i, sentence in enumerate(sentences):
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=sentence),
            voice=voice_params,
            audio_config=audio_config,
        )
        # LINEAR16 comes back as a WAV container; decode it to float samples.
        import io
        audio, sr = sf.read(io.BytesIO(response.audio_content), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != SAMPLE_RATE:
            log.warning("gcloud returned %d Hz, expected %d", sr, SAMPLE_RATE)

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
        raise RuntimeError("Google Cloud TTS produced no audio")

    full = np.concatenate(pieces)
    peak = float(np.max(np.abs(full))) if full.size else 0.0
    if peak > 0:
        full = full * (0.89 / peak)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), full, SAMPLE_RATE)
    duration = len(full) / SAMPLE_RATE
    log.info("gcloud: %d sentence(s), %.2fs -> %s",
             len(segments), duration, out_path.name)

    return VoiceResult(
        audio_path=out_path,
        sample_rate=SAMPLE_RATE,
        duration_s=duration,
        provider="gcloud",
        voice=voice,
        segments=segments,
    )
