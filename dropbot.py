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
import zipfile
import tarfile
import rarfile
import shutil
import glob

VERSION = "1.6.0"

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

if str(ANONYMOUS_USER_ID) in str(TELEGRAM_ADMIN).split(','):
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
    "ebook": DOWNLOAD_EBOOK if FILTER_EBOOK else DOWNLOAD_PATH,
    "youtube_video": DOWNLOAD_YOUTUBE_VIDEO if FILTER_YOUTUBE_VIDEO else (DOWNLOAD_VIDEO if FILTER_VIDEO else DOWNLOAD_PATH),
    "youtube_audio": DOWNLOAD_YOUTUBE_AUDIO if FILTER_YOUTUBE_AUDIO else (DOWNLOAD_AUDIO if FILTER_AUDIO else DOWNLOAD_PATH)
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
    if await check_admin_and_warn(event):
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
        await event.reply(get_text("downloaded", ico, get_filename_from_path(file_path)))
        debug(get_text("debug_file_downloaded", file_name))

        if is_compressed_file(file_path):
            if is_split_zip(file_name):
                warning(get_text("warning_zip_split_not_supported", file_name))
            else:
                base_name = os.path.splitext(file_path)[0]
                if rarfile.is_rarfile(file_path):
                    base_name = clean_rar_base_name(file_name)
                extracted_path = os.path.join(download_path, os.path.basename(base_name))
                os.makedirs(extracted_path, exist_ok=True)

                extract_result = extract_file(file_path, extracted_path)
                if extract_result == True:
                    buttons = [Button.inline(get_text("button_keep"), data=f"keep:{file_path}"), Button.inline(get_text("button_delete"), data=f"del:{file_path}")]
                    await event.reply(get_text("extracted_pending", extracted_path), buttons=buttons)
                    debug(get_text("debug_file_extracted", file_name))
                elif extract_result == False:
                    await event.reply(get_text("error_file_extracted_user", file_name))
                elif extract_result == "missing_parts":
                    await event.reply(get_text("missing_rar_parts"))

    except asyncio.CancelledError:
        await status_message.edit(get_text("cancelled"), buttons=None)
        if os.path.exists(file_path):
            os.remove(file_path)
        debug(get_text("debug_file_cancelled", file_name))
    finally:
        active_tasks.pop(event.id, None)

def extract_file(file_path, extract_to):
    try:
        if file_path.lower().endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_to)
        elif any(file_path.lower().endswith(ext) for ext in ['.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz']):
            with tarfile.open(file_path, 'r:*') as tar_ref:
                tar_ref.extractall(extract_to)
        elif rarfile.is_rarfile(file_path):
            try:
                with rarfile.RarFile(file_path) as rar_ref:
                    rar_ref.extractall(extract_to)
            except Exception as e:
                msg = str(e).lower()
                if ("need to start from first volume" in msg or
                    "need first volume" in msg or
                    "missing volume" in msg or
                    "unexpected end of archive" in msg):
                    debug(get_text("debug_rar_missing_parts", file_path))
                    return "missing_parts" 
                else:
                    raise
        else:
            return False
        return True

    except Exception as e:
        error(get_text("error_file_extracted", file_path, e))
        if os.path.exists(extract_to):
            try:
                shutil.rmtree(extract_to)
                debug(get_text("debug_deleted_folder", extract_to))
            except Exception as cleanup_error:
                error(get_text("error_deleting_folder", extract_to, cleanup_error))
        return False

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
    if await check_admin_and_warn(event):
        return

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

@bot.on(events.NewMessage(pattern=r'https?://(?:\w+\.)?(youtube\.com|youtu\.be)/(watch\?v=|shorts/)?[\w\-]+'))
async def handle_youtube_link(event):
    if await check_admin_and_warn(event):
        return

    url = clean_youtube_link(event.raw_text.strip())
    buttons = [
        [Button.inline(get_text("audio", AUD_ICO), data=f"yt_audio:{url}"), Button.inline(get_text("video", VID_ICO), data=f"yt_video:{url}")],
        [Button.inline(get_text("button_cancel"), data=f"simplecancel")]
    ]
    await event.reply(get_text("dowload_asking"), buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b"simplecancel"))
