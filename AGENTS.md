# ai-egress-guard — 给 AI Agent 的使用说明

你（AI Agent）被要求用这个工具帮用户解决「ChatGPT / Codex 上不去、转圈、报 403、换节点也不知道哪个能用」这类问题。
本文件是**操作契约**：照着做，别自己发明探测逻辑。

## 0. 一句话

一个 bash 脚本，每轮做三件事：**探当前出口 → 不行就逐节点粗筛 → 只对通过粗筛的节点短暂切组真探**，成功了就留在那个节点，全挂就恢复原节点并进入 120s 冷静期。
状态写进独立 JSON 文件，供菜单栏 / 其它脚本读取。

**它验证的只是「出口可达性」**（HTTP 状态 200/401），不等同于模型可用、账号没限流、长流不断。别把它当健康检查的万能答案。

## 1. 先确认前提（缺一不可）

```sh
# a. Clash Verge / mihomo 在跑，且控制接口 socket 存在
ls /var/folders/*/*/T/verge-mihomo.sock

# b. 混合代理端口通
curl -sS -m 3 -x http://127.0.0.1:7897 -o /dev/null -w '%{http_code}\n' https://chatgpt.com/backend-api/me

# c. 代理组名对得上（默认 AI-AUTO）
curl -sS --noproxy '*' --unix-socket "$(ls /var/folders/*/*/T/verge-mihomo.sock | head -1)" \
  http://localhost/proxies | python3 -c 'import json,sys;print([k for k in json.load(sys.stdin)["proxies"] if "AUTO" in k.upper()][:20])'
```

任一项不满足 → 先报给用户，别继续。**不要**自己改用户的 Clash 配置、不要重启 Clash、不要替用户选节点。

## 2. 两条命令就够

```sh
# 只读巡检：不切组、不改选择，只报结论（排查时先用这个）
AI_EGRESS_LOG=/tmp/guard.log ./ai-egress-guard.sh --check-only
cat "${AI_EGRESS_STATE_DIR:-$HOME/.local/state/ai-egress-guard}/state.json"

# 完整守护（可切组）：用户明确说「帮我切/救一下 GPT」才用
./ai-egress-guard.sh
```

## 3. 读状态，别读日志

状态文件 `${AI_EGRESS_STATE_DIR:-$HOME/.local/state/ai-egress-guard}/state.json`：

| status | 含义 | 你该做什么 |
|---|---|---|
| `healthy` | 当前出口探测通过 | 报告 node 名，结束。别再说「梯子坏了」 |
| `network_unknown` | 基础网络都不通，**没动节点** | 查 WiFi/校园网/热点，别怪 Clash |
| `scanning` | 正在扫描 | 等 30s 再读一次 |
| `unavailable` | 候选全没通过，已恢复原节点，120s 冷静期 | 等 `checked_at + 120` 之后再看；不要立刻重跑刷它 |
| `probe_failed` | `--check-only` 模式下探测失败 | 说明出口确实不通，可建议用户跑完整模式 |
| `control_error` | Clash 控制接口异常 | 查 socket 路径 / `CLASH_SECRET`，别猜 |
| `selection_changed` | 用户手动换过节点 | **停手**，尊重用户选择 |
| `restore_failed` | 想回滚但失败 | 明确告警，让用户手动看 Clash 面板 |
| `interrupted` | 被中断 | 同上，重跑一次即可 |

`success_at` 是最近一次 healthy 的时间戳；`checked_at` 是本轮时间。**判断新鲜度用这两个字段，不要用文件 mtime。**

## 4. 常见对话怎么答

- 「我 GPT 用不了了」→ `--check-only` 跑一遍 → `healthy` 就是出口好，问题在账号/模型/额度，别甩锅网络。
- 「帮我换个节点救一下」→ 完整模式跑一遍，把 `state.json` 的 `status` + `node` 念给用户。
- 「哪个节点能用」→ 跑完整模式，它只会在真正通过探测时切，切完就停在能用的节点上。
- 没有 Clash（纯裸网）→ 这个工具不适用，直说。

## 5. 硬约束

1. **`--check-only` 优先**：不确定时先用只读模式，任何切组行为都要有用户意图依据。
2. **不改用户配置**：只通过 Clash 的 `/proxies/<group>` PUT 切组，绝不写 `config.yaml`、不重启进程、不动 TUN。
3. **不并发跑**：脚本自带 flock，重复启动会直接退出；别绕过。
4. `selection_changed` / `restore_failed` 出现时**停止自动操作**并把原文状态转述给用户。
5. 节点名对不上时只报错，不要猜名字、不要用 `DIRECT` 兜底。
6. `AI_EGRESS_*` 环境变量是你唯一的配置入口，别去 sed 脚本本体。

## 6. 配置项速查

| 变量 | 默认 | 用途 |
|---|---|---|
| `AI_EGRESS_GROUP` | `AI-AUTO` | 要守护的 Clash 代理组 |
| `AI_EGRESS_PROXY` | `http://127.0.0.1:7897` | 混合代理端口 |
| `AI_EGRESS_PROBE_URL` | `https://chatgpt.com/backend-api/me` | 出口探针 URL（200/401 都算通） |
| `AI_EGRESS_SOCK_GLOB` | `/var/folders/*/*/T/verge-mihomo.sock` | 控制接口 socket 通配路径 |
| `CLASH_SECRET` | 空 | 控制接口开了 secret 时填 |
| `AI_EGRESS_STATE_DIR` | `~/.local/state/ai-egress-guard` | 状态目录 |
| `AI_EGRESS_LOG` | `~/Library/Logs/ai-egress-guard.log` | 日志（仅人看） |
| `AI_EGRESS_COOLDOWN` | `120` | 全挂后的冷静期秒数 |
| `AI_EGRESS_EXTRA_NODES` | 空 | 额外候选节点（空格/换行分隔） |

退出码恒为 0（守护脚本不该让 launchd 反复报警），**结论一律看 `state.json`**。
