# 设计：main 追赶 upstream/main（102 ↑ / 17,459 ↓）合并方案

日期：2026-09-26
状态：待用户审阅
分支目标：`dryrun-merge-20260926` → `main`

## 1. 背景与问题

本地 `main`（tip `61f4985305`，2026-09-02）相对上游合点 `217ab2f8df`（2026-08-28）
领先 102 个提交，但落后 `upstream/main`（tip 已到 2026-09-24）**17,459 个提交**。
上游在这一个月跨过了 v0.21.0 → v0.21.5 RC 区间，并对我们深改过的同一条
SQLite/交付账本线做了大量平行演进，包括一次整条 SQLite 访问层的重构
（`576accd92b refactor(sqlite): one open_db/transaction layer for every small store`）。

问题：如何在**不丢失我们 SQLite 事件循环治理成果**的前提下，把 main 一次推进到
上游 tip，并把停机窗口控制在可接受范围。

### 1.1 我方领先内容（upstream 没有的净改动）

50 个文件，+10,275 / −230 行。扣除文档后真正的代码改动：

- **Gateway SQLite 事件循环治理**（工作量最大）：`gateway/delivery_ledger.py`、
  `gateway/channel_directory.py`、`hermes_state.py`、`gateway/run.py`、`gateway/session.py`、
  `gateway/platforms/base.py`、`gateway/platforms/api_server.py` —— 共享 SessionDB 单写
  连接、只读连接池、WAL→DELETE journal、housekeeping 定期 prune/vacuum、send deadline。
- **cron**：`cron/scheduler.py`、`cron/executions.py`、`cron/notepad.py` —— 所有 job tick
  共享单个 SessionDB。
- **杂项**：Discord line watchdog（`plugins/platforms/discord/recovery.py`）、
  GLM-5.3-flash 模型目录、kanban/projects db、审计扫描器 `scripts/audit_sync_sessiondb.py`。

### 1.2 冲突热点（上游也动过的我方文件，按上游提交数排序）

| 上游提交数 | 文件 |
|---|---|
| 327 | `gateway/run.py` |
| 171 | `hermes_state.py` |
| 169 | `cron/scheduler.py` |
| 129 | `gateway/platforms/base.py` |
| 108 | `gateway/platforms/api_server.py` |
| 96 | `hermes_cli/kanban_db.py` |
| 62 | `gateway/session.py` |
| 22 | `gateway/delivery_ledger.py` |
| 19 | `gateway/channel_directory.py` |

另有 `.gitignore`、`.github/workflows/tests.yml`、若干测试文件。

### 1.3 已确认的决策（用户拍板）

1. **推进策略**：每日 dryrun 一次到位（非分段追 tag）。
2. **SQLite 线取舍**：函数级比对取舍。
3. **验证门槛**：聚焦套件 + import 冒烟（窗口内），dryrun 分支隔夜跑全量。

## 2. 方案架构

新建 `dryrun-merge-20260926` 承载合并，产出两份产物：

1. 合并后的分支（`dryrun-merge-20260926`）；
2. 一份《SQLite 函数级取舍决策清单》（本 spec 第 4 节的模板实例），作为合并提交
   说明附件，便于日后追溯每一处取舍的理由。

停机窗口只做已在 dryrun 分支验证过的事（沿用 9/3 停机脚本模式，模板化生成
`merge-20260926.sh`）。

## 3. 数据流 / 执行步骤

### T-1 天（不占停机窗口）

1. `git fetch upstream && git checkout -b dryrun-merge-20260926 main`
2. `git merge upstream/main` → 记录冲突文件清单（预计 15~20 个）。
3. SQLite 治理线逐文件函数级比对（见第 4 节），写取舍决策清单。
4. 非热点文件机械解冲突。
5. 提交合并（提交说明引用决策清单）。
6. dryrun 分支跑全量 pytest（挂后台/隔夜），与 9/3 基线（1728 passed / 1 上游
   自带失败）对比，新增失败逐个归因。
7. 基于 dryrun 分支生成 `merge-20260926.sh`（模板化 9/3 版）。

### T 日停机窗口（预计 8~12 分钟）

