# Upstream Sync Plan: 2026-08-03

> **Date**: 2026-08-03
> **Divergence**: 59 local commits ahead, 2512 upstream commits behind
> **Merge base**: `5349c7c28`
> **Conflicts**: 5 files

## Background

Last sync was 2026-07-23 (commit `0822c8e56`). Since then we made 14
more local commits (read-write split, DELETE journal mode, delivery
ledger fixes, reader pool). Upstream made 2512 commits including a major
session activity watchdog PR (#72424), routing UPSERT optimization, and
many schema/state improvements.

## Conflict inventory (5 files)

### 1. `hermes_state.py` (72 upstream commits, highest risk)

**Our local changes:**
- `get_shared_session_db()` + `close_shared_session_db()` singleton
- `get_read_only_session_db()` reader pool + `AsyncReadOnlySessionDB`
- `apply_wal_with_fallback()` forced to DELETE mode
- `_init_schema` per-db_path cache (`_initialized_dbs`)
- `self._lock` added to `list_pending_handoffs`, `get_handoff_state`,
  `get_compression_lock_holder`
- `_execute_write` health check (closed connection rebuild)

**Upstream changes:**
- `92c736919`: sub-second busy budget for activity writes
- `50c4afe40`: single-row routing UPSERT fast path
- `c2088efe9`: session activity watchdog + stall notify
- `e38055a85`: close read-only connection on FTS probe failure
- FTS5 CJK bigram improvements
- VACUUM throttling
- Multiple retry/patience improvements in `_execute_write`

**Strategy**: Take upstream as base. Re-apply our non-overlapping changes:
- Keep `get_shared_session_db()` + reader pool (upstream doesn't have it)
- Keep `_init_schema` cache
- Keep DELETE journal mode override
- Check if upstream already added `self._lock` to the 3 methods
- Adopt upstream's UPSERT optimization (complements our shared writer)
- Adopt upstream's activity write budget (reduces write contention)

### 2. `gateway/run.py` (171 upstream commits)

**Our local changes:**
- `_session_db_reader` (AsyncReadOnlySessionDB) creation
- `_reader_call` helper
- Handoff watcher uses reader
- Compression checks use reader
- WAL checkpoint removed from housekeeping
- delivery_ledger mark_delivered offloaded to to_thread

**Upstream changes:**
- Session activity watchdog
- Stall notification
- Compression timeout
- Multiple delivery/gateway routing improvements

**Strategy**: Take upstream as base. Re-apply our reader migration and
_reader_call helper. Upstream's watchdog is beneficial and should be
kept.

### 3. `gateway/session.py` (14 upstream commits)

**Our local changes:**
- `SessionStore._db` uses `get_shared_session_db()` with fallback

**Upstream changes:**
- UPSERT routing fast path (`_save_entries` optimization)
- Various routing fixes

**Strategy**: Take upstream as base. Re-apply shared SessionDB.

### 4. `gateway/delivery_ledger.py` (2 upstream commits)

**Our local changes:**
- `_get_conn` routed through shared SessionDB
- `_DB_LOCK` removed from `_get_conn`
- `_close_conn` is no-op

**Strategy**: Check what upstream changed. Likely minor — keep our
version.

### 5. Tests (3 files)

- `tests/test_hermes_state.py` — our TestSharedWriterSessionDB + TestReadOnlyPool
  were lost in last merge, need to restore again
- `tests/gateway/test_channel_directory.py` — minor conflict
- `tests/gateway/test_delivery_ledger.py` — our cached connection test changes

### 6. `.gitignore` (trivial)

Merge both sets of rules.

## Approach

**Option A: Merge upstream into main (recommended)**

Same as last time. Resolve 5 conflicts manually, commit, test, restart.

### Conflict resolution priority

1. `hermes_state.py` — highest risk, most changes
2. `gateway/run.py` — second highest
3. `gateway/session.py` — moderate
4. Tests — after code is resolved
5. `.gitignore` — trivial

### Post-merge checklist

1. Verify `get_shared_session_db()` and reader pool survive the merge
2. Verify DELETE journal mode override survives
3. Verify `_init_schema` cache survives
4. Run `pytest tests/test_hermes_state.py -q` (418+ tests)
5. Run `pytest tests/gateway/test_session.py -q`
6. Restart gateway and verify stability
7. Clean DB if >400 MB (prune cron sessions + VACUUM)

## Key upstream improvements to adopt

| Commit | Description | Why we want it |
|--------|------------|---------------|
| `50c4afe40` | Single-row routing UPSERT | Reduces per-turn write from ~50ms to <1ms |
| `92c736919` | Sub-second activity write budget | Prevents observational writes from blocking responses |
| `c2088efe9` | Session activity watchdog | Auto-detect + restart on freeze (already seen working) |
| `e38055a85` | Close RO connection on FTS probe failure | Prevents connection leak |
| `bd856a02f` | VACUUM throttle | Prevents repeated VACUUM rewrites |

## Key local changes to preserve

| Commit | Description | Why upstream doesn't have it |
|--------|------------|----------------------------|
| `82c65674f` | Shared SessionDB singleton | Eliminates multi-connection write contention |
| `648d236c1` | Reader pool | Eliminates read serialization |
| `47f4e7bac` | DELETE journal mode | Eliminates WAL management |
| `00aa84b63` | `_init_schema` cache | Eliminates DDL per-connection |
| `a65c15222` | `self._lock` on 3 methods | Eliminates connection race |
| `6c89cef9a`+`1ad876703` | delivery_ledger fixes | Event loop blocking + deadlock |
