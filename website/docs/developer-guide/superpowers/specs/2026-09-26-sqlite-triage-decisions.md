# SQLite 函数级取舍决策清单 — dryrun-merge-20260926

- upstream tip（实测 MERGE_HEAD）: `138e33d51f`（计划编写时 `7b11c1bcac`，fetch 后以实测为准）
- merge-base: `217ab2f8df57`
- ours（HEAD / main tip）: `7a155dfca0`
- 取舍规则: spec §4（supersedes / keep / arch-diverged，arch-diverged 默认取上游架构）

**总体结论**：上游在 merge-base 之后把本仓 7–8 月的 SQLite 治理线整体吸收并演进为拆分模块体系
（`hermes_state_registry` / `hermes_state_wal` / `hermes_state_readpool` / `hermes_state_holders` /
`hermes_state_schema` / `hermes_cli.sqlite_util.open_db` / `gateway/session_persistence` /
`hermes_cli/kanban_db_connect` 等，代表提交 `576accd92b` open_db 统一层、`db339f0051` 共享写者
registry 化）。我方 12 个热点文件的全部 SQLite 治理符号在上游均有等价或超集实现；仅两项
我方独有行为无上游对应，作为调用点/包装层保留。**逐块判定结果：12 文件中 9 个整文件
supersedes、2 个含 keep 块、1 个 arch-diverged；10 个文件与上游字节一致。**

档位分布：supersedes 9 文件（另 2 文件内含 keep 块）／ keep 2 块 ／ arch-diverged 1 文件。

---

## 1. hermes_state.py（arch-diverged → 取上游架构）

- 我方行为: 共享单写 `get_shared_session_db`/`_shared_writer`（仅 frozen default path 走单例）、
  `get_read_only_session_db` 轮询只读池、`_initialized_dbs` per-db_path schema 缓存、
  全局 busy_timeout 30s、WAL→DELETE 策略（`resolve_journal_mode` 读 `database.journal_mode`）、
  in-file WAL 辅助（`apply_wal_with_fallback`/`_apply_wal_size_limit`）、
  `AsyncReadOnlySessionDB`（无消费方）。
- 上游对应: `hermes_state_registry.py`（`acquire`/`release`/`release_or_close`，按 resolved path
  引用计数 + inode generation 退休，`db339f0051`）；`SessionDB._read_pool` + `hermes_state_readpool`
  （有界池 + permit，WAL-only 读路径分流）；`hermes_state_wal.py`（`resolve_journal_mode` 读
  `database.journal_mode`、`apply_wal_with_fallback` 带网络盘回退/防降级闸/WAL-reset-bug 闸）；
  busy 预算按上下文分治（读 `_READ_BUSY_TIMEOUT_S=5s`、写连接 1s + patience 循环、账本 10s），
  取代我方一刀切 30s；registry 单实例 per path 自然使 `_initialized_dbs` 缓存失去意义。
- 档位: **arch-diverged**（8 个冲突块全部取上游；解决后经 `git checkout --theirs` 落为与上游
  `138e33d51f:hermes_state.py` 字节一致——手解残留的 `_initialized_dbs` 与 `_insert_session_row`
  旧 in-file 副本均为被拆分模块取代的内容，一并放弃）。
- 落地: 全取上游；`AsyncReadOnlySessionDB`（main 上无任何代码消费方，仅 7/24 计划文档提及）
  不移植——若未来需要 async 只读包装，应在 registry 之上薄封装。我方调用方（gateway/session、
  cron/scheduler、channel_directory）全部改经 registry `acquire`/`release_or_close`（上游已改好）。
- 验证: `tests/test_hermes_state.py` + `tests/hermes_state/`（上游 171 提交自带）；合并提交后
  dryrun 全量。

**schema 耦合提示（不改已合并文件，仅记录）**: 我方 hermes_state.py 曾从
`hermes_state_schema` 导入 `_FTS_BASE_TRIGGERS/_FTS_TRIGRAM_TRIGGERS` 供 in-file
`_init_schema` 使用；取上游后该导入随 in-file `_init_schema` 一并移除，`_init_schema` 现居
`hermes_state_schema.py` mixin（自动合并干净，两 trigger 常量仍在该文件内定义并使用）——
无语义缺口，无需动作。

