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


def test_does_not_flag_dict_get_in_async():
    src = textwrap.dedent("""
        async def handler():
            return some_dict.get("key")
    """)
    findings = scan_source(src, filename="gateway/x.py")
    assert findings == []


def test_does_not_flag_asyncio_get_in_async():
    src = textwrap.dedent("""
        async def handler():
            loop = asyncio.get_running_loop()
            event.set()
    """)
    findings = scan_source(src, filename="gateway/x.py")
    assert findings == []


def test_does_not_flag_unwrap_when_to_thread_wrapped():
    src = textwrap.dedent("""
        async def handler():
            raw_db = getattr(session_db, "_db", session_db)
            return await asyncio.to_thread(raw_db.get_session, "x")
    """)
    findings = scan_source(src, filename="gateway/x.py")
    assert findings == []


def test_still_flags_bare_sync_db_call():
    src = textwrap.dedent("""
        async def handler():
            return session_db.get_session("x")
    """)
    findings = scan_source(src, filename="gateway/x.py")
    assert any("async-context sync SessionDB call" in f.detail for f in findings)
