<div align='center'>
<h1>Bridger</h1>
<h3>Simple multi-way bridge between Telegram, Discord, and WhatsApp written in Python</h3>
<br>
<a href="https://github.com/Rapptz/discord.py">
   <img src="https://img.shields.io/badge/discord.py-2.4.0+-blue?" alt="discord.py"/>
</a>
&nbsp;
<a href="https://github.com/pyrogram/pyrogram">
   <img src="https://img.shields.io/badge/pyrogram-2.0.106+-blue?" alt="pyrogram"/>
</a>
&nbsp;
<a href="https://github.com/krypton-byte/neonize">
   <img src="https://img.shields.io/badge/neonize-0.3.15+-blue?" alt="neonize"/>
</a>
&nbsp;
<a href="https://www.python.org/downloads/">
   <img src="https://img.shields.io/badge/python-3.11+-blue?" alt="Python"/>
</a>
</div>

<br>

> **This is a fork** of [MAymanKH/Bridger](https://github.com/MAymanKH/Bridger).
> See the [Fork changes](#fork-changes) section below for what's been added on top of upstream.

## Overview

A lightweight, fully asynchronous Python application that creates multi-way bridges between Telegram chats, Discord channels, and WhatsApp groups/chats. Messages, media, reactions, and reply threads flow seamlessly across any number of configured bridges.

## Features

- **Multi-way text bridging** — messages sent on any platform in a bridge appear on all others.
- **Reply threading** — replies are matched and natively threaded on the receiving platforms.
- **Reaction synchronization** — bridge emoji reactions across platforms.
- **Rich media support** — photos, videos, audio, voice messages, stickers, and generic documents.
- **Multiple bridges** — configure different combinations of chats and platforms together seamlessly.
- **Flexible Telegram auth** — works as a bot (bot token) or a user account (phone number).
- **Long message chunking** — messages exceeding platform limits are automatically split.
- **Simple to setup** — configure `settings.yaml` and you're ready to go.
- **Docker support** — includes a production-ready `Dockerfile` and `docker-compose.yml`

## Setup

### 0. Prerequisites

- [Python](https://www.python.org/downloads/) 3.11+
- [Git](https://git-scm.com/install/)
- [Docker](https://www.docker.com/get-started/) and Docker Compose (optional)
- Make sure they are added to the PATH environment

### 1. Credentials

- **Discord (Bot):** Create a Bot at [discord.com/developers](https://discord.com/developers/applications) and copy the bot token and application ID. Enable all Privileged Gateway Intents (Message Content, Server Members, Presence).
- **Telegram (Bot or User Account):** Create an Application at [core.telegram.org](https://core.telegram.org/api/obtaining_api_id) and copy the API ID and API Hash. Optionally, create a bot and obtain its token from [@BotFather](https://t.me/BotFather) if you want a bot setup.
- **WhatsApp (User Account):** No prior setup required; you will scan a QR code in the terminal on the first run.

### 2. Clone the repository

```bash
git clone https://github.com/MAymanKH/Bridger.git
cd Bridger
```

### 3. Configure `settings.yaml`

Copy the example config and fill in your credentials:

```bash
cp example.settings.yaml settings.yaml
```

```yaml
# Platform credentials
# Only include the platforms you want to use.
platforms:
  telegram:
    api_id: 123456
    api_hash: your_api_hash_here
  # Choose ONE authentication method (both are optional — omit both for interactive login):
  # bot_token: your_bot_token   # Run as a Telegram bot
  # phone: +12025551234         # Run as a user account

  discord:
    token: your_discord_bot_token
    app_id: 123456789012345678

# Bridges
# Each bridge links two or more platform chats together.
bridges:
  - name: my work bridge
    platforms:
      telegram: -123456        # Telegram chat ID (include the '-')
      discord: 123456789       # Discord channel ID
      whatsapp: 123456789@g.us  # WhatsApp chat JID

  - name: my homies bridge
    platforms:
      telegram: -654321
      whatsapp: 987654321@g.us
```

**Finding chat IDs:**
- **Telegram**: Forward a message to [@userinfobot](https://t.me/userinfobot) or use a Telegram API explorer. Group/channel IDs start with `-100`.
- **Discord**: Enable Developer Mode in settings, right-click a channel → *Copy Channel ID*.
- **WhatsApp**: Learn how to get chat JIDs [here](https://assistro.co/user-guide/zapier/how-to-send-message-to-a-whatsapp-group-guide-to-fetch-group-id/).

### 4a. Run directly with Python

```bash
python -m venv venv

source venv/bin/activate # (On Windows: .\venv\Scripts\activate)

pip install -r requirements.txt

python main.py
```

### 4b. Run with Docker Compose (recommended for servers)

```bash
docker-compose up -d
```

`settings.yaml` is mounted read-only into the container. Message-ID mappings, downloaded media, and the transcoder workspace are stored in a named Docker volume (`message-data`) so they persist across container restarts.

> On the first run, if you configured a phone number (user account mode) for Telegram, you will be prompted for a verification code in the terminal. And for WhatsApp, you will be prompted to scan a QR code in the terminal. After a successful login the session is saved and subsequent starts require no interaction. 

## Notes

- The Discord bot requires the `Message Content` privileged intent to read message text.
- Make sure the Telegram bot has read access to the source chats (add it as an admin).
- This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](https://github.com/MAymanKH/Bridger/blob/main/LICENSE) file for details.

## Fork changes

This fork adds the following on top of upstream `MAymanKH/Bridger`:

### Routing & configuration

- **Directional bridges** — declare `from:` and `to:` per bridge instead of bidirectional `platforms:`. Example: forward a Telegram channel into Discord without replies echoing back.
- **Multi-target lists** — any chat ID in `from:` / `to:` (or legacy `platforms:`) may be a list, fanning out one message into multiple destinations on the same or different platforms.
- **Per-bridge configuration overrides** — `mention_filter`, `always_forward_user_ids`, `destination_size_limit_mb`, and `digest` settings can each be overridden per bridge, falling back to the platform-level default when unset.
- **Hot-reload** — `settings.yaml` changes are picked up live (no container restart) for bridges, mention filters, whitelists, size limits, digest, and janitor settings. Credentials and the platform set still require a restart.
- **Graceful startup errors** — missing / unreadable / malformed `settings.yaml`, write-protected `messages/` directory, and DB-init failures now log a clear actionable message and exit with code `2` instead of dumping a traceback.

### Filtering & forwarding

- **Mention filter** (`mention_filter:` per platform or bridge) — accepts a string or list. Only messages containing one of the substrings are bridged. Includes reply-context retroactive forward (a reply to a mention also pulls the parent), mention-only-text skip (a bare `@bot` reply doesn't relay), and DM bypass.
- **Always-forward whitelist** (`always_forward_user_ids:`) — listed users' messages bypass the mention filter unconditionally.
- **Forward sender attribution** — bridged forwards are attributed to the **original** author (Telegram `forward_from` / `forward_from_chat` / `forward_sender_name`; Discord `message_snapshots[0].author` when the API exposes it).
- **Discord "Forward Message" unpacking** — extracts content + attachments from `message_snapshots`, including embed-fallback and sticker fallback.
- **Discord mention humanization** — raw `<@123>` / `<#456>` / `<:emoji:789>` markup is converted to readable `@displayname` / `#channelname` / `:emoji:` before bridging.

### Digest mode

- **Debounced digest** (`digest.enabled: true`) — buffer text messages until the chat goes quiet for `wait_seconds`, then send one formatted thread. Optional `max_wait_seconds` hard cap for channels that never go silent. Hot-reloadable per bridge.
- **Per-platform safe formatting** — Telegram digest uses native `<b>` + `<blockquote>` (HTML parse mode); Discord uses `**bold**` + `> blockquote` Markdown. Same content rendered natively in each destination.
- **Source-timestamp ordering** — entries sort by the source platform's message timestamp before render, so async handler concurrency can never reorder messages relative to their actual send order.
- **Consecutive same-sender grouping** — a burst of messages from one user shares a single `Name · HH:MM` header, with all content under one blockquote.
- **Media in digest** — `[🖼 photo.jpg]` placeholder lines appear in the thread for every attachment. Optional `buffer_media: true` holds files until the digest fires, so the actual media and the digest text arrive together.

### Media pipeline

- **Per-platform / per-bridge destination size limit** (`destination_size_limit_mb`) — caps what the bridge accepts when delivering into a chat. Defaults: 50 MB Telegram (bot API cap), 10 MB Discord (unboosted).
- **ffmpeg transcoding** — files larger than the destination limit are re-encoded (H.264 / AAC / quality-scaled JPEG) to fit. Falls back to a "too big" notice for non-media. Bounded by a per-encode timeout (5 min default) so a malicious file can't hang the bridge.
- **Pre + post-download size caps** — incoming files are refused without download if the source claims more than 4× the destination limit; the actual disk size is re-verified after download in case the source lied.
- **Configurable transcode location** — output goes to `messages/transcoded/` by default (visible through the same Docker volume that holds `bridge.db`), or any path via `BRIDGER_TRANSCODE_DIR`.

### Operational

- **Janitor** — background sweep deletes orphaned media (`max_age_seconds`, default 1 hour) and prunes old `bridge.db` mapping rows (`db_ttl_seconds`, default 30 days). Both knobs hot-reloadable.
- **Discord login retry with linear backoff** — Discord's `40062` rate-limit lockouts no longer crash the bridge; retries with `Retry-After`-aware, jittered linear backoff, capped at 5 minutes.
- **Timezone setting** (`timezone: Europe/Berlin`) — digest timestamps and any human-readable times use the configured zone. Includes `tzdata` in the Docker image.
- **Reaction forwarding off by default** (`forward_reactions: false`) — was historically on; many users find cross-platform reaction notifications noisy.
- **`forward_reactions` toggle, `--SkipBuild/--SkipPush/--SkipRemote` deploy modes**, and per-machine `deploy.env.ps1` for ergonomic local-to-NAS deployments.
- **Versioning** — startup log line `Bridger v<semver>+<short-sha>` so you know exactly which build is running.

### Security hardening

- **Path traversal sanitization** — user-supplied media filenames (Telegram document, Discord attachment, WhatsApp document) are sanitized through `safe_basename` and validated against the target dir with `commonpath`.
- **Telegram HTML outbound** — switched from Markdown parse mode to HTML + `html.escape` on all user content. Closes the markdown-link injection vector (hostile display name like `[Support](https://phish)`).
- **Log injection sanitizer** — user-controlled fields (sender names, message content, file names) go through `safe_for_log` before any `%s` interpolation. Newlines, ANSI escapes, and control chars become visible `\xNN` escapes instead of breaking log lines.
- **File permission hardening** — `bridge.db` and `my_bot.session` are `chmod 600` after creation (POSIX).
- **`settings.yaml` permissions warning** — startup log warns if the file is group/world-readable (POSIX).
- **ffmpeg timeout** — encode and probe subprocesses are wrapped in `asyncio.wait_for` so a malformed media file can't hang the bridge indefinitely.

### Tests

- **154 unit tests** covering config helpers, filter resolution, digest scheduling/ordering/grouping, janitor sweep + DB pruning, security regressions (path traversal, log injection, HTML escape, file permissions, forward attribution), startup error handling. Run with `python -m unittest discover tests`. No real network or DB required.