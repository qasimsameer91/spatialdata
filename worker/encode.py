"""FFmpeg assembly: frames + audio -> final MP4, encoded on the AMD GPU.

The final encode always uses `h264_amf` (hardware, RX 6600 XT). libx264 is
never used for the deliverable; it is available only as an explicit fallback
if the AMF encoder is missing, and that fact is logged loudly.

Attribution is composited here rather than drawn into the map frames, so the
render loop stays purely cartographic and the credit can be restyled without
re-rendering a single frame.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .log import get as get_logger

log = get_logger("encode")


class EncodeError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise EncodeError("ffmpeg not found on PATH")
    return exe


def ffprobe_bin() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise EncodeError("ffprobe not found on PATH")
    return exe


def has_encoder(name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30).stdout
    except (subprocess.SubprocessError, EncodeError):
        return False
    return any(line.split()[1] == name
               for line in out.splitlines()
               if line.strip() and len(line.split()) > 1)


def probe(path: Path) -> dict:
    """Return ffprobe's format+stream JSON for a media file."""
    result = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_format", "-show_streams",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise EncodeError(f"ffprobe failed for {path}: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout)


def duration_of(path: Path) -> float:
    info = probe(path)
    return float(info.get("format", {}).get("duration", 0.0))


@dataclass
class EncodeSettings:
    encoder: str = "h264_amf"
    #: Constant-quantiser targets. Lower is better quality, larger file.
    qp_i: int = 18
    qp_p: int = 20
    quality: str = "quality"
    bitrate: Optional[str] = None      # set to use peak-VBR instead of CQP
    max_bitrate: Optional[str] = None
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    gop_seconds: float = 2.0
    bframes: int = 2
    #: VBAQ spends bits where the eye looks; worth it for sky/terrain gradients.
    vbaq: bool = True

    @classmethod
    def from_config(cls, cfg) -> "EncodeSettings":
        return cls(
            encoder=cfg.get("encode.encoder", "h264_amf"),
            quality=cfg.get("encode.quality", "quality"),
            bitrate=cfg.get("encode.bitrate"),
            max_bitrate=cfg.get("encode.max_bitrate"),
            pix_fmt=cfg.get("encode.pix_fmt", "yuv420p"),
            audio_codec=cfg.get("encode.audio_codec", "aac"),
            audio_bitrate=cfg.get("encode.audio_bitrate", "192k"),
            qp_i=int(cfg.get("encode.qp_i", 18)),
            qp_p=int(cfg.get("encode.qp_p", 20)),
        )

    def video_args(self, fps: int) -> list[str]:
        gop = max(1, int(round(fps * self.gop_seconds)))
        if self.encoder == "h264_amf":
            args = [
                "-c:v", "h264_amf",
                "-usage", "transcoding",
                "-quality", self.quality,
                "-profile:v", "high",
                "-g", str(gop),
                "-bf", str(self.bframes),
            ]
            if self.vbaq:
                args += ["-vbaq", "1"]
            if self.bitrate:
                args += ["-rc", "vbr_peak", "-b:v", self.bitrate,
                         "-maxrate", self.max_bitrate or self.bitrate]
            else:
                args += ["-rc", "cqp", "-qp_i", str(self.qp_i),
                         "-qp_p", str(self.qp_p)]
            return args
        # Software fallback. Not used for deliverables.
        log.warning("encoding with %s instead of the h264_amf hardware encoder",
                    self.encoder)
        return ["-c:v", self.encoder, "-preset", "medium", "-crf", "18",
                "-g", str(gop)]


def resolve_settings(settings: EncodeSettings) -> EncodeSettings:
    """Fall back to libx264 only if the hardware encoder is genuinely absent."""
    if settings.encoder == "h264_amf" and not has_encoder("h264_amf"):
        log.error("h264_amf not available in this ffmpeg build; "
                  "falling back to libx264 (software)")
        return EncodeSettings(**{**settings.__dict__, "encoder": "libx264"})
    return settings


def _run(cmd: Sequence[str], what: str) -> None:
    log.debug("%s: %s", what, " ".join(str(c) for c in cmd))
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        raise EncodeError(f"{what} failed (exit {result.returncode}):\n{tail}")


