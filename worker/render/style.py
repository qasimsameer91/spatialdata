"""Composes the MapLibre style document used by the headless renderer.

Every URL in the returned style points at the local `TileServer`, so the render
page can load a full cartographic basemap, 3D terrain, satellite imagery and
data overlays without a single live network request.

Basemap modes
-------------
``vector``    OpenFreeMap "liberty" - full cartography, good for city scenes
``satellite`` GIBS true-colour raster with only labels and boundaries on top
``relief``    Natural Earth II shaded relief raster (served by OpenFreeMap)

Overlay layers are always *added* to the style with their visibility driven
per-frame by the renderer, so toggling an overlay mid-video never needs a
style reload (which would flush the tile cache and cause a visible hitch).
"""
from __future__ import annotations

import copy
import json
from typing import Any, Optional

from ..log import get as get_logger
from ..sources import gibs

log = get_logger("render.style")

OFM_TILEJSON_PATH = "/planet"
NE_RELIEF_PATH = "natural_earth/ne2sr/{z}/{x}/{y}.png"

# OpenFreeMap's published styles, ordered light-to-dark for the UI.
#
# `fiord` is the default: `dark` is genuinely too murky over terrain (mean
# frame brightness ~73 against fiord's ~95 on the same alpine view), and a
# viewer reads an under-exposed flyover as a broken render rather than a
# stylistic choice. `dark` remains available for night or city scenes where
# the extra contrast helps.
VECTOR_STYLES = ("fiord", "dark", "positron", "liberty", "bright")
DEFAULT_VECTOR_STYLE = "fiord"


def vector_style_path(name: str) -> str:
    if name not in VECTOR_STYLES:
        raise ValueError(f"unknown vector style {name!r}; "
                         f"choose from {list(VECTOR_STYLES)}")
    return f"/styles/{name}"


LAYER_HILLSHADE = "sd-hillshade"

TERRAIN_SOURCE = "sd-terrain"
SAT_SOURCE = "sd-satellite"
SAT_AFTER_SOURCE = "sd-satellite-after"
WEATHER_SOURCE = "sd-weather"
POP_SOURCE = "sd-population"
BORDER_SOURCE = "sd-borders"

# The basemap raster (satellite/relief modes) is NOT an overlay: it is the
# picture itself and must always be visible. It therefore needs an id the
# overlay controller never touches - sharing `sd-satellite-raster` with the
# satellite *overlay* made applyOverlays() hide the whole basemap every frame.
LAYER_BASEMAP_RASTER = "sd-basemap-raster"

# Overlay layer ids the renderer toggles per frame.
LAYER_SATELLITE = "sd-satellite-raster"
LAYER_SATELLITE_AFTER = "sd-satellite-after-raster"
LAYER_WEATHER = "sd-weather-raster"
LAYER_POP_FILL = "sd-population-fill"
LAYER_BORDER_LINE = "sd-borders-line"
LAYER_BORDER_GLOW = "sd-borders-glow"

OVERLAY_LAYERS = {
    "satellite": [LAYER_SATELLITE],
    "satellite_after": [LAYER_SATELLITE_AFTER],
    "weather": [LAYER_WEATHER],
    "population": [LAYER_POP_FILL],
    "borders": [LAYER_BORDER_GLOW, LAYER_BORDER_LINE],
}


def _sky() -> dict:
    """A subtle atmosphere, which sells the 3D terrain at low pitch angles.

    In MapLibre GL JS 5 the sky is a root-level style property, not a layer
    (the `sky` layer type only exists in Mapbox GL JS).
    """
    return {
        "sky-color": "#0b1d33",
        "sky-horizon-blend": 0.6,
        "horizon-color": "#87a5c4",
        "horizon-fog-blend": 0.55,
        "fog-color": "#c8d8e8",
        "fog-ground-blend": 0.12,
        "atmosphere-blend": 0.8,
    }


# Hillshade palettes per basemap style. A bright highlight over a dark
# basemap destroys label legibility - the style's light text and dark halo
# both disappear against blown-out terrain - so dark styles get a muted,
# low-key shade and light styles get the conventional bright one.
HILLSHADE_PALETTES: dict[str, dict] = {
    "dark": {"highlight_color": "#7e8fa1", "shadow_color": "#05090f",
             "accent_color": "#16202b", "exaggeration": 0.45},
    "fiord": {"highlight_color": "#8496a8", "shadow_color": "#070c14",
              "accent_color": "#1a2530", "exaggeration": 0.45},
    "positron": {"highlight_color": "#fbf7f0", "shadow_color": "#5d6b7c",
                 "accent_color": "#98a5b4", "exaggeration": 0.5},
    "liberty": {"highlight_color": "#fdf8ee", "shadow_color": "#55636f",
                "accent_color": "#93a1ad", "exaggeration": 0.5},
    "bright": {"highlight_color": "#fffaf2", "shadow_color": "#5a6672",
               "accent_color": "#9aa6b2", "exaggeration": 0.5},
}


