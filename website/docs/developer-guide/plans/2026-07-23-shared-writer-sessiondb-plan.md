# Shared Writer SessionDB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all per-call `SessionDB()` write-access construction sites with a process-level shared singleton, eliminating SQLite WAL write-lock contention.

**Architecture:** Add `get_shared_session_db()` factory to `hermes_state.py`. All write-access callers get the same `SessionDB` instance (one `sqlite3.Connection`, one `threading.Lock`). Read-only callers keep their own connections. Gateway runtime and cron scheduler are migrated first (highest impact); CLI sites are optional follow-up.

**Tech Stack:** Python 3, sqlite3 (WAL mode), threading, pytest

**Spec:** [2026-07-23-shared-writer-sessiondb-design.md](2026-07-23-shared-writer-sessiondb-design.md)

---

## File Structure

| File | Responsibility | Task |
|------|---------------|------|
| `hermes_state.py` ~line 157 | Add `_shared_writer`, `_shared_writer_lock`, `get_shared_session_db()`, `close_shared_session_db()` | Task 1 |
| `tests/test_hermes_state.py` | Test shared singleton + thread safety | Task 1 |
| `gateway/run.py:3425` | Replace `SessionDB()` with `get_shared_session_db()` | Task 2 |
| `gateway/session.py:1072` | Replace `SessionDB()` with `get_shared_session_db()` | Task 2 |
| `cron/scheduler.py:504-540` | Replace local `_get_shared_session_db` with global one | Task 3 |
| `cron/scheduler.py:570-580` | Update shutdown to use `close_shared_session_db()` | Task 3 |

---

### Task 1: Add `get_shared_session_db()` factory + tests

