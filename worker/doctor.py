"""Environment checks: does this machine have what the pipeline needs?"""
from __future__ import annotations

import shutil
import socket
import sys
from pathlib import Path

CHECKS_PASSED = "  ok   "
CHECKS_WARN = "  warn "
CHECKS_FAIL = "  FAIL "


def _probe_host(host: str, port: int = 443, timeout: float = 6.0) -> bool:
    try:
        socket.getaddrinfo(host, port)
    except OSError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_checks(cfg) -> bool:
    ok = True
    print("spatialdata environment check\n")

    print("tools")
    for tool, required in (("ffmpeg", True), ("ffprobe", True)):
        found = shutil.which(tool)
        print(f"{CHECKS_PASSED if found else CHECKS_FAIL}{tool:10} "
              f"{found or 'not on PATH'}")
        ok = ok and (bool(found) or not required)

    print("\npython packages")
    for module, required in (("playwright", True), ("aiohttp", True),
                             ("numpy", True), ("PIL", True), ("cv2", False),
                             ("kokoro", False), ("faster_whisper", False),
                             ("fastapi", False), ("uvicorn", False)):
        try:
            __import__(module)
            print(f"{CHECKS_PASSED}{module}")
        except ImportError:
            level = CHECKS_FAIL if required else CHECKS_WARN
            print(f"{level}{module} not installed")
            ok = ok and not required

    print("\nhardware encoder")
    from .encode import has_encoder
    if has_encoder("h264_amf"):
        print(f"{CHECKS_PASSED}h264_amf available (AMD hardware encode)")
    else:
        print(f"{CHECKS_WARN}h264_amf missing; encodes fall back to libx264 (slow)")

    print("\nheadless browser")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-angle=d3d11"])
            page = browser.new_page()
            renderer = page.evaluate(
                """() => {const c=document.createElement('canvas');
                   const gl=c.getContext('webgl2')||c.getContext('webgl');
                   if(!gl) return 'no-webgl';
                   const d=gl.getExtension('WEBGL_debug_renderer_info');
                   return d?gl.getParameter(d.UNMASKED_RENDERER_WEBGL):'unknown';}"""
            )
            browser.close()
        soft = "swiftshader" in str(renderer).lower()
        print(f"{CHECKS_WARN if soft else CHECKS_PASSED}webgl: {renderer}")
        if soft:
            print("       (software rendering - frames will render slowly)")
    except Exception as exc:
        print(f"{CHECKS_FAIL}chromium: {exc}")
        print("       run: python -m playwright install chromium")
        ok = False

    print("\nupstream services")
    for host, label in (("tiles.openfreemap.org", "basemap"),
                        ("tiles.mapterhorn.com", "terrain"),
                        ("gibs.earthdata.nasa.gov", "satellite/weather")):
        reachable = _probe_host(host)
        print(f"{CHECKS_PASSED if reachable else CHECKS_WARN}{label:18} {host}"
              f"{'' if reachable else '  UNREACHABLE'}")

    print("\nbundled data")
    data = cfg.path("data")
    for sub, note in (("natural_earth", "borders fall back to the basemap layer"),
                      ("kontur", "population overlay is skipped"),
                      ("music", "no background music available")):
        files = list((data / sub).glob("*")) if (data / sub).is_dir() else []
        real = [f for f in files if f.is_file() and f.suffix != ".txt"]
        if real:
            print(f"{CHECKS_PASSED}{sub:16} {len(real)} file(s)")
        else:
            print(f"{CHECKS_WARN}{sub:16} empty - {note}")

    print("\ncache")
    from .cache.store import CacheStore
    stats = CacheStore(cfg.path("cache")).stats()
    print(f"       {stats['files']:,} files, {stats['bytes']/1e6:,.1f} MB")

    print("\n" + ("all required checks passed" if ok
                  else "some required checks FAILED"))
    return ok