## 2. gateway/run.py（supersedes）

- 我方行为: 两处净改动——(a) 注释掉 kanban notifier/dispatcher watcher 启动（理由: 本地未用
  kanban，且 notifier 独立 kanban.db 连接在 DELETE 模式下与 state.db 争锁）；(b) 压缩超时
  用户提示消息改为 elapsed/ceiling/距上次模型输出的秒数。
- 上游对应: (a) watcher 启动迁至 `gateway/run_startup.py` 的 `_PRE_RECONNECT_WATCHERS` 表驱动，
  且各自带配置闸（`kanban.notify_in_gateway`、`kanban.dispatch_in_gateway`——禁用走 config.yaml，
  正是 arch-diverged 规则里"我方行为改写为层上开关"的落点）；争锁前提已失效（上游 WAL 优先 +
  registry，kanban_db 亦有共享 open 路径）。(b) 超时提示重构为
  `_hygiene_compression_timeout_message(total_exhausted, elapsed, idle_timeout, progress_observed)`
  + commit-fence 取消/adopt 语义，信息量是我方改法的超集。
- 档位: **supersedes**（2 块均取上游）。
- 落地: 与上游字节一致。本地"禁用 kanban watcher"意图改由 config.yaml
  （`kanban.dispatch_in_gateway: false` / `kanban.notify_in_gateway: false`）表达，不改代码。
- 验证: 上游 tests/gateway/（kanban watchers、hygiene）＋ dryrun 全量。

## 3. cron/scheduler.py（supersedes）

- 我方行为: run_job 内改用 `get_shared_session_db()` 单例（有界初始化超时），清理处不再
  per-job `close()`（单例进程级所有）。
- 上游对应: `_open_cron_session_db(job)` 经 `hermes_state_registry.acquire()`（超时界定的
  worker 线程 + #72782 迟到结果关闭回调全保留），且额外复制 contextvars 使多 profile tick
  各自解析 home——比我方单例更正确；清理处 `release_or_close(_session_db)` 即"不切断共享写者"
  的 registry 表达。
- 档位: **supersedes**（2 块取上游）。
- 落地: 与上游字节一致。
- 验证: `tests/cron/`（上游 169 提交自带）＋ dryrun 全量。

## 4. gateway/platforms/base.py（supersedes + 1 keep）

- 我方行为: (a) `_send_with_retry` 内四处发送点经 `_send_with_deadline`
  （`_send_deadline_seconds`，默认 60s，`send_timeout_seconds` 平台覆盖；黑洞化发送不得
  钉死会话——2026-08-26 Discord blackhole 事故线，我方提交 `478805be05`，**未进上游**）；
  (b) 280 行 in-line 交付账本 bracket（发送前 record_obligation、mark_attempting、
  事后 mark_delivered/mark_failed、A3 空投递 loud-failure）。
- 上游对应: (a) 无等价（上游 base.py 无 send deadline；`asyncio.wait_for` 仅用于 typing/
  history/media 查询等）。(b) 全套迁入 `_send_final_ledgered` + `_record_delivery_obligation`
  + `_finalize_delivery_obligation` + `_deliver_attachments`（A3 守卫在内），且是我方超集:
  `ledger_message_id` 队列链身份、reconnect 触发 redeliver sweep、flood 惩罚定时重投
  （#91653）、`asyncio.to_thread` 卸载账本读写。
- 档位: (b) **supersedes**；(a) **keep**。
- 落地: 重试环与交付路径整体取上游；send deadline 改挂为层上调用点——(1) 上游 `_send`
  closure 内改走 `_send_with_deadline`（首发、重试 `_send_again`、失败通知三路全覆盖）；
  (2) 新增 `_await_send_deadline(coro, chat_id)`（从 `_send_with_deadline` 提取 wait_for 包络，
  timeout→不可重试 SendResult 的映射不变），用于包住上游可覆写的 `_send_plain_fallback`
  协程——保住"平台可覆写 fallback"的同时不让 fallback 裸奔。计 diff 78 行，全部为我方
  deadline 线。
