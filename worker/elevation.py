"""Terrain elevation sampled from the cached DEM, and smoothed for the camera.

Why this exists: with 3D terrain on, MapLibre clamps the camera's centre to the
ground by default, so the camera rides up and down over every ridge it passes.
On a flyover that reads as the camera bobbing - the shot lurches upward each
time a hill passes under it, which no real aerial shot does.

The fix is to take control of the centre elevation: sample the terrain along
the camera path, low-pass filter it, and feed that smoothed profile back as an
explicit elevation with `centerClampedToGround` turned off. The camera then
rises gently over a mountain range instead of tracking each bump, the way a
helicopter would.

Elevation is read from the Mapterhorn terrarium tiles the prefetch stage has
already written to disk, so this needs no network and no browser round-trip:

    elevation_m = (R * 256 + G + B / 256) - 32768
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional, Sequence

from .camera import CameraState
from .log import get as get_logger
from .tiles import clamp_lat

log = get_logger("elevation")

TERRARIUM_OFFSET = 32768.0
DEFAULT_DEM_ZOOM = 11


class ElevationSampler:
    """Bilinear elevation lookup over cached terrarium DEM tiles."""

    def __init__(self, store, upstream: str = "mapterhorn",
                 template: str = "/{z}/{x}/{y}.webp",
                 zoom: int = DEFAULT_DEM_ZOOM, tile_size: int = 512) -> None:
        self.store = store
        self.upstream = upstream
        self.template = template
        self.zoom = zoom
        self.tile_size = tile_size
        self._tiles: dict[tuple[int, int, int], Optional[object]] = {}
        self.misses = 0

    def _tile_path(self, z: int, x: int, y: int) -> str:
        return (self.template.replace("{z}", str(z))
                .replace("{x}", str(x)).replace("{y}", str(y)))

    def _load(self, z: int, x: int, y: int):
        """Decode one DEM tile to a float array, or None when not cached."""
        key = (z, x, y)
        if key in self._tiles:
            return self._tiles[key]

        entry = self.store.get(self.upstream, self._tile_path(z, x, y))
        array = None
        if entry is not None:
            try:
                import io

                import numpy as np
                from PIL import Image

                rgb = np.asarray(
                    Image.open(io.BytesIO(entry.body)).convert("RGB"),
                    dtype=np.float32)
                array = (rgb[..., 0] * 256.0 + rgb[..., 1]
                         + rgb[..., 2] / 256.0) - TERRARIUM_OFFSET
            except Exception as exc:
                log.debug("could not decode DEM tile %s: %s", key, exc)
                array = None
        self._tiles[key] = array
        return array

    def sample(self, lng: float, lat: float, default: float = 0.0) -> float:
        """Elevation in metres at a coordinate, bilinearly interpolated.

        Falls back to progressively coarser zooms when a tile is not cached.
        A coarse elevation is far better than dropping to sea level, which
        would put a sudden cliff into the camera's height profile.
        """
        lat = clamp_lat(lat)
        for z in range(self.zoom, -1, -1):
            n = 1 << z
            fx = (lng + 180.0) / 360.0 * n
            fy = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
            tx, ty = int(fx), int(fy)
            if not (0 <= tx < n and 0 <= ty < n):
                continue
            array = self._load(z, tx, ty)
            if array is not None:
                return self._bilinear(array, fx - tx, fy - ty)
        self.misses += 1
        return default

    @staticmethod
    def _bilinear(array, u: float, v: float) -> float:
        """Sample a tile array at fractional position (u, v) in [0, 1)."""
        size = array.shape[0]
        # Pixel position inside the tile.
        px = u * (size - 1)
        py = v * (size - 1)
        x0, y0 = int(px), int(py)
        x1 = min(size - 1, x0 + 1)
        y1 = min(size - 1, y0 + 1)
        dx, dy = px - x0, py - y0

        top = array[y0, x0] * (1 - dx) + array[y0, x1] * dx
        bottom = array[y1, x0] * (1 - dx) + array[y1, x1] * dx
        return float(top * (1 - dy) + bottom * dy)

    def profile(self, track: Sequence[CameraState]) -> list[float]:
        return [self.sample(c.lng, c.lat) for c in track]


def smooth(values: Sequence[float], window: int) -> list[float]:
    """Centred rolling mean, with the ends held rather than tapering to zero."""
    n = len(values)
    if n == 0 or window < 2:
        return list(values)
    half = max(1, window // 2)
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def stabilise_track(track: Sequence[CameraState], sampler: "ElevationSampler",
                    *, fps: int, smoothing_s: float = 2.5,
                    follow: float = 0.85) -> list[float]:
    """Elevation per frame that the camera should use, smoothed for motion.

    `follow` is how much of the terrain's rise the camera adopts: 1.0 hugs the
    smoothed ground, 0.0 flies at a constant height. Slightly under 1 keeps a
    long climb feeling like flight rather than a ground-follow.

    Returns metres per frame, and logs how much bob was removed.
    """
    raw = sampler.profile(track)
    if not raw:
        return []

    window = max(3, int(round(smoothing_s * fps)))
    smoothed = smooth(raw, window)

    base = sum(smoothed) / len(smoothed)
    out = [base + (v - base) * follow for v in smoothed]

    # Frame-to-frame change is what the eye reads as bob.
    def max_step(seq: Sequence[float]) -> float:
        return max((abs(b - a) for a, b in zip(seq, seq[1:])), default=0.0)

    before, after = max_step(raw), max_step(out)
    if sampler.misses:
        log.warning("%d DEM tiles missing while sampling elevation; "
                    "those frames fall back to sea level", sampler.misses)
    log.info("camera elevation stabilised: peak step %.1f m -> %.1f m "
             "(range %.0f-%.0f m)", before, after, min(out), max(out))
    return out
