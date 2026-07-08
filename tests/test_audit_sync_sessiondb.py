"""Scanner self-tests: AST detection of sync SessionDB calls in async contexts + unwrap anti-pattern."""
import textwrap

from scripts.audit_sync_sessiondb import scan_source


def test_flags_sync_db_call_in_async_function():
    src = textwrap.dedent("""
        async def handler():
            return db.get_session("x")
    """)
    findings = scan_source(src, filename="gateway/x.py")
    assert any("async-context sync SessionDB call" in f.detail for f in findings)


def test_does_not_flag_sync_call_in_sync_function():
    src = textwrap.dedent("""
        def init():
            return db.get_session("x")
    """)
    findings = scan_source(src, filename="gateway/x.py")
    assert findings == []


def test_flags_getattr_unwrap_of_async_db():
    src = textwrap.dedent("""
        async def handler():
            raw = getattr(session_db, "_db", session_db)
            return raw.get_session("x")
    """)
    findings = scan_source(src, filename="gateway/x.py")
    assert any("AsyncSessionDB unwrap" in f.detail for f in findings)


def test_respects_safe_marker():
    src = textwrap.dedent("""
        async def handler():
            return db.get_session("x")  # SYNC_SESSIONDB_SAFE: startup-only, loop not running
    """)
    findings = scan_source(src, filename="gateway/x.py")
    assert findings == []
