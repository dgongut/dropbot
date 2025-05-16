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
import asyncio
import glob

VERSION = "1.2.0"

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
pending_files = {}
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

    if isinstance(task, asyncio.subprocess.Process):
        task.terminate()
        await event.answer(get_text("cancelling"))
    elif isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        await event.answer(get_text("cancelling"))
    else:
        await event.answer(get_text("already_cancelled"))

@bot.on(events.NewMessage(pattern=r'https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+'))
async def handle_youtube_link(event):
    url = event.raw_text.strip()
    buttons = [
        [Button.inline(get_text("audio"), data=f"yt_audio:{url}"), Button.inline(get_text("video"), data=f"yt_video:{url}")]
    ]
    await event.reply(get_text("dowload_asking"), buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b"yt_(audio|video):(.+)"))
async def handle_format_selection(event):
    await event.answer()
    format_type = event.pattern_match.group(1).decode()
    url = event.pattern_match.group(2).decode()
    is_audio = format_type == "audio"

    format_flag = "bestaudio" if is_audio else "bv*+ba/best"
    output_dir = DOWNLOAD_PATHS["audio"] if is_audio else DOWNLOAD_PATHS["video"]

    status_message = await event.edit(
        get_text("downloading", "🎵" if is_audio else "🎥"),
        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")]
    )

    cmd = [
        "yt-dlp",
        "-f", format_flag,
        "-o", os.path.join(output_dir, "%(title).200s.%(ext)s"),
        url
    ]

    if is_audio:
        cmd.extend(["--extract-audio", "--audio-format", "mp3"])
    else:
        cmd.extend(["--merge-output-format", "mp4"])

    task = asyncio.create_task(run_yt_dlp(event, cmd, status_message))
    active_tasks[event.id] = task

async def run_yt_dlp(event, cmd, status_message):
    try:
        debug(get_text("debug_creating_yt_subprocess"))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        active_tasks[event.id] = proc

        stdout, stderr = await proc.communicate()
        debug(get_text("debug_exiting_yt_subprocess", proc.returncode))

        if proc.returncode == -15:
            await handle_cancel(status_message)
            return

        await status_message.delete()
        if proc.returncode == 0:
            stdout_lines = stdout.decode().splitlines()
            file_path = extract_file_path(stdout_lines)

            if file_path and os.path.exists(file_path):
                await handle_success(event, file_path)
            else:
                debug(get_text("error_output_file_not_found", file_path))
                icon = "🎵" if file_path.endswith(".mp3") else "🎥"
                await event.reply(get_text("downloaded", icon, file_path))
        else:
            error_output = stderr.decode()
            debug(get_text("error_yt_failed", error_output))
            await event.reply(get_text("error_yt_failed_user"))

    except asyncio.CancelledError:
        await handle_cancel(status_message)
        raise
    finally:
        debug(get_text("debug_cleaning_yt_subprocess", event.id))
        active_tasks.pop(event.id, None)

def extract_file_path(stdout_lines):
    file_path = None
    for line in stdout_lines:
        debug(f"yt-dlp: {line}")
        if "[download] Destination: " in line:
            possible_path = line.split("[download] Destination: ")[-1].strip()
            if possible_path:
                file_path = possible_path
        elif "[Merger]" in line and "Merging formats into" in line:
            file_path = line.split("Merging formats into")[-1].strip().strip('"')
            debug(get_text("debug_file_path_merged_detected", file_path))
            break
        elif "[ExtractAudio]" in line and "Destination:" in line:
            file_path = line.split("Destination:")[-1].strip()
            debug(get_text("debug_file_path_audio_detected", file_path))
            break
    return file_path

async def handle_success(event, file_path):
    try:
        file_size = os.path.getsize(file_path)
        debug(get_text("debug_filesize", file_size))

        icon = "🎵" if file_path.endswith(".mp3") else "🎥"
        await event.reply(get_text("downloaded", icon, file_path))

        if file_size <= 2 * 1024 * 1024 * 1024:
            pending_files[event.id] = file_path
            buttons = [
                [
                    Button.inline(get_text("button_send"), data=f"send:{event.id}"),
                    Button.inline(get_text("button_send_and_delete"), data=f"senddelete:{event.id}"),
                ],
                [Button.inline(get_text("button_only_in_server"), data=f"nosend:{event.id}")]
            ]
            await event.reply(get_text("upload_asking"), buttons=buttons)
        else:
            debug(get_text("debug_filesize_to_high_to_telegram"))
    except Exception as e:
        debug(get_text("error_sending_the_file", e))


@bot.on(events.CallbackQuery(pattern=b"(send|senddelete|nosend):(.+)"))
async def handle_send_choice(event):
    await event.answer()
    action = event.pattern_match.group(1).decode()
    file_id = int(event.pattern_match.group(2).decode())
    file_path = pending_files.get(file_id)

    if not os.path.exists(file_path):
        await event.edit(get_text("error_file_does_not_exist_user"))
        error(get_text("error_file_does_not_exist", file_path))
        return

    if action in ("send", "senddelete"):
        try:
            sending_msg = await event.edit(
                get_text("sending", os.path.basename(file_path)),
                parse_mode="markdown"
            )
            message = await bot.send_file(event.chat_id, file_path, caption=os.path.basename(file_path))
            debug(get_text("debug_sent", file_path))
            await sending_msg.delete()

            if action == "senddelete":
                os.remove(file_path)
                debug(get_text("debug_sent_and_delete", file_path))
                await event.respond(get_text("deleted_from_server"), reply_to=message.id)
        except Exception as e:
            await event.reply(get_text("error_sending_the_file_user"))
            debug(get_text("error_sending_the_file", e))
    else:
        await event.delete()

async def handle_cancel(status_message):
    await status_message.edit(get_text("cancelled"), buttons=None)
    debug(get_text("debug_yt_download_cancelled"))
    cleanup_partials()

def cleanup_partials():
    pattern = os.path.join(DOWNLOAD_PATHS["video"], "*.part*")
    for f in glob.glob(pattern):
        debug(get_text("debug_yt_cleaning_partial_files", f))
        os.remove(f)

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