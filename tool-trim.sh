#!/usr/bin/env bash
# tool-trim.sh -- Hermes launcher: race proxy lifecycle + live toolset trim.
#
# Wraps the REAL hermes (hermes-real) so every invocation:
#   1. STARTS the race proxy if not healthy (refcounted) and stops it when the
#      last concurrent session exits.
#   2. TRIMS the tool catalog: builds a single -t/--toolsets union from hermes's
#      LIVE enabled tools + MCP servers, overridden by config/tools.yaml
#      (state: ON -> force in, OFF -> force out). Applied as one -t flag on the
#      real hermes call to cut the eager tool-schema payload / token cost.
#   3. PASSES THROUGH every hermes subcommand/flag untouched (unless the user
#      already supplied their own -t/--toolsets, which wins -- no YAML override).
#
# The real hermes is NEVER modified and the hermes-agent source is never touched,
# so `hermes update` (a git pull on that repo) stays clean. This script, the race
# proxy code, and the runtime config (config/) all live in this repo, outside the
# hermes-agent tree, and keys are read at proxy load time from Hermes's
# credential pool (~/.hermes/auth.json -> credential_pool.<provider>).
#
# Wiring on a machine:
#   - ~/.local/bin/hermes   -> a thin shim:  exec "$HOME/.../tool-trim.sh" "$@"
#   - ~/.zshrc              -> alias hermes="$HOME/.../tool-trim.sh"

set -u

# Self-locate so the repo can live anywhere / be cloned anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RACE_PROXY_DIR="$SCRIPT_DIR"
RACE_PROXY_CONFIG="$RACE_PROXY_DIR/config/race_proxy.local.yaml"
TOOLS_CONFIG="$RACE_PROXY_DIR/config/tools.yaml"

# Runtime state (pid / refcount / lock / logs) stays outside the repo under
# ~/.hermes so nothing transient pollutes git and nothing is machine-bound.
RACE_PROXY_LOG="$HOME/.hermes/race_proxy.log"
RACE_PROXY_ERR="$HOME/.hermes/race_proxy.err.log"
RACE_PROXY_PIDFILE="$HOME/.hermes/race_proxy.pid"
RACE_PROXY_REFCOUNT="$HOME/.hermes/race_proxy.refcount"
RACE_PROXY_LOCKDIR="$HOME/.hermes/race_proxy.lock"
RACE_PROXY_HEALTH_URL="http://127.0.0.1:8977/health"

PYTHON_BIN="$HOME/.hermes/hermes-agent/venv/bin/python"
REAL_HERMES="$HOME/.local/bin/hermes-real"

[ -x "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3)"
[ -x "$REAL_HERMES" ] || { echo "error: real hermes not found: $REAL_HERMES" >&2; exit 1; }

# build_toolsets: print the -t union (comma-separated) of enabled toolsets for
# this run, or return 1 to signal "bypass" (user passed their own -t/--toolsets).
# Base    = live enabled toolsets + MCP servers from `hermes tools list`
# Override= tools.yaml: ON->force in, OFF->force out, unlisted->hermes default.
build_toolsets() {
    for _a in "$@"; do
        if [ "$_a" = "-t" ] || [ "$_a" = "--toolsets" ]; then
            return 1   # explicit user -t wins -> bypass YAML control
        fi
    done

    local out base forced_in forced_out names n
    out="$("$REAL_HERMES" tools list --platform cli 2>/dev/null)"

    # Base: enabled builtin/plugin toolsets + MCP server names.
    base="$(awk '
        /^MCP servers:/ {mcp=1; next}
        /^[[:space:]]*$/ {mcp=0; next}
        {
            if (!mcp && match($0, /enabled[[:space:]]+[A-Za-z0-9_-]+/)) {
                s=substr($0,RSTART+7,RLENGTH-7)
                sub(/^[[:space:]]+/,"",s); sub(/[[:space:]].*$/,"",s)
                print s
            }
            if (mcp && match($0, /^[[:space:]]*[A-Za-z0-9_-]+/)) print $1
        }' <<< "$out")"

    # Safety: if we could not read the live list at all, do NOT build a stripped
    # set (that would silently drop hermes defaults). Bypass instead.
    if [ -z "${base//[[:space:]]/}" ] && [ ! -f "$TOOLS_CONFIG" ]; then
        return 1
    fi

    # Overrides from tools.yaml.
    forced_in=""; forced_out=""
    if [ -f "$TOOLS_CONFIG" ]; then
        local key state
        while IFS= read -r key; do
            [ -z "$key" ] && continue
            state=$(sed -nE '/^[[:space:]]*'"$key"'[[:space:]]*:[[:space:]].*state:[[:space:]]*([A-Za-z]+)/ s/.*state:[[:space:]]*([A-Za-z]+).*/\1/p' "$TOOLS_CONFIG" | tr -d ' ')
            case "$state" in
                ON|on|true|True|1)    forced_in="  $forced_in $key";;
                OFF|off|false|False|0) forced_out=" $forced_out $key";;
            esac
        done < <(sed -nE 's/^[[:space:]]*([A-Za-z0-9_-]+)[[:space:]]*:[[:space:]]*\{.*state:.*/\1/p' "$TOOLS_CONFIG")
    fi

    names="$base $forced_in"
    for n in $forced_out; do
        names=$(printf '%s\n' "$names" | sed -E "s/(^|[[:space:]])${n}([[:space:]]|$)/ /g")
    done

    echo "$names" | tr ' ' '\n' | sort -u | grep -v '^$' | paste -sd, -
}

