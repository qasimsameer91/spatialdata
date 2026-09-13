"""Dashboard: job queue API plus the static UI.

The same app runs in two places, which is what makes the local-first / Railway
split work without two codebases:

* **locally** (`python -m worker serve`) it serves the UI *and* runs a worker
  thread that renders queued jobs on this machine.
* **on Railway** (`--no-worker`, or WORKER_ENABLED=0) it serves only the UI and
  the queue. The heavy local machine polls it over the same REST API, so the
  hosted side never renders anything and stays inside a free tier.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import Config, load_config
from .job import OVERLAY_NAMES, STAGES, JobSpec, JobState, JobStore
from .log import get as get_logger

log = get_logger("dashboard")

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"


# A job id becomes a directory name, so it must never be able to walk out of
# the output tree. Starlette will not match "/" inside a path parameter, but
# "%5C" decodes to a backslash, which is a separator on Windows: the id
# "..%5C.." reached files outside output/ and returned them over HTTP.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def safe_job_dir(cfg: Config, job_id: str) -> Path:
    """Resolve a job's output directory, refusing anything that escapes it."""
    if not _JOB_ID_RE.match(job_id or "") or ".." in job_id:
        raise HTTPException(status_code=400, detail="invalid job id")
    root = cfg.path("output").resolve()
    target = (root / job_id).resolve()
    # Belt and braces: even a pattern-clean id must land inside the root.
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="invalid job id")
    return target


def _auth_ok(request: Request, token: Optional[str]) -> bool:
    """Shared-secret check, used only when a token is configured."""
    if not token:
        return True
    supplied = (request.headers.get("X-Spatialdata-Token")
                or request.query_params.get("token"))
    return supplied == token


