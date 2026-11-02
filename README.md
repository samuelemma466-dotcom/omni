# omni

A permissioned local AI agent for **Omarchy / Arch / Hyprland**.

Omni is the core of a floating assistant: a local daemon that can read and write files,
run commands, inspect and flash USB devices, install drivers, and call plugins — but every
dangerous action goes through an explicit approval gate and an audit log. The chat overlay is
a Quickshell plugin that talks to the daemon over localhost SSE.

```
omni CLI ──┐
           ├── permission broker ── audit log (~/.local/state/omni/audit.jsonl)
omni serve ┘        │
                    ├── built-in tools: fs.*, shell.exec, sys.info
                    └── plugins: usb.* (detect, driver recipes, flash)
                              │
                              └── LLM: Ollama (local) or free cloud APIs
```

The overlay is a separate plugin under `omarchy-plugin/`. It starts `omni serve` on demand
and renders tool calls and approval requests inline; it never bypasses the broker.

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
ollama pull qwen2.5:1.5b    # ~1 GB, the default for slow CPUs
# then set providers.ollama.model = "qwen2.5:1.5b" in ~/.config/omni/config.toml
```

If Ollama is not running:

```bash
systemctl --user enable --now ollama   # user unit, models in ~/.ollama/models
ollama pull qwen2.5:1.5b               # ~1 GB; qwen2.5:3b is smarter but ~2x slower
```

The repo ships no Ollama unit; the one used on this machine is
`~/.config/systemd/user/ollama.service` and binds `127.0.0.1:11434`.

### Fallbacks: local first, free cloud second

Omni builds a provider chain and moves to the next entry when one fails
(offline, out of memory, timeout). The default chain is:

```
ollama -> gemini -> groq -> openrouter -> pollinations
```

Only providers that can actually work are kept: `gemini`, `groq` and
`openrouter` are skipped unless their key is set, and `pollinations` needs no
key at all. So on a machine with no keys the chain is just
`ollama -> pollinations`, and Omni still answers when Ollama is down — at the
cost of sending that prompt to a third party.

A turn also moves down the chain when the local model is **too slow**
(`providers.ollama.timeout`, default 60 s) or returns a malformed tool call.
That keeps a cold 2011-era CPU from hanging the overlay for minutes: the slow
local attempt is abandoned and the free provider answers instead.

```bash
export GEMINI_API_KEY=...        # free tier: https://aistudio.google.com/apikey
# or GROQ_API_KEY / OPENROUTER_API_KEY
```

Change the order or disable fallbacks in `~/.config/omni/config.toml`:

```toml
[agent]
fallback = ["gemini", "pollinations"]   # or `fallback = false` for local only
```

### Tuning a slow CPU

On a memory-constrained machine, cap the model's footprint in
`[providers.ollama]`:

```toml
num_ctx = 2048      # KV cache size; 4096 doubles it
num_predict = 512   # max generated tokens per model call
num_thread = 4      # 0 = let Ollama decide
keep_alive = "5m"   # unload the model when idle, giving RAM back
timeout = 60        # give up and use the next provider
```

A model is only loaded with new options after it is reloaded, so restart
Ollama after changing `num_thread`:

```bash
systemctl --user restart ollama
```

### What to expect on old hardware

Measured on a 2011 dual-core i5 with 8 GB of RAM and the desktop running
(`qwen2.5:1.5b`, 2k context, 4 threads):

| | |
|---|---|
| first turn after a reload | ~25–40 s (loads the model, evaluates the prompt) |
| later turns, same prompt prefix | ~1–3 s — Ollama reuses the cached prompt |
| long answers | ~1–2 tokens/s, so keep answers short |
| tool calls on 1.5B | unreliable; 3B is much better but roughly twice as slow |

The cached-prefix behaviour is why the system prompt and tool list are kept
stable: changing either one throws the cache away and the next turn is slow
again.

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

## Phones plugin

Detects phones on USB and reads their details — **no developer mode required** for the
basics, and it works on Android and iPhone alike.

```bash
./bin/omni call phones.detect     # who is on the bus + MTP/adb/apple details + hints
./bin/omni call phones.watch --args '{"seconds":120}'   # live plug/unplug window
./bin/omni call phones.log        # recent events from the persistent log
```

What each bridge gives you, no phone settings touched:

| Bridge | Needs | Reads |
|---|---|---|
| MTP (`mtp-detect`) | nothing | any Android: model, OS version, serial, storage stats |
| adb (`android-tools`) | USB debugging on the phone | getprop details, then install/screenshot/logcat later |
| libimobiledevice (`ideviceinfo`) | `usbmuxd` running | iPhone/iPad model, iOS version, serial |

`phones.watch` watches kernel uevents via `udevadm` (no sudo), classifies each plug/unplug
as phone/hub/unknown, attaches MTP identity when a phone appears, and appends to
`~/.local/state/omni/phones/events.jsonl` so the overlay can replay history.

When no phone is found, `phones.detect` explains the usual suspects — charge-only cable
(no data wires), dead port, or charge-only USB mode — because the most common "detector is
broken" report is really a cable that only carries power.

Bootloader/fastboot flashing and per-vendor unlock recipes are deliberately out of scope
for now: they require OEM unlocking on the phone and differ per vendor.

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

## Floating overlay

The overlay is a normal Omarchy/Quickshell plugin in `omarchy-plugin/`. It is not a second
Quickshell process: it mounts inside `omarchy-shell` as a `keepLoaded` overlay and uses
layer-shell keyboard focus only while open.

```bash
cd ~/Projects/omni
./omarchy-plugin/install.sh            # link CLI + plugin, enable, add keybind
./omarchy-plugin/install.sh --restart  # same, then restart the shell
```

What the installer does:

- links `bin/omni` to `~/.local/bin/omni` (symlink-safe launcher);
- runs `omni init` so the config and token exist;
- links this repo's `omarchy-plugin/` to `~/.config/omarchy/plugins/jovibe.omni`;
- enables the plugin and adds `SUPER + ALT + O` to `~/.config/hypr/bindings.lua`.

`SUPER + SHIFT + A` is the stock ChatGPT webapp binding, so Omni deliberately uses
`SUPER + ALT + O` instead of shadowing it. Change the line in `bindings.lua` if you prefer
another key.

Open it with `SUPER + ALT + O` or:

```bash
omarchy-shell shell toggle jovibe.omni
```

The overlay starts `omni serve` on first use (and restarts it with backoff if it dies). The
daemon log is `~/.local/state/omni/daemon.log`; the token is read from
`~/.local/state/omni/token`. If you change `server.port` in the config, export `OMNI_PORT`
in the shell's environment as well, because the launcher health-checks that port before
starting a second daemon.

The transcript renders `status`, `tool_call`, and `tool_result` events as they arrive.
When the broker asks for approval, a card appears with the tool, risk level, reason,
capabilities, and a diff/argument preview:

- **Allow once** approves that single call;
- **Allow session** grants that tool for the approval window (hidden for high-risk tools);
- **Deny** refuses it; the agent sees the denial and can suggest an alternative.

Closing the overlay does not cancel a running turn; reopen it to see the result. **Stop**
cancels the current turn and denies any pending approvals.

If the shell is already running, plugin *manifests* reload automatically when the symlink
changes, but Quickshell caches QML. After editing `Omni.qml`, run `omarchy-restart-shell`
(or the installer with `--restart`) to load the new code.

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
- [x] Quickshell overlay on a Hyprland keybind
- [ ] Token-by-token streaming
- [ ] Service bridges (Jarvis, Omachy, KDE Connect)
- [ ] More recipes (scanners, printers, serial adapters)
