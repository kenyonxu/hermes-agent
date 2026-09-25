# SQLite 事件循环阻塞 — 执行计划

> 目标：消除 Gateway 中所有同步 SessionDB 调用对 asyncio 事件循环的阻塞
> 架构：P0 点修复 → P1 全量扫描 → P2 架构改进 + CI 门禁
> 技术栈：Python asyncio, SQLite (WAL), hermes-agent gateway

---

### P0: 修复 #5 — `_busy_session_handler` 阻塞

**文件**：`gateway/run.py:5026-5053`

- [ ] **步骤 1**：将 `db.get_compression_lock_holder()` 包装为 `await asyncio.to_thread(...)`

```python
# gateway/run.py ~5026 — Before
holder = db.get_compression_lock_holder(session_id)

# After
holder = await asyncio.to_thread(db.get_compression_lock_holder, session_id)
```

- [ ] **步骤 2**：语法检查

```bash
python3 -c "import ast; ast.parse(open('gateway/run.py').read())"
```
预期：exit 0

- [ ] **步骤 3**：运行现有压缩测试

```bash
python -m pytest tests/gateway/test_compression_interrupt_demotion_56391.py -v
```
预期：全绿

- [ ] **步骤 4**：Commit

```bash
git add gateway/run.py
git commit -m "fix(gateway): offload compression lock check to thread pool (#5)"
```

---

### P1: 全量扫描器

**文件**：`scripts/audit_sync_sessiondb.py`（新建）

- [ ] **步骤 1**：编写扫描器骨架

```python
#!/usr/bin/env python3
"""扫描 gateway/ cron/ tools/ 中所有同步 SessionDB 调用点，按风险分级输出。"""

import sys, re, ast
from pathlib import Path

SYNC_DB_PATTERNS = [
    (re.compile(r'\._db\.'), 'SessionStore._db'),
    (re.compile(r'SessionDB\(\)'), 'bare SessionDB()'),
    (re.compile(r'\.(get_|list_|find_|create_|end_|open_|record_|bind_|is_|rewind_|load_|set_|update_|delete_)\w+\('), 'db.method()'),
]
SAFE_MARKER = re.compile(r'SYNC_SESSIONDB_SAFE')

def classify(path: Path, line_no: int, context: str) -> str:
    if 'heartbeat' in context or '_watcher' in context or 'slash_worker' in context:
        return '🔴 RED'
    if 'async def' in context or 'await ' in context:
        return '🔴 RED'
    if 'run_in_executor' in context or 'to_thread' in context:
        return '🟢 GREEN'
    if '__init__' in context or 'startup' in context or 'load_adapters' in context:
        return '🟡 AMBER'
    return '🟡 AMBER'

def scan(root: Path) -> dict:
    results = {'🔴 RED': [], '🟡 AMBER': [], '🟢 GREEN': []}
    for py_file in root.rglob('*.py'):
        lines = py_file.read_text().split('\n')
        for i, line in enumerate(lines, 1):
            for pat, label in SYNC_DB_PATTERNS:
                if pat.search(line) and not SAFE_MARKER.search(line):
                    level = classify(py_file, i, '\n'.join(lines[max(0,i-3):i+3]))
                    results[level].append(f'{py_file}:{i} [{label}] {line.strip()[:100]}')
    return results

if __name__ == '__main__':
    roots = [Path(p) for p in sys.argv[1:]] or [Path('gateway'), Path('cron'), Path('tools')]
    all_results = {}
    for r in roots:
        all_results.update(scan(r))
    for level in ['🔴 RED', '🟡 AMBER', '🟢 GREEN']:
        for item in all_results[level]:
            print(f'{level} {item}')
    if all_results['🔴 RED']:
        sys.exit(1)
```

- [ ] **步骤 2**：运行扫描

```bash
python3 scripts/audit_sync_sessiondb.py gateway/ cron/ tools/; echo "exit=$?"
```
预期：exit=1，列出未修复的 RED 调用点

- [ ] **步骤 3**：Commit

---

### P2a: AsyncSessionDB 默认化

**文件**：`gateway/run.py`、`gateway/session.py`

- [ ] **步骤 1**：确认 `AsyncSessionDB` 可用

```bash
grep -n "class AsyncSessionDB" hermes_state.py
```
预期：`6308:class AsyncSessionDB:`

- [ ] **步骤 2**：`run.py` 中 `self._session_db` 已为 AsyncSessionDB，无需改

- [ ] **步骤 3**：`session.py:940` 替换裸 `SessionDB()` → `AsyncSessionDB(SessionDB())`

- [ ] **步骤 4**：验证

```bash
python3 -c "import ast; ast.parse(open('gateway/session.py').read())"
python -m pytest tests/gateway/ -k "session" -q
```

---

### P2b: SessionStore async 化

**文件**：`gateway/session.py`

- [ ] **步骤 1**：`SessionStore._db` 改为 `AsyncSessionDB` 实例

- [ ] **步骤 2**：内部同步调用全部加 `await` + `to_thread`

- [ ] **步骤 3**：运行全量 gateway 测试

---

### P2c: sessions.json 异步化

**文件**：`gateway/session.py`

- [ ] **步骤 1**：`_save()` 方法包装 `asyncio.to_thread`

- [ ] **步骤 2**：确认 #59203 已将路由移到 state.db，sessions.json 降级为可选

---

### P2d: CI 门禁 + 代码规范

- [ ] **步骤 1**：`.github/workflows/ci.yml` 追加扫描步骤

- [ ] **步骤 2**：给既有合规点标注 `# SYNC_SESSIONDB_SAFE` 注释

- [ ] **步骤 3**：全量扫描清零验证

```bash
python3 scripts/audit_sync_sessiondb.py gateway/ cron/ tools/; echo "exit=$?"
```
预期：exit=0