- 验证: `tests/gateway/test_send_deadline.py`（我方 `478805be05` 配套，契约"every send attempt
  is bounded by `_send_deadline_seconds()`"在我方挂载下仍成立）——**本机 runner 因无关插件
  依赖问题未能执行（见报告 concerns），合并后 dryrun 全量必跑**。

## 5. gateway/channel_directory.py（supersedes + 1 keep）

- 我方行为: (a) `_get_cached_session_db` 模块级缓存只读 SessionDB（每 profile 一连）；
  (b) `build_channel_directory` 重入闸（`_channel_dir_building`，防三处触发点重叠构建）。
- 上游对应: (a) `_build_from_sessions_db` 经 `hermes_state_registry.acquire()/release_or_close`
  ——连接复用目标一致且 profile 切换不失效（我方缓存首开后钉死，有跨 profile 陈旧风险）；
  (b) 无等价（上游同样三处触发: run_startup / run_adapters / run.py housekeeping）。
- 档位: (a) **supersedes**；(b) **keep**。
- 落地: 缓存删除；重入闸保留为上游函数之上的薄包装（`_build_channel_directory_impl` 拆分
  结构不变，docstring 用上游精简版）。计 diff 20 行。
- 验证: `tests/gateway/test_channel_directory.py`（上游现存）应不受影响；闸行为无既有专测，
  依赖 dryrun 全量回归。

## 6. gateway/session.py（supersedes）

- 我方行为: in-file `_open_session_db_for_active_scope`（frozen default path 走共享单例、
  其余 per-profile handle）、`_db` property、`close_all_db_handles`。
- 上游对应: 全部迁入 `gateway/session_persistence.py` mixin（`SessionPersistenceMixin`），所有
  path 一律 `hermes_state_registry.acquire(path)`（"process-wide registry: one writer per path"），
  live-system guard 不可缓存语义、pinned-override 测试语义、shutdown 全 handle 清扫均保留。
- 档位: **supersedes**。
- 落地: 与上游字节一致（mixin 文件自动合并干净，SessionStore MRO 已接）。
- 验证: `tests/gateway/test_session_hygiene.py` 等聚焦件 + dryrun 全量。

## 7. gateway/delivery_ledger.py（supersedes）

- 我方行为: 手写 `_connect`（`timeout=30` 连 shared state.db + 失败关连接）。
- 上游对应: `open_db(_db_path(), db_label="state.db (delivery_ledger)", busy_timeout_ms=10_000,
  row_factory=None, initialize=...)`——"SessionDB owns the durable PRAGMA set" 的分层表述；
  close-on-failure 由 open_db 内建。
- 档位: **supersedes**。
- 落地: 与上游字节一致；我方 30s 连接超时由上游统一的 10s ledger busy 预算取代。
- 验证: `tests/gateway/test_delivery_ledger*.py` + dryrun 全量。

## 8. gateway/platforms/api_server.py（supersedes）

- 我方行为: ResponseStore 连接 journal 保持原样（2026-08-13 fix log：切换日志模式取排它锁）。
- 上游对应: `apply_wal_with_fallback(conn, db_label="response_store.db")`——唯一带 WAL-reset
  闸/网络盘静默回退/不降级不变量的 setter，init 时一次执行。
- 档位: **supersedes**。
- 落地: 与上游字节一致。
- 验证: `tests/gateway/test_api_server.py` + dryrun 全量。

## 9. hermes_cli/kanban_db.py（supersedes）

- 我方行为: 1601 行 in-file 连接/治理栈（`_resolve_busy_timeout_ms` 默认 120s、跨进程 init 锁、
  dispatch tick 锁、WAL checkpoint 节流、损坏隔离/备份/索引修复、repair_db、迁移、
  busy 重试 write_txn）。
- 上游对应: 整栈迁入 `hermes_cli/kanban_db_connect.py`（docstring 同源），符号 diff 证明为
  我方集合的严格超集（另加 `_try_lock_nb`/`_unlock`/`_backup_label`/`_probe_integrity`/
  `_probe_for_corruption`/`_backfill_legacy_runs` 等）；`connect` 改支持 `board=`。
- 档位: **supersedes**。
- 落地: 与上游字节一致（kanban_db_connect.py 自动合并干净并已由尾部 import 重导出）。
- 验证: 配对测试 `tests/hermes_cli/test_kanban_db.py`（在 cold 清单，Task 4 处理时连带确认）。

