# dikodingbot

> A single-operator Telegram bot that runs the [Claude Code](https://claude.com/claude-code) CLI in a chosen local workspace.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Send text or files to your bot on Telegram; dikodingbot forwards them to the local `claude` CLI inside a selected workspace, streams progress back, and remembers the Claude session per workspace so follow-up messages continue the conversation.

> [!WARNING]
> **This bot executes Claude Code on your machine with a Telegram front-end - treat it as remote code execution.** The default permission mode is `dontAsk` (autonomous for normal work, destructive actions blocked), and `/perm bypassPermissions` removes that guard entirely. Only expose it to a trusted operator on a trusted machine.

---

## Features

- 🔐 **Single-operator authorization** - only one Telegram user ID may talk to the bot; everyone else is rejected.
- 💾 **Per-workspace sessions** - Claude session UUIDs are persisted so `/switch`-ing back to a project resumes its conversation.
- 📡 **Live progress streaming** - Claude's response text streams into a single Telegram message as it's generated, alongside the current activity (tool name, thinking, …) and elapsed time. The full untruncated output is delivered at completion.
- 🧠 **Runtime model switching** - `/model` lists what your router offers and switches models without a restart.
- ⚙️ **Runtime permission control** - `/perm` moves between `dontAsk`, `plan`, and `bypassPermissions` on the fly; the choice survives restarts.
- 📎 **File uploads** - send any document; it lands in the active workspace and Claude sees it.
- 🛑 **Interruptible** - `/status` and `/cancel` remain responsive during long runs; `/cancel` SIGTERMs the whole process group cleanly.
- 🧭 **Path-traversal safe** - `/switch` is confined to `BASE_DIR`; uploads strip path components.

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
| `SESSION_FILE` | | `sessions.json` (next to `bot.py`) | Where the per-workspace session map is persisted |
| `LOG_LEVEL` | | `INFO` | Python logging level |
| `CLAUDE_CONFIG_DIR` | | `~/.claude` | Where Claude Code stores session transcripts; read to check whether a session is resumable |
| `ANTHROPIC_BASE_URL` | | - | Optional: route Claude Code through a custom endpoint |
| `ANTHROPIC_API_KEY` | | - | Optional: API key for the above endpoint |
| `ANTHROPIC_MODEL` | | - | Optional: override model selection (initial value; `/model` supersedes it at runtime) |
| `MODEL_FILE` | | `active_model.txt` (next to `bot.py`) | Where the `/model` selection is persisted across restarts |
| `MODEL_CACHE_TTL` | | `60` | Seconds to cache the router `/v1/models` response |
| `PERMISSION_MODE` | | `dontAsk` | Starting Claude permission mode (`bypassPermissions`, `plan`, …). Runtime-switchable via `/perm` |
| `PERMISSION_FILE` | | `active_permission.txt` (next to `bot.py`) | Where the `/perm` selection is persisted |

## Usage

Once running, open Telegram, find your bot, and send:

- Any **text message** → forwarded to Claude as a prompt in the active workspace.
- Any **file/document** → downloaded into the active workspace, then Claude is asked to look at it.

### Commands

| Command | What it does |
|---|---|
| `/projects` | List folders in `BASE_DIR`; the active one is marked ✅ |
| `/switch <folder>` | Switch active workspace to `BASE_DIR/<folder>` (creates it if missing) |
| `/reset` | Clear the recorded Claude session UUID so the next message starts fresh |
| `/model [name]` | Show or switch the active Claude model (queries 9Router `/v1/models`) |
| `/perm [mode]` | Show or switch the Claude permission mode (`dontAsk` default; also `bypassPermissions`, `plan`) |
| `/status` | Show the running task's PID, elapsed time, and prompt preview |
| `/cancel` | Terminate the running Claude process group |
| `/code` | Send the whole source tree as a timestamped ZIP |
| `/code <file>` | Send a single source file (e.g. `/code runner.py`) |
| `/help` | Command menu |

Session state per workspace means: after `/switch project-a`, any prompt continues the Claude conversation you had in `project-a` last time; `/switch project-b` picks up wherever you left off there.

## Architecture

A small, focused Python codebase - flat layout, no framework beyond `python-telegram-bot`:

```
bot.py             # entry point - wires everything together
config.py          # env parsing and validation
session_store.py   # atomic {workspace: session-uuid} persistence
state.py           # RunState + claude_lock (in-flight process record)
telegram_io.py     # HTML escaping + rate-limit-aware delivery
auth.py            # single-operator authorization gate
runner.py          # subprocess spawn + stream-json parser
models.py          # /model persistence + 9Router /v1/models discovery
handlers.py        # all Telegram command / message handlers (incl. /perm)
tests/             # unit tests
```

Concurrency: one Claude run at a time via `asyncio.Lock`. `/status` and `/cancel` are registered with `block=False` so they remain responsive during a long run.

Process cleanup: Claude is spawned with `start_new_session=True` so `/cancel` can `killpg` the entire tree (including any tools Claude launched).

## Running tests

```bash
python -m unittest discover -s tests -p "test_*.py" -t .
```

Tests use minimal stubs for the `telegram` module so they run without the real dependency. Coverage includes:

- Env parsing and validation
- Authorization gate (including fail-safe behavior)
- Session store round-trip and corrupt-JSON tolerance
- Path-traversal defense (`/switch` guard)
- Stream-JSON event parser (`_StreamState`), including live-preview rendering and oversized-line tolerance
- Delivery under flood control: `send_chunks` retry on 429, plain-text fallback, and the file upload fallback
- Model selection persistence (`/model` file/env precedence)
- Permission-mode persistence (`/perm` file/env precedence, bot-safe mode set)

## Security model

This bot is deliberately built for a **single trusted operator on a trusted machine**. The security posture is:

- ✅ Every incoming update is authorized against `ALLOWED_USER_ID`. `0` means "reject everyone."
- ✅ `/switch` cannot escape `BASE_DIR` (verified via `os.path.commonpath`).
- ✅ Uploads strip client-supplied path components, refuse to overwrite, and enforce `MAX_UPLOAD_BYTES`.
- ✅ `/code` ships only an explicit allowlist of project files (source modules, docs, and `tests/*.py`) - never `.env`, `sessions.json`, `active_model.txt`, `*.bak-*`, or `venv/`.
- ⚠️ Claude runs with a configurable permission mode, default **`dontAsk`** (autonomous for normal operations, blocks destructive actions unless allowlisted). Switching to `bypassPermissions` via `/perm` or `PERMISSION_MODE` grants full local execution authority - the startup banner prints the active mode.
- ⚠️ There is **no rate limiting** - an authorized user can trigger unbounded runs.

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
