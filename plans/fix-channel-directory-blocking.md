# Spec + Plan: channel directory SessionDB 构造阻塞修复

> **触发**: 2026-07-07 — Discord heartbeat 阻塞 740 秒
> **Traceback**: `build_channel_directory → _build_from_sessions("discord") → _build_from_sessions_db → SessionDB() → sqlite3.connect`
> **上游 gap**: 无相关 issue/PR，最近似的 `#53966` 仍在 OPEN
> **模式**: 又一个"同步 `SessionDB()` 构造 + SQLite 查询在事件循环上"的变体

---

## 1. 问题

`build_channel_directory()` （async）每 5 分钟或启动时调用一次，扫描 state.db 中的网关 session 数据来构建频道目录。它对每个平台调用 `_build_from_sessions("discord")` → `_build_from_sessions_db()`，后者同步构造一个新的 `SessionDB()` → `sqlite3.connect()` 在 1.4GB+ 的 state.db 上阻塞事件循环数秒到数十秒。

### 调用链（同步路径）

```
build_channel_directory (async, line 111)
  → _build_from_sessions("discord") (sync, line 199)
    → _build_from_sessions_db("discord") (sync, line 271)
      → db = SessionDB() → sqlite3.connect(state.db) ❌ 阻塞
      → db.list_gateway_sessions(...) → 另一个同步查询
    → _build_from_sessions_json("discord") (sync, line 274 — fallback)
```

### 观察到的时间线（sanitized）

```
gateway.log:
14:36:15  response ready — 最后正常的 agent 回复

agent.log:
[blocked for 680s] → [blocked for 740s]

traceback:
  build_channel_directory
  → _build_from_sessions("discord")
  → _build_from_sessions_db(...)
  → db = SessionDB()
  → self._conn = sqlite3.connect(...)
```

---

## 2. 修复（v2 — 经 Codex GLM 5.2 审查修订）

**双轨策略**: ① `SessionDB(read_only=True)` 一行消除 `_init_schema` 秒级开销（Footprint Ladder 第 1 档）；② `asyncio.to_thread` 将整个 DB 操作卸载到线程池做防御。

### 改动范围

**仅一个文件**: `gateway/channel_directory.py`

不做改动: `gateway/run.py`（所有调用者已经 `await build_channel_directory(...)` 或在 threadsafe 路径上）

### Step 0: 一行消除 `_init_schema` 开销（❗最关键）

```python
# gateway/session_db.py — SessionDB.__init__ 添加 read_only 参数
def __init__(self, ..., read_only: bool = False):
    ...
    if read_only:
        self._conn.execute("PRAGMA query_only = ON")  # 跳过 _init_schema
```

```python
# gateway/channel_directory.py — _build_from_sessions_db (line ~271)
# Before
db = SessionDB()

# After
db = SessionDB(read_only=True)
```

**效果**: 1.4GB state.db 上 `_init_schema` 从秒级→毫秒级。20 平台整轮刷新从可能 >30s 塌缩至远低于 30s 阈值。

### Step 1: `_build_from_sessions` → async

```python
# Before (line 265)
def _build_from_sessions(platform_name: str) -> List[Dict[str, str]]:
    entries = _build_from_sessions_db(platform_name)
    if entries:
        return entries
    return _build_from_sessions_json(platform_name)

# After
async def _build_from_sessions(platform_name: str) -> List[Dict[str, str]]:
    entries = await asyncio.to_thread(_build_from_sessions_db, platform_name)
    if entries:
        return entries
    return await asyncio.to_thread(_build_from_sessions_json, platform_name)
```

**为什么包装整个函数而不是只包装 SessionDB 构造**: `_build_from_sessions_db` 是一个原子操作：打开 DB → 查询 → 关闭 DB。将其作为一个整体卸载到线程池，在 Python `threading.Lock` 下运行是安全的。

### Step 2: `_build_discord` → async ⭐ Codex 发现

**Line 124 在热路径上也是同步调用**，必须一并改造：

```python
# Before (line 166)
def _build_discord(adapter) -> List[Dict[str, str]]:

# After
async def _build_discord(adapter) -> List[Dict[str, str]]:
```

调用点 (line 124): `platforms["discord"] = await _build_discord(adapter)`

### Step 3: `build_channel_directory` 中的调用者加 `await`

所有现有的同步调用点需要添加 `await`：

| 行号 | 平台 | 更改 |
|------|------|------|
| 124 | **discord（热路径）** | `await _build_discord(adapter)` ⭐ |
| 138 | plugin platforms | `await _build_from_sessions(plat_name)` |
| 146 | plugin adapters | `await _build_from_sessions(entry.name)` |
| 199 | discord fallback | `await _build_from_sessions("discord")` |
| 213 | slack | `await _build_from_sessions("slack")` |
| 257 | slack entries loop | `await _build_from_sessions("slack")` |

