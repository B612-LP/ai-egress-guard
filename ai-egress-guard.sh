#!/bin/bash
# ai-egress-guard — keep a Clash/mihomo proxy group pointed at a working ChatGPT egress.
# Probes every cycle; on repeated failure it picks a healthy node from the same group.
# Design notes:
#   - Only egress reachability is tested (HTTP probe); this is NOT model/account health.
#   - Two-stage scan: coarse-filter every candidate with mihomo's per-node /delay probe
#     (never touches the group selector, so in-flight traffic is not cut), then only
#     switch the group for candidates that pass the coarse filter.
#   - Never switches when basic connectivity can't be confirmed; never races the user:
#     every write re-checks the selector first and restores the start node on exit.
set -u
STATE_DIR="${AI_EGRESS_STATE_DIR:-$HOME/.local/state/ai-egress-guard}"
mkdir -p "$STATE_DIR"

GROUP="${AI_EGRESS_GROUP:-AI-AUTO}"
PROXY="${AI_EGRESS_PROXY:-http://127.0.0.1:7897}"
PROBE_URL="${AI_EGRESS_PROBE_URL:-https://chatgpt.com/backend-api/me}"
PROBE_URL_ENC=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "$PROBE_URL")
LOG="${AI_EGRESS_LOG:-$HOME/Library/Logs/ai-egress-guard.log}"
SOCK_GLOB="${AI_EGRESS_SOCK_GLOB:-/var/folders/*/*/T/verge-mihomo.sock}"
IDLE="$STATE_DIR/idle"
COOLDOWN="${AI_EGRESS_COOLDOWN:-120}"
CHECK_ONLY=0
[ "${1:-}" = --check-only ] && CHECK_ONLY=1

# flock is inherited across exec and held for the whole probe+recover cycle;
# process exit releases it, no stale locks.
if [ "${AI_EGRESS_LOCKED:-}" != 1 ]; then
  exec python3 - "$0" "$@" <<'PY'
import fcntl, os, sys
lock=os.path.join(os.environ['AI_EGRESS_STATE_DIR'], 'guard.lock')
f=open(lock, 'a')
try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError: sys.exit(0)
os.set_inheritable(f.fileno(), True)
os.environ['AI_EGRESS_LOCKED']='1'
os.execv('/bin/bash', ['/bin/bash', sys.argv[1], *sys.argv[2:]])
PY
fi

# Independent state file; atomic replace so menus don't guess the light from logs.
record_state() {
  python3 - "$1" "${2:-}" <<'PY'
import json, os, pathlib, tempfile, time, sys
root=pathlib.Path(os.environ['AI_EGRESS_STATE_DIR']).resolve()
p=root/'state.json'
try: old=json.loads(p.read_text())
except (ValueError, OSError): old={}
if not isinstance(old, dict): old={}
status,node=sys.argv[1:]
now=int(time.time())
success=old.get('success_at',0)
if not isinstance(success,int): success=0
data=dict(version=1,status=status,node=node,checked_at=now,
          success_at=now if status=='healthy' else success)
with tempfile.NamedTemporaryFile(mode='w',dir=root,prefix='state.',suffix='.tmp',delete=False) as f:
 json.dump(data,f,ensure_ascii=False);f.write('\n');tmp=pathlib.Path(f.name)
os.replace(tmp,p)
if old.get('status') != status or (status=='healthy' and old.get('node') != node):
 messages={'healthy':'egress probe passed (not a generation check)',
 'network_unknown':'basic network unconfirmed, kept original node',
 'scanning':'egress failing, scanning candidates',
 'unavailable':'no candidate passed this round; restored start node; recheck after cooldown',
 'control_error':'Clash control API error, scan stopped',
 'restore_failed':'failed to restore start node, check Clash',
 'selection_changed':'selector changed externally, standing down',
 'probe_failed':'egress probe failed (read-only, no switch)',
 'interrupted':'probe interrupted, scan stopped'}
 log=pathlib.Path(os.environ.get('AI_EGRESS_LOG',''))
 try:
  if str(log): 
   log.parent.mkdir(parents=True, exist_ok=True)
   with log.open('a') as f: f.write(time.strftime('%F %T ')+messages.get(status,status)+' | '+node+'\n')
 except OSError: pass
PY
}
healthy() {
  record_state healthy "$1"
}

