# Read-Write Split + WAL Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only connection pool alongside the shared writer SessionDB so read-heavy callers stop contending for the writer's `self._lock`, and cap WAL growth via aggressive auto-checkpoint.

**Architecture:** A pool of 3 `SessionDB(read_only=True)` instances served round-robin via `get_read_only_session_db()`. An `AsyncReadOnlySessionDB` wrapper mirrors `AsyncSessionDB` but resolves a pool member per call. The gateway creates `_session_db_reader` after the writer; handoff watcher + compression checks migrate to it. Writer gets `PRAGMA wal_autocheckpoint=500`.

**Tech Stack:** Python 3, sqlite3 (WAL mode), threading, pytest

**Spec:** [2026-07-24-read-write-split-design.md](2026-07-24-read-write-split-design.md)

---

## File Structure

| File | Responsibility | Task |
|------|---------------|------|
| `hermes_state.py` ~line 157 | Reader pool vars + `get_read_only_session_db()` + `close_read_only_session_db()` | Task 1 |
| `hermes_state.py` ~line 9760 | `AsyncReadOnlySessionDB` class | Task 1 |
| `hermes_state.py` ~line 1500 | `PRAGMA wal_autocheckpoint=500` on writer connect | Task 1 |
| `tests/test_hermes_state.py` | Reader pool + read-only enforcement + visibility tests | Task 1 |
| `gateway/run.py:3432` | Create `_session_db_reader` after writer | Task 2 |
| `gateway/run.py:5787` | `_session_has_compression_in_flight` uses reader for `get_compression_lock_holder` | Task 2 |
| `gateway/run.py:8350` | Handoff watcher uses reader for `list_pending_handoffs` | Task 2 |

---

### Task 1: Reader pool + AsyncReadOnlySessionDB + auto-checkpoint + tests

**Files:**
- Modify: `hermes_state.py` (reader pool after line ~190, `AsyncReadOnlySessionDB` after `AsyncSessionDB` class, `wal_autocheckpoint` in `_connect_and_init`)
- Modify: `tests/test_hermes_state.py` (add `TestReadOnlyPool` class)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_hermes_state.py`:

```python
class TestReadOnlyPool:
    """get_read_only_session_db returns pooled read-only instances."""

    def test_pool_returns_distinct_instances(self, tmp_path, monkeypatch):
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "pool.db")
        hs._shared_writer = None
        hs._shared_readers = []
        try:
            hs.get_shared_session_db()  # writer first
            dbs = [hs.get_read_only_session_db() for _ in range(hs.READER_POOL_SIZE)]
            assert len(set(id(d) for d in dbs)) == hs.READER_POOL_SIZE
        finally:
            hs.close_read_only_session_db()
            hs.close_shared_session_db()
            hs._shared_writer = None
            hs._shared_readers = []

    def test_round_robin_cycles(self, tmp_path, monkeypatch):
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "pool.db")
        hs._shared_writer = None
        hs._shared_readers = []
        hs._reader_rr = 0
        try:
            hs.get_shared_session_db()
            d1 = hs.get_read_only_session_db()
            d2 = hs.get_read_only_session_db()
            assert d1 is not d2
            # After POOL_SIZE calls, we cycle back
            for _ in range(hs.READER_POOL_SIZE - 2):
                hs.get_read_only_session_db()
            d_next = hs.get_read_only_session_db()
            assert d_next is d1  # round-robin wraps
        finally:
            hs.close_read_only_session_db()
            hs.close_shared_session_db()
            hs._shared_writer = None
            hs._shared_readers = []

    def test_reader_rejects_writes(self, tmp_path, monkeypatch):
        import sqlite3
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "pool.db")
        hs._shared_writer = None
        hs._shared_readers = []
        try:
            writer = hs.get_shared_session_db()
            writer.create_session("s1", source="cli")
            reader = hs.get_read_only_session_db()
            with pytest.raises(sqlite3.OperationalError):
                reader.append_message("s1", role="user", content="hello")
        finally:
            hs.close_read_only_session_db()
            hs.close_shared_session_db()
            hs._shared_writer = None
            hs._shared_readers = []

    def test_write_visible_to_reader(self, tmp_path, monkeypatch):
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "pool.db")
        hs._shared_writer = None
        hs._shared_readers = []
        try:
            writer = hs.get_shared_session_db()
            writer.create_session("s1", source="cli", model="m1")
            reader = hs.get_read_only_session_db()
            row = reader.get_session("s1")
            assert row is not None
            assert row["source"] == "cli"
        finally:
            hs.close_read_only_session_db()
            hs.close_shared_session_db()
            hs._shared_writer = None
            hs._shared_readers = []

    def test_reader_requires_writer_first(self, tmp_path, monkeypatch):
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "pool.db")
        hs._shared_writer = None
        hs._shared_readers = []
        with pytest.raises(RuntimeError, match="writer SessionDB"):
            hs.get_read_only_session_db()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hermes_state.py::TestReadOnlyPool -v --no-header`
Expected: FAIL — `AttributeError: module 'hermes_state' has no attribute 'get_read_only_session_db'`

- [ ] **Step 3: Add reader pool to hermes_state.py**

After the `close_shared_session_db()` function (around line ~190), add:

```python
READER_POOL_SIZE = 3
_shared_readers: list = []
_shared_reader_lock = threading.Lock()
_reader_rr = 0


