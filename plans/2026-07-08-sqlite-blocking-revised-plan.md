# SQLite 事件循环阻塞 — 修订执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 彻底消除 Gateway 中同步 SessionDB 调用与 WAL checkpoint 对 asyncio 事件循环的阻塞,并建立回归门禁。

**Architecture:** 三层修复 ——(1)止血:把热路径上残留的同步 DB 调用整体 async 化;(2)治标工具:AST 扫描器 + CI 门禁,防止新阻塞点回流;(3)治本:把 WAL checkpoint 从写热路径剥离到 housekeeping 独立线程,并补齐缺失的 DB prune/archive 调度(state.db 膨胀到 1.4GB 的根因)。

**Tech Stack:** Python 3 asyncio, SQLite (WAL), hermes-agent gateway, pytest / pytest-asyncio

---

## 背景:为何修订原计划

原计划(`plans/sqlite-event-loop-blocking-plan.md` + `plans/sqlite-event-loop-blocking-solution.md` + `docs/sqlite-event-loop-blocking.md`)方向正确(根因 = 同步 DB × asyncio 边界无约束),但存在 6 个实质性缺陷,本计划逐项修正:

| # | 原计划缺陷 | 本计划修正 |
|---|-----------|-----------|
| 1 | P0 照搬会编译失败:`_session_has_compression_in_flight` 改 `await` 但未声明签名变 async,也未改 5278 调用点 | Task 1:整体 async 化 + 调用点 await 重构 |
| 2 | P0 漏掉阻塞源 A(`_ensure_loaded_locked` 同步锁+JSON 读),只修阻塞源 B | Task 1:两个阻塞源都 offload |
| 3 | P2a "AsyncSessionDB 默认化" 对 10 处 `getattr(x,"_db",x)` unwrap 反模式无效 | Task 2:扫描器加 unwrap 专项规则;Task 1 示范正确修法 |
| 4 | P1 扫描器用正则字符串匹配,误报漏报高 | Task 2:AST 重写 |
| 5 | P2 全是接口层 async 化,未触及 state.db 1.4GB 根因(WAL checkpoint 在写热路径 + 无 prune 调度) | Task 3 + Task 4:治本 |
| 6 | 无事件循环阻塞回归测试,修了 #5 无法防 #6 | Task 5:回归压测门禁 |

**P2 收窄声明**:原 P2b(SessionStore async 化)、P2c(sessions.json 异步化)推迟到上游 #23717(Pluggable SessionDB Provider RFC)定调,避免与未来上游改动大面积冲突。本计划只保留 P2a(消除 unwrap 反模式)+ 代码规范 + CI 门禁(Task 2 + Task 6)。

---

## 文件结构

| 文件 | 职责 | 任务 |
|------|------|------|
| `gateway/run.py:5026-5054` | `_session_has_compression_in_flight` 方法体 | Task 1 改 |
| `gateway/run.py:5276-5279` | 上述方法的调用点(`_handle_active_session_busy_message`) | Task 1 改 |
| `tests/gateway/test_compression_in_flight_check.py` | Task 1 行为测试(新建) | Task 1 |
| `scripts/audit_sync_sessiondb.py` | AST 扫描器(新建) | Task 2 |
| `tests/test_audit_sync_sessiondb.py` | 扫描器自测(新建) | Task 2 |
| `hermes_state.py:1171-1172` | 写热路径 WAL checkpoint 触发点 | Task 3 改 |
| `hermes_state.py:1194-1223` | `_try_wal_checkpoint` 加 mode 参数 | Task 3 改 |
| `gateway/run.py:19676-19762` | `_start_gateway_housekeeping` 加 WAL truncate + DB prune tick | Task 3 + Task 4 改 |
| `gateway/run.py:20292` 附近 | housekeeping 启动处传 `session_db` | Task 3 改 |
| `tests/test_housekeeping_db_governance.py` | housekeeping DB 治理测试(新建) | Task 3 + Task 4 |
| `tests/gateway/test_event_loop_blocking_regression.py` | 事件循环阻塞回归测试(新建) | Task 5 |
| `.github/workflows/ci.yml` | 扫描器门禁步骤 | Task 6 改 |
| `docs/sync-sessiondb-policy.md` | 同步 SessionDB 使用规范(新建) | Task 6 |

---

## Task 1: P0 修正 — `_session_has_compression_in_flight` async 化(双阻塞源)

