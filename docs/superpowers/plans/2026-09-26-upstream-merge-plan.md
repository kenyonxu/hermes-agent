# Upstream 合并（102 ↑ / 17,459 ↓）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `main` 一次推进到 `upstream/main` tip（`7b11c1bcac`），不丢失我方 SQLite 事件循环治理成果，停机窗口控制在 ~12 分钟内。

**Architecture:** 新建 `dryrun-merge-20260926` 承载合并（T-1 天完成冲突解决 + 全量 pytest 隔夜），产出两份产物：合并分支本身 + 一份《SQLite 函数级取舍决策清单》。T 日停机窗口只做已在 dryrun 验证过的动作（聚焦套件 + editable 冒烟 + 推送重启）。

**Tech Stack:** git merge / pytest（`scripts/run_tests.sh` CI-parity）/ systemd user service / pip editable。

**Spec:** `docs/superpowers/specs/2026-09-26-upstream-merge-design.md`

## Global Constraints

- 合点 merge-base：`217ab2f8df57`（2026-08-28）。
- upstream tip：`7b11c1bcac`（2026-09-24 晚）。**合并前必须重新 fetch**——daily dryrun 流程可能已把上游又往前推。
- 停机用 `systemctl --user stop/start hermes-gateway`（**不是** `zhihui gateway stop`；本机 zhihui profile 的 gateway 跑在默认名字的 unit 下，见 spec §6）。
- 取舍三档规则（spec §4）：`supersedes` / `keep` / `arch-diverged`；arch-diverged 默认取上游架构，我方行为改写成层上策略。
- 上游已废弃 `py-modules` 静态列表（改用 `_root_py_modules` 辅助 + `packages.find`），**不要把它加回来**；editable 重装逻辑跟随上游新机制。
- 测试一律走 `scripts/run_tests.sh`，不直接调 `pytest`（CI parity）。
- 合并提交说明必须引用取舍决策清单文件路径。

## 预先查明的事实（计划已按此校准）

- `tests/test_hermes_state.py`（单文件）**上游已删除**，分裂为 `tests/hermes_state/` 目录 → 聚焦套件命令与 9/3 版不同（见 Task 7）。
- 上游已存在 `hermes_state_schema.py`（上游 1,311 行 vs 我方 1,529 行）→ 冲突文件之一，进取舍清单。
- 我方独有的测试/工具（上游没有，合并后保留）：`tests/gateway/test_send_deadline.py`、`tests/gateway/test_event_loop_blocking_regression.py`、`tests/cron/test_shared_session_db.py`、`tests/test_audit_sync_sessiondb.py`、`tests/test_housekeeping_db_governance.py`、`scripts/audit_sync_sessiondb.py`。
- `tests/hermes_cli/test_kanban_db.py` 上游已有（双方同名，需比对内容）。
- 9/3 deselect 的 satellite 路由测试在上游仍存在 → 聚焦套件沿用 deselect（仅当它仍表现顺序依赖时）。
- 上游依赖增量约 225 行（含一批 `python_version >= '3.14'` 钉版），`hermes_startup_watchdog.py` 已在树根 → editable 必须重装。

---

### Task 1: 冻结输入，建 dryrun 分支

**Files:**
- Create: `dryrun-merge-20260926` 分支（无文件改动）
- Modify: 无

**Interfaces:**
- Consumes: 干净的 `main`（tip `53d43f1df8` 含 spec 提交）、已 fetch 的 `upstream/main`。
- Produces: 分支 `dryrun-merge-20260926` 指向 main tip；变量 `UP_TIP` 供后续任务引用。

- [ ] **Step 1: 确认工作区可合并状态**

已提交的 spec（`53d43f1df8`）在 main 上。剩余未跟踪/脏文件（`graphify-out/`、`merge-20260903.sh`、`package-lock.json`、`MERGE_FIX_LOG.md` 等）不影响合并——它们要么 gitignore、要么不与上游冲突。确认无未提交的**已跟踪代码文件**改动：

