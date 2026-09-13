"""Job specification and on-disk job state.

A job is the single unit of work the dashboard queues and the local worker
executes. It is persisted as JSON so a job survives a worker restart and so
the dashboard can read status without sharing memory with the worker.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .sources import gibs
from .tiles import BBox

# Ordered pipeline stages. The dashboard renders progress against this list,
# so the order here is the order the UI shows.
STAGES = [
    "planning",
    "fetching_data",
    "rendering_frames",
    "generating_voiceover",
    "mixing_audio",
    "encoding",
    "done",
]

OVERLAY_NAMES = ("satellite", "satellite_after", "weather",
                 "population", "borders")


#: Job ids become filenames and URL segments, so they are validated rather
#: than sanitised - see JobStore.path_for.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def new_job_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


@dataclass
class JobSpec:
    """Everything needed to render one documentary clip."""

    # -- what to show ----------------------------------------------------
    bbox: list[float] = field(default_factory=lambda: [-0.51, 51.28, 0.33, 51.69])
    place_name: str = "London"
    date: Optional[str] = None
    date_end: Optional[str] = None

    basemap: str = "vector"            # vector | satellite | relief
    vector_style: str = "fiord"        # OpenFreeMap style for basemap=vector
    hillshade: bool = True
    label_density: str = "balanced"
    satellite_layer: str = "truecolor_viirs"
    weather_layer: Optional[str] = None
    overlays: list[str] = field(default_factory=list)
    terrain: bool = True

    # -- how to move -----------------------------------------------------
    shots: Optional[list[dict]] = None   # None -> auto_shots
    duration_s: float = 10.0

    # -- output ----------------------------------------------------------
    width: int = 1920
    height: int = 1080
    fps: int = 30

    # -- narration / audio ----------------------------------------------
    narration: str = ""
    tts_provider: str = "kokoro"
    tts_voice: str = "af_heart"
    music: Optional[str] = None
    beats: Optional[list[dict]] = None

    title: str = ""

    def __post_init__(self) -> None:
        self.validate()

    # -- validation ------------------------------------------------------
    def validate(self) -> None:
        BBox.from_list(self.bbox)   # raises on a malformed box
        if self.basemap not in ("vector", "satellite", "relief"):
            raise ValueError(f"basemap must be vector|satellite|relief, "
                             f"got {self.basemap!r}")
        if self.basemap == "vector":
            from .render.style import LABEL_DENSITY_DROP, vector_style_path
            vector_style_path(self.vector_style)   # raises on an unknown style
            if self.label_density not in LABEL_DENSITY_DROP:
                raise ValueError(
                    f"label_density must be one of "
                    f"{sorted(LABEL_DENSITY_DROP)}, got {self.label_density!r}")
        unknown = set(self.overlays) - set(OVERLAY_NAMES)
        if unknown:
            raise ValueError(f"unknown overlays {sorted(unknown)}; "
                             f"valid: {list(OVERLAY_NAMES)}")
        if self.date:
            gibs.normalise_date(self.date)
        if self.date_end:
            gibs.date_range(self.date or self.date_end, self.date_end)
        # A weather overlay with no layer chosen would silently render
        # nothing, so fall back to the default rather than failing quietly.
        if "weather" in self.overlays and not self.weather_layer:
            self.weather_layer = "clouds"
        if self.weather_layer:
            gibs.get_layer(self.weather_layer)
        # "satellite_after" needs a second date to mean anything.
        if "satellite_after" in self.overlays and not self.date_end:
            raise ValueError(
                "the 'satellite_after' overlay needs date_end set - it "
                "cross-fades imagery from date to date_end")
        if self.basemap == "satellite" or "satellite" in self.overlays:
            gibs.get_layer(self.satellite_layer)
        from .tts.catalog import VOICES_BY_PROVIDER
        if self.tts_provider not in VOICES_BY_PROVIDER:
            raise ValueError(
                f"tts_provider must be one of {sorted(VOICES_BY_PROVIDER)}, "
                f"got {self.tts_provider!r}")
        if not (1 <= self.fps <= 120):
            raise ValueError(f"fps must be 1..120, got {self.fps}")
        if not (0.5 <= self.duration_s <= 900):
            raise ValueError(f"duration_s must be 0.5..900, got {self.duration_s}")
        for dim, name in ((self.width, "width"), (self.height, "height")):
            if not (160 <= dim <= 3840):
                raise ValueError(f"{name} must be 160..3840, got {dim}")
            if dim % 2:
                raise ValueError(f"{name} must be even for h264, got {dim}")

    @property
    def box(self) -> BBox:
        return BBox.from_list(self.bbox)

    @property
    def resolved_date(self) -> str:
        return gibs.normalise_date(self.date)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JobSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class JobState:
    """Mutable progress record, written to disk after every update."""

    id: str
    spec: JobSpec
    stage: str = "planning"
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    artifacts: dict[str, str] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.stage in ("done", "failed")

    @property
    def elapsed_s(self) -> float:
        end = self.finished_at or time.time()
        return end - (self.started_at or self.created_at)

    def eta_s(self) -> Optional[float]:
        """Rough remaining time from progress so far."""
        if self.progress <= 0.01 or self.is_terminal:
            return None
        return self.elapsed_s * (1.0 - self.progress) / self.progress

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "spec": self.spec.as_dict(),
            "stage": self.stage,
            "stage_index": STAGES.index(self.stage) if self.stage in STAGES else -1,
            "stages": STAGES,
            "progress": round(self.progress, 4),
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "elapsed_s": round(self.elapsed_s, 2),
            "eta_s": round(self.eta_s(), 1) if self.eta_s() else None,
            "artifacts": self.artifacts,
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobState":
        state = cls(id=data["id"], spec=JobSpec.from_dict(data.get("spec", {})))
        for key in ("stage", "progress", "message", "error", "created_at",
                    "updated_at", "started_at", "finished_at", "artifacts", "stats"):
            if key in data and data[key] is not None:
                setattr(state, key, data[key])
        return state


class JobStore:
    """Filesystem-backed job queue shared by the worker and the dashboard."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, job_id: str) -> Path:
        """Path for a job record, rejecting anything that is not a clean id.

        Stripping unsafe characters instead of rejecting them silently maps
        distinct ids onto one file - "../../etc" and "etc" both became
        etc.json, so one job could overwrite another's record.
        """
        if not job_id or not _JOB_ID_RE.match(job_id):
            raise ValueError(
                f"invalid job id {job_id!r}: use letters, digits, dot, dash "
                "or underscore, starting with a letter or digit"
            )
        return self.root / f"{job_id}.json"

    def create(self, spec: JobSpec, job_id: Optional[str] = None) -> JobState:
        state = JobState(id=job_id or new_job_id(), spec=spec)
        self.save(state)
        return state

    def save(self, state: JobState) -> None:
        state.updated_at = time.time()
        target = self.path_for(state.id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.as_dict(), indent=2), encoding="utf-8")
        tmp.replace(target)

    def load(self, job_id: str) -> Optional[JobState]:
        path = self.path_for(job_id)
        if not path.is_file():
            return None
        try:
            return JobState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError):
            return None

    def list(self, limit: int = 100) -> list[JobState]:
        states: list[JobState] = []
        for path in sorted(self.root.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                states.append(JobState.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, KeyError):
                continue
        return states

    def next_pending(self) -> Optional[JobState]:
        """Oldest queued job, for the worker's polling loop."""
        pending = [s for s in self.list(limit=500)
                   if s.stage == "planning" and s.started_at is None]
        pending.sort(key=lambda s: s.created_at)
        return pending[0] if pending else None

    def delete(self, job_id: str) -> bool:
        path = self.path_for(job_id)
        if path.is_file():
            path.unlink()
            return True
        return False
