"""Deadline semantics for ``_send_with_retry`` (gateway/platforms/base.py).

Contract: every send attempt is bounded by ``_send_deadline_seconds()``. A
hung platform send (request out, response never arrives — the black-holed
final-send incidents of 2026-08-26/28) must return a non-retryable timeout
failure within the deadline instead of leaking the await and freezing the
session until process restart. Deadline timeouts are NOT retried (delivery
state unknown — the request may have been delivered) and must NOT trigger
the plain-text fallback; ordinary transient errors keep their retry path.
"""

import asyncio
import time

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _Adapter(BasePlatformAdapter):  # type: ignore[misc]
    """Minimal concrete adapter whose send behavior is injected per test."""

    def __init__(self, send_fn):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self._send_fn = send_fn
        self.calls = 0
        self.contents = []

    async def connect(self, *, is_reconnect: bool = False):  # pragma: no cover
        return True

    async def disconnect(self):  # pragma: no cover - unused
        return None

    async def get_chat_info(self, chat_id):  # pragma: no cover - unused
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls += 1
        self.contents.append(content)
        return await self._send_fn(chat_id, content, reply_to, metadata)


async def _hanging_send(chat_id, content, reply_to, metadata):
    """Simulate a black-holed request: never returns, never raises."""
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_hung_send_fails_within_deadline_and_is_not_retried():
    adapter = _Adapter(_hanging_send)
    adapter._send_deadline_override = 0.2

    started = time.monotonic()
    result = await adapter._send_with_retry("C1", "final answer")
    elapsed = time.monotonic() - started

    assert not result.success
    assert result.retryable is False
    # Exactly one attempt: timeout errors must not be retried (the request
    # may already be delivered server-side — a retry could duplicate it).
    assert adapter.calls == 1
    assert elapsed < 5.0
    # And no plain-text fallback attempt either.
    assert all("plain text" not in c for c in adapter.contents)


@pytest.mark.asyncio
async def test_deadline_failure_recognized_as_timeout_error():
    # The error string must land in the existing timeout channel: matched by
    # _is_timeout_error ("timed out"), NOT by the retryable-error patterns.
    adapter = _Adapter(_hanging_send)
    adapter._send_deadline_override = 0.1
    result = await adapter._send_with_retry("C1", "final answer")

    assert BasePlatformAdapter._is_timeout_error(result.error) is True
    assert BasePlatformAdapter._is_retryable_error(result.error) is False


@pytest.mark.asyncio
async def test_fast_successful_send_unaffected_by_deadline():
    async def ok_send(chat_id, content, reply_to, metadata):
        return SendResult(success=True, message_id="m1")

    adapter = _Adapter(ok_send)
    adapter._send_deadline_override = 5.0
    result = await adapter._send_with_retry("C1", "hello")
    assert result.success
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_transient_errors_still_retry_within_deadline():
    state = {"n": 0}

    async def flaky_send(chat_id, content, reply_to, metadata):
        state["n"] += 1
        if state["n"] == 1:
            return SendResult(
                success=False, error="ConnectionError: reset by peer", retryable=True
            )
        return SendResult(success=True, message_id="m2")

    adapter = _Adapter(flaky_send)
    adapter._send_deadline_override = 5.0
    result = await adapter._send_with_retry("C1", "hello", base_delay=0.01)
    assert result.success
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_deadline_configurable_via_platform_extra():
    async def slow_then_ok(chat_id, content, reply_to, metadata):
        await asyncio.sleep(0.05)
        return SendResult(success=True, message_id="m3")

    adapter = _Adapter(slow_then_ok)
    # No override attr set: the platform config section must be honored.
    extra = adapter.config.__dict__.setdefault("extra", {})
    extra["send_timeout_seconds"] = 0.03

    result = await adapter._send_with_retry("C1", "hello")
    assert not result.success
    assert "timed out" in (result.error or "").lower()
