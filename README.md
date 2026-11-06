# ai-egress-guard

GPT 出口自愈守护：让走 Clash 代理的 ChatGPT/Codex 订阅链路（`chatgpt.com/backend-api`）在节点劣化、被风控封禁、机场整池抖动时**自动切换、自动恢复**，并带一个 macOS 菜单栏（SwiftBar）状态面板。

> 诞生背景：mihomo 的延迟探测可能将 403 风控页也记为「有延迟」。守护在故障切换时再对 ChatGPT 端点发起未认证请求，只接受 HTTP 401/200；这比单纯看延迟更严格，但仍不能证明账号或模型可用。

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

### 按需维护备用节点

健康时不主动换出口。后台维护最多两个备胎：定期粗测现有备胎，失败后复测确认；不足时才分批寻找补位，优先找与当前节点属于不同订阅的节点，找够即停。未配置订阅归属或归属未知时，按组成员顺序寻找。全体候选不可用时恢复起始节点并进入冷静期，稍后再尝试。

### 判定与切换流程

1. **探针**：每 10s 走代理请求 `GET https://chatgpt.com/backend-api/me`，只认 401/200；未认证的 401 不证明账号、模型或长连接可用。
2. **确认**：首次失败后间隔 2 秒重试，仍失败且基础网络正常才进入恢复流程；实际耗时取决于探测超时。
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

1. 在 Clash 配置里准备 `AI-AUTO` 选择组与分流规则（建议放订阅 profile 的增强层）。可选：创建 `~/.dsh/state/ai-egress-subscriptions.json`，写入节点名到订阅 ID 的 JSON 对象，例如 `{"节点 A":"订阅 1","节点 B":"订阅 2"}`。名称须与组成员完全一致；缺失、无效或未识别的归属不会猜测。此文件只用于备胎选择，不包含节点连接配置，也不会自动同步订阅。
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

- 历史日志中曾出现故障确认后 1–5 秒恢复的个例；并非速度保证，检测窗口、扫描及节点响应均影响耗时。
- 所有候选不可用时，恢复时间取决于网络和服务端恢复情况。

## 设计取舍

- **不用 Clash 原生 url-test/fallback**：健康检查语义不对（403 算健康），组内成员探测也无法表达「业务可用」。
- **粗筛 ≠ 终审**：`/delay` 探测快且不切组，但会被 403 骗；最终以带真实探针的切组验证为准。
- **兜底节点是静态快照**：跨订阅节点定义注入增强层后不随那个订阅的更新变化，失效时守护自动跳过（无害），需要定期人工/脚本同步。

## License

MIT
