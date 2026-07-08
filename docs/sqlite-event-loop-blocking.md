# Hermes Gateway 同步 SQLite 调用阻塞事件循环 — 问题总览

> **涉及平台**: 全部（Discord、飞书等依 asyncio 事件循环运行）
> **首次发现**: 2026-06-09 | **持续影响**: 至今
> **根本原因**: `SessionDB` 的同步 `conn.execute()` 调用了 asyncio 事件循环，在 state.db 膨胀至 1.4GB+ 文件缓存压力下阻塞了 Discord 心跳和消息分发

---

## 1. 问题本质

Hermes Gateway 是 asyncio 应用。它的消息处理、平台心跳、cron 调度全部运行在同一个事件循环上。但**事件循环中散布着对 `SessionDB`（SQLite `state.db`）的同步调用**——这些 `conn.execute()` 在锁上阻塞或等待磁盘 I/O，从而阻塞整个事件循环，导致以下后果：

- Discord WebSocket 心跳无法发送 → Discord 断开连接
- 入站消息无法处理 → 用户发消息无回复
- 网关无法处理 SIGTERM → 关机过程中挂起 90 秒后被 SIGKILL
- 飞书和其他平台同样受影响

当 `state.db` 很小时（<100MB），这些阻塞通常不到 1ms，影响几乎不可见。但随着数据库增长至 1.4GB / 24,740 个会话 / 145,983 条消息后，SQLite 的 WAL 检查点、文件缓存未命中和锁竞争将个别调用推到秒级甚至分钟级。一旦阻塞开始，累积效应会迅速使网关无法响应。

`SessionDB` 中约有 **50+ 个方法**，大多数为同步方法。**其中任何一个只要在事件循环回调中被调用，就可能触发阻塞。**

---

## 2. 发现历史（打地鼠模式）

每个阻塞点都是独立发现的——在网关因心跳被阻塞后，通过生产环境中采样到的 traceback 定位。截至 2026-07-07，**在消息热路径上已发现 5 个不同的阻塞点**：

| # | 发现时间 | 阻塞点 | 修复 | 来源 |
|---|---------|--------|------|------|
| 1 | 6/9 | `_handoff_watcher` → `list_pending_handoffs` → `SELECT *` 全扫描 | ① 索引（我们，merged） ② `AsyncSessionDB`（上游 v0.18.0） | #40695 / #43504 / #55159 |
| 2 | 7/5 | `get_or_create_session` → `_is_session_ended_in_db` → `get_session` → `SELECT` | `asyncio.to_thread` 包装（我们） | 本地 commit |
| 3 | 7/5 | `_ensure_loaded` → 同步 `sessions.json` 读操作 | ① #59203 将路由移到 state.db（上游） ② `asyncio.to_thread` 包装（我们） | #59203 + 本地 commit |
| 4 | 7/7 | `build_channel_directory` → `_build_from_sessions_db` → `SessionDB()` 构造 + `list_gateway_sessions` | `read_only` + `to_thread`（我们） | 本地 commit |
| 5 | 7/7 | `_busy_session_handler` → `_session_has_compression_in_flight` → `get_compression_lock_holder` → `SELECT` | 🔴 未修复 | — |

### 为什么此模式会重复出现

`SessionDB` 设计为一个**同步**数据访问层（`check_same_thread=False` + `threading.Lock`）。上游意识到该模式存在问题，并在 `AsyncSessionDB`（`hermes_state.py:5812`）中通过 `asyncio.to_thread` 包装器提供了一个异步门面。然而 `AsyncSessionDB` 仅用于 `gateway/run.py` 中的 `self._session_db`，**未用于以下场景**：

- `gateway/session.py` → `SessionStore._db`（我们的修复 #2 + #3）
- `gateway/channel_directory.py` → 临时 `SessionDB()` 实例（我们的修复 #4）
- `gateway/run.py` → 直接 sync 查询（#5，未修复）
- 以及其他可能在 async 上下文中调用同步 `SessionDB` 方法的区域

**每一个新的阻塞点都是同一个根本缺陷的不同表现形式**：同步 SQLite 调用了事件循环。修复一个点只是把下一个最慢的点暴露出来。

---

## 3. 当前缓解措施（本地）

我们在上游修复基础上维护了以下本地补丁：

- `hermes_state.py`：`idx_sessions_handoff_state` 索引（已 upstream，仍带本地标记）
- `gateway/run.py`：7 处 `_session_store` 调用包装了 `asyncio.to_thread`
- `gateway/slash_commands.py`：29 处 `_session_store` 调用包装了 `asyncio.to_thread`
- `gateway/channel_directory.py`：`SessionDB(read_only=True)` + 线程卸载

上游已合并的缓解措施（我们已同步）：

- `AsyncSessionDB` 包装器及其在 `self._session_db` 中的使用（#55159）
- #59203：将路由索引从 `sessions.json` 移到 `state.db`
- #44383 + #44432：Discord 运行时任务退出恢复和僵尸客户端防护

### 局限性

- `#5` 仍未修复
- 可能还有更多未被发现的阻塞点——`SessionDB` 约 50+ 个方法，其中多数为同步方法
- `session_store._save()` 仍然同步写入 `sessions.json`（尽管 #59203 将其降级为可选）
- 向后兼容意味着修复在本地维护且需重新应用

---

## 完成状态(2026-07-08)

| 项 | 状态 |
| --- | --- |
| #5 `_session_has_compression_in_flight` 双阻塞源 offload | ✅ Task 1 |
| AST 扫描器 + unwrap 专项 + CI 门禁 | ✅ Task 2 / Task 6 |
| WAL TRUNCATE 移出写热路径 | ✅ Task 3 |
| housekeeping 定期 prune/archive/vacuum | ✅ Task 4 |
| 事件循环阻塞回归门禁 | ✅ Task 5 |
| P2b/c(SessionStore + sessions.json async 化) | ⏸ 推迟到上游 #23717 |

---

## 4. 行动方案

### 短期：修复 #5

`_session_has_compression_in_flight`（`gateway/run.py:5026`）在 busy-session 处理程序中同步调用 `db.get_compression_lock_holder()`。将调用包装为 `await asyncio.to_thread(...)`。

### 中期：扫描全量调用面

对 `SessionDB` 同步方法的所有 async 上下文调用点进行全文扫描：

```bash
# 在 async def 或 asyncio 任务中寻找同步 conn.execute 调用
grep -rn "\._conn\.execute\|db\.\(get\|list\|find\|create\|end\|open\|record\|bind\|is_\|rewind\|load\|set_\|update\|delete\|s\.\)" \
  gateway/run.py gateway/session.py gateway/*.py plugins/platforms/*/
```

目标：**零个同步 `SessionDB` 调用了异步热路径**。

### 长期：架构改进（上游提案）

1. **将 `AsyncSessionDB` 改为默认值**：网关启动时创建 `AsyncSessionDB(SessionDB())`，需要原始 `SessionDB` 时才用 `.db`。这与 `threading.Lock` 语义一致，且避免了逐个调用点的包装。

2. **显式代码规范**：同步 `SessionDB` 的调用必须带有文档注释，说明为何在此上下文中是安全的（例如 "仅启动时，事件循环开始之前"）。

3. **集成到 `__getattr__` 的 lint 规则**：将原始 `SessionDB` 方法从 async 上下文调用的情况标记为警告。
