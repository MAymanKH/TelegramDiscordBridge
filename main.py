import asyncio


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
    asyncio.run(main())