def get_read_only_session_db() -> "SessionDB":
    """Round-robin read-only SessionDB from a small pool.

    Must only be called after the shared writer exists: a read-only
    connection cannot perform crash recovery and relies on the -shm/-wal
    files the writer creates.
    """
    global _reader_rr
    with _shared_reader_lock:
        if not _shared_readers:
            if get_shared_session_db() is None:
                raise RuntimeError(
                    "writer SessionDB must be initialized first"
                )
            _shared_readers.extend(
                SessionDB(read_only=True) for _ in range(READER_POOL_SIZE)
            )
        db = _shared_readers[_reader_rr % len(_shared_readers)]
        _reader_rr += 1
        return db


def close_read_only_session_db() -> None:
    """Close all pool members (shutdown, test reset)."""
    global _shared_readers
    with _shared_reader_lock:
        for db in _shared_readers:
            try:
                db.close()
            except Exception:
                pass
        _shared_readers = []
```

- [ ] **Step 4: Add AsyncReadOnlySessionDB class**

After the `AsyncSessionDB` class (around line ~9745), add:

```python
class AsyncReadOnlySessionDB:
    """Async wrapper that resolves a pool member per call.

    Mirrors AsyncSessionDB but uses get_read_only_session_db() so reads
    never contend with the writer's self._lock. Each call rotates to the
    next pool connection for read concurrency.
    """

    def __getattr__(self, name: str):
        def _resolve(*args, **kwargs):
            db = get_read_only_session_db()
            attr = getattr(db, name)
            if not callable(attr):
                return attr
            return attr(*args, **kwargs)

        async def _offloaded(*args, **kwargs):
            return await asyncio.to_thread(_resolve, *args, **kwargs)

        return _offloaded
```

- [ ] **Step 5: Add wal_autocheckpoint to writer connect**

In `_connect_and_init()` (around line ~1497), after `apply_wal_with_fallback(self._conn, ...)`:

```python
                self._conn.execute("PRAGMA wal_autocheckpoint=500")
```

Add this line right after the existing `apply_wal_with_fallback` call and before `PRAGMA foreign_keys=ON`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_hermes_state.py::TestReadOnlyPool -v --no-header`
Expected: 5 PASS.

- [ ] **Step 7: Run existing state tests**

Run: `python -m pytest tests/test_hermes_state.py -q --no-header`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add hermes_state.py tests/test_hermes_state.py
git commit -m "feat(state): read-only connection pool + wal_autocheckpoint=500

Add get_read_only_session_db() — a round-robin pool of 3
SessionDB(read_only=True) instances. WAL mode allows unlimited
concurrent readers that never block the writer. AsyncReadOnlySessionDB
mirrors AsyncSessionDB but resolves a pool member per call.