```bash
cd /home/kai-remote/github/hermes-agent
git diff --quiet HEAD -- '*.py' && echo "py clean" || git diff --stat HEAD -- '*.py'
```

Expected: `py clean`（spec 提交后无 Python 代码脏改）。

- [ ] **Step 2: 重新 fetch 并记录 upstream tip**

```bash
cd /home/kai-remote/github/hermes-agent
git fetch upstream 2>&1 | tail -2
UP_TIP=$(git rev-parse --short upstream/main)
echo "UP_TIP=$UP_TIP"
git rev-list --left-right --count main...upstream/main
```

Expected: `UP_TIP` 打印（计划编写时为 `7b11c1bcac`，若上游又推进则为更新值——用这个真实值替换后续所有 `7b11c1bcac`）；ahead/behind 第二列 ≥17459。

- [ ] **Step 3: 建 dryrun 分支**

```bash
cd /home/kai-remote/github/hermes-agent
git checkout -b dryrun-merge-20260926 main
git branch --show-current
```

Expected: 输出 `dryrun-merge-20260926`。

- [ ] **Step 4: Commit（分支即产物，无文件提交）**

分支创建无需提交。记录起点：

```bash
git log --oneline -1
```

Expected: tip 为 `53d43f1df8 docs: design spec for upstream merge catch-up ...`。

---

### Task 2: 执行合并，产出冲突文件清单

**Files:**
- Modify: 若干冲突文件（本任务只列清单，不解冲突）

**Interfaces:**
- Consumes: `dryrun-merge-20260926`（Task 1）、`UP_TIP`。
- Produces: `/tmp/merge-conflicts-20260926.txt`（冲突文件清单），供 Task 3/4 分拣。

- [ ] **Step 1: 发起合并（预期大量冲突）**

```bash
cd /home/kai-remote/github/hermes-agent
git merge upstream/main --no-edit 2>&1 | tail -30
```

Expected: 输出 `CONFLICT (content): ...` 多行，`Automatic merge failed; fix conflicts and then commit the result.`。**不要**在此步解决。

- [ ] **Step 2: 导出冲突清单**

```bash
cd /home/kai-remote/github/hermes-agent
git diff --name-only --diff-filter=U | sort > /tmp/merge-conflicts-20260926.txt
wc -l /tmp/merge-conflicts-20260926.txt
cat /tmp/merge-conflicts-20260926.txt
```

Expected: 清单落盘。根据冲突面预测，应含第 1.2 节热点文件（`gateway/run.py`、`hermes_state.py`、`cron/scheduler.py`、`gateway/platforms/base.py`、`gateway/platforms/api_server.py`、`hermes_cli/kanban_db.py`、`gateway/session.py`、`gateway/delivery_ledger.py`、`gateway/channel_directory.py`、`hermes_state_schema.py` 等）+ 若干 `.gitignore`/workflow/测试文件。

- [ ] **Step 3: 分拣热点 vs 非热点**

```bash
cd /home/kai-remote/github/hermes-agent
HOT='gateway/run.py hermes_state.py cron/scheduler.py gateway/platforms/base.py gateway/platforms/api_server.py hermes_cli/kanban_db.py gateway/session.py gateway/delivery_ledger.py gateway/channel_directory.py hermes_state_schema.py cron/executions.py cron/notepad.py agent/verification_evidence.py'
grep -Ff <(echo "$HOT" | tr ' ' '\n') /tmp/merge-conflicts-20260926.txt > /tmp/hot.txt || true
comm -23 /tmp/merge-conflicts-20260926.txt <(sort /tmp/hot.txt) > /tmp/cold.txt
echo "HOT:"; cat /tmp/hot.txt; echo "COLD:"; cat /tmp/cold.txt
```

Expected: `/tmp/hot.txt`（进 Task 3 函数级取舍）与 `/tmp/cold.txt`（进 Task 4 机械解冲突）分开。

