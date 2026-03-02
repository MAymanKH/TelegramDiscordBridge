import time
import json
import os
import asyncio

import yaml
import aiosqlite
import discord
from discord.ext import commands
import utils


# --- Configuration ---
settings = utils.load_settings()
bridges = settings['bridges']


# --- Helper Functions ---

def check_chat(chat_name):
    """Look up the Discord channel for a bridge by name."""
    for target in bridges:
        if chat_name == target['name']:
            return bot.get_channel(target['discord_chat_id'])
    return None


# --- Polling: Telegram → Discord ---

async def detect_text_change():
    db_file = "messages/telegram/text.db"
    latest_timestamp = 0

    while True:
        await asyncio.sleep(0.2)
        try:
            async with aiosqlite.connect(db_file) as db:
                async with db.execute('SELECT MAX(sent_at) FROM messages') as cursor:
                    row = await cursor.fetchone()
                    if row[0] is None or row[0] <= latest_timestamp:
                        continue
                    latest_timestamp = row[0]
                    async with db.execute(
                        'SELECT content, sender, chat, replied_to_text, replied_to_sender '
                        'FROM messages WHERE sent_at = ?',
                        (latest_timestamp,)
                    ) as cursor:
                        row = await cursor.fetchone()
                        content, sender, chat, replied_to_text, replied_to_sender = row
        except Exception as e:
            print(f"Error in detect_text_change: {e}")
            continue

        channel = check_chat(chat)
        if channel is None:
            continue

        limit = 1800

        async def reply_or_send(message_content):
            if replied_to_text is None:
                await channel.send(message_content)
            else:
                async for msg in channel.history(limit=100):
                    if (msg.content == replied_to_text
                            or msg.content == f"*{replied_to_sender}:*\n{replied_to_text}"):
                        await msg.reply(message_content)
                        break
                else:
                    await channel.send(message_content)

        if len(content) > limit:
            chunks = [content[i:i + limit] for i in range(0, len(content), limit)]
            for chunk in chunks:
                if sender == chat:
                    await reply_or_send(chunk)
                else:
                    await reply_or_send(f"*{sender}:*\n{chunk}")
        else:
            if sender == chat:
                await reply_or_send(content)
            else:
                await reply_or_send(f"*{sender}:*\n{content}")


async def detect_new_files():
    while True:
        await asyncio.sleep(0.5)
        for file in os.listdir("messages/telegram"):
            file_extension = os.path.splitext(file)[1].lower()

            if file_extension == ".temp":
                continue

            if file_extension not in utils.MEDIA_EXTENSIONS:
                if file not in utils.IGNORED_FILES:
                    os.remove(f"messages/telegram/{file}")
                continue

            with open('messages/telegram/attachments.json', "r") as f:
                data = json.load(f)

            file_path = f"messages/telegram/{file}"
            sender = data["message"]["sender"]
            chat = data["message"]["chat"]
            channel = check_chat(chat)
            if channel is None:
                continue

            try:
                if os.path.getsize(file_path) > 8388608:
                    if sender == chat:
                        await channel.send(
                            "File size is over 8MB, so I can't send it. Sorry :("
                        )
                    else:
                        await channel.send(
                            f"*{sender}:* File size is over 8MB, so I can't send it. Sorry :("
                        )
                else:
                    if sender == chat:
                        await channel.send(file=discord.File(file_path))
                    else:
                        await channel.send(
                            file=discord.File(file_path),
                            content=f"*{sender}:* [{file}]"
                        )
            finally:
                if os.path.isfile(file_path):
                    os.remove(file_path)


# --- Bot Setup ---

class MyBot(commands.Bot):
    def __init__(self):
        app_id = settings['discord']['app_id']
        super().__init__(
            command_prefix=".",
            intents=discord.Intents.all(),
            application_id=app_id
        )

    async def close(self):
        await super().close()

    async def on_ready(self):
        print("Discord bot is ready")
        # Create the DB table before starting background tasks
        await utils.init_db("messages/discord/text.db")
        # Fire-and-forget background polling tasks
        asyncio.create_task(detect_text_change())
        asyncio.create_task(detect_new_files())


bot = MyBot()


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Find the bridge for this channel
    for target in bridges:
        if message.channel.id == target['discord_chat_id']:
            chname = target['name']
            break
    else:
        return

    sender = message.author.display_name

    # Download attachments if present
    if message.attachments:
        original_name = message.attachments[0].filename
        file_name, file_type = os.path.splitext(original_name)
        if file_type == ".webp":
            file_type = ".png"
        file_path = utils.get_unique_filepath("messages/discord", file_name, file_type)

        # Save attachment info in attachments.json
        json_file_path = "messages/discord/attachments.json"
        utils.save_attachment_json(json_file_path, file_path, sender, chname)
        await message.attachments[0].save(fp=file_path)

    # Save text messages to DB (skip empty content from attachment-only messages)
    if message.content:
        db_file_path = "messages/discord/text.db"
        replied_to_text = None
        replied_to_sender = None
        
        if message.reference and getattr(message.reference, 'resolved', None) and isinstance(message.reference.resolved, discord.Message):
            replied_to_text = message.reference.resolved.content
            replied_to_sender = message.reference.resolved.author.display_name

        await utils.save_text_to_db(db_file_path, message.content, sender, chname, replied_to_text, replied_to_sender)

token = settings['discord']['token']
bot.run(token)
