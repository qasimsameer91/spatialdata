"""Ground footprint of a tilted MapLibre camera.

The prefetch stage needs to know exactly which tiles a frame will ask for. For
a top-down camera that is just the viewport box, but a pitched camera sees far
toward the horizon, and guessing at a padding factor either over-fetches
badly (wasting the upstream's bandwidth) or under-fetches (blank tiles).

So this reproduces MapLibre's camera model: perspective projection with a
36.87 degree vertical FOV, camera pitched back from nadir, rays cast through
the viewport corners and intersected with the ground plane. Verified against
`map.unproject()` in the browser by tests/test_frustum_matches_maplibre.py.
"""
from __future__ import annotations

import math
from typing import Sequence

from .camera import CameraState
from .tiles import BBox, MAX_LAT, TILE_SIZE, clamp_lat

# MapLibre's default vertical field of view (radians).
DEFAULT_FOV = 0.6435011087932844

# A ray angled at or above the horizon never meets the ground. Rather than
# treating that as infinite, clamp how far a frame is allowed to see, or a
# single horizon-grazing frame would demand tiles for half a continent.
MAX_GROUND_DISTANCE_SCREENS = 12.0


def _merc_x(lng: float) -> float:
    return (lng + 180.0) / 360.0


def _merc_y(lat: float) -> float:
    lat = clamp_lat(lat)
    return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0


def _inv_merc_x(x: float) -> float:
    return x * 360.0 - 180.0


def _inv_merc_y(y: float) -> float:
    y = min(1.0, max(0.0, y))
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))


def ground_points(cam: CameraState, width: int, height: int, *,
                  fov: float = DEFAULT_FOV,
                  samples: int = 5) -> list[tuple[float, float]]:
    """Project a grid of screen points onto the ground, returning lng/lat pairs.

    Sampling the viewport edges rather than only its corners matters at high
    pitch, where the ground footprint is a trapezoid whose widest point is the
    top edge, and whose sides bow outward.
    """
    pitch = math.radians(max(0.0, min(85.0, cam.pitch)))
    bearing = math.radians(cam.bearing)
    sin_p, cos_p = math.sin(pitch), math.cos(pitch)
    sin_b, cos_b = math.sin(bearing), math.cos(bearing)

    # Focal length in pixels: the distance at which the viewport height
    # subtends the field of view. This is MapLibre's cameraToCenterDistance.
    focal = 0.5 * height / math.tan(fov / 2.0)
    cam_height = focal * cos_p
    max_dist = MAX_GROUND_DISTANCE_SCREENS * max(width, height)

    world_size = TILE_SIZE * (2.0 ** cam.zoom)
    cx, cy = _merc_x(cam.lng), _merc_y(cam.lat)

    out: list[tuple[float, float]] = []
    for i in range(samples):
        sx = (i / (samples - 1) - 0.5) * width
        for j in range(samples):
            sy = (j / (samples - 1) - 0.5) * height

            # Ray direction in world axes (X east, Y north, Z up), before
            # applying the map bearing.
            dir_x = sx
            dir_y = -sy * cos_p + focal * sin_p
            dir_z = -sy * sin_p - focal * cos_p

            if dir_z >= -1e-9:
                # Ray points at or above the horizon: clamp to the far limit.
                t = max_dist / max(1e-6, math.hypot(dir_x, dir_y))
            else:
                t = cam_height / -dir_z

            gx = t * dir_x
            gy = -focal * sin_p + t * dir_y
            dist = math.hypot(gx, gy)
            if dist > max_dist:
                scale = max_dist / dist
                gx *= scale
                gy *= scale

            # Apply the map bearing, then convert pixel offsets to mercator.
            wx = gx * cos_b + gy * sin_b
            wy = -gx * sin_b + gy * cos_b

            mx = cx + wx / world_size
            my = cy - wy / world_size
            out.append((_inv_merc_x(mx), _inv_merc_y(my)))
    return out


def camera_bbox(cam: CameraState, width: int, height: int, *,
                fov: float = DEFAULT_FOV) -> BBox:
    """Axis-aligned ground bbox visible from this camera state."""
    pts = ground_points(cam, width, height, fov=fov)
    lngs = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    west, east = min(lngs), max(lngs)
    # A view spanning the antimeridian shows up as a near-global longitude
    # span; treat that as global rather than emitting a nonsense box.
    if east - west > 180.0:
        west, east = -180.0, 180.0
    return BBox(max(-180.0, west), clamp_lat(min(lats)),
                min(180.0, east), clamp_lat(max(lats)))


def track_bbox(track: Sequence[CameraState], width: int, height: int) -> BBox:
    """Union of the ground footprints of every frame in a camera track."""
    if not track:
        raise ValueError("empty camera track")
    boxes = [camera_bbox(c, width, height) for c in track]
    return BBox(
        min(b.west for b in boxes),
        max(-MAX_LAT, min(b.south for b in boxes)),
        max(b.east for b in boxes),
        min(MAX_LAT, max(b.north for b in boxes)),
    )


def source_zoom(cam_zoom: float, tile_size: int, *, maxzoom: int = 22,
                minzoom: int = 0) -> int:
    """Integer tile zoom MapLibre requests for a source of this tile size.

    A 512px source uses the zoom directly; a 256px source needs one extra
    level to cover the same ground at the same screen density.
    """
    adjust = math.log2(TILE_SIZE / max(1, tile_size))
    z = math.floor(cam_zoom + adjust + 1e-9)
    return max(minzoom, min(maxzoom, int(z)))
