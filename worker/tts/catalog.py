"""Voice catalogues and provider availability, with no heavy imports.

Kept deliberately dependency-free so the hosted dashboard can describe the
available voices in /api/meta without numpy, torch or Kokoro installed. The
provider modules import their voice list from here; nothing here imports them.
"""
from __future__ import annotations

import importlib.util
import os

# Kokoro voice packs. The prefix encodes language/gender: af = American
# female, am = American male, bf/bm = British.
KOKORO_VOICES = [
    {"id": "af_heart", "label": "Heart (US female, warm)", "lang": "a"},
    {"id": "af_bella", "label": "Bella (US female, bright)", "lang": "a"},
    {"id": "af_nicole", "label": "Nicole (US female, soft)", "lang": "a"},
    {"id": "af_sarah", "label": "Sarah (US female, neutral)", "lang": "a"},
    {"id": "am_michael", "label": "Michael (US male, warm)", "lang": "a"},
    {"id": "am_adam", "label": "Adam (US male, deep)", "lang": "a"},
    {"id": "bf_emma", "label": "Emma (UK female)", "lang": "b"},
    {"id": "bf_isabella", "label": "Isabella (UK female)", "lang": "b"},
    {"id": "bm_george", "label": "George (UK male, documentary)", "lang": "b"},
    {"id": "bm_lewis", "label": "Lewis (UK male)", "lang": "b"},
]

# A small curated set; the full Google catalogue is large and mostly
# irrelevant for documentary narration.
GCLOUD_VOICES = [
    {"id": "en-US-Chirp3-HD-Charon", "label": "Charon (US male, documentary)"},
    {"id": "en-US-Chirp3-HD-Kore", "label": "Kore (US female, warm)"},
    {"id": "en-US-Chirp3-HD-Puck", "label": "Puck (US male, bright)"},
    {"id": "en-US-Chirp3-HD-Aoede", "label": "Aoede (US female, calm)"},
    {"id": "en-GB-Chirp3-HD-Fenrir", "label": "Fenrir (UK male)"},
    {"id": "en-GB-Chirp3-HD-Leda", "label": "Leda (UK female)"},
]

VOICES_BY_PROVIDER = {
    "kokoro": KOKORO_VOICES,
    "gcloud": GCLOUD_VOICES,
}


def _installed(module: str) -> bool:
    """True if a module could be imported, without actually importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def kokoro_available() -> bool:
    return _installed("kokoro")


def gcloud_available() -> bool:
    # Needs both the client library and credentials to be usable at all.
    return (_installed("google.cloud.texttospeech")
            and bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")))


def available_providers() -> dict[str, bool]:
    return {"kokoro": kokoro_available(), "gcloud": gcloud_available()}


def lang_for_voice(voice: str) -> str:
    """Kokoro selects its G2P backend from a one-letter language code."""
    for entry in KOKORO_VOICES:
        if entry["id"] == voice:
            return entry["lang"]
    # Unknown voice: infer from the conventional prefix.
    return "b" if voice.startswith(("bf", "bm")) else "a"
