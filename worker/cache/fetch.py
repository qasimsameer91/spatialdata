"""Polite, cache-backed HTTP fetching against the registered upstreams.

Everything the pipeline pulls from the network goes through `Fetcher`, which
enforces the fair-use rules in one place:

* a token-bucket rate limit and a concurrency cap per upstream
* exponential backoff with jitter, and respect for Retry-After on 429/503
* cache-first reads, so a warmed cache produces zero network traffic
* negative caching of 404s so prefetch never re-hammers absent tiles
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import aiohttp

from ..log import get as get_logger
from ..sources import upstreams as up_registry
from ..sources.upstreams import USER_AGENT, Upstream
from .store import CacheEntry, CacheStore

log = get_logger("cache.fetch")

# Upstream statuses that are permanent: cache them so we never ask again.
_PERMANENT_MISS = {400, 401, 403, 404, 410}
# Statuses worth retrying after a pause.
_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """Raised when a URL could not be retrieved after all retries."""


@dataclass
class FetchStats:
    requested: int = 0
    from_cache: int = 0
    downloaded: int = 0
    missing: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "FetchStats") -> None:
        self.requested += other.requested
        self.from_cache += other.from_cache
        self.downloaded += other.downloaded
        self.missing += other.missing
        self.failed += other.failed
        self.bytes_downloaded += other.bytes_downloaded
        self.errors.extend(other.errors)

    def as_dict(self) -> dict:
        return {
            "requested": self.requested,
            "from_cache": self.from_cache,
            "downloaded": self.downloaded,
            "missing": self.missing,
            "failed": self.failed,
            "mb_downloaded": round(self.bytes_downloaded / 1e6, 2),
            "errors": self.errors[:10],
        }


class _RateLimiter:
    """Simple async token bucket: at most `rps` starts per second."""

    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / max(0.1, rps)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self.interval


class Fetcher:
    """Cache-first async HTTP client bound to the upstream registry."""

    def __init__(self, store: CacheStore, *, timeout_s: float = 30.0,
                 max_retries: int = 4, backoff_base_s: float = 0.75,
                 rps_scale: float = 1.0) -> None:
        self.store = store
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.rps_scale = rps_scale
        self.stats = FetchStats()
        self._session: aiohttp.ClientSession | None = None
        self._limiters: dict[str, _RateLimiter] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}

    async def __aenter__(self) -> "Fetcher":
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _gate(self, u: Upstream) -> tuple[_RateLimiter, asyncio.Semaphore]:
        if u.name not in self._limiters:
            self._limiters[u.name] = _RateLimiter(u.rps * self.rps_scale)
            self._sems[u.name] = asyncio.Semaphore(u.concurrency)
        return self._limiters[u.name], self._sems[u.name]

    async def get(self, upstream: str, path: str, *,
                  force: bool = False) -> CacheEntry | None:
        """Return the object, from cache when possible.

        Returns None when the upstream authoritatively has no such object.
        Raises FetchError when the object exists but could not be retrieved.
        """
        self.stats.requested += 1
        if not force:
            cached = self.store.get(upstream, path)
            if cached is not None:
                self.stats.from_cache += 1
                return cached
            if self.store.is_known_miss(upstream, path):
                self.stats.missing += 1
                return None

        u = up_registry.get(upstream)
        limiter, sem = self._gate(u)
        url = u.url(path)
        assert self._session is not None, "Fetcher must be used as an async context manager"

        last_err = "unknown"
        for attempt in range(self.max_retries + 1):
            try:
                async with sem:
                    await limiter.acquire()
                    async with self._session.get(url, headers=u.headers or None) as resp:
                        if resp.status == 200:
                            body = await resp.read()
                            ctype = resp.headers.get("Content-Type")
                            self.store.put(upstream, path, body, ctype)
                            self.stats.downloaded += 1
                            self.stats.bytes_downloaded += len(body)
                            return CacheEntry(body, ctype or "application/octet-stream",
                                              from_cache=False)
                        if resp.status in _PERMANENT_MISS:
                            self.store.put_miss(upstream, path, resp.status)
                            self.stats.missing += 1
                            return None
                        if resp.status in _RETRYABLE:
                            last_err = f"HTTP {resp.status}"
                            retry_after = resp.headers.get("Retry-After")
                            delay = self._backoff(attempt, retry_after)
                        else:
                            last_err = f"HTTP {resp.status}"
                            delay = self._backoff(attempt, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network/DNS/timeout
                last_err = f"{type(exc).__name__}: {exc}"
                delay = self._backoff(attempt, None)

            if attempt < self.max_retries:
                log.debug("retry %d/%d %s (%s) in %.1fs",
                          attempt + 1, self.max_retries, url, last_err, delay)
                await asyncio.sleep(delay)

        self.stats.failed += 1
        msg = f"{url} failed after {self.max_retries + 1} attempts: {last_err}"
        self.stats.errors.append(msg)
        raise FetchError(msg)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                # Cap server-suggested waits so one bad header cannot stall a run.
                return min(30.0, float(retry_after))
            except ValueError:
                pass
        return self.backoff_base_s * (2 ** attempt) * (0.6 + 0.8 * random.random())

    async def get_many(self, items: Sequence[tuple[str, str]], *,
                       on_progress=None, tolerate_failures: bool = True) -> FetchStats:
        """Fetch many (upstream, path) pairs concurrently.

        Concurrency is bounded per-upstream by the registry, so passing a large
        list here is safe; it will not burst against the service.
        """
        stats = FetchStats()
        done = 0
        total = len(items)
        lock = asyncio.Lock()

        async def one(upstream: str, path: str) -> None:
            nonlocal done
            try:
                entry = await self.get(upstream, path)
                async with lock:
                    if entry is None:
                        stats.missing += 1
                    elif entry.from_cache:
                        stats.from_cache += 1
                    else:
                        stats.downloaded += 1
                        stats.bytes_downloaded += len(entry.body)
            except FetchError as exc:
                async with lock:
                    stats.failed += 1
                    stats.errors.append(str(exc))
                if not tolerate_failures:
                    raise
            finally:
                async with lock:
                    done += 1
                    stats.requested += 1
                    if on_progress and (done % 25 == 0 or done == total):
                        on_progress(done, total)

        await asyncio.gather(*(one(u, p) for u, p in items))
        return stats


async def fetch_all(store: CacheStore, items: Sequence[tuple[str, str]], **kw) -> FetchStats:
    """Convenience wrapper for a one-shot batch fetch."""
    on_progress = kw.pop("on_progress", None)
    async with Fetcher(store, **kw) as f:
        return await f.get_many(items, on_progress=on_progress)
