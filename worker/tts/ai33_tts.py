"""AI33 / OpenSpeaker (https://ai33.pro) - optional, PAID voiceover provider.

A large voice library (ElevenLabs, Minimax, Edge, Fish Audio) behind one
endpoint. It is the one provider here that costs money: every task spends
credits, which are logged and recorded in the render's stats. It is never the
default and never selected implicitly - Kokoro stays the free local default.

The reason it is worth having beyond the voices is word timing. The API
returns word-level timestamps from the TTS engine itself, which are exact and
keep proper nouns intact. Whisper transcribes what it hears, and it mangles
exactly the place names the overlay cues depend on ("Zermatt" -> "Sermot").

The whole narration is sent as one task rather than per sentence: one request,
one charge, and natural prosody across sentence boundaries. Sentence segments
for the beats timeline are then recovered from the native word timings.

Everything is asynchronous: POST returns a task id, collected by polling.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional, Sequence
from urllib.parse import urlencode

import numpy as np

from ..env import load_env
from ..log import get as get_logger
from . import Segment, VoiceResult, Word
from .catalog import AI33_VOICES as VOICES, ai33_available  # noqa: F401

log = get_logger("tts.ai33")

SAMPLE_RATE = 24000
DEFAULT_BASE_URL = "https://api.ai33.pro"
POLL_INTERVAL_S = 3.0
TIMEOUT_S = 600.0
USER_AGENT = "spatialdata-worker/1.0"
CRLF = "\r\n"


class AI33Error(RuntimeError):
    pass


def is_available() -> bool:
    return ai33_available()


def _base_url() -> str:
    load_env()
    return os.environ.get("AI33_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    load_env()
    key = os.environ.get("AI33_API_KEY", "").strip()
    if not key:
        raise AI33Error(
            "AI33 is selected but AI33_API_KEY is not set. Add it to .env in "
            "the project root (key from https://ai33.pro -> API Key), or use "
            "the free local 'kokoro' provider.")
    return key


def _request(method: str, url: str, *, body: Optional[bytes] = None,
             content_type: Optional[str] = None, auth: bool = True,
             timeout: float = 60.0) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if auth:
        headers["xi-api-key"] = _api_key()
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        try:
            parsed = json.loads(detail)
            detail = parsed.get("message") or parsed.get("error") or detail
        except (ValueError, AttributeError):
            pass
        # Never echo request headers: they carry the key.
        raise AI33Error(f"AI33 {method} {url.split('?')[0]} failed "
                        f"(HTTP {exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise AI33Error(f"AI33 unreachable: {exc.reason}") from None


def _multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----spatialdata{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}{CRLF}"
                     f'Content-Disposition: form-data; name="{name}"{CRLF}{CRLF}'
                     f"{value}{CRLF}")
    parts.append(f"--{boundary}--{CRLF}")
    return "".join(parts).encode("utf-8"), f"multipart/form-data; boundary={boundary}"


def list_voices(provider: str = "edge", *, query: Optional[str] = None,
                language: Optional[str] = None, page_size: int = 60) -> list[dict]:
    """Voices from the live library. `provider` is required by the API:
    elevenlabs, minimax, clone, edge, kokoro, vbee or fishaudio."""
    params = {"provider": provider, "page_size": str(page_size)}
    if query:
        params["q"] = query
    if language:
        params["language"] = language
    raw = _request("GET", f"{_base_url()}/v3/voices?{urlencode(params)}")
    data = json.loads(raw).get("data") or []
    return [{"id": v.get("voice_id"), "label": v.get("name"),
             "language": v.get("language"), "gender": v.get("gender"),
             "provider": provider} for v in data]


def _wait(task_id: str, on_progress: Optional[Callable[[float, str], None]]) -> dict:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        task = json.loads(_request("GET", f"{_base_url()}/v1/task/{task_id}"))
        status = task.get("status")
        if status == "done":
            return task
        if status in ("error", "failed"):
            raise AI33Error("AI33 task failed: "
                            f"{task.get('error_message') or 'unknown error'}")
        if on_progress:
            pct = task.get("progress")
            frac = float(pct) / 100.0 if isinstance(pct, (int, float)) else 0.3
            on_progress(0.1 + 0.6 * max(0.0, min(1.0, frac)), f"ai33 task {status}")
        time.sleep(POLL_INTERVAL_S)
    raise AI33Error(f"AI33 task {task_id} timed out after {TIMEOUT_S:.0f}s")


def parse_transcript(payload) -> list[Word]:
    """AI33's transcript into Words.

    The API interleaves `spacing` entries between words and names the token
    `text`; both differ from the pipeline's shape. The payload may be one
    segment object or a list of them.
    """
    segments = payload if isinstance(payload, list) else [payload]
    words: list[Word] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        for w in seg.get("words") or []:
            if w.get("type") and w.get("type") != "word":
                continue
            token = str(w.get("text", "")).strip()
            if not token:
                continue
            try:
                start, end = float(w.get("start", 0)), float(w.get("end", 0))
            except (TypeError, ValueError):
                continue
            words.append(Word(token, start, max(start, end)))
    return words


def segments_from_words(sentences: Sequence[str], heard: Sequence[Word],
                        duration: float) -> list[Segment]:
    """Recover sentence segments from word timings for the whole narration.

    Timings are mapped onto the script's own words by sequence alignment, so
    the script's spelling survives, and a word the engine split or merged
    does not shift every later sentence.
    """
    from .align import _map_to_script

    script_words = [w for s in sentences for w in s.split()]
    mapped = _map_to_script(script_words, list(heard), 0.0, max(duration, 1e-3))

    segments: list[Segment] = []
    cursor = 0
    for i, sentence in enumerate(sentences):
        count = len(sentence.split())
        words = mapped[cursor:cursor + count]
        cursor += count
        if not words:
            continue
        start = words[0].start
        end = max(w.end for w in words)
        if segments and start < segments[-1].end:
            start = segments[-1].end
        segments.append(Segment(index=i, text=sentence, start=start,
                                end=max(start, end), words=list(words)))
    return segments


def _decode_to_wav(src: Path, dest: Path) -> float:
    """Decode the provider's MP3 to mono PCM at the pipeline's sample rate,
    peak-normalised the same way Kokoro output is. Returns the duration."""
    import soundfile as sf

    from ..encode import ffmpeg_bin

    tmp = dest.with_suffix(".decoded.wav")
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", str(tmp)],
        capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.is_file():
        raise AI33Error(f"could not decode AI33 audio: {proc.stderr.strip()[:300]}")
    audio, _sr = sf.read(str(tmp), dtype="float32")
    tmp.unlink(missing_ok=True)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio * (0.89 / peak)
    sf.write(str(dest), audio, SAMPLE_RATE)
    return len(audio) / SAMPLE_RATE


def synthesize(sentences: Sequence[str], out_path: Path, *,
               voice: str = "edge_en-US-AriaNeural", cfg=None,
               on_progress: Optional[Callable[[float, str], None]] = None
               ) -> VoiceResult:
    text = " ".join(s.strip() for s in sentences if s.strip())
    if not text:
        raise AI33Error("narration text is empty")
    speed = float(cfg.get("tts.speed", 1.0)) if cfg else 1.0

    body, ctype = _multipart({"text": text, "voice_id": voice,
                              "speed": str(speed), "with_transcript": "true"})
    created = json.loads(_request("POST", f"{_base_url()}/v3/text-to-speech",
                                  body=body, content_type=ctype))
    task_id = created.get("task_id")
    if not created.get("success") or not task_id:
        raise AI33Error(f"AI33 did not return a task id: {json.dumps(created)[:200]}")
    log.info("ai33: tts task %s (voice %s, %d chars)", task_id, voice, len(text))
    if on_progress:
        on_progress(0.1, "ai33 task queued")

    task = _wait(task_id, on_progress)
    meta = task.get("metadata") or {}
    if not meta.get("audio_url"):
        raise AI33Error("AI33 finished but returned no audio_url")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mp3 = out_path.with_name(out_path.stem + "_ai33.mp3")
    mp3.write_bytes(_request("GET", meta["audio_url"], auth=False, timeout=120))
    duration = _decode_to_wav(mp3, out_path)

    heard: list[Word] = []
    aligned_with = "ai33:native"
    if meta.get("json_url"):
        try:
            heard = parse_transcript(json.loads(
                _request("GET", meta["json_url"], auth=False)))
        except (AI33Error, ValueError) as exc:
            log.warning("ai33 transcript unavailable (%s)", exc)
    if not heard:
        # Timings are the bonus, not the product: fall back to Whisper.
        from . import align
        if align.is_available():
            model = cfg.get("tts.whisper_model", "base.en") if cfg else "base.en"
            heard = align.transcribe_words(out_path, model)
            aligned_with = f"whisper:{model}"
        else:
            aligned_with = "estimated"

    if on_progress:
        on_progress(0.85, "mapping word timings")
    segments = segments_from_words(list(sentences), heard, duration)
    words = [w for seg in segments for w in seg.words]

    credits = (float(task.get("credit_cost") or 0)
               + float(meta.get("transcript_credit_cost") or 0))
    log.info("ai33: %.2fs, %d words (%s), %.0f credits",
             duration, len(words), aligned_with, credits)

    return VoiceResult(
        audio_path=out_path, sample_rate=SAMPLE_RATE, duration_s=duration,
        provider="ai33", voice=voice, segments=segments, words=words,
        aligned_with=aligned_with, credits=credits)
