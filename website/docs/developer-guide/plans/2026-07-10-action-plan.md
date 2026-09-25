# Action Plan: Session Store Lock I/O Fix

> **Date**: 2026-07-10
> **Analysis**: [2026-07-10-session-store-lock-analysis.md](2026-07-10-session-store-lock-analysis.md)
> **Spec**: [2026-07-10-session-store-lock-io-fix.md](2026-07-10-session-store-lock-io-fix.md)
> **Implementation**: [superpowers/plans/2026-07-10-session-store-lock-io-fix.md](../superpowers/plans/2026-07-10-session-store-lock-io-fix.md)

---

## Background

`get_or_create_session` in `gateway/session.py` holds `self._lock` during
six blocking I/O operations on every inbound message. A code comment at
line 1607 claims "SQLite calls are made outside the lock" but this is only
true for `_compression_tip_for_session_id`. The remaining I/O
(`_is_session_ended_in_db`, `_should_reset`, `_save` x4,
`_recover_session_from_db`) was never moved out.

This is **not** caused by our previous `to_thread` commits (`61354774f`,
`b74b7d9f1`). Those helped by moving the lock off the event loop thread.
This fix addresses the remaining lock contention between concurrent
`to_thread` workers.

## Upstream gap

No upstream issue or PR addresses this specific problem. PR #55159
(AsyncSessionDB, merged) offloaded `SessionDB` calls from the event loop
but `SessionStore` creates its own `SessionDB()` directly (session.py:949),
bypassing the facade entirely. Issue #53297 (15-30s session activation
delay) is the most likely user-visible symptom.

## PR strategy (two PRs, sequenced)

### PR 1: Previous to_thread commits (not yet submitted)

Commits `61354774f` and `b74b7d9f1` offload `get_or_create_session` and
`_session_has_compression_in_flight` calls off the event loop via
`asyncio.to_thread`. These are not on upstream/main. They fill the gap
left by PR #55159 and should be submitted first as a small, focused PR.

### PR 2: This fix (lock I/O refactor)

The four-phase refactor described in the spec. Higher risk -- touches
stale-routing self-heal (#54878), compression tip recovery, reset policy,
and session creation. Submit after PR 1 lands to avoid merge conflicts.

## Current action

Executing the implementation plan (PR 2 implementation). Steps:

1. Write failing tests (`test_session_store_lock_io.py`)
2. Add `_save_entries(snapshot)` helper
3. Add `_query_recoverable_session` DB-only method
4. Rewrite `get_or_create_session` into four-phase lock/no-lock split
5. Run all tests (new + existing)
6. Commit
