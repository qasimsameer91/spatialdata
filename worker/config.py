"""Configuration loading and defaults for the spatialdata render worker.

Settings resolve in this order (last wins):
    built-in DEFAULTS  ->  config/default.json  ->  config/<profile>.json
    ->  config/local.json  ->  per-job overrides

The profile layer is selected with the SPATIALDATA_PROFILE environment
variable, so one checkout can render at full quality on a GPU box and at
reduced settings on a CPU-only runner without editing any tracked file.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent

# A profile name becomes a filename, so it may not contain path separators.
_PROFILE_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

DEFAULTS: Dict[str, Any] = {
    "paths": {
        "cache": "cache",
        "output": "output",
        "jobs": "jobs",
        "data": "data",
    },
    "render": {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        # Ceilings the machine imposes on whatever the job file asks for.
        # null = no ceiling. A job is downscaled to fit, keeping its aspect
        # ratio, rather than being rejected -- the same job file should render
        # on a GPU box at full size and on a CPU runner at a size it can
        # actually finish.
        "max_width": None,
        "max_height": None,
        "max_fps": None,
        "pixel_ratio": 1,
        # How the local tile server behaves on a cache miss during rendering.
        #   "warn"    -> fetch upstream, log loudly (safe default, never breaks a render)
        #   "offline" -> refuse, serve placeholder (guarantees zero live requests mid-frame)
        "miss_policy": "warn",
        # Ask Chromium for a hardware GL context. Set false on a machine with
        # no GPU (a CI runner) to go straight to SwiftShader software rendering
        # instead of failing to get a WebGL context at all.
        "gpu": True,
        # Max wall-clock seconds to wait for the map to settle before capturing a frame.
        "idle_timeout_s": 20.0,
        "terrain_exaggeration": 1.3,
        "terrain_maxzoom": 12,
        # Camera height stabilisation over 3D terrain. Without it the camera
        # rides up and down over every ridge it passes.
        "elevation_sample_zoom": 11,
        "elevation_smoothing_s": 2.5,
        # 1.0 hugs the smoothed ground, 0.0 flies flat.
        "elevation_follow": 0.85,
        # Extra seconds of video after the narration ends.
        "narration_tail_s": 1.5,
    },
    "prefetch": {
        # Polite concurrency against public-good services. Do not raise casually.
        "concurrency": 6,
        "requests_per_second": 12.0,
        "max_retries": 4,
        "backoff_base_s": 0.75,
        "timeout_s": 30.0,
        # Extra ring of tiles fetched around the visible area, absorbing camera jitter.
        "tile_padding": 1,
    },
    "encode": {
        # Hardware encode on the AMD RX 6600 XT. Never libx264 for the final encode.
        "encoder": "h264_amf",
        "quality": "quality",
        # null = constant-quantiser (the qp_i/qp_p below), which targets a
        # quality rather than a size. Setting a bitrate here switches to
        # peak-VBR and makes qp_i/qp_p dead settings.
        "bitrate": None,
        "max_bitrate": None,
        "qp_i": 18,
        "qp_p": 20,
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_bitrate": "192k",
        # Closing credit card, in seconds. 0 = off, which is the default: the
        # corner attribution burned into every frame already satisfies the
        # data licences, so the card is optional garnish.
        "end_card_seconds": 0.0,
    },
    "audio": {
        # Music is loudness-normalised to music_lufs first, then trimmed by
        # music_gain_db, so the bed is predictable for any source track.
        "music_lufs": -20.0,
        "music_gain_db": -12.0,
        "duck_gain_db": -14.0,
        "voice_gain_db": 0.0,
        "duck_attack_ms": 200,
        "duck_release_ms": 700,
    },
    "tts": {
        "provider": "kokoro",
        "voice": "af_heart",
        "speed": 1.0,
        "sample_rate": 24000,
        "align": "whisper",
        "whisper_model": "base.en",
    },
    "overlays": {
        "population": {
            # Sequential scale applied to Kontur H3 population counts.
            "color_scale": ["#0d0887", "#6a00a8", "#b12a90", "#e16462", "#fca636", "#f0f921"],
            # Reads as a data layer over the terrain rather than paint over it.
            "opacity": 0.68,
            "max_population": 8000,
        },
        "borders": {"color": "#ffd166", "width": 1.4, "opacity": 0.9},
        "weather": {"opacity": 0.6},
        "satellite": {"opacity": 1.0},
    },
    # Compact credit burned into the corner of every frame. The longer
    # "attribution" below is the full notice used in docs and the end card.
    "attribution_short": (
        "© OpenStreetMap · OpenFreeMap · Mapterhorn · "
        "NASA GIBS · Kontur · Natural Earth"
    ),
    "attribution": (
        "Map data (c) OpenStreetMap contributors, OpenFreeMap. "
        "Satellite imagery: NASA GIBS/EOSDIS. "
        "Population data: Kontur Population (CC BY). "
        "Boundaries: Natural Earth."
    ),
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Resolved settings plus absolute paths derived from the project root."""

    def __init__(self, data: Dict[str, Any], root: Path = ROOT) -> None:
        self.data = data
        self.root = root

    @classmethod
    def load(cls, overrides: Dict[str, Any] | None = None, root: Path = ROOT) -> "Config":
        merged = copy.deepcopy(DEFAULTS)
        profile = os.environ.get("SPATIALDATA_PROFILE", "").strip()
        names = ["default.json"]
        if profile:
            if not _PROFILE_RE.match(profile):
                raise ValueError(
                    f"SPATIALDATA_PROFILE={profile!r} is not a plain profile name")
            names.append(f"{profile}.json")
        names.append("local.json")
        for name in names:
            path = root / "config" / name
            if path.is_file():
                merged = _deep_merge(merged, json.loads(path.read_text(encoding="utf-8")))
            elif name == f"{profile}.json":
                raise FileNotFoundError(
                    f"SPATIALDATA_PROFILE={profile!r} but config/{name} does not exist")
        if overrides:
            merged = _deep_merge(merged, overrides)
        return cls(merged, root)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        """Fetch a nested value with a dotted path, e.g. cfg.get('render.fps')."""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, name: str) -> Path:
        """Absolute path for one of the configured project directories."""
        raw = Path(self.data["paths"][name])
        resolved = raw if raw.is_absolute() else self.root / raw
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    def to_json(self) -> str:
        return json.dumps(self.data, indent=2)


def load_config(overrides: Dict[str, Any] | None = None) -> Config:
    return Config.load(overrides)
