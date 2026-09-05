"""Political boundaries for the borders overlay.

Two sources, in preference order:

``basemap``       the OpenMapTiles ``boundary`` vector layer that ships inside
                  the OpenFreeMap basemap tiles. Costs no extra download at
                  all - the tiles are already prefetched for the basemap - and
                  it is vector, so it stays crisp at every zoom.
``natural_earth`` a bundled Natural Earth GeoJSON, clipped to the job's bbox.
                  Public-domain, downloaded once by data/download_data.py and
                  read from disk thereafter; never fetched live per render.

The basemap source is the default because it is free of extra bandwidth and
always available. Natural Earth is preferred when present and explicitly
requested, since its generalised coastlines and disputed-boundary handling are
editorially cleaner for a documentary.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..log import get as get_logger
from ..tiles import BBox

log = get_logger("sources.borders")

# Files download_data.py writes, in the order we prefer them.
NE_CANDIDATES = (
    "ne_10m_admin_0_boundary_lines_land.geojson",
    "ne_50m_admin_0_boundary_lines_land.geojson",
    "ne_110m_admin_0_boundary_lines_land.geojson",
)


def natural_earth_dir(cfg) -> Path:
    return cfg.path("data") / "natural_earth"


def find_bundled(cfg) -> Optional[Path]:
    """Highest-resolution bundled Natural Earth boundary file, if any."""
    root = natural_earth_dir(cfg)
    for name in NE_CANDIDATES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def build_for_bbox(bbox: BBox, out_path: Path, cfg,
                   source: str = "auto") -> Optional[Path]:
    """Write a clipped boundary GeoJSON for this bbox.

    Returns None when the overlay should be drawn from the basemap's own
    vector tiles instead, which is the normal path.
    """
    if source == "basemap":
        return None
    bundled = find_bundled(cfg)
    if bundled is None:
        if source == "natural_earth":
            log.warning(
                "borders source 'natural_earth' requested but no bundled file "
                "found in %s - run data/download_data.py. Falling back to the "
                "basemap boundary layer.", natural_earth_dir(cfg))
        return None

    log.info("clipping %s to job bbox", bundled.name)
    clipped = clip_geojson(json.loads(bundled.read_text(encoding="utf-8")), bbox)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(clipped), encoding="utf-8")
    log.info("wrote %d boundary features to %s",
             len(clipped["features"]), out_path.name)
    return out_path


def clip_geojson(doc: dict, bbox: BBox, margin: float = 0.25) -> dict:
    """Keep only features whose bounds intersect the (padded) bbox.

    This is a bounding-box filter, not a geometric clip: cutting line
    geometries at the box edge would create visible cut ends inside the frame
    whenever the camera looks outward, so whole features are kept.
    """
    box = bbox.padded(margin)
    kept: list[dict] = []
    for feature in doc.get("features", []):
        geom = feature.get("geometry")
        if not geom:
            continue
        bounds = _geometry_bounds(geom.get("coordinates"), geom.get("type"))
        if bounds is None:
            continue
        west, south, east, north = bounds
        if east < box.west or west > box.east or north < box.south or south > box.north:
            continue
        kept.append(feature)
    return {"type": "FeatureCollection", "features": kept}


def _geometry_bounds(coords: Any, geom_type: str | None) -> Optional[tuple]:
    """Bounds of an arbitrarily nested GeoJSON coordinate array."""
    if coords is None:
        return None
    west = south = float("inf")
    east = north = float("-inf")

    stack = [coords]
    while stack:
        node = stack.pop()
        if not isinstance(node, (list, tuple)) or not node:
            continue
        if isinstance(node[0], (int, float)) and len(node) >= 2:
            lng, lat = float(node[0]), float(node[1])
            west, east = min(west, lng), max(east, lng)
            south, north = min(south, lat), max(north, lat)
        else:
            stack.extend(node)

    if west == float("inf"):
        return None
    return (west, south, east, north)
