# dikodingbot

> A single-operator Telegram bot that runs the [Claude Code](https://claude.com/claude-code) CLI in a chosen local workspace.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Send text or files to your bot on Telegram; dikodingbot forwards them to the local `claude` CLI inside a selected workspace, streams progress back, and remembers the Claude session per workspace so follow-up messages continue the conversation.

> [!WARNING]
> **This bot executes Claude Code on your machine with a Telegram front-end. Treat it as remote code execution.** The default permission mode is `dontAsk` (autonomous for normal work, destructive actions blocked), and `/perm bypassPermissions` removes that guard entirely. Only expose it to a trusted operator on a trusted machine.

---

## Features

- 🔐 **Single-operator authorization** - only one Telegram user ID may talk to the bot; everyone else is rejected.
- 💾 **Per-workspace sessions** - Claude session UUIDs are persisted so `/switch`-ing back to a project resumes its conversation.
- 🧵 **Forum topics** - in a forum supergroup, each topic binds to a workspace folder with `/bind`; plain messages there run Claude in that folder, in parallel with other topics.
- 📡 **Live progress streaming** - Claude's response text streams into a single Telegram message as it's generated, alongside the current activity (tool name, thinking, …) and elapsed time. The full untruncated output is delivered at completion.
- 🧠 **Runtime model switching** - `/model` lists what your router offers and switches models without a restart. In a topic it scopes to that folder; in a private chat it sets the global default.
- ⚙️ **Runtime permission control** - `/perm` moves between `dontAsk`, `plan`, and `bypassPermissions` on the fly; the choice survives restarts. In a topic it scopes to that folder; in a private chat it sets the global default.
- 📎 **File uploads** - send any document; it lands in the active workspace and Claude sees it.
- 🛑 **Interruptible** - `/status` and `/cancel` remain responsive during long runs; `/cancel` SIGTERMs the whole process group cleanly.
- 🧭 **Path-traversal safe** - `/switch` and `/bind` are confined to `BASE_DIR` (including symlinks pointing outside); uploads strip path components.

## Requirements

- Python 3.10 or newer
- The [Claude Code](https://claude.com/claude-code) CLI installed and on `PATH` (or point `CLAUDE_BIN` at it)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID (message [@userinfobot](https://t.me/userinfobot))

## Installation

```bash
git clone https://github.com/firmanelhakim/dikodingbot.git
cd dikodingbot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your BOT_TOKEN and ALLOWED_USER_ID

python bot.py
```

## Configuration

All configuration is via environment variables (loaded from `.env`). See [`.env.example`](.env.example) for the annotated template.

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | - | Telegram bot token from @BotFather |
| `ALLOWED_USER_ID` | ✅ | `0` | Numeric Telegram user ID of the single allowed operator. `0` rejects everyone (fail-safe) |
| `CLAUDE_BIN` | | `claude` | Path to the Claude Code CLI binary |
| `BASE_DIR` | | `~/workspace` | Root folder for workspaces. `/projects` lists its subfolders; `/switch` is confined to it |
| `CLAUDE_TIMEOUT` | | `0` | Seconds before a hung `claude` call is killed. `0` = no timeout |
| `MAX_UPLOAD_BYTES` | | `26214400` | Upload size cap (25 MiB by default) |
| `CLAUDE_STREAM_LIMIT` | | `10485760` | Buffer cap for one line of `stream-json` output (10 MiB). Raise if a single tool result is bigger |
| `LIVE_EDIT_INTERVAL` | | `4.0` | Seconds between live-preview edits. Telegram counts edits against the same ~20/min per-chat quota as the final reply, so editing faster can starve delivery |
| `SEND_MAX_RETRIES` | | `3` | Retries for a message rejected with 429, each waiting the `retry_after` the API returns |
| `SEND_MAX_RETRY_WAIT` | | `60` | Longest single retry wait (seconds) before falling back to sending the output as a file |
| `SEND_CHUNK_DELAY` | | `0.4` | Pause between chunks of a long reply, so a multi-part answer doesn't trip the rate limit by itself |
| `SEND_RATE_LIMIT` | | `19.0` | Tokens/minute for the shared per-chat pacing bucket. Keeps a single run untouched and paces parallel topics down |
| `SEND_RATE_BURST` | | `5.0` | Burst allowance above the sustained rate, so a short answer goes out immediately |
| `SESSION_FILE` | | `sessions.json` (next to `bot.py`) | Where the per-workspace session map is persisted |
| `TOPICS_FILE` | | `topics.json` (next to `bot.py`) | Where the topic → folder binding map is persisted |
| `LOG_LEVEL` | | `INFO` | Python logging level |
| `CLAUDE_CONFIG_DIR` | | `~/.claude` | Where Claude Code stores session transcripts; read to check whether a session is resumable |
| `ANTHROPIC_BASE_URL` | | - | Optional: route Claude Code through a custom endpoint |
| `ANTHROPIC_API_KEY` | | - | Optional: API key for the above endpoint |
| `ANTHROPIC_MODEL` | | - | Optional: override model selection (initial value; `/model` supersedes it at runtime) |
| `MODEL_FILE` | | `active_model.txt` (next to `bot.py`) | Where the global `/model` selection is persisted across restarts |
| `MODELS_FILE` | | `models.json` (next to `bot.py`) | Where per-folder `/model` overrides are persisted (set inside a topic; empty otherwise) |
| `MODEL_CACHE_TTL` | | `60` | Seconds to cache the router `/v1/models` response |
| `PERMISSION_MODE` | | `dontAsk` | Starting global Claude permission mode (`bypassPermissions`, `plan`, …). Runtime-switchable via `/perm` |
| `PERMISSION_FILE` | | `active_permission.txt` (next to `bot.py`) | Where the global `/perm` selection is persisted |
| `PERMISSIONS_FILE` | | `permissions.json` (next to `bot.py`) | Where per-folder `/perm` overrides are persisted (set inside a topic; empty otherwise) |

## Usage

Once running, open Telegram, find your bot, and send:

- Any **text message** → forwarded to Claude as a prompt in the active workspace.
- Any **file/document** → downloaded into the active workspace, then Claude is asked to look at it.

### Commands

| Command | What it does |
|---|---|
| `/projects` | List folders in `BASE_DIR`; the active one is marked ✅ |
| `/switch <folder>` | Switch active workspace to `BASE_DIR/<folder>` (creates it if missing) |
| `/bind <folder>` | In a forum topic: bind this topic to `BASE_DIR/<folder>` (creates it if missing). A folder may be bound to only one topic |
| `/unbind` | In a forum topic: detach it from its folder |
| `/reset` | Clear the recorded Claude session UUID so the next message starts fresh |
| `/model [name]` | Show or switch the active Claude model (queries 9Router `/v1/models`). In a topic it sets that topic's folder only; in a private chat it sets the global default |
| `/perm [mode]` | Show or switch the Claude permission mode (`dontAsk` default; also `bypassPermissions`, `plan`). In a topic it sets that topic's folder only; in a private chat it sets the global default |
| `/status` | Show the running task's PID, elapsed time, and prompt preview |
| `/cancel` | Terminate the running Claude process group |
| `/list` | List files in the active workspace (directories first, then files) |
| `/list <subdir>` | List a subfolder of the active workspace |
| `/list -r [n]` | List recursively, `n` levels down from the start (default 2, max 6; prunes `.git`, `venv/`, `node_modules/`, caches) |
| `/code` | Send the active workspace as a timestamped ZIP (secrets, runtime state, caches, and backups excluded) |
| `/code <file>` | Send a single file from the active workspace |
| `/help` | Command menu |

Session state per workspace means: after `/switch project-a`, any prompt continues the Claude conversation you had in `project-a` last time; `/switch project-b` picks up wherever you left off there.

### Forum topics

The bot also works inside a forum supergroup, where each topic maps to one workspace folder. Enable Topics in the group settings, add the bot, and turn off privacy mode (`/setprivacy` -> Disable in BotFather). Then `/bind <folder>` in a topic maps it to `BASE_DIR/<folder>`, and plain messages in that topic run Claude there. Each folder may be bound to only one topic; a second `/bind` on a taken folder is refused. Topics run in parallel in different folders, each with its own session; two messages aimed at the same folder still serialize. The General topic and private chats have no thread id and use the single `/switch`-selected folder. `/perm` and `/model` inside a topic set that folder's mode or model only (persisted in `permissions.json` and `models.json`); the private-chat versions set the global defaults.

## Architecture

A small, focused Python codebase, flat layout, no framework beyond `python-telegram-bot`:

```
bot.py             # entry point - wires everything together
config.py          # env parsing and validation
session_store.py   # atomic {workspace: session-uuid} persistence
state.py           # RunState + one lock per workspace folder
topics.py          # topic -> folder routing and /bind persistence
telegram_io.py     # HTML escaping + rate-limit-aware delivery
auth.py            # single-operator authorization gate
runner.py          # subprocess spawn + stream-json parser
models.py          # /model persistence + 9Router /v1/models discovery
handlers.py        # all Telegram command / message handlers
tests/             # unit tests
```

Concurrency: one Claude run at a time per workspace folder, via a lock keyed by directory. Topics bound to different folders run in parallel; two messages aimed at the same folder serialize. `/status` and `/cancel` are registered with `block=False` so they remain responsive during a long run.

Process cleanup: Claude is spawned with `start_new_session=True` so `/cancel` can `killpg` the entire tree (including any tools Claude launched).

## Running tests

```bash
python -m unittest discover -s tests -p "test_*.py" -t .
```

Tests use minimal stubs for the `telegram` module so they run without the real dependency. Coverage includes:

- Env parsing and validation
- Authorization gate (including fail-safe behavior)
- Session store round-trip and corrupt-JSON tolerance
- Topic routing and persistence (`topics.json` round-trip, `resolve_dir` fallback)
- Path-traversal defense (`/switch` and `/bind` guards, including symlink escape)
- Per-folder concurrency (two folders in parallel, one folder serialized)
- Stream-JSON event parser (`_StreamState`), including live-preview rendering and oversized-line tolerance
- Delivery under flood control: `send_chunks` retry on 429, plain-text fallback, the file upload fallback, and the shared per-chat pacing bucket
- Model selection persistence (`/model` file/env precedence, per-folder `models.json` overrides)
- Permission-mode persistence (`/perm` file/env precedence, per-folder `permissions.json` overrides, bot-safe mode set)

## Security model

This bot is deliberately built for a **single trusted operator on a trusted machine**. The security posture is:

- ✅ Every incoming update is authorized against `ALLOWED_USER_ID`. `0` means "reject everyone."
- ✅ `/switch` and `/bind` cannot escape `BASE_DIR` (verified via `os.path.realpath` + `commonpath`, which also rejects symlinks pointing outside).
- ✅ Uploads strip client-supplied path components, refuse to overwrite, and enforce `MAX_UPLOAD_BYTES`.
- ✅ `/code` zips the active workspace but never `.env`, `sessions.json`, `topics.json`, `permissions.json`, `models.json`, `active_model.txt`, `active_permission.txt`, `*.bak-*` rotations, or `venv/` and cache directories, even when those are named explicitly.
- ⚠️ Claude runs with a configurable permission mode, default **`dontAsk`** (autonomous for normal operations, blocks destructive actions unless allowlisted). Switching to `bypassPermissions` via `/perm` or `PERMISSION_MODE` grants full local execution authority; the startup banner prints the active mode.
- ⚠️ Outbound messages are paced per chat (see `SEND_RATE_LIMIT`), but there is **no limit on how many Claude runs** an authorized user can trigger, and group mode runs with privacy off so the bot sees every topic message.

Recommended deployment:

1. Run as an unprivileged OS user.
2. Keep the bot on a trusted network, or behind a firewall.
3. Never commit `.env` (the `.gitignore` covers `.env*` including backup rotations).
4. Rotate `BOT_TOKEN` and any API keys immediately if they leak.
5. Prefer a container or sandbox for production use.

## Contributing

Contributions are welcome. Please:

1. Open an issue describing the change first for anything non-trivial.
2. Add tests for any new behavior.
3. Match the existing code style (type hints, docstrings, module-level `logging`).
4. Run the tests locally before opening a PR.

## License

[MIT](LICENSE) © dikodingbot contributors
