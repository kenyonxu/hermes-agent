# Gateway Freeze Root Cause: _init_schema DDL Write-Lock Contention

> **Date**: 2026-07-16
> **Status**: root cause identified, fix not yet implemented
> **Evidence**: 3 py-spy dumps (2026-07-14 10:24, 2026-07-14 13:00, 2026-07-16 09:08)

---

## Symptom

Gateway freezes every few hours to days. Discord messages stop getting
responses. Process stays alive (S state) but all threads are idle — no CPU
activity, no log output. Gateway log stops updating for hours. Requires
`systemctl --user restart hermes-gateway.service` to recover.

## Root cause

`SessionDB.__init__` calls `_connect_and_init` (hermes_state.py:937), which
calls `_init_schema` (hermes_state.py:1346). Every `SessionDB()` constructor
runs the full DDL reconciliation:

1. `cursor.executescript(SCHEMA_SQL)` — 15+ `CREATE TABLE IF NOT EXISTS` +
   `CREATE INDEX IF NOT EXISTS` statements. `executescript` implicitly
   commits and starts a new transaction, acquiring a SQLite **reserved lock**
   that blocks all concurrent readers in WAL mode.

2. `_reconcile_columns(cursor)` — runs `PRAGMA table_info` for every table
   and may execute `ALTER TABLE ADD COLUMN` if columns are missing.

3. `cursor.execute("UPDATE messages SET active = 1 WHERE active IS NULL")` —
   a write operation that holds the write lock until committed.

Multiple gateway components create their own `SessionDB()` instances:

| Component | When | Connection |
|-----------|------|------------|
| `gateway/run.py:3043` | startup (shared via AsyncSessionDB) | 1 instance |
| `gateway/session.py:949` (SessionStore) | startup | 1 instance |
| `cron/scheduler.py` (`_get_shared_session_db`) | first cron tick | 1 instance (our fix) |
| `gateway/channel_directory.py` (`_get_cached_session_db`) | first channel dir build | 1 instance (our fix) |
| `gateway/mirror.py` | per-call | new each time |
| `hermes_cli/console_engine.py` | per-call | new each time |
| `hermes_cli/goals.py` | per-call | new each time |
| `hermes_cli/status.py` | per-call | new each time |

When two or more `SessionDB()` instances run `_init_schema` concurrently
(e.g. a cron tick + channel directory refresh + handoff watcher), their DDL
`executescript` calls compete for the SQLite reserved lock. The `timeout=30.0`
change means they now WAIT 30 seconds instead of failing at 1 second — but
30 seconds of blocking is still enough to freeze the gateway.

## Evidence timeline (py-spy dumps)

### 2026-07-14 10:24 (pre-fix, 1.6 GB DB)

```
cron-parallel_2: _connect_and_init (hermes_state.py:937) ← SessionDB() constructor
cron-parallel_3: _connect_and_init (hermes_state.py:937) ← same
asyncio_1:       _execute_write (hermes_state.py:1161) ← replace_gateway_routing_entries
asyncio_2:       _sqlite_connect (kanban_db.py:1318) ← kanban collector
```

### 2026-07-14 13:00 (post cron-shared-sessiondb + timeout=30)

```
asyncio_1:  list_pending_handoffs (hermes_state.py:6406)      ← handoff watcher
cron-parallel_0: get_compression_failure_cooldown (hermes_state.py:2137)
cron-parallel_1: get_compression_failure_cooldown (hermes_state.py:2133)
asyncio_2:  __init__ (hermes_state.py:924) ← channel_directory SessionDB(read_only=True)
asyncio_3:  get_compression_tip (hermes_state.py:2939)
```

### 2026-07-16 09:08 (post channel-directory-cache + overlap guard)

```
asyncio_1:      list_pending_handoffs (hermes_state.py:6406)
cron-parallel_0: get_compression_failure_cooldown (hermes_state.py:2137)
cron-parallel_1: get_compression_failure_cooldown (hermes_state.py:2133)
asyncio_2:      list_gateway_sessions (hermes_state.py:1848) ← channel directory (cached instance)
```

