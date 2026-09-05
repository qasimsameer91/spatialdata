"""Local worker that renders jobs queued on a remote dashboard.

This is the second half of the local-first / Railway split. The dashboard is
hosted (cheap, always on, does no heavy work); this process runs on the machine
with the GPU and the tile cache. It polls for work, renders locally, streams
progress back, and uploads the finished MP4.

Nothing renders on the hosted side, which is what keeps that side inside a free
tier: it only ever stores JSON job records and receives one file per job.

    python -m worker poll --url https://your-app.up.railway.app --token SECRET
"""
from __future__ import annotations

import os
import platform
import time
from pathlib import Path
from typing import Optional

import urllib.error
import urllib.request
import json

from .config import Config, load_config
from .job import JobSpec, JobState
from .log import get as get_logger

log = get_logger("remote")

WORKER_NAME = f"{platform.node()}"


class RemoteError(RuntimeError):
    pass


class DashboardClient:
    """Thin REST client for the dashboard's job API."""

    def __init__(self, base_url: str, token: Optional[str] = None,
                 timeout: float = 60.0) -> None:
        self.base = base_url.rstrip("/")
        self.token = token or os.environ.get("SPATIALDATA_TOKEN")
        self.timeout = timeout

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 content_type: str = "application/json",
                 timeout: Optional[float] = None) -> dict:
        url = f"{self.base}/api{path}"
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", content_type)
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("X-Spatialdata-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:
                raw = res.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise RemoteError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RemoteError(f"{method} {path} -> {exc.reason}") from None

    # -- API surface ------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health", timeout=15)

    def claim(self) -> Optional[dict]:
        payload = json.dumps({"worker": WORKER_NAME}).encode()
        return self._request("POST", "/jobs/claim", body=payload).get("job")

    def progress(self, job_id: str, **fields) -> None:
        body = json.dumps(fields).encode()
        self._request("POST", f"/jobs/{job_id}/progress", body=body, timeout=30)

    def upload(self, job_id: str, path: Path) -> dict:
        data = Path(path).read_bytes()
        url = f"{self.base}/api/jobs/{job_id}/upload"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("X-Filename", Path(path).name)
        if self.token:
            req.add_header("X-Spatialdata-Token", self.token)
        # Large uploads over a slow link need far more than the default.
        try:
            with urllib.request.urlopen(req, timeout=900) as res:
                return json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise RemoteError(f"upload -> HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise RemoteError(f"upload -> {exc.reason}") from None


def render_remote_job(client: DashboardClient, job: dict,
                      cfg: Config) -> None:
    """Render one claimed job locally and report the result upstream."""
    from .pipeline import Pipeline

    job_id = job["id"]
    spec = JobSpec.from_dict(job.get("spec", {}))
    log.info("claimed job %s (%s)", job_id, spec.place_name)

    last_sent = 0.0

    def on_progress(stage: str, pct: float, message: str) -> None:
        nonlocal last_sent
        now = time.time()
        # Throttle: a 500-frame render would otherwise post hundreds of
        # updates and burn the hosted side's request budget for no benefit.
        terminal = stage in ("done", "failed") or pct >= 1.0
        if not terminal and now - last_sent < 2.0:
            return
        last_sent = now
        try:
            client.progress(job_id, stage=stage, progress=pct, message=message)
        except RemoteError as exc:
            log.warning("progress update failed (continuing render): %s", exc)

    try:
        result = Pipeline(cfg, on_progress=on_progress).run(spec, job_id)
    except Exception as exc:
        log.exception("job %s failed locally", job_id)
        try:
            client.progress(job_id, stage="failed", progress=0.0,
                            message="failed", error=f"{type(exc).__name__}: {exc}")
        except RemoteError:
            pass
        return

    video = result["artifacts"].get("video")
    if video and Path(video).is_file():
        size_mb = Path(video).stat().st_size / 1e6
        log.info("uploading %s (%.1f MB)", Path(video).name, size_mb)
        try:
            client.progress(job_id, stage="encoding", progress=0.95,
                            message=f"uploading {size_mb:.0f} MB")
            client.upload(job_id, Path(video))
        except RemoteError as exc:
            log.error("upload failed: %s", exc)
            client.progress(job_id, stage="failed", progress=1.0,
                            message="render ok, upload failed", error=str(exc))
            return

    client.progress(job_id, stage="done", progress=1.0, message="complete",
                    stats=result["stats"],
                    artifacts={"video": str(video)} if video else {})
    log.info("job %s complete", job_id)


def poll_forever(cfg: Optional[Config] = None, *, base_url: str,
                 token: Optional[str] = None, interval_s: float = 10.0) -> None:
    """Claim and render jobs until interrupted."""
    cfg = cfg or load_config()
    client = DashboardClient(base_url, token)

    try:
        health = client.health()
        log.info("connected to %s (worker_enabled=%s)",
                 base_url, health.get("worker"))
        if health.get("worker"):
            log.warning("the dashboard is also running its own worker; "
                        "start it with --no-worker (or WORKER_ENABLED=0) so "
                        "jobs are not rendered twice")
    except RemoteError as exc:
        log.error("cannot reach dashboard: %s", exc)
        return

    log.info("polling every %.0fs as %r", interval_s, WORKER_NAME)
    backoff = interval_s
    while True:
        try:
            job = client.claim()
            backoff = interval_s
        except RemoteError as exc:
            # Keep retrying, but back off so a dashboard restart or a dropped
            # link does not turn into a hot loop against the hosted side.
            log.warning("claim failed: %s (retrying in %.0fs)", exc, backoff)
            time.sleep(backoff)
            backoff = min(300.0, backoff * 2)
            continue

        if job is None:
            time.sleep(interval_s)
            continue

        render_remote_job(client, job, cfg)
