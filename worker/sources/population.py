"""Kontur Population (CC BY) -> clipped GeoJSON for the population overlay.

Kontur ships as a GeoPackage of H3 hexagons with a `population` count per cell.
A GeoPackage is a SQLite database, and its geometry blobs are a small binary
header followed by standard WKB, so this module reads it with `sqlite3` and a
compact WKB parser rather than pulling GDAL/Fiona/GeoPandas onto the render
worker. That keeps the worker's dependency set small and avoids the usual
GDAL-on-Windows install pain.

Fair use: the dataset is downloaded **once** by `data/download_data.py` and
read from local disk thereafter. Nothing here touches the network, and the
per-job extract is cached so re-rendering the same region costs nothing.
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ..log import get as get_logger
from ..tiles import BBox

log = get_logger("sources.population")

# Files download_data.py produces, finest first: a city scene wants 400m cells,
# but any of these works and a coarser file beats no overlay at all.
KONTUR_CANDIDATES = (
    "kontur_population_400m.gpkg",
    "kontur_population_3km.gpkg",
    "kontur_population_22km.gpkg",
)

# WKB geometry type codes we understand.
_WKB_POLYGON = 3
_WKB_MULTIPOLYGON = 6


def kontur_dir(cfg) -> Path:
    return cfg.path("data") / "kontur"


# Approximate cell width of each build, used to match dataset to view scale.
CELL_KM = {
    "kontur_population_400m.gpkg": 0.4,
    "kontur_population_3km.gpkg": 3.0,
    "kontur_population_22km.gpkg": 22.0,
}
#: A density surface reads as data only when cells are small relative to the
#: frame. Below roughly this many cells across, the hexagons stop looking like
#: a measurement and start looking like abstract shapes covering the terrain.
MIN_CELLS_ACROSS = 35


def find_bundled(cfg, prefer: Optional[str] = None) -> Optional[Path]:
    """Locate a bundled Kontur GeoPackage."""
    root = kontur_dir(cfg)
    names = (prefer,) + KONTUR_CANDIDATES if prefer else KONTUR_CANDIDATES
    for name in names:
        if not name:
            continue
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def available_datasets(cfg) -> list[Path]:
    root = kontur_dir(cfg)
    return [root / n for n in KONTUR_CANDIDATES if (root / n).is_file()]


def bbox_width_km(bbox: BBox) -> float:
    mid_lat = (bbox.south + bbox.north) / 2.0
    return abs(bbox.east - bbox.west) * 111.32 * math.cos(math.radians(mid_lat))


def choose_dataset(cfg, bbox: BBox,
                   prefer: Optional[str] = None) -> Optional[Path]:
    """Pick the Kontur build whose cell size suits this view.

    3km hexagons over a 30km-wide shot are each a tenth of the frame, which
    reads as abstract colour rather than population. Prefer the finest build
    that is actually installed, and fall back gracefully when it is not.
    """
    if prefer:
        explicit = kontur_dir(cfg) / prefer
        if explicit.is_file():
            return explicit
        log.warning("requested Kontur dataset %r not found; auto-selecting",
                    prefer)

    installed = available_datasets(cfg)
    if not installed:
        return None

    width_km = max(1e-3, bbox_width_km(bbox))
    ideal_cell = width_km / MIN_CELLS_ACROSS

    # Finest first: take the coarsest build that still resolves the view, so we
    # avoid loading 400m cells for a whole continent.
    suitable = [p for p in installed if CELL_KM.get(p.name, 1e9) <= ideal_cell]
    if suitable:
        return max(suitable, key=lambda p: CELL_KM.get(p.name, 0.0))

    finest = min(installed, key=lambda p: CELL_KM.get(p.name, 1e9))
    cells_across = width_km / CELL_KM.get(finest.name, 1.0)
    log.warning(
        "view is %.0f km wide; the finest installed Kontur build (%s) gives "
        "only ~%.0f cells across, so the overlay will look blocky. "
        "Run: python data/download_data.py --kontur 400m",
        width_km, finest.name, cells_across)
    return finest


# ------------------------------------------------------------ WKB parsing ---
def _parse_gpkg_blob(blob: bytes) -> Optional[bytes]:
    """Strip the GeoPackage binary header, returning the inner WKB.

    Layout: magic 'GP', version, flags, srs_id, then an optional envelope
    whose size is encoded in bits 1-3 of the flags byte.
    """
    if len(blob) < 8 or blob[0:2] != b"GP":
        return None
    flags = blob[3]
    envelope_code = (flags >> 1) & 0x07
    envelope_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    if envelope_code not in envelope_sizes:
        return None
    return blob[8 + envelope_sizes[envelope_code]:]


def _read_wkb_polygons(wkb: bytes) -> list[list[list[list[float]]]]:
    """Parse WKB Polygon/MultiPolygon into GeoJSON-style coordinate arrays."""
    if len(wkb) < 5:
        return []
    endian = "<" if wkb[0] == 1 else ">"
    geom_type = struct.unpack_from(endian + "I", wkb, 1)[0] & 0xFF
    offset = 5

    def read_polygon(off: int) -> tuple[list[list[list[float]]], int]:
        (n_rings,) = struct.unpack_from(endian + "I", wkb, off)
        off += 4
        rings: list[list[list[float]]] = []
        for _ in range(n_rings):
            (n_pts,) = struct.unpack_from(endian + "I", wkb, off)
            off += 4
            coords = struct.unpack_from(endian + f"{n_pts * 2}d", wkb, off)
            off += n_pts * 16
            rings.append([[coords[i], coords[i + 1]]
                          for i in range(0, len(coords), 2)])
        return rings, off

    if geom_type == _WKB_POLYGON:
        rings, _ = read_polygon(offset)
        return [rings]
    if geom_type == _WKB_MULTIPOLYGON:
        (n_polys,) = struct.unpack_from(endian + "I", wkb, offset)
        offset += 4
        out = []
        for _ in range(n_polys):
            # Each member polygon carries its own endianness + type prefix.
            offset += 5
            rings, offset = read_polygon(offset)
            out.append(rings)
        return out
    return []


# ----------------------------------------------------------------- reading ---
# Kontur publishes in EPSG:3857 (Web Mercator, metres) rather than EPSG:4326.
# Querying its R-tree with degrees silently matches nothing, so the CRS is
# read from the file and both the query box and the output geometry are
# converted. Only these two systems are supported; anything else is refused
# loudly rather than producing an empty overlay.
_WEB_MERCATOR_R = 6378137.0
SUPPORTED_SRS = (4326, 3857, 0, -1)


def _lnglat_to_3857(lng: float, lat: float) -> tuple[float, float]:
    lat = max(-85.06, min(85.06, lat))
    x = _WEB_MERCATOR_R * math.radians(lng)
    y = _WEB_MERCATOR_R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def _3857_to_lnglat(x: float, y: float) -> tuple[float, float]:
    lng = math.degrees(x / _WEB_MERCATOR_R)
    lat = math.degrees(2 * math.atan(math.exp(y / _WEB_MERCATOR_R)) - math.pi / 2)
    return lng, lat


def _geometry_table(conn: sqlite3.Connection) -> tuple[str, str]:
    """Find the feature table and its geometry column from gpkg metadata."""
    row = conn.execute(
        "SELECT table_name, column_name FROM gpkg_geometry_columns LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("no gpkg_geometry_columns entry; not a GeoPackage?")
    return row[0], row[1]


def _srs_id(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(
        "SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?",
        (table,)).fetchone()
    srs = int(row[0]) if row and row[0] is not None else 4326
    if srs not in SUPPORTED_SRS:
        raise ValueError(
            f"GeoPackage uses EPSG:{srs}, which this reader cannot reproject. "
            "Supported: EPSG:4326 and EPSG:3857."
        )
    return srs


def _population_column(conn: sqlite3.Connection, table: str) -> str:
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    for candidate in ("population", "pop", "value", "count"):
        if candidate in cols:
            return candidate
    raise ValueError(f"no population column found in {table}; columns={cols}")


def read_hexagons(path: Path, bbox: BBox, *, limit: int = 200_000
                  ) -> Iterator[tuple[float, list]]:
    """Yield (population, polygon_coords) for cells intersecting bbox.

    Uses the GeoPackage R-tree index when present, which turns a global
    dataset into a fast bbox query instead of a full table scan.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        table, geom_col = _geometry_table(conn)
        pop_col = _population_column(conn, table)
        srs = _srs_id(conn, table)
        rtree = f"rtree_{table}_{geom_col}"
        has_rtree = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (rtree,)).fetchone() is not None

        # Express the query box in the dataset's own coordinate system.
        if srs == 3857:
            qw, qs = _lnglat_to_3857(bbox.west, bbox.south)
            qe, qn = _lnglat_to_3857(bbox.east, bbox.north)
        else:
            qw, qs, qe, qn = bbox.west, bbox.south, bbox.east, bbox.north

        if has_rtree:
            sql = (f'SELECT t."{pop_col}", t."{geom_col}" FROM "{table}" t '
                   f'JOIN "{rtree}" r ON t.rowid = r.id '
                   f'WHERE r.maxx >= ? AND r.minx <= ? '
                   f'AND r.maxy >= ? AND r.miny <= ? LIMIT ?')
            params = (qw, qe, qs, qn, limit)
        else:
            log.warning("%s has no R-tree index; falling back to a full scan",
                        path.name)
            sql = f'SELECT "{pop_col}", "{geom_col}" FROM "{table}" LIMIT ?'
            params = (limit,)

        for population, blob in conn.execute(sql, params):
            if blob is None:
                continue
            wkb = _parse_gpkg_blob(blob)
            if wkb is None:
                continue
            for rings in _read_wkb_polygons(wkb):
                if srs == 3857:
                    # GeoJSON must be lng/lat, so project the cell back.
                    rings = [[list(_3857_to_lnglat(x, y)) for x, y in ring]
                             for ring in rings]
                yield (float(population or 0.0), rings)
    finally:
        conn.close()