## Mitigations applied so far

| Fix | Commit | Effect |
|-----|--------|--------|
| DB prune + VACUUM (1.6 GB → 287 MB) | manual | Faster individual queries |
| cron shared SessionDB | `49a36a407` | Eliminates per-tick `SessionDB()` in cron |
| SQLite timeout 1.0 → 30.0 | `ea58523e4` | Prevents cascading timeout failures |
| channel directory read_only + cache | `00273ae39` | Reduces `SessionDB()` creation in channel dir |
| kanban disabled (config) | config.yaml | Eliminates kanban DB contention |

## TRUE root cause (found 2026-07-17)

The actual root cause was found after 5 freeze events and 4 rounds of
mitigations that did not eliminate the problem.

**Three `SessionDB` read methods accessed `self._conn` without acquiring
`self._lock`:**

- `list_pending_handoffs` — called every 2s by the handoff watcher
- `get_handoff_state` — called during handoff processing
- `get_compression_lock_holder` — called during compression checks

`SessionDB` uses `check_same_thread=False` + `threading.Lock` to serialize
all access to `self._conn`. The lock ensures only one thread uses the
`sqlite3.Connection` at a time. Methods that skip the lock race with locked
methods on the same connection object.

Python's sqlite3 C extension has **no internal locking** for concurrent
use of the same connection. When an unlocked method ran simultaneously
with a locked method (e.g. `list_pending_handoffs` while
`get_compression_failure_cooldown` held the lock), both threads entered
`sqlite3_connection.execute()` in C at the same time, corrupting the
connection's internal state and causing a permanent C-level block.

This block is **not affected by `timeout=30.0`** — the timeout operates at
SQLite's busy-handler level (waiting for another process's WAL lock), not
at the sqlite3 Python module level (which is deadlocked internally).

**Fix (commit a65c15222):** add `with self._lock:` to all three methods.

**All previous mitigations were addressing symptoms, not the root cause:**

| Fix | What it addressed | Why it wasn't enough |
|-----|-------------------|----------------------|
| DB prune + VACUUM | Reduced query time | Connection race still possible |
| cron shared SessionDB | Fewer SessionDB instances | Race on gateway's shared instance |
| timeout 1.0 → 30.0 | SQLite busy timeout | Doesn't help sqlite3 module deadlock |
| channel directory cache | Fewer SessionDB instances | Race on gateway's shared instance |
| `_init_schema` cache | Eliminated DDL write lock | Race was on SELECT queries, not DDL |

## Watchdog

`scripts/gateway_freeze_watchdog.sh` runs via crontab every 2 minutes.
On freeze detection (gateway.log stale > 120s), captures py-spy dump to
`~/.hermes/profiles/zhihui/logs/freeze_dumps/`.

---

## Future directions (discussion, not yet planned)

If the current mitigations prove insufficient, a more fundamental
refactor of the SQLite data layer is the next step. Four directions
were discussed:

### 1. Single-writer thread + lock-free reads (recommended)

A dedicated background writer thread processes all writes through a
`queue.Queue`. Reads use read-only WAL connections (never block the
writer). This eliminates write-lock contention entirely — there is
only one writer. `_init_schema` runs once at writer startup. `timeout`
becomes irrelevant.

Pros: root-cause fix, standard SQLite-in-Python pattern (Django
uses it), minimal API change (replace `_execute_write` with
queue submit + Event wait).

Cons: write latency increases by queue depth. Acceptable for Hermes's
write profile (session metadata, routing index — not hot path).
Priority queue or direct-write escape hatch needed for atomic CAS
(`claim_handoff`).

### 2. Connection pool per process (least architectural change)

All `SessionDB()` construction goes through a shared pool (4 write +
8 read connections). `_init_schema` runs once at pool init, not per
connection.