**为什么**:这是消息热路径上的已知阻塞点(#5)。原方法是 `def`(同步),内部有两个阻塞源:`session_store._lock` + `_ensure_loaded_locked()`(JSON 读),以及 `db.get_compression_lock_holder()`(SQLite SELECT)。两者都必须 offload 到线程池,方法签名必须变 `async`,调用点 5278 必须加 `await`。

**Files:**
- Modify: `gateway/run.py:5026-5054`
- Modify: `gateway/run.py:5276-5279`
- Test: `tests/gateway/test_compression_in_flight_check.py`(新建)

- [ ] **Step 1: 确认 pytest-asyncio 可用**

Run: `python -c "import pytest_asyncio; print(pytest_asyncio.__version__)"`
Expected: 打印版本号。若报 ModuleNotFoundError,执行 `pip install pytest-asyncio` 并在 `pytest.ini`/`pyproject.toml` 配置 `asyncio_mode = auto`(参照仓库内现有 async 测试的配置)。

- [ ] **Step 2: 写失败测试 — 方法签名是协程**

Create `tests/gateway/test_compression_in_flight_check.py`:

```python
"""#5 回归:_session_has_compression_in_flight 的两个阻塞源必须 offload 到线程池。"""
import inspect
import threading
from unittest.mock import MagicMock

import pytest


def _make_runner(holder_value=None, record_thread=False, thread_sink=None):
    """构造最小 GatewayRunner,只装该方法依赖的属性。"""
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)

    store = MagicMock()
    store._lock = threading.Lock()
    store._loaded = True
    store._entries = {"k": MagicMock(session_id="sess-123")}
    store._ensure_loaded_locked = lambda: None
    runner.session_store = store

    raw_db = MagicMock()
    if record_thread and thread_sink is not None:
        def _holder(sid):
            thread_sink["thread"] = threading.get_ident()
            return holder_value
        raw_db.get_compression_lock_holder = _holder
    else:
        raw_db.get_compression_lock_holder = MagicMock(return_value=holder_value)

    session_db = MagicMock()
    session_db._db = raw_db  # 模拟 AsyncSessionDB 透传到底层同步 db
    runner._session_db = session_db
    return runner


def test_method_is_coroutine():
    from gateway.run import GatewayRunner
    assert inspect.iscoroutinefunction(
        GatewayRunner._session_has_compression_in_flight
    ), "#5: 方法必须 async,阻塞调用已 offload"
```

- [ ] **Step 3: 运行,确认失败**

Run: `python -m pytest tests/gateway/test_compression_in_flight_check.py::test_method_is_coroutine -v`
Expected: FAIL — `AssertionError`(`def` 非 `async def`)。

- [ ] **Step 4: 写其余失败测试 — 返回值 + 线程 offload**

追加到 `tests/gateway/test_compression_in_flight_check.py`:

```python
@pytest.mark.asyncio
async def test_returns_true_when_lock_held():
    runner = _make_runner(holder_value="agent-1")
    assert await runner._session_has_compression_in_flight("k") is True


@pytest.mark.asyncio
async def test_returns_false_when_no_lock():
    runner = _make_runner(holder_value=None)
    assert await runner._session_has_compression_in_flight("k") is False


@pytest.mark.asyncio
async def test_returns_false_when_no_session_store():
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = None
    runner._session_db = MagicMock()
    assert await runner._session_has_compression_in_flight("k") is False


@pytest.mark.asyncio
async def test_db_call_runs_off_event_loop():
    """回归核心:get_compression_lock_holder 必须在非事件循环线程执行。"""
    sink = {}
    runner = _make_runner(holder_value="agent-1", record_thread=True, thread_sink=sink)
    loop_thread = threading.get_ident()
    await runner._session_has_compression_in_flight("k")
    assert "thread" in sink, "底层 db.get_compression_lock_holder 未被调用"
    assert sink["thread"] != loop_thread, (
        "DB 调用仍在事件循环线程 — #5 未修复(to_thread 未生效)"
    )
```

- [ ] **Step 5: 运行全部新测试,确认全部失败**

Run: `python -m pytest tests/gateway/test_compression_in_flight_check.py -v`
Expected: 4 个 async 测试 FAIL(`TypeError: object bool can't be used in 'await' expression` 或类似 —— 因为方法仍是同步返回 bool),签名测试若 Step 3 后未改仍 FAIL。

- [ ] **Step 6: 实现 — 方法改 async + 抽同步辅助 + 双 offload**

Edit `gateway/run.py:5026-5054`,整体替换方法体为:

```python
    async def _session_has_compression_in_flight(self, session_key: str) -> bool:
        """Return True when a compression lock is held for this session's id.

        Context compression is interrupt-protected (#23975) but gateway
        ``interrupt`` busy-input mode can still start a follow-up turn against
        the pre-rotation parent while compression is mid-flight, producing
        orphaned compression siblings (#56391). Callers demote interrupt to
        queue when this returns True.

        Both blocking sources — the ``session_store`` lock + JSON load, and the
        SQLite ``get_compression_lock_holder`` SELECT — are offloaded to a
        worker thread so a large state.db never freezes the event loop (#5).
        """
        session_store = getattr(self, "session_store", None)
        if not session_key or session_store is None:
            return False
        try:
            session_id = await asyncio.to_thread(
                self._lookup_session_id_under_store_lock, session_store, session_key
            )
        except Exception:
            return False
        if not session_id:
            return False
        session_db = getattr(self, "_session_db", None)
        if session_db is None:
            return False
        raw_db = getattr(session_db, "_db", session_db)
        try:
            holder = await asyncio.to_thread(
                raw_db.get_compression_lock_holder, str(session_id)
            )
            return bool(holder)
        except Exception:
            return False

    @staticmethod
    def _lookup_session_id_under_store_lock(session_store, session_key: str):
        """Sync helper run in the thread pool: read session_id under the store lock.

        # noqa: SLF001 — intentional private access; runs off the event loop.
        """
        with session_store._lock:  # noqa: SLF001
            session_store._ensure_loaded_locked()  # noqa: SLF001
            entry = session_store._entries.get(session_key)  # noqa: SLF001
        return getattr(entry, "session_id", None) if entry is not None else None
```

确认文件顶部已 `import asyncio`(run.py 作为 asyncio 应用必然已导入;若未导入则补 `import asyncio`)。

- [ ] **Step 7: 改调用点 5278 加 await**

Edit `gateway/run.py:5276-5279`:

```python
        demoted_for_compression = (
            effective_mode == "interrupt"
            and await self._session_has_compression_in_flight(session_key)
        )
```

`_handle_active_session_busy_message` 已是 `async def`(run.py:5103),`and await ...` 在布尔表达式合法。

- [ ] **Step 8: 运行新测试,确认全绿**

Run: `python -m pytest tests/gateway/test_compression_in_flight_check.py -v`
Expected: 5 PASS。

- [ ] **Step 9: 运行既有相关测试,确认无回归**

Run: `python -m pytest tests/gateway/test_compression_interrupt_demotion_56391.py tests/gateway/test_busy_session_ack.py tests/gateway/test_internal_event_never_interrupts_busy_session.py -v`
Expected: 全绿。

- [ ] **Step 10: 语法检查**

Run: `python -c "import ast; ast.parse(open('gateway/run.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 11: Commit**

```bash
git add gateway/run.py tests/gateway/test_compression_in_flight_check.py
git commit -m "fix(gateway): offload both blocking sources in compression-in-flight check (#5)

The sync _session_has_compression_in_flight sat on the message hot path
and blocked the event loop twice: under session_store._lock during
_ensure_loaded_locked (JSON read) and via db.get_compression_lock_holder
(SQLite SELECT). Async-ify the method and offload both sources via
asyncio.to_thread; await the call site in _handle_active_session_busy_message."
```

---

## Task 2: AST 扫描器 v2 + unwrap 专项规则

**为什么**:原计划的扫描器用正则字符串匹配,误报漏报高,无法做 CI 门禁。且 `getattr(x,"_db",x)` unwrap 反模式(全仓库 10+ 处)会让 AsyncSessionDB 默认化失效,正则扫描器识别不出。本任务用 AST 精确定位 async 函数体里的同步 SessionDB 调用,并专项标记 unwrap。

**Files:**
- Create: `scripts/audit_sync_sessiondb.py`
- Test: `tests/test_audit_sync_sessiondb.py`

- [ ] **Step 1: 写失败测试 — 扫描器识别 async 体内的同步 DB 调用**

Create `tests/test_audit_sync_sessiondb.py`:

```python
"""扫描器自测:AST 识别 async 体内的同步 SessionDB 调用 + unwrap 反模式。"""
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
```

- [ ] **Step 2: 运行,确认失败**

Run: `python -m pytest tests/test_audit_sync_sessiondb.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.audit_sync_sessiondb'`。

- [ ] **Step 3: 确认 scripts 目录可导入**

若仓库无 `scripts/__init__.py`,创建空文件 `scripts/__init__.py`(使 `from scripts.audit_sync_sessiondb import ...` 可用)。若已有则跳过。

- [ ] **Step 4: 实现扫描器**

Create `scripts/audit_sync_sessiondb.py`:

```python
#!/usr/bin/env python3
"""AST 扫描器:定位 async 函数体里的同步 SessionDB 调用 + unwrap 反模式。

用法:
    python scripts/audit_sync_sessiondb.py gateway/ cron/ tools/
    # 退出码 1 = 发现违规(用于 CI 门禁)

局限(v0):仅做单文件 AST,不做跨文件调用图可达性。定位是"async 函数直接体内
出现同步 DB 调用",这覆盖了绝大多数热路径阻塞点。跨函数可达性留待 v1。
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

# 已知 SessionDB 方法名前缀(同步 API)。命中即视为同步 DB 调用。
SYNC_METHOD_PREFIXES = (
    "get_", "list_", "find_", "create_", "end_", "open_", "record_", "bind_",
    "is_", "rewind_", "load_", "set_", "update_", "delete_", "append_",
    "prune_", "archive_", "vacuum", "rotate", "release_", "acquire_",
)
SAFE_MARKER = "SYNC_SESSIONDB_SAFE"


@dataclass
class Finding:
    filename: str
    lineno: int
    detail: str

    def __str__(self) -> str:
        return f"{self.filename}:{self.lineno} {self.detail}"


def _is_sync_db_call(node: ast.Call) -> bool:
    """node.func 是 x.method(...) 形式,且 method 名命中同步前缀。"""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    name = func.attr
    return any(name.startswith(p) or name == p.rstrip("_") for p in SYNC_METHOD_PREFIXES)


def _line_has_safe_marker(source_lines: List[str], lineno: int) -> bool:
    if 0 < lineno <= len(source_lines):
        return SAFE_MARKER in source_lines[lineno - 1]
    return False


def scan_source(source: str, filename: str = "<src>") -> List[Finding]:
    """扫描单段源码,返回所有违规 Finding。"""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    lines = source.splitlines()
    findings: List[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        is_async = isinstance(node, ast.AsyncFunctionDef)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if _line_has_safe_marker(lines, sub.lineno):
                continue
            # 专项规则:getattr(x, "_db", x) unwrap
            if (
                isinstance(sub.func, ast.Name)
                and sub.func.id == "getattr"
                and len(sub.args) >= 2
                and isinstance(sub.args[1], ast.Constant)
                and sub.args[1].value == "_db"
                and is_async
            ):
                findings.append(Finding(
                    filename, sub.lineno,
                    "AsyncSessionDB unwrap — AsyncSessionDB 默认化对此调用点无效,改用 async 门面",
                ))
                continue
            # async 体内的同步 DB 方法调用
            if is_async and _is_sync_db_call(sub):
                findings.append(Finding(
                    filename, sub.lineno,
                    f"async-context sync SessionDB call ({sub.func.attr})",  # type: ignore[attr-defined]
                ))
    return findings


def scan_path(root: Path) -> Iterable[Finding]:
    for py in root.rglob("*.py"):
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for f in scan_source(source, filename=str(py)):
            yield f


def main(argv: List[str]) -> int:
    roots = [Path(p) for p in argv[1:]] or [Path("gateway"), Path("cron"), Path("tools")]
    findings: List[Finding] = []
    for r in roots:
        if not r.exists():
            print(f"warn: {r} 不存在,跳过", file=sys.stderr)
            continue
        findings.extend(scan_path(r))
    for f in findings:
        print(f"RED {f}")
    print(f"\n共 {len(findings)} 处违规")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 5: 运行扫描器自测,确认全绿**

Run: `python -m pytest tests/test_audit_sync_sessiondb.py -v`
Expected: 4 PASS。

- [ ] **Step 6: 对 gateway/ 实跑,生成基线报告**

Run: `python scripts/audit_sync_sessiondb.py gateway/ > /tmp/audit-baseline.txt 2>&1; echo "exit=$?"`
Expected: `exit=1`,报告列出 async 体内的同步 DB 调用 + unwrap 点(含 run.py:5050 附近的 `_lookup_session_id_under_store_lock` 调用、以及其他 9 处 unwrap)。把这份报告作为后续修复的待办清单。

- [ ] **Step 7: Commit**

```bash
git add scripts/audit_sync_sessiondb.py scripts/__init__.py tests/test_audit_sync_sessiondb.py
git commit -m "feat(scripts): AST-based sync SessionDB audit scanner with unwrap detection"
```

---

## Task 3: WAL checkpoint 移出写热路径(治本之一)

**为什么**:hermes_state.py:1171-1172 在**写热路径**上每 N 次写触发一次 `PRAGMA wal_checkpoint(TRUNCATE)`,持 `self._lock`。state.db 1.4GB 时,TRUNCATE 分钟级,期间所有 DB 操作排队 → 事件循环级延迟。治本:热路径改用非阻塞 PASSIVE checkpoint(只回写可 checkpoint 页,不阻塞 writer),TRUNCATE 回收移到 housekeeping 独立线程周期执行。

**Files:**
- Modify: `hermes_state.py:1194-1223`(`_try_wal_checkpoint` 加 `truncate` 参数)
- Modify: `hermes_state.py:1171-1172`(热路径改 PASSIVE)
- Modify: `gateway/run.py:19676`(`_start_gateway_housekeeping` 签名加 `session_db`)
- Modify: `gateway/run.py:20292` 附近(housekeeping 启动传参)
- Test: `tests/test_housekeeping_db_governance.py`(新建)

- [ ] **Step 1: 确认 checkpoint 频率常量值**

Run: `grep -n "_CHECKPOINT_EVERY_N_WRITES" hermes_state.py`
Expected: 看到常量定义(预计为 50)与 1171 行的引用。记下这个值,Step 4 会用到。

- [ ] **Step 2: 写失败测试 — 热路径 checkpoint 不阻塞(PASSIVE)**

Create `tests/test_housekeeping_db_governance.py`:

```python
"""治本项测试:WAL checkpoint 策略 + housekeeping DB 治理。"""
import threading
from unittest.mock import MagicMock, patch


def test_try_wal_checkpoint_default_is_passive():
    """热路径默认 PASSIVE(非阻塞),不 TRUNCATE。"""
    import hermes_state
    db = hermes_state.SessionDB.__new__(hermes_state.SessionDB)
    db._lock = threading.Lock()
    db._conn = MagicMock()
    captured = {}
    def fake_execute(sql, *a, **kw):
        captured["sql"] = sql
        class _Row:
            def __getitem__(self, i): return 0
            def __iter__(self): return iter([0, 0, 0])
        class _Res:
            def fetchone(self): return [0, 0, 0]
        return _Res()
    db._conn.execute = fake_execute
    db._try_wal_checkpoint()  # 默认 truncate=False
    assert "PASSIVE" in captured["sql"], "热路径 checkpoint 必须默认 PASSIVE"
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
```

- [ ] **Step 3: 运行,确认失败**

Run: `python -m pytest tests/test_housekeeping_db_governance.py::test_try_wal_checkpoint_default_is_passive -v`
Expected: FAIL — `_try_wal_checkpoint()` 不接受参数,或硬编码 TRUNCATE。

- [ ] **Step 4: 实现 — `_try_wal_checkpoint` 加 mode 参数**

Edit `hermes_state.py:1194-1223`,把方法签名与 SQL 改为:

```python
    def _try_wal_checkpoint(self, truncate: bool = False) -> None:
        """Best-effort WAL checkpoint.  Never raises.

        Default PASSIVE: checkpoints as many frames as possible without
        blocking writers — safe for the write hot path. ``truncate=True``
        switches to TRUNCATE (recovers WAL high-water mark, may block writers
        briefly) and is intended for the off-loop housekeeping thread, NOT the
        write path.

        PASSIVE never truncates the WAL file on its own, so a separate periodic
        TRUNCATE (housekeeping) is required to keep the WAL bounded.
        """
        mode = "TRUNCATE" if truncate else "PASSIVE"
        try:
            with self._lock:
                result = self._conn.execute(
                    f"PRAGMA wal_checkpoint({mode})"
                ).fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint(%s): %d/%d pages checkpointed",
                        mode, result[2], result[1],
                    )
        except Exception:
            pass  # Best effort — never fatal.
