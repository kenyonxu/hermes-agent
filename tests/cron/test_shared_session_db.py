"""Tests for the shared SessionDB singleton.

These tests verify the get_shared_session_db singleton is created once and
reused across calls, and is thread-safe under concurrent access.
"""
import threading
from unittest.mock import patch, MagicMock

import pytest


class TestSharedSessionDB:
    """get_shared_session_db returns the same instance on every call."""

    def test_returns_same_instance_across_calls(self, tmp_path, monkeypatch):
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "shared.db")
        hs._shared_writer = None
        try:
            db1 = hs.get_shared_session_db()
            db2 = hs.get_shared_session_db()
            assert db1 is not None
            assert db1 is db2
        finally:
            hs.close_shared_session_db()
            hs._shared_writer = None

    def test_returns_none_on_init_failure(self, tmp_path, monkeypatch):
        """If SessionDB() raises, return None (matching old behavior)."""
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "shared.db")
        hs._shared_writer = None
        with patch("hermes_state.SessionDB", side_effect=Exception("no db")):
            db = hs.get_shared_session_db()
            assert db is None

    def test_close_resets_singleton(self, tmp_path, monkeypatch):
        """After close_shared_session_db, next call creates a new instance."""
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "shared.db")
        hs._shared_writer = None
        try:
            db1 = hs.get_shared_session_db()
            hs.close_shared_session_db()
            db2 = hs.get_shared_session_db()
            assert db1 is not db2
        finally:
            hs.close_shared_session_db()
            hs._shared_writer = None

    def test_thread_safe_lazy_init(self, tmp_path, monkeypatch):
        """Concurrent callers all get the same instance."""
        import hermes_state as hs
        monkeypatch.setattr(hs, "DEFAULT_DB_PATH", tmp_path / "shared.db")
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
            assert len(set(id(r) for r in results)) == 1
        finally:
            hs.close_shared_session_db()
            hs._shared_writer = None
