"""The beats timeline: timestamp -> camera state -> active overlays.

A beat is one narrative moment. It says when it starts, how long it lasts,
which data overlays are visible during it, and optionally where the camera
should be. The renderer asks this module for the overlay state of each frame,
so a population layer can fade in exactly when the narration reaches the
population segment.

Stored as a single JSON document per job, the same shape as the scene-beats
JSON in the AniDoc documentary pipeline, extended with an `overlays` field:

    {
      "fps": 30,
      "beats": [
        {"t": 0.0, "duration": 4.0, "text": "...",
         "overlays": {"borders": {"opacity": 0.9}},
         "camera": {"lng": .., "lat": .., "zoom": .., "pitch": ..}}
      ]
    }

Overlays cross-fade rather than popping, because a hard cut on a translucent
raster reads as a glitch on video.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .job import OVERLAY_NAMES, JobSpec
from .log import get as get_logger

log = get_logger("beats")

DEFAULT_FADE_S = 0.6


@dataclass
class Beat:
    t: float
    duration: float
    text: str = ""
    #: overlay name -> {"opacity": float}. Absent overlays are hidden.
    overlays: dict[str, dict] = field(default_factory=dict)
    camera: Optional[dict] = None
    label: str = ""

    @property
    def end(self) -> float:
        return self.t + self.duration

    def as_dict(self) -> dict:
        out: dict[str, Any] = {
            "t": round(self.t, 3),
            "duration": round(self.duration, 3),
            "overlays": self.overlays,
        }
        if self.text:
            out["text"] = self.text
        if self.label:
            out["label"] = self.label
        if self.camera:
            out["camera"] = self.camera
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Beat":
        overlays = data.get("overlays") or {}
        normalised: dict[str, dict] = {}
        for name, value in overlays.items():
            if name not in OVERLAY_NAMES:
                log.warning("beat references unknown overlay %r; ignoring", name)
                continue
            if isinstance(value, bool):
                normalised[name] = {"opacity": 1.0 if value else 0.0}
            elif isinstance(value, (int, float)):
                normalised[name] = {"opacity": float(value)}
            elif isinstance(value, dict):
                normalised[name] = {"opacity": float(value.get("opacity", 1.0))}
            else:
                # Silently dropping this would leave the overlay mysteriously
                # absent from the render with nothing in the log to explain it.
                log.warning(
                    "beat overlay %r has an unusable opacity %r (%s); expected "
                    "a number, bool or {\"opacity\": n}. Ignoring it.",
                    name, value, type(value).__name__)
        return cls(
            t=float(data.get("t", 0.0)),
            duration=float(data.get("duration", 0.0)),
            text=str(data.get("text", "")),
            overlays=normalised,
            camera=data.get("camera"),
            label=str(data.get("label", "")),
        )


class OverlayTimeline:
    """Resolves the overlay state of any frame, with fades between beats."""

    def __init__(self, beats: Sequence[Beat], frames: int, fps: int,
                 *, fade_s: float = DEFAULT_FADE_S,
                 default_opacity: Optional[dict[str, float]] = None) -> None:
        self.beats = sorted(beats, key=lambda b: b.t)
        self.frames = frames
        self.fps = fps
        self.fade_s = max(0.0, fade_s)
        self.default_opacity = default_opacity or {}
        self._cache: dict[int, dict] = {}

    # -- construction ----------------------------------------------------
    @classmethod
    def from_spec(cls, spec: JobSpec, frames: int, fps: int) -> "OverlayTimeline":
        """Build from an explicit beats list, or default to always-on overlays."""
        defaults = {name: 1.0 for name in spec.overlays}
        if spec.beats:
            beats = [Beat.from_dict(b) for b in spec.beats]
        elif spec.overlays:
            # No timeline given: show every requested overlay for the whole clip.
            beats = [Beat(t=0.0, duration=frames / fps,
                          overlays={n: {"opacity": 1.0} for n in spec.overlays})]
        else:
            beats = []
        return cls(beats, frames, fps, default_opacity=defaults)

    @classmethod
    def from_json(cls, path: Path, frames: int, fps: int) -> "OverlayTimeline":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        beats = [Beat.from_dict(b) for b in doc.get("beats", [])]
        return cls(beats, frames, fps, fade_s=float(doc.get("fade_s", DEFAULT_FADE_S)))

    # -- queries ---------------------------------------------------------
    def opacity_at(self, name: str, t: float) -> float:
        """Opacity of one overlay at time t, including fade in/out ramps."""
        best = 0.0
        for beat in self.beats:
            target = beat.overlays.get(name)
            if target is None:
                continue
            opacity = float(target.get("opacity", 1.0))
            if opacity <= 0.0:
                continue
            fade = min(self.fade_s, beat.duration / 2.0) if beat.duration > 0 else 0.0
            if t < beat.t - fade or t > beat.end + fade:
                continue
            if fade <= 0.0:
                ramp = 1.0 if beat.t <= t <= beat.end else 0.0
            elif t < beat.t:
                ramp = (t - (beat.t - fade)) / fade
            elif t > beat.end:
                ramp = 1.0 - (t - beat.end) / fade
            else:
                ramp = 1.0
            best = max(best, opacity * max(0.0, min(1.0, ramp)))
        return best

    def at_time(self, t: float) -> dict[str, dict]:
        state: dict[str, dict] = {}
        for name in OVERLAY_NAMES:
            opacity = self.opacity_at(name, t)
            if opacity > 0.001:
                state[name] = {"visible": True, "opacity": round(opacity, 4)}
        return state

    def at_frame(self, index: int) -> dict[str, dict]:
        cached = self._cache.get(index)
        if cached is None:
            cached = self.at_time(index / self.fps)
            self._cache[index] = cached
        return cached

    def camera_overrides(self) -> list[tuple[float, dict]]:
        return [(b.t, b.camera) for b in self.beats if b.camera]

    # -- serialisation ---------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "fps": self.fps,
            "frames": self.frames,
            "duration_s": round(self.frames / self.fps, 3),
            "fade_s": self.fade_s,
            "beats": [b.as_dict() for b in self.beats],
        }

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        return path


# Words that suggest an overlay belongs to a narration segment. The timeline
# is generated automatically, so the script itself decides when the population
# layer appears rather than the operator hand-editing keyframes.
OVERLAY_CUES: dict[str, tuple[str, ...]] = {
    # Deliberately excludes bare quantity words like "million": a phrase such
    # as "thirty million years" is about geology, not people, and matching it
    # would raise the population layer over the wrong sentence.
    "population": ("population", "people", "inhabit", "inhabitants", "density",
                   "densely", "populated", "lives in", "live in", "living in",
                   "citizens", "residents", "crowded", "city", "cities",
                   "urban", "town", "towns", "settlement", "settlements"),
    # "between" alone is far too common in ordinary prose to imply a frontier.
    "borders": ("border", "borders", "frontier", "boundary", "boundaries",
                "nation", "nations", "country", "countries", "sovereign",
                "territory", "territories", "divides", "separates"),
    "weather": ("storm", "storms", "cloud", "clouds", "weather", "rain",
                "snow", "wind", "winds", "temperature", "heat", "cold",
                "monsoon", "hurricane", "cyclone", "fire", "fires", "smoke",
                "drought", "flood", "season", "seasonal"),
    "satellite": ("satellite", "orbit", "from space", "imagery", "seen from",
                  "aerial"),
    # The "after" imagery is the second half of a before/after comparison, so
    # it cues on the words that mark the turn.
    "satellite_after": ("after", "afterwards", "later", "by then", "today",
                        "now", "weeks later", "months later", "aftermath",
                        "once the", "when the water", "left behind"),
}


def auto_beats(segments: Sequence[Any], overlays: Sequence[str],
               total_s: float, *, fallback: str = "spread",
               persist: bool = True, sustain_opacity: float = 0.78
               ) -> list[Beat]:
    """Build a timeline from narration segments and the requested overlays.

    Each narration sentence becomes a beat, and an overlay is *introduced* on
    the sentence whose wording calls for it.

    By default an overlay then **stays on** for the rest of the piece at a
    slightly lower opacity. Switching a layer off again the moment its sentence
    ends makes it flash on and vanish, which reads as a glitch rather than an
    edit: the viewer sees a border appear for four seconds and disappear with
    nothing having changed on screen to explain it. Documentary layers
    accumulate - they are introduced, then they settle into the background.

    Set `persist=False` for the strict behaviour where a layer is visible only
    during its own sentence.
    """
    if not segments:
        if not overlays:
            return []
        return [Beat(t=0.0, duration=total_s,
                     overlays={name: {"opacity": 1.0} for name in overlays})]

    beats: list[Beat] = []
    for seg in segments:
        text = getattr(seg, "text", "") or ""
        start = float(getattr(seg, "start", 0.0))
        end = float(getattr(seg, "end", start))
        lowered = text.lower()
        active: dict[str, dict] = {}
        for name in overlays:
            cues = OVERLAY_CUES.get(name, ())
            if any(cue in lowered for cue in cues):
                active[name] = {"opacity": 1.0}
        beats.append(Beat(t=start, duration=max(0.0, end - start),
                          text=text, overlays=active,
                          label=f"segment {getattr(seg, 'index', len(beats))}"))

    uncued = [name for name in overlays
              if not any(name in b.overlays for b in beats)]
    if uncued and fallback == "spread":
        # Spread the remaining overlays over the back half of the piece, where
        # a documentary usually moves from establishing shots to data.
        start_at = len(beats) // 2
        for offset, name in enumerate(uncued):
            idx = min(len(beats) - 1, start_at + offset)
            beats[idx].overlays.setdefault(name, {"opacity": 1.0})
        log.info("overlays %s were never cued by the script; "
                 "introduced in the later beats", uncued)

    if persist:
        for name in overlays:
            introduced = next((i for i, b in enumerate(beats)
                               if name in b.overlays), None)
            if introduced is None:
                continue
            for beat in beats[introduced + 1:]:
                beat.overlays.setdefault(name, {"opacity": sustain_opacity})
    return beats


def beats_from_narration(segments: Iterable[dict], overlays_by_segment:
                         Optional[Sequence[dict]] = None) -> list[Beat]:
    """Turn timed narration segments into beats.

    `segments` are dicts with `start`, `end` and `text`, as produced by the TTS
    alignment step. `overlays_by_segment` optionally assigns overlay state per
    segment; without it the beats carry timing and text only, and overlays can
    be attached later in the dashboard.
    """
    beats: list[Beat] = []
    for i, seg in enumerate(segments):
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        overlays = {}
        if overlays_by_segment and i < len(overlays_by_segment):
            overlays = {k: ({"opacity": float(v)} if isinstance(v, (int, float))
                            else v)
                        for k, v in (overlays_by_segment[i] or {}).items()}
        beats.append(Beat(t=start, duration=max(0.0, end - start),
                          text=str(seg.get("text", "")).strip(),
                          overlays=overlays))
    return beats