- [ ] **Step 4: 暂不 commit**

合并进行中（`git status` 显示 `You have unmerged paths`），保持现场进入 Task 3。

---

### Task 3: SQLite 治理线函数级取舍 + 决策清单

**Files:**
- Modify: `/tmp/hot.txt` 列出的每个热点文件
- Create: `docs/superpowers/specs/2026-09-26-sqlite-triage-decisions.md`

**Interfaces:**
- Consumes: `/tmp/hot.txt`（Task 2）、取舍三档规则（spec §4）。
- Produces: 决策清单文件（每个热点文件一条记录，含档位与落地动作）；热点文件冲突已解决并 `git add`。

**取舍规则（来自 spec §4，逐块判定）：**

| 档位 | 判定条件 | 动作 |
|---|---|---|
| `supersedes` | 上游已覆盖该行为（DELETE-journal 读者等待、损坏恢复、busy 预算语义、FTS 构造等待） | 放弃我方版本，取上游 |
| `keep` | 上游未覆盖（事件循环阻塞卸载、housekeeping WAL 治理、send deadline、cron 共享 SessionDB） | 保留我方实现，移植到上游新架构 |
| `arch-diverged` | 架构分叉（我方共享单写 `get_shared_session_db` + 只读池 vs 上游 `open_db`/`_root_py_modules` 时代架构） | **默认取上游架构**，我方行为改写为其上的开关/配置/调用点 |

- [ ] **Step 1: 建决策清单骨架**

```bash
cd /home/kai-remote/github/hermes-agent
{
  echo "# SQLite 函数级取舍决策清单 — dryrun-merge-20260926"
  echo
  echo "upstream tip: $(git rev-parse --short upstream/main)"
  echo "merge-base: 217ab2f8df57"
  echo
  while read f; do
    echo "## $f"
    echo "- 我方行为: "
    echo "- 上游对应: "
    echo "- 档位: supersedes | keep | arch-diverged"
    echo "- 落地: "
    echo "- 验证: "
    echo
  done < /tmp/hot.txt
} > docs/superpowers/specs/2026-09-26-sqlite-triage-decisions.md
wc -l docs/superpowers/specs/2026-09-26-sqlite-triage-decisions.md
```

Expected: 清单骨架落盘，每个热点文件一节。

- [ ] **Step 2: 逐热点文件函数级比对并填清单**

对每个热点文件 `F`，先看上游新版与我方改动：

```bash
F=hermes_state.py   # 逐文件替换
echo "=== 我方相对合点的改动块 ==="
git diff 217ab2f8df57..main -- "$F" | grep -E '^@@|^[+-]def |^[+-]\s+def ' | head -40
echo "=== 上游对该文件的提交 ==="
git log --oneline 217ab2f8df57..upstream/main -- "$F" | head -20
```

逐块判定档位并解决冲突：
- `hermes_state.py`（上游 171 提交）：我方 `_init_schema` per-db 缓存、busy_timeout 30s、共享单写 `get_shared_session_db`、WAL→DELETE、只读池。上游已补 FTS 构造等待 / 损坏恢复 / busy 预算语义 → 这些块 `supersedes`；事件循环卸载与连接共享策略若无上游对应 → `keep` 或 `arch-diverged`（取上游连接管理，我方策略挂其上）。
- `gateway/run.py`（上游 327 提交）：我方 housekeeping WAL 治理、delivery_ledger 走共享连接、to_thread 卸载。上游重构多 → 大块 `arch-diverged`。
- `gateway/delivery_ledger.py` / `channel_directory.py`：我方连接缓存 / 只读卸载。对照上游 `576accd92b` open_db 层判定。
- `cron/scheduler.py` / `executions.py` / `notepad.py`：我方 tick 共享 SessionDB。判上游是否有等价。
- `hermes_state_schema.py`（上游 1,311 vs 我方 1,529 行）：逐 schema 块比对 FTS triggers。

