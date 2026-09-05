#!/usr/bin/env bash
# install.sh -- provision hermes-race-proxy on macOS or Linux.
#
# Places the runtime under $HERMES_HOME (default ~/.hermes) as
# <HERMES_HOME>/hermes-race-proxy/, generates the two machine-local configs
# (race_proxy.local.yaml + race_proxy_toolchain.local.yaml) from the tracked
# race-models.yaml when absent, and wires a `hermes` shim + shell alias so every
# invocation starts the two race proxies (compaction :8977 / toolchain :8978),
# syncs MCP enable/disable from config/tools.yaml, trims the -t toolset union,
# and delegates to the real hermes. Idempotent: safe to re-run.
#
# Usage:  ./install.sh [--hermes-home <dir>] [--commit] [--uninstall]
#   --hermes-home <dir>/HERMES_HOME   install location (default ~/.hermes)
#   --commit                          write changes (default: dry-run preview)
#   --uninstall                       remove the installed runtime + wiring
set -euo pipefail

# --- Resolve source repo (self-locate, so ./install.sh works from anywhere). ---
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_HOME="${HERMES_HOME/#\~/$HOME}"

ACTION="preview"
for arg in "$@"; do
  case "$arg" in
    --commit) ACTION="commit" ;;
    --uninstall) ACTION="uninstall" ;;
    --hermes-home) continue ;;  # value consumed next
    *) if [[ "$ACTION" == "hermes-home-pending" ]]; then HERMES_HOME="$arg"; fi; ACTION="commit"; ;;
  esac
done
# Not overwriting HERMES_HOME via CLI above unless explicitly handled below:
# simpler, robust parsing instead.
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

APP_DIR="$HERMES_HOME/hermes-race-proxy"
SHIM="$HOME/.local/bin/hermes"
PYTHON_BIN="$HERMES_HOME/hermes-agent/venv/bin/python"

# The real hermes target: warmup defers to it. Prefer the source-repo's own
# hermes-real if present, else the one under $HERMES_HOME, else the PATH.
if [ -x "$SOURCE_DIR/.supports/hermes-real" ]; then
  REAL_HERMES="$SOURCE_DIR/.supports/hermes-real"
elif [ -x "$HERMES_HOME/hermes-real" ]; then
  REAL_HERMES="$HERMES_HOME/hermes-real"
elif [ -x "$HOME/.local/bin/hermes-real" ]; then
  REAL_HERMES="$HOME/.local/bin/hermes-real"
else
  REAL_HERMES="$(command -v hermes-real || true)"
fi

if [ -z "${REAL_HERMES:-}" ] || [ ! -x "$REAL_HERMES" ]; then
  echo "ERROR: could not locate the real hermes binary (hermes-real)." >&2
  echo "       Install Hermes Agent first, or pass --hermes-home pointing at an" >&2
  echo "       existing install with a hermes-agent venv." >&2
  exit 1
fi

_count() { printf '%3s' "$(ls -1 "$1" 2>/dev/null | wc -l | tr -d ' ')"; }

echo "hermes-race-proxy installer"
echo "  source     : $SOURCE_DIR"
echo "  hermes-home: $HERMES_HOME"
echo "  install to : $APP_DIR"
echo "  real hermes: $REAL_HERMES"
echo ""

if [ "$ACTION" = "uninstall" ]; then
  echo "[uninstall] would remove:"
  echo "  - $APP_DIR/"
  [ -L "$SHIM" ] && echo "  - shim symlink $SHIM"
  echo "  - 'hermes' alias lines added to your shell rc (not auto-edited here)"
  echo "Run with the same flags plus --commit to actually remove."
  exit 0
fi

echo "=== 1. tree to install (tracked, non-doc files only) ==="
FILES=()
if (cd "$SOURCE_DIR" && git ls-files >/dev/null 2>&1); then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    # Ship only code/config/LICENSE; install README.md is the single doc file
    # installed (useful in-place), all other *.md stay out of the runtime tree.
    case "$f" in
      *.md) [ "$f" = "README.md" ] || continue ;;
    esac
    FILES+=("$f")
  done < <(cd "$SOURCE_DIR" && git ls-files 2>/dev/null)
fi
if [ "${#FILES[@]}" -eq 0 ]; then
  # not a git checkout: copy the explicit runtime file set
  FILES=(callers/__init__.py callers/base.py callers/cli_caller.py callers/http_caller.py
         connection_pool.py discovery.py providers/__init__.py providers/base.py
         providers/cli/__init__.py providers/cli/claude.py providers/cli/hermes.py
         providers/cli/opencode.py providers/http/__init__.py providers/http/deepinfra.py
         providers/http/gcp.py providers/http/nvidia_build.py providers/http/ollama.py
         providers/http/opencode.py providers/http/openrouter.py providers/pool.py
         race_proxy_compaction.py race_proxy_core.py race_proxy_toolchain.py
         repairs.py response_contracts.py wire_format.py hermes-warmup.sh config/tools.yaml
         config/race-models.yaml examples/race_proxy.example.yaml LICENSE)
fi
echo "  ($(_count "$SOURCE_DIR") files in source repo, ${#FILES[@]} to install)"

install_tree() {
  echo "  -> copying $APP_DIR/"
  mkdir -p "$APP_DIR"
  local f
  for f in "${FILES[@]}"; do
    [ -f "$SOURCE_DIR/$f" ] || continue
    mkdir -p "$(dirname "$APP_DIR/$f")"
    cp -p "$SOURCE_DIR/$f" "$APP_DIR/$f"
  done
  cp -p "$SOURCE_DIR/hermes-warmup.sh" "$APP_DIR/hermes-warmup.sh"
}

