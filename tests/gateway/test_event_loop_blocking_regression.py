"""Event-loop blocking regression gate.

Simulates the #5 root cause: a slow DB operation (sleeping in the thread pool)
must not drift the interval of a concurrent heartbeat tick. This test locks the
"hot-path DB calls must offload" architectural invariant, guarding against
#6/#7 regressions.
"""
import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_slow_db_call_does_not_block_concurrent_heartbeat():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    store = MagicMock()
    store._lock = threading.Lock()
    store._loaded = True
    store._entries = {"k": MagicMock(session_id="s")}
    store._ensure_loaded_locked = lambda: None
    runner.session_store = store

    slow_db = MagicMock()
    def slow_holder(sid):
        time.sleep(0.3)  # simulate blocking under a large DB
        return None
    session_db = MagicMock()
    session_db._db = slow_db
    slow_db.get_compression_lock_holder = slow_holder
    runner._session_db = session_db

    ticks: list[float] = []

    async def heartbeat(stop: asyncio.Event):
        loop = asyncio.get_event_loop()
        next_at = loop.time()
        while not stop.is_set():
            ticks.append(loop.time())
            next_at += 0.05
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    stop = asyncio.Event()
    hb = asyncio.create_task(heartbeat(stop))
    try:
        # Concurrently trigger the slow DB check — if not offloaded, heartbeat stalls 0.3s
        await runner._session_has_compression_in_flight("k")
    finally:
        stop.set()
        await hb

    assert len(ticks) >= 3, "Too few heartbeat ticks, test unreliable"
    intervals = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
    intervals.sort()
    p99 = intervals[max(0, int(len(intervals) * 0.99) - 1)]
    # DB blocks 0.3s; when offloaded correctly the heartbeat p99 should stay well under 0.2s
    assert p99 < 0.2, (
        f"Heartbeat interval p99={p99:.3f}s, event loop blocked by DB — regression (sync hot-path DB call)"
    )
