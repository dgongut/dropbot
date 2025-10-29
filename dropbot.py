import os
import re
import warnings
from telethon import TelegramClient, events, functions, types, Button
from telethon.tl.types import (
    BotCommand, Document, Photo,
    DocumentAttributeFilename, DocumentAttributeVideo, DocumentAttributeAudio
)
from config import *
from translations import get_text, load_locale, PARSE_MODE
from debug import *
from basic import *
import sys
import asyncio
import zipfile
import tarfile
import rarfile
import shutil
import glob

VERSION = "2.0.0"

warnings.filterwarnings('ignore', message='Using async sessions support is an experimental feature')

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
    "url_video": DOWNLOAD_URL_VIDEO if FILTER_URL_VIDEO else (DOWNLOAD_VIDEO if FILTER_VIDEO else DOWNLOAD_PATH),
    "url_audio": DOWNLOAD_URL_AUDIO if FILTER_URL_AUDIO else (DOWNLOAD_AUDIO if FILTER_AUDIO else DOWNLOAD_PATH)
}

for path in DOWNLOAD_PATHS.values():
    os.makedirs(path, exist_ok=True)

bot = TelegramClient("dropbot", TELEGRAM_API_ID, TELEGRAM_API_HASH).start(bot_token=TELEGRAM_TOKEN)
active_tasks = {}

# Intervalo de actualización de progreso adaptativo
# Evita anti-spam cuando hay múltiples descargas paralelas
# Fórmula: max(3, PARALLEL_DOWNLOADS * 1) segundos
# Ejemplos: 2 descargas = 3s, 5 descargas = 5s, 10 descargas = 10s
PROGRESS_UPDATE_INTERVAL = max(3, PARALLEL_DOWNLOADS * 1)
pending_files = {}
pending_urls = {}
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

def create_upload_progress_callback(status_message, file_name):
    """
    Crea un callback de progreso para envíos a Telegram.
    Actualiza el mensaje cada PROGRESS_UPDATE_INTERVAL segundos para evitar anti-spam.
    """
    last_update_time = [0]  # Lista para poder modificar en closure

    async def progress_callback(current, total):
        try:
            current_time = asyncio.get_event_loop().time()
            if current_time - last_update_time[0] >= PROGRESS_UPDATE_INTERVAL:
                last_update_time[0] = current_time

                # Calcular progreso
                percent = (current / total) * 100

                # Convertir bytes a formato legible
                def format_bytes(bytes_val):
                    for unit in ['B', 'KB', 'MB', 'GB']:
                        if bytes_val < 1024.0:
                            return f"{bytes_val:.1f}{unit}"
                        bytes_val /= 1024.0
                    return f"{bytes_val:.1f}TB"

                size_current = format_bytes(current)
                size_total = format_bytes(total)

                # Calcular velocidad (aproximada basada en el intervalo)
                if last_update_time[0] > 0:
                    bytes_diff = current - getattr(progress_callback, 'last_current', 0)
                    time_diff = current_time - getattr(progress_callback, 'last_time', current_time)
                    if time_diff > 0:
                        speed = format_bytes(bytes_diff / time_diff) + "/s"
                    else:
                        speed = "N/A"
                else:
                    speed = "N/A"

                progress_callback.last_current = current
                progress_callback.last_time = current_time

                # Calcular ETA
                if hasattr(progress_callback, 'last_current') and speed != "N/A":
                    remaining_bytes = total - current
                    if bytes_diff > 0 and time_diff > 0:
                        eta_seconds = int(remaining_bytes / (bytes_diff / time_diff))
                        eta_minutes = eta_seconds // 60
                        eta_secs = eta_seconds % 60
                        eta = f"{eta_minutes:02d}:{eta_secs:02d}"
                    else:
                        eta = "N/A"
                else:
                    eta = "N/A"

                # Crear barra de progreso visual
                bar_length = 10
                filled = int(percent / 10)
                bar = "█" * filled + "░" * (bar_length - filled)

                message = get_text("uploading_progress", bar, f"{percent:.1f}", f"{size_current}/{size_total}", speed, eta, file_name)

                await status_message.edit(
                    message,
                    parse_mode=PARSE_MODE
                )
        except Exception as e:
            # Ignorar errores de actualización (FloodWaitError, etc.)
            debug(f"Error actualizando progreso de envío: {e}")

    return progress_callback

