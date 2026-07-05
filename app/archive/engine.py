import asyncio
from app.services.logger import logger

async def start_archive(client):
    logger.info('Archive started')
    # simplified demo loop
    count = 0
    async for msg in client.get_chat_history('me'):
        if msg.media:
            count += 1
        if count > 20:
            break
    logger.info(f'Processed {count} items')
