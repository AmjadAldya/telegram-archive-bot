import asyncio


def main() -> None:
    # Pyrogram's Client binds its dispatcher to whatever loop
    # asyncio.get_event_loop() returns at construction time (module import,
    # via app.bot.client). asyncio.run() always creates and later closes a
    # *separate* loop, so a Client built before it runs never gets its
    # handlers or update workers scheduled on the loop that's actually
    # executing - they register but silently never run. Creating and setting
    # the loop first, then importing, keeps both on the same loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from app.bot.client import run_bot

    try:
        loop.run_until_complete(run_bot())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