def create_progress_callback(status_message, event, file_name):
    """
    Crea un callback de progreso para descargas desde Telegram.
    Actualiza el mensaje cada PROGRESS_UPDATE_INTERVAL segundos para evitar anti-spam.
    """
    last_update_time = [0]  # Lista para poder modificar en closure

    async def progress_callback(current, total):
        try:
            current_time = asyncio.get_event_loop().time()
            if current_time - last_update_time[0] >= PROGRESS_UPDATE_INTERVAL:
                last_update_time[0] = current_time

                # Calcular progreso
                percent = (current / total) * 100

                # Convertir bytes a formato legible
                def format_bytes(bytes_val):
                    for unit in ['B', 'KB', 'MB', 'GB']:
                        if bytes_val < 1024.0:
                            return f"{bytes_val:.1f}{unit}"
                        bytes_val /= 1024.0
                    return f"{bytes_val:.1f}TB"

                size_current = format_bytes(current)
                size_total = format_bytes(total)

                # Calcular velocidad (aproximada basada en el intervalo)
                if last_update_time[0] > 0:
                    bytes_diff = current - getattr(progress_callback, 'last_current', 0)
                    time_diff = current_time - getattr(progress_callback, 'last_time', current_time)
                    if time_diff > 0:
                        speed = format_bytes(bytes_diff / time_diff) + "/s"
                    else:
                        speed = "N/A"
                else:
                    speed = "N/A"

                progress_callback.last_current = current
                progress_callback.last_time = current_time

                # Calcular ETA
                if hasattr(progress_callback, 'last_current') and speed != "N/A":
                    remaining_bytes = total - current
                    if bytes_diff > 0 and time_diff > 0:
                        eta_seconds = int(remaining_bytes / (bytes_diff / time_diff))
                        eta_minutes = eta_seconds // 60
                        eta_secs = eta_seconds % 60
                        eta = f"{eta_minutes:02d}:{eta_secs:02d}"
                    else:
                        eta = "N/A"
                else:
                    eta = "N/A"

                # Crear barra de progreso visual
                bar_length = 10
                filled = int(percent / 10)
                bar = "█" * filled + "░" * (bar_length - filled)

                message = get_text("downloading_progress", bar, f"{percent:.1f}", f"{size_current}/{size_total}", speed, eta, file_name)

                await status_message.edit(
                    message,
                    buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                    parse_mode=PARSE_MODE
                )
        except Exception as e:
            # Ignorar errores de actualización (FloodWaitError, etc.)
            debug(f"Error actualizando progreso de Telegram: {e}")

    return progress_callback

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
        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
        parse_mode=PARSE_MODE
    )

    # Crear callback de progreso
    progress_callback = create_progress_callback(status_message, event, file_name)

    # Intentar descargar con reintentos
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            await bot.download_media(message, file=file_path, progress_callback=progress_callback)
            # Descarga exitosa
            await status_message.delete()
            await event.reply(get_text("downloaded", ico, get_filename_from_path(file_path)), parse_mode=PARSE_MODE)
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
                        await event.reply(get_text("extracted_pending", extracted_path), buttons=buttons, parse_mode=PARSE_MODE)
                        debug(get_text("debug_file_extracted", file_name))
                    elif extract_result == False:
                        await event.reply(get_text("error_file_extracted_user", file_name), parse_mode=PARSE_MODE)
                    elif extract_result == "missing_parts":
                        await event.reply(get_text("missing_rar_parts"), parse_mode=PARSE_MODE)

            # Salir del bucle si la descarga fue exitosa
            break

        except asyncio.CancelledError:
            await status_message.edit(get_text("cancelled"), buttons=None, parse_mode=PARSE_MODE)
            if os.path.exists(file_path):
                os.remove(file_path)
            debug(get_text("debug_file_cancelled", file_name))
            raise

        except (TimeoutError, ValueError) as e:
            error_msg = str(e)
            if "timeout" in error_msg.lower() or "unsuccessful" in error_msg.lower():
                # Eliminar archivo parcialmente descargado
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        debug(get_text("debug_deleted_partial_file", file_path))
                    except Exception as cleanup_error:
                        error(get_text("error_deleting", file_path, cleanup_error))

                # Si aún quedan intentos, reintentar
                if attempt < MAX_DOWNLOAD_RETRIES:
                    debug(get_text("debug_retrying_download", file_name, attempt + 1, MAX_DOWNLOAD_RETRIES, error_msg))
                    try:
                        await status_message.edit(
                            get_text("warning_retrying_download", attempt + 1, MAX_DOWNLOAD_RETRIES),
                            buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                            parse_mode=PARSE_MODE
                        )
                    except Exception as msg_error:
                        error(get_text("error_updating_status_message", msg_error))

                    # Esperar antes de reintentar
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                else:
                    # Último intento fallido
                    error(get_text("error_telegram_timeout", file_name, error_msg))
                    try:
                        await status_message.edit(
                            get_text("error_telegram_timeout_user", file_name),
                            buttons=None,
                            parse_mode=PARSE_MODE
                        )
                    except Exception as msg_error:
                        error(get_text("error_updating_status_message", msg_error))
            else:
                raise

    # Limpiar tareas activas
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
    await event.reply(response, parse_mode=PARSE_MODE)

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

