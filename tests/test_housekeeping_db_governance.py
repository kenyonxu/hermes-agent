"""Root-cause tests: WAL checkpoint strategy + housekeeping DB governance.

state.db bloated to 1.4GB / 145983 messages because SessionDB had
prune_sessions/archive_sessions/vacuum but nothing ever scheduled them. These
tests pin both the WAL checkpoint strategy (PASSIVE hot path vs TRUNCATE
housekeeping) and the housekeeping DB-governance tick so the scheduler cannot
silently regress.
"""
import threading
from unittest.mock import MagicMock, patch


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


def _drive_housekeeping_once(session_db):
    """Run the housekeeping loop far enough for the DB-governance tick to fire.

    DB_GOV_EVERY == 60, and each loop iteration bumps tick_count once, so we let
    it reach a multiple of 60 before stopping. interval is tiny (0.01s) so the
    ~60 iterations complete in well under a second.
    """
    import gateway.run as run_mod

    stop = threading.Event()
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


def test_housekeeping_invokes_auto_prune_when_enabled():
    """With sessions.auto_prune enabled, the DB-governance tick must delegate to
    the config-gated maybe_auto_prune_and_vacuum facility."""
    raw_db = MagicMock()
    raw_db.maybe_auto_prune_and_vacuum = MagicMock(
        return_value={"skipped": False, "pruned": 3, "vacuumed": True}
    )
    raw_db._try_wal_checkpoint = MagicMock()
    session_db = MagicMock()
    session_db._db = raw_db

    cfg = {"sessions": {"auto_prune": True, "retention_days": 90}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        _drive_housekeeping_once(session_db)

    raw_db.maybe_auto_prune_and_vacuum.assert_called()


def test_housekeeping_skips_prune_when_disabled():
    """With sessions.auto_prune disabled (default), the DB-governance tick must
    NOT touch the DB — no silent archive/prune/vacuum."""
    raw_db = MagicMock()
    raw_db.maybe_auto_prune_and_vacuum = MagicMock()
    raw_db._try_wal_checkpoint = MagicMock()
    session_db = MagicMock()
    session_db._db = raw_db

    cfg = {"sessions": {"auto_prune": False}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        _drive_housekeeping_once(session_db)

    raw_db.maybe_auto_prune_and_vacuum.assert_not_called()

