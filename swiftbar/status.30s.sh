#!/bin/bash
# SwiftBar 插件：本机守护总面板（菜单栏唯一图标）
# 2026-10-31 起两合一：合盖工作(keepawake) + GPT 出口守护(ai-egress-guard)
# （GLM 视觉桥 8399 已退役，DSH 改直连智谱 coding 端点，2026-10-31）
# 健康时只显示 🟢；异常才换符号（🔴 GPT全挂 > 🟡 心跳旧 > ⚪️ Clash读不到 > 💤/🌗 已暂停 > ⚫ 合盖中）；详情点开看

# ---------- 合盖工作 ----------
AWAKE="$HOME/.local/bin/awake"
DAEMON="$HOME/.local/bin/dsh-awake-daemon"
KD_MARK="$HOME/.local/state/dsh-keepawake.off"
LID_MARK="$HOME/.local/state/lid-dim.off"

SLEEP_DISABLED=$(/usr/bin/pmset -g | awk '/SleepDisabled/{print $2}')
BATT_OUT=$(/usr/bin/pmset -g batt)
PCT=$(printf '%s\n' "$BATT_OUT" | sed -n '2p' | grep -o '[0-9][0-9]*%')
case "$BATT_OUT" in
  *"'AC Power'"*) SRC="电源" ;;
  *) SRC="电池" ;;
esac
STATUS=$("$DAEMON" --status 2>/dev/null)
LID=$(printf '%s\n' "$STATUS" | awk -F= '/^lid=/{print $2}')

KD_BAD=0
if [ -f "$KD_MARK" ]; then KD="💤 已暂停（合盖会睡）"; KD_BAD=1
elif [ "$SLEEP_DISABLED" = "1" ]; then KD="🟢 开启中（合盖也不睡）"
else KD="⚪ 已关闭（合盖会睡）"; KD_BAD=1
fi

LID_CLOSED=0
if [ -z "$LID" ]; then LID_TXT="❓ 守护没在跑"
elif [ -f "$LID_MARK" ]; then LID_TXT="⏸ 已暂停（合盖不自动熄屏）"
elif [ "$LID" = "closed" ]; then LID_TXT="⚫ 生效中（盖子合着，屏已熄）"; LID_CLOSED=1
else LID_TXT="🟢 生效中（合盖自动熄屏）"
fi

# ---------- GPT 出口守护 ----------
GUARD_LOG="$HOME/Library/Logs/ai-egress-guard.log"
STATE_FILE="$HOME/.dsh/state/ai-egress.json"
SOCK=$(ls /var/folders/*/*/T/verge-mihomo.sock 2>/dev/null | head -1)
CH="Authorization: Bearer ${CLASH_API_SECRET:-set-your-secret}"

CUR=""
[ -n "$SOCK" ] && CUR=$(curl -q -fsS --connect-timeout 1 -m 3 --noproxy '*' --unix-socket "$SOCK" -H "$CH" "http://localhost/proxies/AI-AUTO" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("now") or "")' 2>/dev/null)

# 日志仅供回看；红绿灯只认绑定节点、带时间的独立状态，损坏/过期不沿用旧绿灯。
G_RECORD=$(python3 - "$STATE_FILE" <<'PY'
import json,sys
try:
 d=json.load(open(sys.argv[1]))
 assert d.get('version')==1 and isinstance(d['status'],str) and isinstance(d['node'],str)
 assert isinstance(d['checked_at'],int) and d['checked_at']>0
 assert isinstance(d['success_at'],int) and d['success_at']>=0
 assert not any(x in d['node'] for x in '\r\n\t')
 print(d['status'],d['checked_at'],d['success_at'],d['node'],sep='\t')
except (OSError,ValueError,KeyError,TypeError,AttributeError,AssertionError): pass
PY
)
G_KIND=""; CHECKED=0; SUCCESS=0; G_NODE=""
IFS=$'\t' read -r G_KIND CHECKED SUCCESS G_NODE <<<"$G_RECORD"
NOW=$(date +%s)
REC_AGE=999999
[ -n "$CHECKED" ] && REC_AGE=$((NOW-CHECKED))
LAST=$(tail -n 1 "$GUARD_LOG" 2>/dev/null | cut -d' ' -f3-)

