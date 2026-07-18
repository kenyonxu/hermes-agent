# Gateway Freeze Fix Summary

> **Date**: 2026-07-18
> **Root cause**: Two independent bugs — sqlite3 connection-level race + WAL checkpoint starvation
> **Duration of investigation**: 7 days, 8 freeze events, 5 py-spy dumps
> **Status**: both root causes fixed, under observation

---

## Timeline

| Date | Event | Action |
|------|-------|--------|
| 07-10 | PR #61842 + #61847 (session store lock I/O) merged upstream via salvage #61905 | Synced fork |
| 07-12 | First freeze: Feishu `extra_ua_tags` TypeError + gateway event loop stalled | Fixed lark-oapi version, restarted |
| 07-12 | Second freeze: Discord message unresponsive, all threads idle | py-spy dump: SQLite write-lock contention on 1.6 GB DB |
| 07-14 | DB prune (33k→2k sessions, 1.6 GB→287 MB), cron shared SessionDB, timeout 1.0→30.0 | Three mitigations applied |
| 07-14 | Freeze persists: channel directory piling up SessionDB() instances | channel directory cache + overlap guard |
| 07-14 | Freeze persists: `_init_schema` DDL on every connection | `_init_schema` per-db_path cache |
| 07-16 | Freeze persists after 15h: same py-spy pattern | Discovered 3 methods missing `self._lock` |
| 07-17 | Lock fix deployed, freeze persists after 31h: same pattern but all methods locked | WAL checkpoint starvation hypothesis |
| 07-18 | WAL TRUNCATE checkpoint added to housekeeping (101 MB WAL → 0) | Both root causes addressed |

---

## Root cause 1: sqlite3 connection race (commit a65c15222)

### Bug

`list_pending_handoffs`, `get_handoff_state`, and `get_compression_lock_holder`
accessed `self._conn.execute()` without acquiring `self._lock`. `SessionDB`
uses `check_same_thread=False` + `threading.Lock` to serialize all connection
access. Methods that skip the lock race with locked methods on the same
connection object.

Python's sqlite3 C extension has no internal locking for concurrent use of
the same connection. When an unlocked method ran simultaneously with a locked
method, both threads corrupted the connection's internal state, causing a
permanent C-level block that `timeout=30.0` cannot break.

### Why it was hard to find

The py-spy dumps showed threads stuck in different methods
(`list_pending_handoffs`, `get_compression_tip`, `get_compression_failure_cooldown`).
Initially diagnosed as WAL lock contention — but all the queries were simple
SELECTs that shouldn't block in WAL mode. The real issue was the connection
object itself, not the SQL.

### Fix

```python
# Before (no lock):
cur = self._conn.execute("SELECT ...")

# After (with lock):
with self._lock:
    cur = self._conn.execute("SELECT ...")
```

Found via AST scan: searched for all SessionDB methods that reference
`self._conn` without a `with self._lock:` block.

---

## Root cause 2: WAL checkpoint starvation (commit 381d24529)

### Bug

WAL mode requires periodic checkpointing to merge WAL frames into the main
DB file. Without checkpointing, the WAL file grows unbounded. Every read
must scan all WAL frames to find the latest version of each page —
O(WAL_size) per query.

Observed: WAL grew to 101 MB over 15+ hours of gateway uptime. With 4+
concurrent SessionDB connections all scanning a 101 MB WAL, I/O contention
slowed all queries to a crawl, effectively freezing the gateway.

### Why WAL wasn't being checkpointed

1. `PASSIVE` checkpoint fires every 50 writes per SessionDB instance. But
   each of the 4 SessionDB instances tracks its own write count. Writes
   spread across instances → none reaches the threshold quickly.

2. `TRUNCATE` checkpoint was only called in `SessionDB.close()` (shutdown)
   and `maybe_auto_prune_and_vacuum` (startup). Never during normal runtime.

3. `wal_checkpoint(TRUNCATE)` requires no concurrent readers. With the
   handoff watcher polling every 2s + cron jobs + channel directory, there's
   almost always a reader — so even if called, it returns busy.

### Fix

Added `wal_checkpoint(TRUNCATE)` to gateway housekeeping every 30 minutes.
Uses the gateway's primary SessionDB instance. The TRUNCATE call may fail
(busy) if a reader is active — that's fine, it retries on the next tick.
The key is that it runs frequently enough to prevent WAL growth beyond
~30 minutes of writes (~5-10 MB).

---

## All fixes applied (in order)

| # | Commit | Fix | Root cause addressed |
|---|--------|-----|---------------------|
| 1 | `49a36a407` | cron shared SessionDB singleton | Per-tick DDL overhead |
| 2 | `ea58523e4` | SQLite timeout 1.0 → 30.0 | Concurrent timeout cascade |
| 3 | `00273ae39` | channel directory cached read-only SessionDB + overlap guard | Repeated connection creation |
| 4 | `00aa84b63` | `_init_schema` per-db_path cache | DDL write-lock contention |
| 5 | `a65c15222` | Add `self._lock` to 3 SessionDB read methods | **sqlite3 connection race** |
| 6 | `381d24529` | WAL TRUNCATE checkpoint in housekeeping | **WAL checkpoint starvation** |

### Additional mitigations (not root cause fixes)

- DB prune: 33,340 → 1,956 sessions, 1.6 GB → 287 MB
- kanban disabled (config.yaml `dispatch_in_gateway: false`)
- lark-oapi 1.5.5 → 1.6.8 (Feishu `extra_ua_tags` compatibility)
- freeze watchdog script (py-spy dump on gateway.log staleness)
- NOPASSWD sudo for py-spy (diagnostic tooling)

---

## Lessons

1. **py-spy is essential** — without thread-level Python stack traces, this
   would have been impossible to diagnose. The freeze left no trace in logs;
   only py-spy could show what each thread was doing.

2. **AST scanning beats manual code review** — the missing `self._lock` was
   found by a 10-line AST script that checked every SessionDB method for
   `self._conn` access without a lock block. Manual review missed it across
   5+ freeze events.

3. **"It's not the lock, it's the connection"** — the py-spy dumps kept
   showing SQLite-level blocking. The instinct was to look at SQLite WAL
   locks, PRAGMA settings, and timeout values. The real issue was Python's
   sqlite3 module not being thread-safe on a shared connection — a layer
   below SQLite itself.

4. **WAL needs active management** — SQLite WAL mode is not fire-and-forget.
   In a multi-connection scenario, the default `wal_autocheckpoint=1000`
   pages (~4 MB) is too slow when writes are distributed across connections.
   Periodic explicit `TRUNCATE` checkpoint is necessary.

5. **Each fix shifted the bottleneck** — fixing the connection race exposed
   the WAL growth problem, which was previously masked by the connection
   race causing freezes before the WAL could grow. This is typical of
   debugging cascading concurrency bugs: fix one layer, the next one surfaces.
