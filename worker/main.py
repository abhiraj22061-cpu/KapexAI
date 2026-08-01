'''import asyncio
import signal

from db_service import connect_db, disconnect_db, db


async def main():
    await connect_db()

    stop = asyncio.Event()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    

    while not stop.is_set():
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break

    await disconnect_db()
    print("Worker shut down gracefully")
'''
import asyncio

from worker.agent import run_cli

if __name__ == "__main__":
    #asyncio.run(main())
    asyncio.run(run_cli())