```

- [ ] **Step 5: 运行测试,确认全绿**

Run: `python -m pytest tests/test_housekeeping_db_governance.py -v`
Expected: 2 PASS。

- [ ] **Step 6: 改 housekeeping 签名 + 加 WAL TRUNCATE tick**

Edit `gateway/run.py:19676` 起的 `_start_gateway_housekeeping`,签名加 `session_db=None`,并在 tick 循环里新增 WAL tick。修改如下:

签名行改为:

```python
def _start_gateway_housekeeping(stop_event: threading.Event, adapters=None, loop=None, interval: int = 60, session_db=None):
```

在常量块(`CURATOR_EVERY = 60` 之后)加:

```python
    WAL_CHECKPOINT_EVERY = 60   # ticks — hourly TRUNCATE off the event loop
```

在 `tick_count += 1` 之后、`CHANNEL_DIR_EVERY` 分支之前,加 WAL tick 分支:

```python
        if tick_count % WAL_CHECKPOINT_EVERY == 0 and session_db is not None:
            try:
                raw = getattr(session_db, "_db", session_db)
                raw._try_wal_checkpoint(truncate=True)
                logger.debug("Housekeeping: WAL TRUNCATE checkpoint done")
            except Exception as e:
                logger.debug("WAL checkpoint housekeeping error: %s", e)
