"""One-time interactive login that prints a Pyrogram SESSION_STRING.

Run this locally (never inside a headless container) to authenticate the
Telegram *user* account that will act as the mirror userbot:

    python -m scripts.generate_session

You will be prompted for your phone number, the login code Telegram sends
you, and your two-step-verification password if you have one set. Nothing is
written to disk (`in_memory=True`); the resulting string is only printed to
the terminal. Copy it into SESSION_STRING in your `.env` file and keep it
secret - it grants full access to the account, equivalent to a password.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from pyrogram import Client


async def main() -> None:
    load_dotenv()

    api_id = os.getenv("API_ID") or input("API_ID: ").strip()
    api_hash = os.getenv("API_HASH") or input("API_HASH: ").strip()

    async with Client(
        "session_generator", api_id=int(api_id), api_hash=api_hash, in_memory=True
    ) as client:
        session_string = await client.export_session_string()

    print("\nLogin successful. Add this to your .env file:\n")
    print(f"SESSION_STRING={session_string}\n")
    print("Keep it secret: anyone with this string can act as your account.")


if __name__ == "__main__":
    asyncio.run(main())