async def detect_content_type(url):
    """Detecta el tipo de contenido sin descargarlo usando yt-dlp --dump-json"""
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--skip-download", "--playlist-items", "1", url]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await proc.communicate()

        # Mostrar stderr si hay contenido
        stderr_output = stderr.decode().strip()
        if stderr_output:
            for line in stderr_output.splitlines():
                debug(f"yt-dlp detect stderr: {line}")

        if proc.returncode == 0:
            import json
            lines = stdout.decode().splitlines()

            # Analizar primer item para detectar tipo
            if lines:
                data = json.loads(lines[0])
                vcodec = data.get("vcodec")
                acodec = data.get("acodec")
                ext = data.get("ext", "").lower()

                debug(get_text("debug_content_detected", vcodec, acodec, ext))

                # Detectar tipo de contenido
                if vcodec and vcodec != "none":
                    return "video"  # Tiene video
                elif acodec and acodec != "none":
                    return "audio"  # Solo audio
                elif ext in ["jpg", "jpeg", "png", "gif", "webp", "bmp"]:
                    return "image"  # Solo imagen
            else:
                debug("yt-dlp detect: No se recibió JSON en stdout")
        else:
            debug(f"yt-dlp detect: Falló con código {proc.returncode}")

        return "unknown"
    except Exception as e:
        debug(get_text("error_detecting_content_type", e))
        return "unknown"

@bot.on(events.NewMessage(pattern=r'https?://[^\s]+'))
async def handle_url_link(event):
    if await check_admin_and_warn(event):
        return

    url = event.raw_text.strip()
    url_id = str(event.id)
    pending_urls[url_id] = url

    # Mostrar mensaje de análisis
    analyzing_msg = await event.reply(get_text("analyzing_url"), parse_mode=PARSE_MODE)

    # Detectar tipo de contenido
    content_type = await detect_content_type(url)

    # Crear botones según el tipo de contenido
    if content_type == "video":
        # Video: ofrecer Audio o Video
        buttons = [
            [Button.inline(get_text("audio", AUD_ICO), data=f"url_audio:{url_id}"), Button.inline(get_text("video", VID_ICO), data=f"url_video:{url_id}")],
            [Button.inline(get_text("button_cancel"), data=f"simplecancel:{url_id}")]
        ]
        message = get_text("dowload_asking")
    elif content_type == "image":
        # Imagen: solo descargar
        buttons = [
            [Button.inline(get_text("download_button", "📷"), data=f"url_video:{url_id}")],
            [Button.inline(get_text("button_cancel"), data=f"simplecancel:{url_id}")]
        ]
        message = get_text("download_image_asking")
    elif content_type == "audio":
        # Audio: solo descargar
        buttons = [
            [Button.inline(get_text("download_button", AUD_ICO), data=f"url_audio:{url_id}")],
            [Button.inline(get_text("button_cancel"), data=f"simplecancel:{url_id}")]
        ]
        message = get_text("download_audio_asking")
    else:
        # Desconocido: ofrecer solo opción de descargar (sin audio)
        buttons = [
            [Button.inline(get_text("download_button", "📥"), data=f"url_video:{url_id}")],
            [Button.inline(get_text("button_cancel"), data=f"simplecancel:{url_id}")]
        ]
        message = get_text("download_unknown_asking")

    await analyzing_msg.edit(message, buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"simplecancel:(.+)"))
