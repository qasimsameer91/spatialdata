"""Entry point so the worker runs as `python -m worker`."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
