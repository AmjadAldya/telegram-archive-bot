import asyncio

queue = asyncio.Queue()

async def add_task(task):
    await queue.put(task)

async def get_task():
    return await queue.get()