## 10. cron/notepad.py（supersedes）

- 我方行为: `_initialize_schema` 自设 row_factory + busy_timeout=5000，journal 不动。
- 上游对应: open_db 统一 PRAGMA 栈（busy_timeout_ms/row_factory/WAL fallback + 短锁重试），
  我方 2026-08-13 的"切换锁他连接"顾虑由 `wal_lock_retries` + 硬化的 fallback setter 覆盖。
- 档位: **supersedes**。
- 落地: 与上游字节一致。
- 验证: `tests/cron/` + dryrun 全量。

## 11. cron/executions.py（supersedes）

- 我方行为: `_initialize_schema` 自设 row_factory/busy_timeout/synchronous=FULL。
- 上游对应: open_db(..., synchronous_full=True) kwarg + 统一 PRAGMA 栈；init 内改为引入
  `add_column_if_missing` 迁移助手。
- 档位: **supersedes**。
- 落地: 与上游字节一致。
- 验证: `tests/cron/` + dryrun 全量。

## 12. agent/verification_evidence.py（supersedes）

- 我方行为: 手写 `_connect`（connect(timeout=30) + busy_timeout=5000 + 失败关连接）。
- 上游对应: open_db（父目录创建、PRAGMA 栈、initialize、失败即关全内建）；我方 timeout=30
  本就被自身 PRAGMA busy_timeout=5000 覆盖，属死参数。
- 档位: **supersedes**。
- 落地: 与上游字节一致。
- 验证: `tests/agent/`（verify-on-stop 相关）+ dryrun 全量。

---

## 附：验证与执行状态

- 12 文件冲突标记清零，`ast.parse` 全部通过（brief Step 3 输出 `hot files parse OK`）。
- 12 文件全部 `git add`（unmerged 计数 38 → 26，剩余为 cold/重命名冲突，未触碰）。
- 与上游 `138e33d51f` 逐文件比对：10 个字节一致；`gateway/platforms/base.py`（+78 行，send
  deadline keep）与 `gateway/channel_directory.py`（+20 行，重入闸 keep）为仅有的我方保留。
- 定向测试（test_send_deadline.py 等）本机未能执行：`scripts/run_tests.sh` 的 venv 引导被
  既有工作区插件 `hermes-plugin-superlocalmemory`（要求 mslm-memory>=4.1.0 不可得）阻断，
  且本 checkout 无 .venv/venv——与本次取舍无关的 pm 状态问题；运行时验证按计划落在合并
  提交后的 dryrun 全量（Task 5 之后）。
- 禁 commit/push 遵守：本清单仅 `git add -f` 暂存，提交由 Task 5 统一执行。

---

## 附二：验证与执行记录（合并后补记，2026-09-25 晚）

> 上节"附"写作于冲突解决现场；本节为合并全流程结束后的最终记录，两节并存以保留过程视角。
> "定向测试本机未能执行"的顾虑已被消除（见下）。

### 全量验证（最终树 e54b5584fa）

- 全量 pytest（canonical runner，`HERMES_PYTHON=miniconda3` 旁路）：5,023 文件 /
  **51,785 passed / 70 failed** / 644 skipped（3511s，24 workers）。
- 70 个失败逐文件归因后全部落在 62 文件"归因允许清单"内：42 upstream-inherent（本机
  py3.13.12 vs 上游运行时硬性 `==3.14.*`，含 nemo-relay 版本锁簇）、12 machine-local（conda
  变量泄漏 / 代理窗口 / 宿主组件版本；其中 test_failure_writer_ownership 为探针子进程被
  本机回环代理窗口卡死——探针逻辑 20/20 过，非断言失败）、6 order-dep、2 上游 FLAKY。
- **生产代码零回归**：合并树相对上游 138e33d51f 的 .py 净差异仅两个 keep 薄层 + 我方独有
  测试/脚本；keep 层定向测试全绿（send_deadline 5✓、event_loop_blocking 1✓）。
- 3 个 merge-regression 均为测试侧（取舍只落了生产文件、fork 配对测试未同步），已以提交
  `e54b5584fa` 同步：scheduler 全取上游、housekeeping 删（上游 test_wal_checkpoint_strategy
  继任）、shared_session_db 删（上游 test_shared_session_db_registry 18 测试等价更强覆盖）。