```

- [ ] **Step 7: 改 housekeeping 启动处传参**

Run: `grep -n "_start_gateway_housekeeping" gateway/run.py`
找到启动 `threading.Thread(target=_start_gateway_housekeeping, ...)` 处(预计 run.py:20292 附近),在 `kwargs=` 里加 `"session_db": runner._session_db`。例如:

```python
    housekeeping_thread = threading.Thread(
        target=_start_gateway_housekeeping,
        args=(cron_stop,),
        kwargs={
            "adapters": runner.adapters,
            "loop": asyncio.get_running_loop(),
            "session_db": runner._session_db,
        },
        daemon=True,
        name="gateway-housekeeping",
    )
```

- [ ] **Step 8: 语法检查**

Run: `python -c "import ast; ast.parse(open('gateway/run.py').read()); ast.parse(open('hermes_state.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 9: 运行既有 SessionDB / WAL 相关测试**

Run: `python -m pytest tests/ -k "wal or checkpoint or session_db" -q`
Expected: 全绿。若存在依赖热路径 TRUNCATE 行为的测试失败,将其改为断言 PASSIVE(热路径)+ TRUNCATE(housekeeping)分离,而非恢复旧行为。

- [ ] **Step 10: Commit**

```bash
git add hermes_state.py gateway/run.py tests/test_housekeeping_db_governance.py
git commit -m "perf(state): move WAL TRUNCATE off write hot path to housekeeping

Write-path checkpoint now uses non-blocking PASSIVE; the blocking TRUNCATE
that recovers WAL high-water mark runs hourly in the off-loop housekeeping
thread. state.db at 1.4GB made the old in-loop TRUNCATE block the event
loop for seconds-to-minutes."
```

