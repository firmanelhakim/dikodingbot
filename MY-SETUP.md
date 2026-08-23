# My dikodingbot setup

This is the concrete record of how my instance of dikodingbot is configured,
end to end: Telegram registration, Claude Code, the model router, and the bot
itself. The [README](README.md) describes what the bot does and the generic
install steps. This file is my environment specifically, so paths and versions
reflect my machine.

Real secrets are never shown here. The bot token, API key, and Telegram user
ID appear as `<YOUR_...>` placeholders. Everything else is the actual setup.

## How the pieces connect

```
Telegram (phone app)
    |  HTTPS
Telegram Bot API
    |  long-polling (python-telegram-bot)
dikodingbot (systemd service, /home/firman/workspace/dikodingbot)
    |  spawns a subprocess
Claude Code CLI (2.1.220, /home/firman/.local/bin/claude)
    |  ANTHROPIC_BASE_URL
9router (systemd service, http://127.0.0.1:20128)
    |  upstream
model provider (whatever backends 9router is configured to route to)
```

Four moving parts, in order of what they do:

- **Telegram** is the front end. BotFather issues the token; one numeric user
  ID is the only account allowed to talk to the bot.
- **dikodingbot** is this repo. It polls Telegram, authorizes the operator,
  spawns the Claude Code CLI, streams progress back, and delivers the answer.
- **Claude Code** is Anthropic's CLI. The bot runs it headless with `claude -p`.
- **9router** is an OpenAI-compatible gateway that Claude Code talks to instead
  of Anthropic directly, so the model choice is a router setting rather than a
  Claude Code login.

## Current environment

| Piece | Value |
|---|---|
| OS | Linux amd64, kernel 6.1.0-9-amd64 |
| Python | 3.11.2, project venv at `dikodingbot/venv` |
| Node | v24.19.0 |
| Claude Code | 2.1.220, installed at `~/.local/bin/claude` |
| 9router | 0.5.50, runs at `http://127.0.0.1:20128` |
| `BASE_DIR` | `/home/firman/workspace` |
| Current model | `ds/deepseek-v4-pro-max` (persisted via `/model`) |
| Current permission mode | `bypassPermissions` (persisted via `/perm`) |

The last two live in gitignored runtime files (`active_model.txt` and
`active_permission.txt`), not in `.env`, so they change as I use `/model` and
`/perm` and are not part of the repo. Per-folder overrides set from inside a
topic live in `models.json` and `permissions.json`, also gitignored.

## Workspace layout

`BASE_DIR` is `/home/firman/workspace`. The bot's `/projects` command lists its
subdirectories, and `/switch` moves between them. The two that matter for this
setup:

```
9router/          the router gateway (its own small npm project)
dikodingbot/      this repo; also a switchable workspace
```

Because `dikodingbot/` is itself a workspace under `BASE_DIR`, I can `/switch
dikodingbot` and have Claude Code work on its own source.

## Forum topics (one project per topic)

The bot can also work inside a forum supergroup, where each topic maps to one
workspace folder. This is the multi-project mode: topics run Claude in parallel
in different folders, each with its own session.

Setup:

1. Create a group and enable Topics in its settings (it becomes a forum
   supergroup). Admin rights for the bot are optional.
2. Add the bot to the group. `/setjoingroups` must be Enable and
   `/setprivacy` must be Disable (see the BotFather section).
3. Create a topic for each project (`#project-one`, `#project-two`, ...).
4. In each topic, send `/bind <folder>` once. This creates
   `BASE_DIR/<folder>` if needed and remembers that topic maps to it. A folder
   can be bound to only one topic; `/bind` on a taken folder is refused, so
   `/unbind` there first.

After `/bind`, any plain text sent in that topic runs Claude Code in the bound
folder, in parallel with other topics. `/list`, `/code`, `/status`, `/cancel`,
and `/reset` all operate on the current topic's folder. `/unbind` detaches a
topic from its folder. `/perm` and `/model` inside a topic set that folder's
mode or model only (persisted in `permissions.json` and `models.json`); their
private-chat forms set the global defaults.