### 窗口执行（3 跑 2 回滚）

- run1：`run_tests.sh` 不透传 `--deselect` → 回滚；run2：pip build isolation 经代理拉
  setuptools 撞 SSL 抖动 → 改 `--no-deps --no-build-isolation` 后回滚；run3：聚焦套件
  278 文件零失败、合并、editable、冒烟、push origin 成功（`61f4985305..e54b5584fa`）。
- run3 尾部：重启后 5s 时 unit 尚 activating 被脚本误判 → 本地回滚；手动恢复 main 后撞出
  **上游 v0.21.x 多路复用模型拒独立 unit（exit 78）**，按官方兼容路径为 zhihui profile 配置
  `gateway.standalone: true` 后全绿：pid 1093284，code_sha `e54b5584fa`，版本 0.20.6 →
  **0.21.5**，discord/feishu/api_server 三平台 connected。

### 合并后跟进（已落地部分）

- **Telegram `_resume_partial_send` deadline 包裹**（上游新增路径，我方 send-deadline 治理的
  最后一个未覆盖分支）：`_send_again` 的 resume 分支现受 `_send_deadline_seconds` 约束；超时
  结果**保留 partial_overflow 记录**（raw 附 `resume_timed_out` 标记）且错误串走 timeout 通道
  ——避免 wait_for 取消导致 partial 簿记丢失后、重试/补投整包重发复制已见头部。契约测试
  `tests/gateway/test_send_deadline.py` 增 resume 悬挂/正常完成两用例（7/7 绿）。
- **重入闸并发专测**：`tests/gateway/test_channel_directory.py::TestReentrancyGate` ——重叠
  build 立即返回 `{}`、不进 impl、不写 DIRECTORY_PATH；完成后闸门复位（22/22 绿）。
- `tests/hermes_state/test_hermes_state.py` 未用 `patch` 导入已删（274 绿）。

### #33159 FD-leak 覆盖：关闭（不补测）

事实核验：上游 `tests/hermes_cli/test_kanban_db.py:1943-1950` 的 #33159 注释块是**孤儿注释**
（其后无任何 connect_closing 断言用例）；我方被删旧测试也只"文档化泄漏"（docstring 自认
"upstream behaviour we cannot change"，钉的是 sqlite3 内建 context-manager 不关连接的行为）。
对他人运行时的固有行为钉不变量属 change-detector，无 Hermes 自有不变量可守——**关闭，不补测**。

### 环境遗留（2026-09-25 深夜更新：前两项已根治）

- ~~PM workspace mslm 镜像缺口~~ → **已修（但修法与初判不同）**：初判"pm repair 根治"有误——
  真因是 superlocalmemory 插件把 `mslm-memory>=4.1.0` 声明进 pip_dependencies，而 mslm-memory
  任何已发布版本都硬钉 websockets==16.0 / mcp==1.x，与 hermes 钉的 15.0.1 / 2.0.0 根本冲突，
  PM 工作区解析永无解、连带堵死所有平台 extra 安装。正解：移除插件 pip_dependencies
  （插件是 daemon-first 设计，网关 env 不需要 mslm）。
- ~~42 个 upstream-inherent 失败的 py3.14 环境~~ → **已建**：PM 自带 python 3.14.7
  （`~/.hermes/tools/python-3.14.7+20260901-linux-x64/`），用 uv 建测试 venv
  `/tmp/hermes-test-314`（`uv pip install -e ".[mcp,messaging]" --group dev`；matrix extra
  因 python-olm 无 3.14 轮子且源码构建失败而去除）。canary 验证：relay 簇
  （test_relay_atof_cwd）与 pm 引导族（test_bundle_native 11✓）在该 venv 下全绿。
  用法：`HERMES_PYTHON=/tmp/hermes-test-314/bin/python3 scripts/run_tests.sh …`（venv 在
  /tmp，重启即失——建议后续固化到 ~/.hermes/tools 旁并由 CI/脚本重建）。
- `gateway.standalone: true` 是官方临时 shim，上游移除后需 `hermes gateway migrate
  --multiplex` 迁移拓扑。
