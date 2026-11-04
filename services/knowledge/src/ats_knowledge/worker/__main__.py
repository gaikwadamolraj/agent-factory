"""Entry point of the worker process: `python -m ats_knowledge.worker`.

Sets up telemetry, turns SIGTERM/SIGINT into a graceful stop, and runs the loop (worker/loop.py).
"""
import asyncio
import atexit
import signal

from ats_common.telemetry import setup_observability

from ats_knowledge.config import get_settings
from ats_knowledge.worker.loop import run_worker


def main() -> None:
    settings = get_settings()
    observability = setup_observability(settings)
    atexit.register(observability.shutdown)

    async def runner() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:   # Windows dev: Ctrl+C raises KeyboardInterrupt instead
                pass
        await run_worker(settings, stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