The General topic and private chats have no `message_thread_id`, so they fall
back to the single active folder selected with `/switch`. The binding map lives
in `topics.json` next to `bot.py`, gitignored like `sessions.json`.

## Claude Code setup

Claude Code is installed per-user, not system-wide. On this machine it lives at:

- `~/.local/bin/claude`, a symlink to
- `~/.local/share/claude/versions/2.1.220` (the current version)

It is installed with Claude Code's native installer, not npm. The giveaway is
the layout: the binary sits under `~/.local` and versioned releases under
`~/.local/share/claude/versions/`, which is what the installer script produces
(the npm global prefix here is `/usr`, and `npm ls -g` does not list Claude
Code).

To install or update:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

Updates are handled by the same installer; `claude update` upgrades in place
without touching the bot, which just calls the `claude` on `PATH`.

The bot does not need Claude Code to be logged in to Anthropic. It points the
CLI at 9router through the `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY`
environment variables, so model access is configured at the router, not in
`~/.claude`.

Claude Code keeps its own per-project session transcripts under `~/.claude/`.
The bot reads that directory to decide whether a saved session UUID can still
be resumed, which is what makes `/reset` and `/switch` behave correctly across
restarts.

## 9router setup

9router is an npm CLI that exposes an OpenAI-compatible `/v1/models` endpoint
locally and a web dashboard. It is the router Claude Code points at.

### Installing 9router

It requires Node.js (the package declares `"engines": {"node": ">=18.0.0"}`).
This machine has v24.19.0. It is installed as a local dependency inside its
own workspace, with no `-g` flag, so the version is pinned in `package.json`
and the binary runs from `node_modules/.bin`:

```bash
mkdir -p /home/firman/workspace/9router
cd /home/firman/workspace/9router
npm init -y
npm install 9router
# now the binary is at node_modules/.bin/9router
```

The CLI flags in the service unit below are the important part.

### Running 9router

On first run it creates its state directory at `~/.9router/` (database, auth
secrets, logs, and a runtime dir for a native `sql.js` binary), then opens a
dashboard. The default port is `20128`:

- Dashboard: `http://localhost:20128/dashboard`
- Models endpoint the bot and Claude Code use: `http://localhost:20128/v1/models`

The CLI flags this setup passes:

```bash
9router --no-browser --skip-update --host 127.0.0.1
```

- `--no-browser` - don't try to open a browser on a headless server.
- `--skip-update` - don't phone home to check for a newer version.
- `--host 127.0.0.1` - bind to localhost only, so the dashboard and API are
  not reachable from the network.

### Provider setup and the API key

Connect a provider through the dashboard (Dashboard → Providers), which is
where the model backends and their keys live. Once connected, the dashboard
shows the API key Claude Code should use; copy that into `ANTHROPIC_API_KEY`
in `.env`.

### Running 9router as a service

The systemd unit:

```ini
[Service]
User=firman
WorkingDirectory=/home/firman/workspace/9router
ExecStart=/home/firman/workspace/9router/node_modules/.bin/9router \
    --no-browser --skip-update --host 127.0.0.1
Restart=always
RestartSec=10
```

It listens on `127.0.0.1:20128`, which means it is not reachable from outside
the machine. Claude Code and the bot talk to it over that local address; the
router handles the actual model backends and their keys, so neither the bot nor
Claude Code needs those keys directly.

The bot's `/model` command queries `http://127.0.0.1:20128/v1/models` to list
what the router offers, then writes the chosen ID to `active_model.txt` (the
global default; a per-folder choice from inside a topic goes to `models.json`).

## Telegram setup (BotFather)