_acquire_lock() {
    local tries=0
    while ! mkdir "$RACE_PROXY_LOCKDIR" 2>/dev/null; do
        tries=$((tries + 1))
        if [ "$tries" -gt 50 ]; then
            # Stale lock (crashed holder) — break it after ~5s of waiting.
            rmdir "$RACE_PROXY_LOCKDIR" 2>/dev/null
        fi
        sleep 0.1
    done
}

_release_lock() {
    rmdir "$RACE_PROXY_LOCKDIR" 2>/dev/null
}

_proxy_healthy() {
    curl -s -m 2 -o /dev/null -w "%{http_code}" "$RACE_PROXY_HEALTH_URL" 2>/dev/null | grep -q "^200$"
}

_start_proxy_if_needed() {
    _acquire_lock
    local count
    count=$(cat "$RACE_PROXY_REFCOUNT" 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > "$RACE_PROXY_REFCOUNT"
    if ! _proxy_healthy; then
        nohup "$PYTHON_BIN" "$RACE_PROXY_DIR/race_proxy.py" \
            --config "$RACE_PROXY_CONFIG" --verbose \
            > "$RACE_PROXY_LOG" 2>> "$RACE_PROXY_ERR" &
        echo $! > "$RACE_PROXY_PIDFILE"
        disown
        # Wait briefly so the first hermes call doesn't race a cold proxy.
        local waited=0
        while [ "$waited" -lt 30 ]; do
            if _proxy_healthy; then
                break
            fi
            sleep 0.1
            waited=$((waited + 1))
        done
    fi
    _release_lock
}

_stop_proxy_if_last() {
    _acquire_lock
    local count
    count=$(cat "$RACE_PROXY_REFCOUNT" 2>/dev/null || echo 1)
    count=$((count - 1))
    if [ "$count" -lt 0 ]; then
        count=0
    fi
    echo "$count" > "$RACE_PROXY_REFCOUNT"
    if [ "$count" -eq 0 ]; then
        if [ -f "$RACE_PROXY_PIDFILE" ]; then
            local pid
            pid=$(cat "$RACE_PROXY_PIDFILE" 2>/dev/null)
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null
            fi
            rm -f "$RACE_PROXY_PIDFILE"
        fi
    fi
    _release_lock
}

_start_proxy_if_needed

# Build the -t union unless the user passed their own -t/--toolsets (bypass).
RUN_ARGS=("$@")
if TOOLSETS=$(build_toolsets "$@") && [ -n "$TOOLSETS" ]; then
    RUN_ARGS=(-t "$TOOLSETS" "$@")
fi

# NOT `exec`: we must wait for hermes to return so we can decrement the
# refcount and tear the proxy down when this is the last session.
"$REAL_HERMES" "${RUN_ARGS[@]}"
HERMES_EXIT_CODE=$?

_stop_proxy_if_last

exit "$HERMES_EXIT_CODE"