"""The prefetch plan must cover the extra zoom level terrain pulls in.

MapLibre does not request one zoom level per frame: it subdivides by distance,
so ground near the camera is fetched a level deeper than ground at the
horizon, and 3D terrain strengthens that because high ground stands closer to
the camera than the flat plane the frustum maths assumes.

Planning only the nominal zoom left those tiles to be fetched live in the
middle of the render, which the project's fair-use rule forbids. The four
tiles asserted below are the ones an 8-second Alps flyover actually missed,
on this machine and on a GitHub Actions runner, before the fix.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from worker.camera import auto_shots, build_track  # noqa: E402
from worker.frustum import camera_bbox, near_field_bbox  # noqa: E402
from worker.job import JobSpec  # noqa: E402
from worker.prefetch import TileSource, plan_tiles  # noqa: E402

WIDTH, HEIGHT, FPS = 852, 480, 24

# Observed live fetches during the render. The real basemap source prefixes
# these with a planet build id; the z/x/y triple is the part that matters.
OBSERVED_MISSES = [
    "11/1066/728.pbf", "11/1066/729.pbf",
    "11/1067/728.pbf", "11/1067/729.pbf",
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def alps_track():
    spec_data = json.loads((ROOT / "examples" / "alps.json").read_text(encoding="utf-8"))
    spec_data["narration"] = ""
    spec_data["duration_s"] = 8
    spec = JobSpec.from_dict(spec_data)
    shots = spec.shots or auto_shots(spec.box, width=WIDTH, height=HEIGHT,
                                     duration_s=spec.duration_s)
    return spec, build_track(shots, FPS)


def main() -> int:
    spec, track = alps_track()
    source = TileSource(name="basemap", upstream="ofm",
                        template="{z}/{x}/{y}.pbf",
                        tile_size=512, minzoom=0, maxzoom=14)

    print("The near field is a subset of the frame, nearer than its centre")
    cam = max(track, key=lambda c: c.pitch)
    full = camera_bbox(cam, WIDTH, HEIGHT)
    near = near_field_bbox(cam, WIDTH, HEIGHT, fraction=0.5)
    check("near field sits inside the frame footprint",
          (near.west >= full.west - 1e-9 and near.east <= full.east + 1e-9
           and near.south >= full.south - 1e-9 and near.north <= full.north + 1e-9),
          f"pitch {cam.pitch:.0f}")
    check("near field is genuinely smaller than the whole frame",
          (near.east - near.west) * (near.north - near.south)
          < (full.east - full.west) * (full.north - full.south))

    print("\nPlanning with and without terrain")
    flat = {p for _, p in plan_tiles(track, [source], width=WIDTH, height=HEIGHT,
                                     terrain=False)}
    terrain = {p for _, p in plan_tiles(track, [source], width=WIDTH, height=HEIGHT,
                                        terrain=True)}
    check("terrain plans a superset of the flat plan", flat <= terrain,
          f"flat {len(flat)}, terrain {len(terrain)}")

    print("\nEvery tile the render actually fetched live must now be planned")
    for path in OBSERVED_MISSES:
        check(f"planned {path}", path in terrain)

    print("\nThe extra level must not blow the plan up")
    growth = len(terrain) / max(1, len(flat))
    check("terrain plan stays within 3x the flat plan", growth < 3.0,
          f"{growth:.2f}x")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        return 1
    print("PASS: the prefetch plan covers terrain's extra near-field zoom level")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