Pros: nearly transparent API (`SessionDB()` → `db_pool.acquire()`).
Cons: write-lock contention still exists between pool's write
connections, just without DDL overhead.

### 3. Migrate away from SQLite (likely wrong)

PostgreSQL or DuckDB. Probably incorrect — SQLite is the right
choice for a personal, single-machine, embedded agent. The problem is
misuse of SQLite's concurrency model, not SQLite itself. Migration
cost (FTS5, WAL, PRAGMA tuning) is enormous.

### 4. Tiered storage (most elegant, long-term)

Split hot routing data (session_key → session_id, compression locks)
from cold history (message transcripts). Hot layer lives in memory
dict (already exists as `SessionStore._entries`), writes flush
async to SQLite. Cold layer is append-only, written once per agent
turn start/end.

Pros: routing operations (`get_or_create_session`, `switch_session`)
never touch SQLite at all — zero lock contention.
Cons: significant refactor, async flush correctness, crash recovery
for in-memory state.

### Recommendation

Direction 1 (single-writer thread) has the best cost/benefit ratio.
Direction 4 is the ideal end state but requires more work. Both can
coexist — start with 1, evolve toward 4 over time.

**Decision: defer. Run current mitigations for observation first.**

---

## Update 2026-07-18: freeze persists after lock fix — WAL growth hypothesis

The `self._lock` fix (commit a65c15222) did NOT eliminate the freeze.
py-spy dump from 2026-07-18 shows the SAME pattern — four threads stuck
in SQLite queries:

```
asyncio_1:  get_compression_tip (hermes_state.py:2975)          ← SessionStore._db
asyncio_2:  list_pending_handoffs (hermes_state.py:6444)        ← gateway AsyncSessionDB
cron-parallel_1/2: get_compression_failure_cooldown (hermes_state.py:2168/2172) ← cron shared SessionDB
asyncio_3:  list_gateway_sessions (hermes_state.py:1883)        ← channel directory cached SessionDB
```

All four methods now have `with self._lock:`, but they're on DIFFERENT
`SessionDB` instances. Each instance has its own `threading.Lock` and its
own `sqlite3.Connection` — so the Python-level lock doesn't help.

The freeze is now at the SQLite level: multiple connections to the same
WAL database, with a 101 MB WAL file that hasn't been checkpointed.

### New hypothesis: WAL checkpoint starvation

WAL mode allows concurrent readers + one writer. But when the WAL file
grows large (101 MB observed), every read must scan WAL frames to find
the latest version of each page — making reads O(WAL_size). With 4+
concurrent connections all scanning a 101 MB WAL, the I/O contention
slows everything to a crawl.

