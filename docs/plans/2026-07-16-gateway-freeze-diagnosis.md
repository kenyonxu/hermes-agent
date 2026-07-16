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

## Root fix needed

`_init_schema` should NOT run unconditionally on every `SessionDB()` construction.
It should run once (first connect) and use a persistent marker (e.g. a row in
`state_meta` or `PRAGMA user_version`) to skip DDL on subsequent connections
to the same database file.

Alternatively, `_init_schema` could check whether the schema is already current
via a lightweight `SELECT count(*) FROM sqlite_master` (no write lock) and skip
the full `executescript` if all tables/indexes already exist.

## Watchdog

`scripts/gateway_freeze_watchdog.sh` runs via crontab every 2 minutes.
On freeze detection (gateway.log stale > 120s), captures py-spy dump to
`~/.hermes/profiles/zhihui/logs/freeze_dumps/`.
