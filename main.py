import asyncio
import json
import os


def ensure_directories():
    """Create required message directories and seed files if they don't exist."""
    for folder in ("messages/telegram", "messages/discord"):
        os.makedirs(folder, exist_ok=True)
    for json_path in ("messages/telegram/attachments.json", "messages/discord/attachments.json"):
        if not os.path.isfile(json_path):
            with open(json_path, "w", encoding="utf8") as f:
                json.dump({}, f)


async def run_file(file_name):
    print(f"Starting {file_name}...")
    process = await asyncio.create_subprocess_shell(f"python {file_name}", cwd=".")
    await process.wait()


async def main():
    await asyncio.gather(
        run_file("telegram_bot.py"),
        run_file("discord_bot.py"),
    )


if __name__ == "__main__":
    ensure_directories()
    asyncio.run(main())
