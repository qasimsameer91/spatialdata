"""Local HTTP server that feeds the headless renderer entirely from disk cache.

The render page never talks to the internet. Every tile, glyph, sprite and
style it requests is rewritten to point at this server, which answers from the
`CacheStore`. That is what guarantees the fair-use rule that the frame loop
makes no live network request.

Miss policy decides what happens when the renderer asks for something the
prefetch pass did not warm:

* ``offline`` - refuse and serve a transparent placeholder. Guarantees zero
  live traffic; a gap shows up as a blank tile and a loud warning.
* ``warn``    - fetch it upstream, serve it, and log loudly. Never breaks a
  render, and the warning count tells you the prefetcher needs tuning.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from ..log import get as get_logger
from ..sources import upstreams as up_registry
from .fetch import Fetcher
from .store import CacheStore, content_type_for

log = get_logger("cache.server")

# 1x1 transparent PNG, served when an image tile is missing in offline mode.
_BLANK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)
# An empty vector tile is simply a zero-length protobuf.
_BLANK_PBF = b""


class TileServer:
    """Serves cached upstream content plus the render page, on localhost."""

    def __init__(self, store: CacheStore, *, miss_policy: str = "warn",
                 host: str = "127.0.0.1", port: int = 0) -> None:
        if miss_policy not in ("warn", "offline"):
            raise ValueError(f"miss_policy must be 'warn' or 'offline', got {miss_policy!r}")
        self.store = store
        self.miss_policy = miss_policy
        self.host = host
        self._requested_port = port
        self.port: int | None = None
        self.misses: list[str] = []
        self.served = 0

        self._mounts: dict[str, Path] = {}
        self._json_docs: dict[str, bytes] = {}
        # Memoised URL-rewritten JSON bodies (styles, TileJSON).
        self._rewritten: dict[str, bytes] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: web.AppRunner | None = None
        self._ready = threading.Event()
        self._fetcher: Fetcher | None = None

    # -- public API --------------------------------------------------------
    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("TileServer is not started")
        return f"http://{self.host}:{self.port}"

    def mount(self, name: str, directory: Path) -> str:
        """Expose a local directory at /local/<name>/... and return its URL."""
        self._mounts[name] = Path(directory)
        return f"{self.base_url}/local/{name}" if self.port else f"/local/{name}"

    def put_json(self, name: str, doc: Any) -> str:
        """Serve an in-memory JSON document at /doc/<name>.json."""
        self._json_docs[name] = json.dumps(doc).encode("utf-8")
        return f"{self.base_url}/doc/{name}.json"

    def upstream_prefix(self, upstream: str) -> str:
        return f"{self.base_url}/up/{upstream}"

    def rewrite_urls(self, doc: Any) -> Any:
        """Recursively rewrite upstream URLs in a style/TileJSON to local ones."""
        if isinstance(doc, str):
            for name, u in up_registry.UPSTREAMS.items():
                base = u.base_url.rstrip("/")
                if doc.startswith(base):
                    return self.upstream_prefix(name) + doc[len(base):]
            return doc
        if isinstance(doc, list):
            return [self.rewrite_urls(v) for v in doc]
        if isinstance(doc, dict):
            return {k: self.rewrite_urls(v) for k, v in doc.items()}
        return doc

    def start(self) -> "TileServer":
        self._thread = threading.Thread(target=self._run, name="tile-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise RuntimeError("TileServer failed to start within 20s")
        log.info("tile server on %s (miss_policy=%s)", self.base_url, self.miss_policy)
        return self

    def stop(self) -> None:
        if self._loop and self._runner:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=15)
        if self._thread:
            self._thread.join(timeout=10)
        self._loop = None
        self._thread = None

    def __enter__(self) -> "TileServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def miss_report(self) -> dict:
        return {"served": self.served, "misses": len(self.misses),
                "sample": self.misses[:10]}

    # -- server internals --------------------------------------------------
    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self._h_health)
        app.router.add_get("/doc/{name}.json", self._h_doc)
        app.router.add_get("/local/{name}/{path:.*}", self._h_local)
        app.router.add_get("/up/{upstream}/{path:.*}", self._h_upstream)
        app.router.add_get("/render/{path:.*}", self._h_render)

        self._fetcher = Fetcher(self.store)
        await self._fetcher.__aenter__()

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self._requested_port)
        await site.start()
        sockets = list(self._runner.addresses)
        self.port = sockets[0][1] if sockets else self._requested_port
        self._ready.set()

    async def _shutdown(self) -> None:
        if self._fetcher:
            await self._fetcher.__aexit__(None, None, None)
            self._fetcher = None
        if self._runner:
            await self._runner.cleanup()
        self._loop.call_soon_threadsafe(self._loop.stop)

    # -- handlers ----------------------------------------------------------
    async def _h_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "served": self.served,
                                  "misses": len(self.misses)})

    async def _h_doc(self, request: web.Request) -> web.Response:
        body = self._json_docs.get(request.match_info["name"])
        if body is None:
            raise web.HTTPNotFound()
        self.served += 1
        return web.Response(body=body, content_type="application/json",
                            headers={"Access-Control-Allow-Origin": "*"})

    async def _h_local(self, request: web.Request) -> web.StreamResponse:
        root = self._mounts.get(request.match_info["name"])
        if root is None:
            raise web.HTTPNotFound()
        rel = request.match_info["path"]
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            raise web.HTTPForbidden()
        if not target.is_file():
            raise web.HTTPNotFound()
        self.served += 1
        return web.FileResponse(target, headers={"Access-Control-Allow-Origin": "*"})

    async def _h_render(self, request: web.Request) -> web.StreamResponse:
        root = (Path(__file__).resolve().parent.parent / "render")
        rel = request.match_info["path"] or "map.html"
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise web.HTTPForbidden()
        if not target.is_file():
            raise web.HTTPNotFound()
        self.served += 1
        return web.FileResponse(target)

    async def _h_upstream(self, request: web.Request) -> web.Response:
        upstream = request.match_info["upstream"]
        path = "/" + request.match_info["path"]
        if request.query_string:
            path += "?" + request.query_string

        entry = self.store.get(upstream, path)
        if entry is not None:
            self.served += 1
            return self._ok(*self._localise(entry.body, entry.content_type, path))

        if self.store.is_known_miss(upstream, path):
            return self._placeholder(path, reason="known-404")

        # Not warmed by the prefetch pass.
        self.misses.append(f"{upstream}{path}")
        if self.miss_policy == "offline":
            log.warning("OFFLINE MISS %s%s - prefetch did not warm this URL", upstream, path)
            return self._placeholder(path, reason="offline-miss")

        log.warning("LIVE FETCH during render: %s%s (prefetch missed it)", upstream, path)
        try:
            fetched = await self._fetcher.get(upstream, path)
        except Exception as exc:
            log.error("live fetch failed %s%s: %s", upstream, path, exc)
            return self._placeholder(path, reason="fetch-failed")
        if fetched is None:
            return self._placeholder(path, reason="upstream-404")
        self.served += 1
        return self._ok(*self._localise(fetched.body, fetched.content_type, path))

    def _localise(self, body: bytes, ctype: str, path: str) -> tuple[bytes, str]:
        """Rewrite upstream URLs inside JSON documents to point back at us.

        A TileJSON or style document is cached verbatim, so its tile templates
        are absolute upstream URLs. Served unmodified, MapLibre would follow
        them straight to the internet mid-render and bypass the cache
        entirely, which would silently break the no-live-requests guarantee.
        """
        if "json" not in (ctype or "") and not path.endswith((".json", "/planet")):
            return body, ctype
        cached = self._rewritten.get(path)
        if cached is not None:
            return cached, "application/json"
        try:
            doc = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body, ctype
        out = json.dumps(self.rewrite_urls(doc)).encode("utf-8")
        self._rewritten[path] = out
        return out, "application/json"

    def _ok(self, body: bytes, ctype: str) -> web.Response:
        return web.Response(
            body=body,
            content_type=ctype.split(";")[0].strip() or "application/octet-stream",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=86400"},
        )

    def _placeholder(self, path: str, reason: str) -> web.Response:
        ctype = content_type_for(path)
        if "protobuf" in ctype or "vector" in ctype or path.endswith(".pbf"):
            body, ctype = _BLANK_PBF, "application/x-protobuf"
        elif ctype.startswith("image/"):
            body, ctype = _BLANK_PNG, "image/png"
        else:
            # A missing style or TileJSON is fatal to the render; do not fake it.
            raise web.HTTPNotFound(reason=reason)
        return web.Response(body=body, content_type=ctype,
                            headers={"Access-Control-Allow-Origin": "*",
                                     "X-Cache-Miss": reason})
