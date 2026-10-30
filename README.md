# omni

A permissioned local AI agent for **Omarchy / Arch / Hyprland**.

Omni is the core of a planned floating assistant: a local daemon that can read and write files,
run commands, inspect and flash USB devices, install drivers, and call plugins — but every
dangerous action goes through an explicit approval gate and an audit log. The chat overlay is
the next stage; the core and the USB plugin are usable today.

```
omni CLI ──┐
           ├── permission broker ── audit log (~/.local/state/omni/audit.jsonl)
omni serve ┘        │
                    ├── built-in tools: fs.*, shell.exec, sys.info
                    └── plugins: usb.* (detect, driver recipes, flash)
                              │
                              └── LLM: Ollama (local) or free cloud APIs
```

## Quick start

```bash
cd ~/Projects/omni

./bin/omni init       # writes ~/.config/omni/config.toml + a local API token
./bin/omni doctor     # checks provider, plugins, USB tooling
./bin/omni tools      # list every tool and its risk level
```

If a local model is installed, `doctor` resolves to Ollama automatically.
To use a specific local model:

```bash
ollama pull qwen2.5:3b
# then set agent.model = "qwen2.5:3b" in ~/.config/omni/config.toml
```

If Ollama is not running:

```bash
systemctl --user enable --now ollama   # or: ollama serve
ollama pull qwen2.5:1.5b               # ~1 GB, fastest option on CPU
```

Local inference is CPU-only on this machine (4 cores), so a two-step tool call can take
30–90 seconds. For snappier answers, use a free cloud key:

```bash
export GEMINI_API_KEY=...        # free tier: https://aistudio.google.com/apikey
# or GROQ_API_KEY / OPENROUTER_API_KEY
```

Then set `agent.provider = "gemini"` (or `groq` / `openrouter`) in the config.

## Chat

```bash
./bin/omni chat "how much disk space is left?"
./bin/omni chat "list my USB devices and match them to driver recipes"
./bin/omni chat                    # interactive REPL
./bin/omni chat "..." --yes        # auto-approve prompts (still audited)
./bin/omni chat "..." --dry-run    # tools plan only, nothing changes
```

The agent streams tool calls, asks for approval when required, then answers. Approval prompts
show the tool, the risk level, the arguments, and a diff for file writes:

```
  approval needed: fs.write  [medium]
  why: write requires approval
  args: {"path": "~/notes/todo.md", "content": "- ship omni\n"}
  preview:
    --- a/~/notes/todo.md
    +++ b/~/notes/todo.md
    @@ -1 +1 @@
    -old
    +- ship omni
  allow? [y]es / [s]ession / [N]o:
```

`session` approves that tool for the rest of the approval window (default 300 s).

## Permissions

`~/.config/omni/config.toml`:

```toml
[policy]
read = "allow"      # allow | ask | deny
write = "ask"
exec = "ask"
network = "ask"
device = "ask"
write_roots = ["~", "/tmp"]
```

Hard rules that override policy:

- `high` danger tools always ask (unless you pass `--yes`).
- Writes outside `write_roots` ask.
- Writes to `/etc`, `/usr`, `/boot`, `/var`, `/opt`, `/dev`, `~/.ssh`, `~/.gnupg` always ask.
- Dry runs never ask and never change anything.
- Every decision and invocation is appended to `~/.local/state/omni/audit.jsonl`.

`shell.exec` never uses a shell: pass an `argv` list (or a `command` string that is split with
`shlex`). Pipes, redirection, and `$()` are not interpreted.

## USB plugin

```bash
./bin/omni call usb.list
./bin/omni call usb.drives
./bin/omni call usb.drives --args '{"all":true}'
./bin/omni call usb.recipes --args '{"all":true}'
```

### Install a driver recipe

The bizhub C3351 installer is now the first recipe. Detection is automatic; dry-run is the
default:

```bash
# show the plan
./bin/omni call usb.install_driver \
  --args '{"recipe":"bizhub-c3351","force":true}' --dry-run

# run it (needs a terminal because makepkg calls sudo)
./bin/omni call usb.install_driver \
  --args '{"recipe":"bizhub-c3351","dry_run":false}' --interactive --yes
```

Recipes live in `plugins/usb/recipes/*.json` and are plain JSON:

```json
{
  "id": "my-device",
  "name": "My USB device",
  "vendor": "1234",
  "product": "5678",
  "source_dir": "~/drivers/my-device",
  "steps": [
    {"kind": "pacman", "packages": ["cups"]},
    {"kind": "command", "argv": ["systemctl", "enable", "--now", "cups"], "sudo": true},
    {"kind": "copy", "from": "{source_dir}/driver.tar.gz", "to": "{tmp}/driver.tar.gz"},
    {"kind": "command", "argv": ["makepkg", "-fsi", "--noconfirm"], "cwd": "{tmp}"},
    {"kind": "verify", "argv": ["lsusb", "-d", "1234:5678"]}
  ]
}
```

Supported step kinds: `pacman`, `command`, `copy`, `verify`, `cups_usb_queue`, `print_test`.
Placeholders: `{user}`, `{home}`, `{tmp}`, `{recipe_dir}`, `{source_dir}`.

### Flash a USB drive

Dry-run by default. Real writes require `confirm` to equal the device path.

```bash
# plan
./bin/omni call usb.flash \
  --args '{"image":"~/Downloads/omarchy.iso","device":"/dev/sdb"}' --dry-run

# write for real
./bin/omni call usb.flash \
  --args '{"image":"~/Downloads/omarchy.iso","device":"/dev/sdb","dry_run":false,"confirm":"/dev/sdb"}' \
  --yes
```

Safety guards, all enforced before any write:

- must be a whole disk, never a partition;
- must be removable/USB unless `allow_internal=true`;
- refuses the disk that holds the running system (including LUKS/LVM layouts);
- refuses an image larger than the target;
- refuses mounted partitions unless `unmount=true`;
- optional `sha256` verification;
- real writes need `confirm=<device>`.

Writes use `dd bs=4M conv=fsync`, then `sync` and `blockdev --rereadpt`. Privileged steps use
`sudo` in an interactive terminal, otherwise `pkexec`.

## Server (for the overlay)

```bash
./bin/omni serve          # 127.0.0.1:8737
```

Endpoints (all except `/health` require `X-Omni-Token` or `?token=`; the token is in
`~/.local/state/omni/token`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/api/tools` | tool schemas |
| GET | `/api/plugins` | loaded plugins |
| GET | `/api/approvals` | pending approvals |
| POST | `/api/approvals/<id>` | `{"approved":true,"scope":"once"}` |
| GET | `/api/audit?limit=20` | recent audit events |
| POST | `/api/chat` | SSE stream; body `{"message":"...","history":[]}` |

SSE event types: `status`, `tool_call`, `approval_required`, `tool_result`, `assistant`,
`error`, `done`. The chat request stays open while an approval is pending; the UI posts the
decision on a second connection.

## Plugins

A plugin is a directory with `manifest.json` and a Python `handler.py`:

```
plugins/myplugin/
  manifest.json
  handler.py
```

`handler.py` exports either:

```python
def run(tool_name, args, ctx):
    return {"ok": True, "data": ...}
```

or `TOOLS = {"my.tool": callable}`. Tool calls return a dict or a `ToolResult`.
Plugins are loaded from `~/.config/omni/plugins/*` and `plugins/*` in this repo.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Roadmap

- [x] Permission broker, approvals, audit log
- [x] Built-in file/shell/system tools
- [x] Ollama + free cloud providers
- [x] USB detection, driver recipes, guarded flashing
- [x] Local HTTP/SSE daemon
- [ ] Quickshell overlay on a Hyprland keybind
- [ ] Token-by-token streaming
- [ ] Service bridges (Jarvis, Omachy, KDE Connect)
- [ ] More recipes (scanners, printers, serial adapters)