def _hillshade_layer(cfg: dict, style_name: str = DEFAULT_VECTOR_STYLE) -> dict:
    """Shaded relief over the DEM.

    Without this the 3D terrain has geometry but no light and reads as a
    featureless white model. Illumination is anchored to the *map* rather than
    the viewport so the sun stays put as the camera orbits; on `viewport` the
    shadows swing round with the bearing, which looks badly wrong in motion.
    """
    palette = dict(HILLSHADE_PALETTES.get(style_name, HILLSHADE_PALETTES["dark"]))
    palette.update(cfg or {})
    return {
        "id": LAYER_HILLSHADE,
        "type": "hillshade",
        "source": TERRAIN_SOURCE,
        "paint": {
            "hillshade-exaggeration": float(palette["exaggeration"]),
            "hillshade-illumination-direction": float(
                palette.get("sun_direction", 335)),
            "hillshade-illumination-anchor": "map",
            "hillshade-shadow-color": palette["shadow_color"],
            "hillshade-highlight-color": palette["highlight_color"],
            "hillshade-accent-color": palette["accent_color"],
        },
    }


# Label thinning. At a documentary pitch the camera sees a long way, and
# MapLibre happily draws every hamlet out to the horizon - which collapses
# into an unreadable smear along the skyline. Each density level names the
# label layers to drop from the basemap style.
LABEL_DENSITY_DROP: dict[str, tuple[str, ...]] = {
    "full": (),
    "balanced": ("place_other", "place_suburb", "housenumber", "poi_z16",
                 "poi_z15", "poi_z14", "road_oneway", "road_oneway_opposite"),
    "minimal": ("place_other", "place_suburb", "place_village", "place_town",
                "housenumber", "poi_z16", "poi_z15", "poi_z14", "water_name",
                "highway_name_other", "highway_name_motorway",
                "road_oneway", "road_oneway_opposite"),
    "none": ("*",),
}


def restyle_labels(style: dict) -> dict:
    """Force one legible label treatment across the whole basemap.

    Published styles tune their label colours for a flat, top-down map. Over 3D
    terrain the same labels have to stay readable against both black valley
    shadow and pale atmospheric haze near the horizon, and the stock dark-style
    treatment (light text, dark halo) collapses into unreadable smudges at
    distance. A bright face with a strong dark halo survives both.
    """
    for layer in style.get("layers", []):
        if layer.get("type") != "symbol":
            continue
        if "text-field" not in (layer.get("layout") or {}):
            continue
        paint = layer.setdefault("paint", {})
        paint["text-color"] = "#f4f8fd"
        paint["text-halo-color"] = "rgba(3, 7, 13, 0.9)"
        paint["text-halo-width"] = 1.7
        paint["text-halo-blur"] = 0.3
    return style


def thin_labels(style: dict, density: str = "balanced") -> dict:
    """Drop label layers according to the requested density."""
    if density not in LABEL_DENSITY_DROP:
        raise ValueError(f"unknown label_density {density!r}; "
                         f"choose from {sorted(LABEL_DENSITY_DROP)}")
    drop = LABEL_DENSITY_DROP[density]
    if not drop:
        return style
    if drop == ("*",):
        style["layers"] = [l for l in style["layers"] if l.get("type") != "symbol"]
        return style
    dropped = set(drop)
    style["layers"] = [l for l in style["layers"] if l.get("id") not in dropped]
    return style


def _terrain_source(prefix_mapterhorn: str, maxzoom: int) -> dict:
    return {
        "type": "raster-dem",
        "tiles": [f"{prefix_mapterhorn}/{{z}}/{{x}}/{{y}}.webp"],
        "encoding": "terrarium",
        "tileSize": 512,
        "maxzoom": maxzoom,
        "attribution": "Terrain (c) Mapterhorn",
    }