@dataclass
class PopulationExtract:
    """The clipped GeoJSON plus the colour ceiling that suits this view."""

    path: Path
    dataset: str
    cells: int
    total: float
    peak: float
    #: Population value the colour ramp should top out at. Derived from the
    #: data, never assumed: a 22km cell holds roughly 54x the people of a 3km
    #: cell, so one fixed ceiling makes an entire country clamp to one colour.
    ceiling: float

    def as_dict(self) -> dict:
        return {"dataset": self.dataset, "cells": self.cells,
                "total": round(self.total), "peak": round(self.peak),
                "ceiling": round(self.ceiling)}


def build_for_bbox(bbox: BBox, out_path: Path, cfg,
                   dataset: Optional[str] = None,
                   max_cells: int = 120_000,
                   percentile: float = 0.97) -> Optional[PopulationExtract]:
    """Write a GeoJSON extract of the population hexagons covering bbox."""
    source = choose_dataset(cfg, bbox, dataset)
    if source is None:
        log.warning(
            "no Kontur GeoPackage found in %s - the population overlay will "
            "be skipped. Run: python data/download_data.py --kontur 3km",
            kontur_dir(cfg))
        return None

    # A little margin keeps hexagons at the frame edge from popping in.
    query_box = bbox.padded(0.15)
    features: list[dict] = []
    values: list[float] = []
    total_pop = 0.0
    peak = 0.0

    for population, rings in read_hexagons(source, query_box, limit=max_cells):
        features.append({
            "type": "Feature",
            "properties": {"population": round(population, 2)},
            "geometry": {"type": "Polygon", "coordinates": rings},
        })
        values.append(population)
        total_pop += population
        peak = max(peak, population)

    if not features:
        log.warning("no population cells found for bbox %s", bbox.to_list())
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": features,
    }), encoding="utf-8")

    # Scale the ramp to this extract. Topping out at the single densest cell
    # would wash out everything else, so use a high percentile instead.
    ordered = sorted(values)
    ceiling = ordered[min(len(ordered) - 1, int(len(ordered) * percentile))]
    ceiling = max(1.0, ceiling)

    log.info("population extract: %d cells from %s, total %s, peak %s, "
             "colour ceiling %s (p%d)", len(features), source.name,
             f"{total_pop:,.0f}", f"{peak:,.0f}", f"{ceiling:,.0f}",
             int(percentile * 100))
    return PopulationExtract(out_path, source.name, len(features),
                             total_pop, peak, ceiling)


def suggest_max_population(path: Path, bbox: BBox, percentile: float = 0.98
                           ) -> Optional[float]:
    """A colour-scale ceiling that is robust to a few extreme cells.

    Scaling the choropleth to the single densest hexagon washes out an entire
    country, so the ramp tops out near the 98th percentile instead.
    """
    values = sorted(pop for pop, _ in read_hexagons(path, bbox, limit=200_000))
    if not values:
        return None
    idx = min(len(values) - 1, max(0, int(len(values) * percentile)))
    return values[idx]
