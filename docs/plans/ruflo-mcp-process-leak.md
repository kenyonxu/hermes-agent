# MCP Client ruflo 进程泄漏 — 问题记录

> 发现时间: 2026-07-08 08:00
> 影响: 凌晨至早晨，ruflo npx 进程大量堆积消耗 CPU
> 状态: 已临时关闭 ruflo MCP server

## 现象

凌晨开始，Hermes MCP client 反复尝试连接 ruflo MCP server 失败，每次重试启动新 `npx ruflo@` 进程但不回收旧进程。journalctl 记录从凌晨到 08:18 刷了 **15355 条**失败日志。systemd 重启 gateway 时 SIGKILL 了 6 个残留进程，另有 3 个 "remains running after unit stopped"。

## 根因

MCP client 初始连接重试机制缺陷：
- 连接失败 → 1-4 秒后重试，每次 fork 新 npx 进程
- 旧进程从不回收，瞬时堆积数百个 npx 实例
- "unhandled errors in a TaskGroup (1 sub-exception)" 是标准错误

## 临时缓解

1. 从 `config.yaml` MCP servers 中注释掉 ruflo 条目
2. 仅 "disabled" 可能仍触发初始连接尝试

## 待查

- 凌晨触发 MCP 重连的具体事件（gateway 重启？cron 触发了 MCP tool？）
- 上游是否有 MCP 进程生命周期管理相关的 issue/PR
