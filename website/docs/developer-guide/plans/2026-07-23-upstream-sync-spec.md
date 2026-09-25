# Spec: Sync Fork with Upstream (main → 2046 commits behind)

> **Date**: 2026-07-23
> **Merge base**: `5e849942c` (upstream/main at sync point 2026-07-11)
> **Current divergence**: 40 local commits, 2046 upstream commits
> **Conflicts**: 3 files (`.gitignore`, `cron/scheduler.py`, `hermes_state.py`)

---

## Background

Our fork diverged from upstream on 2026-07-11 after PR #61842/#61847
(session store lock I/O) was merged. Since then we made 40 local commits
fixing gateway freeze issues (SQLite lock contention, WAL checkpoint,
missing self._lock, schema init cache, cron SessionDB sharing, etc).

Upstream meanwhile made 2046 commits including a major schema migration
(v23: external-content FTS + tool-row-free trigram), CJK bigram FTS,
slow-query logging, and two fixes that overlap with ours:
- `c2a3b9ce5` PASSIVE checkpoint (same conclusion as our fix)
- `0695a6bce` periodic FTS5 segment merge

---

## Conflict inventory (3 files)

### 1. `hermes_state.py` — highest risk

Our local changes:
- `timeout=30.0` (upstream may still be `1.0` or changed differently)
- `_init_schema` per-db_path cache (`_initialized_dbs`)
- `self._lock` added to `list_pending_handoffs`, `get_handoff_state`,
  `get_compression_lock_holder`
- `_try_wal_checkpoint` restored `self._lock` + PASSIVE mode
- `_CHECKPOINT_EVERY_N_WRITES` tuning

Upstream changes to same areas:
- Schema v23 (`SCHEMA_SQL` rewrite — external-content FTS, new tables)
- `c2a3b9ce5`: `_try_wal_checkpoint` PASSIVE mode (same fix)
- `0695a6bce`: `_try_optimize_fts` wired into write path
- `9acc4b47f`: `_init_schema` FTS5 table rewrite + column changes
- Multiple FTS corruption self-heal commits

**Strategy**: Take upstream as base, re-apply our non-overlapping fixes:
- Keep `_initialized_dbs` cache (upstream doesn't have it)
- Keep `self._lock` on the 3 methods (upstream may or may not have it)
- Check if upstream already has `timeout=30.0` — if not, re-apply
- Check if upstream already has PASSIVE checkpoint — if yes, our fix is redundant

### 2. `cron/scheduler.py` — medium risk

Our local changes:
- `_get_shared_session_db()` singleton
- `_close_shared_session_db()` on shutdown
- `run_job` uses shared SessionDB instead of per-tick `SessionDB()`
- Removed per-tick `_session_db.close()`

Upstream changes:
- Cron delivery threading (Slack thread origin, in_channel delivery)
- Various cron fix/regression commits

**Strategy**: Keep our shared SessionDB changes, resolve upstream delivery
changes. Likely no semantic conflict — different code paths.

### 3. `.gitignore` — trivial

Our local additions (graphify-out, .worktrees) vs upstream changes.
**Strategy**: Manual merge of both sets of ignore rules.

---

## Auto-merged files (no conflict, but need verification)

- `gateway/run.py` — our WAL checkpoint housekeeping + upstream delivery changes
- `gateway/session.py` — our AsyncSessionStore boundary + upstream fixes
- `gateway/channel_directory.py` — our cached read-only SessionDB
- `gateway/slash_commands.py` — our to_thread offload + upstream changes

---

## Approach

### Option A: Merge upstream into main (recommended)

```bash
git checkout main
git merge upstream/main
# Resolve 3 conflicts manually
# Test
git commit
git push origin main
```

**Pros**: Preserves our 40 commits' history. Simple.
**Cons**: Large merge commit. Some of our fixes are now redundant with
upstream's versions.

### Option B: Rebase our fixes onto upstream

```bash
git checkout -b sync-rebase upstream/main
git cherry-pick <our-non-redundant-commits>
# Drop commits that upstream already fixed
```

**Pros**: Clean linear history. No merge commit.
**Cons**: Tedious (40 commits, need to identify which are redundant).
Risky if commits depend on each other.

### Option C: Fresh start from upstream + re-apply only needed fixes

```bash
git checkout -b sync-fresh upstream/main
# Manually re-apply: _init_schema cache, self._lock, shared SessionDB,
# channel directory cache, timeout=30
```

**Pros**: Cleanest. Only carries forward fixes upstream doesn't have.
**Cons**: Loses our git history for these fixes.

### Recommendation

**Option A** (merge). It's the safest and preserves history. After the
merge, identify redundant fixes (where upstream has equivalent or better)
and note them for cleanup in a follow-up.

---

## Post-merge checklist

1. **Schema v23 migration**: upstream rewrote FTS tables. Our `_init_schema`
   cache keyed on `db_path` — need to verify it works with the new schema
   (cache might skip v23 migration on first connect if a stale cache entry
   exists). **Mitigation**: clear `_initialized_dbs` on merge.

2. **WAL checkpoint**: verify upstream's PASSIVE checkpoint (`c2a3b9ce5`)
   coexists with our housekeeping PASSIVE checkpoint. If both exist, no
   harm — just redundant. If upstream removed `_try_wal_checkpoint` from
   the write path, our housekeeping call may need updating.

3. **Timeout**: verify `timeout=30.0` survived or was already in upstream.
   Upstream issue #57921 PR #58003 may have landed.

4. **`self._lock` on 3 methods**: verify upstream hasn't already added these
   (would cause duplicate `with self._lock:` nesting — not harmful but ugly).

5. **FTS5 segment merge**: after merge, run `hermes sessions optimize` to
   merge accumulated FTS segments (34k+ segments → 1).

6. **Test suite**: run `pytest tests/test_hermes_state.py tests/gateway/ tests/cron/`
   to verify no regressions.

---

## Out-of-scope

- Cleaning up redundant local commits (e.g., `19378ed8f` that we then
  reverted by `1c9ef1a7d`). Leave for history; can squash later.
- Submitting our unique fixes (schema init cache, shared SessionDB) as
  upstream PRs. Defer until merge is stable.