async def cancel_simple(event):
    if await check_admin_and_warn(event):
        return

    url_id = event.pattern_match.group(1).decode()
    pending_urls.pop(url_id, None)
    debug(get_text("debug_url_download_cancelled"))
    await event.delete()

@bot.on(events.CallbackQuery(pattern=b"keep:(.+)"))
async def handle_keep_file(event):
    if await check_admin_and_warn(event):
        return

    await event.answer()
    file_path = event.pattern_match.group(1).decode()
    await event.edit(get_text("extracted", file_path), buttons=None, parse_mode=PARSE_MODE)

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

        await event.edit(msg, buttons=None, parse_mode=PARSE_MODE)

    except Exception as e:
        await event.edit(get_text("error_deleting_user", file_path), buttons=None, parse_mode=PARSE_MODE)
        debug(get_text("error_deleting", file_path, e))

@bot.on(events.CallbackQuery(pattern=b"url_(audio|video):(.+)"))
async def handle_format_selection(event):
    if await check_admin_and_warn(event):
        return

    await event.answer()
    format_type = event.pattern_match.group(1).decode()
    url_id = event.pattern_match.group(2).decode()

    url = pending_urls.get(url_id)
    if not url:
        await event.edit(get_text("error_url_expired"), buttons=None, parse_mode=PARSE_MODE)
        return

    pending_urls.pop(url_id, None)
    is_audio = format_type == "audio"

    format_flag = "bestaudio" if is_audio else "bv*+ba/best"
    output_dir = DOWNLOAD_PATHS["url_audio"] if is_audio else DOWNLOAD_PATHS["url_video"]

    status_message = await event.edit(
        get_text("downloading", AUD_ICO if is_audio else VID_ICO),
        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
        parse_mode=PARSE_MODE
    )

    cmd = [
        "yt-dlp",
        "-f", format_flag,
        "--restrict-filenames",
        "--newline",  # Cada línea de progreso completa (para parsear en tiempo real)
        "-o", os.path.join(output_dir, "%(title).200s.%(ext)s"),
        url
    ]

    if is_audio:
        cmd.extend(["--extract-audio", "--audio-format", "mp3"])
    else:
        cmd.extend(["--merge-output-format", "mp4"])

    task = asyncio.create_task(run_url_download(event, cmd, status_message))
    active_tasks[event.id] = task

def parse_progress(line):
    """
    Parsea líneas de progreso de yt-dlp.
    Formato: [download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:30
    Retorna: {"percent": "45.2", "size": "123.45MiB", "speed": "1.23MiB/s", "eta": "00:30"}
    """
    try:
        # Patrón para capturar: porcentaje, tamaño, velocidad, ETA
        pattern = r'\[download\]\s+(\d+\.?\d*)%\s+of\s+([\d\.]+\w+)(?:\s+at\s+([\d\.]+\w+/s))?(?:\s+ETA\s+([\d:]+))?'
        match = re.search(pattern, line)

        if match:
            return {
                "percent": match.group(1),
                "size": match.group(2),
                "speed": match.group(3) if match.group(3) else "N/A",
                "eta": match.group(4) if match.group(4) else "N/A"
            }
    except Exception as e:
        debug(f"Error parseando progreso: {e}")
    return None

async def update_progress_message(status_message, progress_info, event, file_name=None):
    """Actualiza el mensaje de Telegram con el progreso de descarga"""
    try:
        percent = progress_info["percent"]
        size = progress_info["size"]
        speed = progress_info["speed"]
        eta = progress_info["eta"]

        # Crear barra de progreso visual
        bar_length = 10
        filled = int(float(percent) / 10)
        bar = "█" * filled + "░" * (bar_length - filled)

        # Si no hay nombre de archivo, intentar extraerlo del progreso o usar placeholder
        if not file_name:
            file_name = progress_info.get("filename", "...")

        message = get_text("downloading_progress", bar, percent, size, speed, eta, file_name)

        await status_message.edit(
            message,
            buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
            parse_mode=PARSE_MODE
        )
    except Exception as e:
        # Ignorar errores de actualización (puede ser FloodWaitError)
        debug(f"Error actualizando progreso: {e}")