每定一档，把对应冲突块按规则解决（`git checkout --theirs` 取上游块 / 保留我方块 / 手工改写），然后：

```bash
# 解决完一个文件
git add "$F"
```

把判定结果填入 `docs/superpowers/specs/2026-09-26-sqlite-triage-decisions.md` 对应小节。

Expected: `/tmp/hot.txt` 全部文件 `git add`，决策清单每节四字段（档位/落地/验证/上游对应）填实，无占位。

- [ ] **Step 3: 验证热点取舍不破坏导入**

```bash
cd /home/kai-remote/github/hermes-agent
python3 -c "import ast,sys
for f in open('/tmp/hot.txt'):
    f=f.strip()
    if f.endswith('.py'):
        ast.parse(open(f).read(), f)
print('hot files parse OK')"
```

Expected: `hot files parse OK`（冲突标记已全部清除，语法有效）。

- [ ] **Step 4: 提交取舍决策清单（独立于合并提交，便于追溯）**

```bash
cd /home/kai-remote/github/hermes-agent
git add -f docs/superpowers/specs/2026-09-26-sqlite-triage-decisions.md
git commit -m "docs: SQLite function-level triage decisions for upstream merge $(git rev-parse --short upstream/main)"
git log --oneline -1
```

Expected: 决策清单单独成提交（`docs/superpowers/*` 被 gitignore，需 `-f`）。注意：此提交发生在合并进行中的工作区——git 允许在 unmerged 状态下提交已暂存文件之外的新文件；若 git 拒绝，则把本步清单留到 Task 5 合并提交前一并 `git add -f`。

---

### Task 4: 非热点冲突机械解决

**Files:**
- Modify: `/tmp/cold.txt` 列出的文件

**Interfaces:**
- Consumes: `/tmp/cold.txt`（Task 2）。
- Produces: 非热点冲突全部解决并 `git add`，工作区无 unmerged 路径。

- [ ] **Step 1: 分类处理非热点冲突**

`/tmp/cold.txt` 预计含三类：

```bash
cd /home/kai-remote/github/hermes-agent
echo "=== workflow/gitignore（上游优先） ==="
grep -E '\.github/workflows|\.gitignore' /tmp/cold.txt
echo "=== 测试文件 ==="
grep -E '^tests/' /tmp/cold.txt
echo "=== 其余 ==="
grep -vE '\.github/workflows|\.gitignore|^tests/' /tmp/cold.txt
```

- [ ] **Step 2: 逐文件解决**

策略：
- `.github/workflows/*`、`.gitignore` → 取上游（`git checkout --theirs`），我方若有独有行再手工补回；
- `tests/*`（如 `tests/hermes_cli/test_kanban_db.py` 双方同名）→ 逐块比对，我方新增用例保留、上游修复并入；
- 其余（`plugins/*`、`agent/*` 杂项）→ 函数级看，多数取上游。

```bash
# 示例：workflow 取上游
git checkout --theirs .github/workflows/tests.yml && git add .github/workflows/tests.yml
# 逐文件处理 cold 列表，每解决一个 git add
```

Expected: `git diff --name-only --diff-filter=U` 输出为空（无未合并路径）。

- [ ] **Step 3: 全树语法/冲突标记自检**

```bash
cd /home/kai-remote/github/hermes-agent
git diff --name-only --diff-filter=U | wc -l
! grep -rnE '^(<{7}|={7}|>{7})' --include='*.py' gateway cron hermes_state.py hermes_state_schema.py 2>/dev/null && echo "no conflict markers"
```

Expected: `0` + `no conflict markers`。

---

### Task 5: 提交合并 + dryrun 全量 pytest 隔夜

**Files:**
- Modify: 无新文件；产生合并提交

**Interfaces:**
- Consumes: Task 3/4 已解决全部冲突。
- Produces: 合并提交（说明引用决策清单路径）；全量 pytest 结果日志 `/tmp/pytest-full-20260926.log`。

- [ ] **Step 1: 完成合并提交**

