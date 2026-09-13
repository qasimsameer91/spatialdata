"""AI33 word timings survive into the beats timeline intact.

No network and no credits: this exercises the parsing and segment recovery
against the transcript shape the API returns, including the `spacing` entries
it interleaves and a proper noun that Whisper is known to mangle.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker.tts import VoiceResult, split_sentences  # noqa: E402
from worker.tts.ai33_tts import parse_transcript, segments_from_words  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def word(text, start, end):
    return {"type": "word", "text": text, "start": start, "end": end}


def space(start):
    return {"type": "spacing", "text": " ", "start": start, "end": start + 0.01}


def main() -> int:
    script = ("The Haute Route begins in Chamonix. "
              "It ends in Zermatt, below the Matterhorn.")
    sentences = split_sentences(script)
    tokens = [("The", .10, .25), ("Haute", .26, .51), ("Route", .52, .80),
              ("begins", .81, 1.18), ("in", 1.20, 1.28), ("Chamonix.", 1.30, 1.88),
              ("It", 2.30, 2.41), ("ends", 2.42, 2.70), ("in", 2.71, 2.80),
              ("Zermatt,", 2.82, 3.40), ("below", 3.50, 3.80), ("the", 3.81, 3.90),
              ("Matterhorn.", 3.91, 4.70)]
    entries = []
    for t in tokens:
        entries += [word(*t), space(t[2])]

    print("Transcript parsing")
    words = parse_transcript({"words": entries})
    check("spacing entries are dropped", len(words) == len(tokens), f"{len(words)} words")
    check("a list of segments parses the same",
          len(parse_transcript([{"words": entries}])) == len(tokens))
    check("malformed timings are skipped, not fatal",
          len(parse_transcript({"words": [word("x", "bad", 1), word("ok", 1, 2)]})) == 1)

    print("\nSentence segments recovered from whole-narration timings")
    segs = segments_from_words(sentences, words, duration=5.0)
    check("one segment per sentence", len(segs) == 2, f"{len(segs)}")
    check("first sentence spans its words", abs(segs[0].start - .10) < 1e-6
          and abs(segs[0].end - 1.88) < 1e-6, f"{segs[0].start}-{segs[0].end}")
    check("second sentence starts on its first word", abs(segs[1].start - 2.30) < 1e-6)
    names = [w.word for w in segs[1].words]
    check("proper nouns keep the script's spelling", "Zermatt," in names, str(names))

    print("\nA word the engine split does not shift later sentences")
    split = [w for w in words if w.word != "Chamonix."]
    from worker.tts import Word
    split[5:5] = [Word("Chamo", 1.30, 1.60), Word("nix.", 1.60, 1.88)]
    segs = segments_from_words(sentences, split, duration=5.0)
    check("second sentence still starts at 2.30", abs(segs[1].start - 2.30) < 1e-6,
          f"{segs[1].start}")

    print("\nCredits are recorded, and absent for free providers")
    r = VoiceResult(audio_path=Path("v.wav"), sample_rate=24000, duration_s=1,
                    provider="ai33", voice="edge_en-US-AriaNeural", credits=57.0)
    check("credits in as_dict", r.as_dict()["credits"] == 57.0)
    check("free providers report none",
          VoiceResult(Path("v.wav"), 24000, 1, "kokoro", "af_heart").as_dict()["credits"] is None)

    print("\nWithout a key the provider is unavailable")
    saved = os.environ.pop("AI33_API_KEY", None)
    try:
        import worker.env as env
        env._loaded = True          # do not read the real .env in this check
        from worker.tts.catalog import ai33_available
        check("unavailable with no key", ai33_available() is False)
    finally:
        if saved is not None:
            os.environ["AI33_API_KEY"] = saved

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        return 1
    print("PASS: AI33 transcripts map onto the script and its sentences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
