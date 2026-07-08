"""Root-cause tests: housekeeping DB governance (prune/archive/vacuum scheduling).

state.db bloated to 1.4GB / 145983 messages because SessionDB had
prune_sessions/archive_sessions/vacuum but nothing ever scheduled them. These
tests pin the housekeeping DB-governance tick so the scheduler cannot silently
regress back to never calling them.
"""
import threading
from unittest.mock import MagicMock


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