---

## Task 4: housekeeping 定期 prune/archive(治本之二 — state.db 膨胀根因)

**为什么**:`hermes_state.py` 已实现 `prune_sessions`/`archive_sessions`/`vacuum`,但全仓库无任何调度调用它们(cron 只清 cron 输出文件,housekeeping 只清 image/paste cache)。这是 state.db 累积到 1.4GB / 145983 条消息的直接原因。本任务在 housekeeping 加定期 prune + archive + vacuum tick(复用 Task 3 已传入的 `session_db`)。

**Files:**
- Modify: `gateway/run.py:19676` 起的 `_start_gateway_housekeeping`(新增 DB prune tick)
- Test: `tests/test_housekeeping_db_governance.py`(追加)

- [ ] **Step 1: 确认 prune_sessions / archive_sessions 签名**

Run: `grep -n "def prune_sessions\|def archive_sessions\|def vacuum" hermes_state.py`
Expected: 看到三个方法定义及参数。记下 `prune_sessions` 的 `older_than_days` 参数与 `archive_sessions` 的参数(Task 4 step 3 会用到)。若签名不同,后续代码以实际签名为准调整。

- [ ] **Step 2: 写失败测试 — housekeeping 调 prune/archive**

追加到 `tests/test_housekeeping_db_governance.py`:

```python
def test_housekeeping_invokes_prune_and_archive(monkeypatch):
    """housekeeping 的 DB prune tick 必须调用 prune_sessions + archive_sessions + vacuum。"""
    import gateway.run as run_mod

    calls = []
    raw_db = MagicMock()
    raw_db.prune_sessions = MagicMock(return_value=0)
    raw_db.archive_sessions = MagicMock(return_value=0)
    raw_db.vacuum = MagicMock(return_value=0)
    session_db = MagicMock()
    session_db._db = raw_db

    # 只跑一轮 tick:构造一个 stop_event 立即置位,但先放行一次 DB prune 分支
    stop = threading.Event()
    interval = 0.01
    orig_start = run_mod._start_gateway_housekeeping

    # monkeypatch 让循环只跑一次 DB_GOV 分支后退出
    counter = {"n": 0}
    def patched_stop_wait(timeout):
        counter["n"] += 1
        if counter["n"] >= 1:
            stop.set()
    monkeypatch.setattr(stop, "wait", patched_stop_wait)

    orig = run_mod._start_gateway_housekeeping
    # 直接调用,触发 tick_count % DB_GOV_EVERY == 0 分支(首 tick 即触发)
    orig(stop, adapters=None, loop=None, interval=interval, session_db=session_db)

    raw_db.archive_sessions.assert_called()
    raw_db.prune_sessions.assert_called()
```

- [ ] **Step 3: 运行,确认失败**

Run: `python -m pytest tests/test_housekeeping_db_governance.py::test_housekeeping_invokes_prune_and_archive -v`
Expected: FAIL — `archive_sessions` 未被调用(housekeeping 还没有 DB prune tick)。

- [ ] **Step 4: 实现 — housekeeping 加 DB 治理 tick**

Edit `gateway/run.py` 的 `_start_gateway_housekeeping`,在 Task 3 加的 `WAL_CHECKPOINT_EVERY` 常量后加:

```python
    DB_GOV_EVERY = 60          # ticks — hourly prune/archive/vacuum
    DB_RETENTION_DAYS = 90     # 对齐 hermes_state 既有 retention_days 默认值
```

在 WAL tick 分支之后,加 DB 治理 tick:

```python
        if tick_count % DB_GOV_EVERY == 0 and session_db is not None:
            try:
                raw = getattr(session_db, "_db", session_db)
                archived = raw.archive_sessions(older_than_days=DB_RETENTION_DAYS)
                pruned = raw.prune_sessions(older_than_days=DB_RETENTION_DAYS)
                if archived or pruned:
                    logger.info(
                        "Housekeeping DB gov: archived=%s pruned=%s (retention=%dd)",
                        archived, pruned, DB_RETENTION_DAYS,
                    )
                # vacuum 偶尔跑(每 24 个 DB gov tick ≈ 每天),回收磁盘
                if tick_count % (DB_GOV_EVERY * 24) == 0:
                    raw.vacuum()
                    logger.info("Housekeeping: vacuum done")
            except Exception as e:
                logger.debug("Housekeeping DB governance error: %s", e)
```

注意:若 Step 1 确认的 `prune_sessions`/`archive_sessions` 签名与上方不同(例如返回值或参数名),以实际签名为准修正调用。

- [ ] **Step 5: 运行测试,确认全绿**

Run: `python -m pytest tests/test_housekeeping_db_governance.py -v`
Expected: 3 PASS(含 Task 3 的 2 个 + 本任务 1 个)。

- [ ] **Step 6: 语法检查 + 集成冒烟**

Run: `python -c "import ast; ast.parse(open('gateway/run.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git add gateway/run.py tests/test_housekeeping_db_governance.py
git commit -m "feat(gateway): schedule periodic session prune/archive/vacuum in housekeeping

SessionDB already had prune_sessions/archive_sessions/vacuum but nothing
called them — root cause of state.db bloating to 1.4GB/145983 messages.
Housekeeping now runs hourly prune+archive (90d retention) and a daily
vacuum, all off the event loop."
```

