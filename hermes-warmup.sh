#!/usr/bin/env bash
# hermes-warmup.sh -- the single entry point that wraps every `hermes`
# invocation. It (1) starts the two race proxies -- compaction (:8977, 300s,
# slow big context-summaries) and toolchain (:8978, 60s, mcp/skills_hub/title-
# gen, fail-fast) -- both refcounted to the lives of open sessions, (2) syncs
# config.yaml's mcp_servers.*.enabled from the single policy source
# config/tools.yaml, and (3) builds the -t toolset union from the same file so
# the tool schema the model sees stays minimal. The real hermes is never
# modified, so `hermes update` stays clean; all runtime state lives under
# ~/.hermes, and all code/config lives in this repo. See each function's
# 2-line comment for its job.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RACE_PROXY_DIR="$SCRIPT_DIR"
TOOLS_CONFIG="$RACE_PROXY_DIR/config/tools.yaml"

# --- Proxy A: compaction (slow, 300s). ---
COMP_CONFIG="$RACE_PROXY_DIR/config/race_proxy_compaction.local.yaml"
COMP_ENTRY="$RACE_PROXY_DIR/race_proxy_compaction.py"
COMP_LOG="$HOME/.hermes/race_proxy_compaction.log"
COMP_ERR="$HOME/.hermes/race_proxy_compaction.err.log"
COMP_PIDFILE="$HOME/.hermes/race_proxy_compaction.pid"
COMP_HEALTH="http://127.0.0.1:8977/health"

# --- Proxy B: toolchain (fast, 60s, mcp/skills/title). ---
TOOL_CONFIG="$RACE_PROXY_DIR/config/race_proxy_toolchain.local.yaml"
TOOL_ENTRY="$RACE_PROXY_DIR/race_proxy_toolchain.py"
TOOL_LOG="$HOME/.hermes/race_proxy_toolchain.log"
TOOL_ERR="$HOME/.hermes/race_proxy_toolchain.err.log"
TOOL_PIDFILE="$HOME/.hermes/race_proxy_toolchain.pid"
TOOL_HEALTH="http://127.0.0.1:8978/health"

# --- Shared session refcount (one hermes = +1; last exit = stop both). ---
RACE_PROXY_REFCOUNT="$HOME/.hermes/race_proxy.refcount"
RACE_PROXY_LOCKDIR="$HOME/.hermes/race_proxy.lock"

HERMES_CONFIG_YAML="$HOME/.hermes/config.yaml"
PYTHON_BIN="$HOME/.hermes/hermes-agent/venv/bin/python"
REAL_HERMES="$HOME/.local/bin/hermes-real"

[ -x "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3)"
[ -x "$REAL_HERMES" ] || { echo "error: real hermes not found: $REAL_HERMES" >&2; exit 1; }

# --- Lock: serialize refcount/pidfile mutation across concurrent launches. ---
_acquire_lock() {
  local tries=0
  while ! mkdir "$RACE_PROXY_LOCKDIR" 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -gt 50 ]; then rmdir "$RACE_PROXY_LOCKDIR" 2>/dev/null; fi  # stale holder
    sleep 0.1
  done
}
_release_lock() { rmdir "$RACE_PROXY_LOCKDIR" 2>/dev/null; }

# --- Healthy: true if a proxy answers its /health with HTTP 200. ---
_proxy_healthy() {
  curl -s -m 2 -o /dev/null -w "%{http_code}" "$1" 2>/dev/null | grep -q "^200$"
}

# --- Start one proxy if absent; idempotent, waits up to 3s to warm. ---
_start_proxy() {
  local entry config log err pidfile health
  entry="$1"; config="$2"; log="$3"; err="$4"; pidfile="$5"; health="$6"
  if _proxy_healthy "$health"; then return; fi
  nohup "$PYTHON_BIN" "$entry" --config "$config" --verbose \
    > "$log" 2>> "$err" &
  echo $! > "$pidfile"
  disown
  local waited=0
  while [ "$waited" -lt 30 ]; do
    _proxy_healthy "$health" && break
    sleep 0.1; waited=$((waited + 1))
  done
}

# --- Stop ONE proxy via its pidfile; only called at the last session exit. ---
_stop_proxy() {
  local pidfile pid
  pidfile="$1"
  [ -f "$pidfile" ] || return
  pid=$(cat "$pidfile" 2>/dev/null) || return
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null
  rm -f "$pidfile"
}

# --- Bump refcount and start both proxies for this session. ---
_start_proxies() {
  _acquire_lock
  local count
  count=$(cat "$RACE_PROXY_REFCOUNT" 2>/dev/null || echo 0)
  count=$((count + 1)); echo "$count" > "$RACE_PROXY_REFCOUNT"
  _start_proxy "$COMP_ENTRY" "$COMP_CONFIG" "$COMP_LOG" "$COMP_ERR" "$COMP_PIDFILE" "$COMP_HEALTH"
  _start_proxy "$TOOL_ENTRY" "$TOOL_CONFIG" "$TOOL_LOG" "$TOOL_ERR" "$TOOL_PIDFILE" "$TOOL_HEALTH"
  _release_lock
}

