"""Headless frame capture: Playwright + MapLibre, one deterministic frame at a time.

This is explicitly *not* a screen recording. For every frame the driver sets
the camera, waits for the map to report itself settled, then screenshots. A
run of N frames therefore takes as long as it takes and is reproducible,
rather than being tied to wall-clock playback speed.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from playwright.sync_api import Error as PWError, sync_playwright

from ..camera import CameraState
from ..log import get as get_logger
from .style import OVERLAY_LAYERS

log = get_logger("render.capture")

# Headless Chromium needs to be told to produce a real GL context. We try the
# GPU-backed ANGLE path first and fall back to SwiftShader, which is slower but
# works on any machine.
# The ANGLE backend is platform-specific: d3d11 exists only on Windows, and
# asking for it elsewhere leaves Chromium with no GL at all rather than
# falling back, so pick the backend that the host can actually provide.
_ANGLE_BACKEND = {"win32": "d3d11", "darwin": "metal"}.get(sys.platform, "gl")
_GPU_FLAGS = [
    f"--use-angle={_ANGLE_BACKEND}",
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
]
_SWIFTSHADER_FLAGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
]
_COMMON_FLAGS = [
    "--hide-scrollbars",
    "--mute-audio",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # Frames must not vary with the host's colour management.
    "--force-color-profile=srgb",
    "--disable-lcd-text",
]


@dataclass
class RenderResult:
    frames: int
    frame_dir: Path
    pattern: str
    elapsed_s: float
    timed_out_frames: list[int] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    preview_frames: list[Path] = field(default_factory=list)
    renderer: str = ""

    @property
    def fps_achieved(self) -> float:
        return self.frames / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "frame_dir": str(self.frame_dir),
            "elapsed_s": round(self.elapsed_s, 2),
            "render_fps": round(self.fps_achieved, 2),
            "timed_out_frames": len(self.timed_out_frames),
            "page_errors": self.page_errors[:10],
            "renderer": self.renderer,
        }


class FrameRenderer:
    """Drives the MapLibre render surface frame by frame."""

    def __init__(self, server, style: dict, *, width: int = 1920, height: int = 1080,
                 pixel_ratio: int = 1, terrain_exaggeration: float = 1.3,
                 idle_timeout_s: float = 20.0, image_format: str = "jpeg",
                 jpeg_quality: int = 94, gpu: bool = True,
                 stabilise_elevation: bool = False) -> None:
        self.server = server
        self.style = style
        self.width = width
        self.height = height
        self.pixel_ratio = pixel_ratio
        self.terrain_exaggeration = terrain_exaggeration
        self.idle_timeout_s = idle_timeout_s
        self.image_format = image_format
        self.jpeg_quality = jpeg_quality
        self.gpu = gpu
        self.stabilise_elevation = stabilise_elevation

        self._pw = None
        self._browser = None
        self._page = None
        self.renderer_name = ""

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "FrameRenderer":
        self._pw = sync_playwright().start()
        flags = _COMMON_FLAGS + (_GPU_FLAGS if self.gpu else _SWIFTSHADER_FLAGS)
        try:
            self._browser = self._pw.chromium.launch(headless=True, args=flags)
        except PWError:
            log.warning("GPU launch failed, retrying with SwiftShader")
            self._browser = self._pw.chromium.launch(
                headless=True, args=_COMMON_FLAGS + _SWIFTSHADER_FLAGS)
        context = self._browser.new_context(
            viewport={"width": self.width, "height": self.height},
            device_scale_factor=self.pixel_ratio,
            # A fixed locale keeps label collision and number formatting stable.
            locale="en-US",
            timezone_id="UTC",
            reduced_motion="reduce",
        )
        self._page = context.new_page()
        self._page.on("console", self._on_console)
        self._page.on("pageerror", lambda e: log.warning("page error: %s", e))
        return self

    def __exit__(self, *exc) -> None:
        for closer in (self._browser, self._pw):
            try:
                if closer is not None:
                    closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:
                pass
        self._browser = None
        self._pw = None

    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            log.debug("console.%s %s", msg.type, msg.text[:200])

    # -- boot --------------------------------------------------------------
    def boot(self, first_camera: CameraState, overlays: dict | None = None) -> dict:
        """Load the page and the style, and wait for the first settled frame."""
        style_url = self.server.put_json("style", self.style)
        page_url = f"{self.server.base_url}/render/map.html"
        log.info("loading render surface %s", page_url)
        self._page.goto(page_url, wait_until="domcontentloaded", timeout=60000)

        cfg = {
            "styleUrl": style_url,
            "camera": first_camera.as_dict(),
            "overlays": overlays or {},
            "terrainExaggeration": self.terrain_exaggeration,
            "idleTimeoutMs": int(self.idle_timeout_s * 1000),
            "loadTimeoutMs": 90000,
            "stabiliseElevation": self.stabilise_elevation,
            # Single source of truth for which style layers each overlay owns.
            "overlayLayers": OVERLAY_LAYERS,
        }
        result = self._page.evaluate("cfg => window.__sd.init(cfg)", cfg)
        self.renderer_name = self._detect_renderer()
        log.info("map ready in %sms (settled=%s) via %s",
                 result.get("waitMs"), result.get("settled"), self.renderer_name)
        if result.get("errors"):
            for err in result["errors"][:5]:
                log.warning("style/init error: %s", err)
        return result

    def _detect_renderer(self) -> str:
        """Report whether we ended up on the GPU or on SwiftShader."""
        try:
            return self._page.evaluate(
                """() => {
                    const c = document.createElement('canvas');
                    const gl = c.getContext('webgl2') || c.getContext('webgl');
                    if (!gl) return 'no-webgl';
                    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
                    return dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)
                               : (gl.getParameter(gl.RENDERER) + '');
                }"""
            ) or "unknown"
        except Exception:
            return "unknown"

    # -- the frame loop ----------------------------------------------------
    def render_track(
        self,
        track: Sequence[CameraState],
        out_dir: Path,
        *,
        overlays_for: Optional[Callable[[int], dict]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        preview_at: Iterable[int] = (0,),
        clean: bool = True,
    ) -> RenderResult:
        """Render every camera state to a numbered image file."""
        out_dir = Path(out_dir)
        if clean and out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        ext = "jpg" if self.image_format == "jpeg" else "png"
        pattern = f"frame_%06d.{ext}"
        shot_kwargs: dict = {"type": self.image_format}
        if self.image_format == "jpeg":
            shot_kwargs["quality"] = self.jpeg_quality
        # Playwright's screenshot timeout defaults to 30s, which is fine on a
        # GPU but not on SwiftShader, where a single 3D-terrain frame can take
        # longer than that. Without this, CPU-only rendering fails outright
        # rather than merely being slow.
        shot_kwargs["timeout"] = max(30_000, int(self.idle_timeout_s * 1000) * 3)

        preview_set = set(preview_at)
        previews: list[Path] = []
        timed_out: list[int] = []
        total = len(track)

        first_overlays = overlays_for(0) if overlays_for else {}
        self.boot(track[0], first_overlays)

        started = time.time()
        for index, cam in enumerate(track):
            overlays = overlays_for(index) if overlays_for else {}
            settle = self._page.evaluate(
                "([cam, ovl]) => window.__sd.frame(cam, ovl)",
                [cam.as_dict(), overlays],
            )
            if not settle.get("settled"):
                timed_out.append(index)
                if len(timed_out) <= 5:
                    log.warning("frame %d did not settle within %.1fs (waited %sms)",
                                index, self.idle_timeout_s, settle.get("waitMs"))

            target = out_dir / (pattern % index)
            self._page.screenshot(path=str(target), **shot_kwargs)

            if index in preview_set:
                previews.append(target)

            if on_progress and (index % 10 == 0 or index == total - 1):
                on_progress(index + 1, total)

        elapsed = time.time() - started
        report = self._page.evaluate("() => window.__sd.report()")
        page_errors = report.get("errors", []) if isinstance(report, dict) else []
        if timed_out:
            log.warning("%d/%d frames hit the idle timeout - the cache is probably "
                        "under-warmed for this camera path", len(timed_out), total)

        return RenderResult(
            frames=total,
            frame_dir=out_dir,
            pattern=pattern,
            elapsed_s=elapsed,
            timed_out_frames=timed_out,
            page_errors=page_errors,
            preview_frames=previews,
            renderer=self.renderer_name,
        )

    def snapshot(self, cam: CameraState, path: Path, overlays: dict | None = None) -> Path:
        """Render a single frame - used for dashboard previews and smoke tests."""
        self._page.evaluate("([cam, ovl]) => window.__sd.frame(cam, ovl)",
                            [cam.as_dict(), overlays or {}])
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {"type": "png"} if path.suffix == ".png" else {
            "type": "jpeg", "quality": self.jpeg_quality}
        kwargs["timeout"] = max(30_000, int(self.idle_timeout_s * 1000) * 3)
        self._page.screenshot(path=str(path), **kwargs)
        return path
