"""Word-level timestamps via Whisper forced alignment.

Kokoro returns audio but no word timings, and the beats timeline needs them to
line overlay changes up with the narration. faster-whisper runs locally and
free, and its `word_timestamps=True` mode gives per-word start/end.

Whisper transcribes what it *hears*, which will not always match the script
token for token, so the transcript's word times are mapped back onto the known
script words by sequence alignment. That keeps the script's exact wording
while borrowing Whisper's timing.

If Whisper is unavailable, `attach_estimated_timings` distributes each
sentence's duration across its words by length. That is noticeably less
accurate but keeps the pipeline running.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Optional, Sequence

from ..log import get as get_logger
from . import Segment, VoiceResult, Word

log = get_logger("tts.align")

_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_model_cache: dict[str, object] = {}


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _normalise(word: str) -> str:
    return "".join(_WORD_RE.findall(word.lower()))


def _load_model(model_size: str):
    if model_size not in _model_cache:
        from faster_whisper import WhisperModel
        log.info("loading faster-whisper %s (CPU int8)", model_size)
        # int8 on CPU: the GPU is busy rendering frames, and alignment of a
        # short narration is fast enough on CPU that contention is not worth it.
        _model_cache[model_size] = WhisperModel(model_size, device="cpu",
                                                compute_type="int8")
    return _model_cache[model_size]


def transcribe_words(audio_path: Path, model_size: str = "base.en"
                     ) -> list[Word]:
    """Whisper word timings for a whole audio file."""
    model = _load_model(model_size)
    segments, _info = model.transcribe(
        str(audio_path), word_timestamps=True, vad_filter=False,
        beam_size=1, condition_on_previous_text=False,
    )
    words: list[Word] = []
    for seg in segments:
        for w in (seg.words or []):
            token = w.word.strip()
            if token:
                words.append(Word(token, float(w.start), float(w.end)))
    return words


def attach_word_timings(result: VoiceResult, model_size: str = "base.en") -> None:
    """Transcribe the synthesised audio and map timings onto the script."""
    if not is_available():
        raise RuntimeError("faster-whisper is not installed")

    heard = transcribe_words(result.audio_path, model_size)
    if not heard:
        raise RuntimeError("Whisper returned no words")

    result.aligned_with = f"whisper:{model_size}"
    result.words = []

    for segment in result.segments:
        # Only consider words Whisper placed inside this sentence's span; the
        # sentence boundaries are exact because each was synthesised alone.
        window = [w for w in heard
                  if w.start >= segment.start - 0.12
                  and w.end <= segment.end + 0.12]
        script_words = segment.text.split()
        segment.words = _map_to_script(script_words, window,
                                       segment.start, segment.end)
        result.words.extend(segment.words)

    log.info("aligned %d script words against %d transcribed words",
             len(result.words), len(heard))


def _map_to_script(script_words: Sequence[str], heard: Sequence[Word],
                   start: float, end: float) -> list[Word]:
    """Give every script word a start/end using the transcript's timings."""
    if not script_words:
        return []
    if not heard:
        return _proportional(script_words, start, end)

    a = [_normalise(w) for w in script_words]
    b = [_normalise(w.word) for w in heard]
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)

    out: list[Optional[Word]] = [None] * len(script_words)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            src = heard[block.b + k]
            out[block.a + k] = Word(script_words[block.a + k], src.start, src.end)

    # Interpolate any word Whisper did not match (mis-hearings, numerals).
    _fill_gaps(out, script_words, start, end)
    return [w for w in out if w is not None]


def _fill_gaps(out: list[Optional[Word]], script_words: Sequence[str],
               start: float, end: float) -> None:
    n = len(out)
    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        left = out[i - 1].end if i > 0 and out[i - 1] else start
        right = out[j].start if j < n and out[j] else end
        span = max(1e-3, right - left)
        # Share the gap by word length, so long words get proportionally more.
        weights = [max(1, len(script_words[k])) for k in range(i, j)]
        total = sum(weights)
        cursor = left
        for k, weight in zip(range(i, j), weights):
            width = span * weight / total
            out[k] = Word(script_words[k], cursor, cursor + width)
            cursor += width
        i = j


def _proportional(script_words: Sequence[str], start: float,
                  end: float) -> list[Word]:
    weights = [max(1, len(w)) for w in script_words]
    total = sum(weights)
    span = max(1e-3, end - start)
    out: list[Word] = []
    cursor = start
    for word, weight in zip(script_words, weights):
        width = span * weight / total
        out.append(Word(word, cursor, cursor + width))
        cursor += width
    return out


def attach_estimated_timings(result: VoiceResult) -> None:
    """Fallback: split each sentence's known duration across its words."""
    result.aligned_with = "estimated"
    result.words = []
    for segment in result.segments:
        segment.words = _proportional(segment.text.split(),
                                      segment.start, segment.end)
        result.words.extend(segment.words)
    log.info("estimated timings for %d words (no forced alignment)",
             len(result.words))