# --- Decrement refcount; at zero, stop both proxies. ---
_stop_proxies() {
  _acquire_lock
  local count
  count=$(cat "$RACE_PROXY_REFCOUNT" 2>/dev/null || echo 1)
  count=$((count - 1)); [ "$count" -lt 0 ] && count=0
  echo "$count" > "$RACE_PROXY_REFCOUNT"
  if [ "$count" -eq 0 ]; then
    _stop_proxy "$COMP_PIDFILE"
    _stop_proxy "$TOOL_PIDFILE"
  fi
  _release_lock
}

# --- State ON? Recognizes ON/on/true/True/1/yes. ---
_state_yes() {
  case "$1" in
    ON|on|true|True|TRUE|1|yes) return 0 ;;
    *) return 1 ;;
  esac
}

# --- Sync mcp_servers.*.enabled in config.yaml from tools.yaml `mcp` block. ---
# Only writes when a flag differs; means "flip OFF in tools.yaml to disable".
sync_mcp() {
  [ -f "$TOOLS_CONFIG" ] || return 0
  local plan
  plan="$("$PYTHON_BIN" -c "
import yaml,os,json
cfg=yaml.safe_load(open('$HERMES_CONFIG_YAML')) or {}
tools=yaml.safe_load(open('$TOOLS_CONFIG')) or {}
mcp=tools.get('toolsets',{}).get('mcp',{}) or {}
wanted={}
for name,cfgrow in (cfg.get('mcp_servers',{}) or {}).items():
    row=mcp.get(name)
    state=(row or {}).get('state') if isinstance(row,dict) else 'OFF'
    on=state in (True,'ON','on','true','True','TRUE','1')
    cur=bool(cfgrow.get('enabled')) if cfgrow else True
    if on!=cur: wanted[name]=str(on).lower()
print(json.dumps(wanted))
")"
  [ -z "$plan" ] || [ "$plan" = "{}" ] && return 0
  echo "$plan" | "$PYTHON_BIN" -c "
import json,sys
for n,v in json.load(sys.stdin).items(): print(n,v)
" | while read -r name val; do
    "$REAL_HERMES" config set "mcp_servers.${name}.enabled" "$val" >/dev/null 2>&1
  done
}

# --- Echo the -t union, or return 1 to bypass (user passed their own -t). ---
# Base = live enabled toolsets + MCP servers; tools.yaml overrides ON/OFF.
build_toolsets() {
  for _a in "$@"; do
    [ "$_a" = "-t" ] || [ "$_a" = "--toolsets" ] && return 1
  done
  local out base names n
  out="$("$REAL_HERMES" tools list --platform cli 2>/dev/null)"
  base="$(awk '
    /^MCP servers:/ {mcp=1; next}
    /^[[:space:]]*$/ {mcp=0; next}
    {
      if (!mcp && match($0, /enabled[[:space:]]+[A-Za-z0-9_-]+/)) {
        s=substr($0,RSTART+7,RLENGTH-7); sub(/^[[:space:]]+/,"",s); sub(/[[:space:]].*$/,"",s); print s
      }
      if (mcp && match($0, /^[[:space:]]*[A-Za-z0-9_-]+/)) print $1
    }' <<< "$out")"
  [ -z "${base//[[:space:]]/}" ] && [ ! -f "$TOOLS_CONFIG" ] && return 1
  if [ -f "$TOOLS_CONFIG" ]; then
    local forced_in="" forced_out="" entrykey state
    while IFS='|' read -r entrykey state; do
      [ -z "$entrykey" ] && continue
      if _state_yes "$state"; then forced_in=" $forced_in $entrykey"; else forced_out=" $forced_out $entrykey"; fi
    done < <("$PYTHON_BIN" -c "
import yaml
d=yaml.safe_load(open('$TOOLS_CONFIG')) or {}
for block,cats in (d.get('toolsets') or {}).items():
    if block=='reference': continue
    for k,v in (cats or {}).items():
        if isinstance(v,dict): print(f\"{k}|{v.get('state','OFF')}\")
")
    names="$base $forced_in"
    for n in $forced_out; do
      names=$(printf '%s\n' "$names" | sed -E "s/(^|[[:space:]])${n}([[:space:]]|$)/ /g")
    done
  else
    names="$base"
  fi
  echo "$names" | tr ' ' '\n' | sort -u | grep -v '^$' | paste -sd, -
}

_start_proxies
sync_mcp

RUN_ARGS=("$@")
if TOOLSETS=$(build_toolsets "$@") && [ -n "$TOOLSETS" ]; then
  RUN_ARGS=(-t "$TOOLSETS" "$@")
fi

# NOT exec: must wait so we can decrement the refcount on exit.
"$REAL_HERMES" "${RUN_ARGS[@]}"
HERMES_EXIT_CODE=$?

_stop_proxies
exit "$HERMES_EXIT_CODE"