"""Content cache on local disk, keyed by (upstream, url path).

The cache is deliberately generic: tiles, style JSON, sprites, glyphs and WMTS
capabilities documents all flow through one code path. That is what lets the
render loop point MapLibre at a purely local URL and make zero live requests.

Negative results (404) are cached too, so a prefetch pass does not hammer an
upstream for tiles that legitimately do not exist, e.g. coordinates outside a
GIBS layer's coverage.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..log import get as get_logger

log = get_logger("cache.store")

# Extensions we can map back to a content type without writing a sidecar file.
_CONTENT_TYPES = {
    ".pbf": "application/x-protobuf",
    ".mvt": "application/vnd.mapbox-vector-tile",
    ".json": "application/json",
    ".geojson": "application/geo+json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".xml": "text/xml",
    ".txt": "text/plain",
    ".css": "text/css",
    ".js": "application/javascript",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._\-/]")

#: Windows refuses paths over 260 characters by default, and a long query
#: string can easily push a cache filename past it - which failed the write
#: and, with it, the whole prefetch. Long components are replaced by a digest
#: so the key stays unique and the path stays short.
_MAX_COMPONENT = 120
_MISS_SUFFIX = ".__miss__"
_INTERNAL_SUFFIXES = (_MISS_SUFFIX, ".ct", ".part")


def content_type_for(path: str) -> str:
    ext = os.path.splitext(path.split("?")[0])[1].lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _shorten(rel: str) -> str:
    """Keep every path component within the filesystem's length limit."""
    parts = rel.split("/")
    out: list[str] = []
    for part in parts:
        if len(part) <= _MAX_COMPONENT:
            out.append(part)
            continue
        stem, ext = os.path.splitext(part)
        digest = hashlib.sha1(part.encode("utf-8")).hexdigest()[:16]
        keep = _MAX_COMPONENT - len(ext) - len(digest) - 1
        out.append(f"{stem[:max(0, keep)]}_{digest}{ext}")
    return "/".join(out)


@dataclass
class CacheEntry:
    body: bytes
    content_type: str
    from_cache: bool = True


class CacheStore:
    """Disk-backed cache rooted at ``<cache_dir>/http/<upstream>/<path>``."""

    def __init__(self, cache_dir: Path) -> None:
        self.root = Path(cache_dir) / "http"
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    # -- key / path handling ---------------------------------------------
    def _safe_rel(self, path: str) -> str:
        """Turn a URL path into a safe relative filesystem path.

        Query strings become part of the filename so two URLs differing only by
        query cannot collide, and traversal segments are stripped so a hostile
        or malformed path can never escape the cache root.
        """
        raw = path.split("#", 1)[0]
        url_path, _, query = raw.partition("?")
        parts = [p for p in url_path.split("/") if p not in ("", ".", "..")]
        rel = "/".join(parts) if parts else "__root__"
        if query:
            rel += "__q_" + query
        rel = _UNSAFE.sub("_", rel)
        if not os.path.splitext(rel)[1]:
            # Extensionless URLs (e.g. /styles/liberty) still need a real file.
            rel = rel + "__index"
        return _shorten(rel)

    def path_for(self, upstream: str, path: str) -> Path:
        return self.root / upstream / self._safe_rel(path)

    def _miss_marker(self, upstream: str, path: str) -> Path:
        return Path(str(self.path_for(upstream, path)) + _MISS_SUFFIX)

    # -- reads ------------------------------------------------------------
    def has(self, upstream: str, path: str) -> bool:
        return self.path_for(upstream, path).is_file()

    def is_known_miss(self, upstream: str, path: str) -> bool:
        """True if we previously recorded an authoritative 404 for this URL."""
        return self._miss_marker(upstream, path).is_file()

    def get(self, upstream: str, path: str) -> Optional[CacheEntry]:
        target = self.path_for(upstream, path)
        if not target.is_file():
            self.misses += 1
            return None
        ctype = content_type_for(path)
        sidecar = Path(str(target) + ".ct")
        if sidecar.is_file():
            ctype = sidecar.read_text(encoding="utf-8").strip() or ctype
        self.hits += 1
        return CacheEntry(target.read_bytes(), ctype, from_cache=True)

    # -- writes -----------------------------------------------------------
    def put(self, upstream: str, path: str, body: bytes,
            content_type: str | None = None) -> Path:
        """Atomically store a response body."""
        target = self.path_for(upstream, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory then replace, so a crash
        # mid-write can never leave a truncated tile in the cache.
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        if content_type:
            declared = content_type.split(";")[0].strip()
            if declared and declared != content_type_for(path):
                Path(str(target) + ".ct").write_text(content_type, encoding="utf-8")
        self._miss_marker(upstream, path).unlink(missing_ok=True)
        return target

    def put_miss(self, upstream: str, path: str, status: int) -> None:
        """Record that the upstream authoritatively has no such object."""
        marker = self._miss_marker(upstream, path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"status": status}), encoding="utf-8")

    # -- maintenance ------------------------------------------------------
    def stats(self) -> dict:
        files = 0
        total = 0
        for p in self.root.rglob("*"):
            if p.is_file() and not p.name.endswith(_INTERNAL_SUFFIXES):
                files += 1
                total += p.stat().st_size
        return {"files": files, "bytes": total, "hits": self.hits, "misses": self.misses}

    def clear(self, upstream: str | None = None) -> None:
        target = self.root / upstream if upstream else self.root
        if target.exists():
            shutil.rmtree(target)
        self.root.mkdir(parents=True, exist_ok=True)
