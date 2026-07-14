"""Tests for the shared SessionDB singleton in the cron scheduler.

Each cron job tick previously created a fresh SessionDB(), running DDL
schema checks on every call. These tests verify the singleton is created
once and reused across calls.
"""
import threading
from unittest.mock import patch, MagicMock

import pytest


class TestSharedSessionDB:
    """_get_shared_session_db returns the same instance on every call."""

    def test_returns_same_instance_across_calls(self):
        import cron.scheduler as sched
        sched._shared_session_db = None
        try:
            db1 = sched._get_shared_session_db()
            db2 = sched._get_shared_session_db()
            assert db1 is not None
            assert db1 is db2
        finally:
            sched._close_shared_session_db()

    def test_returns_none_on_init_failure(self):
        """If SessionDB() raises, return None (matching old behavior)."""
        import cron.scheduler as sched
        sched._shared_session_db = None
        with patch("hermes_state.SessionDB", side_effect=Exception("no db")):
            db = sched._get_shared_session_db()
            assert db is None

    def test_close_resets_singleton(self):
        """After _close_shared_session_db, next call creates a new instance."""
        import cron.scheduler as sched
        sched._shared_session_db = None
        try:
            db1 = sched._get_shared_session_db()
            sched._close_shared_session_db()
            db2 = sched._get_shared_session_db()
            assert db1 is not db2
        finally:
            sched._close_shared_session_db()

    def test_thread_safe_lazy_init(self):
        """Concurrent callers all get the same instance."""
        import cron.scheduler as sched
        sched._shared_session_db = None
        results = []
        barrier = threading.Barrier(5)

        def get_db():
            barrier.wait()
            results.append(sched._get_shared_session_db())

        threads = [threading.Thread(target=get_db) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert all(r is results[0] for r in results)
            assert len(set(id(r) for r in results)) == 1
        finally:
            sched._close_shared_session_db()