def _population_paint(cfg: dict) -> dict:
    """Choropleth paint for Kontur H3 hexagons, scaled by population count."""
    scale = cfg.get("color_scale") or ["#0d0887", "#b12a90", "#fca636", "#f0f921"]
    top = float(cfg.get("max_population", 8000) or 8000)
    stops: list[Any] = []
    previous = 0.0
    for i, colour in enumerate(scale):
        # Perceptually the interesting range is the low end, so ramp the
        # breakpoints quadratically rather than linearly.
        frac = (i / max(1, len(scale) - 1)) ** 2
        # MapLibre rejects an interpolate expression whose stops are not
        # strictly ascending, which a small max_population would otherwise
        # produce once several breakpoints round to the same value.
        value = max(frac * top, previous + 1e-3 if i else 0.0)
        previous = value
        stops.extend([value, colour])
    peak = float(cfg.get("opacity", 0.75))
    # Ramp opacity with density as well as colour. A flat opacity paints empty
    # mountainside as solidly as a city, which hides the landscape and inverts
    # the point of the overlay - the eye should be drawn to where people are.
    floor_frac = float(cfg.get("min_opacity_frac", 0.06))
    fade_to = max(1.0, top * float(cfg.get("fade_fraction", 0.06)))
    return {
        "fill-color": ["interpolate", ["linear"],
                       ["coalesce", ["get", "population"], 0]] + stops,
        "fill-opacity": [
            "interpolate", ["linear"],
            ["coalesce", ["get", "population"], 0],
            0.0, round(peak * floor_frac, 4),
            fade_to, round(peak * 0.55, 4),
            top, round(peak, 4),
        ],
        "fill-antialias": False,
    }


