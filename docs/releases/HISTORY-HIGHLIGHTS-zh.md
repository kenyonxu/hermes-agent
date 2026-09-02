# Hermes Agent 历史发布版本 · 亮眼特性清单（2026-09-01 知惠整理）

> 数据来源：`~/github/hermes-agent` 本地仓库 tag 注记（每个 release tag 的官方摘要）。
> 覆盖：v2026.3.12（首个 tag）→ v2026.8.31（最新 tag，**昨天发布**）。
> 「昨天」指 v2026.8.31 = **v0.21.0 The Pantheon Release**，详见第〇节。

## 〇、最新：v0.21.0「The Pantheon Release」（2026-08-31）⭐

- **Bot Mode 内置桌面端**：命名 agent 带形象（faces）、群聊、bot 之间私聊
- Cron 任务获得**持久记忆与连续性**；子代理支持**运行中实时转向**（live steering）
- MCP 面升级为指挥中心；agent 可驱动**应用内浏览器**
- 汇总并完整记录了 v0.20.1–v0.20.6 各 patch 窗口（合计 ~2,100+ PRs）

## 一、2026 年 8 月

### v0.20.6（2026-8-27）
- Rollup 补丁：~525 PRs / ~1,313 commits；完整策展 notes 归入 v0.21.0
- 含 gateway send deadline 修复（black-holed finals 不再冻结会话）、Bedrock cachePoint 拒绝自愈、desktop 下载失败原子保存

### v0.20.5（2026-8-19）/ v0.20.4（8-18）/ v0.20.3（8-16.2）/ v0.20.2（8-16）/ v0.20.1（8-13）
- 五个 rollup patch 合计 **~1,575 PRs / ~3,553 commits**
- 亮点散布：Telegram reply_to_mode finalize-edit 失败保单气泡、Windows Job-Object 跨重启存活、Claude Fable 5.1 入目录、delegate 子代理超时 FD 安全回收、startup route salvage 加固

### v0.20.0「The Herald Release」（2026-8-3）⭐
- **流式对话语音**（barge-in 抢话 + 唤醒词）
- **A2A v1.0**（agent-to-agent 协议）、**签名出站 webhook**、**grounded citations**（带引用的检索回答）
- 桌面平台浪潮：~3,650 commits / ~1,400 PRs / 650+ 贡献者

## 二、2026 年 7 月

### v0.19.1（7-30）
- 稳定 tag：把 v0.19.0 后 ~1,000+ PRs rollup 给下游消费者

### v0.19.0「The Quicksilver Release」（7-20）⭐
- **首 token 延迟（TTFT）全平台降 ~80%**；桌面速度浪潮（20+ perf PRs）
- 终端计费（/subscription、/topup）；**smart approvals 默认开启**
- **可插拔 SecretSource**（Bitwarden/1Password）；**live subagent transcripts**
- **durable delivery + delegation ledgers**；gateway profile 路由
- 新 provider（Fireworks/DeepInfra）+ 前沿模型（GPT-5.6、grok-4.5、kimi-k3、Claude Sonnet 5）
- ~2,245 commits / ~1,065 PRs / 420+ 贡献者

### v0.18.2（7-7.2）/ v0.18.1（7-7）
- WhatsApp Baileys 依赖修复等 hotfix

### v0.18.0「The Judgment Release」（7-1）⭐
- **P0/P1 backlog 100% 清零**（~692 项）
- **Mixture-of-Agents 成为一等可选模型**；agent 自验证（self-verification）
- /learn + /journey；scale-to-zero gateway；Google Vertex AI 接入

## 三、2026 年 6 月

### v0.17.0「The Reach Release」（6-19）⭐
- iMessage（Photon 桥）、Raft 频道、**异步子代理**、图像编辑
- Cursor Composer via xAI Grok；dashboard profile 构建器；记忆工具升级
- WhatsApp Business Cloud、富文本 Telegram、curator 成本优化

