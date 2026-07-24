# Read-Write Split + WAL Management Design

> **Date**: 2026-07-24
> **Supersedes**: [2026-07-23-shared-writer-sessiondb-design.md] for read path
> **Diagnosis**: [2026-07-16-gateway-freeze-diagnosis.md]

## Problem

The shared SessionDB singleton (commit `82c65674f`) eliminated WAL
write-lock contention by serializing all access through one connection +
one `threading.Lock`. But it introduced a new bottleneck: **read
operations are also serialized** by `self._lock`.

On a large WAL file (81 MB observed), a single slow query
(`get_compression_tip` — recursive CTE scanning WAL frames) holds
`self._lock` for seconds, blocking every other read and write. The
handoff watcher (reads every 2s), channel directory (reads every 5 min),
and the Discord message handler (reads + writes) all queue behind it.

Meanwhile WAL grows unchecked because PASSIVE checkpoint every 50 writes
on one connection isn't aggressive enough for high-write workloads.

## Design

### Core principle

**Split read and write paths:**

- **Writes**: one shared SessionDB instance, one connection, one lock,
  serialized. Already implemented (`get_shared_session_db()`). No change.
- **Reads**: independent read-only connection(s) via WAL mode —
  unlimited concurrency, never blocked by writes, never block writes.
- **WAL management**: aggressive auto-checkpoint on the write connection
  to keep WAL small (<5 MB).

### Component 1: Read-only connection pool

Add a cached read-only SessionDB to `hermes_state.py`:

```python
_shared_reader: Optional["SessionDB"] = None
_shared_reader_lock = threading.Lock()

def get_read_only_session_db() -> "SessionDB":
    """Return a cached read-only SessionDB for SELECT queries.

    WAL mode allows unlimited concurrent readers that never block
    writers. This connection skips _init_schema (read-only) and never
    acquires the WAL write lock.
    """
    global _shared_reader
    if _shared_reader is not None:
        return _shared_reader
    with _shared_reader_lock:
        if _shared_reader is not None:
            return _shared_reader
        _shared_reader = SessionDB(read_only=True)
        return _shared_reader
```

Read-only connections use `check_same_thread=False` and do NOT need
`self._lock` for concurrent access — SQLite WAL readers are safe to
concurrent use on the same connection from multiple threads (they never
mutate connection state, only read). The existing `with self._lock:`
around read methods can be kept as a safety net (serializes reads
among themselves but they're fast), or removed for true read concurrency.

**Decision**: keep `self._lock` on read methods. It serializes reads on
the shared reader connection (preventing the sqlite3 connection race we
fixed in commit `a65c15222`), but since the read-only connection never
holds the WAL write lock, it never blocks the writer. The shared reader
and shared writer have DIFFERENT locks — they don't contend.

### Component 2: Migrate read-heavy callers to read-only connection

High-frequency read callers that don't need write access:

| Caller | Method | Frequency | Current | Migrate to |
|--------|--------|-----------|---------|-----------|
| Handoff watcher | `list_pending_handoffs` | every 2s | shared writer | read-only |
| Compression check | `get_compression_tip` | per message | shared writer | read-only |
| Compression check | `get_compression_failure_cooldown` | per agent init | shared writer | read-only |
| Session lookup | `get_session` | per message | shared writer | read-only |
| Session lookup | `get_compression_lock_holder` | per message | shared writer | read-only |
| Channel directory | `list_gateway_sessions` | every 5 min | already read-only | no change |
| Handoff | `get_handoff_state` | per handoff | shared writer | read-only |

**Strategy**: Instead of changing every call site, add a new method on
SessionDB: `get_read_only_view()` that returns a lightweight proxy
pointing to the read-only connection. Or simpler: the callers that
matter most (handoff watcher, compression checks) can directly use
`get_read_only_session_db()`.

**Simplest approach**: `AsyncSessionDB.__getattr__` already offloads
every method via `to_thread`. Add a separate `AsyncReadOnlySessionDB`
(or just a second `AsyncSessionDB` wrapping `get_read_only_session_db()`)
for read-only access. The gateway creates one of each.

### Component 3: Aggressive WAL auto-checkpoint

On the shared writer connection, set:

```python
self._conn.execute("PRAGMA wal_autocheckpoint=500")  # ~2MB
```

SQLite default is 1000 pages (~4MB). Reducing to 500 means PASSIVE
checkpoint triggers more frequently, keeping WAL under ~2MB. Since
there's only one writer now, auto-checkpoint always has a chance to
run (no contention from other writers).

### Component 4: Housekeeping TRUNCATE checkpoint

Already implemented (every 30 min PASSIVE). But PASSIVE doesn't truncate.
Add an occasional TRUNCATE when WAL is small (early morning / idle):

```python
# In housekeeping, every 2 hours:
result = session_db._try_wal_checkpoint(truncate=True)
```

But `_try_wal_checkpoint` now uses PASSIVE-only (upstream change).
Add a separate `_try_wal_truncate()` for the housekeeping path that
uses TRUNCATE mode explicitly, guarded by a "no active readers" check
via `PRAGMA wal_checkpoint(RESTART)`.

### What changes

1. `hermes_state.py`: add `_shared_reader`, `get_read_only_session_db()`,
   `close_read_only_session_db()`, set `wal_autocheckpoint=500` on writer.
2. `gateway/run.py`: create `_session_db_reader` alongside `_session_db`.
3. Handoff watcher: use reader for `list_pending_handoffs`.
4. Compression checks: use reader for `get_compression_tip`,
   `get_compression_failure_cooldown`, `get_compression_lock_holder`.
5. Housekeeping: add periodic TRUNCATE checkpoint.

### What doesn't change

- `SessionDB` internals — no changes to method implementations.
- Write path — still `get_shared_session_db()`, one connection, one lock.
- Read-only callers (channel directory) — already on read-only.
- Test infrastructure — tests create their own SessionDB directly.

## Verification

```bash
python -m pytest tests/test_hermes_state.py -q
python -m pytest tests/gateway/test_session.py tests/cron/ -q
```

After deploy:
- Monitor WAL size: `ls -lh ~/.hermes/profiles/zhihui/state.db-wal`
- Monitor freeze: py-spy dump should show read threads NOT blocking writes.
- Send Discord messages and verify responses within 30s.

## Out of scope

- Removing `self._lock` from read methods on the read-only connection
  (possible future optimization for true read concurrency).
- Migrating CLI call sites (they're single-threaded, no contention).
- Upstream PR — defer until stable.
