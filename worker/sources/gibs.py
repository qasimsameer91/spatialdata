"""NASA GIBS (Global Imagery Browse Services) WMTS client.

GIBS is free, key-less and time-enabled: pass a date and you get the imagery
for that day. It is a public-good service, so this module is careful about it:

* the WMTS capabilities document (~5.6 MB) is fetched **once** and cached to
  disk; `describe_layer` reads the cache, never the network
* a built-in registry of the layers this pipeline actually uses means a normal
  render never needs the capabilities document at all
* tiles are addressed through the shared cache/prefetch layer, so the render
  loop reads them from disk

WMTS REST tile paths are ordered ``{TileMatrix}/{TileRow}/{TileCol}``, i.e.
``{z}/{y}/{x}`` - not the ``{z}/{x}/{y}`` most XYZ services use.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from ..log import get as get_logger

log = get_logger("sources.gibs")

UPSTREAM = "gibs"
# EPSG:3857 endpoint - matches MapLibre's web-mercator projection directly.
CAPABILITIES_PATH = "/wmts/epsg3857/best/1.0.0/WMTSCapabilities.xml"
_NS = {
    "wmts": "http://www.opengis.net/wmts/1.0",
    "ows": "http://www.opengis.net/ows/1.1",
}


@dataclass(frozen=True)
class GibsLayer:
    """A GIBS WMTS layer as this pipeline needs to address it."""

    id: str
    title: str
    #: e.g. GoogleMapsCompatible_Level9 - the last number is the max zoom.
    matrix_set: str
    ext: str = "jpg"
    #: True when the layer expects a date in the URL.
    time_enabled: bool = True
    #: Rough visual role, used by the dashboard to group layers.
    kind: str = "satellite"
    description: str = ""

    @property
    def max_zoom(self) -> int:
        m = re.search(r"Level(\d+)", self.matrix_set)
        return int(m.group(1)) if m else 9

    def tile_path(self, z: int, x: int, y: int, when: str) -> str:
        """WMTS REST path for one tile. Note the {z}/{y}/{x} ordering."""
        time = when if self.time_enabled else "default"
        return (f"/wmts/epsg3857/best/{self.id}/default/{time}/"
                f"{self.matrix_set}/{z}/{y}/{x}.{self.ext}")

    def tile_template(self, prefix: str, when: str) -> str:
        """MapLibre tile URL template pointing at the local cache server."""
        time = when if self.time_enabled else "default"
        return (f"{prefix}/wmts/epsg3857/best/{self.id}/default/{time}/"
                f"{self.matrix_set}/{{z}}/{{y}}/{{x}}.{self.ext}")


# Curated registry: the layers this pipeline offers in the dashboard. Chosen
# for daily global coverage and for looking good in a flyover.
LAYERS: dict[str, GibsLayer] = {
    "truecolor_viirs": GibsLayer(
        id="VIIRS_SNPP_CorrectedReflectance_TrueColor",
        title="VIIRS True Colour (Suomi NPP)",
        matrix_set="GoogleMapsCompatible_Level9",
        kind="satellite",
        description="Daily 250m true-colour imagery. The default satellite base layer.",
    ),
    "truecolor_viirs_noaa20": GibsLayer(
        id="VIIRS_NOAA20_CorrectedReflectance_TrueColor",
        title="VIIRS True Colour (NOAA-20)",
        matrix_set="GoogleMapsCompatible_Level9",
        kind="satellite",
        description="Second daily true-colour pass; useful when SNPP has gaps.",
    ),
    "truecolor_modis_terra": GibsLayer(
        id="MODIS_Terra_CorrectedReflectance_TrueColor",
        title="MODIS Terra True Colour",
        matrix_set="GoogleMapsCompatible_Level9",
        kind="satellite",
        description="Morning overpass true colour, available since 2000.",
    ),
    "bluemarble": GibsLayer(
        id="BlueMarble_ShadedRelief_Bathymetry",
        title="Blue Marble Shaded Relief",
        matrix_set="GoogleMapsCompatible_Level8",
        ext="jpg",
        time_enabled=False,
        kind="satellite",
        description="Cloud-free static basemap. Good for establishing shots.",
    ),
    "clouds": GibsLayer(
        id="MODIS_Terra_Cloud_Top_Temp_Day",
        title="Cloud Top Temperature (Terra, day)",
        matrix_set="GoogleMapsCompatible_Level6",
        ext="png",
        kind="weather",
        description="Storm systems and deep convection - reads as weather on screen.",
    ),
    "aerosol": GibsLayer(
        id="MODIS_Combined_Value_Added_AOD",
        title="Aerosol Optical Depth",
        matrix_set="GoogleMapsCompatible_Level6",
        ext="png",
        kind="weather",
        description="Smoke, dust and haze plumes.",
    ),
    "snow": GibsLayer(
        id="MODIS_Terra_NDSI_Snow_Cover",
        title="Snow Cover (NDSI)",
        matrix_set="GoogleMapsCompatible_Level8",
        ext="png",
        kind="weather",
        description="Seasonal snow extent.",
    ),
    "thermal": GibsLayer(
        id="MODIS_Terra_Thermal_Anomalies_All",
        title="Thermal Anomalies / Fires",
        matrix_set="GoogleMapsCompatible_Level9",
        ext="png",
        kind="weather",
        description="Active fire detections.",
    ),
    "night": GibsLayer(
        id="VIIRS_SNPP_DayNightBand_ENCC",
        title="Day/Night Band (city lights)",
        matrix_set="GoogleMapsCompatible_Level8",
        ext="png",
        kind="satellite",
        description="Night-time lights - striking for population narratives.",
    ),
}


def get_layer(key: str) -> GibsLayer:
    if key in LAYERS:
        return LAYERS[key]
    # Allow passing a raw GIBS layer id too.
    for layer in LAYERS.values():
        if layer.id == key:
            return layer
    raise KeyError(f"unknown GIBS layer {key!r}; known: {sorted(LAYERS)}")


def layers_by_kind(kind: str) -> list[GibsLayer]:
    return [l for l in LAYERS.values() if l.kind == kind]


def normalise_date(value: str | _date | datetime | None) -> str:
    """GIBS wants YYYY-MM-DD.

    Imagery for 'today' is usually not published yet, so a bare None resolves
    to two days ago, which is reliably available for the daily products.
    """
    if value is None:
        return (_date.today() - timedelta(days=2)).isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"date must be YYYY-MM-DD, got {text!r}")
    return text


def date_range(start: str, end: str) -> list[str]:
    """Inclusive list of YYYY-MM-DD strings."""
    d0 = datetime.strptime(normalise_date(start), "%Y-%m-%d").date()
    d1 = datetime.strptime(normalise_date(end), "%Y-%m-%d").date()
    if d1 < d0:
        raise ValueError(f"end date {end} precedes start date {start}")
    span = (d1 - d0).days
    if span > 366:
        raise ValueError(f"date range of {span} days is too large (max 366)")
    return [(d0 + timedelta(days=i)).isoformat() for i in range(span + 1)]


# ------------------------------------------------------- capabilities ------
async def ensure_capabilities(fetcher, *, force: bool = False) -> Optional[bytes]:
    """Fetch the WMTS capabilities document once and cache it.

    This document is ~5.6 MB. Per the fair-use rules it is fetched at most once
    and then read from disk forever; pass force=True only to deliberately
    refresh it.
    """
    if not force and fetcher.store.has(UPSTREAM, CAPABILITIES_PATH):
        log.debug("GIBS capabilities already cached")
        entry = fetcher.store.get(UPSTREAM, CAPABILITIES_PATH)
        return entry.body if entry else None
    log.info("fetching GIBS capabilities document (once, ~5.6MB)")
    entry = await fetcher.get(UPSTREAM, CAPABILITIES_PATH, force=force)
    return entry.body if entry else None


def parse_capabilities(xml_bytes: bytes) -> dict[str, dict]:
    """Parse the cached capabilities document into a layer id -> info map."""
    root = ET.fromstring(xml_bytes)
    out: dict[str, dict] = {}
    for node in root.iter(f"{{{_NS['wmts']}}}Layer"):
        ident = node.find("ows:Identifier", _NS)
        if ident is None or not ident.text:
            continue
        info: dict = {"id": ident.text}
        title = node.find("ows:Title", _NS)
        if title is not None:
            info["title"] = title.text
        fmt = node.find("wmts:Format", _NS)
        if fmt is not None and fmt.text:
            info["format"] = fmt.text
            info["ext"] = {"image/jpeg": "jpg", "image/png": "png"}.get(fmt.text, "png")
        link = node.find("wmts:TileMatrixSetLink/wmts:TileMatrixSet", _NS)
        if link is not None:
            info["matrix_set"] = link.text
        for dim in node.findall("wmts:Dimension", _NS):
            d_id = dim.find("ows:Identifier", _NS)
            if d_id is not None and d_id.text == "Time":
                values = [v.text for v in dim.findall("wmts:Value", _NS) if v.text]
                info["time_enabled"] = True
                if values:
                    info["time_extent"] = values[-1]
        out[ident.text] = info
    return out


def describe_layer(store, layer_id: str) -> Optional[dict]:
    """Look one layer up in the cached capabilities document, offline."""
    entry = store.get(UPSTREAM, CAPABILITIES_PATH)
    if entry is None:
        return None
    return parse_capabilities(entry.body).get(layer_id)


def tile_paths(layer: GibsLayer, tiles: Iterable[tuple[int, int, int]],
               when: str) -> list[tuple[str, str]]:
    """Build (upstream, path) pairs for the prefetcher."""
    when = normalise_date(when)
    return [(UPSTREAM, layer.tile_path(z, x, y, when)) for (z, x, y) in tiles]
