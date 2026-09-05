"""Camera path generation: shot specs in, per-frame keyframes out.

A documentary flyover is described as a list of *shots*. This module expands
them into one `CameraState` per frame, which the headless renderer replays
deterministically (set camera -> wait for idle -> screenshot).

Great-circle interpolation and bearing maths are implemented here rather than
pulled from turf.js: the camera track is a worker-side concern and keeping it
in Python means the whole path can be generated, inspected and unit-tested
without booting a browser.

Shot types
----------
``hold``   stay put for a duration
``flyto``  ease from one camera state to another
``orbit``  rotate the bearing around a fixed centre
``path``   follow a polyline route, optionally auto-heading along the route
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from typing import Any, Callable, Iterable, Optional, Sequence

from .tiles import BBox, clamp_lat, zoom_for_bbox

EARTH_RADIUS_M = 6371008.8


# ---------------------------------------------------------------- easing ---
def _linear(t: float) -> float:
    return t


def _ease_in_out(t: float) -> float:
    return 2 * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 2) / 2


def _ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def _ease_in(t: float) -> float:
    return t * t * t


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


EASINGS: dict[str, Callable[[float], float]] = {
    "linear": _linear,
    "in": _ease_in,
    "out": _ease_out,
    "inout": _ease_in_out,
    "smooth": _smoothstep,
}


def easing(name: str) -> Callable[[float], float]:
    try:
        return EASINGS[name]
    except KeyError:
        raise ValueError(f"unknown easing {name!r}; known: {sorted(EASINGS)}") from None


# ------------------------------------------------------------- geo maths ---
def great_circle_point(a: Sequence[float], b: Sequence[float], t: float) -> tuple[float, float]:
    """Interpolate along the great circle from a to b (both [lng, lat])."""
    lon1, lat1 = math.radians(a[0]), math.radians(clamp_lat(a[1]))
    lon2, lat2 = math.radians(b[0]), math.radians(clamp_lat(b[1]))
    d = 2 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))
    if d < 1e-12:
        return (a[0], a[1])
    sd = math.sin(d)
    A = math.sin((1 - t) * d) / sd
    B = math.sin(t * d) / sd
    x = A * math.cos(lat1) * math.cos(lon1) + B * math.cos(lat2) * math.cos(lon2)
    y = A * math.cos(lat1) * math.sin(lon1) + B * math.cos(lat2) * math.sin(lon2)
    z = A * math.sin(lat1) + B * math.sin(lat2)
    return (math.degrees(math.atan2(y, x)),
            math.degrees(math.atan2(z, math.sqrt(x * x + y * y))))


def haversine_m(a: Sequence[float], b: Sequence[float]) -> float:
    lon1, lat1 = math.radians(a[0]), math.radians(clamp_lat(a[1]))
    lon2, lat2 = math.radians(b[0]), math.radians(clamp_lat(b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def initial_bearing(a: Sequence[float], b: Sequence[float]) -> float:
    """Compass bearing in degrees from a to b."""
    lon1, lat1 = math.radians(a[0]), math.radians(clamp_lat(a[1]))
    lon2, lat2 = math.radians(b[0]), math.radians(clamp_lat(b[1]))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def lerp_angle(a: float, b: float, t: float) -> float:
    """Interpolate between two bearings the short way around the circle."""
    diff = ((b - a + 180.0) % 360.0) - 180.0
    return (a + diff * t) % 360.0


# ---------------------------------------------------------------- states ---
@dataclass
class CameraState:
    lng: float
    lat: float
    zoom: float
    bearing: float = 0.0
    pitch: float = 0.0
    #: Absolute centre elevation in metres. Set by the elevation stabiliser
    #: after prefetch; None means let MapLibre clamp the centre to the ground,
    #: which is correct only when there is no terrain.
    elevation: Optional[float] = None

    def as_dict(self) -> dict:
        out = {
            "lng": round(self.lng, 7),
            "lat": round(self.lat, 7),
            "zoom": round(self.zoom, 5),
            "bearing": round(self.bearing, 4),
            "pitch": round(self.pitch, 4),
        }
        if self.elevation is not None:
            out["elevation"] = round(self.elevation, 2)
        return out

    @classmethod
    def from_any(cls, v: Any) -> "CameraState":
        if isinstance(v, CameraState):
            return v
        if isinstance(v, dict):
            center = v.get("center")
            lng = v.get("lng", center[0] if center else 0.0)
            lat = v.get("lat", center[1] if center else 0.0)
            return cls(float(lng), float(clamp_lat(float(lat))),
                       float(v.get("zoom", 4.0)), float(v.get("bearing", 0.0)),
                       float(v.get("pitch", 0.0)))
        raise TypeError(f"cannot build CameraState from {type(v).__name__}")


def blend(a: CameraState, b: CameraState, t: float) -> CameraState:
    """Blend two camera states; position follows the great circle."""
    lng, lat = great_circle_point((a.lng, a.lat), (b.lng, b.lat), t)
    elevation = None
    if a.elevation is not None and b.elevation is not None:
        elevation = a.elevation + (b.elevation - a.elevation) * t
    return CameraState(
        lng, lat,
        a.zoom + (b.zoom - a.zoom) * t,
        lerp_angle(a.bearing, b.bearing, t),
        a.pitch + (b.pitch - a.pitch) * t,
        elevation,
    )


# ----------------------------------------------------------------- shots ---
def _frames_for(duration_s: float, fps: int) -> int:
    return max(1, int(round(duration_s * fps)))


def _expand_hold(shot: dict, fps: int, prev: CameraState | None) -> list[CameraState]:
    cam = CameraState.from_any(shot.get("camera") or shot.get("at") or prev)
    return [cam] * _frames_for(float(shot.get("duration", 2.0)), fps)


def _expand_flyto(shot: dict, fps: int, prev: CameraState | None) -> list[CameraState]:
    start = CameraState.from_any(shot["from"]) if "from" in shot else prev
    if start is None:
        raise ValueError("flyto needs a 'from' camera or a preceding shot")
    end = CameraState.from_any(shot["to"])
    ease = easing(shot.get("ease", "inout"))
    n = _frames_for(float(shot.get("duration", 4.0)), fps)
    return [blend(start, end, ease(i / max(1, n - 1) if n > 1 else 1.0)) for i in range(n)]


def _expand_orbit(shot: dict, fps: int, prev: CameraState | None) -> list[CameraState]:
    center = shot.get("center")
    if center is None and prev is not None:
        center = [prev.lng, prev.lat]
    if center is None:
        raise ValueError("orbit needs a 'center'")
    zoom = float(shot.get("zoom", prev.zoom if prev else 10.0))
    pitch = float(shot.get("pitch", prev.pitch if prev else 55.0))
    b0 = float(shot.get("bearing_from", prev.bearing if prev else 0.0))
    b1 = float(shot.get("bearing_to", b0 + float(shot.get("degrees", 120.0))))
    ease = easing(shot.get("ease", "smooth"))
    n = _frames_for(float(shot.get("duration", 6.0)), fps)
    out = []
    for i in range(n):
        t = ease(i / max(1, n - 1) if n > 1 else 1.0)
        # Orbit spans may exceed 180 degrees, so interpolate the raw angle
        # rather than taking the short way round.
        out.append(CameraState(float(center[0]), clamp_lat(float(center[1])),
                               zoom, (b0 + (b1 - b0) * t) % 360.0, pitch))
    return out


def _expand_path(shot: dict, fps: int, prev: CameraState | None) -> list[CameraState]:
    route = [list(map(float, p)) for p in shot["route"]]
    if len(route) < 2:
        raise ValueError("path shot needs at least 2 route points")
    n = _frames_for(float(shot.get("duration", 8.0)), fps)
    ease = easing(shot.get("ease", "inout"))

    # Cumulative great-circle distance so the camera moves at constant ground
    # speed regardless of how unevenly the route points are spaced.
    seg = [haversine_m(route[i], route[i + 1]) for i in range(len(route) - 1)]
    total = sum(seg) or 1.0
    cum = [0.0]
    for s in seg:
        cum.append(cum[-1] + s)

    zoom0 = float(shot.get("zoom", prev.zoom if prev else 9.0))
    zoom1 = float(shot.get("zoom_to", zoom0))
    pitch0 = float(shot.get("pitch", prev.pitch if prev else 55.0))
    pitch1 = float(shot.get("pitch_to", pitch0))
    bearing_mode = shot.get("bearing", "follow")

    out: list[CameraState] = []
    for i in range(n):
        u = ease(i / max(1, n - 1) if n > 1 else 1.0)
        target = u * total
        k = max(0, min(len(seg) - 1, _bisect(cum, target)))
        span = seg[k] or 1.0
        local = min(1.0, max(0.0, (target - cum[k]) / span))
        lng, lat = great_circle_point(route[k], route[k + 1], local)
        if bearing_mode == "follow":
            bearing = initial_bearing(route[k], route[k + 1])
        elif isinstance(bearing_mode, (int, float)):
            bearing = float(bearing_mode)
        else:
            bearing = prev.bearing if prev else 0.0
        out.append(CameraState(lng, lat, zoom0 + (zoom1 - zoom0) * u,
                               bearing, pitch0 + (pitch1 - pitch0) * u))

    if bearing_mode == "follow":
        _smooth_bearings(out, window=max(3, fps // 3))
    return out


def _bisect(cum: list[float], target: float) -> int:
    lo, hi = 0, len(cum) - 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cum[mid] <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _smooth_bearings(states: list[CameraState], window: int) -> None:
    """Rolling circular mean, so route corners do not snap the camera round."""
    if window < 2 or len(states) < 3:
        return
    raw = [s.bearing for s in states]
    half = window // 2
    for i, s in enumerate(states):
        lo, hi = max(0, i - half), min(len(raw), i + half + 1)
        xs = sum(math.cos(math.radians(b)) for b in raw[lo:hi])
        ys = sum(math.sin(math.radians(b)) for b in raw[lo:hi])
        if abs(xs) > 1e-12 or abs(ys) > 1e-12:
            s.bearing = (math.degrees(math.atan2(ys, xs)) + 360.0) % 360.0


_EXPANDERS: dict[str, Callable[[dict, int, CameraState | None], list[CameraState]]] = {
    "hold": _expand_hold,
    "flyto": _expand_flyto,
    "orbit": _expand_orbit,
    "path": _expand_path,
}


def build_track(shots: Iterable[dict], fps: int) -> list[CameraState]:
    """Expand a shot list into one CameraState per frame."""
    track: list[CameraState] = []
    prev: CameraState | None = None
    for idx, shot in enumerate(shots):
        kind = shot.get("type", "hold")
        expander = _EXPANDERS.get(kind)
        if expander is None:
            raise ValueError(f"shot {idx}: unknown type {kind!r}; "
                             f"known: {sorted(_EXPANDERS)}")
        frames = expander(shot, fps, prev)
        if not frames:
            continue
        track.extend(frames)
        prev = frames[-1]
    if not track:
        raise ValueError("camera track is empty - no shots produced frames")
    return track


def auto_shots(bbox: BBox, *, width: int, height: int, duration_s: float = 10.0,
               pitch: float = 55.0, zoom_in: float = 1.4) -> list[dict]:
    """A sensible default flyover for a region when no shots are specified.

    Establishing wide view, a slow push-in with rotation, then a short orbit.
    """
    lng, lat = bbox.center
    z_fit = zoom_for_bbox(bbox, width, height)
    wide = max(1.0, z_fit - 0.4)
    close = min(16.0, z_fit + zoom_in)
    return [
        {"type": "hold",
         "camera": {"lng": lng, "lat": lat, "zoom": wide, "bearing": -18.0, "pitch": 25.0},
         "duration": duration_s * 0.15},
        {"type": "flyto",
         "to": {"lng": lng, "lat": lat, "zoom": close, "bearing": 22.0, "pitch": pitch},
         "duration": duration_s * 0.55, "ease": "inout"},
        {"type": "orbit",
         "center": [lng, lat], "zoom": close, "pitch": pitch,
         "bearing_from": 22.0, "degrees": 40.0,
         "duration": duration_s * 0.30, "ease": "smooth"},
    ]


def track_to_json(track: Sequence[CameraState], fps: int) -> str:
    return json.dumps({
        "fps": fps,
        "frames": len(track),
        "duration_s": round(len(track) / fps, 3),
        "camera": [c.as_dict() for c in track],
    }, indent=2)
