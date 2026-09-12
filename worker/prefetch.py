"""Warm the disk cache for a camera track before any frame is rendered.

The fair-use rule this enforces: *the render loop never makes a live network
request*. To honour it we have to know, ahead of time, exactly which tiles the
renderer will ask for. Two things make that possible:

1. `worker.frustum` reproduces MapLibre's camera projection exactly, so we can
   compute each frame's true ground footprint (verified against the browser in
   tests/test_frustum_matches_maplibre.py).
2. The tile sources are read back out of the *composed style document*, so the
   prefetch plan is derived from the very same URLs the renderer will use
   rather than from a parallel, drift-prone list.

Ancestor tiles are included because MapLibre draws a parent tile while a child
is still loading; without them the first frames of a zoom-in flash blank.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from .camera import CameraState
from .cache.fetch import Fetcher, FetchStats
from .frustum import camera_bbox, near_field_bbox, source_zoom
from .log import get as get_logger
from .sources.upstreams import split_url
from .tiles import BBox, tiles_for_bbox

log = get_logger("prefetch")

# Refuse to plan an unreasonable download; a runaway camera path should fail
# loudly rather than quietly pulling gigabytes off a public-good service.
DEFAULT_TILE_BUDGET = 60_000

# Glyph ranges to warm per fontstack. OSM name tags are multilingual, so a
# basemap anywhere in the world pulls well beyond Latin: rendering the Alps
# alone requested Cyrillic, Arabic and General Punctuation. These files are
# small and cached permanently, so warming the common blocks up front is far
# cheaper than discovering them as live fetches mid-render.
GLYPH_RANGES = (
    "0-255",        # Latin + Latin-1 Supplement
    "256-511",      # Latin Extended-A/B
    "512-767",      # Latin Extended-B, IPA
    "768-1023",     # Combining diacritics, Greek
    "1024-1279",    # Cyrillic
    "1280-1535",    # Cyrillic Supplement, Armenian
    "1536-1791",    # Arabic
    "7936-8191",    # Greek Extended
    "8192-8447",    # General Punctuation (dashes, quotes - very common)
    "11264-11519",  # Glagolitic / Latin Ext-C
    "11520-11775",  # Georgian
)


@dataclass(frozen=True)
class TileSource:
    """One tile pyramid the renderer will pull from."""

    name: str
    upstream: str
    #: Path template containing {z}/{x}/{y}, relative to the upstream root.
    template: str
    tile_size: int = 512
    minzoom: int = 0
    maxzoom: int = 14

    def path(self, z: int, x: int, y: int) -> str:
        return (self.template
                .replace("{z}", str(z))
                .replace("{x}", str(x))
                .replace("{y}", str(y)))


def sources_from_style(style: dict, store=None) -> list[TileSource]:
    """Read tile pyramids back out of a composed style document.

    A source may declare its tiles inline (``tiles``) or point at a TileJSON
    document (``url``) that the renderer will dereference at load time. The
    OpenFreeMap basemap uses the latter, so resolving it is not optional: skip
    it and the entire vector basemap goes un-prefetched.
    """
    out: list[TileSource] = []
    for name, src in (style.get("sources") or {}).items():
        spec = dict(src)
        tiles = spec.get("tiles")

        if not tiles and spec.get("url"):
            resolved = _resolve_tilejson(spec["url"], store)
            if resolved is None:
                log.warning("source %r points at TileJSON %s which is not cached; "
                            "its tiles cannot be prefetched",
                            name, str(spec["url"])[:80])
                continue
            # TileJSON values fill in only what the style did not state itself.
            tiles = resolved.get("tiles")
            for key in ("minzoom", "maxzoom"):
                if key not in spec and key in resolved:
                    spec[key] = resolved[key]

        if not tiles:
            continue
        resolved_tpl = split_url(tiles[0])
        if resolved_tpl is None:
            log.warning("source %r has an unrecognised tile URL (%s); "
                        "skipping prefetch", name, tiles[0][:80])
            continue
        upstream, template = resolved_tpl
        kind = spec.get("type")
        # A raster-dem is always 512 here; raster sources declare their own.
        tile_size = int(spec.get("tileSize", 256 if kind == "raster" else 512))
        out.append(TileSource(
            name=name,
            upstream=upstream,
            template=template,
            tile_size=tile_size,
            minzoom=int(spec.get("minzoom", 0)),
            maxzoom=int(spec.get("maxzoom", 14)),
        ))
    return out


def _resolve_tilejson(url: str, store) -> Optional[dict]:
    """Load a TileJSON document that the prefetch pass already cached."""
    if store is None:
        return None
    parts = split_url(url)
    if parts is None:
        return None
    entry = store.get(*parts)
    if entry is None:
        return None
    try:
        return json.loads(entry.body)
    except (ValueError, UnicodeDecodeError):
        log.warning("TileJSON at %s is not valid JSON", url[:80])
        return None


def support_urls(style: dict) -> list[tuple[str, str]]:
    """Glyph ranges and sprite images the style needs, as (upstream, path)."""
    items: list[tuple[str, str]] = []

    glyphs = style.get("glyphs")
    if glyphs:
        parts = split_url(glyphs)
        if parts:
            upstream, template = parts
            for stack in _fontstacks(style):
                for rng in GLYPH_RANGES:
                    items.append((upstream, template
                                  .replace("{fontstack}", stack)
                                  .replace("{range}", rng)))

    sprite = style.get("sprite")
    if isinstance(sprite, list):
        sprite_urls = [s.get("url") for s in sprite if isinstance(s, dict)]
    else:
        sprite_urls = [sprite]
    for url in filter(None, sprite_urls):
        parts = split_url(url)
        if parts is None:
            continue
        upstream, base = parts
        for suffix in (".json", ".png", "@2x.json", "@2x.png"):
            items.append((upstream, base + suffix))
    return items


def _fontstacks(style: dict) -> set[str]:
    stacks: set[str] = set()
    for layer in style.get("layers", []):
        font = (layer.get("layout") or {}).get("text-font")
        if isinstance(font, list) and all(isinstance(f, str) for f in font):
            stacks.add(",".join(font))
    return stacks or {"Noto Sans Regular"}


# How much of the viewport, measured from the bottom edge, MapLibre may fetch
# one zoom level deeper than the frame's nominal zoom.
NEAR_FIELD_FRACTION = 0.5


def _terrain_margin(pitch: float) -> float:
    """Fractional bbox growth to cover terrain that a flat frustum misses.

    At nadir the correction is small (raised ground only shifts the edges
    slightly). Near the horizon a modest elevation reaches much further, so the
    margin grows with pitch. Empirically 0.06 -> 0.30 removes the residual
    misses on alpine camera paths without materially inflating the download.
    """
    t = max(0.0, min(1.0, pitch / 70.0))
    return 0.06 + 0.24 * (t ** 1.5)


def plan_tiles(
    track: Sequence[CameraState],
    sources: Iterable[TileSource],
    *,
    width: int,
    height: int,
    padding: int = 1,
    include_ancestors: bool = True,
    budget: int = DEFAULT_TILE_BUDGET,
    terrain: bool = False,
) -> list[tuple[str, str]]:
    """Compute the exact (upstream, path) set the camera track will request.

    With 3D terrain the flat-ground frustum is not enough: elevated ground
    beyond the flat horizon rises into view, and raised terrain nearby pushes
    the visible edge outward. `terrain=True` widens each frame's footprint to
    account for that, by an amount that grows with pitch.
    """
    sources = list(sources)
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []

    # Frames overwhelmingly overlap, so cache the per-frame footprint by a
    # rounded camera signature to avoid recomputing near-identical boxes.
    box_cache: dict[tuple, tuple[BBox, BBox | None]] = {}

    for cam in track:
        key = (round(cam.lng, 4), round(cam.lat, 4), round(cam.zoom, 2),
               round(cam.bearing, 1), round(cam.pitch, 1))
        cached = box_cache.get(key)
        if cached is None:
            bbox = camera_bbox(cam, width, height)
            near = None
            if terrain:
                bbox = bbox.padded(_terrain_margin(cam.pitch))
                # Terrain raises the ground toward the camera, so MapLibre
                # fetches the near half of the frame a level deeper than the
                # frame's nominal zoom. Planning only the nominal zoom left
                # those tiles to be fetched live, mid-render.
                near = near_field_bbox(cam, width, height,
                                       fraction=NEAR_FIELD_FRACTION)
            cached = (bbox, near)
            box_cache[key] = cached
        bbox, near = cached

        for src in sources:
            z = source_zoom(cam.zoom, src.tile_size,
                            maxzoom=src.maxzoom, minzoom=src.minzoom)

            def add(tz: int, tx: int, ty: int) -> None:
                item = (src.upstream, src.path(tz, tx, ty))
                if item in seen:
                    return
                seen.add(item)
                ordered.append(item)
                if len(ordered) > budget:
                    raise RuntimeError(
                        f"prefetch plan exceeded {budget} tiles. Narrow the "
                        "region, lower the zoom, or raise the budget "
                        "deliberately."
                    )

            planned = list(tiles_for_bbox(bbox, z, padding=padding))
            if near is not None and z < src.maxzoom:
                planned += tiles_for_bbox(near, z + 1, padding=padding)

            for (tz, tx, ty) in planned:
                add(tz, tx, ty)
                if not include_ancestors:
                    continue
                # Derive ancestors by halving the tile coordinate rather than
                # re-deriving them from the bbox. Re-deriving loses edge tiles
                # whenever a bbox corner sits just inside a coarser tile, which
                # showed up as blank parent tiles during fast zoom-outs.
                ax, ay = tx, ty
                for az in range(tz - 1, src.minzoom - 1, -1):
                    ax, ay = ax >> 1, ay >> 1
                    add(az, ax, ay)
    return ordered


async def warm(
    fetcher: Fetcher,
    items: Sequence[tuple[str, str]],
    *,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> FetchStats:
    """Download everything in the plan that is not already cached."""
    if not items:
        return FetchStats()
    log.info("prefetching %d objects", len(items))
    stats = await fetcher.get_many(items, on_progress=on_progress)
    log.info("prefetch done: %s", stats.as_dict())
    if stats.failed:
        log.warning("%d objects failed to prefetch; the render may show gaps",
                    stats.failed)
    return stats


def plan_for_style(
    style: dict,
    track: Sequence[CameraState],
    *,
    width: int,
    height: int,
    padding: int = 1,
    budget: int = DEFAULT_TILE_BUDGET,
    store=None,
) -> list[tuple[str, str]]:
    """Full plan for a composed style: support files first, then tiles."""
    items = support_urls(style)
    items += plan_tiles(track, sources_from_style(style, store),
                        width=width, height=height,
                        padding=padding, budget=budget,
                        # Terrain is declared on the style itself, so the plan
                        # widens automatically for 3D jobs.
                        terrain=bool(style.get("terrain")))
    # Preserve order (support files first) while removing duplicates.
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def summarise(items: Sequence[tuple[str, str]]) -> dict:
    counts: dict[str, int] = {}
    for upstream, _ in items:
        counts[upstream] = counts.get(upstream, 0) + 1
    return {"total": len(items), "by_upstream": counts}