**6 个 `await`**，仅在这一个文件内。

---

## 3. 变更清单

| 文件 | 行 | 变更 |
|------|-----|------|
| `gateway/session_db.py` | `__init__` | 新增 `read_only` 参数 + `PRAGMA query_only=ON` |
| `gateway/channel_directory.py` | ~271 | `SessionDB()` → `SessionDB(read_only=True)` |
| `gateway/channel_directory.py` | 166 | `def _build_discord` → `async def` ⭐ |
| `gateway/channel_directory.py` | 265 | `def _build_from_sessions` → `async def` |
| `gateway/channel_directory.py` | 271,274 | `_build_from_sessions_db` / `_build_from_sessions_json` → `await asyncio.to_thread(...)` |
| `gateway/channel_directory.py` | 124,138,146,199,213,257 | 6 处 `await _build_*(...)` |
| `gateway/run.py` | 构建调用处 | 确认已有 `await build_channel_directory(...)` |

**总变更: 2 文件，1 个 `read_only` 参数 + 2 个函数签名 async + 2 个 to_thread 包装 + 6 个调用者 await**。

---

## 4. 回归风险

| 风险 | 缓解 |
|------|------|
| `_build_from_sessions` 变为 async → 现有调用者漏掉 `await` | 所有调用者都在同一个文件的同一个 async 函数内，静态可枚举；不会静默失败 |
| `_build_discord` 变为 async → 漏 `await` 返回协程未执行 | 调用点只有 Line 124 一处，同函数内可直接验证 |
| `asyncio.to_thread` 的开销 | 每 5 分钟调用一次，非热路径；一次线程切换完全可以接受 |
| `SessionDB(read_only=True)` → 连接写操作失败 | `PRAGMA query_only=ON` 在连接级别只读；`_build_from_sessions_db` 仅执行 SELECT，安全 |
| 周期路径 30s 超时（`fut.result(timeout=30)`）→ 频道目录长期不刷新 | read_only 消除 `_init_schema` 秒级开销后整轮 <30s；超时日志建议从 `logger.debug` 提升至 `logger.warning` 以便监控 ⚠️ |
| `to_thread` 超时后被遗留的悬挂线程 | `fut.result` 超时取消协程，但线程池中的 DB 操作会跑完再回收——影响有限但需注意 |
| `_build_from_sessions_json` 读 sessions.json → I/O 在任意线程中 | `sessions.json` 读操作是无状态的，在任意线程中安全 |
| 与 #59203 的交互 | #59203 将路由移到 state.db → `_build_from_sessions_json` 的回退将逐渐失效，但 `read_only` + `to_thread` 仍有防御价值 |

---

## 5. 验证

```bash
# === 静态检查 ===
# 1. 语法检查
python3 -c "import ast; ast.parse(open('gateway/channel_directory.py').read())"
python3 -c "import ast; ast.parse(open('gateway/session_db.py').read())"

# 2. 确认无漏 await —— 搜索所有 _build_discord / _build_from_sessions 调用点
grep -n '_build_discord\|_build_from_sessions' gateway/channel_directory.py

# === E2E 验证（必须 — 项目对 I/O 链的 E2E 要求）===
# 3. 真实 state.db 的 E2E
python3 -c "
import asyncio
from gateway.channel_directory import build_channel_directory
async def test():
    result = await build_channel_directory({})
    assert 'platforms' in result
    print(f'OK: {len(result.get(\"platforms\", {}))} platforms')
asyncio.run(test())
"

# 4. 循环不被饿死的不变量测试
python3 -c "
import asyncio, time
from gateway.channel_directory import build_channel_directory
async def test():
    tick_intervals = []
    last = time.monotonic()
    async def tick():
        nonlocal last
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            tick_intervals.append(now - last)
            last = now
    # 并发运行 tick + channel directory
    tick_task = asyncio.create_task(tick())
    await build_channel_directory({})
    tick_task.cancel()
    max_gap = max(tick_intervals[-100:]) if tick_intervals else 0
    assert max_gap < 2.0, f'Max tick gap {max_gap:.2f}s — loop was starved!'
    print(f'OK: max tick gap = {max_gap:.3f}s')
asyncio.run(test())
"

# === 运行时验证 ===
# 5. 重启后确认频道目录仍正常构建
grep "Channel directory" gateway.log | tail -3

# 6. 监控心跳阻塞（应降为零）
grep "heartbeat blocked" agent.log

# 7. 确认频道目录每 5 分钟刷新仍正常工作
grep "build_channel_directory\|channel directory built" gateway.log | tail -5

# 8. 运行现有测试套件
bash scripts/run_tests.sh
```
