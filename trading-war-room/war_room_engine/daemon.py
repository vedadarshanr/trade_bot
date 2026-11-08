"""Unified background daemon: copy tracker + scanners + exit cycle."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import httpx

# Allow `python3 -m war_room_engine.daemon` from anywhere.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from war_room_engine.copy_tracker import CopyTracker  # noqa: E402
from war_room_engine.db import get_db  # noqa: E402
from war_room_engine.paths import STATE  # noqa: E402
from war_room_engine.scanners import PacketEmitter, PolymarketScanner, PumpfunScanner  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("war_room.daemon")

LEADERBOARD_EVERY = 1800.0
ACTIVITY_EVERY = 45.0
POLY_SCAN_EVERY = 60.0
EXIT_EVERY = 30.0


async def run() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    db = get_db()
    stop = asyncio.Event()

    def _stop(*_: object) -> None:
        log.info("shutdown signal received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda s, f: _stop())
        except Exception:
            pass

    async with httpx.AsyncClient(timeout=25.0) as http:
        emitter = PacketEmitter(db=db)
        async with CopyTracker(db=db, http=http) as tracker:
            poly = PolymarketScanner(emitter, tracker, http=http)
            pump = PumpfunScanner(emitter, tracker, http=http)
            await tracker.start_pump_ws()
            await pump.start()

            # Immediate first leaderboard attempt so SMART_WALLETS.md populates.
            try:
                await tracker.refresh_polymarket_leaderboard()
            except Exception as exc:
                log.warning("initial leaderboard failed: %s", exc)
                tracker.write_smart_wallets_md()

            last_lb = asyncio.get_event_loop().time()
            last_act = 0.0
            last_poly = 0.0
            last_exit = 0.0

            log.info("war_room daemon running (paper/read-only)")
            while not stop.is_set():
                now = asyncio.get_event_loop().time()
                try:
                    if now - last_lb >= LEADERBOARD_EVERY:
                        await tracker.refresh_polymarket_leaderboard()
                        last_lb = now
                    if now - last_act >= ACTIVITY_EVERY:
                        await tracker.poll_polymarket_activity()
                        last_act = now
                    if now - last_poly >= POLY_SCAN_EVERY:
                        n = await poly.scan_once()
                        log.info("polymarket scan emitted %d packets", n)
                        last_poly = now
                    if now - last_exit >= EXIT_EVERY:
                        # Local import to avoid circular weight at module import.
                        from war_room_engine.cli_vote import cmd_run_exit_cycle

                        class _A:
                            pass

                        cmd_run_exit_cycle(_A())
                        last_exit = now
                except Exception:
                    log.exception("daemon cycle error")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

            await pump.stop()
    db.close()
    log.info("daemon stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
