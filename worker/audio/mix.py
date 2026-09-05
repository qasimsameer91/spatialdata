"""Audio bed: narration over music, with the music ducked under the voice.

Uses FFmpeg's `sidechaincompress`, which drives a compressor on the music from
the voice signal. That ducks the bed only while someone is actually speaking
and lets it swell back in the gaps, which is what makes a documentary mix feel
deliberate rather than merely quiet.

Handles the usual length mismatches: music shorter than the video is looped,
music longer is trimmed, and everything is faded so nothing ends abruptly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from ..encode import EncodeError, ffmpeg_bin, probe
from ..log import get as get_logger

log = get_logger("audio.mix")

FADE_IN_S = 1.2
FADE_OUT_S = 2.5


def _run(cmd: list[str], what: str) -> None:
    log.debug("%s: %s", what, " ".join(str(c) for c in cmd))
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-12:])
        raise EncodeError(f"{what} failed (exit {result.returncode}):\n{tail}")


def duration_of(path: Path) -> float:
    info = probe(Path(path))
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "audio" and stream.get("duration"):
            return float(stream["duration"])
    return float(info.get("format", {}).get("duration", 0.0))


def build_track(*, voice_path: Optional[Path], music_path: Optional[Path],
                out_path: Path, target_s: float, cfg=None) -> Optional[Path]:
    """Mix narration and music into one WAV of exactly `target_s` seconds."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if target_s <= 0:
        raise ValueError(
            f"audio target_s must be positive, got {target_s}. A zero or "
            "negative duration produces an ffmpeg filter error rather than a "
            "usable track."
        )
    has_voice = voice_path is not None and Path(voice_path).is_file()
    has_music = music_path is not None and Path(music_path).is_file()
    if not has_voice and not has_music:
        return None

    def setting(key: str, default: float) -> float:
        return float(cfg.get(f"audio.{key}", default)) if cfg else default

    # Music is loudness-normalised before the bed gain is applied. A fixed dB
    # offset cannot work on its own: the bundled tracks alone span -25.6 to
    # -19.3 LUFS, so one -22 dB setting leaves one track sitting nicely under
    # the narration and makes another inaudible. Normalising first makes the
    # bed predictable whatever track is dropped into data/music.
    music_lufs = setting("music_lufs", -20.0)
    music_gain = setting("music_gain_db", -12.0)
    duck_gain = setting("duck_gain_db", -14.0)
    voice_gain = setting("voice_gain_db", 0.0)
    attack = setting("duck_attack_ms", 200)
    release = setting("duck_release_ms", 700)

    cmd: list[str] = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"]
    filters: list[str] = []
    idx = 0
    voice_idx = music_idx = None

    if has_voice:
        cmd += ["-i", str(voice_path)]
        voice_idx = idx
        idx += 1
    if has_music:
        # Loop the bed so a short track still covers a long video; the trim
        # below cuts it back to length.
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]
        music_idx = idx
        idx += 1

    fade_out_start = max(0.0, target_s - FADE_OUT_S)

    if has_voice and has_music:
        # The voice feeds both the mix and the compressor's sidechain input.
        filters.append(
            f"[{voice_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            f"channel_layouts=stereo,volume={voice_gain}dB,"
            f"apad=whole_dur={target_s},atrim=0:{target_s},asplit=2[vmix][vkey]"
        )
        filters.append(
            f"[{music_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            f"channel_layouts=stereo,"
            f"loudnorm=I={music_lufs}:TP=-3:LRA=11,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:"
            f"channel_layouts=stereo,volume={music_gain}dB,"
            f"atrim=0:{target_s}[bed]"
        )
        # threshold/ratio chosen so speech pulls the bed down by roughly
        # (duck_gain - music_gain) dB without pumping on sibilants.
        makeup = max(0.1, min(4.0, 10 ** ((duck_gain - music_gain) / 20.0)))
        filters.append(
            f"[bed][vkey]sidechaincompress=threshold=0.02:ratio=12:"
            f"attack={attack}:release={release}:makeup=1:level_sc=1[ducked]"
        )
        filters.append(f"[ducked]volume={makeup}[bedduck]")
        filters.append(
            f"[vmix][bedduck]amix=inputs=2:duration=first:dropout_transition=0:"
            f"normalize=0[mixed]"
        )
        chain_in = "mixed"
    elif has_voice:
        filters.append(
            f"[{voice_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            f"channel_layouts=stereo,volume={voice_gain}dB,"
            f"apad=whole_dur={target_s},atrim=0:{target_s}[mixed]"
        )
        chain_in = "mixed"
    else:
        filters.append(
            f"[{music_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
            f"channel_layouts=stereo,"
            # No narration to duck under, so the bed sits louder.
            f"loudnorm=I={music_lufs}:TP=-3:LRA=11,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:"
            f"channel_layouts=stereo,volume={music_gain / 2.0}dB,"
            f"atrim=0:{target_s}[mixed]"
        )
        chain_in = "mixed"

    filters.append(
        f"[{chain_in}]afade=t=in:st=0:d={FADE_IN_S},"
        f"afade=t=out:st={fade_out_start:.3f}:d={FADE_OUT_S},"
        # A limiter catches the moment voice and an un-ducked swell collide.
        f"alimiter=limit=0.97:level=disabled[out]"
    )

    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]",
            "-t", f"{target_s:.3f}", "-c:a", "pcm_s16le", "-ar", "48000",
            "-ac", "2", str(out_path)]

    log.info("mixing audio (voice=%s music=%s) to %.2fs",
             has_voice, has_music, target_s)
    _run(cmd, "audio mix")
    if not out_path.is_file():
        raise EncodeError(f"audio mix produced no output at {out_path}")
    return out_path


def list_music(data_dir: Path) -> list[dict]:
    """Bundled royalty-free tracks available to the dashboard."""
    music_dir = Path(data_dir) / "music"
    if not music_dir.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(music_dir.iterdir()):
        if path.suffix.lower() not in (".mp3", ".wav", ".flac", ".ogg", ".m4a"):
            continue
        entry = {"id": path.name, "label": path.stem.replace("_", " ").title()}
        try:
            entry["duration_s"] = round(duration_of(path), 1)
        except Exception:
            entry["duration_s"] = None
        out.append(entry)
    return out