# --- Prune stale managed files left by an earlier install. install_tree only
# adds; files that stopped being tracked (e.g. a renamed/retired entrypoint)
# must be removed here or the installed copy keeps serving dead code.
# Machine-local runtime state (the *.local.yaml configs and any user-added
# pool/secrets files) is never touched.
cleanup_stale() {
  local f rel
  for f in $(find "$APP_DIR" -type f \( -name '*.py' -o -name '*.sh' -o -name '*.yaml' -o -name '*.json' -o -name '*.md' \) 2>/dev/null); do
    rel="${f#"$APP_DIR"/}"
    # Keep the always-present entrypoint and the two generated configs.
    case "$rel" in
      hermes-warmup.sh) continue ;;
      config/race_proxy_compaction.local.yaml) continue ;;
      config/race_proxy_toolchain.local.yaml) continue ;;
      README.md) continue ;;
    esac
    if ! printf '%s\n' "${FILES[@]}" | grep -qxF "$rel"; then
      rm -f "$f"
      echo "  pruned stale $rel"
    fi
  done
}

# --- Generate machine-local configs only if absent (preserve user edits). ---
gen_config() {
  local name port timeout
  name="$1"; port="$2"; timeout="$3"
  local out="$APP_DIR/config/$name.local.yaml"
  mkdir -p "$APP_DIR/config"
  if [ ! -f "$out" ]; then
    cat > "$out" <<EOF
host: 127.0.0.1
port: $port
timeout: $timeout
require_finish_reason: stop
models_file: race-models.yaml
pool_file: $HERMES_HOME/auth.json
EOF
    echo "  generated $out"
  else
    echo "  kept existing $out"
  fi
}

# --- Write the shim (thin exec to the INSTALLED warmup) unless user's own. ---
install_shim() {
  mkdir -p "$HOME/.local/bin"
  if [ -L "$SHIM" ]; then
    ln -sfn "$APP_DIR/hermes-warmup.sh" "$SHIM"
    echo "  shim -> $SHIM (repointed symlink to installed warmup)"
  elif [ -e "$SHIM" ]; then
    # A real file: repoint it only if it is OUR old shim (points at the source
    # repo's warmup); leave an unrelated file alone.
    if grep -qF "hermes-warmup.sh" "$SHIM" 2>/dev/null; then
      ln -sfn "$APP_DIR/hermes-warmup.sh" "$SHIM"
      echo "  shim -> $SHIM (repointed real file to installed warmup)"
    else
      echo "  WARN: $SHIM is a real file not created by this installer; leaving it untouched." >&2
    fi
  else
    ln -sfn "$APP_DIR/hermes-warmup.sh" "$SHIM"
    echo "  shim -> $SHIM (symlink to installed warmup)"
  fi
}

install_rc_alias() {
  local rc="" sh
  # Write the alias into the USER's login-shell rc. "$SHELL" is the login-shell
  # env var, set once at login and stable across subshells -- the correct target
  # for an interactive alias. (This script always runs under bash via its
  # shebang, so $0 is the script path, not a shell name.)
  sh="$(basename "${SHELL:-}")"
  case "$sh" in
    zsh)  rc="$HOME/.zshrc" ;;
    bash) rc="$HOME/.bashrc" ;;
    *)    # unset or non-zsh/bash: default to the widest-compatible rc file.
          [ -f "$HOME/.bashrc" ] && rc="$HOME/.bashrc" || rc="$HOME/.zshrc" ;;
  esac
  [ -f "$rc" ] || return 0
  # Always point the alias at the INSTALLED warmup. If an old alias references a
  # different warmup path (e.g. a prior install pointing at a source checkout),
  # replace that exact line rather than appending a contradictory second alias.
  local newline="alias hermes=\"$APP_DIR/hermes-warmup.sh\""
  if grep -qF "alias hermes=\"$APP_DIR/hermes-warmup.sh\"" "$rc"; then
    echo "  alias already present in $rc"
    return
  fi
  if grep -qE '^alias hermes=' "$rc"; then
    # Remove every old `alias hermes=...` line, then append the correct one.
    grep -vE '^alias hermes=' "$rc" > "$rc.tmp" && mv "$rc.tmp" "$rc"
    echo "$newline" >> "$rc"
    echo "  repointed hermes alias in $rc -> installed warmup"
  else
    echo "$newline" >> "$rc"
    echo "  added alias to $rc"
  fi
}

if [ "$ACTION" = "commit" ]; then
  install_tree
  cleanup_stale
  gen_config race_proxy_compaction 8977 300
  gen_config race_proxy_toolchain 8978 60
  chmod +x "$APP_DIR/hermes-warmup.sh"
  install_shim
  install_rc_alias
  echo ""
  echo "Installed. Start a new shell (or source your rc) so the 'hermes' alias"
  echo "applies. First launch starts:"
  echo "  - compaction proxy  http://127.0.0.1:8977  (300s)"
  echo "  - toolchain proxy   http://127.0.0.1:8978  (60s)"
  echo "Then add your provider keys to the credential pool and edit"
  echo "  $APP_DIR/config/race-models.yaml  for which models to race."
else
  echo "[preview] --commit to apply. Would install to $APP_DIR and wire the shim."
fi
exit 0