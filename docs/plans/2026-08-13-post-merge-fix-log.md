# Post-Upstream-Merge Fix Log (2026-08-03 — 2026-08-13)

> **Merge base**: `a37251bc6` (2026-08-03 upstream sync, 2512 upstream commits)
> **Root cause class**: Journal mode contention under DELETE mode

## Background

On 2026-07-26 we switched from WAL to DELETE journal mode (commit
`47f4e7bac`) to eliminate WAL checkpoint starvation. This worked well
until the 2026-08-03 upstream merge introduced three new independent
SQLite modules that each called `apply_wal_with_fallback()` on every
`_connect()`. In DELETE mode, switching to WAL requires exclusive
database access — each attempt froze all other connections for the
duration.

## Symptom timeline

| Date | Symptom | Root cause |
|------|---------|------------|
| 08-03 | Gateway watchdog auto-restart (delivery_ledger._connect blocks event loop) | delivery_ledger._initialize_schema calls apply_wal_with_fallback |
| 08-06 | Frequent freezes, kanban notifier + delivery_ledger contending | Both modules creating independent connections that fight for journal mode lock |
| 08-08 | "Cannot operate on a closed database" after every message | delivery_ledger._transaction() closes shared SessionDB connection |
| 08-09 | Provider auth failures + closed database | DeepSeek key expiry + delivery_ledger connection lifecycle |
| 08-10 | Persistent freezes, compression blocking | delivery_ledger still calling apply_wal_with_fallback (merge regression) |
| 08-11 | _connect blocks on every cron tick | cron/executions.py also calls apply_wal_with_fallback |
| 08-12 | More freezes + session compression stalls | Same + verification_evidence.py third offender |
| 08-13 | Final fix: last apply_wal_with_fallback removed | All three modules cleaned |

## Fixes applied (chronological)

### 1. Kanban watchers disabled (`a22603ae55`)

Upstream merge re-enabled kanban notifier/dispatcher watchers despite
config `kanban.dispatch_in_gateway: false`. The notifier was not gated
by this config and ran every 5s, connecting to `kanban.db` and blocking.
Commented out `_spawn_supervised` calls for both watchers.

### 2. delivery_ledger connection routing (`75189ee340`)

Initial fix: route `delivery_ledger._connect()` through
`get_shared_session_db()._conn` to avoid independent connections.

### 3. delivery_ledger deadlock fix (`1ad876703`)

The shared-connection approach created an AB-BA deadlock:
`_DB_LOCK` (delivery_ledger) held while accessing `_conn` outside
`SessionDB._lock`. Removed `_DB_LOCK` from `_get_conn`.

### 4. delivery_ledger cached connection (`cd58a3eabf`)

`_transaction()` always closes its connection in `finally`. When
`_connect()` returned the shared writer's connection, every
`mark_delivered` call closed it for everyone. Fixed by caching a
dedicated connection.

### 5. Reverted to upstream independent-conn (`501f3e1e0e`)

Cached dedicated connection broke test isolation (survived across
pytest fixtures). Reverted to upstream's fresh-connection-per-call
pattern, which is correct — `_transaction()` closes only its own
connection.

### 6. SessionStore auto-rebuild (`9fa3a90b2a`)

When the shared SessionDB connection was closed by any code path,
`SessionStore._db` kept a stale reference. Added `_get_db()` helper
that checks connection health and rebuilds from
`get_shared_session_db()` if closed.

### 7. delivery_ledger WAL removal (`1e9ee0fbf6`)

Removed `apply_wal_with_fallback` from `_initialize_schema`. This was
the first fix targeting the actual root cause: in DELETE mode, every
WAL initialization attempt blocks all connections.

### 8. delivery_ledger + cron executions WAL removal (`2c14d50285`)

Removed `apply_wal_with_fallback` from both `delivery_ledger.py` and
`cron/executions.py`. Bumped delivery_ledger timeout from 10s to 30s.

### 9. verification_evidence WAL removal (`e45d8a2c19`)

Removed `apply_wal_with_fallback` from `agent/verification_evidence.py`.
This was the last remaining external caller.

### 10. Remaining apply_wal_with_fallback callers removed (2026-08-13)

The Verification section below previously claimed only `hermes_state.py`
called `apply_wal_with_fallback`. A same-day audit found that claim was
wrong — seven external call sites remained, and all were removed:

| Module | DB file | Notes |
| -------- | --------- | ------- |
| `tools/async_delegation.py` | state.db | **Hot path** — every `_connect()` hit the shared DB |
| `cron/notepad.py` | cron/notepad.db | every notepad transaction |
| `hermes_cli/kanban_db.py` (2 sites) | kanban.db | every connect, incl. steady-state fast path |
| `gateway/platforms/api_server.py` | response_store.db | store init |
| `plugins/memory/holographic/store.py` | memory_store.db | `_init_db()` |
| `plugins/platforms/discord/recovery.py` | discord_recovery.db | recovery ledger init |
| `hermes_cli/projects_db.py` | projects.db | every connect |

The two kanban NFS-fallback tests were rewritten to the new contract —
`connect()` must never attempt a journal-mode switch:
`test_connect_never_attempts_wal_switch`,
`test_connect_preserves_preexisting_wal_mode`.

### 11. `database.journal_mode: delete` set in config (2026-08-13)

Applied to both `~/.hermes/config.yaml` and the active profile
`~/.hermes/profiles/zhihui/config.yaml`. The gateway resolves config
against the **profile** home, so editing only the default-home file would
have left it unprotected. Verified: `resolve_journal_mode()` returns
`delete` under both homes.

### Why the surviving callers had not been freezing the gateway

