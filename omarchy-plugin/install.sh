#!/usr/bin/env bash
#
# Install the Omni overlay plugin for Omarchy.
#
# Idempotent: safe to re-run. It links the CLI and the plugin into place, makes
# sure config/token exist, enables the plugin, and adds a Hyprland keybind.
# Nothing is overwritten except the two symlinks it owns.

set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_SRC="$REPO/omarchy-plugin"
PLUGIN_ID="jovibe.omni"
PLUGIN_DST="$HOME/.config/omarchy/plugins/$PLUGIN_ID"
BIN_DST="$HOME/.local/bin/omni"
BINDINGS="$HOME/.config/hypr/bindings.lua"
KEYBIND='o.bind("SUPER + ALT + O", "Omni assistant", "omarchy-shell shell toggle jovibe.omni")'

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }

[[ -x "$REPO/bin/omni" ]] || { warn "missing $REPO/bin/omni"; exit 1; }
chmod +x "$REPO/bin/omni" "$PLUGIN_SRC/run-daemon.sh" "$PLUGIN_SRC/install.sh"

say "Linking the omni CLI into ~/.local/bin"
mkdir -p "$HOME/.local/bin"
ln -sfn "$REPO/bin/omni" "$BIN_DST"

say "Ensuring config and server token"
"$BIN_DST" init

say "Linking the plugin into ~/.config/omarchy/plugins"
mkdir -p "$HOME/.config/omarchy/plugins"
ln -sfn "$PLUGIN_SRC" "$PLUGIN_DST"

if command -v omarchy >/dev/null 2>&1; then
  if omarchy plugin validate "$PLUGIN_SRC" >/dev/null 2>&1; then
    say "Manifest validated"
  else
    warn "manifest validation failed; run: omarchy plugin validate $PLUGIN_SRC"
  fi
  # The shell discovers plugins from disk. Enable is best-effort: a fresh
  # third-party plugin is enabled by default, but an explicit enable also
  # records it in shell.json for users who disabled it before.
  omarchy plugin enable "$PLUGIN_ID" >/dev/null 2>&1 \
    || warn "could not enable $PLUGIN_ID automatically; enable it from the Omarchy menu"
fi

say "Adding the Hyprland keybind"
if [[ -f "$BINDINGS" ]]; then
  if grep -q "jovibe.omni" "$BINDINGS"; then
    say "Keybind already present"
  else
    {
      printf '\n'
      printf '%s\n' "-- Omni assistant overlay (Super+Alt+O)"
      printf '%s\n' "$KEYBIND"
    } >>"$BINDINGS"
    say "Appended to $BINDINGS"
  fi
else
  warn "$BINDINGS not found; add this line yourself:"
  printf '  %s\n' "$KEYBIND" >&2
fi

if command -v hyprctl >/dev/null 2>&1; then
  hyprctl reload >/dev/null 2>&1 || true
fi

if [[ "${1:-}" == "--restart" ]]; then
  say "Restarting the Omarchy shell"
  omarchy-restart-shell || warn "shell restart failed; restart it from the Omarchy menu"
fi

say "Done"
cat <<EOF

Omni is installed.

  Open:    Super+Alt+O
           (or: omarchy-shell shell toggle $PLUGIN_ID)
  CLI:     omni chat
  Daemon:  started by the overlay on demand
  Log:     ~/.local/state/omni/daemon.log

Choose a model:
  ollama serve & ollama pull qwen2.5:3b
  # or export GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY,
  # then restart the shell so the daemon inherits it.

Check the install:
  omni doctor

After updating the plugin code, reload it:
  omarchy-restart-shell          # or rerun this installer with --restart
EOF