```bash
cd /home/kai-remote/github/hermes-agent
git commit --no-edit 2>/dev/null || git commit -m "Merge remote-tracking branch 'upstream/main' ($(git rev-parse --short upstream/main)) into dryrun-merge-20260926

SQLite 取舍见 docs/superpowers/specs/2026-09-26-sqlite-triage-decisions.md"
git log --oneline -1
```

Expected: 合并提交生成，说明含 upstream tip 短哈希与决策清单路径。

- [ ] **Step 2: 后台跑全量 pytest（CI-parity）**

```bash
cd /home/kai-remote/github/hermes-agent
nohup scripts/run_tests.sh > /tmp/pytest-full-20260926.log 2>&1 &
echo "pytest PID $!"
```

Expected: 全量套件后台运行（数千测试，数十分钟到数小时）。

- [ ] **Step 3: 隔夜等待并收结果**

```bash
cd /home/kai-remote/github/hermes-agent
# 跑完后：
tail -40 /tmp/pytest-full-20260926.log
```

Expected: 统计 passed/failed。与 9/3 基线（1728 passed / 1 上游自带 satellite 顺序依赖失败）对比。

- [ ] **Step 4: 归因新增失败**

```bash
grep -E '^FAILED|ERROR' /tmp/pytest-full-20260926.log | head -30
```

Expected: 若出现**非上游自带**的新失败 → 归因到 Task 3 的具体取舍决策，回 dryrun 分支修（对应决策档位判错则改档），重跑该文件确认后**才**进 Task 6。若全部为上游自带（单跑可过），可进 Task 6。

---

### Task 6: 生成停机脚本 merge-20260926.sh

**Files:**
- Create: `merge-20260926.sh`（仓库根，模板化自 `merge-20260903.sh`）

**Interfaces:**
- Consumes: dryrun 分支全量验证通过（Task 5）、`UP_TIP`、上游 py-modules 机制变更。
- Produces: 可执行停机脚本，窗口内运行。

- [ ] **Step 1: 确认上游 packaging 机制（决定 editable 步骤）**

上游已废弃 `py-modules` 静态列表（改用 `packages.find` + `_root_py_modules`）。确认当前 pyproject 形态：

```bash
cd /home/kai-remote/github/hermes-agent
grep -nE "py-modules|_root_py_modules|packages" pyproject.toml | head
ls hermes_startup_watchdog.py 2>/dev/null && echo "watchdog at root"
```

Expected: 确认 editable 重装步骤的命令与新机制一致（不是简单复刻 9/3 的 pip 参数，而是按上游新布局重装）。

- [ ] **Step 2: 写脚本**

基于 `merge-20260903.sh` 模板，替换：分支名 `dryrun-merge-20260926`、upstream tip、聚焦套件命令（Task 7 的最终版）、editable 步骤。关键段：

```bash
#!/bin/bash
# merge-20260926.sh 停机合并脚本
# 内容：main + upstream <UP_TIP> + SQLite 取舍（决策见 specs/2026-09-26-sqlite-triage-decisions.md）
set -e
cd /home/kai-remote/github/hermes-agent

echo "=== [0/7] 前置检查 ==="
git status --short | grep -qE '^\s*M.*\.py$' && { echo "❌ 有未提交 py 改动"; exit 1; } || echo "py clean"

echo "=== [1/7] 停 Gateway ==="
systemctl --user stop hermes-gateway
sleep 2
systemctl --user is-active hermes-gateway 2>/dev/null && { echo "❌ 未停干净"; exit 1; } || echo "已停"

echo "=== [2/7] 聚焦套件验证 ==="
scripts/run_tests.sh tests/gateway/test_session_hygiene.py tests/hermes_state/ tests/cron/ -q \
  || { echo "❌ 验证失败，回滚"; systemctl --user start hermes-gateway; exit 1; }

echo "=== [3/7] 合并 dryrun 分支 ==="
git checkout main && git merge dryrun-merge-20260926 --no-edit
git log --oneline -1

echo "=== [4/7] 重装 editable ==="
proxychains4 pip install -e ".[mcp,messaging,matrix]" 2>&1 | tail -3

echo "=== [5/7] import 冒烟 ==="
cd /home/kai-remote && env -u PYTHONPATH python3 -c "import hermes_cli, hermes_startup_watchdog; print('import OK')" \
  || { echo "❌ 冒烟失败"; systemctl --user start hermes-gateway; exit 1; }

echo "=== [6/7] 推送 ==="
cd /home/kai-remote/github/hermes-agent && proxychains4 git push origin main

echo "=== [7/7] 重启 Gateway ==="
systemctl --user start hermes-gateway
sleep 5
systemctl --user is-active hermes-gateway && echo "✅ 完成"
```

