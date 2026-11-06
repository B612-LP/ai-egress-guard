# ai-egress-guard — 给 AI Agent 的使用说明

你（AI Agent）被要求用这个工具帮用户解决「ChatGPT / Codex 上不去、转圈、报 403、换节点也不知道哪个能用」这类问题。
本文件是**操作契约**：照着做，别自己发明探测逻辑。

> 2026-11-03 起核心重写为 Python（`ai-egress-guard.py`，纯标准库，约 660 行）；`ai-egress-guard.sh` 只是稳定入口壳。
> 旧版 bash 实现见 git 历史首个提交（`git show 4b239f9:ai-egress-guard.sh`）。

## 0. 一句话

每 10 秒一轮：**探当前出口（只认 HTTP 401/200）→ 坏了先对候选逐个粗筛（不切组）→ 只对粗筛通过者短暂切组真探**，成了就留在那个节点；全挂就恢复起始节点并进入 120s 冷静期。另有 60s 一轮的后台待命侦察（维护备用候选缓存）。状态写进独立 JSON，供菜单栏 / 其它脚本读取。

**它验证的只是「出口可达性」**（HTTP 401/200），不等同于模型可用、账号没限流、长流不断。别把它当健康检查的万能答案。

## 1. 先确认前提（缺一不可）

```sh
# a. Clash Verge / mihomo 在跑，且控制接口 socket 存在
ls /var/folders/*/*/T/verge-mihomo.sock

# b. 混合代理端口通
curl -sS -m 3 -x http://127.0.0.1:7897 -o /dev/null -w '%{http_code}\n' https://chatgpt.com/backend-api/me

# c. 代理组名对得上（默认 AI-AUTO）
curl -sS --noproxy '*' --unix-socket "$(ls /var/folders/*/*/T/verge-mihomo.sock | head -1)" \
  -H "Authorization: Bearer ${CLASH_API_SECRET:-set-your-secret}" \
  http://localhost/proxies | python3 -c 'import json,sys;print([k for k in json.load(sys.stdin)["proxies"] if "AUTO" in k.upper()][:20])'
```

任一项不满足 → 先报给用户，别继续。**不要**自己改用户的 Clash 配置、不要重启 Clash、不要替用户选节点。

## 2. 两条命令就够

```sh
# 只读巡检：不切组、不改选择，只报结论（排查时先用这个）
bash ~/.dsh/bin/ai-egress-guard.sh --check-only
cat ~/.dsh/state/ai-egress.json

# 完整守护（可切组）：用户明确说「帮我切/救一下 GPT」才用
bash ~/.dsh/bin/ai-egress-guard.sh
```

launchd 已在常态运行（`com.b612.ai-egress-guard` 10s 一轮、`com.b612.ai-egress-scout` 60s 一轮），所以多数时候**直接读状态文件即可**，不必手动跑。

## 3. 读状态，别读日志

状态文件 `~/.dsh/state/ai-egress.json`（字段：`version` / `status` / `node` / `checked_at` / `success_at`）：

| status | 含义 | 你该做什么 |
|---|---|---|
| `healthy` | 当前出口探测通过 | 报告 node 名，结束。别再说「梯子坏了」 |
| `network_unknown` | 基础网络都不通，**没动节点** | 查 WiFi/校园网/热点，别怪 Clash |
| `scanning` | 正在扫描 | 等 30s 再读一次 |
| `unavailable` | 候选全没通过，已恢复原节点，120s 冷静期 | 等 `checked_at + 120` 之后再看；不要立刻重跑刷它 |
| `probe_failed` | `--check-only` 模式下探测失败 | 说明出口确实不通，可建议用户跑完整模式 |
| `control_error` | Clash 控制接口异常 | 查 socket 路径 / `CLASH_API_SECRET`，别猜 |
| `selection_changed` | 用户手动换过节点 | **停手**，尊重用户选择 |
| `restore_failed` | 想回滚但失败 | 明确告警，让用户手动看 Clash 面板 |
| `interrupted` | 被中断 | 同上，重跑一次即可 |

`success_at` 是最近一次 healthy 的时间戳；`checked_at` 是本轮时间。**判断新鲜度用这两个字段，不要用文件 mtime。**

备用候选缓存：`~/.dsh/state/ai-egress-standbys.json`（侦察产出，含粗筛延迟），可作为「接下来该试谁」的参考。后台最多维护两个备胎，定时复测现有备胎，失败复测确认、不足才分批补选；优先另一订阅，找够即停。订阅归属由 `~/.dsh/state/ai-egress-subscriptions.json` 显式映射节点名到订阅 ID，未知时不猜。后台不切换健康出口；粗筛通过不代表账号可用。

## 4. 常见对话怎么答

- 「我 GPT 用不了了」→ 读状态文件（或 `--check-only`）→ `healthy` 就是出口好，问题在账号/模型/额度，别甩锅网络。
- 「帮我换个节点救一下」→ 完整模式跑一遍，把状态文件的 `status` + `node` 念给用户。
- 「哪个节点能用」→ 跑完整模式，它只会在真正通过探测时切，切完就停在能用的节点上。
- 没有 Clash（纯裸网）→ 这个工具不适用，直说。

## 5. 硬约束

1. **只读优先**：不确定时先读状态文件或跑 `--check-only`，任何切组行为都要有用户意图依据。
2. **不改用户配置**：只通过 Clash 的 `/proxies/<group>` PUT 切组，绝不写 `config.yaml`、不重启进程、不动 TUN。
3. **不并发跑**：脚本自带 fcntl 锁，重复启动会直接退出；别绕过。
4. `selection_changed` / `restore_failed` 出现时**停止自动操作**并把原文状态转述给用户。
5. 节点名对不上时只报错，不要猜名字、不要用 `DIRECT` 兜底。

## 6. 配置速查

控制器密钥使用环境变量，订阅归属使用上面的本地 JSON 文件；其余是 `ai-egress-guard.py` 顶部常量（改常量 = 改代码，改完 launchd 下轮自动生效）：

| 项 | 值 | 说明 |
|---|---|---|
| `CLASH_API_SECRET`（环境变量） | `set-your-secret`（占位默认值） | Clash 外部控制器密钥，**部署时必须改** |
| `GROUP` | `AI-AUTO` | 要守护的 Clash 代理组 |
| `URL` | `https://chatgpt.com/backend-api/me` | 出口探针（401/200 都算通） |
| `PROXY` | `http://127.0.0.1:7897` | 混合代理端口 |
| `TTL` / `REFRESH` / `COOLDOWN` | 120 / 60 / 120 | 健康保鲜 / 后台刷新 / 全挂冷静期（秒） |
| `BACKGROUND_CONCURRENCY` / `RECOVERY_CONCURRENCY` | 2 / 6 | 侦察 / 恢复扫描并发 |
| 状态文件 | `~/.dsh/state/ai-egress.json` | 只读消费 |
| 日志 | `~/Library/Logs/ai-egress-guard.log` | 仅人看 |

退出码恒为 0（守护脚本不该让 launchd 反复报警），**结论一律看状态文件**。
