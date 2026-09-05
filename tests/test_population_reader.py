"""Verify the dependency-free Kontur GeoPackage reader.

The real Kontur dataset is a multi-gigabyte download, so this builds a small
but *structurally genuine* GeoPackage - correct `gpkg_contents`,
`gpkg_geometry_columns`, GPKG binary headers, WKB polygons and an R-tree
index - and reads it back through the production code path.

That exercises everything the real file would: header/envelope stripping, WKB
polygon and multipolygon parsing, R-tree bbox filtering, and population column
detection.

Run directly:  python tests/test_population_reader.py
"""
from __future__ import annotations

import math
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from worker.sources.population import (  # noqa: E402
    _parse_gpkg_blob, _read_wkb_polygons, build_for_bbox, read_hexagons,
    suggest_max_population,
)
from worker.tiles import BBox  # noqa: E402


def hexagon(cx: float, cy: float, r: float) -> list[list[float]]:
    """A closed flat-topped hexagon ring, like an H3 cell."""
    pts = []
    for i in range(6):
        a = math.radians(60 * i)
        pts.append([cx + r * math.cos(a), cy + r * math.sin(a) * 0.8])
    pts.append(pts[0])
    return pts


def wkb_polygon(ring: list[list[float]]) -> bytes:
    """Little-endian WKB Polygon with a single ring."""
    out = struct.pack("<BI", 1, 3) + struct.pack("<I", 1)
    out += struct.pack("<I", len(ring))
    for x, y in ring:
        out += struct.pack("<dd", x, y)
    return out


def wkb_multipolygon(rings: list[list[list[float]]]) -> bytes:
    out = struct.pack("<BI", 1, 6) + struct.pack("<I", len(rings))
    for ring in rings:
        out += wkb_polygon(ring)
    return out


def gpkg_blob(wkb: bytes, envelope: tuple | None = None) -> bytes:
    """Wrap WKB in a GeoPackage binary header."""
    if envelope:
        flags = 0b0000_0011           # little-endian, envelope code 1 (xy)
        header = b"GP" + bytes([0, flags]) + struct.pack("<i", 4326)
        header += struct.pack("<4d", *envelope)
    else:
        flags = 0b0000_0001           # little-endian, no envelope
        header = b"GP" + bytes([0, flags]) + struct.pack("<i", 4326)
    return header + wkb


