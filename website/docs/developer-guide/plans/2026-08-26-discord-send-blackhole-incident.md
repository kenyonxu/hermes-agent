# Diagnostic Record — Discord Session Freeze via Black-Holed Final Send (2026-08-26)

> **Surface**: Discord gateway session (`zhihui` profile)
> **Impact**: final reply of one turn silently lost; the chat session then froze
> for all subsequent messages until a gateway restart (~40 min user-visible)
> **Status**: unblocked by restart at 20:22; durable fixes NOT yet applied
> **Related**: 2026-08-22 watchdog restart storm (same flaky proxy line),
> 2026-08-13 post-merge fix log (delivery-obligation machinery history)

## Summary

At 19:41 the user asked the agent to pull the `fuse` project. The turn ran
normally (85.4s, 4 API calls) and streamed its tool-progress messages to
Discord. The **final 727-char response was never delivered**: its HTTP send
through the mihomo SOCKS line was black-holed (request out, response never
arrives), the awaiting coroutine hung **forever** (discord.py applies no total
timeout on this request), and — critically — that stuck coroutine kept the
session marked active. Every subsequent message from the user hit the
"interrupt current task" queueing path and was never processed. Both
delivery-recovery layers were inactive for this loss (obligation is written
only after successful send; missed-message backfill had an empty channel
scope). A gateway restart cleared the leaked coroutine; the queued message
was lost and needed a manual resend.

## Timeline (local CST)

| Time | Event | Evidence |
|------|-------|----------|
| 19:41:49 | Inbound `知惠~拉一下fuse工程…` | gateway.log `inbound message` |
| 19:42:20 | `MSLM engine init timed out after 30.0s — provider disabled` | errors.log (unrelated to the loss; turn continued) |
| 19:42:41–52 | Tool-progress messages delivered (main text + terminal blocks) | Discord REST channel fetch |
| 19:43:16 | `response ready … 727 chars` + `Sending response (727 chars)` | gateway.log — **last activity of the turn** |
| — | 727-char message never appears in the channel | Discord REST: last bot message is 19:42:52 |
| — | No `delivery_obligations` row created for this send | state.db: newest row is 18:17:54 (`delivered`) |
| 20:19:12 | User sends a 13-char message; batch flush logs, **no** `inbound message` line | gateway.log — adapter-level queueing, session-active guard |
| 20:19:12+ | User sees `⚡ Interrupting current task. I'll respond to your message shortly.` — which itself sends fine | Discord client |
| 20:22:04 | `systemctl --user restart hermes-gateway` | this session |
| 20:22:19 | Discord reconnects in 5s; all 3 platforms green | gateway.log |
| ~20:24 | **No missed-message recovery / backfill logs; ledger DB still absent** | gateway.log, `gateway/discord_message_recovery.db` missing |

## Diagnosis chain

1. **The gateway was alive and connected throughout.** Instantaneous CPU ~1%,
   websocket heartbeat traffic confirmed flowing via mihomo `/connections`
   counter deltas (+44B up / +121B down over 30s on the
   `gateway-us-east1-c.discord.gg` connection). The 08-22 "socket_closed"
   stale-websocket class was NOT in play.
2. **The final send was black-holed, not errored.** No exception in
   errors.log after 19:42:25, no retry, no obligation row. One aiohttp
   request through `socks5://127.0.0.1:7891` (mihomo `Proxy` group →
   🇹🇼 TW1 台湾_TR) simply never returned. discord.py's HTTP client does not
   bound this wait, so the coroutine leaks permanently.
3. **The stuck coroutine held the session active.** The user's 20:19 message
   reached the adapter (batch flush logged) but never produced an
   `inbound message` line — it took the `_pending_messages` queueing path
   guarded by `session_key in _active_sessions`, with the "Interrupting
   current task" notice sent to the user. Queue processing waits for the
   current task to finish; it never does. **Session frozen process-wide.**
4. **Correction of an initial misread:** `gateway_state.json` showed
   `active_agents: 0`, which first suggested the turn had cleaned up. That
   value was stale (written at process start 08:39). The "⚡ Interrupting"
   reply proved the in-process active-session set was still held. Lesson:
   treat `gateway_state.json` runtime fields as point-in-time, not live.
5. **Restart cleared it.** Process replacement killed the leaked coroutine;
   Discord reconnected in 5s. The 08-22 watchdog fix (3-consecutive-miss
   before restart) behaved — no restart loop during or after.

## Why both recovery layers failed

### 1. Delivery obligations are written only after success

`delivery_obligations` rows for healthy turns are created and marked
`delivered` within ~1s of `Sending response` (observed: attempts=0, state
transition immediate). The black-holed send never reached the ledger write,
so nothing existed for the obligation-recovery worker to adopt. Compare the
2026-08-13 addendum incident, where a row WAS created (stuck in
`attempting`) and a restart recovered it — the machinery exists but was
never engaged here. **Write the obligation BEFORE attempting the send.**

### 2. Missed-message backfill had an empty scan scope

`plugins/platforms/discord/adapter.py`:

- `_missed_message_backfill_channels()` (line ~2493) derives the scan set
  from `allowed_channels` ∪ `free_response_channels` (config) when neither
  `missed_message_backfill.channels` nor
  `DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS` is set.
- Profile config has `allowed_channels: ''` and
  `free_response_channels: ''` → **empty scan set → nothing can be
  recovered**, regardless of the ledger.
- The admission gate reads the same keys with the opposite semantics:
  empty string = "no restriction" (bot answers anywhere). Two readers, two
  meanings for the same empty value.
- Additionally, after the 20:22 restart the backfill task left **no log
  lines at all** — neither "durable ledger unavailable" nor "no channels
  configured" — and the lazy ledger file
  (`gateway/discord_message_recovery.db`, created on first
  `DiscordRecoveryStore.call()`) was never created. So the task likely never
  executed its first ledger touch. Root cause of the non-execution is
  unresolved (needs a focused pass; call site is `_ensure_missed_message_
  backfill_task()` from `connect()`, adapter.py ~line 1404).

## Recovered content

The lost 727-char response is intact in `state.db → messages` (assistant,
19:43:16, session for thread 1493514459812073512). The agent's own session
history also retains it, so asking the bot to "resend the conclusion"
works without any data restoration.

## Actions taken (2026-08-26 evening)

1. Diagnosed live (no changes until the restart): REST channel fetch with
   the bot token through the same proxy, mihomo `/connections` counter
   deltas, `delivery_obligations` and `messages` queries, gateway_state /
   health endpoint reads.
2. `systemctl --user restart hermes-gateway.service` at 20:22:04 — cleared
   the leaked coroutine; user instructed to resend the queued message.
3. This record.

No code or config changes were made during this incident.

## Durable fixes (proposed, none applied)

| # | Fix | Layer | Notes |
|---|-----|-------|-------|
| 1 | Total timeout (e.g. 60s) + retry around the adapter's final `send()` | hermes-agent (upstream-able) | **IMPLEMENTED 2026-08-28** — see update below |
| 2 | Write the delivery obligation BEFORE attempting the send | hermes-agent (upstream-able) | Pre-send recording already existed; the silent best-effort swallow was the gap. **IMPLEMENTED 2026-08-28** (visibility fix) |
| 3 | Align empty-string semantics of `allowed_channels`/`free_response_channels` between admission gate and backfill scope (or explicitly configure `missed_message_backfill.channels`) | local config + upstream discussion | Quickest local mitigation: list the active thread/channel IDs |
| 4 | Investigate why the backfill task produced zero logs after restart | hermes-agent | Possibly never scheduled on fresh connect |
| 5 | mihomo line-availability detection + switching (probe Discord through the current node; `PUT /proxies/Proxy` to switch; `DELETE /connections` to force the ws to re-dial) | local ops | **IMPLEMENTED 2026-08-28** — see update below |

## Update 2026-08-29 — fixes #1/#2 implemented; nightly mirror-sync wipe discovered

The black-hole recurred 2026-08-28 16:45 (second occurrence in two days,
this time on the TW4 line — line-independent, as predicted), after which
fixes #1/#2 landed. Code inspection refined the original plan:

- `_send_with_retry` (gateway/platforms/base.py) had NO deadline around any
  of its `await self.send(...)` calls — a hung request never raises, so
  neither the retry loop nor the delivery-failure notice could ever fire.
- Pre-send obligation recording (record_obligation → mark_attempting before
  the send await) already existed and is contract-tested
  (`tests/gateway/test_delivery_ledger_producer.py`). The gap: failures were
  swallowed at DEBUG, so a failed ledger write (likely state.db contention)
  stripped the send of its recovery net invisibly — both incidents' sends
  have NO obligation row.

Changes (all in `gateway/platforms/base.py`):

1. New `_send_deadline_seconds()` (default 60s; per-platform override via
   the `send_timeout_seconds` key in the platform config section, or the
   `_send_deadline_override` attr for tests) and `_send_with_deadline()`.
   All four send sites inside `_send_with_retry` are bounded. Deadline
   expiry maps to a non-retryable timeout failure whose error string
   matches `_is_timeout_error` and NOT `_RETRYABLE_ERROR_PATTERNS`, so the
   EXISTING timeout semantics apply: no retry (delivery state unknown), no
   plain-text fallback, turn ends, session released.
2. Obligation ledger failures now log at WARNING instead of DEBUG.

Regression tests: `tests/gateway/test_send_deadline.py` (5 tests). With the
producer and stream-contract suites: 18/18 green.

Also applied to the zhihui profile config (compression section):
`idle_compact_after_seconds: 1800` (idle-time compaction, supported but off
by default — a 325s blocking compression hit this thread at 16:22 on the
28th) and `protect_last_n: 20 → 10`.

**Nightly-wipe discovery (2026-08-29):** `git reflog` shows
`reset: moving to HEAD` every night at 02:30 — the crontab entry for
`~/.hermes/scripts/sync_github_mirrors.py` hard-resets the working tree
before mirroring, silently destroying uncommitted changes (it took the
unpushed #1/#2 fixes the first night; it is also the likely historical
killer of the never-committed `scripts/gateway_freeze_watchdog.sh`).
Lesson: in this repo, commit AND push before 02:30 or lose the work. The
mirror script itself needs a guard (skip reset on dirty tree / stash
first) — pending, owner's call.

