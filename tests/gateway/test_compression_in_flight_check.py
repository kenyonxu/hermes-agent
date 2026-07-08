"""#5 regression: _session_has_compression_in_flight must offload both blocking sources to thread pool."""
import inspect
import threading
from unittest.mock import MagicMock

import pytest


def _make_runner(holder_value=None,
                 record_db_thread=False, db_thread_sink=None,
                 record_store_thread=False, store_thread_sink=None):
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)

    store = MagicMock()
    store._lock = threading.Lock()
    store._loaded = True
    store._entries = {"k": MagicMock(session_id="sess-123")}

    if record_store_thread and store_thread_sink is not None:
        def _load_and_record():
            store_thread_sink["thread"] = threading.get_ident()
        store._ensure_loaded_locked = _load_and_record
    else:
        store._ensure_loaded_locked = lambda: None

    runner.session_store = store

    raw_db = MagicMock()
    if record_db_thread and db_thread_sink is not None:
        def _holder(sid):
            db_thread_sink["thread"] = threading.get_ident()
            return holder_value
        raw_db.get_compression_lock_holder = _holder
    else:
        raw_db.get_compression_lock_holder = MagicMock(return_value=holder_value)

    session_db = MagicMock()
    session_db._db = raw_db
    runner._session_db = session_db
    return runner


def test_method_is_coroutine():
    from gateway.run import GatewayRunner
    assert inspect.iscoroutinefunction(
        GatewayRunner._session_has_compression_in_flight
    ), "#5: method must be async, blocking calls offloaded"


@pytest.mark.asyncio
async def test_returns_true_when_lock_held():
    runner = _make_runner(holder_value="agent-1")
    assert await runner._session_has_compression_in_flight("k") is True


@pytest.mark.asyncio
async def test_returns_false_when_no_lock():
    runner = _make_runner(holder_value=None)
    assert await runner._session_has_compression_in_flight("k") is False


@pytest.mark.asyncio
async def test_returns_false_when_no_session_store():
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = None
    runner._session_db = MagicMock()
    assert await runner._session_has_compression_in_flight("k") is False


@pytest.mark.asyncio
async def test_returns_false_when_no_session_db():
    """session_store is valid but _session_db is None -- must return False."""
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner.session_store._lock = threading.Lock()
    runner.session_store._loaded = True
    runner.session_store._entries = {"k": MagicMock(session_id="sess-123")}
    runner.session_store._ensure_loaded_locked = lambda: None
    runner._session_db = None
    assert await runner._session_has_compression_in_flight("k") is False


@pytest.mark.asyncio
async def test_returns_false_when_db_throws():
    """get_compression_lock_holder raises -- method must return False gracefully."""
    runner = _make_runner(holder_value="agent-1")
    runner._session_db._db.get_compression_lock_holder.side_effect = RuntimeError("boom")
    assert await runner._session_has_compression_in_flight("k") is False


@pytest.mark.asyncio
async def test_db_call_runs_off_event_loop():
    """Both blocking sources MUST execute in non-event-loop threads."""
    db_sink = {}
    store_sink = {}
    runner = _make_runner(
        holder_value="agent-1",
        record_db_thread=True, db_thread_sink=db_sink,
        record_store_thread=True, store_thread_sink=store_sink,
    )
    loop_thread = threading.get_ident()
    await runner._session_has_compression_in_flight("k")

    assert "thread" in store_sink, (
        "_ensure_loaded_locked was not called -- store lock path not exercised"
    )
    assert store_sink["thread"] != loop_thread, (
        "_ensure_loaded_locked still on event loop thread -- "
        "store lock + JSON path NOT offloaded (#5)"
    )

    assert "thread" in db_sink, (
        "underlying db.get_compression_lock_holder was not called"
    )
    assert db_sink["thread"] != loop_thread, (
        "DB call still on event loop thread -- #5 NOT fixed (to_thread not applied)"
    )