async def run_url_download(event, cmd, status_message):
    try:
        debug(get_text("debug_creating_url_subprocess"))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        active_tasks[event.id] = proc

        # Variables para control de progreso
        stdout_lines = []
        last_update_time = 0
        update_interval = PROGRESS_UPDATE_INTERVAL  # Intervalo dinámico basado en PARALLEL_DOWNLOADS
        current_filename = None  # Almacenar el nombre del archivo actual

        # Leer stdout línea por línea en tiempo real
        async def read_stdout():
            nonlocal last_update_time, current_filename
            async for line in proc.stdout:
                line_str = line.decode().strip()
                stdout_lines.append(line_str)
                debug(f"yt-dlp: {line_str}")

                # Detectar nombre del archivo: [download] Destination: /path/to/file.mp4
                if "[download] Destination:" in line_str:
                    # Extraer el path completo y obtener solo el nombre del archivo
                    path_start = line_str.find("Destination:") + len("Destination:")
                    full_path = line_str[path_start:].strip()
                    current_filename = os.path.basename(full_path)
                    debug(f"Nombre de archivo detectado: {current_filename}")

                # Detectar líneas de progreso: [download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:30
                if "[download]" in line_str and "%" in line_str:
                    current_time = asyncio.get_event_loop().time()
                    if current_time - last_update_time >= update_interval:
                        last_update_time = current_time
                        # Parsear y actualizar mensaje
                        progress_info = parse_progress(line_str)
                        if progress_info:
                            await update_progress_message(status_message, progress_info, event, current_filename)

        # Leer stderr en paralelo
        async def read_stderr():
            stderr_lines = []
            async for line in proc.stderr:
                line_str = line.decode().strip()
                stderr_lines.append(line_str)
                if line_str:
                    debug(f"yt-dlp stderr: {line_str}")
            return stderr_lines

        # Ejecutar lectura de stdout y stderr en paralelo
        stderr_task = asyncio.create_task(read_stderr())
        await read_stdout()
        stderr_lines = await stderr_task

        # Esperar a que termine el proceso
        await proc.wait()
        debug(get_text("debug_exiting_url_subprocess", proc.returncode))

        if proc.returncode == -15:
            await handle_cancel(status_message)
            return

        await status_message.delete()
        if proc.returncode == 0:
            file_paths = extract_file_paths(stdout_lines)

            if file_paths:
                for file_path in file_paths:
                    if os.path.exists(file_path):
                        await handle_success(event, file_path)
                    else:
                        debug(get_text("error_output_file_not_found", file_path))
            else:
                debug(get_text("error_no_files_downloaded"))
                debug(f"Comando ejecutado: {' '.join(cmd)}")
                debug(f"Stdout completo: {chr(10).join(stdout_lines)}")
                await event.reply(get_text("error_url_failed_user"), parse_mode=PARSE_MODE)
        else:
            stderr_output = "\n".join(stderr_lines)
            debug(get_text("error_url_failed", stderr_output))
            await event.reply(get_text("error_url_failed_user"), parse_mode=PARSE_MODE)

    except asyncio.CancelledError:
        await handle_cancel(status_message)
        raise
    finally:
        debug(get_text("debug_cleaning_url_subprocess", event.id))
        active_tasks.pop(event.id, None)

def extract_file_paths(stdout_lines):
    """Extrae todas las rutas de archivos descargados (soporta playlists/carruseles)"""
    file_paths = []
    current_file = None
    is_partial = False

    for line in stdout_lines:
        debug(f"yt-dlp: {line}")

        # Detectar archivo de destino inicial
        if "[download] Destination: " in line:
            possible_path = line.split("[download] Destination: ")[-1].strip()
            if possible_path:
                current_file = possible_path
                # Detectar si es un archivo parcial (será mergeado después)
                is_partial = ".fdash-" in possible_path or ".f" in possible_path.split(".")[-2] if "." in possible_path else False

        # Detectar merge de formatos (este es el archivo final)
        elif "[Merger]" in line and "Merging formats into" in line:
            merged_path = line.split("Merging formats into")[-1].strip().strip('"')
            debug(get_text("debug_file_path_merged_detected", merged_path))
            current_file = merged_path
            is_partial = False  # El archivo mergeado es el final
            # Agregar inmediatamente el archivo mergeado
            if current_file and current_file not in file_paths:
                file_paths.append(current_file)
                debug(get_text("debug_file_added_to_list", current_file))

        # Detectar extracción de audio (este es el archivo final)
        elif "[ExtractAudio]" in line and "Destination:" in line:
            audio_path = line.split("Destination:")[-1].strip()
            debug(get_text("debug_file_path_audio_detected", audio_path))
            current_file = audio_path
            is_partial = False  # El audio extraído es el final
            # Agregar inmediatamente el audio extraído
            if current_file and current_file not in file_paths:
                file_paths.append(current_file)
                debug(get_text("debug_file_added_to_list", current_file))

        # Detectar finalización de descarga (solo agregar si NO es parcial)
        elif "[download] 100%" in line or "has already been downloaded" in line:
            if current_file and not is_partial and current_file not in file_paths:
                file_paths.append(current_file)
                debug(get_text("debug_file_added_to_list", current_file))
                current_file = None

    return file_paths