def create_app(cfg: Optional[Config] = None, *, worker: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    store = JobStore(cfg.path("jobs"))
    token = os.environ.get("SPATIALDATA_TOKEN") or cfg.get("dashboard.token")

    app = FastAPI(title="spatialdata", docs_url="/api/docs")
    app.state.cfg = cfg
    app.state.store = store
    app.state.token = token
    app.state.render_queue: queue.Queue = queue.Queue()
    app.state.worker_enabled = worker

    # ---------------------------------------------------------- middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not _auth_ok(request, token):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        return await call_next(request)

    # ---------------------------------------------------------- meta
    @app.get("/api/meta")
    def meta() -> dict:
        """Everything the UI needs to build its forms."""
        from .audio.mix import list_music
        from .render.style import LABEL_DENSITY_DROP, VECTOR_STYLES
        from .sources import gibs
        from .tts import available_providers, list_voices
        from .tts.catalog import PAID_PROVIDERS

        return {
            "stages": STAGES,
            # Read from the job model, never re-listed here: a hard-coded
            # copy drifts the moment a new overlay is added, leaving the UI
            # offering a layer the worker cannot draw (or hiding one it can).
            "overlays": list(OVERLAY_NAMES),
            "basemaps": ["vector", "satellite", "relief"],
            "vector_styles": list(VECTOR_STYLES),
            "label_densities": sorted(LABEL_DENSITY_DROP),
            "satellite_layers": [
                {"id": k, "label": v.title, "description": v.description}
                for k, v in gibs.LAYERS.items() if v.kind == "satellite"],
            "weather_layers": [
                {"id": k, "label": v.title, "description": v.description}
                for k, v in gibs.LAYERS.items() if v.kind == "weather"],
            "tts_providers": [
                {"id": name, "available": ok,
                 "paid": name in PAID_PROVIDERS,
                 "voices": list_voices(name) if ok else []}
                for name, ok in available_providers().items()],
            "music": list_music(cfg.path("data")),
            "defaults": {
                "width": cfg.get("render.width", 1920),
                "height": cfg.get("render.height", 1080),
                "fps": cfg.get("render.fps", 30),
                "terrain_exaggeration": cfg.get("render.terrain_exaggeration", 1.3),
                "population_color_scale": cfg.get("overlays.population.color_scale"),
            },
            "worker_enabled": app.state.worker_enabled,
            "attribution": cfg.get("attribution"),
        }

    @app.get("/api/geocode")
    async def geocode(q: str = Query(..., min_length=2, max_length=120)) -> dict:
        """Place-name search for the region picker.

        Nominatim's usage policy allows one request per second with an
        identifying User-Agent; the shared Fetcher enforces that and caches
        every result, so repeated searches cost nothing.
        """
        from urllib.parse import urlencode
        from .cache.fetch import Fetcher
        from .cache.store import CacheStore

        query = urlencode({"q": q, "format": "json", "limit": "6",
                           "polygon_bbox": "1"})
        async with Fetcher(CacheStore(cfg.path("cache"))) as fetcher:
            entry = await fetcher.get("nominatim", f"/search?{query}")
        if entry is None:
            return {"results": []}
        try:
            raw = json.loads(entry.body)
        except ValueError:
            return {"results": []}

        results = []
        for item in raw:
            bb = item.get("boundingbox")
            if not bb or len(bb) != 4:
                continue
            # Nominatim gives [south, north, west, east]; we want [w,s,e,n].
            south, north, west, east = (float(v) for v in bb)
            results.append({
                "name": item.get("display_name", "")[:120],
                "short": item.get("name") or item.get("display_name", "")[:40],
                "type": item.get("addresstype") or item.get("type"),
                "bbox": [west, south, east, north],
                "center": [float(item["lon"]), float(item["lat"])],
            })
        return {"results": results, "attribution": "Search: OSM Nominatim"}

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "worker": app.state.worker_enabled,
                "queued": app.state.render_queue.qsize()}

    # ---------------------------------------------------------- jobs
    @app.get("/api/jobs")
    def list_jobs(limit: int = Query(50, ge=1, le=500)) -> dict:
        return {"jobs": [j.as_dict() for j in store.list(limit=limit)]}

    @app.post("/api/jobs")
    def create_job(payload: dict = Body(...)) -> dict:
        try:
            spec = JobSpec.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        state = store.create(spec)
        if app.state.worker_enabled:
            app.state.render_queue.put(state.id)
        log.info("queued job %s (%s)", state.id, spec.place_name)
        return state.as_dict()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        state = store.load(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="job not found")
        return state.as_dict()

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str) -> dict:
        return {"deleted": store.delete(job_id)}

    # ------------------------------------------- remote worker protocol
    @app.post("/api/jobs/claim")
    def claim_job(payload: dict = Body(default={})) -> dict:
        """A remote worker takes the oldest queued job.

        Claiming marks the job started so two workers cannot pick up the same
        one, which is the whole coordination mechanism for the split setup.
        """
        state = store.next_pending()
        if state is None:
            return {"job": None}
        state.started_at = time.time()
        state.message = f"claimed by {payload.get('worker', 'remote')}"
        store.save(state)
        return {"job": state.as_dict()}

    @app.post("/api/jobs/{job_id}/progress")
    def report_progress(job_id: str, payload: dict = Body(...)) -> dict:
        state = store.load(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="job not found")
        for key in ("stage", "progress", "message", "error"):
            if key in payload:
                setattr(state, key, payload[key])
        if payload.get("stats"):
            state.stats.update(payload["stats"])
        if payload.get("artifacts"):
            state.artifacts.update(payload["artifacts"])
        if state.stage in ("done", "failed"):
            state.finished_at = time.time()
        store.save(state)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/upload")
    async def upload_result(job_id: str, request: Request) -> dict:
        """Receive the finished MP4 from a remote worker."""
        state = store.load(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="job not found")
        out_dir = safe_job_dir(cfg, job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = request.headers.get("X-Filename", f"{job_id}.mp4")
        # Keep the uploaded name to a bare filename; a path here would let a
        # remote worker write anywhere on the dashboard host.
        target = out_dir / Path(name).name
        size = 0
        with open(target, "wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                size += len(chunk)
        state.artifacts["video"] = str(target)
        store.save(state)
        log.info("received %s for job %s (%.1f MB)", target.name, job_id, size / 1e6)
        return {"ok": True, "path": str(target), "bytes": size}

    # ---------------------------------------------------------- artifacts
    @app.get("/api/jobs/{job_id}/video")
    def job_video(job_id: str):
        state = store.load(job_id)
        if state is None or "video" not in state.artifacts:
            raise HTTPException(status_code=404, detail="no video for this job")
        path = Path(state.artifacts["video"]).resolve()
        root = cfg.path("output").resolve()
        # The path comes from the job record, which a remote worker writes.
        # Serve it only if it really sits under our own output tree.
        if root not in path.parents:
            raise HTTPException(status_code=400, detail="video path is outside output/")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="video file is missing")
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    @app.get("/api/jobs/{job_id}/preview/{index}")
    def job_preview(job_id: str, index: int):
        previews = sorted((safe_job_dir(cfg, job_id) / "previews").glob("preview_*"))
        if not previews or index >= len(previews):
            raise HTTPException(status_code=404, detail="no such preview")
        return FileResponse(previews[index], media_type="image/jpeg")

    @app.get("/api/jobs/{job_id}/beats")
    def job_beats(job_id: str):
        path = safe_job_dir(cfg, job_id) / "beats.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no beats for this job")
        return Response(path.read_text(encoding="utf-8"),
                        media_type="application/json")

    # ---------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings() -> dict:
        local = cfg.root / "config" / "local.json"
        return {"config": json.loads(local.read_text(encoding="utf-8"))
                if local.is_file() else {}}

    @app.put("/api/settings")
    def put_settings(payload: dict = Body(...)) -> dict:
        local = cfg.root / "config" / "local.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("settings written to %s", local)
        return {"ok": True, "note": "restart the worker to apply"}

    # ---------------------------------------------------------- static UI
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True),
                  name="static")

    return app


def _worker_loop(app: FastAPI) -> None:
    """Render queued jobs one at a time, in a background thread."""
    from .pipeline import run_job

    cfg: Config = app.state.cfg
    store: JobStore = app.state.store

    # Anything left queued from a previous run is picked up on start.
    for state in store.list(limit=200):
        if state.stage == "planning" and state.started_at is None:
            app.state.render_queue.put(state.id)

    while True:
        job_id = app.state.render_queue.get()
        try:
            state = store.load(job_id)
            if state is None or state.is_terminal:
                continue
            log.info("rendering job %s", job_id)
            run_job(store, state, cfg)
        except Exception:
            log.exception("worker loop error on job %s", job_id)
        finally:
            app.state.render_queue.task_done()


def run_server(cfg: Optional[Config] = None, *, host: str = "127.0.0.1",
               port: int = 8000, worker: bool = True) -> None:
    import uvicorn

    cfg = cfg or load_config()
    # Railway injects PORT; honour it so the same command works in both places.
    port = int(os.environ.get("PORT", port))
    if os.environ.get("WORKER_ENABLED", "").strip() in ("0", "false", "no"):
        worker = False

    app = create_app(cfg, worker=worker)
    if worker:
        threading.Thread(target=_worker_loop, args=(app,),
                         name="render-worker", daemon=True).start()
        log.info("render worker thread started")
    else:
        log.info("UI only; jobs must be claimed by a remote worker")

    log.info("dashboard on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
