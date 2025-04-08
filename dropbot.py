import os
from telethon import TelegramClient, events, functions, types, Button
from telethon.tl.types import (
    BotCommand, Document, Photo,
    DocumentAttributeFilename, DocumentAttributeVideo, DocumentAttributeAudio
)
from config import *
from translations import *
from debug import *
from basic import *
import sys
import re
import asyncio

VERSION = "1.1.0"

if LANGUAGE.lower() not in ("es", "en"):
    error("LANGUAGE only can be ES/EN")
    sys.exit(1)

load_locale(LANGUAGE.lower())

if DEFAULT_EMPTY_STR == TELEGRAM_TOKEN:
    error(get_text("error_bot_token"))
    sys.exit(1)

if DEFAULT_EMPTY_STR == TELEGRAM_ADMIN:
    error(get_text("error_bot_telegram_admin"))
    sys.exit(1)

if ANONYMOUS_USER_ID == TELEGRAM_ADMIN:
    error(get_text("error_bot_telegram_admin_anonymous"))
    sys.exit(1)

if PARALLEL_DOWNLOADS < 1:
    error(get_text("error_parallel_downloads"))
    sys.exit(1)

DOWNLOAD_PATHS = {
    "audio": DOWNLOAD_AUDIO if FILTER_AUDIO else DOWNLOAD_PATH,
    "video": DOWNLOAD_VIDEO if FILTER_VIDEO else DOWNLOAD_PATH,
    "photo": DOWNLOAD_PHOTO if FILTER_PHOTO else DOWNLOAD_PATH,
    "torrent": DOWNLOAD_TORRENT if FILTER_TORRENT else DOWNLOAD_PATH,
    "ebook": DOWNLOAD_EBOOK if FILTER_EBOOK else DOWNLOAD_PATH
}

for path in DOWNLOAD_PATHS.values():
    os.makedirs(path, exist_ok=True)

bot = TelegramClient("dropbot", TELEGRAM_API_ID, TELEGRAM_API_HASH).start(bot_token=TELEGRAM_TOKEN)
active_tasks = {}
download_semaphore = asyncio.Semaphore(PARALLEL_DOWNLOADS)

def get_download_path(event):
    message = event.message
    file_name = message.file.name if message.file else None
    file_extension = os.path.splitext(file_name)[1].lower() if file_name else ""
    
    if file_extension in EXTENSIONS_TORRENT:
        return DOWNLOAD_PATHS["torrent"], TOR_ICO
    elif file_extension in EXTENSIONS_EBOOK:
        return DOWNLOAD_PATHS["ebook"], BOO_ICO
    elif file_extension in EXTENSIONS_VIDEO or message.video:
        return DOWNLOAD_PATHS["video"], VID_ICO
    elif file_extension in EXTENSIONS_AUDIO or message.audio:
        return DOWNLOAD_PATHS["audio"], AUD_ICO
    elif file_extension in EXTENSIONS_IMAGE or message.photo:
        return DOWNLOAD_PATHS["photo"], IMG_ICO
    return DOWNLOAD_PATH, DEF_ICO


@bot.on(events.NewMessage(func=lambda e: e.document or e.video or e.audio or e.photo))
async def handle_files(event):
    if not is_admin(event.sender_id):
        debug(get_text("warning_not_admin", event.sender_id))
        return
    
    task = asyncio.create_task(limited_download(event))
    active_tasks[event.id] = task

async def limited_download(event):
    async with download_semaphore:
        await download_media(event)

async def download_media(event):
    message = event.message
    media = message.document or message.video or message.audio or message.photo
    if not media:
        return
    file_name = get_file_name(media)
    debug(get_text("debug_file_received", file_name))
    download_path, ico = get_download_path(event)
    file_path = os.path.join(download_path, file_name)

    status_message = await event.reply(
        get_text("downloading", ico),
        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")]
    )

    try:
        await bot.download_media(message, file=file_path)
        await status_message.delete()
        await event.reply(get_text("downloaded", ico, file_path))
        debug(get_text("debug_file_downloaded", file_name))
    except asyncio.CancelledError:
        await status_message.edit(get_text("cancelled"), buttons=None)
        if os.path.exists(file_path):
            os.remove(file_path)
        debug(get_text("debug_file_cancelled", file_name))
    finally:
        active_tasks.pop(event.id, None)

def get_file_name(media):
    if isinstance(media, Document):
        file_name = next(
            (attr.file_name for attr in media.attributes if isinstance(attr, DocumentAttributeFilename)), 
            None
        )
        file_name = sanitize_filename(file_name) if file_name else None
        if not file_name:
            if any(isinstance(attr, DocumentAttributeVideo) for attr in media.attributes):
                return f"video_{media.id}.mp4"
            if any(isinstance(attr, DocumentAttributeAudio) for attr in media.attributes):
                return f"audio_{media.id}.mp3"
            return f"file_{media.id}"
        return file_name
    elif isinstance(media, Photo):
        return f"photo_{media.id}.jpg"
    else:
        return f"file_{media.id}"

@bot.on(events.NewMessage(pattern=r"/(start|donate|version)"))
async def handle_start(event):
    if not is_admin(event.sender_id):
        debug(get_text("warning_not_admin", event.sender_id))
        response = get_text("user_not_admin")
    elif event.raw_text == "/start":
        response = get_text("welcome_message")
    elif event.raw_text == "/donate":
        response = get_text("donate")
    elif event.raw_text == "/version":
        response = get_text("version", VERSION)
    await event.reply(response)

@bot.on(events.CallbackQuery(data=lambda data: data.startswith(b"cancel:")))
async def cancel_download(event):
    msg_id = int(event.data.decode().split(":")[1])
    task = active_tasks.get(msg_id)
    if task and not task.done():
        task.cancel()
        await event.answer(get_text("cancelling"))
    else:
        await event.answer(get_text("already_cancelled"))

@bot.on(events.NewMessage)
async def handle_message(event):
    if not is_admin(event.sender_id):
        debug(get_text("warning_not_admin", event.sender_id))
        return
    if re.search(URL_PATTERN, event.raw_text):
        await event.reply(get_text("error_url"))
        return
    
async def send_startup_message():
    await bot.send_message(int(TELEGRAM_ADMIN), get_text("initial_message", VERSION))

async def set_commands():
    commands = [
        BotCommand("start", get_text("menu_start")),
        BotCommand("version", get_text("menu_version")),
        BotCommand("donate", get_text("menu_donate")),
    ]
    await bot(functions.bots.SetBotCommandsRequest(
        scope=types.BotCommandScopeDefault(),
        lang_code=LANGUAGE.lower(),
        commands=commands
    ))

async def main():
    debug(f"DropBot v{VERSION}")
    await bot.start()
    await set_commands()
    await send_startup_message()
    await bot.run_until_disconnected()

if __name__ == "__main__":
    bot.loop.run_until_complete(main())