async def get_video_metadata(file_path):
    """Obtiene metadatos del video usando ffprobe"""
    try:
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, _ = await proc.communicate()

        if proc.returncode == 0:
            import json
            data = json.loads(stdout.decode())

            # Buscar el stream de video
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    duration = int(float(data.get("format", {}).get("duration", 0)))
                    width = stream.get("width", 0)
                    height = stream.get("height", 0)
                    return duration, width, height

        return None, None, None
    except Exception as e:
        debug(get_text("error_getting_video_metadata", e))
        return None, None, None

async def handle_success(event, file_path):
    try:
        file_size = os.path.getsize(file_path)
        debug(get_text("debug_filesize", file_size))

        icon = "🎵" if file_path.endswith(".mp3") else "🎥"
        await event.reply(get_text("downloaded", icon, get_filename_from_path(file_path)), parse_mode=PARSE_MODE)

        if file_size <= 2 * 1024 * 1024 * 1024:
            pending_files[event.id] = file_path
            buttons = [
                [
                    Button.inline(get_text("button_send"), data=f"send:{event.id}"),
                    Button.inline(get_text("button_send_and_delete"), data=f"senddelete:{event.id}"),
                ],
                [Button.inline(get_text("button_only_in_server"), data=f"nosend:{event.id}")]
            ]
            await event.reply(get_text("upload_asking"), buttons=buttons, parse_mode=PARSE_MODE)
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
        await event.edit(get_text("error_file_does_not_exist_user"), parse_mode=PARSE_MODE)
        error(get_text("error_file_does_not_exist", file_path))
        return

    if action in ("send", "senddelete"):
        try:
            sending_msg = await event.edit(
                get_text("sending", os.path.basename(file_path)),
                parse_mode=PARSE_MODE
            )

            # Detectar si es un video y obtener metadatos
            is_video = file_path.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv'))
            attributes = None

            if is_video:
                duration, width, height = await get_video_metadata(file_path)
                if duration and width and height:
                    from telethon.tl.types import DocumentAttributeVideo
                    attributes = [DocumentAttributeVideo(
                        duration=duration,
                        w=width,
                        h=height,
                        supports_streaming=True
                    )]
                    debug(get_text("debug_video_metadata", duration, width, height))

            # Crear callback de progreso para el envío
            upload_progress = create_upload_progress_callback(sending_msg, os.path.basename(file_path))

            message = await bot.send_file(
                event.chat_id,
                file_path,
                caption=os.path.basename(file_path),
                attributes=attributes,
                supports_streaming=True if is_video else None,
                progress_callback=upload_progress
            )
            debug(get_text("debug_sent", file_path))
            await sending_msg.delete()

            if action == "senddelete":
                os.remove(file_path)
                debug(get_text("debug_sent_and_delete", file_path))
                await event.respond(get_text("deleted_from_server"), reply_to=message.id, parse_mode=PARSE_MODE)
        except Exception as e:
            await event.reply(get_text("error_sending_the_file_user"), parse_mode=PARSE_MODE)
            debug(get_text("error_sending_the_file", e))
    else:
        await event.delete()

async def handle_cancel(status_message):
    await status_message.edit(get_text("cancelled"), buttons=None, parse_mode=PARSE_MODE)
    debug(get_text("debug_url_download_cancelled"))
    cleanup_partials()

def cleanup_partials():
    pattern = os.path.join(DOWNLOAD_PATHS["url_video"], "*.part*")
    for f in glob.glob(pattern):
        debug(get_text("debug_url_cleaning_partial_files", f))
        os.remove(f)

async def send_startup_message():
    admins = TELEGRAM_ADMIN.split(',')
    for admin in admins:
        try:
            await bot.send_message(int(admin), get_text("initial_message", VERSION), parse_mode=PARSE_MODE)
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