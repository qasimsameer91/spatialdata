"""The render pipeline: a JobSpec in, a finished MP4 out.

Stage order matches `job.STAGES`, and each stage reports progress through a
callback so the dashboard can show a live bar and an ETA. All heavy work runs
here on the local machine; nothing in this module talks to the dashboard host.

The important invariant is the ordering of stages 2 and 3: *all* data is
fetched and cached before the first frame is rendered, so the frame loop never
makes a live network request.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from . import compose, encode, prefetch
from .cache.fetch import Fetcher
from .cache.server import TileServer
from .cache.store import CacheStore
from .camera import CameraState, auto_shots, build_track
from .config import Config, load_config
from .job import JobSpec, JobState, JobStore
from .log import get as get_logger
from .render.capture import FrameRenderer
from .render.style import build_style
from .sources import gibs

log = get_logger("pipeline")

ProgressFn = Callable[[str, float, str], None]


def _noop(stage: str, progress: float, message: str) -> None:
    log.info("[%s] %.0f%% %s", stage, progress * 100, message)


class Pipeline:
    """Executes one job end to end."""

    def __init__(self, cfg: Optional[Config] = None,
                 on_progress: Optional[ProgressFn] = None) -> None:
        self.cfg = cfg or load_config()
        self.on_progress = on_progress or _noop
        self.store = CacheStore(self.cfg.path("cache"))

    # -- helpers ---------------------------------------------------------
    def _emit(self, stage: str, progress: float, message: str = "") -> None:
        try:
            self.on_progress(stage, max(0.0, min(1.0, progress)), message)
        except Exception:
            log.exception("progress callback raised; continuing")

    def job_dir(self, job_id: str) -> Path:
        d = self.cfg.path("output") / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- the run ---------------------------------------------------------
    def run(self, spec: JobSpec, job_id: str = "adhoc") -> dict:
        spec.validate()
        out_dir = self.job_dir(job_id)
        artifacts: dict[str, str] = {}
        stats: dict[str, object] = {}
        started = time.time()

        width, height, fps = self._capped_output(spec)
        self._check_disk(spec, out_dir)

        # ------------------------------------------ 0. voiceover (first)
        # Narration is synthesised before the camera track because it decides
        # how long the piece is: a documentary is cut to its script, not the
        # other way round. With no narration the spec's duration is used.
        voice = None
        if spec.narration.strip():
            voice = self._synthesize_voice(spec, out_dir, stats)

        duration_s = spec.duration_s
        if voice is not None:
            tail = float(self.cfg.get("render.narration_tail_s", 1.5))
            duration_s = max(spec.duration_s, voice.duration_s + tail)
            stats["narration_driven_duration"] = round(duration_s, 2)

        # ---------------------------------------------------- 1. planning
        self._emit("planning", 0.0, "building camera track")
        shots = spec.shots or auto_shots(spec.box, width=width, height=height,
                                         duration_s=duration_s)
        track = build_track(shots, fps)

        # Explicit shots fix the picture length regardless of the script, so a
        # script longer than the shots would be cut off mid-sentence. Warn
        # loudly rather than shipping a video whose narration stops dead.
        if spec.shots and voice is not None:
            picture_s = len(track) / fps
            if voice.duration_s > picture_s + 0.05:
                log.warning(
                    "narration is %.1fs but the explicit shots only cover "
                    "%.1fs - the voiceover will be cut short by %.1fs. "
                    "Lengthen the shots or shorten the script.",
                    voice.duration_s, picture_s, voice.duration_s - picture_s)
                stats["narration_truncated_s"] = round(
                    voice.duration_s - picture_s, 2)

        timeline = self._build_timeline(spec, voice, len(track), fps, out_dir)
        overlays_for = timeline.at_frame
        self._write_track(out_dir, shots, track, fps)
        stats["frames"] = len(track)
        stats["duration_s"] = round(len(track) / fps, 2)
        artifacts["beats"] = str(out_dir / "beats.json")
        self._emit("planning", 1.0, f"{len(track)} frames planned")

        # The tile server must exist before the style, because every URL in
        # the style is one of its endpoints.
        with TileServer(self.store,
                        miss_policy=self.cfg.get("render.miss_policy", "warn")) as server:
            # The style and TileJSON documents must be cached before the style
            # can be composed, since composing reads them straight from disk.
            self._emit("fetching_data", 0.0, "fetching style documents")
            asyncio.run(self._warm(self._bootstrap_docs(spec), quiet=True))

            self._population_ceiling = None
            self._population_stats = None
            overlay_urls = self._overlay_sources(spec, server, out_dir)

            overlay_cfg = dict(self.cfg.get("overlays", {}) or {})
            if self._population_ceiling:
                pop_cfg = dict(overlay_cfg.get("population", {}) or {})
                pop_cfg["max_population"] = self._population_ceiling
                overlay_cfg["population"] = pop_cfg
                stats["population"] = self._population_stats

            style = build_style(
                server,
                basemap=spec.basemap,
                satellite_layer=spec.satellite_layer,
                weather_layer=spec.weather_layer,
                date=spec.date,
                terrain=spec.terrain,
                terrain_maxzoom=int(self.cfg.get("render.terrain_maxzoom", 12)),
                vector_style=spec.vector_style,
                hillshade=spec.hillshade,
                label_density=spec.label_density,
                overlay_cfg=overlay_cfg,
                population_url=overlay_urls.get("population"),
                borders_url=overlay_urls.get("borders"),
                borders="borders" in spec.overlays,
                satellite=bool({"satellite", "satellite_after"}
                               & set(spec.overlays)),
                date_end=spec.date_end,
            )
            (out_dir / "style.json").write_text(json.dumps(style, indent=2),
                                                encoding="utf-8")

            # ------------------------------------------ 2. fetch + cache
            self._emit("fetching_data", 0.0, "planning tile downloads")
            plan = prefetch.plan_for_style(
                style, track, width=width, height=height,
                padding=int(self.cfg.get("prefetch.tile_padding", 1)),
                store=self.store,
            )
            summary = prefetch.summarise(plan)
            stats["prefetch_plan"] = summary
            self._emit("fetching_data", 0.02,
                       f"{summary['total']} objects to warm")

            fetch_stats = asyncio.run(self._warm(plan))
            stats["prefetch"] = fetch_stats.as_dict()
            self._emit("fetching_data", 1.0,
                       f"cached {summary['total']} objects "
                       f"({fetch_stats.as_dict()['mb_downloaded']} MB new)")

            # ------------------------------- 2b. stabilise camera height
            # Must run after the prefetch: it reads the DEM tiles from disk.
            if spec.terrain:
                stats["elevation"] = self._stabilise_elevation(track, fps)
                # Re-save the track: it now carries the stabilised camera
                # heights, and the file should describe what actually rendered.
                self._write_track(out_dir, shots, track, fps)

            # --------------------------------------------- 3. render loop
            self._emit("rendering_frames", 0.0, "starting headless renderer")
            frame_dir = out_dir / "frames"
            preview_dir = out_dir / "previews"
            preview_at = self._preview_indices(len(track))

            with FrameRenderer(
                server, style,
                width=width, height=height,
                pixel_ratio=int(self.cfg.get("render.pixel_ratio", 1)),
                terrain_exaggeration=float(
                    self.cfg.get("render.terrain_exaggeration", 1.3)),
                idle_timeout_s=float(self.cfg.get("render.idle_timeout_s", 20.0)),
                gpu=bool(self.cfg.get("render.gpu", True)),
                stabilise_elevation=bool(spec.terrain),
            ) as renderer:
                result = renderer.render_track(
                    track, frame_dir,
                    overlays_for=overlays_for,
                    preview_at=preview_at,
                    on_progress=lambda done, total: self._emit(
                        "rendering_frames", done / total,
                        f"frame {done}/{total}"),
                )

            stats["render"] = result.as_dict()
            stats["render_misses"] = server.miss_report()
            if server.miss_report()["misses"]:
                log.warning("%d cache misses during render; prefetch under-warmed",
                            server.miss_report()["misses"])

            # Copy the sample frames somewhere stable for the dashboard.
            preview_dir.mkdir(parents=True, exist_ok=True)
            previews = []
            for i, src in enumerate(result.preview_frames):
                dest = preview_dir / f"preview_{i:02d}{src.suffix}"
                shutil.copyfile(src, dest)
                previews.append(str(dest))
            artifacts["previews"] = json.dumps(previews)
            sheet = encode.make_preview_sheet(
                result.preview_frames, preview_dir / "sheet.jpg")
            if sheet:
                artifacts["preview_sheet"] = str(sheet)

        # ------------------------------------------------- 4/5. audio mix
        # The audio has to cover the end card too, otherwise the credits play
        # in silence after the music cuts out at the last rendered frame.
        end_card_s = float(self.cfg.get("encode.end_card_seconds", 0.0))
        audio_path = self._mix_audio(spec, voice, out_dir,
                                     len(track) / fps + end_card_s)

        # --------------------------------------------------- 6. encoding
        self._emit("encoding", 0.1, "compositing attribution")
        attr_png = compose.attribution_strip(
            width, height,
            text=self.cfg.get("attribution_short") or compose.SHORT_ATTRIBUTION,
            out_path=out_dir / "attribution.png")
        # Credit only what this render actually used.
        used = ["basemap"]
        if spec.terrain:
            used.append("terrain")
        if spec.basemap == "satellite" or "satellite" in spec.overlays:
            used.append("satellite")
        if "satellite_after" in spec.overlays:
            used.append("satellite")
        used += [o for o in spec.overlays if o in ("satellite", "weather",
                                                   "population", "borders")]
        if self._resolve_music(spec):
            used.append("music")
        card_seconds = float(self.cfg.get("encode.end_card_seconds", 0.0))
        card_png = compose.end_card(
            width, height,
            title=spec.title or spec.place_name,
            lines=compose.credits_for(used),
            out_path=out_dir / "endcard.png") if card_seconds > 0 else None

        self._emit("encoding", 0.3, "encoding with h264_amf")
        mp4 = encode.assemble(
            frame_dir=result.frame_dir,
            pattern=result.pattern,
            fps=fps,
            out_path=out_dir / f"{job_id}.mp4",
            audio_path=audio_path,
            attribution_png=attr_png,
            end_card_png=card_png,
            end_card_seconds=card_seconds,
            settings=encode.EncodeSettings.from_config(self.cfg),
        )
        artifacts["video"] = str(mp4)
        artifacts["camera_track"] = str(out_dir / "camera_track.json")
        stats["video"] = {
            "path": str(mp4),
            "size_mb": round(mp4.stat().st_size / 1e6, 2),
            "duration_s": round(encode.duration_of(mp4), 2),
        }
        stats["total_s"] = round(time.time() - started, 1)

        # The render's own account of itself, next to the film rather than
        # only in the job queue, so an output directory copied somewhere else
        # still explains how it was made.
        stats_path = out_dir / "stats.json"
        stats_path.write_text(json.dumps(stats, indent=2, default=str),
                              encoding="utf-8")
        artifacts["stats"] = str(stats_path)

        self._emit("done", 1.0, f"rendered {mp4.name}")
        return {"artifacts": artifacts, "stats": stats}

    # -- stage helpers ---------------------------------------------------
    def _bootstrap_docs(self, spec: JobSpec) -> list[tuple[str, str]]:
        """Style/TileJSON documents needed before a style can be composed."""
        from .render.style import OFM_TILEJSON_PATH, vector_style_path
        docs = [("ofm", OFM_TILEJSON_PATH)]
        if spec.basemap == "vector":
            docs.append(("ofm", vector_style_path(spec.vector_style)))
        return docs

    def _capped_output(self, spec: JobSpec) -> tuple[int, int, int]:
        """Fit the job's requested output inside this machine's ceilings.

        A CPU-only runner cannot finish 1920x1080 with 3D terrain in any
        reasonable time, but the same job file should still work there. Rather
        than refuse it, scale it down: the aspect ratio is preserved and both
        dimensions stay even, which h264 requires.
        """
        width, height, fps = spec.width, spec.height, spec.fps

        max_w = self.cfg.get("render.max_width")
        max_h = self.cfg.get("render.max_height")
        scale = 1.0
        if max_w:
            scale = min(scale, int(max_w) / width)
        if max_h:
            scale = min(scale, int(max_h) / height)
        if scale < 1.0:
            # Round to even; never collapse a dimension below the 160 floor
            # that JobSpec.validate enforces.
            width = max(160, int(width * scale) // 2 * 2)
            height = max(160, int(height * scale) // 2 * 2)

        max_fps = self.cfg.get("render.max_fps")
        if max_fps:
            fps = min(fps, int(max_fps))

        if (width, height, fps) != (spec.width, spec.height, spec.fps):
            log.warning(
                "output capped to %dx%d @%dfps (job asked for %dx%d @%dfps) "
                "- this machine's render.max_* ceiling",
                width, height, fps, spec.width, spec.height, spec.fps)
        return width, height, fps

    def _check_disk(self, spec: JobSpec, out_dir: Path) -> None:
        """Refuse to start a render that cannot fit on disk.

        Running out of space mid-encode does not raise: ffmpeg writes a
        truncated file and exits cleanly, leaving an MP4 whose NAL units are
        corrupt. Far better to say so before spending ten minutes rendering.
        """
        import shutil as _shutil

        frames = int(spec.duration_s * spec.fps)
        # Measured ~280 KB per 1280x720 JPEG frame; scale by pixel count.
        per_frame = 280_000 * (spec.width * spec.height) / (1280 * 720)
        # Frames, plus the encoded video, plus headroom for the muxer.
        need = frames * per_frame * 1.35 + 400e6
        free = _shutil.disk_usage(out_dir).free
        if free < need:
            raise RuntimeError(
                f"not enough free disk for this render: need about "
                f"{need/1e9:.1f} GB, only {free/1e9:.1f} GB free. "
                f"Delete old frame directories (output/*/frames) or lower the "
                f"resolution or duration."
            )
        if free < need * 2:
            log.warning("disk is tight: %.1f GB free, this render needs "
                        "about %.1f GB", free / 1e9, need / 1e9)

    def _write_track(self, out_dir: Path, shots: list, track, fps: int) -> None:
        (out_dir / "camera_track.json").write_text(
            json.dumps({"fps": fps, "frames": len(track), "shots": shots,
                        "camera": [c.as_dict() for c in track]}, indent=2),
            encoding="utf-8")

    def _stabilise_elevation(self, track, fps: int) -> dict:
        """Give the camera a smooth height profile over the terrain.

        MapLibre clamps the camera centre to the ground when terrain is on, so
        the shot jolts upward over every ridge. Sampling the cached DEM along
        the path and low-pass filtering it removes that bob while still letting
        the camera rise over a mountain range.
        """
        from .elevation import ElevationSampler, stabilise_track

        sampler = ElevationSampler(
            self.store,
            zoom=int(self.cfg.get("render.elevation_sample_zoom", 11)))
        heights = stabilise_track(
            track, sampler, fps=fps,
            smoothing_s=float(self.cfg.get("render.elevation_smoothing_s", 2.5)),
            follow=float(self.cfg.get("render.elevation_follow", 0.85)))
        if not heights:
            return {}
        for cam, height in zip(track, heights):
            cam.elevation = height
        raw_steps = [abs(b - a) for a, b in zip(heights, heights[1:])]
        return {
            "min_m": round(min(heights)),
            "max_m": round(max(heights)),
            "max_step_m": round(max(raw_steps), 2) if raw_steps else 0.0,
            "dem_misses": sampler.misses,
        }

    async def _warm(self, plan, quiet: bool = False):
        async with Fetcher(
            self.store,
            timeout_s=float(self.cfg.get("prefetch.timeout_s", 30.0)),
            max_retries=int(self.cfg.get("prefetch.max_retries", 4)),
            backoff_base_s=float(self.cfg.get("prefetch.backoff_base_s", 0.75)),
        ) as fetcher:
            return await prefetch.warm(
                fetcher, plan,
                on_progress=None if quiet else (
                    lambda done, total: self._emit(
                        "fetching_data", done / total, f"cached {done}/{total}")),
            )

    def _preview_indices(self, frames: int) -> list[int]:
        """Sample frames early so a bad camera path is caught before the end."""
        if frames <= 1:
            return [0]
        picks = {0, min(frames - 1, max(1, frames // 20)),
                 frames // 4, frames // 2, frames - 1}
        return sorted(i for i in picks if 0 <= i < frames)

    def _overlay_sources(self, spec: JobSpec, server: TileServer,
                         out_dir: Path) -> dict[str, str]:
        """Write per-job overlay GeoJSON and return URLs the style can use.

        Populated in later phases; returns an empty mapping when the job asks
        for no vector overlays.
        """
        urls: dict[str, str] = {}
        wanted = set(spec.overlays)
        if not wanted & {"borders", "population"}:
            return urls

        from .sources import borders as borders_src, population as pop_src

        data_dir = out_dir / "overlays"
        data_dir.mkdir(parents=True, exist_ok=True)
        mount = server.mount(f"overlays", data_dir)

        if "borders" in wanted:
            path = borders_src.build_for_bbox(spec.box, data_dir / "borders.geojson",
                                              cfg=self.cfg)
            if path:
                urls["borders"] = f"{mount}/{path.name}"
        if "population" in wanted:
            extract = pop_src.build_for_bbox(
                spec.box, data_dir / "population.geojson", cfg=self.cfg)
            if extract:
                urls["population"] = f"{mount}/{extract.path.name}"
                # The colour ramp has to match the cells this extract actually
                # contains. A fixed ceiling makes a whole country clamp to one
                # colour as soon as a coarser Kontur build is selected.
                self._population_ceiling = extract.ceiling
                self._population_stats = extract.as_dict()
        return urls

    def _overlay_resolver(self, spec: JobSpec, track, fps: int):
        """Map a frame index to the overlay visibility state for that frame."""
        from .beats import OverlayTimeline
        timeline = OverlayTimeline.from_spec(spec, len(track), fps)
        return timeline.at_frame

    def _synthesize_voice(self, spec: JobSpec, out_dir: Path, stats: dict):
        """Stage 6: narration audio plus word-level timings."""
        from .tts import synthesize
        self._emit("generating_voiceover", 0.05,
                   f"synthesising narration with {spec.tts_provider}")
        voice = synthesize(
            spec.narration, out_dir / "voice.wav",
            provider=spec.tts_provider, voice=spec.tts_voice, cfg=self.cfg,
            on_progress=lambda p, m: self._emit("generating_voiceover", p, m),
        )
        (out_dir / "voice_timings.json").write_text(
            json.dumps(voice.as_dict(), indent=2), encoding="utf-8")
        stats["voice"] = {
            "provider": voice.provider, "voice": voice.voice,
            "duration_s": round(voice.duration_s, 2),
            "segments": len(voice.segments), "words": len(voice.words),
            "aligned_with": voice.aligned_with,
        }
        if voice.credits is not None:
            stats["voice"]["credits"] = voice.credits
        return voice

    def _build_timeline(self, spec: JobSpec, voice, frames: int, fps: int,
                        out_dir: Path):
        """Stage 7: beats JSON mapping time -> camera -> active overlays."""
        from .beats import OverlayTimeline, auto_beats

        if spec.beats:
            timeline = OverlayTimeline.from_spec(spec, frames, fps)
        elif voice is not None and spec.overlays:
            beats = auto_beats(voice.segments, spec.overlays, frames / fps)
            timeline = OverlayTimeline(beats, frames, fps)
        else:
            timeline = OverlayTimeline.from_spec(spec, frames, fps)
        timeline.write(out_dir / "beats.json")
        return timeline

    def _mix_audio(self, spec: JobSpec, voice, out_dir: Path,
                   video_s: float) -> Optional[Path]:
        """Stage 8: narration over music, music ducked under narration."""
        music = self._resolve_music(spec)
        if voice is None and music is None:
            return None
        from .audio import mix as audio_mix
        self._emit("mixing_audio", 0.4, "mixing narration and music")
        mixed = audio_mix.build_track(
            voice_path=voice.audio_path if voice else None,
            music_path=music,
            out_path=out_dir / "audio.wav",
            target_s=video_s,
            cfg=self.cfg,
        )
        self._emit("mixing_audio", 1.0, "audio ready")
        return mixed

    def _resolve_music(self, spec: JobSpec) -> Optional[Path]:
        if not spec.music:
            return None
        candidate = Path(spec.music)
        if candidate.is_file():
            return candidate
        bundled = self.cfg.path("data") / "music" / spec.music
        return bundled if bundled.is_file() else None


def run_job(store: JobStore, state: JobState, cfg: Optional[Config] = None) -> JobState:
    """Execute a queued job, keeping its persisted state up to date."""
    state.started_at = time.time()
    state.stage = "planning"
    state.error = None
    store.save(state)

    def progress(stage: str, pct: float, message: str) -> None:
        state.stage = stage
        state.progress = pct
        state.message = message
        store.save(state)

    pipeline = Pipeline(cfg, on_progress=progress)
    try:
        result = pipeline.run(state.spec, state.id)
        state.artifacts.update(result["artifacts"])
        state.stats.update(result["stats"])
        state.stage = "done"
        state.progress = 1.0
        state.message = "complete"
    except Exception as exc:
        log.exception("job %s failed", state.id)
        state.stage = "failed"
        state.error = f"{type(exc).__name__}: {exc}"
        state.message = "failed"
    finally:
        state.finished_at = time.time()
        store.save(state)
    return state
