# Spec: Move all I/O out of session_store._lock in get_or_create_session

> **Analysis**: [2026-07-10-session-store-lock-analysis.md](2026-07-10-session-store-lock-analysis.md)
> **Implementation plan**: [docs/superpowers/plans/2026-07-10-session-store-lock-io-fix.md](../superpowers/plans/2026-07-10-session-store-lock-io-fix.md)

---

## 1. Problem

`gateway/session.py:get_or_create_session` (line 1588) is called on every
inbound message. Its second `with self._lock:` block (line 1622) holds the
store lock for the duration of **six** blocking operations, but a code
comment at line 1607 claims the opposite:

```python
# SQLite calls are made outside the lock to avoid holding it during I/O.
```

That comment is accurate only for `_compression_tip_for_session_id`,
which was moved out. The remaining I/O was left behind.

### Complete I/O inventory inside the second lock block

| # | Call | Line | I/O type | Frequency | Previous doc missed? |
|---|------|------|----------|-----------|---------------------|
| 1 | `_ensure_loaded_locked()` | 1623 | no-op after startup | every msg | covered |
| 2 | `_is_session_ended_in_db()` | 1649 | SQLite SELECT | every msg (existing session) | covered |
| 3 | `_should_reset()` -> `_has_active_processes_fn()` | 1671, 1679 | callback (may block) | every msg (existing session) | **missed** |
| 4 | `_save()` | 1696, 1702, 1727, 1748 | SQLite write + JSON write + `os.fsync` | every msg | partially covered |
| 5 | `_recover_session_from_db()` | 1718 | SQLite SELECT + `reopen_session` UPDATE | recovery path only | **missed** |

The happy path (session continues, no reset) hits #2 + #3 + #4 on every
single message. On a large `state.db` (~1.4 GB reported) or slow storage,
that holds `self._lock` for tens to hundreds of milliseconds per message,
serializing concurrent `to_thread` workers.

### What the function already does right

- `_compression_tip_for_session_id` (line 1616) -- SQLite SELECT, correctly
  outside the lock
- `end_session` (line 1757) -- SQLite UPDATE, correctly outside the lock
- `create_session` (line 1763) -- SQLite INSERT, correctly outside the lock
- `_record_gateway_session_peer` (line 1767) -- SQLite INSERT, correctly
  outside the lock

The refactor extends this existing pattern to the remaining calls.

---

## 2. Approach: two-phase refactor

Instead of surgically extracting individual I/O calls (which leaves gaps and
risks `dict changed size during iteration` on `_save`), restructure the
function into a clean lock / no-lock split that mirrors what the bottom of
the function already does:

### Phase 1: under lock -- pure in-memory work

- `_ensure_loaded_locked()` (no-op)
- Read / pop / create entries in `self._entries`
- Determine `db_end_session_id`, `db_create_kwargs`, reset flags
- Snapshot `self._entries` for the save (avoids concurrent-mutation race)
- Set `_needs_save = True` instead of calling `_save()`
- Set `_needs_recover = True` instead of calling `_recover_session_from_db`

### Phase 1b: outside lock -- SQLite lookups needed before phase 2

- `_is_session_ended_in_db(session_id)` -- result feeds back into phase 2
- `_should_reset` -> `_has_active_processes_fn()` -- result feeds phase 2

These are read-only checks on `session_id` (immutable on `SessionEntry`).
After they complete, phase 2 re-validates the entry under lock before
mutating `_entries`.

### Phase 2: under lock -- apply decisions

- Re-validate entry (same `session_id`? still present?)
- Pop stale entries, create new entries
- Capture entries snapshot + flags

### Phase 3: outside lock -- all write I/O

- `_recover_session_from_db` if `_needs_recover`
- `_save(entries_snapshot)` if `_needs_save`
- `end_session`, `create_session`, `_record_gateway_session_peer`
  (already here)

---

## 3. Why not the original surgical approach

The previous spec proposed two separate fixes:
1. Move `_is_session_ended_in_db` outside the lock
2. Defer `_save()` via a dirty flag

Problems with that approach:

- **Misses 4 of 6 I/O points.** The happy path still holds the lock during
  `_should_reset` and the recovery path still holds it during
  `_recover_session_from_db`.
- **`_save()` race risk.** Reading `self._entries` outside the lock without
  a snapshot can trigger `RuntimeError: dictionary changed size during
  iteration` if another worker adds/removes a key concurrently.
- **Early returns.** Three `return entry` statements inside the lock block
  (lines 1697, 1703, 1724) each preceded by `_save()`. Converting each to a
  dirty flag requires eliminating the early returns, which the previous plan
  acknowledged but did not address.
- **Incomplete pseudocode.** The previous plan's fix 1 step 3 handled only
  the stale branch and hand-waved the 45-line else branch.

The two-phase approach is one coherent change, not two fragile patches.

---

## 4. Scope

**In scope:** `get_or_create_session` in `gateway/session.py` only. This is
the per-message hot path.

**Out of scope but noted for follow-up:**

- `update_session` (line 1786) -- also calls `_save()` under lock, but runs
  after the agent turn, not on the message intake critical path.
- `set_model_override`, `switch_session`, `reset_session` -- all call
  `_save()` under lock, but are rare (slash-command driven).
- `_schedule_resume_pending_sessions` in `gateway/run.py` -- acquires the
  lock on the event loop thread, but only at startup/reconnect and
  `_ensure_loaded_locked` is a no-op there.

---

## 5. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Entry modified between phase 1b and phase 2 | Re-validate `entry.session_id` matches the value checked in 1b; if not, re-run from phase 1 (rare) |
| `_save()` iterates `self._entries` outside lock | Snapshot `dict(self._entries)` under lock; pass snapshot to `_save` or to a new `_save_snapshot(snapshot)` method |
| `_recover_session_from_db` sets `_entries[key]` | Split: DB query + entry construction outside lock; `_entries[key] = entry` assignment under lock in phase 2 |
| Behavioral change: save no longer atomic with entry mutation | Acceptable: `_save` writes a consistent point-in-time snapshot; a concurrent message that also sets `_needs_save` triggers its own save |
| Lost save if process crashes between lock release and `_save` | Same risk as the existing post-lock `create_session` / `end_session` calls; the routing index is a recoverable cache (primary store is `state.db`) |

---

## 6. Verification

```bash
# New lock-I/O regression test
python -m pytest tests/gateway/test_session_store_lock_io.py -v

# Existing session store tests (behavioral regression)
python -m pytest tests/gateway/test_session.py tests/gateway/test_session_store_runtime_stale_guard.py -q

# Compression-in-flight check still works (uses session_store lock)
python -m pytest tests/gateway/test_compression_in_flight_check.py -q

# Full gateway session test suite
python -m pytest tests/gateway/ -k "session" -q
```
