# Session Store Lock Analysis: get_or_create_session per-message I/O

> **Date**: 2026-07-10 | **Method**: source + git history review
> **Supersedes**: `2026-07-10-deadlock-session-store-lock.md` (premise was wrong)
> **Status**: analysis complete -- fixes identified, not yet implemented

---

## TL;DR

The previous document diagnosed a thread-pool-exhaustion deadlock caused by
`_lookup_session_id_under_store_lock` calling `_ensure_loaded_locked()`
under lock. That diagnosis is **incorrect**: `_ensure_loaded_locked()` is
a no-op after startup (`_loaded` is set to `True` once and never reset).

The **real** per-message lock-holding I/O lives in `get_or_create_session`
in gateway/session.py, which calls `_is_session_ended_in_db()` (SQLite
SELECT) and `_save()` (SQLite write + JSON write + `os.fsync`) **inside**
the `self._lock` block on every inbound message. These are the calls that
can hold the lock for tens to hundreds of milliseconds.

**Was this caused by our changes?** No. The lock-holding I/O in
`get_or_create_session` predates all of our recent `to_thread` work. Our
`to_thread` offload (`61354774f`, `b74b7d9f1`) made things *better*, not
worse -- it moved the event-loop thread off the store lock on the
compression-in-flight path. The remaining problem is pre-existing code
that was never addressed.

---

## Why the original deadlock diagnosis is wrong

### 1. `_ensure_loaded_locked()` does I/O exactly once

The `_loaded` flag lifecycle (verified across the entire file):

| Location | Line | Value |
|----------|------|-------|
| `__init__` | session.py:926 | `self._loaded = False` |
| `_ensure_loaded_locked` | session.py:1042 | `self._loaded = True` |

`_loaded` is **never** reset to `False` after the first load. The guard at
session.py:981:

```python
if self._loaded:
    return
```

makes every subsequent call a sub-microsecond no-op. After gateway startup
(first inbound message or explicit `store._ensure_loaded()` at init), the
disk read and SQLite routing-table load never happen again.

### 2. The proposed fix changes nothing in production

The double-checked-locking fix in the original document:

```python
if not session_store._loaded:          # False after startup
    with session_store._lock:
        if not session_store._loaded:
            session_store._ensure_loaded_locked()
with session_store._lock:              # same as current code
    entry = session_store._entries.get(session_key)
```

After the first few seconds of gateway uptime, `session_store._loaded` is
always `True`, so the outer `if` short-circuits and execution falls
through to Step 2 -- which is identical to the current code. The fix is a
no-op in production.

### 3. Thread-pool exhaustion cannot deadlock the event loop

Even in the hypothetical worst case (all threads contending on one lock):

- `asyncio.to_thread` uses the **default loop executor** (~28 threads on
  this machine: `min(32, cpu_count + 4)`), not the gateway's own 10-thread
  `_get_executor` pool.
- Thread-pool saturation **delays** `to_thread` callbacks; it does not
  freeze the event loop. The loop continues running other coroutines,
  timers, and I/O.
- The event loop thread itself never acquires `session_store._lock` on the
  compression-in-flight path -- that lock is taken only inside worker
  threads.
- The strace `futex(FUTEX_WAIT_PRIVATE, 2, NULL)` pattern across 8 threads
  is characteristic of GIL contention, not a deadlock. Threads that finish
  their work wait for the GIL to return to Python -- that is normal idle
  behavior.

---

## The real problem: `get_or_create_session` does I/O under lock

Every inbound message goes through `get_or_create_session`. Its second
`with self._lock:` block (session.py:1614) holds the store lock while
performing three blocking operations:

```
get_or_create_session (per inbound message)
  with self._lock:
       _ensure_loaded_locked()           # no-op after startup
       _is_session_ended_in_db()         # SQLite SELECT -- every message
       _compression_tip_for_session_id() # moved out of lock (pre-existing fix)
       _save()                           # SQLite write + JSON write + os.fsync
```

### Operation 1: `_is_session_ended_in_db()` -- SQLite SELECT under lock

