from __future__ import annotations

import asyncio
import signal

from ticketsbot.config import Settings
from ticketsbot.db import Database
from ticketsbot.workers import WorkerManager


async def main() -> None:
    settings = Settings(workers_enabled=True)
    db = Database(settings)
    await db.initialize()
    manager = WorkerManager(db, settings)
    manager.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try: loop.add_signal_handler(getattr(signal, name), stop.set)
            except NotImplementedError: pass
    try:
        await stop.wait()
    finally:
        await manager.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
