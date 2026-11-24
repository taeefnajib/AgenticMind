"""``agenticmind-worker``: a process that only executes runs (ADR-0039).

Point it at the same database (and data directory) as the API::

    AGENTICMIND_DATABASE_URL=postgresql://... AGENTICMIND_DATA_DIR=/shared agenticmind-worker

Run as many as you like; each claims queued runs under a lease and resumes runs from workers that died.
Give the API ``AGENTICMIND_EMBEDDED_WORKER=0`` to leave execution entirely to workers. Stop with Ctrl-C:
running runs get a short grace period, then are handed back to be resumed by another worker.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .main import create_app
from .settings import Settings

log = logging.getLogger("agenticmind.worker")


async def run_worker(settings: Settings | None = None, stop: asyncio.Event | None = None) -> None:
    settings = settings or Settings()
    app = create_app(settings)
    services = app.state.services
    runs = services.runs
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: KeyboardInterrupt ends asyncio.run instead
            pass
    await runs.start_worker()
    log.info("worker %s started (max %d concurrent runs)", runs.worker_id, runs.max_concurrency)
    try:
        await stop.wait()
    finally:
        await runs.shutdown()
        if services.observability is not None:
            await services.observability.drain()
        log.info("worker %s stopped", runs.worker_id)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