Open a chat with [@BotFather](https://t.me/BotFather) and follow these steps.

### 1. Create the bot

Send `/newbot`. BotFather asks for two names:

- **Display name**, which can be anything and may contain spaces. I use
  something human-readable.
- **Username**, which must be unique, lowercase, and end in `bot`. This is the
  bot's handle.

On success BotFather returns the token, shaped like
`123456789:AA...rest-of-token`. Copy it. This is `BOT_TOKEN` in `.env`. Treat
it like a password: it lets whoever holds it control the bot.

### 2. Let the bot join groups

This bot is remote code execution, so keep it out of groups run by strangers.
If you use it only in private chats, you can disable `/setjoingroups`. If you
use forum topics, the bot must be addable to a group:

- Send `/setjoingroups`, pick the bot, choose **Enable** (needed for topics),
  or **Disable** if the bot stays in private chats.

### 3. Set the command menu

Send `/setcommands`, pick the bot, and paste this list verbatim:

```
projects - List folders in BASE_DIR; active one is marked
switch - Switch active workspace: /switch <folder>
bind - Bind this topic to a folder: /bind <folder>
unbind - Unbind this topic from its folder
reset - Clear conversation memory for a fresh start
model - Show or switch active Claude model: /model [name]
perm - Show or switch permission mode: /perm [dontAsk|bypassPermissions|plan]
status - Show running task PID, elapsed time, and prompt
cancel - Terminate the running Claude process
list - List files in the active workspace
code - Send active workspace as ZIP, or /code <file> for one file
help - Show the command menu
```

This makes Telegram show a `/` button with these entries, matching the handlers
the bot registers. `/start` is not in the list because the bot maps it to the
same help screen. `/bind` and `/unbind` only do anything inside a forum topic,
but listing them here keeps the menu consistent.

### 4. Turn off privacy mode (required for group prompts)

By default a bot in a group has privacy mode on and only sees commands,
mentions, and replies to its own messages. The whole point of this setup is to
send plain-text prompts in a topic, so privacy mode must be off:

- Send `/setprivacy`, pick the bot, choose **Disable**.

This is what lets the bot receive every message in a topic, not just commands.

### 5. Other BotFather settings (optional)

- `/setdescription` and `/setabouttext` - the bot profile shown in chat. Useful
  but cosmetic.
- `/setuserpic` - the bot's avatar. Also cosmetic.

If the token ever leaks, revoke and reissue it with `/revoke` in BotFather, then
update `.env` and restart the bot.

### 6. Get your Telegram user ID

The bot authorizes exactly one account. To find its numeric ID, message
[@userinfobot](https://t.me/userinfobot) from the account you want to operate
the bot. It replies with your numeric ID. Put that number in `ALLOWED_USER_ID`.

## Wiring it all together, from scratch

The order that makes each later step testable:

1. **Install Node (for 9router) and Claude Code.**

   ```bash
   # Node for the router (see step 2).
   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
   sudo apt-get install -y nodejs
   node --version

   # Claude Code, per-user, under ~/.local.
   curl -fsSL https://claude.ai/install.sh | bash
   claude --version
   ```

2. **Install and start 9router.** Follow the [9router setup](#9router-setup)
   section: install the local dependency, connect a provider through the
   dashboard at `http://localhost:20128/dashboard`, and copy the API key it
   shows. Confirm the endpoint:

   ```bash
   curl http://127.0.0.1:20128/v1/models
   ```

3. **Register the bot in BotFather** and record the token, then get your user
   ID from @userinfobot. (Steps in the section above.)

4. **Clone and set up the bot.**

   ```bash
   cd /home/firman/workspace
   git clone git@github.com:firmanelhakim/dikodingbot.git
   cd dikodingbot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

5. **Fill in `.env`.** The keys and what goes in each:

   ```ini
   BOT_TOKEN=<YOUR_BOT_TOKEN>
   ALLOWED_USER_ID=<YOUR_TELEGRAM_USER_ID>
   BASE_DIR=/home/firman/workspace
   CLAUDE_BIN=/home/firman/.local/bin/claude
   CLAUDE_TIMEOUT=0
   ANTHROPIC_BASE_URL=http://127.0.0.1:20128
   ANTHROPIC_API_KEY=<YOUR_ROUTER_API_KEY>
   ANTHROPIC_MODEL=dikodingbot
   ```

   `ANTHROPIC_MODEL` is only the starting default. The `/model` command writes
   `active_model.txt`, and that file takes precedence over the env var on the
   next run. On this machine the current effective model is
   `ds/deepseek-v4-pro-max`, set via `/model`.

   `.env` is gitignored (`*.env*` and `*.bak-*`), so it never reaches GitHub.
   See `.env.example` for the other optional variables (`CLAUDE_STREAM_LIMIT`,
   the rate-limit knobs, `LOG_LEVEL`, and so on).

6. **Install the two systemd units** (9router and dikodingbot). Both units are
   shown in full in their sections above. Enable and start both:

   ```bash
   sudo systemctl enable --now 9router.service
   sudo systemctl enable --now dikodingbot.service
   sudo systemctl status dikodingbot.service
   ```

7. **Smoke test.** In Telegram, send `/start` to the bot. It should reply with
   the command menu. Then `/projects` to confirm it sees your workspaces, and a
   plain message to run a first Claude Code task. `/status` and `/cancel` should
   work while it runs.

## The service user

The bot runs as `firman`, the normal login user, not a dedicated system
account:

```
uid=1000(firman) gid=1000(firman) groups=1000(firman),100(users),109(docker)
```

A regular user with a home directory is the right choice here, not
`adduser --system`. Two reasons:

- Claude Code reads `~/.claude` for its session transcripts, credentials, and
  settings. The `HOME` environment variable the unit sets must point at a real
  home directory with those files, or `/switch` and `/reset` lose track of
  sessions.
- The bot runs Claude Code with whatever permission mode is active. Running it
  as your own user keeps file ownership simple: every file Claude Code writes
  into a workspace is owned by you, and nothing needs group or root juggling.

If you prefer a dedicated account, it still needs a home directory and write
access to `BASE_DIR`:

```bash
sudo useradd -m -s /bin/bash dikodingbot
```

then change `User=` in both units and make sure `~/.claude` and the workspace
tree are readable by that user. This machine keeps it simple and runs as
`firman`.

## Passwordless sudo

`firman` is not in the `sudo` group. Passwordless sudo is granted by a direct
line in the main sudoers file:

```
firman ALL=(ALL:ALL) NOPASSWD: ALL
```

To add it, edit sudoers with `visudo` (never a plain editor, because a syntax
error in this file locks you out of sudo):

```bash
sudo visudo
```

Add the line, save, and confirm with:

```bash
sudo -n true && echo ok
```

`sudo -n true` succeeding with no password prompt is the check.

This grant is deliberately broad because the setup touches systemd units,
Node, and the router under one account. If you want the minimum instead, scope
it to the two commands this setup actually needs elevated:

```
firman ALL=(root) NOPASSWD: /usr/bin/systemctl restart dikodingbot.service, /usr/bin/systemctl enable --now dikodingbot.service, /usr/bin/systemctl enable --now 9router.service
```

Whichever form you use, keep the `NOPASSWD` lines in one obvious place and
review it when you change machines.

## Installing the bot as a systemd service

The unit lives at `/etc/systemd/system/dikodingbot.service`:

```ini
[Unit]
Description=Dikodingbot Telegram Bridge to Claude Code
After=network.target

[Service]
Type=simple
User=firman
WorkingDirectory=/home/firman/workspace/dikodingbot
ExecStart=/home/firman/workspace/dikodingbot/venv/bin/python /home/firman/workspace/dikodingbot/bot.py
Restart=always
RestartSec=10
Environment="HOME=/home/firman"

[Install]
WantedBy=multi-user.target
```

Notes on each choice:

- `Type=simple` - the bot is a long-running foreground process with no daemon
  mode, which is what `simple` expects.
- `User=firman` and `WorkingDirectory=.../dikodingbot` - run as the same user
  whose `~/.claude` and workspace the bot touches.
- `Environment="HOME=/home/firman"` - systemd does not inherit a login
  shell's environment, so `HOME` is set explicitly. Without it, Claude Code
  cannot find its config and session directories.
- `Restart=always` with `RestartSec=10` - the bot comes back after a crash or
  after `systemctl restart`, with a short pause so a crash loop does not spin
  the CPU.

Write the file, then install and start it:

```bash
sudo cp dikodingbot.service /etc/systemd/system/dikodingbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now dikodingbot.service
sudo systemctl status dikodingbot.service
```

`enable --now` does three things at once: `enable` creates the
`multi-user.target.wants/dikodingbot.service` symlink so the service starts on
boot, and `--now` starts it immediately. `daemon-reload` is required after
creating or editing a unit file.

After any change to the unit file, run `daemon-reload` again before restarting:

```bash
sudo systemctl daemon-reload
sudo systemctl restart dikodingbot.service
```

The 9router service is installed the same way, with its own unit (shown in the
9router section above).

## Operating notes

- **Model and permission mode are runtime state.** `/model` and `/perm` persist
  to gitignored files, so they survive restarts but are not in the repo. In a
  private chat they write the global `active_model.txt` / `active_permission.txt`;
  inside a topic they write per-folder `models.json` / `permissions.json`
  overrides. Check the current values with `/model` (no argument) and `/perm`
  (no argument).
- **One run at a time, per folder.** The bot serializes Claude Code runs behind
  a lock keyed by workspace directory. Two topics bound to different folders
  run in parallel; a second message in the same folder while its task runs gets
  a "task already in progress" reply rather than queuing.
- **Restarts.** All `.env` constants are read at import time, so changing
  `.env` requires `sudo systemctl restart dikodingbot.service`. `/model` and
  `/perm` do not.
- **Updates.** Pull new code, then restart:

  ```bash
  cd /home/firman/workspace/dikodingbot
  git pull
  sudo systemctl restart dikodingbot.service
  ```

## Troubleshooting: startup crash on a flaky network

Right after `systemctl start`, the boot logs can show a traceback ending in
`anyio.NoEventLoopError: Not currently running on any asynchronous event loop`
and `ExtBot is not properly initialized`. This is not a bug in the bot.

The sequence: PTB's polling bootstrap calls `delete_webhook`. When the Telegram
API is returning transient errors (a `502 Bad Gateway` was observed on the
production instance at the same moment), the `httpx` request path tears down
half-finished connections, and httpcore's cancellation-shield cleanup calls
`anyio` code that assumes a running event loop. The failure surfaces as a
misleading `NoEventLoopError`, then the retry loop logs `Failed run number 3 of
0. Aborting` and the process exits.

systemd restarts the service (`Restart=always`, `RestartSec=10`) and, once the
network settles, the next bootstrap succeeds with `Application started`. It is
self-healing and only ever appears when the network is failing at startup.

If you see it, check the cause rather than the symptom: confirm Telegram is
reachable (`getWebhookInfo` should return quickly), then `systemctl restart
dikodingbot.service` once the network is back. There is no code fix to apply;
the only durable mitigations are to keep `Restart=always` and, if the flakiness
is regular, raise `RestartSec` so a crash loop during an outage does not spin
the CPU.

## Things worth adding later

This document records the setup but not every operational topic. Candidates
for a follow-up section, in rough order of usefulness:

- **Router model catalog** - which models 9router currently exposes and how to
  add a new one, since that is where model choice actually lives.
- **Backup and restore** - `sessions.json`, `topics.json`, `models.json`,
  `permissions.json`, `active_model.txt`, and `active_permission.txt` are the
  runtime state; a note on backing them up and restoring after a fresh clone.
- **Troubleshooting** - the two failure modes already fixed (an oversized
  `stream-json` line breaking the run, and the live preview starving the final
  message under Telegram's rate limit) and the log lines to look for. The
  startup `NoEventLoopError` is now covered in its own section above.
- **Security checklist** - confirm 9router stays bound to `127.0.0.1`, rotate
  the bot token on leak, and review the group posture: the bot now joins groups
  and runs with privacy mode off, so keep it out of groups run by strangers
  even though the single-operator gate still rejects everyone else.