def build_fixture(path: Path, cols: int = 24, rows: int = 24,
                  srs: int = 4326) -> dict:
    """Create a valid minimal GeoPackage of population hexagons.

    `srs` selects the coordinate system. Real Kontur data is EPSG:3857
    (metres), which is exactly the case that silently returned zero rows
    before the reader learned to reproject the query box.
    """
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute("PRAGMA application_id = 1196444487")   # 'GPKG'
    c.executescript(
        """
        CREATE TABLE gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY,
            organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
            definition TEXT NOT NULL, description TEXT);
        CREATE TABLE gpkg_contents (
            table_name TEXT PRIMARY KEY, data_type TEXT NOT NULL,
            identifier TEXT UNIQUE, description TEXT DEFAULT '',
            last_change DATETIME, min_x DOUBLE, min_y DOUBLE,
            max_x DOUBLE, max_y DOUBLE, srs_id INTEGER);
        CREATE TABLE gpkg_geometry_columns (
            table_name TEXT NOT NULL, column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL, m TINYINT NOT NULL,
            CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name));
        CREATE TABLE population (
            fid INTEGER PRIMARY KEY AUTOINCREMENT,
            h3 TEXT, population REAL, geom BLOB);
        CREATE VIRTUAL TABLE rtree_population_geom USING rtree(
            id, minx, maxx, miny, maxy);
        """
    )
    c.execute("INSERT INTO gpkg_spatial_ref_sys VALUES "
              "('WGS 84', 4326, 'EPSG', 4326, 'GEOGCS[\"WGS 84\"]', NULL)")
    c.execute("INSERT INTO gpkg_geometry_columns VALUES "
              "('population', 'geom', 'POLYGON', ?, 0, 0)", (srs,))

    west, south = 6.0, 45.0
    step = 0.05
    radius = step * 0.55
    expected_in_box = 0
    peak = 0.0
    probe = BBox(6.20, 45.20, 6.40, 45.40)

    for i in range(cols):
        for j in range(rows):
            cx = west + i * step
            cy = south + j * step
            ring = hexagon(cx, cy, radius)
            if srs == 3857:
                from worker.sources.population import _lnglat_to_3857
                ring = [list(_lnglat_to_3857(px, py)) for px, py in ring]
            # A population field with one dense cluster, so percentile logic
            # has something non-uniform to work with.
            d = math.hypot(i - cols * 0.35, j - rows * 0.6)
            pop = round(max(0.0, 9000 * math.exp(-(d ** 2) / 18.0)) + i + j, 2)
            peak = max(peak, pop)

            # Exercise both geometry types and both header shapes.
            if (i + j) % 17 == 0:
                wkb = wkb_multipolygon([ring])
            else:
                wkb = wkb_polygon(ring)
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            env = (min(xs), max(xs), min(ys), max(ys)) if (i % 2 == 0) else None
            blob = gpkg_blob(wkb, env)

            c.execute("INSERT INTO population (h3, population, geom) "
                      "VALUES (?,?,?)", (f"8a{i:03d}{j:03d}", pop, blob))
            rid = c.lastrowid
            c.execute("INSERT INTO rtree_population_geom VALUES (?,?,?,?,?)",
                      (rid, min(xs), max(xs), min(ys), max(ys)))
            if (max(xs) >= probe.west and min(xs) <= probe.east
                    and max(ys) >= probe.south and min(ys) <= probe.north):
                expected_in_box += 1

    c.execute("INSERT INTO gpkg_contents VALUES "
              "('population','features','population','',NULL,?,?,?,?,?)",
              (west, south, west + cols * step, south + rows * step, srs))
    conn.commit()
    conn.close()
    return {"total": cols * rows, "expected_in_box": expected_in_box,
            "probe": probe, "peak": peak}


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    tmp = Path(tempfile.mkdtemp())
    gpkg = tmp / "kontur_population_3km.gpkg"
    meta = build_fixture(gpkg)
    print(f"fixture: {meta['total']} hexagons, {gpkg.stat().st_size/1024:.0f} KB\n")

    print("unit: header + WKB parsing")
    ring = hexagon(7.0, 45.5, 0.02)
    for name, wkb in (("polygon", wkb_polygon(ring)),
                      ("multipolygon", wkb_multipolygon([ring]))):
        for env_label, env in (("no-envelope", None),
                               ("xy-envelope", (6.9, 7.1, 45.4, 45.6))):
            inner = _parse_gpkg_blob(gpkg_blob(wkb, env))
            polys = _read_wkb_polygons(inner) if inner else []
            ok = len(polys) == 1 and len(polys[0][0]) == 7
            check(f"{name} / {env_label}", ok,
                  f"rings={len(polys[0]) if polys else 0} "
                  f"pts={len(polys[0][0]) if polys else 0}")
    check("rejects non-GPKG blob", _parse_gpkg_blob(b"NOTGPKG") is None)

    print("\nreader: R-tree bbox query")
    probe = meta["probe"]
    rows = list(read_hexagons(gpkg, probe))
    check("returns cells", len(rows) > 0, f"got {len(rows)}")
    check("bbox filter excludes the rest",
          len(rows) < meta["total"], f"{len(rows)} of {meta['total']}")
    check("matches expected count",
          len(rows) == meta["expected_in_box"],
          f"got {len(rows)}, expected {meta['expected_in_box']}")
    check("populations are numeric and non-negative",
          all(isinstance(p, float) and p >= 0 for p, _ in rows))
    check("geometries are closed rings",
          all(g[0][0] == g[0][-1] for _, g in rows))

    print("\npercentile ceiling")
    ceiling = suggest_max_population(gpkg, BBox(6.0, 45.0, 7.2, 46.2))
    check("returns a ceiling", ceiling is not None, f"{ceiling}")
    check("below the peak (robust to outliers)",
          ceiling is not None and ceiling < meta["peak"],
          f"ceiling={ceiling:.0f} peak={meta['peak']:.0f}")

    print("\nbuild_for_bbox -> GeoJSON")

    class FakeCfg:
        def path(self, _name):
            return tmp

    (tmp / "kontur").mkdir(exist_ok=True)
    gpkg.replace(tmp / "kontur" / "kontur_population_3km.gpkg")
    extract = build_for_bbox(probe, tmp / "pop.geojson", FakeCfg())
    out = extract.path if extract else None
    check("wrote GeoJSON", out is not None and out.is_file())
    check("reports the dataset it used",
          extract is not None and extract.dataset.endswith(".gpkg"),
          extract.dataset if extract else "")
    check("colour ceiling comes from the data, not a constant",
          extract is not None and 0 < extract.ceiling <= extract.peak,
          f"ceiling={extract.ceiling:.0f} peak={extract.peak:.0f}" if extract else "")
    if out:
        import json
        doc = json.loads(out.read_text(encoding="utf-8"))
        check("FeatureCollection", doc.get("type") == "FeatureCollection")
        check("features carry population",
              all("population" in f["properties"] for f in doc["features"]),
              f"{len(doc['features'])} features")
        check("geometry is Polygon",
              all(f["geometry"]["type"] == "Polygon" for f in doc["features"]))

    print("\nEPSG:3857 dataset (real Kontur uses metres, not degrees)")
    merc = tmp / "merc.gpkg"
    build_fixture(merc, srs=3857)
    merc_rows = list(read_hexagons(merc, probe))
    check("R-tree query reprojects the bbox", len(merc_rows) > 0,
          f"got {len(merc_rows)} cells")
    check("count matches the EPSG:4326 fixture",
          len(merc_rows) == meta["expected_in_box"],
          f"{len(merc_rows)} vs {meta['expected_in_box']}")
    if merc_rows:
        vx, vy = merc_rows[0][1][0][0]
        check("output is degrees, not metres",
              -180 <= vx <= 180 and -90 <= vy <= 90, f"({vx:.4f}, {vy:.4f})")

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed: {failures}")
        return 1
    print("PASS: Kontur GeoPackage reader works without GDAL/GeoPandas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
