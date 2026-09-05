"""Registry of the free, no-key upstream services this pipeline is allowed to use.

Every network read in the pipeline goes through one of these entries, which is
what makes the fair-use rules enforceable in one place: politeness limits live
on the Upstream, and the cache layer keys off its name.

Nothing here requires an API key, a token, or a credit card. Do not add a
metered or key-gated service to this table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

USER_AGENT = (
    "spatialdata-render-worker/0.1 "
    "(+local documentary render pipeline; contact: local operator) "
    "python-aiohttp"
)


@dataclass(frozen=True)
class Upstream:
    name: str
    base_url: str
    #: Human-readable note about why this service is free to use.
    terms: str
    #: Requests/second ceiling applied by the fetcher. These are public-good
    #: services, so the ceilings are deliberately conservative.
    rps: float = 8.0
    concurrency: int = 4
    headers: Dict[str, str] = field(default_factory=dict)

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


UPSTREAMS: Dict[str, Upstream] = {
    "ofm": Upstream(
        name="ofm",
        base_url="https://tiles.openfreemap.org",
        terms="OpenFreeMap: free and unlimited, no API key. Data (c) OpenStreetMap contributors.",
        rps=12.0,
        concurrency=6,
    ),
    "mapterhorn": Upstream(
        name="mapterhorn",
        base_url="https://tiles.mapterhorn.com",
        terms="Mapterhorn terrain-RGB raster DEM: free, no API key.",
        rps=10.0,
        concurrency=6,
    ),
    "nominatim": Upstream(
        name="nominatim",
        base_url="https://nominatim.openstreetmap.org",
        terms=(
            "OSM Nominatim: free, no key. Its usage policy caps this at one "
            "request per second with an identifying User-Agent, so the limits "
            "below are set accordingly and every lookup is cached. Only the "
            "dashboard's place search uses it - never the render loop."
        ),
        rps=1.0,
        concurrency=1,
    ),
    "gibs": Upstream(
        name="gibs",
        base_url="https://gibs.earthdata.nasa.gov",
        terms=(
            "NASA GIBS/EOSDIS: free, no key, no signup. Public-good service - "
            "capabilities documents are fetched once and cached; tiles are "
            "pre-fetched before rendering, never during the frame loop."
        ),
        rps=8.0,
        concurrency=4,
    ),
}


def split_url(url: str) -> tuple[str, str] | None:
    """Resolve a URL to (upstream_name, path), or None if it is not ours.

    Accepts both the local proxy form (``http://127.0.0.1:PORT/up/ofm/x.pbf``)
    and the raw upstream form (``https://tiles.openfreemap.org/x.pbf``). Tile
    templates reach us in either shape depending on whether they came from a
    rewritten style or from a TileJSON body, and both must resolve.
    """
    if not isinstance(url, str):
        return None
    marker = "/up/"
    idx = url.find(marker)
    if idx != -1 and url.startswith("http"):
        rest = url[idx + len(marker):]
        name, _, path = rest.partition("/")
        if name in UPSTREAMS:
            return name, "/" + path
    for name, u in UPSTREAMS.items():
        base = u.base_url.rstrip("/")
        if url.startswith(base):
            return name, url[len(base):] or "/"
    return None


def get(name: str) -> Upstream:
    try:
        return UPSTREAMS[name]
    except KeyError:
        raise KeyError(
            f"unknown upstream {name!r}; known: {sorted(UPSTREAMS)}. "
            "Any addition must be free and key-less."
        ) from None