def build_style(
    server,
    *,
    basemap: str = "vector",
    satellite_layer: str = "truecolor_viirs",
    weather_layer: Optional[str] = None,
    date: Optional[str] = None,
    terrain: bool = True,
    terrain_maxzoom: int = 12,
    vector_style: str = DEFAULT_VECTOR_STYLE,
    hillshade: bool = True,
    label_density: str = "balanced",
    overlay_cfg: Optional[dict] = None,
    population_url: Optional[str] = None,
    borders_url: Optional[str] = None,
    borders: bool = False,
    satellite: bool = False,
    date_end: Optional[str] = None,
) -> dict:
    """Build the complete style document.

    `server` is a started TileServer; its cache must already be warmed for the
    region being rendered.
    """
    overlay_cfg = overlay_cfg or {}
    ofm = server.upstream_prefix("ofm")
    mapterhorn = server.upstream_prefix("mapterhorn")
    gibs_prefix = server.upstream_prefix("gibs")
    when = gibs.normalise_date(date)

    if basemap == "vector":
        style = restyle_labels(
            thin_labels(_vector_base(server, vector_style), label_density))
    else:
        style = _raster_base(server, basemap, satellite_layer, when, ofm, gibs_prefix)

    style.setdefault("sources", {})
    style.setdefault("layers", [])

    # -- 3D terrain ------------------------------------------------------
    if terrain:
        style["sources"][TERRAIN_SOURCE] = _terrain_source(mapterhorn, terrain_maxzoom)
        style["terrain"] = {"source": TERRAIN_SOURCE, "exaggeration": 1.0}
        style["sky"] = _sky()
        if hillshade:
            # Sits above the ground fills but below roads and labels, so the
            # relief shades the landscape without muddying the cartography.
            style["layers"].insert(_first_line_or_symbol_index(style["layers"]),
                                   _hillshade_layer(overlay_cfg.get("hillshade", {}),
                                                    vector_style))

    # -- overlays --------------------------------------------------------
    # Inserted beneath label layers so place names stay readable on top.
    insert_at = _first_symbol_index(style["layers"])

    # Satellite imagery *as an overlay*, laid over the vector basemap and
    # faded in by the timeline. Distinct from basemap="satellite", where the
    # imagery is the picture itself and is never toggled.
    if satellite and basemap != "satellite":
        sat = gibs.get_layer(satellite_layer)
        style["sources"][SAT_SOURCE] = {
            "type": "raster",
            "tiles": [sat.tile_template(gibs_prefix, when)],
            "tileSize": 256,
            "maxzoom": sat.max_zoom,
            "attribution": "NASA GIBS/EOSDIS",
        }
        style["layers"].insert(insert_at, {
            "id": LAYER_SATELLITE,
            "type": "raster",
            "source": SAT_SOURCE,
            "layout": {"visibility": "none"},
            "paint": {"raster-opacity": float(
                overlay_cfg.get("satellite", {}).get("opacity", 1.0))},
        })
        insert_at += 1

    # A second, later date of the same imagery. This is what makes a
    # before/after story possible - flood, fire, drought - by cross-fading
    # between two days of the same satellite layer.
    if date_end and satellite:
        sat = gibs.get_layer(satellite_layer)
        later = gibs.normalise_date(date_end)
        style["sources"][SAT_AFTER_SOURCE] = {
            "type": "raster",
            "tiles": [sat.tile_template(gibs_prefix, later)],
            "tileSize": 256,
            "maxzoom": sat.max_zoom,
            "attribution": "NASA GIBS/EOSDIS",
        }
        style["layers"].insert(insert_at, {
            "id": LAYER_SATELLITE_AFTER,
            "type": "raster",
            "source": SAT_AFTER_SOURCE,
            "layout": {"visibility": "none"},
            "paint": {"raster-opacity": float(
                overlay_cfg.get("satellite_after", {}).get("opacity", 1.0))},
        })
        insert_at += 1

    if weather_layer:
        wl = gibs.get_layer(weather_layer)
        style["sources"][WEATHER_SOURCE] = {
            "type": "raster",
            "tiles": [wl.tile_template(gibs_prefix, when)],
            "tileSize": 256,
            "maxzoom": wl.max_zoom,
            "attribution": "NASA GIBS/EOSDIS",
        }
        style["layers"].insert(insert_at, {
            "id": LAYER_WEATHER,
            "type": "raster",
            "source": WEATHER_SOURCE,
            "layout": {"visibility": "none"},
            "paint": {"raster-opacity": float(
                overlay_cfg.get("weather", {}).get("opacity", 0.6))},
        })
        insert_at += 1

    if population_url:
        style["sources"][POP_SOURCE] = {
            "type": "geojson",
            "data": population_url,
            "attribution": "Kontur Population (CC BY)",
        }
        style["layers"].insert(insert_at, {
            "id": LAYER_POP_FILL,
            "type": "fill",
            "source": POP_SOURCE,
            "layout": {"visibility": "none"},
            "paint": _population_paint(overlay_cfg.get("population", {})),
        })
        insert_at += 1

    if borders_url or borders:
        bcfg = overlay_cfg.get("borders", {})
        if borders_url:
            # Bundled Natural Earth GeoJSON, clipped to the job bbox.
            style["sources"][BORDER_SOURCE] = {
                "type": "geojson",
                "data": borders_url,
                "attribution": "Natural Earth",
            }
            source_ref: dict = {"source": BORDER_SOURCE}
        else:
            # Draw from the basemap's own OpenMapTiles `boundary` layer. Those
            # tiles are already prefetched for the basemap, so the borders
            # overlay costs no additional download.
            source_ref = {"source": "openmaptiles", "source-layer": "boundary"}
        # Maritime boundaries clutter a coastal shot without adding meaning,
        # and admin_level > 2 would draw internal states as if they were
        # international borders.
        border_filter = ["all",
                         ["<=", ["get", "admin_level"], int(bcfg.get("admin_level", 2))],
                         ["!=", ["get", "maritime"], 1]]
        if borders_url:
            border_filter = ["literal", True]
        # A wide translucent glow under a crisp line reads far better on video
        # than a single hairline, which shimmers under compression.
        glow = {
            "id": LAYER_BORDER_GLOW,
            "type": "line",
            "layout": {"visibility": "none", "line-cap": "round", "line-join": "round"},
            "paint": {
                "line-color": bcfg.get("color", "#ffd166"),
                "line-width": float(bcfg.get("width", 1.4)) * 4.0,
                "line-opacity": float(bcfg.get("opacity", 0.9)) * 0.28,
                "line-blur": 3.0,
            },
        }
        line = {
            "id": LAYER_BORDER_LINE,
            "type": "line",
            "layout": {"visibility": "none", "line-cap": "round", "line-join": "round"},
            "paint": {
                "line-color": bcfg.get("color", "#ffd166"),
                "line-width": float(bcfg.get("width", 1.4)),
                "line-opacity": float(bcfg.get("opacity", 0.9)),
            },
        }
        for layer in (glow, line):
            layer.update(source_ref)
            if not borders_url:
                layer["filter"] = border_filter
        style["layers"].insert(insert_at, glow)
        style["layers"].insert(insert_at + 1, line)

    assert_no_overlay_collisions(style)
    return style


def _vector_base(server, name: str = DEFAULT_VECTOR_STYLE) -> dict:
    """An OpenFreeMap published style, with every URL rewritten to local."""
    path = vector_style_path(name)
    entry = server.store.get("ofm", path)
    if entry is None:
        raise RuntimeError(
            f"OpenFreeMap style not in cache ({path}). "
            "Run the prefetch stage before building a style."
        )
    style = json.loads(entry.body)
    style = server.rewrite_urls(style)
    style["glyphs"] = f"{server.upstream_prefix('ofm')}/fonts/{{fontstack}}/{{range}}.pbf"
    return style


