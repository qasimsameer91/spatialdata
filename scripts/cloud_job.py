#!/usr/bin/env python3
"""Resolve a workflow_dispatch job input into a validated job file.

The dispatch input is either the name of a file in examples/ ("indus") or a
whole job document pasted as JSON. Both end up as one file on disk that
`python -m worker render --job` can take, validated here so a typo fails in
two seconds rather than after the data download.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from worker.job import JobSpec  # noqa: E402

EXAMPLES = ROOT / "examples"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def resolve(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        raise SystemExit("no job given")

    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"job input is not valid JSON: {exc}") from None

    name = raw[:-5] if raw.endswith(".json") else raw
    name = name.rsplit("/", 1)[-1]
    if not _NAME_RE.match(name):
        raise SystemExit(f"{raw!r} is neither JSON nor an example name")
    path = EXAMPLES / f"{name}.json"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in EXAMPLES.glob("*.json")))
        raise SystemExit(f"no example named {name!r}. Available: {available}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    data = resolve(sys.argv[1] if len(sys.argv) > 1 else "")
    # Not jobs/ -- the JobStore keeps its own <job-id>.json state files there
    # and would overwrite this one the moment the render starts.
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / ".render" / "job.json"

    try:
        spec = JobSpec.from_dict(data)      # raises on anything malformed
    except (ValueError, TypeError) as exc:
        raise SystemExit(f"job is not renderable: {exc}") from None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Which population dataset the run needs, so the workflow downloads only
    # that one. 400m unpacks to ~9 GB and will not fit a runner's disk.
    span = max(spec.bbox[2] - spec.bbox[0], spec.bbox[3] - spec.bbox[1])
    kontur = "22km" if span > 20 else "3km"

    print(f"job      {spec.place_name or '(unnamed)'}")
    print(f"bbox     {spec.bbox}  (span {span:.2f} deg)")
    print(f"overlays {', '.join(spec.overlays) or 'none'}")
    print(f"asked    {spec.width}x{spec.height}@{spec.fps}, {spec.duration_s}s")
    print(f"terrain  {'3D on' if spec.terrain else 'flat'}")
    print(f"written  {out}")

    needs_pop = "population" in spec.overlays
    slug = re.sub(r"[^a-z0-9]+", "-", (spec.place_name or "render").lower()).strip("-")
    outputs = {
        "job_file": str(out),
        "kontur": kontur if needs_pop else "",
        "needs_population": str(needs_pop).lower(),
        "slug": slug or "render",
    }
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        lines = [f"{key}={value}" for key, value in outputs.items()]
        with open(gh, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        for key, value in outputs.items():
            print(f"  [output] {key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