SOCK=$(ls $SOCK_GLOB 2>/dev/null | head -1)
AUTH=()
[ -n "${CLASH_SECRET:-}" ] && AUTH=(-H "Authorization: Bearer $CLASH_SECRET")
api_get() { curl -q -fsS --connect-timeout 1 -m 3 --noproxy '*' --unix-socket "$SOCK" "${AUTH[@]+"${AUTH[@]}"}" "http://localhost/$1" 2>/dev/null; }
current_node() { api_get "proxies/$GROUP" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("now") or "")' 2>/dev/null; }
group_members() { api_get "proxies/$GROUP" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("\n".join(d.get("all") or []))' 2>/dev/null; }
api_put() {
  local body out
  body=$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1]}))' "$1") || return 1
  out=$(curl -q -sS --connect-timeout 1 -m 3 --noproxy '*' --unix-socket "$SOCK" "${AUTH[@]+"${AUTH[@]}"}" \
    -X PUT --data-binary "$body" -o /dev/null -w '%{http_code}' "http://localhost/proxies/$GROUP" 2>/dev/null) || return 1
  case "$out" in 200|204) return 0 ;; *) return 1 ;; esac
}

# Per-node out-of-band probe: request goes through the named node without changing
# the group selector. mihomo counts ANY response (including a 403 ban page) as a
# delay, so this is only a coarse filter.
api_delay() {
  curl -q -sS --connect-timeout 1 -m 8 --noproxy '*' --unix-socket "$SOCK" "${AUTH[@]+"${AUTH[@]}"}" \
    "http://localhost/proxies/$1/delay?timeout=5000&url=$2" 2>/dev/null
}

# curl must complete fully; a timeout after receiving 200/401 headers is not success.
probe() {
  local out
  if out=$(curl -q -sS --connect-timeout 3 -m 6 -x "$PROXY" --noproxy '' -o /dev/null \
       -w '%{http_code} %{time_total}' "$PROBE_URL" 2>/dev/null); then
    case "$out" in '200 '*|'401 '*) return 0 ;; esac
  fi
  return 1
}

