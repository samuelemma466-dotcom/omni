# Omni overlay plugin

A Quickshell overlay for `omarchy-shell`. It is the chat surface for the
permissioned agent in the parent repo; all tool calls, approvals, and audit
records still live in `omni serve`.

## Install

```bash
cd ~/Projects/omni
./omarchy-plugin/install.sh
```

The installer links the CLI, creates the config/token, links this directory to
`~/.config/omarchy/plugins/jovibe.omni`, enables the plugin, and adds
`SUPER + ALT + O` to `~/.config/hypr/bindings.lua`.

Re-run with `--restart` after changing `Omni.qml`; Quickshell caches QML even
though the plugin manifest hot-reloads.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | Omarchy plugin manifest (`kinds: ["overlay"]`, `keepLoaded: true`) |
| `Omni.qml` | the overlay UI, SSE client, approval cards, daemon supervisor |
| `run-daemon.sh` | starts `omni serve` with `pdeathsig`, a health check, and a log |
| `install.sh` | idempotent linker/enabler |

## How it talks to the daemon

- Reads the token from `~/.local/state/omni/token`.
- `POST /api/chat` with `curl -N`; each `data: {...}` line is parsed as an SSE
  event (`status`, `tool_call`, `approval_required`, `tool_result`,
  `assistant`, `error`, `done`).
- Approvals are posted to `/api/approvals/<id>` with
  `{"approved": bool, "scope": "once"|"session"}`.
- The daemon is started on plugin load and health-checked every 5 s while the
  overlay is open. `run-daemon.sh` refuses to start a second instance when the
  port already answers.

## Debugging

```bash
# Is the plugin discovered and enabled?
omarchy plugin list | grep jovibe.omni

# Open it without the keybind
omarchy-shell shell toggle jovibe.omni

# Inspect live overlay state
omarchy-shell shell call jovibe.omni debugState ''

# Daemon log
tail -f ~/.local/state/omni/daemon.log

# Shell/QML errors
journalctl --user -t omarchy-shell -f
```

`debugState` returns `opened`, `serverOk`, `busy`, token length, transcript and
approval counts, and the computed card/panel size.

## Permissions

The overlay can only approve or deny a request the broker already decided to
ask about. It cannot call a tool directly, bypass `write_roots`, or skip the
high-risk rule. The server enforces the same policy for the CLI and the
overlay.
