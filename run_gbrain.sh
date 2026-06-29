#!/bin/bash
# GBrain runner — uses project-local brain data and source
# Usage: bash run_gbrain.sh <command> [args...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GBRAIN_HOME_UNIX="$SCRIPT_DIR/data/gbrain"
export GBRAIN_HOME="$GBRAIN_HOME_UNIX"
# Load API keys from .env
if [ -f "$GBRAIN_HOME_UNIX/.gbrain/.env" ]; then
  set -a; source "$GBRAIN_HOME_UNIX/.gbrain/.env"; set +a
fi

# In WSL this script may resolve a Windows-hosted Bun from /mnt/c. That Bun
# runs as win32 and cannot read /mnt/... or /tmp/... paths directly.
if command -v wslpath >/dev/null 2>&1; then
  BUN_PLATFORM="$(bun -e 'console.log(process.platform)' 2>/dev/null || true)"
  if [ "$BUN_PLATFORM" = "win32" ]; then
    BRIDGE_ENV="GBRAIN_HOME/p"
    for name in OPENAI_API_KEY OPENAI_BASE_URL DEEPSEEK_API_KEY DEEPSEEK_BASE_URL DEEPSEEK_MODEL DASHSCOPE_API_KEY DASHSCOPE_BASE_URL; do
      if [ -n "${!name:-}" ]; then
        BRIDGE_ENV="$BRIDGE_ENV:$name"
      fi
    done
    export WSLENV="$BRIDGE_ENV${WSLENV:+:$WSLENV}"
    ARGS=()
    for arg in "$@"; do
      if [ -e "$arg" ]; then
        ARGS+=("$(wslpath -w "$arg")")
      else
        ARGS+=("$arg")
      fi
    done
    set -- "${ARGS[@]}"
  fi
fi

cd "$SCRIPT_DIR/gbrain"
bun run src/cli.ts "$@"
