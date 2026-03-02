import os
import json
import time
import yaml
import aiosqlite

PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | {".webp", ".mp4", ".mp3", ".ogg", ".pdf", ".apk"}
IGNORED_FILES = {"attachments.json", "text.json", "text.db"}

def load_settings():
    """Load settings from settings.yaml."""
    with open('settings.yaml', 'r') as file:
        return yaml.safe_load(file)

def get_unique_filepath(directory, file_name, file_type):
    """Generate a unique file path, appending a counter if the file already exists.
    file_type should include the leading dot, e.g. '.png'.
    """
    file_path = os.path.join(directory, f"{file_name}{file_type}")
    if not os.path.isfile(file_path):
        return file_path
    file_count = 2
    while True:
        candidate = os.path.join(directory, f"{file_name}_({file_count}){file_type}")
        if not os.path.isfile(candidate):
            return candidate
        file_count += 1

def save_attachment_json(json_file_path, file_path, sender, chat):
    """Save attachment metadata to a JSON file."""
    with open(json_file_path, "r", encoding="utf8") as f:
        data = json.load(f)
    with open(json_file_path, "w", encoding="utf8") as f:
        data["message"] = {
            "path": file_path,
            "sender": sender,
            "chat": chat,
        }
        json.dump(data, f, sort_keys=True, indent=4, ensure_ascii=False)

async def save_text_to_db(db_path, content, sender, chat, replied_to_text=None, replied_to_sender=None):
    """Insert a text message into the SQLite database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            '''INSERT INTO messages
               (content, sender, chat, replied_to_text, replied_to_sender, sent_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (content, sender, chat, replied_to_text, replied_to_sender, int(time.time() * 1000))
        )
        await db.commit()

async def init_db(db_path):
    """Initialize SQLite database with unified schema."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            content TEXT,
                            sender TEXT,
                            chat TEXT,
                            replied_to_text TEXT,
                            replied_to_sender TEXT,
                            sent_at INT
                        )''')
        await db.commit()