WAL checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`) would fix this by
merging WAL frames into the main DB file. But:

1. `TRUNCATE` checkpoint requires exclusive access — it fails if any
   connection is mid-read (returns busy code 1).
2. `PASSIVE` checkpoint runs every 50 writes per SessionDB instance, but
   each instance tracks its own write count independently. If writes are
   spread across 4 instances, none reaches the threshold quickly.
3. Gateway housekeeping does NOT call `wal_checkpoint(TRUNCATE)`. Only
   `SessionDB.close()` (on shutdown) and `maybe_auto_prune_and_vacuum`
   (startup only) do TRUNCATE.

### Proposed fix

Add a periodic `wal_checkpoint(TRUNCATE)` to gateway housekeeping (every
hour), and reduce `_CHECKPOINT_EVERY_N_WRITES` from 50 to 20 so PASSIVE
checkpoint fires more often. Also consider `PRAGMA wal_autocheckpoint`
setting (currently defaults to 1000 pages = ~4 MB, but may not trigger
when writes are spread across connections).

---

## Update 2026-07-23: upstream sync + delivery_ledger + ongoing contention

### After upstream merge (2046 commits)

Merged upstream/main into fork. Key upstream changes that overlap with
our fixes:

- `c2a3b9ce5` PASSIVE WAL checkpoint (same as our fix)
- `0695a6bce` FTS5 segment merge in write path (reduces WAL hold time)
- `9acc4b47f` Schema v23 (external-content FTS + tool-row-free trigram)
- New `delivery_ledger.py` module (upstream addition)

### New freeze source: delivery_ledger

`delivery_ledger._connect()` created a new `sqlite3.connect()` + ran
`CREATE TABLE IF NOT EXISTS` DDL on every call. `mark_delivered` was
called from the event loop main thread via `_process_message_background`,
blocking the entire loop. Fixed by caching a singleton connection
(commit `6c89cef9a`).

### Ongoing contention: multi-connection write lock

Even after all fixes, the gateway still freezes when multiple SessionDB
instances concurrently write to state.db:

```
asyncio_1: _execute_write → replace_gateway_routing_entries (session store)
ThreadPoolExecutor-2_0: _execute_write → update_system_prompt (agent)
cron-parallel: _execute_write → create_session (cron job)
```

Each SessionDB instance has its own `sqlite3.Connection` to the same
WAL database. `BEGIN IMMEDIATE` on one connection blocks all others.
With `timeout=30s` + 15 retries, a single contested write can block
for up to 450 seconds before giving up.

### Root cause (confirmed, structural)

**SQLite is a single-writer database. Hermes creates N independent
SessionDB connections (gateway, cron, channel directory, delivery
ledger, SessionStore) that each acquire WAL write locks. No amount of
per-component caching or locking fixes this — the contention is at the
SQLite level, between separate connections.**

### Mitigations applied (all in this fork)

| # | Commit | Fix | Effect |
|---|--------|-----|--------|
| 1 | `49a36a407` | cron shared SessionDB | 1 conn for all cron ticks |
| 2 | `ea58523e4` | SQLite timeout 1.0→30.0 | Prevents cascading timeouts |
| 3 | `00273ae39` | channel directory cache + overlap guard | 1 conn for channel dir |
| 4 | `00aa84b63` | `_init_schema` per-db_path cache | DDL once per DB |
| 5 | `a65c15222` | `self._lock` on 3 SessionDB methods | Connection-level race |
| 6 | `381d24529`+`d4b405ab7`+`1c9ef1a7d` | WAL PASSIVE checkpoint in housekeeping | WAL bounded |
| 7 | `6c89cef9a` | delivery_ledger connection cache | 1 conn for delivery |
| 8 | `24bfe7840` | Auto-prune cron sessions every 2h | DB size bounded |
| - | config | cron frequency 10m→30m, staggered | 67% fewer writes |
| - | config | kanban disabled | Eliminates kanban.db contention |
| - | config | lark-oapi 1.5.5→1.6.8 | Feishu reconnect loop fix |

### Upstream status (2026-07-23)

| Issue/PR | Status | Relevant? |
|----------|--------|-----------|
| #57921 | open | timeout=1.0 too short (our fix: 30.0) |
| #39140 | open | schema init cache (our fix: _initialized_dbs) |
| #60884 | closed | Salvage of DB offloads — explicitly excluded schema cache |
| #44795 | open | _try_wal_checkpoint TRUNCATE corrupts WAL |
| #64573 | open | SQLite lock → cron session source=unknown |
| #24745 | open | TOCTOU race creating duplicate SessionDB connections |
| #54889 | closed | WAL checkpoint blocked by unclosed connections (memory provider) |
| `c2a3b9ce5` | merged | PASSIVE checkpoint (same as our fix) |
| `0695a6bce` | merged | FTS5 segment merge in write path |

**No upstream PR or issue proposes a systematic fix for the multi-connection
write-lock contention. All fixes are per-component patches.**

### Recommended root fix (deferred)

**Single-writer thread pattern**: one background thread processes all
state.db writes through a queue. Reads use read-only WAL connections
(never block writes). Eliminates write-lock contention entirely.

This is the same pattern Django uses for SQLite (`db.backends.sqlite3`).
See "Future directions" section above for full discussion.
