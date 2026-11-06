# ai-egress-guard

GPT 出口自愈守护：让走 Clash 代理的 ChatGPT/Codex 订阅链路（`chatgpt.com/backend-api`）在节点劣化、被风控封禁、机场整池抖动时**自动切换、自动恢复**，并带一个 macOS 菜单栏（SwiftBar）状态面板。

> 诞生背景：Clash 原生的 `url-test`/`fallback` 健康检查只测「TCP 能不能连上」，mihomo 甚至把 403 风控页都记成「有延迟」——被封的节点照样被判定健康。这个守护用**真实业务探针**（只认 HTTP 401/200）做决策，堵住这个缺口。

## 架构

```
DSH / ChatGPT App 的 GPT 请求
   → chatgpt.com/backend-api
   → Clash (127.0.0.1:7897)
   → 规则: chatgpt.com / openai.com / oaistatic.com / oaiusercontent.com → AI-AUTO 组
   → AI-AUTO 当前选中的节点 → 出口
                    ↑
        ai-egress-guard 每 10s 巡检一次
```

### 三层防线

| 层 | 内容 | 行为 |
|---|---|---|
| ① 主池 | 订阅内的全部节点（按地区排序，如日本优先） | 单节点坏 → 秒级切换到下一个可用节点（实测典型 1–5s） |
| ② 跨订阅兜底 | 另一个订阅的少量节点排在组成员最末尾 | 主池全灭时启用；注入方法是 Clash Verge「增强层」的 `proxies append`（静态快照，失效自动跳过） |
| ③ 等待恢复 | 全部候选不可用 | 退回起始节点 + 冷静期后重扫，机场侧恢复即自动接上 |

### 判定与切换流程

1. **探针**：每 10s 走代理真打 `GET https://chatgpt.com/backend-api/me`，**只认 401/200**（401 = 能摸到 Cloudflare，链路通；其余超时/非 2xx/403 一律算坏）。
2. **确认**：连续失败约 8.5s 才判定故障（防抖）。
3. **两段式扫描**（切换过程不惊动在途连接的其它候选）：
   - 粗筛：`GET /proxies/{节点}/delay` 直达探测每个候选，**完全不切组**，在途请求不受影响（注意 mihomo 把 403 也算有延迟，粗筛只当初筛）；
   - 终审：粗筛通过者切组做真探针，401/200 才算数，一轮典型只切 1 次组。
4. **不抢方向盘**：检测到用户在 Clash Verge 里手动选了节点，守护停止自动切换。
5. **全灭**：恢复起始节点，冷静期（120s）后自动重扫。

## 组成

| 文件 | 作用 |
|---|---|
| `ai-egress-guard.py` | 核心实现（纯标准库 Python3，约 660 行） |
| `ai-egress-guard.sh` | 稳定入口壳（launchd / SwiftBar / 手动都调它） |
| `launchd/com.b612.ai-egress-guard.plist` | 10s 一轮巡检（跑完即退，无常驻内存） |
| `launchd/com.b612.ai-egress-scout.plist` | 60s 一轮后台待命侦察（维护备用候选缓存） |
| `swiftbar/status.30s.sh` | 菜单栏面板：健康时只显示 🟢，异常变 🔴/🟡/⚪️/💤 等，点开看详情、开关与日志 |
| `install.sh` | 一键部署 |

## 安装

前置：macOS + Clash Verge（mihomo 内核）+ SwiftBar（可选，仅面板）。

1. 在 Clash 配置里准备 `AI-AUTO` 选择组与分流规则（建议放订阅 profile 的增强层，订阅更新不丢）。组员按优先级排序，可把第二订阅的兜底节点排最后。
2. 设置 Clash 外部控制器（unix socket）密钥，并通过环境变量告诉守护：
   ```bash
   export CLASH_API_SECRET='你的密钥'   # launchd 环境可在 plist 加 EnvironmentVariables
   ```
3. 部署：
   ```bash
   ./install.sh
   ```
4. 手动跑一轮验证（健康时静默、exit 0）：
   ```bash
   bash ~/.dsh/bin/ai-egress-guard.sh && echo OK
   ```

## 状态与日志

| 路径 | 内容 |
|---|---|
| `~/.dsh/state/ai-egress.json` | 当前状态：`status` / `node` / `checked_at` / `success_at` |
| `~/Library/Logs/ai-egress-guard.log` | 巡检与事件日志（fault_confirmed / candidate_* / unavailable / origin_restored / healthy …） |
| `~/Library/Logs/ai-egress-guard.launchd.log` | launchd 标准输出 |
| `~/.dsh/state/ai-egress-standbys.json` | 备用候选缓存（侦察产出） |

## 实测数据（2026-11，单用户 Mac）

- 单节点故障自愈：**1–5 秒**（占全部故障的绝大多数）
- 整池熄灭自愈：取决于机场侧恢复速度（实测 1–5 分钟量级），守护行为正确但只能等待；接入跨订阅兜底后可在池灭时提供退路
- 资源占用：每轮约 70ms CPU；无常驻内存（launchd 拉起 → 跑完即退）

## 设计取舍

- **不用 Clash 原生 url-test/fallback**：健康检查语义不对（403 算健康），组内成员探测也无法表达「业务可用」。
- **粗筛 ≠ 终审**：`/delay` 探测快且不切组，但会被 403 骗；最终以带真实探针的切组验证为准。
- **兜底节点是静态快照**：跨订阅节点定义注入增强层后不随那个订阅的更新变化，失效时守护自动跳过（无害），需要定期人工/脚本同步。

## License

MIT
