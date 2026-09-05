"""One-time downloader for the bundled static datasets and starter music.

Run this once after cloning. Everything it fetches is then read from local
disk forever - the render pipeline never re-downloads these, which is the
fair-use rule for the public-good services this project depends on.

    python data/download_data.py --all
    python data/download_data.py --natural-earth
    python data/download_data.py --kontur 3km
    python data/download_data.py --music

Licences (all free, all attributed automatically in the video's end credits):
    Natural Earth      public domain
    Kontur Population  CC BY 4.0
    Kevin MacLeod      CC BY 4.0 (incompetech.com)
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USER_AGENT = "spatialdata-setup/0.1 (one-time dataset download)"

# -- Natural Earth ---------------------------------------------------------
# Public domain. naciscdn is the project's own CDN; the GitHub mirror is the
# documented fallback.
NE_GEOJSON_BASE = ("https://raw.githubusercontent.com/nvkelso/"
                   "natural-earth-vector/master/geojson/")
NE_MIRROR_BASE = ("https://github.com/nvkelso/natural-earth-vector/raw/"
                  "master/geojson/")
# Natural Earth publishes GeoJSON directly, so no shapefile conversion (and no
# GDAL/GeoPandas dependency) is needed. 110m is enough for global scenes, 50m
# for country scale, 10m for close work.
NATURAL_EARTH = [
    "ne_110m_admin_0_boundary_lines_land",
    "ne_50m_admin_0_boundary_lines_land",
    "ne_10m_admin_0_boundary_lines_land",
]

# -- Kontur Population -----------------------------------------------------
# CC BY. The 3km build is right for country/global scenes; 400m is for city
# zoom-ins and is a much larger download.
KONTUR_BASE = ("https://geodata-eu-central-1-kontur-public.s3.eu-central-1"
               ".amazonaws.com/kontur_datasets/")
KONTUR = {
    # H3 resolution 4 (~22km cells): tiny, and plenty for a whole-continent or
    # global scene where a cell is a pixel or two on screen anyway.
    "22km": {"file": "kontur_population_20231101_r4.gpkg.gz",
             "out": "kontur_population_22km.gpkg", "approx_mb": 7},
    # H3 resolution 6 (~3km cells): the default. Country and region scale.
    "3km": {"file": "kontur_population_20231101_r6.gpkg.gz",
            "out": "kontur_population_3km.gpkg", "approx_mb": 185},
    # H3 resolution 8 (~400m cells): city zoom-ins. Large - it unpacks to
    # roughly 8-10 GB, so check free disk before pulling this one.
    "400m": {"file": "kontur_population_20231101.gpkg.gz",
             "out": "kontur_population_400m.gpkg", "approx_mb": 2437},
}

# -- Starter music ---------------------------------------------------------
# Kevin MacLeod, CC BY 4.0. Chosen for slow, unobtrusive documentary beds that
# sit well under narration.
MUSIC_BASE = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
MUSIC = [
    ("Ossuary 1 - A Beginning.mp3", "ossuary_1_a_beginning.mp3"),
    ("Long Note Two.mp3", "long_note_two.mp3"),
    ("Impact Lento.mp3", "impact_lento.mp3"),
    ("Ethereal Relaxation.mp3", "ethereal_relaxation.mp3"),
    ("Deep Haze.mp3", "deep_haze.mp3"),
]

MUSIC_CREDITS = """Starter music bundled with spatialdata
=======================================

All tracks by Kevin MacLeod (incompetech.com), licensed under
Creative Commons: By Attribution 4.0 International
https://creativecommons.org/licenses/by/4.0/

The render pipeline burns an attribution credit into every video, but if you
publish a video using one of these beds you should keep the music credit in
your description too:

    Music by Kevin MacLeod (incompetech.com), CC BY 4.0

Replace these with your own tracks by dropping audio files into this folder;
they show up in the dashboard's music picker automatically.
"""


def _fetch(url: str, dest: Path, *, timeout: int = 120) -> bool:
    """Download one file with a progress line. Returns False on failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        started = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = 100 * done / total
                        sys.stdout.write(
                            f"\r    {dest.name}: {pct:5.1f}% "
                            f"({done/1e6:.1f}/{total/1e6:.1f} MB)")
                    else:
                        sys.stdout.write(
                            f"\r    {dest.name}: {done/1e6:.1f} MB")
                    sys.stdout.flush()
        tmp.replace(dest)
        print(f"\r    {dest.name}: done ({dest.stat().st_size/1e6:.1f} MB "
              f"in {time.time()-started:.0f}s)" + " " * 20)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        print(f"\r    {dest.name}: FAILED - {exc}" + " " * 20)
        return False