Incidental protection, not design: this host's SQLite is **3.45.1**, which
falls inside the WAL-reset-bug range (3.7.0–3.51.2; backports only in
3.50.7/3.44.6), so `is_sqlite_wal_reset_vulnerable()` gated every
`apply_wal_with_fallback` call into `_apply_delete_for_wal_reset_bug()` —
a **no-wait** path that never blocks. Upgrading SQLite to ≥3.50.7 /
≥3.51.3 (cf. `17bf3c8283`, "repair vulnerable managed SQLite builds")
would have disarmed that gate and re-armed the freeze:
`resolve_journal_mode()` defaults to `wal`
(`hermes_cli/config_defaults.py:17`), so every surviving caller would have
attempted a blocking `PRAGMA journal_mode=WAL` per connect. Fixes #10
and #11 defuse that time bomb.

## Root cause analysis

### The journal mode contention pattern

```
Thread A (delivery_ledger.mark_delivered):
  _connect() → sqlite3.connect(state.db)
  _initialize_schema() → apply_wal_with_fallback()
    → PRAGMA journal_mode=WAL
    → SQLite needs EXCLUSIVE lock to switch modes
    → BLOCKED: other connections hold SHARED/RESERVED locks

Thread B (any SessionDB operation):
  _execute_write → BEGIN IMMEDIATE
    → BLOCKED: Thread A holds EXCLUSIVE lock attempt
```

In DELETE mode, switching to WAL is a heavyweight operation requiring
exclusive access. Every `delivery_ledger` write, every cron execution
record, and every verification evidence write triggered this.

### Why it didn't manifest before the upstream merge

Before the merge, our `apply_wal_with_fallback` override forced DELETE
mode and returned immediately without trying WAL. The upstream merge
brought the full upstream resolver back, which:

1. Checks `resolve_journal_mode()` config (defaults to WAL)
2. Tries `PRAGMA journal_mode=WAL`
3. Falls back to DELETE only on failure

Since `resolve_journal_mode()` returns `wal` by default, every
`_connect()` call in the three new modules attempted to switch to WAL.

### The three offending modules

| Module | DB file | Trigger frequency | Fix commit |
|--------|---------|-------------------|------------|
| `gateway/delivery_ledger.py` | state.db | every Discord reply | `1e9ee0fbf6` |
| `cron/executions.py` | cron/executions.db | every cron tick | `2c14d50285` |
| `agent/verification_evidence.py` | verification_evidence.db | every tool verification | `e45d8a2c19` |

## Verification

```
$ grep -rn "apply_wal_with_fallback" --include="*.py" .   # tests excluded
hermes_state.py:1005: def apply_wal_with_fallback(...)
hermes_state.py:2844:     apply_wal_with_fallback(self._conn, ...)  # SessionDB.__init__
```

Only `hermes_state.py` (internal to SessionDB, which honors the configured
`database.journal_mode: delete`) calls it now. **No external module
attempts journal-mode switching.**

> **Correction (2026-08-13):** an earlier revision of this section showed
> the same grep output *before* fix #10, but that output did not match the
> tree — seven external callers still existed at the time
> (async_delegation, notepad, kanban_db ×2, api_server, holographic,
> discord recovery, projects_db). The grep above was re-run after fix #10
> and reflects the true current state.

Test evidence after fixes #10–#11 (conda python, pytest 9.0.3):

- `tests/hermes_cli/test_kanban_db.py` (full file) +
  `test_journal_mode_config.py` + `test_doctor_journal_modes.py`:
  73 passed, 1 skipped
- `test_projects_db.py` + `test_delegate_cascade_49148.py`: 10 passed
- `tests/cron/test_notepad.py` + delegation/cronjob suites: 43 passed
- `test_api_server_*.py` (3 files): 46 passed
- discord gateway suites: 10 passed

## Lessons

1. **Upstream merge must audit all `apply_wal_with_fallback` callers** —
   not just hermes_state.py. The function is exported and used by
   delivery_ledger, cron/executions, verification_evidence, kanban_db,
   and potentially others.

2. **The resolver respects config, but defaults to WAL** — after merge,
   `resolve_journal_mode()` returns `wal` unless `database.journal_mode`
   is explicitly set in config.yaml. Now set to `delete` in both the
   default home and the active profile config (fix #11). Note the gateway
   resolves config against the **profile** home — setting only
   `~/.hermes/config.yaml` leaves the profile unprotected.

3. **Independent SQLite modules are a recurring problem** — each new
   upstream module that creates its own `sqlite3.connect` + schema init
   is a potential lock contention source. The shared SessionDB singleton
   only covers `state.db`; these modules have their own DB files.

4. **Incidental protection is not a fix** — the surviving callers were
   harmless only because this host's SQLite 3.45.1 trips the WAL-reset-bug
   gate (no-wait DELETE path). A runtime upgrade to ≥3.50.7/≥3.51.3 would
   have re-armed the freeze. And when verifying "no more callers",
   actually re-run the grep against the tree — the earlier Verification
   output was written from intent, not from reality.

## Recommended follow-up — DONE (2026-08-13)

~~Set `database.journal_mode: delete` in config.yaml so even if a future
upstream module calls `apply_wal_with_fallback`, the resolver returns
`delete` and skips the WAL attempt entirely.~~ Applied to both
`~/.hermes/config.yaml` and `~/.hermes/profiles/zhihui/config.yaml`
(fix #11). Caveat: `hermes_cli/config_defaults.py` still ships
`"journal_mode": "wal"` as the factory default — newly provisioned
profiles inherit it, so repeat fix #11 (or change the shipped default)
when creating a new profile.
