"""Root-cause tests: WAL checkpoint strategy + housekeeping DB governance.

state.db bloated to 1.4GB / 145983 messages because SessionDB had
prune_sessions/archive_sessions/vacuum but nothing ever scheduled them. These
tests pin both the WAL checkpoint strategy (PASSIVE hot path vs TRUNCATE
housekeeping) and the housekeeping DB-governance tick so the scheduler cannot
silently regress.
"""
import threading
from unittest.mock import MagicMock


def test_try_wal_checkpoint_default_is_passive():
    """Hot path defaults to PASSIVE (non-blocking), NOT TRUNCATE."""
    import hermes_state
    db = hermes_state.SessionDB.__new__(hermes_state.SessionDB)
    db._lock = threading.Lock()
    db._conn = MagicMock()
    captured = {}
    def fake_execute(sql, *a, **kw):
        captured["sql"] = sql
        class _Res:
            def fetchone(self): return [0, 0, 0]
        return _Res()
    db._conn.execute = fake_execute
    db._try_wal_checkpoint()
    assert "PASSIVE" in captured["sql"], "Hot path checkpoint MUST default to PASSIVE"
    assert "TRUNCATE" not in captured["sql"]


def test_try_wal_checkpoint_truncate_opt_in():
    import hermes_state
    db = hermes_state.SessionDB.__new__(hermes_state.SessionDB)
    db._lock = threading.Lock()
    db._conn = MagicMock()
    captured = {}
    def fake_execute(sql, *a, **kw):
        captured["sql"] = sql
        class _Res:
            def fetchone(self): return [0, 0, 0]
        return _Res()
    db._conn.execute = fake_execute
    db._try_wal_checkpoint(truncate=True)
    assert "TRUNCATE" in captured["sql"]


def test_housekeeping_invokes_prune_and_archive():
    """Housekeeping DB prune tick must invoke prune_sessions + archive_sessions + vacuum."""
    import gateway.run as run_mod

    stop = threading.Event()
    raw_db = MagicMock()
    raw_db.prune_sessions = MagicMock(return_value=0)
    raw_db.archive_sessions = MagicMock(return_value=0)
    raw_db.vacuum = MagicMock(return_value=0)
    raw_db._try_wal_checkpoint = MagicMock()
    session_db = MagicMock()
    session_db._db = raw_db

    # Drive the loop far enough that the DB-governance tick (DB_GOV_EVERY == 60)
    # actually fires. Each loop iteration bumps tick_count once, so we must let
    # it reach a multiple of 60 before stopping. interval is tiny (0.01s) so the
    # ~60 iterations complete in well under a second.
    counter = {"n": 0}
    orig_wait = stop.wait

    def patched_wait(timeout):
        counter["n"] += 1
        if counter["n"] >= 60:
            stop.set()
        orig_wait(timeout)

    stop.wait = patched_wait

    run_mod._start_gateway_housekeeping(
        stop, adapters=None, loop=None, interval=0.01, session_db=session_db
    )

    raw_db.archive_sessions.assert_called()
    raw_db.prune_sessions.assert_called()