def assemble(
    *,
    frame_dir: Path,
    pattern: str,
    fps: int,
    out_path: Path,
    audio_path: Optional[Path] = None,
    attribution_png: Optional[Path] = None,
    end_card_png: Optional[Path] = None,
    end_card_seconds: float = 4.0,
    settings: Optional[EncodeSettings] = None,
) -> Path:
    """Stitch the rendered frames into the final MP4.

    Filter graph: the frame sequence gets the attribution strip overlaid, the
    end card is scaled and appended, and the result is encoded once on the GPU.
    """
    settings = resolve_settings(settings or EncodeSettings())
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    frames_glob = str(Path(frame_dir) / pattern)
    cmd: list[str] = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                      "-framerate", str(fps), "-i", frames_glob]

    filters: list[str] = []
    inputs = 1

    # Rendered frames arrive as full-range JPEG (yuvj420p). Encoded straight
    # through, the result is tagged full-range and plays back with crushed
    # blacks or blown highlights depending on the player. Convert to limited
    # range BT.709 up front so the whole graph works in video range.
    filters.append("[0:v]scale=in_range=full:out_range=limited,"
                   "format=yuv420p,setsar=1[vsrc]")
    video_label = "vsrc"

    if attribution_png and Path(attribution_png).is_file():
        cmd += ["-i", str(attribution_png)]
        attr_idx = inputs
        inputs += 1
        filters.append(f"[{video_label}][{attr_idx}:v]overlay=0:0:format=auto[vmain]")
        video_label = "vmain"

    if end_card_png and Path(end_card_png).is_file() and end_card_seconds > 0:
        cmd += ["-loop", "1", "-framerate", str(fps),
                "-t", str(end_card_seconds), "-i", str(end_card_png)]
        card_idx = inputs
        inputs += 1
        # Fade the card in so it does not cut abruptly from the last frame.
        filters.append(
            f"[{card_idx}:v]format=yuv420p,fade=t=in:st=0:d=0.6[vcard]"
        )
        filters.append(f"[{video_label}]format=yuv420p[vbody]")
        filters.append("[vbody][vcard]concat=n=2:v=1:a=0[vout]")
        video_label = "vout"

    audio_index: Optional[int] = None
    if audio_path and Path(audio_path).is_file():
        cmd += ["-i", str(audio_path)]
        audio_index = inputs
        inputs += 1

    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{video_label}]"]

    if audio_index is not None:
        cmd += ["-map", f"{audio_index}:a", "-c:a", settings.audio_codec,
                "-b:a", settings.audio_bitrate]
        # The end card outlives the narration, so let the video define length.
        cmd += ["-shortest"] if end_card_png is None else []

    cmd += settings.video_args(fps)
    cmd += [
        "-pix_fmt", settings.pix_fmt,
        # Tag the stream so players interpret the range and primaries the way
        # the filter graph produced them.
        "-color_range", "tv",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-movflags", "+faststart",
        str(out_path),
    ]

    log.info("encoding %s with %s", out_path.name, settings.encoder)
    _run(cmd, "final encode")
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise EncodeError(f"encode produced no output at {out_path}")
    return out_path


def make_preview_sheet(frames: Sequence[Path], out_path: Path,
                       columns: int = 3, width: int = 480) -> Optional[Path]:
    """Contact sheet of sample frames, shown in the dashboard during a render."""
    frames = [f for f in frames if Path(f).is_file()]
    if not frames:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = (len(frames) + columns - 1) // columns
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"]
    for f in frames:
        cmd += ["-i", str(f)]
    # `tile` consumes a single stream of frames, not N parallel inputs, so the
    # scaled stills are concatenated into one stream first. Heights are forced
    # even and identical, since concat rejects mismatched frame geometry.
    chain = "".join(
        f"[{i}:v]scale={width}:-2,setsar=1,format=rgb24[s{i}];"
        for i in range(len(frames))
    )
    refs = "".join(f"[s{i}]" for i in range(len(frames)))
    chain += f"{refs}concat=n={len(frames)}:v=1:a=0[cat];"
    chain += f"[cat]tile={columns}x{rows}[out]"
    cmd += ["-filter_complex", chain, "-map", "[out]", "-frames:v", "1", str(out_path)]
    try:
        _run(cmd, "preview sheet")
    except EncodeError as exc:
        log.warning("preview sheet failed: %s", exc)
        return None
    return out_path