8. 前置检查工作区干净；`systemctl --user stop hermes-gateway`（注意：本机 zhihui
   profile 的 gateway 跑在**默认名字**的 `hermes-gateway.service` unit 下，
   见第 6 节环境备注）。
9. 聚焦套件验证：`tests/gateway/test_session_hygiene.py` + `tests/test_hermes_state.py`
   + `tests/hermes_state/` + `tests/cron/`（沿用 9/3 的 deselect 名单，仅当该上游
   自带顺序依赖测试仍存在时保留）。
10. `git merge dryrun-merge-20260926 --no-edit` 到 main。
11. `pip install -e ".[mcp,messaging,matrix]"`（上游若新增 py-modules 必做，
    用 `git diff 61f4985305 upstream/main -- pyproject.toml` 预查）。
12. 干净环境 import 冒烟（`env -u PYTHONPATH`）。
13. push origin + `systemctl --user start hermes-gateway` + journalctl 观察投递恢复。

## 4. SQLite 函数级取舍规则（核心）

对第 1.2 节热点文件，逐文件先读上游新版，再对照我方 diff
（`git diff 217ab2f8df..main -- <file>`）逐块判定为三档之一：

| 档位 | 判定条件 | 动作 |
|---|---|---|
| **upstream supersedes** | 上游已覆盖该行为（如 DELETE-journal 读者等待、损坏恢复、busy 预算语义） | 放弃我方版本 |
| **keep ours** | 上游未覆盖（事件循环阻塞卸载、housekeeping WAL 治理、send deadline、cron 共享 SessionDB） | 保留我方实现，移植到上游新架构 |
| **arch-diverged** | 架构层分叉（我方共享单写+只读池 vs 上游 `open_db` 层） | **默认取上游架构**，把我方行为改写成该层之上的开关/配置/调用点，不硬塞两套连接管理 |

arch-diverged 默认「取上游架构」的理由：上游的 open_db 层是未来所有修复的落点，
逆着它维护一套平行连接管理会在每次后续合并重复付架构冲突成本；我方行为（防阻塞、
治理调度）大多是策略而非机制，可以挂在机制之上。

### 4.1 决策清单模板（逐文件填写）

```
文件: <path>
我方行为: <一句话>
上游对应: <commit/行为/无>
档位: supersedes | keep | arch-diverged
落地: <放弃/保留在 <符号>/改写为 <上游接口> 的调用>
验证: <对应测试>
```

## 5. 错误处理与回滚

- **dryrun 全量出现非上游自带新失败** → 归因到具体取舍决策，回 dryrun 分支修，
  不进窗口。
- **窗口内聚焦套件失败** → 脚本自动 `systemctl --user start hermes-gateway` 回滚，
  main 不动（沿用 9/3 逻辑）。
- **gateway 重启后投递未恢复（10 分钟阈值）** → 保留 dryrun 分支，
  `git reset --hard 61f4985305` 回 main，回滚 gateway。
- **editable 冒烟失败** → 不 push，查 py-modules 映射，回滚 gateway。

## 6. 环境备注（本机实测）

- zhihui profile 的 gateway 实际跑在默认名字的 `hermes-gateway.service`（unit 内
  手写 `HERMES_HOME=.../profiles/zhihui`），`zhihui gateway stop` 找不到
  `hermes-gateway-zhihui.service` 会报 "No gateway running"。停机脚本直接用
  `systemctl --user stop/start hermes-gateway`，不依赖 `zhihui gateway stop`。
- 该 unit 带 64G MemoryMax drop-in 与代理环境，重启时随 unit 自动加载，无需额外处理。

## 7. 测试

- dryrun 分支全量 pytest（隔夜，CI-parity `scripts/run_tests.sh`）；
- 窗口聚焦套件 + import 冒烟；
- gateway 真实投递观察（重启后 10 分钟内 discord/feishu/api_server 恢复 connected）。

## 8. 不做的事（YAGNI）

- 不分段追上游 tag；
- 不在本次合并里把 `hermes-gateway.service` 改名为 `hermes-gateway-zhihui.service`
  （独立运维事项，单独处理）；
- 不处理工作区未提交脏文件（`graphify-out/GRAPH_REPORT.md`、`package-lock.json` 等），
  合并前置检查只要求 dryrun 分支基于干净的 main。
