"""Verify worker/frustum.py reproduces MapLibre's own camera projection.

The prefetch stage decides which tiles to download from the Python frustum
model. If that model disagrees with MapLibre, the render either stalls on
missing tiles or we waste an upstream's bandwidth. So rather than trusting the
maths, this drives a real MapLibre instance with an empty style (no network)
and compares `map.unproject()` against the Python result.

Run directly:  python tests/test_frustum_matches_maplibre.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from worker.camera import CameraState  # noqa: E402
from worker.frustum import DEFAULT_FOV, ground_points  # noqa: E402

WIDTH, HEIGHT = 1280, 720

CASES = [
    CameraState(lng=-0.09, lat=51.485, zoom=11.5, bearing=0, pitch=0),
    CameraState(lng=-0.09, lat=51.485, zoom=11.5, bearing=22, pitch=45),
    CameraState(lng=-0.09, lat=51.485, zoom=11.5, bearing=22, pitch=60),
    CameraState(lng=7.6584, lat=45.9763, zoom=9.0, bearing=140, pitch=55),
    CameraState(lng=139.7, lat=35.68, zoom=13.0, bearing=-75, pitch=30),
    CameraState(lng=-74.006, lat=40.7128, zoom=6.0, bearing=200, pitch=65),
]

# Screen-space sample points, as fractions of the viewport.
CORNERS = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.5, 0.5)]

PAGE = """
() => {
  const map = new maplibregl.Map({
    container: 'map',
    style: {version: 8, sources: {}, layers: []},
    center: [0, 0], zoom: 2, interactive: false, attributionControl: false,
    // Must match map.html: MapLibre's default maxPitch is 60, which would
    // silently clamp the steep cases and make this comparison meaningless.
    maxPitch: 85
  });
  window.__m = map;
  return new Promise(r => map.once('load', () => r(true)));
}
"""

PROBE = """
([cam, pts, w, h]) => {
  const map = window.__m;
  map.jumpTo({center: [cam.lng, cam.lat], zoom: cam.zoom,
              bearing: cam.bearing, pitch: cam.pitch});
  return pts.map(([fx, fy]) => {
    const ll = map.unproject([fx * w, fy * h]);
    return [ll.lng, ll.lat];
  });
}
"""


def main() -> int:
    html = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<link rel=stylesheet href="vendor/maplibre-gl.css">
<style>html,body{{margin:0}}#map{{position:absolute;inset:0;width:{WIDTH}px;height:{HEIGHT}px}}</style>
</head><body><div id=map></div>
<script src="vendor/maplibre-gl.js"></script></body></html>"""
    tmp = ROOT / "worker" / "render" / "_frustum_probe.html"
    tmp.write_text(html, encoding="utf-8")

    failures: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--use-angle=d3d11", "--hide-scrollbars", "--force-color-profile=srgb"],
            )
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            page.goto(tmp.as_uri(), wait_until="domcontentloaded")
            page.evaluate(PAGE)

            print(f"{'case':>34}  {'corner':>8}  {'dlng':>9}  {'dlat':>9}")
            for cam in CASES:
                actual = page.evaluate(PROBE, [cam.as_dict(), CORNERS, WIDTH, HEIGHT])
                # ground_points samples a 5x5 grid; pull the matching entries.
                mine = _sample_corners(cam)
                label = f"z{cam.zoom:g} b{cam.bearing:g} p{cam.pitch:g}"
                for (name, want), got in zip(zip(_CORNER_NAMES, actual), mine):
                    dlng = _lng_delta(want[0], got[0])
                    dlat = want[1] - got[1]
                    ok = abs(dlng) < 0.02 and abs(dlat) < 0.02
                    if not ok:
                        failures.append(f"{label} {name}: "
                                        f"maplibre=({want[0]:.5f},{want[1]:.5f}) "
                                        f"python=({got[0]:.5f},{got[1]:.5f})")
                    flag = "" if ok else "   <-- MISMATCH"
                    print(f"{label:>34}  {name:>8}  {dlng:9.5f}  {dlat:9.5f}{flag}")
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)

    print()
    if failures:
        print(f"FAIL: {len(failures)} corner(s) disagree with MapLibre")
        for f in failures[:10]:
            print("  ", f)
        return 1
    print("PASS: python frustum matches MapLibre unproject on all cases")
    return 0


_CORNER_NAMES = ["TL", "TR", "BL", "BR", "C"]


def _sample_corners(cam: CameraState) -> list[tuple[float, float]]:
    """Pull the 4 corners + centre out of the 5x5 ground_points grid."""
    grid = ground_points(cam, WIDTH, HEIGHT, samples=5)
    # ground_points iterates i over x (columns) then j over y (rows).
    def at(ix: int, iy: int) -> tuple[float, float]:
        return grid[ix * 5 + iy]
    return [at(0, 0), at(4, 0), at(0, 4), at(4, 4), at(2, 2)]


def _lng_delta(a: float, b: float) -> float:
    """Longitude difference, wrapped to [-180, 180]."""
    return ((a - b + 180.0) % 360.0) - 180.0


if __name__ == "__main__":
    raise SystemExit(main())