---

## Task 5: 事件循环阻塞回归测试

**为什么**:Task 1 修了 #5,但 SessionDB 有 50+ 方法,#6、#7 迟早出现。需要一个横跨全仓库的回归门禁:慢 DB 操作不得阻塞并发事件循环任务(心跳)。这样未来任何在热路径引入同步 DB 调用的改动,都会被这个测试抓住。

**Files:**
- Create: `tests/gateway/test_event_loop_blocking_regression.py`

- [ ] **Step 1: 写回归测试**

Create `tests/gateway/test_event_loop_blocking_regression.py`:

```python
"""事件循环阻塞回归门禁。

模拟 #5 病根:一个慢 DB 操作(在线程池里 sleep)不得漂移并发心跳 tick 的间隔。
此测试锁定"热路径 DB 调用必须 offload"这一架构不变量,防 #6/#7 回流。
"""
import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_slow_db_call_does_not_block_concurrent_heartbeat():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    store = MagicMock()
    store._lock = threading.Lock()
    store._loaded = True
    store._entries = {"k": MagicMock(session_id="s")}
    store._ensure_loaded_locked = lambda: None
    runner.session_store = store

    slow_db = MagicMock()
    def slow_holder(sid):
        time.sleep(0.3)  # 模拟大 DB 下的阻塞
        return None
    session_db = MagicMock()
    session_db._db = slow_db
    slow_db.get_compression_lock_holder = slow_holder
    runner._session_db = session_db

    ticks: list[float] = []

    async def heartbeat(stop: asyncio.Event):
        loop = asyncio.get_event_loop()
        next_at = loop.time()
        while not stop.is_set():
            ticks.append(loop.time())
            next_at += 0.05
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    stop = asyncio.Event()
    hb = asyncio.create_task(heartbeat(stop))
    try:
        # 并发触发慢 DB 检查 —— 若未 offload,心跳会卡 0.3s
        await runner._session_has_compression_in_flight("k")
    finally:
        stop.set()
        await hb

    assert len(ticks) >= 3, "心跳 tick 数不足,测试不可信"
    intervals = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
    intervals.sort()
    p99 = intervals[max(0, int(len(intervals) * 0.99) - 1)]
    # DB 阻塞 0.3s;offload 正确时心跳 p99 应远低于 0.2s
    assert p99 < 0.2, (
        f"心跳间隔 p99={p99:.3f}s,事件循环被 DB 阻塞 —— 回归(热路径同步 DB 调用)"
    )
```

- [ ] **Step 2: 运行,确认通过(Task 1 已修,应绿)**

Run: `python -m pytest tests/gateway/test_event_loop_blocking_regression.py -v`
Expected: PASS。若 FAIL,说明 Task 1 的 offload 未生效,回到 Task 1 Step 6 检查 `to_thread`。

- [ ] **Step 3: 反向验证 — 临时去掉 await,确认测试能抓住回归**

临时把 `gateway/run.py` Task 1 Step 6 的 `holder = await asyncio.to_thread(...)` 改回 `holder = raw_db.get_compression_lock_holder(str(session_id))`(同步直调),运行同一测试:

Run: `python -m pytest tests/gateway/test_event_loop_blocking_regression.py -v`
Expected: FAIL(`p99 > 0.2`)。**确认测试有效后,还原 await 改动**。

- [ ] **Step 4: 确认还原后仍绿**

Run: `python -m pytest tests/gateway/test_event_loop_blocking_regression.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add tests/gateway/test_event_loop_blocking_regression.py
git commit -m "test(gateway): event-loop blocking regression gate for hot-path DB calls"
```

---

## Task 6: 规范文档 + CI 门禁 + P2 收窄声明

**为什么**:扫描器(Task 2)必须进 CI 才能防回流;同步 DB 调用的安全场景需要文档化(`# SYNC_SESSIONDB_SAFE` 标注规范);P2b/c 推迟决定需要记录,避免后人误以为漏做。

**Files:**
- Create: `docs/sync-sessiondb-policy.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/sqlite-event-loop-blocking.md`(追加完成状态)

- [ ] **Step 1: 写规范文档**

Create `docs/sync-sessiondb-policy.md`:

```markdown
# 同步 SessionDB 使用规范

## 背景

`SessionDB`(hermes_state.py)是同步 SQLite 数据访问层。Gateway 是 asyncio 应用。
两者边界若无约束,同步 `conn.execute()` 会在锁/磁盘 I/O 上阻塞事件循环,导致
Discord 心跳断开、消息堆积、SIGTERM 挂起。详见 `docs/sqlite-event-loop-blocking.md`。

## 规则

1. **async 函数体内禁止直接调用同步 SessionDB 方法。** 必须二选一:
   - 通过 `AsyncSessionDB` 门面(`await self._session_db.method(...)`),或
   - `await asyncio.to_thread(raw_db.method, ...)`

2. **禁止 `getattr(x, "_db", x)` unwrap 反模式。** 它绕过 AsyncSessionDB 门面,
   使默认化失效。若需底层同步句柄,显式注释并用 `to_thread` 包裹调用。

3. **确属安全的同步调用,必须标注 `# SYNC_SESSIONDB_SAFE: <理由>`。** 合法理由示例:
   - 仅在启动时、事件循环开始之前
   - 已在 `run_in_executor` / `to_thread` 内
   - housekeeping 独立线程(非事件循环)