**Files:**
- Modify: `hermes_state.py` (after line 157, `DEFAULT_DB_PATH` definition)
- Modify: `tests/test_hermes_state.py` (add test class)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_hermes_state.py`:

```python
class TestSharedWriterSessionDB:
    """get_shared_session_db returns one process-wide instance for write access."""

    def test_returns_same_instance(self):
        import hermes_state as hs
        hs._shared_writer = None
        try:
            db1 = hs.get_shared_session_db()
            db2 = hs.get_shared_session_db()
            assert db1 is not None
            assert db1 is db2
        finally:
            hs.close_shared_session_db()
            hs._shared_writer = None

    def test_close_resets_singleton(self):
        import hermes_state as hs
        hs._shared_writer = None
        try:
            db1 = hs.get_shared_session_db()
            hs.close_shared_session_db()
            db2 = hs.get_shared_session_db()
            assert db1 is not db2
        finally:
            hs.close_shared_session_db()
            hs._shared_writer = None

    def test_thread_safe_init(self):
        import hermes_state as hs
        import threading
        hs._shared_writer = None
        results = []
        barrier = threading.Barrier(5)

        def get_db():
            barrier.wait()
            results.append(hs.get_shared_session_db())

        threads = [threading.Thread(target=get_db) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert all(r is results[0] for r in results)
        finally:
            hs.close_shared_session_db()
            hs._shared_writer = None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hermes_state.py::TestSharedWriterSessionDB -v`
Expected: FAIL — `AttributeError: module 'hermes_state' has no attribute 'get_shared_session_db'`

- [ ] **Step 3: Add factory functions to hermes_state.py**

After the `DEFAULT_DB_PATH` line (line 157) in `hermes_state.py`, add:

```python
_shared_writer: Optional["SessionDB"] = None
_shared_writer_lock = threading.Lock()


def get_shared_session_db(db_path: Path = None) -> "SessionDB":
    """Return the process-wide shared SessionDB for write access.

    All callers writing to the same db_path share one SessionDB instance,
    one sqlite3.Connection, and one threading.Lock. This eliminates WAL
    write-lock contention between concurrent callers (gateway, cron,
    session store).

    Read-only callers should continue to use ``SessionDB(read_only=True)``
    directly — read-only WAL connections never acquire the write lock.

    In test environments (``PYTEST_CURRENT_TEST`` in env), returns a fresh
    ``SessionDB()`` each call so test isolation is preserved.
    """
    global _shared_writer
    _in_test = "PYTEST_CURRENT_TEST" in __import__("os").environ
    if _in_test:
        return SessionDB(db_path or DEFAULT_DB_PATH)

    key = str(db_path or DEFAULT_DB_PATH)
    if _shared_writer is not None and str(_shared_writer.db_path) == key:
        return _shared_writer
    with _shared_writer_lock:
        if _shared_writer is not None and str(_shared_writer.db_path) == key:
            return _shared_writer
        _shared_writer = SessionDB(db_path or DEFAULT_DB_PATH)
        return _shared_writer


def close_shared_session_db() -> None:
    """Close and reset the shared writer SessionDB singleton."""
    global _shared_writer
    with _shared_writer_lock:
        if _shared_writer is not None:
            try:
                _shared_writer.close()
            except Exception:
                pass
            _shared_writer = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hermes_state.py::TestSharedWriterSessionDB -v`
Expected: 3 PASS.

- [ ] **Step 5: Run existing state tests**

Run: `python -m pytest tests/test_hermes_state.py -q --no-header`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add hermes_state.py tests/test_hermes_state.py
git commit -m "feat(state): add get_shared_session_db process-level singleton factory

get_shared_session_db returns one process-wide SessionDB instance for
write access, eliminating WAL write-lock contention between concurrent
callers. Read-only callers continue to use SessionDB(read_only=True)
directly. Test environments bypass the singleton for isolation."
```

---

### Task 2: Migrate gateway runtime call sites

**Files:**
- Modify: `gateway/run.py:3423-3425` (gateway SessionDB init)
- Modify: `gateway/session.py:1070-1072` (SessionStore._db init)

- [ ] **Step 1: Update gateway/run.py**

In `gateway/run.py`, find the SessionDB initialization (line ~3423):

```python
        self._session_db = None
        try:
            from hermes_state import AsyncSessionDB, SessionDB
            self._session_db = AsyncSessionDB(SessionDB())
```

Replace `SessionDB()` with `get_shared_session_db()`:

```python
        self._session_db = None
        try:
            from hermes_state import AsyncSessionDB, get_shared_session_db
            self._session_db = AsyncSessionDB(get_shared_session_db())
```

- [ ] **Step 2: Update gateway/session.py**

In `gateway/session.py`, find the SessionStore init (line ~1070):

```python
        self._db = None
        try:
            from hermes_state import SessionDB
            self._db = SessionDB()
```

Replace with:

```python
        self._db = None
        try:
            from hermes_state import get_shared_session_db
            self._db = get_shared_session_db()
```

- [ ] **Step 3: Verify syntax**

Run: `python -c "import ast; ast.parse(open('gateway/run.py').read()); ast.parse(open('gateway/session.py').read()); print('ok')"`

- [ ] **Step 4: Run gateway tests**

Run: `python -m pytest tests/gateway/test_session.py tests/gateway/test_session_store_lock_io.py -q --no-header`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py gateway/session.py
git commit -m "fix(gateway): use shared SessionDB singleton for gateway and session store

Both gateway/run.py (AsyncSessionDB wrapper) and gateway/session.py
(SessionStore._db) now use get_shared_session_db() instead of creating
their own SessionDB() instances. They share one sqlite3.Connection,
eliminating the WAL write-lock contention that caused gateway freezes."
```

---

### Task 3: Migrate cron scheduler + cleanup

**Files:**
- Modify: `cron/scheduler.py:340-540` (replace local singleton with global)
- Modify: `cron/scheduler.py:570-583` (update shutdown)

- [ ] **Step 1: Replace local singleton with global import**

In `cron/scheduler.py`, remove the local `_shared_session_db`,
`_shared_session_db_lock`, `_get_shared_session_db`, and
`_close_shared_session_db` (lines 340-540).

Replace all references to `_get_shared_session_db()` with
`get_shared_session_db()` from `hermes_state`. Add the import at the top
of `run_job` where `_session_db` is assigned:

Find the current usage (line ~2660 in the merged code):
```python
    _session_db = _get_shared_session_db()
    if _session_db is None:
        logger.debug("Job '%s': shared SQLite session store not available", job.get("id", "?"))
```

Replace with:
```python
    from hermes_state import get_shared_session_db
    _session_db = get_shared_session_db()
    if _session_db is None:
        logger.debug("Job '%s': shared SQLite session store not available", job.get("id", "?"))
```

Also remove the module-level variables and functions (lines 340-343,
504-540).

- [ ] **Step 2: Update shutdown to use global close**

In `_shutdown_parallel_pool` (line ~570), replace:
```python
    _close_shared_session_db()
```
with:
```python
    from hermes_state import close_shared_session_db
    close_shared_session_db()
```

- [ ] **Step 3: Verify syntax**

Run: `python -c "import ast; ast.parse(open('cron/scheduler.py').read()); print('ok')"`

- [ ] **Step 4: Run cron tests**

Run: `python -m pytest tests/cron/test_shared_session_db.py -q --no-header`
Expected: tests may need updating since the local functions are removed.
If they fail, update them to import from `hermes_state` instead:

```python
# In test file, replace:
#   import cron.scheduler as sched
#   sched._get_shared_session_db()
# With:
#   import hermes_state as hs
#   hs.get_shared_session_db()
```

- [ ] **Step 5: Commit**

```bash
git add cron/scheduler.py tests/cron/test_shared_session_db.py
git commit -m "refactor(cron): use global get_shared_session_db from hermes_state

Remove cron/scheduler.py's local _shared_session_db singleton and replace
with the global get_shared_session_db() from hermes_state. This unifies
the cron scheduler and the gateway onto one shared SessionDB instance,
ensuring they never contend for the SQLite WAL write lock."
```

---

### Task 4: Full test sweep + restart gateway

**Files:**
- No code changes — verification only

- [ ] **Step 1: Run full state test suite**

Run: `python -m pytest tests/test_hermes_state.py -q --no-header`
Expected: all PASS.

- [ ] **Step 2: Run gateway + cron tests**

Run: `python -m pytest tests/gateway/test_session.py tests/gateway/test_session_store_lock_io.py tests/cron/ -q --no-header`
Expected: all PASS.

- [ ] **Step 3: Restart gateway**

```bash
systemctl --user restart hermes-gateway.service
```

- [ ] **Step 4: Verify gateway is healthy**

Wait 60s, then check:
```bash
systemctl --user status hermes-gateway.service | head -4
grep -E "✓ (discord|feishu)|Gateway running" ~/.hermes/profiles/zhihui/logs/agent.log | tail -3
```
Expected: active (running), 3 platforms connected.

- [ ] **Step 5: Send a test message and verify response**

Monitor gateway.log for the Discord message to be received and a response
sent within 30 seconds (no freeze).

- [ ] **Step 6: Push**

```bash
git push origin main
```
