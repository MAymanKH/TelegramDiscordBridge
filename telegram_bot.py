import json
import os
import asyncio
import aiosqlite

# Ensure an event loop exists for Pyrogram in Python 3.14+
asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, types
import utils

# --- Configuration ---
settings = utils.load_settings()
telegram_info = settings['telegram']
bridges = settings['bridges']

api_id = telegram_info['api_id']
api_hash = telegram_info['api_hash']
bot_token = telegram_info.get('bot_token')
phone_number = telegram_info.get('phone')

if bot_token:
    app = Client("my_bot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)
elif phone_number:
    app = Client("my_bot", api_id=api_id, api_hash=api_hash, phone_number=phone_number)
else:
    # Falls back to interactive session login
    app = Client("my_bot", api_id=api_id, api_hash=api_hash)

SOURCE_CHATS = [target['telegram_chat_id'] for target in bridges]

# Track processed media groups to avoid duplicate downloads (Issue #4 fix)
_processed_media_groups = set()


# --- Helper Functions ---

def get_sender_name(message, fallback="Unknown"):
    """Extract sender display name from a Pyrogram message."""
    try:
        first = message.from_user.first_name or ""
        last = message.from_user.last_name or ""
        name = f"{first} {last}".strip()
        return name if name else (message.from_user.username or fallback)
    except AttributeError:
        return fallback


def get_media_info(media):
    """Extract (file_name, file_extension) from a Pyrogram media object.
    file_extension includes the leading dot, e.g. '.png'.
    """
    if media.document:
        name, ext = os.path.splitext(media.document.file_name)
        return name, ext
    elif media.photo:
        return "image", ".png"
    elif media.video:
        return "video", ".mp4"
    elif media.audio:
        return "audio", ".mp3"
    elif media.voice:
        return "voice", ".mp3"
    elif media.sticker:
        return "sticker", ".webp"
    return "file", ""

# --- Startup ---

async def main():
    print("Telegram bot is alive")
    # Create the DB table before starting background tasks
    await utils.init_db("messages/telegram/text.db")
    # Run background polling tasks concurrently, blocking main() so the client stays alive
    async with app:
        await asyncio.gather(detect_text_change(), detect_new_files())


# --- Incoming Telegram Messages ---

@app.on_message(filters.chat(SOURCE_CHATS))
async def my_handler(client: Client, message: types.Message):
    # Identify which bridge this message belongs to
    for target in bridges:
        if message.chat.id == target['telegram_chat_id']:
            chname = target['name']
            break
    else:
        return

    sender = get_sender_name(message, fallback=chname)
    db_file_path = "messages/telegram/text.db"
    json_file_path = "messages/telegram/attachments.json"

    # --- Media Group (Issue #4 fix: deduplicate) ---
    if message.media_group_id:
        if message.media_group_id in _processed_media_groups:
            return
        _processed_media_groups.add(message.media_group_id)
        if len(_processed_media_groups) > 1000:
            _processed_media_groups.clear()

        media_group = await app.get_media_group(message.chat.id, message.id)
        for i, media in enumerate(media_group):
            await asyncio.sleep(0.5)
            file_name, file_type = get_media_info(media)
            file_path = f"messages/telegram/{file_name}_({i}){file_type}"
            utils.save_attachment_json(json_file_path, file_path, sender, chname)
            await client.download_media(media, file_name=file_path)
        if message.caption:
            await utils.save_text_to_db(db_file_path, message.caption, sender, chname)

    # --- Single Attachment ---
    elif message.media:
        file_name, file_type = get_media_info(message)
        file_path = utils.get_unique_filepath("messages/telegram", file_name, file_type)
        await client.download_media(message, file_path)
        utils.save_attachment_json(json_file_path, file_path, sender, chname)
        if message.caption:
            await utils.save_text_to_db(db_file_path, message.caption, sender, chname)

    # --- Text Message ---
    else:
        replied_to = message.reply_to_message
        if replied_to is None:
            replied_to_text = None
            replied_to_sender = None
        else:
            replied_to_text = replied_to.text
            replied_to_sender = get_sender_name(replied_to, fallback=chname)
        await utils.save_text_to_db(
            db_file_path, message.text, sender, chname,
            replied_to_text, replied_to_sender
        )


# --- Outgoing: Discord → Telegram ---

def check_chat(chat_name):
    """Look up the Telegram chat ID for a bridge by name."""
    for target in bridges:
        if chat_name == target['name']:
            return target['telegram_chat_id']
    return None


async def detect_text_change():
    db_file = "messages/discord/text.db"
    last_id = 0

    while True:
        await asyncio.sleep(0.2)
        try:
            async with aiosqlite.connect(db_file) as db:
                async with db.execute('SELECT MAX(id) FROM messages') as cursor:
                    row = await cursor.fetchone()
                    if row[0] is None or row[0] <= last_id:
                        continue
                    last_id = row[0]
                    async with db.execute(
                        'SELECT content, sender, chat FROM messages WHERE id = ?',
                        (last_id,)
                    ) as cursor:
                        row = await cursor.fetchone()
                        content, sender, chat = row
        except Exception as e:
            continue

        if not content:
            continue

        chat_id = check_chat(chat)
        if chat_id is None:
            continue

        limit = 1800
        if len(content) > limit:
            chunks = [content[i:i + limit] for i in range(0, len(content), limit)]
            for chunk in chunks:
                await app.send_message(chat_id, f"**{sender}:**\n{chunk}")
        else:
            await app.send_message(chat_id, f"**{sender}:**\n{content}")


async def detect_new_files():
    while True:
        await asyncio.sleep(1)
        for file in os.listdir("messages/discord"):
            file_extension = os.path.splitext(file)[1].lower()

            if file_extension == ".temp":
                continue

            if file_extension not in utils.MEDIA_EXTENSIONS:
                if file not in utils.IGNORED_FILES:
                    os.remove(f"messages/discord/{file}")
                continue

            with open('messages/discord/attachments.json', "r") as f:
                data = json.load(f)

            file_path = f"messages/discord/{file}"
            sender = data["message"]["sender"]
            chat = data["message"]["chat"]
            chat_id = check_chat(chat)
            if chat_id is None:
                continue

            try:
                if os.path.getsize(file_path) > 8388608:
                    await app.send_message(
                        chat_id,
                        f"**{sender}:** File size is over 8MB, can't send it."
                    )
                elif file_extension in utils.PHOTO_EXTENSIONS:
                    await app.send_photo(chat_id, file_path, caption=f"**{sender}:**")
                elif file_extension == ".mp4":
                    await app.send_video(chat_id, file_path, caption=f"**{sender}:**")
                elif file_extension == ".mp3":
                    await app.send_audio(chat_id, file_path, caption=f"**{sender}:**")
                elif file_extension == ".ogg":
                    await app.send_voice(chat_id, file_path, caption=f"**{sender}:**")
                elif file_extension == ".webp":
                    # Issue #8 fix: send webp as document instead of silently dropping
                    await app.send_document(chat_id, file_path, caption=f"**{sender}:**")
                elif file_extension in (".pdf", ".apk"):
                    await app.send_document(chat_id, file_path, caption=f"**{sender}:**")
            finally:
                if os.path.isfile(file_path):
                    os.remove(file_path)


app.run(main())