def _raster_base(server, basemap: str, satellite_layer: str, when: str,
                 ofm: str, gibs_prefix: str) -> dict:
    """A minimal style: one raster base plus boundaries and place labels."""
    if basemap == "satellite":
        layer = gibs.get_layer(satellite_layer)
        source = {
            "type": "raster",
            "tiles": [layer.tile_template(gibs_prefix, when)],
            "tileSize": 256,
            "maxzoom": layer.max_zoom,
            "attribution": "NASA GIBS/EOSDIS",
        }
    elif basemap == "relief":
        source = {
            "type": "raster",
            "tiles": [f"{ofm}/{NE_RELIEF_PATH}"],
            "tileSize": 256,
            "maxzoom": 6,
            "attribution": "Natural Earth II shaded relief",
        }
    else:
        raise ValueError(f"unknown basemap {basemap!r}; "
                         "expected 'vector', 'satellite' or 'relief'")

    tj = server.store.get("ofm", OFM_TILEJSON_PATH)
    if tj is None:
        raise RuntimeError("OpenFreeMap TileJSON not cached; run prefetch first")
    tilejson = server.rewrite_urls(json.loads(tj.body))

    return {
        "version": 8,
        "name": f"spatialdata-{basemap}",
        "glyphs": f"{ofm}/fonts/{{fontstack}}/{{range}}.pbf",
        "sources": {
            SAT_SOURCE: source,
            "openmaptiles": {"type": "vector", "tiles": tilejson["tiles"],
                             "minzoom": tilejson.get("minzoom", 0),
                             "maxzoom": tilejson.get("maxzoom", 14)},
        },
        "layers": [
            {"id": "sd-background", "type": "background",
             "paint": {"background-color": "#04070d"}},
            {"id": LAYER_BASEMAP_RASTER, "type": "raster", "source": SAT_SOURCE,
             "paint": {"raster-opacity": 1.0}},
            {"id": "sd-admin-line", "type": "line", "source": "openmaptiles",
             "source-layer": "boundary",
             "filter": ["<=", ["get", "admin_level"], 2],
             "paint": {"line-color": "#9fb6cd", "line-width": 0.8, "line-opacity": 0.55}},
            {"id": "sd-place-label", "type": "symbol", "source": "openmaptiles",
             "source-layer": "place",
             "filter": ["in", ["get", "class"], ["literal", ["country", "state", "city"]]],
             "layout": {"text-field": ["get", "name"],
                        "text-font": ["Noto Sans Regular"],
                        "text-size": ["interpolate", ["linear"], ["zoom"], 3, 11, 10, 16]},
             "paint": {"text-color": "#f2f6fb", "text-halo-color": "#04070d",
                       "text-halo-width": 1.4}},
        ],
    }


def _first_line_or_symbol_index(layers: list[dict]) -> int:
    """Index of the first line/symbol layer, i.e. just above the ground fills."""
    for i, layer in enumerate(layers):
        if layer.get("type") in ("line", "symbol"):
            return i
    return len(layers)


def _first_symbol_index(layers: list[dict]) -> int:
    """Index of the first symbol layer, so overlays slot under the labels."""
    for i, layer in enumerate(layers):
        if layer.get("type") == "symbol":
            return i
    return len(layers)


def assert_no_overlay_collisions(style: dict) -> None:
    """The basemap must not reuse an overlay's layer id.

    An overlay id is hidden by default every frame, so a basemap layer that
    borrows one silently disappears from the whole render.
    """
    overlay_ids = {lid for ids in OVERLAY_LAYERS.values() for lid in ids}
    basemap_ids = {l["id"] for l in style.get("layers", [])
                   if (l.get("layout") or {}).get("visibility") != "none"}
    clash = overlay_ids & basemap_ids
    if clash:
        raise AssertionError(
            f"basemap layer(s) {sorted(clash)} reuse an overlay id and would "
            "be hidden by the per-frame overlay controller")


def style_summary(style: dict) -> dict:
    return {
        "sources": sorted(style.get("sources", {})),
        "layers": len(style.get("layers", [])),
        "terrain": bool(style.get("terrain")),
        "overlay_layers": [l["id"] for l in style.get("layers", [])
                           if l["id"].startswith("sd-")],
    }