Set PRAGMA wal_autocheckpoint=500 (~2MB) on the writer connection to
cap WAL growth without TRUNCATE (which corrupts B-trees on large DBs,
issue #45383).

Tests: pool round-robin, distinct instances, write rejection,
write visibility, writer-first requirement."
```

---

### Task 2: Migrate gateway read-heavy callers to reader

**Files:**
- Modify: `gateway/run.py:3432` (create `_session_db_reader`)
- Modify: `gateway/run.py:5787` (`_session_has_compression_in_flight`)
- Modify: `gateway/run.py:8350` (handoff watcher)

- [ ] **Step 1: Create _session_db_reader in gateway init**

In `gateway/run.py`, after the `_session_db` init block (line ~3432), add:

```python
        self._session_db_reader = None
        try:
            from hermes_state import AsyncReadOnlySessionDB
            self._session_db_reader = AsyncReadOnlySessionDB()
        except Exception as e:
            logger.debug("Read-only session DB pool not available: %s", e)
```

- [ ] **Step 2: Migrate handoff watcher to reader**

In the `_handoff_watcher` method (line ~8350), find:

```python
                pending = await self._session_db.list_pending_handoffs()
```

Replace with:

```python
                if self._session_db_reader is not None:
                    pending = await self._session_db_reader.list_pending_handoffs()
                else:
                    pending = await self._session_db.list_pending_handoffs()
```

- [ ] **Step 3: Migrate compression lock holder check to reader**

In `_session_has_compression_in_flight` (line ~5787), find:

```python
        session_db = getattr(self, "_session_db", None)
        if session_db is None:
            return False
        raw_db = getattr(session_db, "_db", session_db)
        try:
            holder = await asyncio.to_thread(
                raw_db.get_compression_lock_holder, str(session_id)
            )
```

Replace with:

```python
        reader = getattr(self, "_session_db_reader", None)
        if reader is not None:
            try:
                holder = await reader.get_compression_lock_holder(str(session_id))
                return bool(holder)
            except Exception:
                pass  # fall through to writer
        session_db = getattr(self, "_session_db", None)
        if session_db is None:
            return False
        raw_db = getattr(session_db, "_db", session_db)
        try:
            holder = await asyncio.to_thread(
                raw_db.get_compression_lock_holder, str(session_id)
            )
```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('gateway/run.py').read()); print('ok')"`

- [ ] **Step 5: Run gateway tests**

Run: `python -m pytest tests/gateway/test_session.py tests/gateway/test_session_store_lock_io.py -q --no-header`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add gateway/run.py
git commit -m "fix(gateway): migrate handoff watcher + compression check to reader pool

Handoff watcher (list_pending_handoffs, every 2s) and compression
in-flight check (get_compression_lock_holder, per message) now use
the read-only pool instead of the shared writer. These high-frequency
reads no longer contend for the writer's self._lock.

Reader falls back to writer if the pool is unavailable."
```

---

### Task 3: Full test sweep + restart gateway

**Files:**
- No code changes — verification only

- [ ] **Step 1: Run full state + gateway + cron tests**

Run: `python -m pytest tests/test_hermes_state.py tests/gateway/test_session.py tests/cron/test_shared_session_db.py -q --no-header`
Expected: all PASS.

- [ ] **Step 2: Restart gateway**

```bash
systemctl --user restart hermes-gateway.service
```

- [ ] **Step 3: Verify gateway health**

Wait 60s, check:
```bash
systemctl --user status hermes-gateway.service | head -4
grep -E "✓ (discord|feishu)|Gateway running" ~/.hermes/profiles/zhihui/logs/agent.log | tail -3
```

- [ ] **Step 4: Monitor WAL size**

```bash
ls -lh ~/.hermes/profiles/zhihui/state.db-wal
```
Expected: WAL stable at ~2-4 MB, not growing unbounded.

- [ ] **Step 5: Push**

```bash
git push origin main
```
