# Gateway 同步 SQLite 阻塞事件循环 — 整体解决方案

> Codex GLM 5.2 @ 2026-07-07 22:39
> 基于 docs/sqlite-event-loop-blocking.md + 源码对照

---

## 1. 打地鼠为何不可持续

`SessionDB` 约 50+ 个同步方法。每一个只要在 async 上下文中被调用，就是潜在的阻塞点。至今已发现 5 个——都是在生产环境中通过 heartbeat 被阻塞后采样 traceback 定位。修一个只是把下一个最慢的暴露出来。

根本原因：**SessionDB 是同步 API，但 Gateway 是 asyncio 应用。两者之间的边界没有强制约束。** `AsyncSessionDB` 包装器已存在（#55159-mergerd），但仅用于 `gateway/run.py` 中的 `self._session_db`，未推广到 `SessionStore._db`、`channel_directory` 临时实例、`slash_commands` 内部调用等。

---

## 2. 短期（P0）：修复 #5 — 1-2 小时

**阻塞点**：`_session_has_compression_in_flight` → `db.get_compression_lock_holder()`
**路径**：`run.py:5278` → `_handle_active_session_busy_message` (async) → `_busy_session_handler` → `run.py:5026` (同步 SQLite)
**修复**：将 `db.get_compression_lock_holder()` 包装为 `await asyncio.to_thread(db.get_compression_lock_holder, ...)`

**验证**：重启 gateway → 触发压缩流程 → 确认 msg 热路径无阻塞。

---

## 3. 中期（P1）：全量扫描 — 半天

写 `audit_sync_sessiondb.py` 扫描脚本：

```bash
rg -n "\._db\.|SessionDB\(\)|db\.\w+\(" gateway/ --include="*.py" \
  | python3 audit_sync_sessiondb.py --classify
```

输出：按风险分级（热路径 / 低频 / 启动时）列出所有 SessionDB 同步调用点。

**风险分级**：
| 级别 | 场景 | 处理 |
|------|------|------|
| 🔴 热路径 | 消息处理 / heartbeat / cron | 立即 to_thread |
| 🟡 中频 | API 端点 / slash command | to_thread |
| 🟢 低频 | 启动时初始化 | 注释标注，暂不处理 |

**关键残留点**（已识别）：
- `session.py:940` — 裸 `SessionDB()`
- `api_server.py:1153` — 裸 `SessionDB()`
- `mirror.py:117,196` — 裸 `SessionDB()`
- `slash_commands.py:4097` — 已在 `run_in_executor` 内 ✅

---

## 4. 长期（P2）：架构改进 — 2-3 天

### 4a. AsyncSessionDB 默认化
`gateway/run.py` 启动时将 `self._session_db = AsyncSessionDB(SessionDB())` 作为规范。

### 4b. SessionStore async 化
`SessionStore._db` → 替换为 `AsyncSessionDB` 实例，所有内部调用走 `to_thread`。

### 4c. sessions.json 异步化
`session_store._save()` 仍同步写 JSON。在 #59203 已将其降级为可选后，进一步用 `aiofiles` 或 `to_thread` 包装。

### 4d. 显式代码规范
同步 `SessionDB` 调用必须带注释说明上下文安全（如 "仅启动时，事件循环未开始"）。

---

## 5. 上游 issue/PR 对比

| 上游 | 状态 | 与本 spec 关系 |
|------|------|---------------|
| #40695+#40782+#40974 | 已 merged | 我们的 #1，点修复 |
| #55159 | 已 merged | AsyncSessionDB 包装器——但只用在 self._session_db |
| #48564 | OPEN | RFC 伞状追踪器——本 spec 是其 SQLite 子项 |
| #52197 | OPEN | agent-cache 锁阻塞——独立维度，不纳入本 spec |
| #30759 | 未解决 | 序列化 handoff——加锁但仍同步调用，本质未解决 |
| #23717 | RFC | Pluggable SessionDB Provider——本 spec 长期项是其前置步骤 |
| #16856 | — | MCP 懒加载——同病根，不同模块 |
| #18511 | — | Dashboard analytics——同病根 |

**给上游的三个澄清请求**：
1. #30759 "未解决"的确切语义（锁阻塞 vs 协议序列化）
2. #52197 是否应独立追踪
3. #23717 对"AsyncSessionDB 先默认化再抽 ABC"的共识——本 spec 据此排关键路径

---

## 6. 工作量估计

| 阶段 | 内容 | 工时 |
|------|------|------|
| P0 | 修复 #5 + 测试 | 1-2h |
| P1 | 全量扫描脚本 + 分类报告 | 半天 |
| P2a | AsyncSessionDB 默认化 | 半天 |
| P2b | SessionStore async 化 | 1天 |
| P2c | sessions.json 异步化 | 半天 |
| P2d | 代码规范文档 | 1h |

**关键路径**：P0 → P1 扫描 → 根据扫描结果优先修🔴热路径 → P2 架构改进

---

## 验证锚点（源码行号）

- #5 阻塞点：`gateway/run.py:5026-5053`
- #5 热路径：`run.py:5278 → run.py:5103 → base.py:4735`
- `get_compression_lock_holder`：`hermes_state.py:2311-2323`
- `AsyncSessionDB`：`hermes_state.py:6308-6321`
- 裸 `SessionDB()` 残留：`session.py:940`, `api_server.py:1153`, `mirror.py:117,196`
