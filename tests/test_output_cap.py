"""The output ceiling that lets one job file render on a GPU box and a runner.

A CPU-only runner cannot finish 1920x1080 with 3D terrain in any sane time,
so config/cloud.json caps the output. The job file must not need editing for
that, and the cap must not distort the picture or emit a size h264 rejects.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker.config import Config, DEFAULTS  # noqa: E402
from worker.job import JobSpec  # noqa: E402
from worker.pipeline import Pipeline  # noqa: E402

BOX = [7.30, 45.75, 8.20, 46.30]
failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}  got {got}")
    if not ok:
        failures.append(f"{label}: got {got}, want {want}")


def pipeline_with(**render) -> Pipeline:
    import copy
    data = copy.deepcopy(DEFAULTS)
    data["render"].update(render)
    pipe = Pipeline.__new__(Pipeline)     # no I/O; only _capped_output is used
    pipe.cfg = Config(data)
    return pipe


def main() -> int:
    print("No ceiling configured")
    plain = pipeline_with()
    check("the job's own size is used",
          plain._capped_output(JobSpec(bbox=BOX, width=1920, height=1080, fps=30)),
          (1920, 1080, 30))

    print("\nCloud ceiling: 854x480 @24")
    cloud = pipeline_with(max_width=854, max_height=480, max_fps=24)

    got = cloud._capped_output(JobSpec(bbox=BOX, width=1920, height=1080, fps=30))
    check("1080p landscape is scaled down", got, (852, 480, 24))
    # Rounding both dimensions to even shifts the ratio very slightly; a
    # visible distortion would be far larger than this.
    check("aspect ratio is preserved to within 1%",
          abs(got[0] / got[1] - 16 / 9) < 0.0178, True)

    got = cloud._capped_output(JobSpec(bbox=BOX, width=1080, height=1920, fps=30))
    check("portrait is capped on its long side", got, (270, 480, 24))

    check("a job already under the ceiling is untouched",
          cloud._capped_output(JobSpec(bbox=BOX, width=640, height=360, fps=24)),
          (640, 360, 24))

    check("a slower job is not sped up to the fps ceiling",
          cloud._capped_output(JobSpec(bbox=BOX, width=640, height=360, fps=12))[2],
          12)

    print("\nEvery capped size must stay legal for h264 and for JobSpec")
    for w, h in ((1920, 1080), (1280, 720), (1080, 1920), (3840, 2160), (854, 481 - 1)):
        cw, ch, _ = cloud._capped_output(JobSpec(bbox=BOX, width=w, height=h, fps=30))
        ok = cw % 2 == 0 and ch % 2 == 0 and cw >= 160 and ch >= 160
        print(f"  {'PASS' if ok else 'FAIL'}  {w}x{h} -> {cw}x{ch}")
        if not ok:
            failures.append(f"{w}x{h} capped to an illegal {cw}x{ch}")

    print()
    if failures:
        for line in failures:
            print("FAILED:", line)
        return 1
    print("PASS: output ceiling scales jobs without distorting or breaking them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
