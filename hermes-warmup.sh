#!/usr/bin/env bash
# hermes-warmup.sh -- the single entry point that wraps every `hermes`
# invocation. It (1) starts the two race proxies -- compaction (:8977, 300s,
# slow big context-summaries) and toolchain (:8978, 60s, mcp/skills_hub/title-
# gen, fail-fast) -- both refcounted to the lives of open sessions.
# Hermes manages toolsets and MCP configuration itself. The real hermes is never
# modified, so `hermes update` stays clean; all runtime state lives under
# ~/.hermes, and all code/config lives in this repo. See each function's
# 2-line comment for its job.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RACE_PROXY_DIR="$SCRIPT_DIR"

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

_start_proxies

# NOT exec: must wait so we can decrement the refcount on exit.
"$REAL_HERMES" "$@"
HERMES_EXIT_CODE=$?

_stop_proxies
exit "$HERMES_EXIT_CODE"