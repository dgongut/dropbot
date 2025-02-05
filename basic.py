from config import TELEGRAM_ADMIN
from telethon.errors import FloodWaitError
import asyncio

def is_admin(id):
    return id == int(TELEGRAM_ADMIN)

async def safe_edit_message(message, new_content):
    try:
        await message.edit(new_content)
    except FloodWaitError as e:
        print(f"Telegram ha impuesto un bloqueo de {e.seconds} segundos.")
        await asyncio.sleep(e.seconds)
    except MessageNotModifiedError:
        pass