### v0.16.0「The Surface Release」（6-5）⭐
- **原生桌面 app** + 浏览器管理面板
- 远程 gateway connect；**简体中文桌面 UI**；精简默认技能集
- NVIDIA/skills trusted tap；fuzzy 模型选择器；/undo

## 四、2026 年 5 月

### v0.15.1（5-29）
- Dashboard 无限重载循环 hotfix + 一批加固（kanban worker SIGTERM、/yolo、Docker 加固）

### v0.15.0「The Velocity Release」（5-28）⭐
- **run_agent.py 16k → 3.8k LOC 神级重构**
- **Kanban 成长为多 agent 平台**（104 PRs）；**session_search 提速 4500×**
- promptware 防御；Bitwarden Secrets Manager；ntfy 成第 23 个平台
- 15 P0 + 65 P1 关闭；747 PRs / 321 贡献者

### v0.14.0「The Foundation Release」（5-16）⭐
- **Hermes 全平台可安装**：原生 Windows（早期 beta）+ PyPI wheel
- 冷启动性能浪潮；供应链加固；OAuth provider 本地 OpenAI 兼容代理
- 跨会话 Claude prompt cache；LINE + SimpleX（新平台）；/handoff live
- x_search、vision_analyze passthrough、LSP 诊断、computer_use cua-driver
- 12 P0 + 50 P1 关闭

### v0.13.0「The Tenacity Release」（5-7）⭐
- **「说到做到」**：durable multi-agent Kanban、/goal 持久目标、Checkpoints v2
- gateway 自动恢复；no_agent cron watchdog；8 项 P0 安全关闭；Google Chat（第 20 平台）

## 五、2026 年 4 月

### v0.12.0「The Curator Release」（4-30）⭐
- **自主后台 Curator + 自我改进循环大升级**（Hermes 会自己维护自己）
- 4 个新推理 provider；MS Teams + Yuanbao（18/19 平台）；Spotify/Google Meet；ComfyUI + TouchDesigner-MCP 内置

### v0.11.0「The Interface Release」（4-23）⭐
- **全新 Ink TUI**；可插拔传输架构；原生 AWS Bedrock
- GPT-5.5 via Codex OAuth；QQBot（第 17 平台）；dashboard 插件系统
- 1,556 commits / 761 PRs / 290 贡献者

### v0.10.0「Tool Gateway」（4-16）
- Nous Portal 订阅者直接复用订阅获得 web search / 图像生成 / TTS / 浏览器自动化

### v0.9.0「The Everywhere Release」（4-13）
- **Termux/Android 移动端**；iMessage + WeChat；Fast Mode（OpenAI/Anthropic）
- 后台进程监控；本地 web dashboard；16 平台深度安全加固

### v0.8.0「The Intelligence Release」（4-8）
- 原生 Google AI Studio；**live 模型切换**；自优化 GPT/Codex 指引；MCP OAuth 2.1；209 PRs

## 六、起点（2026 年 3 月）

### v2026.3.17「The streaming, plugins, and provider release」
- 统一流式输出、**插件架构落地**、原生 Anthropic provider、smart approvals、/browser CDP 连接、ACP IDE 集成、voice mode、PII 脱敏、持久 shell——248 PRs

### v2026.3.12「第一个正式 tag」
- 多平台消息 gateway（Telegram/Discord/Slack/WhatsApp/Signal/Email/Home Assistant）——216 PRs / 63 贡献者

## 七、彩蛋：三个命名规律

Hermes 的 release 命名是一条隐秘的时间线——**Foundation（打地基）→ Tenacity（说到做到）→ Curator（自己照顾自己）→ Velocity（提速）→ Reach（触达更多平台）→ Surface（浮出桌面）→ Judgment（清完旧账）→ Quicksilver（快银加速）→ Herald（语音传令）→ Pantheon（众神殿：多 agent 同堂）**。

从单 agent 到众神殿，半年走完。
