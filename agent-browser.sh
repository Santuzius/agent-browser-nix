#!/usr/bin/env bash
# agent-browser: make sure the agent Vivaldi ("Agent" profile, its own data folder) is running with a DevTools port on 127.0.0.1, then optionally run a command.
#
# --class=agent-browser sets the Wayland app id (X11: WM_CLASS), so window managers can tell this window from the normal Vivaldi, e.g. to start it minimized.
#
#   agent-browser            start it if needed, print the DevTools WebSocket URL
#   agent-browser exec CMD…  start it if needed, then exec CMD (wraps the Browser MCP server)
#
# The profile lives in its own data folder because Chromium-based browsers refuse a DevTools port on their default data folder (~/.config/vivaldi). The port only listens on 127.0.0.1, but any local program can drive this browser and use its logins.
set -euo pipefail

data="${AGENT_BROWSER_DATA:-${XDG_CONFIG_HOME:-$HOME/.config}/agent-browser}"
port="${AGENT_BROWSER_PORT:-9333}"
vivaldi="${AGENT_BROWSER_VIVALDI:-vivaldi}"

version() { curl -sf --max-time 2 "http://127.0.0.1:$port/json/version"; }

if ! version >/dev/null; then
  mkdir -p "$data"
  touch "$data/First Run"
  setsid -f "$vivaldi" --user-data-dir="$data" --remote-debugging-port="$port" --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check --class=agent-browser >/dev/null 2>&1 </dev/null
  for _ in $(seq 1 60); do
    version >/dev/null && break
    sleep 0.25
  done
  version >/dev/null || { echo "agent-browser: Vivaldi did not open its DevTools port $port within 15 s" >&2; exit 1; }
fi

if [ "${1:-}" = exec ]; then
  shift
  exec "$@"
fi
version | sed -n 's/.*"webSocketDebuggerUrl": *"\([^"]*\)".*/\1/p'