async def cancel_simple(event):
    if await check_admin_and_warn(event):
        return

    debug(get_text("debug_yt_download_cancelled"))
    await event.delete()

@bot.on(events.CallbackQuery(pattern=b"keep:(.+)"))
async def handle_keep_file(event):
    if await check_admin_and_warn(event):
        return

    await event.answer()
    file_path = event.pattern_match.group(1).decode()
    await event.edit(get_text("extracted", file_path), buttons=None)

@bot.on(events.CallbackQuery(pattern=b"del:(.+)"))
async def handle_delete_file(event):
    if await check_admin_and_warn(event):
        return

    await event.answer()
    file_path = event.pattern_match.group(1).decode()

    try:
        if os.path.isfile(file_path):
            filename = os.path.basename(file_path).lower()
            dirname = os.path.dirname(file_path)
            rar_patterns = [
                r"(.*)\.part\d+\.rar$",
                r"(.*)\.r\d{2}$",
                r"(.*)\.rar$"
            ]

            matched_base = None
            for pattern in rar_patterns:
                m = re.match(pattern, filename)
                if m:
                    matched_base = m.group(1)
                    break

            if matched_base:
                all_parts = []
                for f in os.listdir(dirname):
                    f_lower = f.lower()
                    if (f_lower.startswith(matched_base)
                        and (f_lower.endswith(".rar") or re.match(r".*\.r\d{2}$", f_lower) or ".part" in f_lower)):
                        full_path = os.path.join(dirname, f)
                        if os.path.isfile(full_path):
                            all_parts.append(full_path)

                for part in all_parts:
                    try:
                        os.remove(part)
                        msg = get_text("extracted_and_deleted_with_parts", file_path)
                        debug(get_text("debug_deleted_file", part))
                    except Exception as e:
                        debug(get_text("error_deleting", file_path, e))
            else:
                os.remove(file_path)
                msg = get_text("extracted_and_deleted", file_path)
                debug(get_text("debug_deleted_file", file_path))
        elif os.path.isdir(file_path):
            msg = get_text("error_trying_to_delete_folder_user", file_path)
            error(get_text("error_trying_to_delete_folder", file_path))
        else:
            msg = get_text("error_file_does_not_exist_user")
            error(get_text("error_file_does_not_exist", file_path))

        await event.edit(msg, buttons=None)

    except Exception as e:
        await event.edit(get_text("error_deleting_user", file_path), buttons=None)
        debug(get_text("error_deleting", file_path, e))

@bot.on(events.CallbackQuery(pattern=b"yt_(audio|video):(.+)"))
async def handle_format_selection(event):
    if await check_admin_and_warn(event):
        return

    await event.answer()
    format_type = event.pattern_match.group(1).decode()
    url = event.pattern_match.group(2).decode()
    is_audio = format_type == "audio"

    format_flag = "bestaudio" if is_audio else "bv*+ba/best"
    output_dir = DOWNLOAD_PATHS["youtube_audio"] if is_audio else DOWNLOAD_PATHS["youtube_video"]

    status_message = await event.edit(
        get_text("downloading", AUD_ICO if is_audio else VID_ICO),
        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")]
    )

    cmd = [
        "yt-dlp",
        "-f", format_flag,
        "--restrict-filenames",
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
                icon = AUD_ICO if file_path.endswith(".mp3") else VID_ICO
                await event.reply(get_text("downloaded", icon, get_filename_from_path(file_path)))
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
        await event.reply(get_text("downloaded", icon, get_filename_from_path(file_path)))

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
    if await check_admin_and_warn(event):
        return

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
    admins = TELEGRAM_ADMIN.split(',')
    for admin in admins:
        try:
            await bot.send_message(int(admin), get_text("initial_message", VERSION))
        except Exception as e:
            error(get_text("error_sending_initial_message", e))

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

async def check_admin_and_warn(event):
    if not is_admin(event.sender_id):
        sender = await event.get_sender()
        username = sender.username if sender else None
        warning(get_text("warning_not_admin", event.sender_id, username))
        return True
    return False

async def main():
    debug(f"DropBot v{VERSION}")
    await bot.start()
    await set_commands()
    await send_startup_message()
    await bot.run_until_disconnected()

if __name__ == "__main__":
    bot.loop.run_until_complete(main())