Added in commit `3a83b6bc5` (Jun 30, 2026) to self-heal stale routing
keys at message time (#54878). This call performs
`db.get_session(session_id)` -- a synchronous SQLite SELECT -- while
holding `self._lock`. On a large `state.db` (the commit `61354774f` that
motivated the `to_thread` offload mentions a ~1.4 GB database), this can
take seconds.

### Operation 2: `_save()` -- full routing index rewrite + fsync under lock

`_save()` (session.py:1133) performs:

1. `replace_gateway_routing_entries()` -- replaces the **entire** routing
   index in one SQLite transaction (grows with session count)
2. If `write_sessions_json` is True (default): `json.dump()` + `f.flush()`
   + `os.fsync(f.fileno())` + atomic replace

`os.fsync` is synchronous disk I/O -- 10-100 ms on SSD, potentially more
on networked storage. This runs on every message that updates an entry
(which is every message, since `updated_at = now` is set unconditionally).

### The irony

Lines session.py:1612-1613 contain this comment, written when
`_compression_tip_for_session_id` was correctly moved outside the lock:

```python
# SQLite calls are made outside the lock to avoid holding it during I/O.
# All _entries / _loaded mutations are protected by self._lock.
```

But `_is_session_ended_in_db()` and `_save()` -- the two heaviest I/O
operations -- were left inside the lock, violating the very principle the
comment states.

---

## Is this caused by our recent changes?

**No.** The lock-holding I/O is pre-existing. Our changes made things
better:

| Commit | Date | What it did | Effect on this problem |
|--------|------|-------------|------------------------|
| `61354774f` | Jul 5 | Offloaded `get_or_create_session` and 24 other hot-path calls to `asyncio.to_thread` | **Helped** -- moved the lock off the event loop thread |
| `b74b7d9f1` | Jul 8 | Async-ified `_session_has_compression_in_flight`, offloaded to `to_thread` | **Helped** -- moved compression lock check off event loop |
| `3a83b6bc5` | Jun 30 | Added `_is_session_ended_in_db()` inside the lock in `get_or_create_session` | **Added one SQLite SELECT under lock** -- but this is the stale-routing self-heal (#54878), a correctness fix for silent message drops |
| `38b1c7dce` | May 7 | Inlined `list_resume_pending()` into `_schedule_resume_pending_sessions()`, which accesses `session_store._lock` directly on the event loop thread | **Introduced event-loop-thread lock access** -- but only at startup / platform reconnect, not per-message |

The timeline: the `_save()` under lock existed since the original
`SessionStore` implementation (commit `619c72e56`). The
`_is_session_ended_in_db()` under lock was added Jun 30. Our `to_thread`
work (Jul 5-8) reduced the symptom by moving the whole
`get_or_create_session` call off the event loop, but the lock contention
between concurrent worker threads remains.

**Bottom line**: our changes did not introduce this problem. They reduced
its severity by moving the lock off the event loop. The remaining issue is
that multiple `to_thread` workers calling `get_or_create_session` still
contend on `self._lock` during SQLite I/O + `os.fsync`.

---

## Related: event-loop-thread lock access in `_schedule_resume_pending_sessions`

`_schedule_resume_pending_sessions` (run.py:6370, lock at run.py:6395)
acquires `session_store._lock` and calls `_ensure_loaded_locked()` directly
on the **event loop thread** -- not via `to_thread`. It is called from:

- Gateway startup (run.py:7147)
- Platform reconnect handler (run.py:7768)

After startup, `_ensure_loaded_locked()` is a no-op and the lock is held
only for an in-memory list comprehension -- microseconds. This is low-risk
but inconsistent with the `to_thread` pattern used everywhere else, and a
future change that adds I/O inside this snapshot block would reintroduce
event-loop blocking.

This was introduced by commit `38b1c7dce` (May 7, 2026), which inlined
the old `list_resume_pending()` public method (which had its own internal
locking) directly into `run.py` with bare `session_store._lock` access.

---

## Recommended fixes

### Fix 1: Move `_is_session_ended_in_db()` outside the lock

Pattern already used by `_compression_tip_for_session_id`: read
`session_id` under lock, release lock, do SQLite check, re-acquire lock if
the entry needs modification.

```python
# In get_or_create_session, replace the in-lock call:
#   if self._is_session_ended_in_db(entry.session_id):  # CURRENT: under lock
# with: read session_id under lock, check outside lock
```

Risk: between the lock release and the stale check, another thread could
modify the entry. Mitigate with a re-check after re-acquiring the lock
(the entry's session_id is immutable for the lifetime of a SessionEntry).

### Fix 2: Defer `_save()` to after lock release

```python
# Current: _save() called inside the lock block
# Proposed: set a _dirty flag, release lock, then _save()
```

The routing index is a dict; snapshot the entries under lock and write
them outside. This is safe because `_save()` writes a consistent snapshot
-- another concurrent message that also sets `_dirty` will trigger its own
save on the next release, or a coalesced save.

### Fix 3: Wrap `_schedule_resume_pending_sessions` snapshot in `to_thread`

Low priority (startup/reconnect only, currently a no-op load), but makes
the codebase consistent with the `to_thread` offload pattern and prevents
future regressions if I/O is added to the snapshot block.

---

## Appendix: comparison of all blocking fixes

| # | Commit | Problem | Fix | Still relevant? |
|---|--------|---------|-----|-----------------|
| 1 | pre-`61354774f` | handoff watcher SQLite scan blocks event loop | AsyncSessionDB + index | Resolved |
| 2 | `61354774f` | `get_or_create_session` blocks event loop | `asyncio.to_thread` | Partially -- moved off event loop but lock contention remains |
| 3 | `b74b7d9f1` | compression-in-flight check blocks event loop | `async def` + `to_thread` | Resolved |
| 4 | -- | channel directory `SessionDB()` blocks event loop | `read_only` + `to_thread` | Resolved |
| 5 | -- | compression lock SQLite blocks event loop | `async def` + `to_thread` | Resolved |
| **6** | **this doc** | **`get_or_create_session` does SQLite SELECT + full routing index rewrite + `os.fsync` under `self._lock`, per message** | **Move I/O outside lock (Fix 1 + 2)** | **Open** |