# No Wi-Fi checks, no single ping: at least one HTTPS control must return the
# exact expected body. curlrc and env proxies are disabled; if a system TUN takes
# over, this only proves that path works.
base_network_ok() {
  local body
  if body=$(curl -q -fsS --connect-timeout 2 -m 4 --proxy '' --noproxy '*' \
      -w '\n%{http_code}' https://captive.apple.com/hotspot-detect.html 2>/dev/null); then
    if printf '%s' "$body" | python3 -c 'import sys; s=sys.stdin.read();body,_,code=s.rpartition("\n");expected="<html><head><title>success</title></head><body>success</body></html>";sys.exit(0 if code=="200" and "".join(body.lower().split())==expected else 1)'; then return 0; fi
  fi
  if body=$(curl -q -fsS --connect-timeout 2 -m 4 --proxy '' --noproxy '*' \
      -w '\n%{http_code}' https://www.msftconnecttest.com/connecttest.txt 2>/dev/null); then
    [ "$body" = $'Microsoft Connect Test\n200' ] && return 0
  fi
  return 1
}

cur=""; last=""; inflight=""; restore_needed=0
# Clash has no atomic compare-and-set; re-check before every write to best-effort
# avoid clobbering a manual node pick.
cleanup() {
  [ "$restore_needed" = 1 ] || return 0
  local actual
  actual=$(current_node)
  if [ -z "$actual" ]; then record_state restore_failed ""; return; fi
  if [ "$actual" = "$cur" ]; then return; fi
  if [ "$actual" != "$last" ] && [ "$actual" != "$inflight" ]; then
    record_state selection_changed "$actual"; return
  fi
  api_put "$cur"
  actual=$(current_node)
  if [ "$actual" = "$cur" ]; then return; fi
  record_state restore_failed "$actual"
}
trap cleanup EXIT
trap 'record_state interrupted "$cur"; exit 0' INT TERM HUP

[ -n "$SOCK" ] || { record_state control_error ""; exit 0; }
cur=$(current_node); last="$cur"
[ -n "$cur" ] || { record_state control_error ""; exit 0; }

# Candidates come from the group's own member list; extras can be appended via
# AI_EGRESS_EXTRA_NODES (newline- or space-separated).
# macOS ships bash 3.2: no mapfile/readarray, so fill the array with a read loop.
NODES=()
_append_nodes() { local n; while IFS= read -r n; do [ -n "$n" ] && NODES+=("$n"); done; }
group_members | tr ' ' '\n' | _append_nodes
[ -n "${AI_EGRESS_EXTRA_NODES:-}" ] && printf '%s' "$AI_EGRESS_EXTRA_NODES" | tr ' \n' '\n\n' | _append_nodes

if probe; then
  actual=$(current_node)
  [ -n "$actual" ] || { record_state control_error ""; exit 0; }
  if [ "$actual" = "$cur" ]; then healthy "$cur"; else record_state selection_changed "$actual"; fi
  exit 0
fi
sleep 2
if probe; then
  actual=$(current_node)
  [ -n "$actual" ] || { record_state control_error ""; exit 0; }
  if [ "$actual" = "$cur" ]; then healthy "$cur"; else record_state selection_changed "$actual"; fi
  exit 0
fi
actual=$(current_node)
[ -n "$actual" ] || { record_state control_error ""; exit 0; }
[ "$actual" = "$cur" ] || { record_state selection_changed "$actual"; exit 0; }
if ! base_network_ok; then
  printf '0\n' >"$IDLE" # do not inherit outage as cooldown
  record_state network_unknown "$cur"
  exit 0
fi
[ "$CHECK_ONLY" = 0 ] || { record_state probe_failed "$cur"; exit 0; }

now=$(date +%s)
idle=0
if [ -f "$IDLE" ]; then read -r idle <"$IDLE" || idle=0; fi
case "$idle" in ''|*[!0-9]*) idle=0 ;; esac
if [ "$idle" -gt 0 ] && [ "$now" -ge "$idle" ] && [ $((now-idle)) -lt "$COOLDOWN" ]; then
  record_state unavailable "$cur"; exit 0
fi
record_state scanning "$cur"
# Two-stage scan: coarse filter never touches the selector; only candidates that
# pass get a real group-switch probe. A typical round switches the group once.
for cand in "${NODES[@]}"; do
  [ "$cand" = "$cur" ] && continue
  actual=$(current_node)
  [ -n "$actual" ] || { record_state control_error ""; exit 0; }
  if [ "$actual" != "$last" ]; then
    restore_needed=0; record_state selection_changed "$actual"; exit 0
  fi
  enc=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "$cand") || {
    record_state control_error "$last"; exit 0
  }
  if ! api_delay "$enc" "$PROBE_URL_ENC" | grep -q '"delay":'; then
    continue
  fi
  if ! base_network_ok; then
    printf '0\n' >"$IDLE"; record_state network_unknown "$cur"; exit 0
  fi
  inflight="$cand"; restore_needed=1 # record intent before the request; covers PUT timeouts that still took effect
  if ! api_put "$cand"; then record_state control_error "$last"; exit 0; fi
  actual=$(current_node)
  [ -n "$actual" ] || { record_state control_error ""; exit 0; }
  if [ "$actual" != "$cand" ]; then
    restore_needed=0; record_state selection_changed "$actual"; exit 0
  fi
  last="$cand"; inflight=""
  if probe; then
    actual=$(current_node)
    [ -n "$actual" ] || { record_state control_error ""; exit 0; }
    if [ "$actual" != "$cand" ]; then
      restore_needed=0; record_state selection_changed "$actual"; exit 0
    fi
    restore_needed=0; healthy "$cand"; exit 0
  fi
  # Real probe failed (e.g. a banned node that passed the coarse filter): try the
  # next candidate; on total failure the EXIT trap restores the start node.
done
if ! base_network_ok; then
  printf '0\n' >"$IDLE"; record_state network_unknown "$cur"; exit 0
fi
printf '%s\n' "$(date +%s)" >"$IDLE"
record_state unavailable "$cur"
exit 0
