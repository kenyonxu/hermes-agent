# Shared Writer SessionDB Design

> **Date**: 2026-07-23
> **Diagnosis**: [2026-07-16-gateway-freeze-diagnosis.md](../../plans/2026-07-16-gateway-freeze-diagnosis.md)
> **Approach**: A — process-level shared write connection

## Problem

27 call sites across the codebase create independent `SessionDB()`
instances. Each instance opens a separate `sqlite3.Connection` to the
same `state.db` WAL file. When two instances call `_execute_write`
(`BEGIN IMMEDIATE`) concurrently, they compete for the SQLite WAL write
lock. The `timeout=30.0` + 15 retries means a single contested write
can block for up to 450 seconds.

This has caused 10+ gateway freeze events over 2 weeks. Every fix
(schema cache, shared cron SessionDB, channel directory cache,
delivery ledger cache, WAL checkpoint) reduced contention at specific
sites but could not eliminate it — the contention is structural, at the
SQLite connection level.

## Design

### Core principle

**One write connection per process per database file.**

All `SessionDB` instances that write to `state.db` share a single
`sqlite3.Connection` and a single `threading.Lock`. Read-only callers
(`read_only=True`) keep their own connections (they never acquire the
WAL write lock and never contend).

### Implementation: `get_shared_session_db()` factory

Add a module-level function to `hermes_state.py`:

```python
_shared_writer: Optional["SessionDB"] = None
_shared_writer_lock = threading.Lock()

def get_shared_session_db(db_path: Path = None) -> "SessionDB":
    """Return the process-wide shared SessionDB for write access.

    All callers writing to the same db_path share one SessionDB instance,
    one sqlite3.Connection, and one threading.Lock. This eliminates WAL
    write-lock contention between concurrent callers.

    Read-only callers should continue to use SessionDB(read_only=True)
    directly — read-only WAL connections never acquire the write lock
    and never contend.
    """
    global _shared_writer
    key = str(db_path or DEFAULT_DB_PATH)
    if _shared_writer is not None and str(_shared_writer.db_path) == key:
        return _shared_writer
    with _shared_writer_lock:
        if _shared_writer is not None and str(_shared_writer.db_path) == key:
            return _shared_writer
        _shared_writer = SessionDB(db_path or DEFAULT_DB_PATH)
        return _shared_writer
```

### Migration plan

Replace all `SessionDB()` write-access call sites with
`get_shared_session_db()`. The 27 call sites fall into three categories:

**Category 1: Gateway runtime (highest impact)**

These are the sites that cause gateway freezes — they run concurrently
on the event loop's thread pool:

| File | Line | Current | Replace with |
|------|------|---------|-------------|
| `gateway/run.py` | 3425 | `AsyncSessionDB(SessionDB())` | `AsyncSessionDB(get_shared_session_db())` |
| `gateway/session.py` | 1072 | `self._db = SessionDB()` | `self._db = get_shared_session_db()` |
| `gateway/delivery_ledger.py` | cached | Already cached (singleton) | Already shared |
| `gateway/channel_directory.py` | 128 | `SessionDB(read_only=True)` | No change (read-only) |

**Category 2: Cron scheduler**

Already has its own shared singleton (`_get_shared_session_db`).
Replace with the global one for consistency:

| File | Line | Current | Replace with |
|------|------|---------|-------------|
| `cron/scheduler.py` | 520 | `_shared_session_db = SessionDB()` | Use `get_shared_session_db()` from hermes_state |

**Category 3: CLI / oneshot / console (low impact)**

These run in single-threaded contexts (CLI), not concurrent with the
gateway. Migration is optional but recommended for consistency:

| File | Lines | Count |
|------|-------|-------|
| `hermes_cli/console_engine.py` | 1324, 1340, 1411, 1448, 1469 | 5 |
| `hermes_cli/main.py` | 1284, 1423, 1476 | 3 |
| `hermes_cli/goals.py` | 528 | 1 |
| `hermes_cli/oneshot.py` | 307 | 1 |
| `hermes_cli/cli_commands_mixin.py` | 591 | 1 |
| `gateway/slash_commands.py` | 4479 | 1 |
| `gateway/mirror.py` | 117, 196 | 2 |

**Total**: 11 gateway/cron sites (high priority) + 14 CLI sites (optional).

### What changes

- `hermes_state.py`: add `_shared_writer`, `_shared_writer_lock`,
  `get_shared_session_db()`, `close_shared_session_db()`
- All Category 1 + 2 call sites: replace `SessionDB()` with
  `get_shared_session_db()`
- `cron/scheduler.py`: replace `_get_shared_session_db()` with
  `get_shared_session_db()` from hermes_state
- Category 3: optional, can be done in a follow-up

### What doesn't change

- `SessionDB` internals — no changes to `_execute_write`, `_init_schema`,
  `_try_wal_checkpoint`, etc.
- Read-only callers — `SessionDB(read_only=True)` stays as-is
- `AsyncSessionDB` — still wraps the shared instance, still offloads
  via `asyncio.to_thread`
- Test fixtures — bypass the shared singleton (PYTEST_CURRENT_TEST guard
  already in `_init_schema` cache; add same guard to shared writer)

### Thread safety

With a single shared `SessionDB` instance:
- `self._lock` (threading.Lock) serializes ALL connection access — reads
  and writes. Only one thread uses `self._conn` at a time.
- `BEGIN IMMEDIATE` never contends with another connection because there
  IS no other write connection.
- Read-only connections (channel directory, dashboard) use separate
  `sqlite3.Connection` objects but never call `BEGIN IMMEDIATE` — they
  only do `SELECT`, which in WAL mode does not block the writer.

The serialization cost is that all writes (and reads on the shared
connection) go through one lock. But since each operation takes
microseconds to low milliseconds, and the alternative is multi-second
lock contention freezes, this is a massive net improvement.

### Shutdown

`close_shared_session_db()` closes the singleton's connection. Called
from:
- `gateway/run.py` shutdown sequence (alongside `_shutdown_executor`)
- `cron/scheduler.py` `_shutdown_parallel_pool` (replaces
  `_close_shared_session_db`)

### Backward compatibility

`SessionDB()` direct construction still works for:
- Tests (PYTEST_CURRENT_TEST bypasses all caches)
- Read-only callers
- Future code that needs a separate connection for a specific reason

No API removal — just a new factory function that the call sites
opt into.

## Verification

```bash
# State tests
python -m pytest tests/test_hermes_state.py -q

# Gateway session tests
python -m pytest tests/gateway/test_session.py tests/gateway/test_session_store_lock_io.py -q

# Cron tests
python -m pytest tests/cron/test_shared_session_db.py -q

# Delivery ledger tests
python -m pytest tests/gateway/test_delivery_ledger.py -q

# Full gateway sweep
python -m pytest tests/gateway/ -k "session or delivery or cron" -q
```

## Out of scope

- Single-writer-thread queue pattern (Option B) — more complex, fire-
  and-forget semantics change the API contract. Can be built on top of
  this shared connection later if needed.
- CLI call site migration (Category 3) — optional follow-up.
- Upstream PR — defer until stable locally.
