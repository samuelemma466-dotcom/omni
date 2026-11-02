#!/usr/bin/env bash
#
# Launch the Omni daemon from the overlay plugin.
#
# stdout/stderr are appended to ~/.local/state/omni/daemon.log so the daemon's
# log survives even though omarchy-shell does not forward child output to the
# journal. pdeathsig makes the daemon exit if omarchy-shell dies.
#
# Exit codes used by Omni.qml:
#   0   another daemon already owns the port (healthy), or a clean shutdown
#   78  the omni executable is missing (do not restart)
#   *   the daemon exited unexpectedly (the overlay backs off and retries)

set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omni"
LOG="$STATE_DIR/daemon.log"
PORT="${OMNI_PORT:-8737}"
OMNI_BIN="${OMNI_BIN:-$HOME/.local/bin/omni}"

# Free Gemini API key (free tier: https://aistudio.google.com/apikey)
# Required for Gemini to appear in the provider chain.
export GEMINI_API_KEY="${GEMINI_API_KEY:-AQ.Ab8RN6KPXa2cwtXyvSL8hKqsnRqGt2rK8a9qghnAKWQjWRp_2g}"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

if [[ -f "$LOG" ]] && (( $(stat -c %s "$LOG" 2>/dev/null || echo 0) > 1048576 )); then
  mv -f "$LOG" "$LOG.1"
fi

if [[ ! -x "$OMNI_BIN" ]]; then
  echo "omni: executable not found at $OMNI_BIN" >>"$LOG"
  exit 78
fi

# Never fight a daemon that is already serving the port (a manual `omni serve`,
# or a previous overlay instance that has not exited yet).
if curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "omni: daemon already running on port $PORT" >>"$LOG"
  exit 0
fi

echo "omni: starting daemon $(date -Is)" >>"$LOG"
exec setpriv --pdeathsig TERM "$OMNI_BIN" serve >>"$LOG" 2>&1
