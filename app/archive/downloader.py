from __future__ import annotations

from pathlib import Path


async def download_media(client, message, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded_file = await client.download_media(message, file_name=str(target_dir))
    if not downloaded_file:
        raise RuntimeError(f"Failed to download media for message {message.id}")

    return Path(downloaded_file)