4. **新代码会被 CI 扫描器拦截。** 见 `scripts/audit_sync_sessiondb.py`。

## P2 收窄声明(2026-07-08)

原计划 P2b(SessionStore async 化)、P2c(sessions.json 异步化)推迟到上游
#23717(Pluggable SessionDB Provider RFC)定调。在此之前,以点修复 + AsyncSessionDB
门面 + 治本(WAL/prune)为主,避免与上游未来 ABC 改动大面积冲突。
```

- [ ] **Step 2: 给既有合规点标注 SAFE marker**

Run: `grep -rn "SessionDB(read_only=True)\|run_in_executor.*SessionDB\|to_thread.*SessionDB" gateway/ hermes_state.py`
对每个确属安全(已在 executor/thread 内或启动时)的调用点,行尾追加 `# SYNC_SESSIONDB_SAFE: <理由>`。例如 `gateway/channel_directory.py:283`、`gateway/slash_commands.py:4097`(已在 executor 内)。

- [ ] **Step 3: 扫描器清零验证**

Run: `python scripts/audit_sync_sessiondb.py gateway/ cron/ tools/; echo "exit=$?"`
Expected: `exit=0`(所有 async 体内同步 DB 调用已修或已标 SAFE)。若仍有 RED,回到对应 Task 处理或标注 SAFE。

- [ ] **Step 4: 加 CI 门禁步骤**

Edit `.github/workflows/ci.yml`,在现有 test job 的 steps 里,pytest 步骤之前加:

```yaml
      - name: Audit sync SessionDB calls (event-loop blocking gate)
        run: python scripts/audit_sync_sessiondb.py gateway/ cron/ tools/
```

若仓库 CI 用其他配置文件(`.github/workflows/*.yml`),定位主 workflow 后追加此 step。

- [ ] **Step 5: 更新问题总览文档的完成状态**

Edit `docs/sqlite-event-loop-blocking.md`,在末尾"4. 行动方案"之前插入"完成状态(2026-07-08)"小节:

```markdown
## 完成状态(2026-07-08)

| 项 | 状态 |
|----|------|
| #5 `_session_has_compression_in_flight` 双阻塞源 offload | ✅ Task 1 |
| AST 扫描器 + unwrap 专项 + CI 门禁 | ✅ Task 2 / Task 6 |
| WAL TRUNCATE 移出写热路径 | ✅ Task 3 |
| housekeeping 定期 prune/archive/vacuum | ✅ Task 4 |
| 事件循环阻塞回归门禁 | ✅ Task 5 |
| P2b/c(SessionStore + sessions.json async 化) | ⏸ 推迟到上游 #23717 |
```

- [ ] **Step 6: Commit**

```bash
git add docs/sync-sessiondb-policy.md docs/sqlite-event-loop-blocking.md .github/workflows/ci.yml gateway/
git commit -m "docs+ci: sync SessionDB policy, CI audit gate, P2 narrowing declaration"
```

---

## 验证锚点(源码行号,执行时核对)

- #5 方法:`gateway/run.py:5026`(原)/ 修订后 async 版同位
- #5 调用点:`gateway/run.py:5276-5279`
- `get_compression_lock_holder`:`hermes_state.py:2311`
- `AsyncSessionDB`(门面):`hermes_state.py:6308`
- unwrap 反模式(run.py 10 处):3459 / 3558 / 3628 / 5050 / 8138 / 8139 / 10922 / 13013 / 17777 / 18590
- WAL 热路径触发:`hermes_state.py:1171-1172`
- `_try_wal_checkpoint`:`hermes_state.py:1194`
- housekeeping tick 循环:`gateway/run.py:19700`
- housekeeping 启动传参:`gateway/run.py:20292` 附近
- prune/archive/vacuum API:`hermes_state.py:5417 / 5443 / 6096`

---

## 执行顺序与依赖

```
Task 1 (止血 #5) ──┐
                   ├─→ Task 5 (回归门禁,依赖 Task 1 的 async 方法)
Task 2 (扫描器) ───┴─→ Task 6 (CI 门禁 + 规范,依赖 Task 2 + Task 3)
Task 3 (WAL 治本) ──→ Task 4 (DB 治理,复用 Task 3 传入的 session_db)
```

Task 1 / Task 2 / Task 3 互相独立,可并行。Task 4 依赖 Task 3(housekeeping 签名)。Task 5 依赖 Task 1。Task 6 是收尾,依赖 Task 2 + Task 3。