注：`tests/test_hermes_state.py` 已从聚焦命令去掉（上游已删除该单文件），改用 `tests/hermes_state/` 目录。

- [ ] **Step 3: 静态校验脚本**

```bash
bash -n merge-20260926.sh && echo "syntax OK"
```

Expected: `syntax OK`。

---

### Task 7: 停机窗口执行（T 日）

**Files:**
- Modify: `main`（合入 dryrun）、本机 gateway 服务状态

**Interfaces:**
- Consumes: `merge-20260926.sh`（Task 6）、dryrun 全量验证通过（Task 5）。
- Produces: main 推进到 upstream tip；gateway 恢复投递。

- [ ] **Step 1: 挑不赶稿窗口，跑脚本**

```bash
bash /home/kai-remote/github/hermes-agent/merge-20260926.sh 2>&1 | tee /tmp/merge-20260926.log
```

Expected: 7 步全绿，尾部 `✅ 完成`。任一验证步骤失败会自动 `systemctl --user start hermes-gateway` 回滚且 main 不动。

- [ ] **Step 2: 观察 gateway 投递恢复（10 分钟阈值）**

```bash
sleep 60
journalctl --user -u hermes-gateway --since "-2 min" --no-pager | tail -20
cat /home/kai-remote/.hermes/profiles/zhihui/gateway_state.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['gateway_state'], list(d['platforms'].keys()))"
```

Expected: `gateway_state` 为 `running`，`discord`/`feishu`/`api_server` 陆续回到 `connected`。10 分钟内未恢复 → 执行回滚（Step 3）。

- [ ] **Step 3: 回滚路径（仅投递未恢复时）**

```bash
cd /home/kai-remote/github/hermes-agent
git checkout main && git reset --hard 61f4985305
systemctl --user restart hermes-gateway
```

Expected: main 回到合并前，gateway 用旧代码恢复。dryrun 分支保留用于事后归因。

- [ ] **Step 4: 收尾记录**

```bash
printf '%s\n' "$(date '+%F %T') | $(git rev-parse --short HEAD) | Merge dryrun-merge-20260926 (upstream <UP_TIP>) via maintenance window" \
  | tee -a /home/kai-remote/.hermes/cron/update.log
```

Expected: update.log 追加一行合并记录。

---

## Self-Review 记录

- **Spec 覆盖**：spec §3 的 T-1（步骤1-7）映射到 Task 1-5；T 日窗口（步骤8-13）映射到 Task 6-7；§4 取舍规则落到 Task 3；§5 回滚落到 Task 5 Step4 / Task 7 Step1/3；§6 环境备注落到 Global Constraints + Task 6 停机命令。无遗漏。
- **占位符**：Task 3 决策清单为模板骨架 + 明确的填写指令（每文件四字段），非 "TODO" 空话；`UP_TIP` 为 Task 1 Step2 实测变量，非占位。
- **类型一致**：分支名 `dryrun-merge-20260926`、决策清单文件名、脚本名 `merge-20260926.sh`、日志路径在 Task 间一致。聚焦套件命令已按上游实况修正（去掉已删除的 `tests/test_hermes_state.py`，改用 `tests/hermes_state/`）。