def _try_mirrors(urls: list[str], dest: Path) -> bool:
    for i, url in enumerate(urls):
        if i:
            print(f"    trying mirror {i + 1}/{len(urls)}")
        if _fetch(url, dest):
            return True
    return False


def download_natural_earth(resolutions: tuple[str, ...] = ("110m", "50m", "10m")) -> int:
    """Boundary lines as GeoJSON, straight from the Natural Earth repo."""
    out_dir = ROOT / "natural_earth"
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for name in NATURAL_EARTH:
        if not any(f"_{r}_" in name for r in resolutions):
            continue
        dest = out_dir / f"{name}.geojson"
        if dest.is_file():
            print(f"  {name}: already present "
                  f"({dest.stat().st_size/1e6:.1f} MB)")
            ok += 1
            continue
        urls = [NE_GEOJSON_BASE + dest.name, NE_MIRROR_BASE + dest.name]
        if _try_mirrors(urls, dest):
            ok += 1
    return ok


def download_kontur(resolution: str = "3km") -> bool:
    entry = KONTUR.get(resolution)
    if entry is None:
        print(f"  unknown Kontur resolution {resolution!r}; "
              f"choose from {sorted(KONTUR)}")
        return False
    out_dir = ROOT / "kontur"
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / entry["out"]
    if final.is_file():
        print(f"  {entry['out']}: already present "
              f"({final.stat().st_size/1e6:.0f} MB)")
        return True

    print(f"  {entry['out']}: ~{entry['approx_mb']} MB download")
    if entry["approx_mb"] > 1000:
        free_gb = shutil.disk_usage(out_dir).free / 1e9
        # The gzip unpacks to several times its download size.
        need_gb = entry["approx_mb"] * 4.5 / 1000
        print(f"    disk: {free_gb:.1f} GB free, roughly {need_gb:.1f} GB needed")
        if free_gb < need_gb:
            print("    not enough free space; use --kontur 3km instead")
            return False

    gz_path = out_dir / (entry["out"] + ".gz")
    url = KONTUR_BASE + entry["file"]
    # Large S3 transfers get reset often enough that one attempt is not enough.
    for attempt in range(1, 4):
        if _fetch(url, gz_path, timeout=900):
            break
        if attempt < 3:
            print(f"    retrying ({attempt + 1}/3) after a pause...")
            time.sleep(5 * attempt)
    else:
        return False

    print("    decompressing...")
    try:
        with gzip.open(gz_path, "rb") as src, open(final, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 22)
        gz_path.unlink(missing_ok=True)
        print(f"    {final.name}: {final.stat().st_size/1e6:.0f} MB")
        return True
    except OSError as exc:
        print(f"    decompression failed: {exc}")
        return False


def download_music() -> int:
    out_dir = ROOT / "music"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "CREDITS.txt").write_text(MUSIC_CREDITS, encoding="utf-8")
    ok = 0
    for remote, local in MUSIC:
        dest = out_dir / local
        if dest.is_file():
            print(f"  {local}: already present")
            ok += 1
            continue
        url = MUSIC_BASE + urllib.parse.quote(remote)
        if _fetch(url, dest, timeout=180):
            ok += 1
    return ok


def main() -> int:
    import urllib.parse  # noqa: F401  (used in download_music)

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="fetch everything")
    parser.add_argument("--natural-earth", action="store_true")
    parser.add_argument("--kontur", nargs="?", const="3km",
                        choices=sorted(KONTUR), help="population dataset resolution")
    parser.add_argument("--music", action="store_true")
    args = parser.parse_args()

    if not any([args.all, args.natural_earth, args.kontur, args.music]):
        parser.print_help()
        return 1

    results: dict[str, str] = {}

    if args.all or args.natural_earth:
        print("\nNatural Earth (public domain)")
        count = download_natural_earth()
        results["natural_earth"] = f"{count} file(s)"

    if args.all or args.kontur:
        print("\nKontur Population (CC BY)")
        ok = download_kontur(args.kontur or "3km")
        results["kontur"] = "ok" if ok else "failed"

    if args.all or args.music:
        print("\nStarter music (Kevin MacLeod, CC BY 4.0)")
        count = download_music()
        results["music"] = f"{count} track(s)"

    print("\nSummary")
    for key, value in results.items():
        print(f"  {key:16} {value}")
    print("\nAnything that failed is optional: the borders overlay falls back "
          "to the basemap's own boundary layer, and a job simply skips the "
          "population overlay when no Kontur file is present.")
    return 0


if __name__ == "__main__":
    import urllib.parse
    raise SystemExit(main())
