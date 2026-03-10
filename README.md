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

## Overview

A lightweight, fully asynchronous Python application that creates multi-way bridges between Telegram chats, Discord channels, and WhatsApp groups/chats. Messages, media, reactions, and reply threads flow seamlessly across any number of configured bridges.

## Features

- **Multi-way text bridging** — messages sent on any platform in a bridge appear on all others.
- **Reply threading** — replies are matched and natively threaded on the receiving platforms.
- **Reaction synchronization** — seamlessly bridge emoji reactions across platforms.
- **Rich media support** — photos, videos, audio, voice messages, stickers, and generic documents.
- **Multiple bridges** — group different combinations of chats and platforms together in a single `settings.yaml`.
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

- **Discord**: Create a Bot at [discord.com/developers](https://discord.com/developers/applications) and copy the **bot token** and **application ID**. Enable all Privileged Gateway Intents (Message Content, Server Members, Presence).
- **Telegram**: Create an Application at [core.telegram.org](https://core.telegram.org/api/obtaining_api_id) and copy the **API ID** and **API Hash**. Optionally, use a bot token from [@BotFather](https://t.me/BotFather).
- **WhatsApp**: No prior setup required; you will scan a QR code in the terminal on the first run.

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

`settings.yaml` is mounted read-only into the container. Telegram session data and message queues are stored in named Docker volumes (`telegram-sessions`, `message-data`) so they persist across container restarts.

> On the first run, if you configured a phone number (user account mode) for Telegram, you will be prompted for a verification code in the terminal. And for WhatsApp, you will be prompted to scan a QR code in the terminal. After a successful login the session is saved and subsequent starts require no interaction. 

## Notes

- The Discord bot requires the `Message Content` privileged intent to read message text.
- Make sure the Telegram bot has read access to the source chats (add it as an admin).
- This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](https://github.com/MAymanKH/TelegramDiscordBridge/blob/main/LICENSE) file for details.