G_LEVEL=2
G_STATE="🟡 等待守护重新探测（不沿用旧故障日志）"
if [ -z "$CUR" ]; then G_STATE="⚪️ Clash 未运行或读不到 AI-AUTO 组"; G_LEVEL=1
elif [ "$REC_AGE" -gt 180 ] || [ "$REC_AGE" -lt 0 ]; then
  G_STATE="🟡 探测记录缺失或过期，当前状态未确认"
elif [ "$G_KIND" = scanning ]; then G_STATE="🟡 基础网络对照通过，正在复查出口"
elif [ "$G_KIND" = restore_failed ]; then G_STATE="🔴 恢复起始节点失败，请检查 Clash"; G_LEVEL=3
elif [ "$G_KIND" = control_error ]; then G_STATE="⚪️ Clash 控制接口异常；已停止扫描"; G_LEVEL=1
elif [ "$G_NODE" != "$CUR" ]; then G_STATE="🟡 节点已变化，等待新探测"
else
  case "$G_KIND" in
    healthy) G_STATE="🟢 出口探测通过（${REC_AGE}s 前；非模型生成验证）"; G_LEVEL=0 ;;
    network_unknown) G_STATE="🟡 基础网络待确认；保持原节点，等待恢复" ;;
    unavailable) G_STATE="🔴 本轮候选出口未通过探测；已恢复原节点，稍后复查"; G_LEVEL=3 ;;
    selection_changed) G_STATE="🟡 检测到外部切换；已停止干预，等待探测" ;;
    probe_failed) G_STATE="🟡 出口探测未通过（只读检查，未切节点）" ;;
    interrupted) G_STATE="🟡 上轮探测被中断，等待复查" ;;
  esac
fi

# ---------- 菜单栏图标：异常优先，其次合盖状态，健康绿灯 ----------
TITLE="🟢"
[ -f "$LID_MARK" ] && TITLE="🌗"
[ -f "$KD_MARK" ] && TITLE="💤"
[ "$LID_CLOSED" -eq 1 ] && TITLE="⚫"
[ "$G_LEVEL" -ge 2 ] && TITLE="🟡"
[ "$G_LEVEL" -eq 1 ] && TITLE="⚪️"
[ "$G_LEVEL" -eq 3 ] && TITLE="🔴"

echo "$TITLE"
echo "---"
echo "【合盖工作】"
echo "合盖不睡 ｜ $KD"
echo "合盖熄屏 ｜ $LID_TXT"
echo "$SRC ｜ 电量 ${PCT:-?}"
echo "切换 合盖工作 | bash=$AWAKE toggle terminal=false refresh=true"
echo "切换 合盖熄屏 | bash=$AWAKE lid toggle terminal=false refresh=true"
echo "---"
echo "【GPT 出口】${CUR:+ ｜ $CUR}"
echo "守护 ｜ $G_STATE"
echo "最近动作 ｜ ${LAST:-（还没有日志）}"
echo "手动跑一轮探测 | bash=$HOME/.dsh/bin/ai-egress-guard.sh terminal=false refresh=true"
echo "---"
echo "看 合盖日志 | bash=/usr/bin/tail param1=-20 param2=$HOME/Library/Logs/dsh-keepawake.log terminal=true"
echo "看 熄屏日志 | bash=/usr/bin/tail param1=-20 param2=$HOME/Library/Logs/dsh-awake.log terminal=true"
echo "看 GPT 守护日志 | bash=/usr/bin/tail param1=-30 param2=$GUARD_LOG terminal=true"
echo "---"
echo "刷新 | refresh=true"