**2026-08-29 11:22 recurrence, different layer:** first message of the
morning froze mid-send with NO obligation row, NO timeout error — the hang
is BEFORE the deadline-protected send, in the `asyncio.to_thread` ledger
write: errors.log shows the turn's superlocalmemory init thread wedged
("MSLM init thread did not terminate gracefully") seconds earlier, the
2026-08-13 SQLite global-VFS-mutex convoy class. The whole-process sqlite
convoy means the to_thread sqlite call never returns and nothing raises.
Restart clears it. Structural fix still lives in the slm repo.

## Update 2026-08-28 — line watchdog implemented (fix #5)

The predicted failure recurred the same morning: from ~08:33 the TW1 node
passed generic traffic (gstatic 204 in 0.7s) while timing out on Discord
(5s), and the gateway burned an hour in reconnect backoff + watchdog
restarts. Manual switch to 🇹🇼 TW4 台湾_HY2 restored Discord in 4s. The
auto-switch mechanism was then implemented and deployed:

- **Script**: `~/.hermes/profiles/zhihui/scripts/discord-line-watchdog.sh`
  (systemd: `hermes-discord-line-watchdog.timer`, every 2 min).
- **Probe matrix**: Discord `GET /api/v10/gateway` via the SOCKS port ×3
  (first success = healthy; ≥2/3 fail = line fault) with a generic-URL
  control probe — if both paths are down it logs SKIP (switching cannot
  help) and leaves the gateway watchdog in charge.
- **Selection**: `GET /group/Proxy/delay?url=discord.com`, excluding
  nested groups (Auto's gstatic health check is exactly the blind spot),
  DIRECT/REJECT, the current node, and nodes tried in the last 30 min
  (state file prevents flip-flopping between two bad nodes; both old and
  new nodes are recorded on each switch).
- **Redial**: after a verified switch it DELETEs ONLY the Discord
  connections (`/connections/{id}` filtered by host) so discord.py's own
  reconnect lands on the new line within seconds — other proxied traffic
  (e.g. in-flight LLM API calls) is never touched.
- **Verified**: bash -n; live healthy-path run; mock-mihomo test of the
  full switch branch (ranking/exclusion, PUT payload, tried-file writes,
  targeted DELETE hitting only the Discord connection); two live timer
  ticks logging OK. Env overrides (`LINE_WD_*`) make every branch
  testable without touching the live proxy.

Remaining known gaps (deliberate, for now): single switch per run (next
candidate on the next tick, ~2 min later); no quota/expiry awareness
(nodes are inlined, so mihomo exposes no `subscriptionInfo`); TW1 is
eligible again after the 30-min TTL, which is intended (its failures are
intermittent, not permanent).

## Related prior incidents

- **2026-08-22 01:03–09:13 — watchdog restart storm (216 restarts).** The
  external `discord-watchdog.sh` restarted the whole gateway on a single
  missed TCP check, interrupting the agent's own reconnect backoff
  (30/60/120s) after a stale-websocket event; the proxy line was resetting
  TLS handshakes ~2/3 of attempts that morning. Fixed by rewriting the
  watchdog to require 3 consecutive misses (~6 min) with a state-file
  counter (`~/.hermes/profiles/zhihui/scripts/discord-watchdog.sh`), and by
  removing the stale crontab entry pointing at the never-committed
  `scripts/gateway_freeze_watchdog.sh` (backup:
  `~/.hermes/scripts/crontab.backup-20260822`). Verified live; behaved
  correctly during tonight's restart.
- **2026-08-13 — delivery obligation stuck in `attempting`** (superlocalmemory
  WAL-close global-mutex deadlock addendum): a response was generated but
  never delivered while `/health` stayed 200; restart adopted the orphaned
  obligation. Same "final delivery is the fragile step" theme as tonight.

## Environment notes (for future incidents)

- mihomo v1.19.28, `external-controller: 127.0.0.1:9090` (no secret),
  HTTP 7890 / SOCKS 7891; rules send all Discord domains to the `Proxy`
  **select** group (manual choice, currently 🇹🇼 TW1 台湾_TR); an
  `Auto | PandaFan.sh` url-test group exists but health-checks against
  gstatic, which does not reflect Discord-path health. Nodes are inlined
  (no subscription provider), so mihomo's API exposes **no traffic-quota
  information**.
- Useful live checks proven this incident:
  - channel truth: `GET /channels/{id}/messages` with the bot token via
    `curl --socks5-hostname 127.0.0.1:7891`;
  - ws liveness: mihomo `GET /connections` counter deltas on the
    `gateway-*.discord.gg` connection;
  - delivery state: `delivery_obligations` in
    `~/.hermes/profiles/zhihui/state.db` (columns: `state`, not `status`);
  - `gateway_state.json` runtime fields are point-in-time snapshots.
