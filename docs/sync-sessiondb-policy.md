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
