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

# AI33 / OpenSpeaker. A starting set proven in the mapediting project; the
# live library is far larger and any voice id from it can be passed through.
# IDs carry their engine as a prefix: edge_, elevenlabs_, minimax_, ...
AI33_VOICES = [
    {"id": "edge_en-US-AriaNeural", "label": "Aria (US female, Edge)"},
    {"id": "edge_en-US-GuyNeural", "label": "Guy (US male, Edge)"},
    {"id": "edge_en-GB-SoniaNeural", "label": "Sonia (UK female, Edge)"},
    {"id": "elevenlabs_nPczCjzI2devNBz1zQrb", "label": "Brian (US male, deep, ElevenLabs)"},
    {"id": "elevenlabs_pFZP5JQG7iQjIQuC4Bku", "label": "Lily (female, velvety, ElevenLabs)"},
]

VOICES_BY_PROVIDER = {
    "kokoro": KOKORO_VOICES,
    "gcloud": GCLOUD_VOICES,
    "ai33": AI33_VOICES,
}

#: Providers that spend money per use. The UI labels these; nothing selects
#: one implicitly.
PAID_PROVIDERS = frozenset({"ai33"})


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


def ai33_available() -> bool:
    # A key is all it needs; the client is the standard library.
    from ..env import load_env
    load_env()
    return bool(os.environ.get("AI33_API_KEY", "").strip())


def available_providers() -> dict[str, bool]:
    return {"kokoro": kokoro_available(), "gcloud": gcloud_available(),
            "ai33": ai33_available()}


def lang_for_voice(voice: str) -> str:
    """Kokoro selects its G2P backend from a one-letter language code."""
    for entry in KOKORO_VOICES:
        if entry["id"] == voice:
            return entry["lang"]
    # Unknown voice: infer from the conventional prefix.
    return "b" if voice.startswith(("bf", "bm")) else "a"
