<div align='center'>
<h1>Telegram Discord Bridge</h1>
<h3>Simple two-way bridge between Telegram and Discord written in Python</h3>
<br>
<a href="https://github.com/Rapptz/discord.py">
   <img src="https://img.shields.io/badge/discord.py-2.4.0+-blue?" alt="discord.py"/>
</a>
&nbsp;
<a href="https://github.com/pyrogram/pyrogram">
   <img src="https://img.shields.io/badge/pyrogram-2.0.106+-blue?" alt="pyrogram"/>
</a>
&nbsp;
<a href="https://www.python.org/downloads/">
   <img src="https://img.shields.io/badge/python-3.11+-blue?" alt="Python"/>
</a>
</div>

<br>

## Overview

A lightweight, fully asynchronous Python application that creates two-way bridges between Telegram chats and Discord channels. Messages, media, reactions, and reply threads flow in both directions across any number of configured channel pairs.

## Features

- **Two-way text bridging** — messages sent in Telegram appear in Discord and vice versa
- **Reply threading** — replies are matched and threaded on the receiving platform
- **Reaction synchronization** — support bridging emoji reactions on messages to the other platform
- **Rich media support** — photos, videos, audio, voice messages, stickers, PDFs, and generic documents
- **Long message chunking** — messages exceeding 1,800 characters are automatically split
- **File size guard** — files larger than 8 MB trigger a friendly warning instead of failing silently
- **Multiple bridges** — configure as many Telegram ↔ Discord pairs as needed in a single `settings.yaml`
- **Flexible Telegram auth** — works as a bot (bot token) or a user account (phone number)
- **Simple to setup** — Just a few minutes to fully setup, configure, and run
- **Docker support** — includes a production-ready `Dockerfile` and `docker-compose.yml`

## Setup

### 0. Prerequisites

- [Python](https://www.python.org/downloads/) 3.11 or higher
- [Git](https://git-scm.com/install/)
- [Docker](https://www.docker.com/get-started/) and Docker Compose (optional)
- Make sure they are added to the PATH environment

### 1. Credentials

- Create a **Discord Bot** at [discord.com/developers](https://discord.com/developers/applications) and copy the **bot token** and **application ID**. Enable all Privileged Gateway Intents (Message Content, Server Members, Presence).
- Create a **Telegram Application** at [core.telegram.org](https://core.telegram.org/api/obtaining_api_id) and copy the **API ID** and **API Hash**.
- Optionally create a Telegram bot via [@BotFather](https://t.me/BotFather) and copy its **bot token** if you prefer running as a bot rather than a user account.

### 2. Clone the repository

```bash
git clone https://github.com/MAymanKH/TelegramDiscordBridge.git
cd TelegramDiscordBridge
```

### 3. Configure `settings.yaml`

Copy the example config and fill in your credentials:

```bash
cp example.settings.yaml settings.yaml
```

```yaml
telegram:
  api_id: 123456
  api_hash: your_api_hash_here
  # Choose ONE authentication method (both are optional — omit both for interactive login):
  # bot_token: your_bot_token   # Run as a Telegram bot
  # phone: +12025551234         # Run as a user account

discord:
  token: your_discord_bot_token
  app_id: 123456789012345678

bridges:
  - name: my-bridge
    telegram_chat_id: -1001234567890   # Include the leading '-' for groups/channels
    discord_chat_id: 987654321098765432

  # Add more bridges as needed:
  # - name: another-bridge
  #   telegram_chat_id: -1009876543210
  #   discord_chat_id: 111222333444555666
```

**Finding chat IDs:**
- **Telegram**: Forward a message from the target chat to [@userinfobot](https://t.me/userinfobot), or use a Telegram API explorer. Group/channel IDs start with `-100`.
- **Discord**: Enable Developer Mode in Discord settings, then right-click a channel → *Copy Channel ID*.

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

`settings.yaml` is mounted read-only into the container. Telegram session data and message queues are stored in named Docker volumes (`telegram-sessions`, `message-data`) so they persist across container restarts.

> On the first run, if you configured a phone number (user account mode), Pyrogram will prompt for a verification code in the terminal. After a successful login the session is saved to `my_bot.session` and subsequent starts require no interaction.

## Notes

- The Discord bot requires the `Message Content` privileged intent to read message text.
- Make sure the Telegram bot has read access to the source chats (add it as an admin).
- This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](https://github.com/MAymanKH/TelegramDiscordBridge/blob/main/LICENSE) file for details.