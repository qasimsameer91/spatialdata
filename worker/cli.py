"""Command line for the local render worker.

    python -m worker render --place "The Alps" --bbox 7.3,45.85,8.05,46.25
    python -m worker render --job examples/alps.json
    python -m worker serve            # dashboard on localhost
    python -m worker poll             # take jobs from a Railway dashboard
    python -m worker doctor           # check the environment
    python -m worker cache --stats
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import load_config
from .job import JobSpec, JobStore
from .log import get as get_logger, setup as setup_logging

log = get_logger("cli")


def _parse_bbox(text: str) -> list[float]:
    parts = [p for p in text.replace(" ", "").split(",") if p]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"bbox needs 4 comma-separated numbers (west,south,east,north), "
            f"got {len(parts)}")
    try:
        return [float(p) for p in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"bbox values must be numbers: {exc}")


def _spec_from_args(args) -> JobSpec:
    if args.job:
        data = json.loads(Path(args.job).read_text(encoding="utf-8"))
        spec = JobSpec.from_dict(data)
    else:
        spec = JobSpec()

    # Explicit flags override anything in the job file.
    overrides = {
        "bbox": args.bbox, "place_name": args.place, "title": args.title,
        "date": args.date, "date_end": args.date_end, "basemap": args.basemap,
        "vector_style": args.style, "label_density": args.labels,
        "satellite_layer": args.satellite_layer, "weather_layer": args.weather_layer,
        "duration_s": args.duration, "fps": args.fps,
        "tts_provider": args.tts, "tts_voice": args.voice, "music": args.music,
    }
    for key, value in overrides.items():
        if value is not None:
            setattr(spec, key, value)

    if args.overlays is not None:
        spec.overlays = [o for o in args.overlays.split(",") if o]
    if args.narration:
        path = Path(args.narration)
        spec.narration = (path.read_text(encoding="utf-8")
                          if path.is_file() else args.narration)
    if args.width and args.height:
        spec.width, spec.height = args.width, args.height
    if args.no_terrain:
        spec.terrain = False
    spec.validate()
    return spec


def cmd_render(args) -> int:
    from .pipeline import Pipeline

    spec = _spec_from_args(args)
    overrides: dict = {}
    if args.offline:
        overrides["render"] = {"miss_policy": "offline"}
    cfg = load_config(overrides)

    job_id = args.id or f"cli-{time.strftime('%Y%m%d-%H%M%S')}"
    print(f"job {job_id}: {spec.place_name} "
          f"[{', '.join(spec.overlays) or 'no overlays'}] "
          f"{spec.width}x{spec.height}@{spec.fps}")

    # Record the job so a CLI render shows up in the dashboard alongside
    # queued ones, rather than being invisible to the UI that can play it.
    store = JobStore(cfg.path("jobs"))
    state = store.create(spec, job_id=job_id)
    state.started_at = time.time()
    store.save(state)

    started = time.time()
    try:
        result = Pipeline(cfg).run(spec, job_id)
    except Exception as exc:
        state.stage, state.error = "failed", f"{type(exc).__name__}: {exc}"
        state.finished_at = time.time()
        store.save(state)
        raise
    stats = result["stats"]

    state.artifacts.update(result["artifacts"])
    state.stats.update(stats)
    state.stage, state.progress, state.message = "done", 1.0, "complete"
    state.finished_at = time.time()
    store.save(state)

    print(f"\ndone in {time.time() - started:.0f}s")
    print(f"  video    {stats['video']['path']}")
    print(f"           {stats['video']['size_mb']} MB, "
          f"{stats['video']['duration_s']}s")
    print(f"  frames   {stats['frames']} at {stats['render']['render_fps']} fps")
    misses = stats.get("render_misses", {}).get("misses", 0)
    print(f"  cache    {stats['prefetch_plan']['total']} objects planned, "
          f"{misses} miss(es) during render")
    if misses:
        print("           (misses mean the prefetch was under-warmed; "
              "the render still completed)")
    return 0


def cmd_serve(args) -> int:
    from .dashboard_app import run_server
    cfg = load_config()
    run_server(cfg, host=args.host, port=args.port, worker=not args.no_worker)
    return 0


def cmd_poll(args) -> int:
    from .remote import poll_forever
    cfg = load_config()
    poll_forever(cfg, base_url=args.url, token=args.token,
                 interval_s=args.interval)
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run_checks
    return 0 if run_checks(load_config()) else 1


def cmd_cache(args) -> int:
    from .cache.store import CacheStore
    cfg = load_config()
    store = CacheStore(cfg.path("cache"))
    if args.clear:
        store.clear(args.upstream)
        print(f"cleared cache for {args.upstream or 'all upstreams'}")
        return 0
    stats = store.stats()
    print(f"cache at {store.root}")
    print(f"  files {stats['files']:,}")
    print(f"  size  {stats['bytes']/1e6:,.1f} MB")
    return 0


def cmd_jobs(args) -> int:
    cfg = load_config()
    store = JobStore(cfg.path("jobs"))
    jobs = store.list(limit=args.limit)
    if not jobs:
        print("no jobs")
        return 0
    print(f"{'id':30} {'stage':18} {'progress':>8}  message")
    for job in jobs:
        print(f"{job.id:30} {job.stage:18} {job.progress*100:7.0f}%  "
              f"{job.message[:44]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worker", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("render", help="render one job locally")
    r.add_argument("--job", help="path to a job JSON file")
    r.add_argument("--id", help="job id (default: timestamp)")
    r.add_argument("--bbox", type=_parse_bbox, help="west,south,east,north")
    r.add_argument("--place", help="place name shown in the credits")
    r.add_argument("--title", help="title on the end card")
    r.add_argument("--date", help="YYYY-MM-DD for time-enabled imagery")
    r.add_argument("--date-end", help="end of a date range")
    r.add_argument("--basemap", choices=["vector", "satellite", "relief"])
    r.add_argument("--style", choices=["dark", "fiord", "positron", "liberty", "bright"],
                   help="basemap style for --basemap vector")
    r.add_argument("--labels", choices=["full", "balanced", "minimal", "none"],
                   help="label density")
    r.add_argument("--overlays", help="comma list: borders,population,weather,satellite")
    r.add_argument("--satellite-layer")
    r.add_argument("--weather-layer")
    r.add_argument("--narration", help="script text, or a path to a .txt file")
    r.add_argument("--tts", choices=["kokoro", "gcloud", "ai33"],
                   help="voiceover provider; ai33 is paid and needs AI33_API_KEY")
    r.add_argument("--voice")
    r.add_argument("--music", help="file in data/music, or a path")
    r.add_argument("--duration", type=float, help="seconds (narration may extend it)")
    r.add_argument("--fps", type=int)
    r.add_argument("--width", type=int)
    r.add_argument("--height", type=int)
    r.add_argument("--no-terrain", action="store_true")
    r.add_argument("--offline", action="store_true",
                   help="fail loudly instead of fetching on a cache miss")
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("serve", help="run the dashboard locally")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-worker", action="store_true",
                   help="serve the UI only; do not process jobs")
    s.set_defaults(func=cmd_serve)

    p = sub.add_parser("poll", help="process jobs from a remote dashboard")
    p.add_argument("--url", required=True, help="dashboard base URL")
    p.add_argument("--token", help="shared secret (or set SPATIALDATA_TOKEN)")
    p.add_argument("--interval", type=float, default=10.0)
    p.set_defaults(func=cmd_poll)

    d = sub.add_parser("doctor", help="check the local environment")
    d.set_defaults(func=cmd_doctor)

    c = sub.add_parser("cache", help="inspect or clear the tile cache")
    c.add_argument("--clear", action="store_true")
    c.add_argument("--upstream", help="limit --clear to one upstream")
    c.set_defaults(func=cmd_cache)

    j = sub.add_parser("jobs", help="list jobs")
    j.add_argument("--limit", type=int, default=25)
    j.set_defaults(func=cmd_jobs)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import logging
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        print(f"\nerror: {exc}\n(run with -v for a traceback)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
