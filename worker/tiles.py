"""Web-Mercator (EPSG:3857 / XYZ) tile math.

Self-contained on purpose: the pipeline needs only a handful of conversions,
which is not worth a geopandas/pyproj dependency on the render worker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator, Sequence

TILE_SIZE = 512  # MapLibre vector/raster tiles render at 512px logical size
MAX_LAT = 85.0511287798066  # Web-Mercator clamp

#: Smallest bbox side we will render, in degrees (~100 m). Anything smaller
#: has no scale for the camera to fit and drives the zoom to its ceiling.
MIN_BBOX_SPAN_DEG = 0.001

#: Ceiling for a fitted camera zoom. The vector basemap tops out at z14 and
#: the DEM at z12-15, so beyond roughly this the picture is overzoomed mush
#: rather than detail.
MAX_FIT_ZOOM = 16.5


@dataclass(frozen=True)
class BBox:
    """Geographic bounding box in degrees."""

    west: float
    south: float
    east: float
    north: float

    @classmethod
    def from_list(cls, seq: Sequence[float]) -> "BBox":
        if len(seq) != 4:
            raise ValueError(f"bbox needs 4 values [w,s,e,n], got {len(seq)}")
        try:
            w, s, e, n = (float(v) for v in seq)
        except (TypeError, ValueError):
            raise ValueError(f"bbox values must be numbers, got {list(seq)}") from None
        if any(v != v for v in (w, s, e, n)):          # NaN
            raise ValueError(f"bbox contains NaN: {list(seq)}")
        if not (-90.0 <= s <= 90.0 and -90.0 <= n <= 90.0):
            raise ValueError(
                f"bbox latitudes must be within -90..90, got south={s}, "
                f"north={n}")
        if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0):
            raise ValueError(
                f"bbox longitudes must be within -180..180, got west={w}, "
                f"east={e}")
        if w > e:
            raise ValueError(
                f"bbox west ({w}) must be <= east ({e}). A region crossing the "
                "antimeridian is not supported; split it into two jobs.")
        if s > n:
            raise ValueError(f"bbox south ({s}) must be <= north ({n})")
        # A degenerate box has no scale to fit a camera to, and would drive
        # zoom_for_bbox to its ceiling - rendering a meaningless extreme
        # close-up of overzoomed tiles rather than failing.
        if (e - w) < MIN_BBOX_SPAN_DEG or (n - s) < MIN_BBOX_SPAN_DEG:
            raise ValueError(
                f"bbox is too small to render: spans {e - w:.6f} x {n - s:.6f} "
                f"degrees, minimum is {MIN_BBOX_SPAN_DEG} (about 100 m). "
                "Draw a larger region."
            )
        return cls(w, s, e, n)

    def to_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]

    @property
    def center(self) -> tuple[float, float]:
        return ((self.west + self.east) / 2.0, (self.south + self.north) / 2.0)

    def padded(self, frac: float) -> "BBox":
        """Grow the box by a fraction of its own span, clamped to valid ranges."""
        dx = (self.east - self.west) * frac
        dy = (self.north - self.south) * frac
        return BBox(
            max(-180.0, self.west - dx),
            max(-MAX_LAT, self.south - dy),
            min(180.0, self.east + dx),
            min(MAX_LAT, self.north + dy),
        )

    def contains(self, lng: float, lat: float) -> bool:
        return self.west <= lng <= self.east and self.south <= lat <= self.north


def clamp_lat(lat: float) -> float:
    return max(-MAX_LAT, min(MAX_LAT, lat))


def lnglat_to_tile(lng: float, lat: float, z: int) -> tuple[int, int]:
    """Return the (x, y) tile index containing this coordinate at zoom z."""
    n = 1 << z
    lat = clamp_lat(lat)
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_to_lnglat(x: int, y: int, z: int) -> tuple[float, float]:
    """North-west corner of the given tile."""
    n = 1 << z
    lng = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lng, lat


def tile_bbox(x: int, y: int, z: int) -> BBox:
    w, n = tile_to_lnglat(x, y, z)
    e, s = tile_to_lnglat(x + 1, y + 1, z)
    return BBox(w, s, e, n)


def tiles_for_bbox(bbox: BBox, z: int, padding: int = 0) -> Iterator[tuple[int, int, int]]:
    """Yield every (z, x, y) tile covering bbox, optionally with a padding ring."""
    n = 1 << z
    x0, y0 = lnglat_to_tile(bbox.west, bbox.north, z)
    x1, y1 = lnglat_to_tile(bbox.east, bbox.south, z)
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    x0 = max(0, x0 - padding)
    y0 = max(0, y0 - padding)
    x1 = min(n - 1, x1 + padding)
    y1 = min(n - 1, y1 + padding)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            yield (z, x, y)


def count_tiles_for_bbox(bbox: BBox, z: int, padding: int = 0) -> int:
    """Tile count without materialising the list -- used for prefetch budgeting."""
    n = 1 << z
    x0, y0 = lnglat_to_tile(bbox.west, bbox.north, z)
    x1, y1 = lnglat_to_tile(bbox.east, bbox.south, z)
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    w = min(n - 1, x1 + padding) - max(0, x0 - padding) + 1
    h = min(n - 1, y1 + padding) - max(0, y0 - padding) + 1
    return max(0, w) * max(0, h)


def zoom_for_bbox(bbox: BBox, width: int, height: int, tile_size: int = TILE_SIZE) -> float:
    """Fractional zoom at which bbox exactly fits a width x height viewport."""
    def merc_y(lat: float) -> float:
        lat = clamp_lat(lat)
        return math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))

    lng_span = max(1e-9, (bbox.east - bbox.west) / 360.0)
    lat_span = max(1e-9, abs(merc_y(bbox.north) - merc_y(bbox.south)) / (2.0 * math.pi))
    zx = math.log2(width / (tile_size * lng_span))
    zy = math.log2(height / (tile_size * lat_span))
    return max(0.0, min(MAX_FIT_ZOOM, min(zx, zy)))


def viewport_bbox(lng: float, lat: float, zoom: float, width: int, height: int,
                  tile_size: int = TILE_SIZE) -> BBox:
    """Approximate ground bbox visible for a top-down camera.

    Pitch is deliberately ignored; callers pad generously for tilted cameras
    because a pitched view sees much further toward the horizon.
    """
    scale = tile_size * (2.0 ** zoom)
    lng_span = 360.0 * width / scale
    world_y = (1.0 - math.asinh(math.tan(math.radians(clamp_lat(lat)))) / math.pi) / 2.0
    dy = (height / scale)
    def y_to_lat(wy: float) -> float:
        wy = max(0.0, min(1.0, wy))
        return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * wy))))
    return BBox(
        max(-180.0, lng - lng_span / 2.0),
        clamp_lat(y_to_lat(world_y + dy / 2.0)),
        min(180.0, lng + lng_span / 2.0),
        clamp_lat(y_to_lat(world_y - dy / 2.0)),
    )
