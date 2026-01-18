import os
import re
import sys
import asyncio
import zipfile
import tarfile
import rarfile
import shutil
import glob
import requests
import time
import json
import warnings
from urllib.parse import urlparse, unquote
from telethon import TelegramClient, events, functions, types, Button
from telethon.tl.types import (
    BotCommand, Document, Photo,
    DocumentAttributeFilename, DocumentAttributeVideo, DocumentAttributeAudio
)
from config import *
from translations import get_text, load_locale, PARSE_MODE
from debug import *
from basic import *
from message_queue import TelegramMessageQueue

VERSION = "3.1.2"

warnings.filterwarnings('ignore', message='Using async sessions support is an experimental feature')

if LANGUAGE.lower() not in ("es", "en"):
    error("[CONFIG] LANGUAGE only can be ES/EN")
    sys.exit(1)

load_locale(LANGUAGE.lower())

if DEFAULT_EMPTY_STR == TELEGRAM_TOKEN:
    error("[CONFIG] Bot token needs to be configured with the TELEGRAM_TOKEN variable")
    sys.exit(1)

if DEFAULT_EMPTY_STR == TELEGRAM_ADMIN:
    error("[CONFIG] The chatId of the user who will interact with the bot needs to be configured with the TELEGRAM_ADMIN variable")
    sys.exit(1)

if str(ANONYMOUS_USER_ID) in str(TELEGRAM_ADMIN).split(','):
	error("[CONFIG] You cannot be anonymous to control the bot. In the TELEGRAM_ADMIN variable, you must put your user id.")
	sys.exit(1)

if PARALLEL_DOWNLOADS < 1:
    error("[CONFIG] The minimum number of parallel downloads is 1. An incorrect number has been configured in the PARALLEL_DOWNLOADS variable")
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

# Carpeta temporal para archivos de conversión, descargas, thumbnails, etc.
TEMP_DIR = "/tmp/dropbot_conversions"

# Limpiar TODA la carpeta temporal al arrancar (eliminar archivos huérfanos de sesiones anteriores)
debug(f"[STARTUP] Cleaning temporary directory: {TEMP_DIR}...")
try:
    if os.path.exists(TEMP_DIR):
        # Eliminar toda la carpeta y su contenido
        shutil.rmtree(TEMP_DIR)
        debug(f"[STARTUP] Removed temporary directory and all contents")

    # Recrear la carpeta vacía
    os.makedirs(TEMP_DIR, exist_ok=True)
    debug(f"[STARTUP] ✅ Temporary directory cleaned and recreated")
except Exception as e:
    warning(f"[STARTUP] Could not clean temporary directory: {e}")
    # Si falla, al menos intentar crear la carpeta
    os.makedirs(TEMP_DIR, exist_ok=True)

# Configurar rarfile para usar unrar
try:
    # Intentar configurar la herramienta de extracción RAR
    # unrar está disponible en Ubuntu y tiene soporte completo para RAR/RAR5
    rarfile.UNRAR_TOOL = "unrar"
    rarfile.tool_setup()
    debug("[STARTUP] RAR extraction tool (unrar) configured successfully")
except rarfile.RarCannotExec:
    warning("[STARTUP] unrar tool not found. RAR extraction will not work. Install unrar package.")
except Exception as e:
    warning(f"[STARTUP] Error configuring RAR tool: {e}")

bot = TelegramClient("dropbot", TELEGRAM_API_ID, TELEGRAM_API_HASH).start(bot_token=TELEGRAM_TOKEN)
active_tasks = {}
cancelled_conversions = set()  # Para rastrear conversiones canceladas por el usuario
send_original_requests = set()  # Para rastrear solicitudes de envío de archivo original sin conversión
pending_file_actions = {}  # Para almacenar archivos pendientes de renombrar/eliminar
pending_renames = {}  # Para almacenar archivos esperando nuevo nombre (lista por usuario)
list_messages = {}  # Para rastrear mensajes de /list y /manage que deben borrarse juntos: {user_id: [msg1, msg2, ...]}

# Intervalo de actualización de progreso adaptativo
# Evita anti-spam cuando hay múltiples descargas paralelas
# Telegram permite ~20 mensajes/minuto en grupos, pero con ediciones es más restrictivo
# Usamos un intervalo muy conservador para evitar FloodWaitError
# Fórmula: max(10, PARALLEL_DOWNLOADS * 1.5) segundos
# Ejemplos: 1 descarga = 10s (6 ediciones/min), 5 descargas = 10s (30 ediciones/min), 10 descargas = 15s (40 ediciones/min)
PROGRESS_UPDATE_INTERVAL = max(10, PARALLEL_DOWNLOADS * 1.5)
pending_files = {}
pending_urls = {}
playlist_downloads = {}  # Para rastrear descargas de playlist en progreso: {event_id: {"is_full_playlist": bool, "final_output_dir": str, "downloaded_files": []}}
download_semaphore = asyncio.Semaphore(PARALLEL_DOWNLOADS)

# Inicializar cola de mensajes para evitar FloodWaitError
# delay_between_messages: tiempo entre mensajes (configurable, default: 0.5s)
# max_retries: número de reintentos en caso de error (configurable, default: 5)
message_queue = TelegramMessageQueue(
    delay_between_messages=MESSAGE_QUEUE_DELAY,
    max_retries=MESSAGE_QUEUE_MAX_RETRIES
)

# Funciones wrapper para usar la cola de mensajes
async def safe_edit(message, *args, wait_for_result=False, **kwargs):
    """Edita un mensaje usando la cola para evitar rate limiting"""
    return await message_queue.add_message(message.edit, *args, wait_for_result=wait_for_result, **kwargs)

async def safe_reply(event, *args, wait_for_result=False, **kwargs):
    """Responde a un evento usando la cola para evitar rate limiting"""
    return await message_queue.add_message(event.reply, *args, wait_for_result=wait_for_result, **kwargs)

async def safe_respond(event, *args, wait_for_result=False, **kwargs):
    """Responde a un evento usando event.respond y la cola para evitar rate limiting"""
    return await message_queue.add_message(event.respond, *args, wait_for_result=wait_for_result, **kwargs)

async def safe_answer(event, *args, wait_for_result=False, **kwargs):
    """Responde a un callback query usando event.answer y la cola para evitar rate limiting"""
    return await message_queue.add_message(event.answer, *args, wait_for_result=wait_for_result, **kwargs)

async def safe_delete(message, *args, wait_for_result=False, **kwargs):
    """Elimina un mensaje usando la cola para evitar rate limiting"""
    return await message_queue.add_message(message.delete, *args, wait_for_result=wait_for_result, **kwargs)

async def safe_send_message(chat_id, *args, wait_for_result=False, **kwargs):
    """Envía un mensaje usando la cola para evitar rate limiting"""
    return await message_queue.add_message(bot.send_message, chat_id, *args, wait_for_result=wait_for_result, **kwargs)

async def safe_send_file(chat_id, *args, wait_for_result=False, **kwargs):
    """Envía un archivo usando la cola para evitar rate limiting"""
    return await message_queue.add_message(bot.send_file, chat_id, *args, wait_for_result=wait_for_result, **kwargs)

async def get_array_donors_online():
    """Obtiene la lista de donantes desde el servidor"""
    headers = {
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }

    try:
        response = requests.get(DONORS_URL, headers=headers, timeout=10)
        if response.status_code == 200:
            try:
                data = response.json()
                if isinstance(data, list):
                    data.sort()
                    return data
                else:
                    error(f"Error getting donors list: data is not a list [{str(data)}]")
                    return []
            except ValueError:
                error(f"Error getting donors list: data is not a json [{response.text}]")
                return []
        else:
            error(f"Error getting donors list: error code [{response.status_code}]")
            return []
    except Exception as e:
        error(f"[DONORS] Error getting donors list: {str(e)}")
        return []

async def print_donors(chat_id):
    """Muestra la lista de donantes"""
    donors = await get_array_donors_online()
    if donors:
        result = ""
        for donor in donors:
            result += f"· {donor}\n"
        await safe_send_message(chat_id, get_text("donors_list", result), parse_mode="HTML")
    else:
        await safe_send_message(chat_id, get_text("error_getting_donors"), parse_mode=PARSE_MODE)

async def handle_list_files(event):
    """Lista los archivos descargados en el servidor"""
    try:
        # Parsear el comando para obtener la categoría
        command_parts = event.raw_text.split()
        category = command_parts[1] if len(command_parts) > 1 else "all"

        # Mapear categorías a directorios
        category_map = {
            "all": list(DOWNLOAD_PATHS.values()),
            "video": [DOWNLOAD_PATHS["video"], DOWNLOAD_PATHS["url_video"]],
            "audio": [DOWNLOAD_PATHS["audio"], DOWNLOAD_PATHS["url_audio"]],
            "photo": [DOWNLOAD_PATHS["photo"]],
            "torrent": [DOWNLOAD_PATHS["torrent"]],
            "ebook": [DOWNLOAD_PATHS["ebook"]]
        }

        directories = category_map.get(category, list(DOWNLOAD_PATHS.values()))

        # Eliminar directorios duplicados (cuando los filtros están desactivados, todos apuntan a /downloads)
        directories = list(set(directories))

        # Recopilar archivos
        files_info = []
        total_size = 0
        seen_files = set()  # Para evitar duplicados por ruta completa

        for directory in directories:
            if not os.path.exists(directory):
                continue

            for filename in os.listdir(directory):
                file_path = os.path.join(directory, filename)

                # Ignorar archivos temporales y ocultos
                if filename.startswith('.') or '_thumb.jpg' in filename:
                    continue

                # Evitar duplicados usando la ruta completa
                if file_path in seen_files:
                    continue
                seen_files.add(file_path)

                try:
                    is_directory = os.path.isdir(file_path)

                    if is_directory:
                        # Es una carpeta
                        file_size = get_directory_size(file_path)
                        icon = "📁"
                        item_type = "folder"
                    else:
                        # Es un archivo
                        file_size = os.path.getsize(file_path)
                        file_ext = os.path.splitext(filename)[1].lower()
                        icon = get_file_icon(file_ext)
                        item_type = "file"

                    files_info.append({
                        "name": filename,
                        "size": file_size,
                        "size_formatted": format_file_size(file_size),
                        "icon": icon,
                        "path": file_path,
                        "type": item_type
                    })
                    total_size += file_size
                except Exception as e:
                    warning(f"[FILE_LIST] Error processing {filename}: {e}")

        # Ordenar alfabéticamente por nombre
        files_info.sort(key=lambda x: x["name"].lower())

        if not files_info:
            await safe_reply(event, get_text("list_empty"), parse_mode=PARSE_MODE)
            return

        # Construir mensajes (partiendo si es necesario)
        # Límite de Telegram: 4096 caracteres, dejamos margen de seguridad
        MAX_MESSAGE_LENGTH = 3800

        total_size_formatted = format_file_size(total_size)
        header = f"📂 **Archivos en el servidor**\n\n"

        # Contar archivos y carpetas
        file_count = sum(1 for item in files_info if item["type"] == "file")
        folder_count = sum(1 for item in files_info if item["type"] == "folder")

        if folder_count > 0:
            footer = f"\n\n{get_text('total_files_folders_space', file_count, folder_count, total_size_formatted)}"
        else:
            footer = f"\n\n{get_text('total_files_space', file_count, total_size_formatted)}"

        messages = []
        current_message = ""
        current_count = 0

        for i, file_info in enumerate(files_info, 1):
            # Truncar nombre si es muy largo
            name = file_info["name"]
            display_name = name
            if len(name) > 40:
                display_name = name[:37] + "..."

            # Crear entrada de archivo o carpeta (sin mostrar la ruta)
            file_entry = f"{i}. {file_info['icon']} `{display_name}`\n   💾 {file_info['size_formatted']}"

            # Calcular longitud del mensaje con header y footer
            test_message = header + current_message + "\n\n" + file_entry + footer

            if len(test_message) > MAX_MESSAGE_LENGTH and current_message:
                # Guardar mensaje actual y empezar uno nuevo
                final_message = header + current_message + footer
                messages.append(final_message)
                current_message = file_entry
                current_count = 0
            else:
                # Agregar al mensaje actual
                if current_message:
                    current_message += "\n\n" + file_entry
                else:
                    current_message = file_entry
                current_count += 1

        # Agregar el último mensaje
        if current_message:
            final_message = header + current_message + footer
            messages.append(final_message)

        # Crear botones de categorías (excluyendo la categoría actual)
        category_buttons = get_category_buttons(exclude_category=category)

        # Enviar mensaje principal con lista de archivos
        for idx, msg in enumerate(messages, 1):
            if len(messages) > 1:
                # Si hay múltiples mensajes, agregar indicador de página
                msg = msg.replace("📂 **Archivos en el servidor**", f"📂 **Archivos en el servidor** (Parte {idx}/{len(messages)})")

            # Solo agregar botones de categorías al último mensaje
            buttons = category_buttons if idx == len(messages) else None
            await safe_reply(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

    except Exception as e:
        error(f"[FILE_LIST] Error listing files: {e}")
        await safe_reply(event, get_text("error_list_files"), parse_mode=PARSE_MODE)

def get_directory_size(directory):
    """Calcula el tamaño total de una carpeta recursivamente"""
    total_size = 0
    try:
        for dirpath, _, filenames in os.walk(directory):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.exists(filepath):
                    total_size += os.path.getsize(filepath)
    except Exception as e:
        warning(f"[FILE_SIZE] Error calculating folder size {directory}: {e}")
    return total_size

def get_unique_filename(directory, filename):
    """
    Genera un nombre de archivo único en el directorio especificado.
    Si el archivo existe, agrega un sufijo (1), (2), etc.
    """
    file_path = os.path.join(directory, filename)

    # Si no existe, retornar el nombre original
    if not os.path.exists(file_path):
        return filename

    # Separar nombre base y extensión
    base_name, extension = os.path.splitext(filename)
    counter = 1

    # Buscar un nombre único
    while True:
        new_filename = f"{base_name} ({counter}){extension}"
        new_path = os.path.join(directory, new_filename)
        if not os.path.exists(new_path):
            return new_filename
        counter += 1

def get_file_icon(file_extension):
    """Determina el icono según la extensión del archivo usando las constantes de config.py"""
    if file_extension in EXTENSIONS_TORRENT:
        return TOR_ICO
    elif file_extension in EXTENSIONS_EBOOK:
        return BOO_ICO
    elif file_extension in EXTENSIONS_VIDEO:
        return VID_ICO
    elif file_extension in EXTENSIONS_AUDIO:
        return AUD_ICO
    elif file_extension in EXTENSIONS_IMAGE:
        return IMG_ICO
    elif file_extension in EXTENSIONS_COMPRESSED:
        return ZIP_ICO
    else:
        return DEF_ICO

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
    try:
        debug(f"[DOWNLOAD] limited_download() called for event.id={event.id}")
        async with download_semaphore:
            await download_media(event)
        debug(f"[DOWNLOAD] limited_download() completed for event.id={event.id}")
    except Exception as e:
        error(f"[DOWNLOAD] ❌ Unhandled exception in limited_download for event.id={event.id}: {e}")
        error(f"[DOWNLOAD] Exception type: {type(e).__name__}")
        import traceback
        error(f"[DOWNLOAD] Traceback: {traceback.format_exc()}")
        # No re-lanzar para evitar que Telethon capture silenciosamente

def create_upload_progress_callback(status_message, file_name):
    """
    Crea un callback de progreso para envíos a Telegram.
    Actualiza el mensaje cada PROGRESS_UPDATE_INTERVAL segundos para evitar anti-spam.
    """
    last_update_time = [0]  # Lista para poder modificar en closure
    message_deleted = [False]  # Flag para detectar si el mensaje fue eliminado

    async def progress_callback(current, total):
        # Si no hay mensaje de estado o fue eliminado, no hacer nada
        if status_message is None or message_deleted[0]:
            return
        try:
            current_time = asyncio.get_event_loop().time()
            # Siempre actualizar si llegamos al 100%, sin importar el intervalo
            is_complete = (current >= total)
            should_update = (current_time - last_update_time[0] >= PROGRESS_UPDATE_INTERVAL) or is_complete

            if should_update:
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
                bar_length = 20
                filled = int(bar_length * percent / 100)
                bar = "█" * filled + "░" * (bar_length - filled)

                message = get_text("uploading_progress", bar, f"{percent:.1f}", f"{size_current}/{size_total}", speed, eta, file_name)

                # Usar edición directa sin cola para actualizaciones de progreso (más rápido)
                try:
                    await status_message.edit(message, parse_mode=PARSE_MODE)
                except Exception as edit_error:
                    # Si el mensaje fue eliminado o es inválido, marcar como eliminado y dejar de intentar
                    error_msg = str(edit_error)
                    if "message ID is invalid" in error_msg or "MESSAGE_ID_INVALID" in error_msg:
                        debug(f"[UPLOAD PROGRESS] Message was deleted, stopping progress updates")
                        message_deleted[0] = True
                    else:
                        # Otros errores (FloodWaitError, mensaje no modificado, etc.) - solo log
                        warning(f"[UPLOAD] Error editing progress message: {edit_error}")
        except Exception as e:
            # Ignorar errores de actualización (FloodWaitError, etc.)
            warning(f"[UPLOAD] Error updating upload progress: {e}")

    return progress_callback

def create_progress_callback(status_message, event, file_name):
    """
    Crea un callback de progreso para descargas desde Telegram.
    Actualiza el mensaje cada PROGRESS_UPDATE_INTERVAL segundos para evitar anti-spam.
    """
    last_update_time = [0]  # Lista para poder modificar en closure
    message_deleted = [False]  # Flag para detectar si el mensaje fue eliminado

    async def progress_callback(current, total):
        # Si no hay mensaje de estado o fue eliminado, no hacer nada
        if status_message is None or message_deleted[0]:
            return

        try:
            current_time = asyncio.get_event_loop().time()
            # Siempre actualizar si llegamos al 100%, sin importar el intervalo
            is_complete = (current >= total)
            should_update = (current_time - last_update_time[0] >= PROGRESS_UPDATE_INTERVAL) or is_complete

            # Log cuando llegamos al 100%
            if is_complete and not hasattr(progress_callback, 'logged_100'):
                debug(f"[DOWNLOAD PROGRESS] Reached 100% ({current}/{total} bytes)")
                progress_callback.logged_100 = True

            if should_update:
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
                bar_length = 20
                filled = int(bar_length * percent / 100)
                bar = "█" * filled + "░" * (bar_length - filled)

                message = get_text("downloading_progress", bar, f"{percent:.1f}", f"{size_current}/{size_total}", speed, eta, file_name)

                # Usar edición directa sin cola para actualizaciones de progreso (más rápido)
                try:
                    # Log cuando actualizamos al 100%
                    if is_complete:
                        debug(f"[DOWNLOAD PROGRESS] Updating message to 100%")

                    await status_message.edit(
                        message,
                        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                        parse_mode=PARSE_MODE
                    )

                    # Log cuando se actualiza exitosamente al 100%
                    if is_complete:
                        debug(f"[DOWNLOAD PROGRESS] ✅ Message updated to 100% successfully")

                except Exception as edit_error:
                    # Si el mensaje fue eliminado o es inválido, marcar como eliminado y dejar de intentar
                    error_msg = str(edit_error)
                    if "message ID is invalid" in error_msg or "MESSAGE_ID_INVALID" in error_msg:
                        debug(f"[DOWNLOAD PROGRESS] Message was deleted, stopping progress updates")
                        message_deleted[0] = True
                    else:
                        # Otros errores (FloodWaitError, mensaje no modificado, etc.) - solo log
                        warning(f"[DOWNLOAD] Error editing progress message: {edit_error}")
        except Exception as e:
            # Ignorar errores de actualización (FloodWaitError, etc.)
            warning(f"[DOWNLOAD] Error updating Telegram progress: {e}")

    return progress_callback

async def download_media(event):
    debug(f"[DOWNLOAD] download_media() called for event.id={event.id}")
    message = event.message
    media = message.document or message.video or message.audio or message.photo
    if not media:
        debug(f"[DOWNLOAD] No media found in message, returning")
        return
    file_name = get_file_name(media)
    debug(f"[DOWNLOAD] File {file_name} - Received, starting download")
    download_path, ico = get_download_path(event)

    # Generar nombre único si el archivo ya existe
    unique_file_name = get_unique_filename(download_path, file_name)
    if unique_file_name != file_name:
        debug(f"[DOWNLOAD] Duplicate file detected. Renaming: {file_name} -> {unique_file_name}")

    # Descargar primero a /tmp para que no aparezca en /list mientras se descarga
    timestamp_ms = int(time.time() * 1000)
    temp_file_path = os.path.join(TEMP_DIR, f"{unique_file_name}_{timestamp_ms}_download")
    final_file_path = os.path.join(download_path, unique_file_name)

    debug(f"[DOWNLOAD] Temporary path: {temp_file_path}")
    debug(f"[DOWNLOAD] Final path: {final_file_path}")

    status_message = await safe_reply(
        event,
        get_text("downloading", ico),
        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
        parse_mode=PARSE_MODE,
        wait_for_result=True
    )

    # Si no se pudo crear el mensaje de estado (timeout en cola), continuar sin él
    if status_message is None:
        warning(f"[DOWNLOAD] Could not create status message for {file_name} - continuing without progress updates")
        progress_callback = None
    else:
        # Crear callback de progreso
        progress_callback = create_progress_callback(status_message, event, file_name)

    # Intentar descargar con reintentos
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            # Descargar a archivo temporal con timeout
            # Timeout dinámico: 10 minutos base + 2 minutos por cada 100MB de archivo
            # Esto previene que descargas se queden colgadas indefinidamente
            # Ejemplos: 100MB=12min, 500MB=20min, 1GB=30min, 2GB=50min
            # Basado en datos reales: descargas normales tardan 2-22min para archivos de 1-2GB
            file_size_mb = message.file.size / (1024 * 1024) if message.file and message.file.size else 100
            download_timeout = 600 + (file_size_mb / 100) * 120  # 10 min base + 2 min por cada 100MB
            debug(f"[DOWNLOAD] Download timeout set to {int(download_timeout)}s ({int(download_timeout/60)}min) for {file_size_mb:.1f}MB file")
            debug(f"[DOWNLOAD] Starting download to: {temp_file_path}")
            debug(f"[DOWNLOAD] Progress callback enabled: {progress_callback is not None}")

            await asyncio.wait_for(
                bot.download_media(message, file=temp_file_path, progress_callback=progress_callback),
                timeout=download_timeout
            )

            debug(f"[DOWNLOAD] ✅ bot.download_media() completed successfully")
            debug(f"[DOWNLOAD] Checking if temp file exists...")

            # Mover archivo de /tmp a carpeta final
            debug(f"[DOWNLOAD] Moving file from temp to final location...")
            debug(f"[DOWNLOAD] Source: {temp_file_path}")
            debug(f"[DOWNLOAD] Destination: {final_file_path}")

            try:
                # Verificar que el archivo temporal existe antes de mover
                if not os.path.exists(temp_file_path):
                    error(f"[DOWNLOAD] ❌ Temporary file not found: {temp_file_path}")
                    raise FileNotFoundError(f"Temporary file not found: {temp_file_path}")

                debug(f"[DOWNLOAD] ✅ Temporary file exists")
                temp_size = os.path.getsize(temp_file_path)
                debug(f"[DOWNLOAD] Temporary file size: {temp_size} bytes ({temp_size / (1024*1024):.2f} MB)")

                # Verificar permisos de lectura
                if not os.access(temp_file_path, os.R_OK):
                    error(f"[DOWNLOAD] ❌ No read permission for temp file: {temp_file_path}")
                    raise PermissionError(f"No read permission for temp file")

                debug(f"[DOWNLOAD] ✅ Temp file is readable")

                # Verificar que el directorio de destino existe
                dest_dir = os.path.dirname(final_file_path)
                if not os.path.exists(dest_dir):
                    error(f"[DOWNLOAD] ❌ Destination directory does not exist: {dest_dir}")
                    raise FileNotFoundError(f"Destination directory not found: {dest_dir}")

                debug(f"[DOWNLOAD] ✅ Destination directory exists: {dest_dir}")

                # Verificar permisos de escritura en destino
                if not os.access(dest_dir, os.W_OK):
                    error(f"[DOWNLOAD] ❌ No write permission for destination directory: {dest_dir}")
                    raise PermissionError(f"No write permission for destination directory")

                debug(f"[DOWNLOAD] ✅ Destination directory is writable")

                # Mover archivo de forma asíncrona para no bloquear el event loop
                # shutil.move() puede tardar mucho con archivos grandes
                debug(f"[DOWNLOAD] Executing shutil.move() asynchronously...")
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, shutil.move, temp_file_path, final_file_path)
                debug(f"[DOWNLOAD] ✅ shutil.move() completed")

                # Verificar que el archivo final existe
                if not os.path.exists(final_file_path):
                    error(f"[DOWNLOAD] ❌ Final file not found after move: {final_file_path}")
                    raise FileNotFoundError(f"Final file not found after move: {final_file_path}")

                debug(f"[DOWNLOAD] ✅ Final file exists")
                final_size = os.path.getsize(final_file_path)
                debug(f"[DOWNLOAD] Final file size: {final_size} bytes ({final_size / (1024*1024):.2f} MB)")

                # Verificar que los tamaños coinciden
                if temp_size != final_size:
                    warning(f"[DOWNLOAD] ⚠️ File size mismatch! Temp: {temp_size}, Final: {final_size}")
                else:
                    debug(f"[DOWNLOAD] ✅ File sizes match")

            except Exception as move_error:
                error(f"[DOWNLOAD] ❌ Error moving file: {move_error}")
                error(f"[DOWNLOAD] Error type: {type(move_error).__name__}")
                import traceback
                error(f"[DOWNLOAD] Traceback: {traceback.format_exc()}")
                # Re-lanzar la excepción para que se maneje en el except general
                raise

            # Descarga exitosa - borrar mensaje de progreso
            debug(f"[DOWNLOAD] Deleting progress message...")
            if status_message:
                try:
                    debug(f"[DOWNLOAD] Calling safe_delete with wait_for_result=True...")
                    delete_result = await safe_delete(status_message, wait_for_result=True)
                    debug(f"[DOWNLOAD] safe_delete returned: {delete_result}")
                    debug(f"[DOWNLOAD] ✅ Progress message deleted successfully")
                except asyncio.TimeoutError:
                    warning(f"[DOWNLOAD] ⚠️ Timeout deleting progress message (waited 5 minutes)")
                except Exception as delete_error:
                    warning(f"[DOWNLOAD] ⚠️ Could not delete progress message: {delete_error}")
                    warning(f"[DOWNLOAD] Delete error type: {type(delete_error).__name__}")
                    # Continuar aunque falle el borrado
            else:
                debug(f"[DOWNLOAD] No status message to delete")

            # Mostrar información detallada del archivo descargado (sin botones de acción)
            debug(f"[DOWNLOAD] Calling handle_success for: {final_file_path}")
            try:
                await handle_success(event, final_file_path, show_action_buttons=False)
                debug(f"[DOWNLOAD] ✅ handle_success completed")
            except Exception as success_error:
                error(f"[DOWNLOAD] ❌ Error in handle_success: {success_error}")
                error(f"[DOWNLOAD] Success error type: {type(success_error).__name__}")
                import traceback
                error(f"[DOWNLOAD] Traceback: {traceback.format_exc()}")
                # Re-lanzar para que se capture en el except general
                raise

            debug(f"[DOWNLOAD] ✅ File {file_name} - Downloaded successfully")

            # Salir del bucle si la descarga fue exitosa
            debug(f"[DOWNLOAD] Breaking from retry loop")
            break

        except asyncio.CancelledError:
            if status_message:
                await safe_edit(status_message, get_text("cancelled"), buttons=None, parse_mode=PARSE_MODE)
            # Limpiar archivo temporal si existe
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
                debug(f"[DOWNLOAD] Temporary file deleted after cancellation: {temp_file_path}")
            # Limpiar archivo final si se movió
            if os.path.exists(final_file_path):
                os.remove(final_file_path)
                debug(f"[DOWNLOAD] Final file deleted after cancellation: {final_file_path}")
            debug(f"[DOWNLOAD] File {file_name} - Cancelled")
            raise

        except asyncio.TimeoutError:
            # Timeout específico de asyncio.wait_for
            error(f"[DOWNLOAD] Download timeout after {int(download_timeout)}s for {file_name}")

            # Eliminar archivo parcialmente descargado (temporal)
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    debug(f"[DOWNLOAD] Partial temp file deleted after timeout: {temp_file_path}")
                except Exception as cleanup_error:
                    warning(f"[DOWNLOAD] Error deleting {temp_file_path}: {cleanup_error}")

            # Si aún quedan intentos, reintentar
            if attempt < MAX_DOWNLOAD_RETRIES:
                debug(f"[DOWNLOAD] Retrying download of {file_name} (attempt {attempt + 1} of {MAX_DOWNLOAD_RETRIES}) after timeout")
                if status_message:
                    try:
                        await safe_edit(
                            status_message,
                            get_text("warning_retrying_download", attempt + 1, MAX_DOWNLOAD_RETRIES),
                            buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                            parse_mode=PARSE_MODE
                        )
                    except Exception as msg_error:
                        error(f"[DOWNLOAD] Error updating status message: {msg_error}")

                # Esperar antes de reintentar
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                # Último intento fallido
                error(f"[DOWNLOAD] Download failed after {MAX_DOWNLOAD_RETRIES} attempts due to timeout")
                if status_message:
                    try:
                        await safe_edit(
                            status_message,
                            get_text("error_telegram_timeout_user", file_name),
                            buttons=None,
                            parse_mode=PARSE_MODE
                        )
                    except Exception as msg_error:
                        error(f"[DOWNLOAD] Error updating status message: {msg_error}")

        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__

            # Loguear SIEMPRE el error antes de decidir qué hacer
            error(f"[DOWNLOAD] ❌ Exception caught in download loop: {error_type} - {error_msg}")
            import traceback
            error(f"[DOWNLOAD] Traceback: {traceback.format_exc()}")

            # Errores que deben reintentar: TimeoutError, ValueError, errores de red, errores internos de Telegram
            should_retry = (
                isinstance(e, (TimeoutError, ValueError)) or
                "timeout" in error_msg.lower() or
                "unsuccessful" in error_msg.lower() or
                "internal" in error_msg.lower() or
                "getfilerequest" in error_msg.lower() or
                "too slow" in error_msg.lower() or
                "connection" in error_msg.lower() or
                "network" in error_msg.lower()
            )

            debug(f"[DOWNLOAD] should_retry={should_retry} for error type {error_type}")

            if should_retry:
                # Eliminar archivo parcialmente descargado (temporal)
                if os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                        debug(f"[DOWNLOAD] Partial temp file deleted after error: {temp_file_path}")
                    except Exception as cleanup_error:
                        warning(f"[DOWNLOAD] Error deleting {temp_file_path}: {cleanup_error}")

                # Si aún quedan intentos, reintentar
                if attempt < MAX_DOWNLOAD_RETRIES:
                    debug(f"[DOWNLOAD] Retrying download of {file_name} (attempt {attempt + 1} of {MAX_DOWNLOAD_RETRIES}) after error: {error_msg}")
                    if status_message:
                        try:
                            await safe_edit(
                                status_message,
                                get_text("warning_retrying_download", attempt + 1, MAX_DOWNLOAD_RETRIES),
                                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                                parse_mode=PARSE_MODE
                            )
                        except Exception as msg_error:
                            error(f"[DOWNLOAD] Error updating status message: {msg_error}")

                    # Esperar antes de reintentar
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                else:
                    # Último intento fallido
                    error(f"[DOWNLOAD] Telegram timeout while downloading {file_name}: {error_msg}")
                    if status_message:
                        try:
                            await safe_edit(
                                status_message,
                                get_text("error_telegram_timeout_user", file_name),
                                buttons=None,
                                parse_mode=PARSE_MODE
                            )
                        except Exception as msg_error:
                            error(f"[DOWNLOAD] Error updating status message: {msg_error}")
            else:
                # Error que NO debe reintentar - loguear y re-lanzar
                error(f"[DOWNLOAD] ❌ Non-retryable error, re-raising: {error_type} - {error_msg}")
                raise

    # Si llegamos aquí, el bucle terminó sin break (todos los intentos fallaron)
    debug(f"[DOWNLOAD] Exited retry loop for {file_name}")

    # Limpiar tareas activas
    active_tasks.pop(event.id, None)
    debug(f"[DOWNLOAD] Cleaned up active task for event.id={event.id}")

def extract_file(file_path, extract_to):
    try:
        filename = os.path.basename(file_path)
        if file_path.lower().endswith('.zip'):
            debug(f"[EXTRACT] Extracting ZIP file: {filename}")
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                file_count = len(zip_ref.namelist())
                debug(f"[EXTRACT] ZIP contains {file_count} files")
                zip_ref.extractall(extract_to)
            debug(f"[EXTRACT] ZIP extraction completed: {filename}")
        elif any(file_path.lower().endswith(ext) for ext in ['.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz']):
            debug(f"[EXTRACT] Extracting TAR file: {filename}")
            with tarfile.open(file_path, 'r:*') as tar_ref:
                file_count = len(tar_ref.getmembers())
                debug(f"[EXTRACT] TAR contains {file_count} files")
                tar_ref.extractall(extract_to)
            debug(f"[EXTRACT] TAR extraction completed: {filename}")
        elif rarfile.is_rarfile(file_path):
            try:
                debug(f"[EXTRACT] Extracting RAR file: {filename}")
                with rarfile.RarFile(file_path) as rar_ref:
                    file_count = len(rar_ref.namelist())
                    debug(f"[EXTRACT] RAR contains {file_count} files")
                    rar_ref.extractall(extract_to)
                debug(f"[EXTRACT] RAR extraction completed: {filename}")
            except rarfile.RarCannotExec as e:
                error(f"[EXTRACT] RAR extraction tool not found: {e}")
                return False
            except Exception as e:
                msg = str(e).lower()
                if ("need to start from first volume" in msg or
                    "need first volume" in msg or
                    "missing volume" in msg or
                    "unexpected end of archive" in msg):
                    warning(f"[EXTRACT] File {file_path} - Missing RAR parts")
                    # Eliminar carpeta vacía creada
                    if os.path.exists(extract_to):
                        try:
                            shutil.rmtree(extract_to)
                            debug(f"[EXTRACT] Deleted empty folder after missing parts error: {extract_to}")
                        except Exception as cleanup_error:
                            warning(f"[EXTRACT] Error deleting empty folder {extract_to}: {cleanup_error}")
                    return "missing_parts"
                elif ("failed the read enough data" in msg):
                    # Este error puede ocurrir en archivos RAR válidos cuando 7z intenta leer más datos
                    # de los disponibles al final del archivo. Verificamos si la extracción fue completa.
                    if os.path.exists(extract_to) and os.listdir(extract_to):
                        # Verificar si hay archivos de 0 bytes (señal de extracción incompleta)
                        has_zero_byte_files = False
                        zero_byte_count = 0
                        total_files = 0

                        for root, dirs, files in os.walk(extract_to):
                            for filename in files:
                                total_files += 1
                                file_full_path = os.path.join(root, filename)
                                if os.path.getsize(file_full_path) == 0:
                                    has_zero_byte_files = True
                                    zero_byte_count += 1
                                    debug(f"[EXTRACT] Found zero-byte file: {file_full_path}")

                        if has_zero_byte_files:
                            warning(f"[EXTRACT] File {file_path} - Partial extraction: {zero_byte_count}/{total_files} files are empty or incomplete")
                            # Borrar la carpeta parcialmente extraída
                            try:
                                shutil.rmtree(extract_to)
                                debug(f"[EXTRACT] Deleted partially extracted folder: {extract_to}")
                            except Exception as cleanup_error:
                                warning(f"[EXTRACT] Error deleting partially extracted folder {extract_to}: {cleanup_error}")
                            return "partial"
                        else:
                            debug(f"[EXTRACT] File {file_path} - Extraction completed despite read warning")
                            return True
                    else:
                        error(f"[EXTRACT] File {file_path} - Corrupted or incomplete RAR file: {e}")
                        # Eliminar carpeta vacía creada
                        if os.path.exists(extract_to):
                            try:
                                shutil.rmtree(extract_to)
                                debug(f"[EXTRACT] Deleted empty folder after corruption error: {extract_to}")
                            except Exception as cleanup_error:
                                warning(f"[EXTRACT] Error deleting empty folder {extract_to}: {cleanup_error}")
                        return "corrupted"
                elif ("corrupt" in msg or
                      "damaged" in msg or
                      "bad rar file" in msg or
                      "crc failed" in msg or
                      "checksum error" in msg):
                    error(f"[EXTRACT] File {file_path} - Corrupted or incomplete RAR file: {e}")
                    # Eliminar carpeta vacía creada
                    if os.path.exists(extract_to):
                        try:
                            shutil.rmtree(extract_to)
                            debug(f"[EXTRACT] Deleted empty folder after corruption error: {extract_to}")
                        except Exception as cleanup_error:
                            warning(f"[EXTRACT] Error deleting empty folder {extract_to}: {cleanup_error}")
                    return "corrupted"
                else:
                    raise
        else:
            return False
        return True

    except Exception as e:
        error(f"[EXTRACT] Error extracting file {file_path}: {e}")
        if os.path.exists(extract_to):
            try:
                shutil.rmtree(extract_to)
                debug(f"[EXTRACT] Deleted folder: {extract_to}")
            except Exception as cleanup_error:
                warning(f"[EXTRACT] Error deleting folder {extract_to}: {cleanup_error}")
        return False

def get_extraction_message_and_buttons(extract_result, filename, extracted_path, file_path, file_id=None, from_manage=False):
    """
    Genera mensajes y botones consistentes para los resultados de extracción.

    Args:
        extract_result: Resultado de extract_file() (True, False, "missing_parts", "corrupted")
        filename: Nombre del archivo comprimido
        extracted_path: Ruta donde se extrajo (o intentó extraer)
        file_path: Ruta completa del archivo comprimido
        file_id: ID del archivo (solo para flujo de /manage)
        from_manage: True si viene del flujo de /manage, False si es automático

    Returns:
        tuple: (mensaje, botones)
    """
    if extract_result == True:
        # Extracción exitosa
        msg = f"✅ **{get_text('extraction_success_title')}**\n\n"
        msg += f"📄 **{get_text('extraction_file')}:** `{filename}`\n"
        msg += f"📁 **{get_text('extraction_folder')}:** `{os.path.basename(extracted_path)}`\n\n"
        msg += get_text('extraction_ask_delete')

        if from_manage:
            # Botones para flujo de /manage
            buttons = [
                [
                    Button.inline(f"🗑️ {get_text('extraction_delete_compressed')}", data=f"delcompressed:{file_id}"),
                    Button.inline(f"💾 {get_text('extraction_keep_compressed')}", data=f"keepcompressed:{file_id}")
                ],
                [
                    Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
                    Button.inline(get_text("button_close"), data="close")
                ]
            ]
        else:
            # Botones para flujo automático
            buttons = [
                [
                    Button.inline(get_text("button_delete"), data=f"del:{file_path}"),
                    Button.inline(get_text("button_keep"), data=f"keep:{file_path}")
                ]
            ]

    elif extract_result == "missing_parts":
        # Faltan partes del archivo RAR
        msg = f"❌ **{get_text('extraction_missing_parts_title')}**\n\n"
        msg += f"📄 `{filename}`\n\n"
        msg += get_text('extraction_missing_parts_desc')

        if from_manage:
            buttons = [[
                Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
                Button.inline(get_text("button_close"), data="close")
            ]]
        else:
            buttons = None

    elif extract_result == "partial":
        # Extracción parcial - se borra automáticamente
        msg = f"❌ **{get_text('extraction_partial_title')}**\n\n"
        msg += f"📄 `{filename}`\n\n"
        msg += get_text('extraction_partial_desc')

        if from_manage:
            buttons = [[
                Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
                Button.inline(get_text("button_close"), data="close")
            ]]
        else:
            buttons = None

    elif extract_result == "corrupted":
        # Archivo corrupto o incompleto
        msg = f"❌ **{get_text('extraction_corrupted_title')}**\n\n"
        msg += f"📄 `{filename}`\n\n"
        msg += get_text('extraction_corrupted_desc')

        if from_manage:
            buttons = [[
                Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
                Button.inline(get_text("button_close"), data="close")
            ]]
        else:
            buttons = None

    else:
        # Error general en la extracción
        msg = f"❌ **{get_text('extraction_error_title')}**\n\n"
        msg += f"📄 `{filename}`\n\n"
        msg += get_text('extraction_error_desc')

        if from_manage:
            buttons = [[
                Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
                Button.inline(get_text("button_close"), data="close")
            ]]
        else:
            buttons = None

    return msg, buttons

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

def get_available_categories():
    """Retorna las categorías disponibles según los filtros activos"""
    categories = []

    # Siempre está disponible la carpeta principal
    categories.append(("all", "📦 Todos"))

    # Agregar categorías según filtros activos
    if FILTER_VIDEO or FILTER_URL_VIDEO:
        categories.append(("video", f"{VID_ICO} Videos"))
    if FILTER_AUDIO or FILTER_URL_AUDIO:
        categories.append(("audio", f"{AUD_ICO} Audios"))
    if FILTER_PHOTO:
        categories.append(("photo", f"{IMG_ICO} Fotos"))
    if FILTER_TORRENT:
        categories.append(("torrent", f"{TOR_ICO} Torrents"))
    if FILTER_EBOOK:
        categories.append(("ebook", f"{BOO_ICO} Ebooks"))

    return categories

def get_category_buttons(exclude_category=None):
    """Retorna los botones de categorías, opcionalmente excluyendo una categoría"""
    categories = get_available_categories()

    buttons = []
    row = []
    for cat_id, cat_name in categories:
        # Excluir la categoría actual si se especifica
        if exclude_category and cat_id == exclude_category:
            continue

        row.append(Button.inline(cat_name, data=f"listcat:{cat_id}"))
        if len(row) == 3:
            buttons.append(row)
            row = []

    # Agregar última fila si quedaron botones
    if row:
        buttons.append(row)

    # Agregar botón de cerrar en fila separada
    buttons.append([Button.inline("❌ Cerrar", data="close")])

    return buttons

@bot.on(events.NewMessage(pattern=r"/(start|donate|version|donors|list|manage)"))
async def handle_start(event):
    # Borrar el comando del usuario para mantener el chat limpio
    try:
        await event.delete()
    except:
        pass

    if not is_admin(event.sender_id):
        debug(f"[AUTH] User {event.sender_id} is not an admin and tried to use the bot")
        response = get_text("user_not_admin")
        await safe_send_message(event.chat_id, response, parse_mode=PARSE_MODE)
    elif event.raw_text == "/start":
        response = get_text("welcome_message")
        await safe_send_message(event.chat_id, response, parse_mode=PARSE_MODE)
    elif event.raw_text == "/donate":
        response = get_text("donate")
        await safe_send_message(event.chat_id, response, parse_mode=PARSE_MODE)
    elif event.raw_text == "/version":
        response = get_text("version", VERSION)
        await safe_send_message(event.chat_id, response, parse_mode=PARSE_MODE)
    elif event.raw_text == "/donors":
        await print_donors(event.chat_id)
    elif event.raw_text == "/list":
        debug(f"[LIST] /list command received")

        # Mostrar menú de categorías
        buttons = get_category_buttons()
        debug(f"[LIST] Category buttons: {buttons}")
        msg = get_text("list_select_category")
        await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE)
        debug(f"[LIST] Menu sent")
    elif event.raw_text == "/manage":
        # Borrar el comando del usuario
        try:
            await event.delete()
        except:
            pass

        # Mostrar menú de categorías para gestionar
        # Crear botones de categorías pero con callback "managecat:" en lugar de "listcat:"
        categories = get_available_categories()
        buttons = []
        row = []
        for cat_id, cat_name in categories:
            row.append(Button.inline(cat_name, data=f"managecat:{cat_id}"))
            if len(row) == 3:
                buttons.append(row)
                row = []

        # Agregar última fila si quedaron botones
        if row:
            buttons.append(row)

        # Agregar botón de cerrar
        buttons.append([Button.inline("❌ Cerrar", data="close")])

        msg = get_text("manage_select_category")
        await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(data=lambda data: data.startswith(b"cancel:")))
async def cancel_download(event):
    if await check_admin_and_warn(event):
        return

    msg_id = int(event.data.decode().split(":")[1])
    task = active_tasks.get(msg_id)

    # Verificar si es una descarga de playlist en progreso
    playlist_info = playlist_downloads.get(msg_id)
    has_partial_playlist = False
    partial_count = 0
    partial_total = 0

    if playlist_info and playlist_info.get("is_full_playlist"):
        total_videos = playlist_info.get("total_videos", 0)
        final_output_dir = playlist_info.get("final_output_dir")

        debug(f"[CANCEL] Playlist download cancelled. Searching for completed files in {TEMP_DIR}")

        # Buscar TODOS los archivos en /tmp
        all_temp_files = []
        try:
            if os.path.exists(TEMP_DIR):
                all_temp_files = [os.path.join(TEMP_DIR, f) for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
                debug(f"[CANCEL] Found {len(all_temp_files)} total files in /tmp")
                for f in all_temp_files:
                    debug(f"[CANCEL]   - {os.path.basename(f)}")
        except Exception as e:
            error(f"[CANCEL] Error listing /tmp directory: {e}")

        # Filtrar archivos completos de vídeo/audio (no parciales de yt-dlp)
        # Archivos parciales de yt-dlp tienen extensiones como .f137.mp4, .f140.m4a, .part
        # Archivos completos tienen extensiones normales: .mp4, .webm, .mkv, .m4a, .mp3
        completed_files = []
        for f in all_temp_files:
            basename = os.path.basename(f)
            # Ignorar archivos parciales
            if '.part' in basename or '.f' in basename.split('.')[-2] if len(basename.split('.')) > 2 else False:
                debug(f"[CANCEL] Skipping partial file: {basename}")
                continue
            # Solo archivos de vídeo/audio completos
            if basename.endswith(('.mp4', '.webm', '.mkv', '.m4a', '.mp3', '.opus')):
                completed_files.append(f)
                debug(f"[CANCEL] Found completed file: {basename}")

        if len(completed_files) > 0:
            debug(f"[CANCEL] Moving {len(completed_files)} completed files to final location...")

            moved_count = 0
            for temp_file_path in completed_files:
                try:
                    filename = os.path.basename(temp_file_path)
                    final_file_path = os.path.join(final_output_dir, filename)

                    shutil.move(temp_file_path, final_file_path)
                    moved_count += 1
                    debug(f"[CANCEL] ✅ Moved: {filename}")
                except Exception as e:
                    error(f"[CANCEL] Error moving file {temp_file_path}: {e}")

            if moved_count > 0:
                has_partial_playlist = True
                partial_count = moved_count
                partial_total = total_videos
                debug(f"[CANCEL] Successfully moved {moved_count} files before cancelling")
        else:
            debug(f"[CANCEL] No completed files found to move")

        # Limpiar información de playlist
        playlist_downloads.pop(msg_id, None)

    # Terminar el proceso
    if isinstance(task, asyncio.subprocess.Process):
        task.terminate()
        await safe_answer(event, get_text("cancelling"))
    elif isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        await safe_answer(event, get_text("cancelling"))
    else:
        # Ya fue cancelada o terminada, borrar el mensaje
        await safe_delete(event)
        return

    # Esperar un momento para que el proceso termine
    await asyncio.sleep(0.5)

    # Mostrar mensaje apropiado según si hay archivos parciales
    if has_partial_playlist and partial_count > 0:
        message = get_text("cancelled_playlist_partial", partial_count, partial_total)
        await safe_edit(event, message, buttons=None, parse_mode=PARSE_MODE)
    # Si no hay archivos parciales, el mensaje normal de cancelación se mostrará en handle_cancel

async def is_direct_download_url(url):
    """
    Detecta si una URL es un enlace directo a un archivo descargable.
    Retorna (is_direct, filename, content_type, download_path, icon) donde:
    - is_direct: True si es descarga directa
    - filename: Nombre del archivo extraído de la URL
    - content_type: Tipo de contenido detectado
    - download_path: Ruta de descarga según el tipo
    - icon: Icono según el tipo de archivo
    """
    try:
        # Combinar todas las extensiones conocidas
        all_extensions = (
            list(EXTENSIONS_VIDEO) +
            list(EXTENSIONS_AUDIO) +
            list(EXTENSIONS_IMAGE) +
            list(EXTENSIONS_TORRENT) +
            list(EXTENSIONS_EBOOK)
        )

        # Extraer path de la URL (sin query params)
        parsed = urlparse(url)
        path = unquote(parsed.path)

        # Verificar si termina con una extensión conocida
        path_lower = path.lower()
        for ext in all_extensions:
            if path_lower.endswith(ext):
                # Extraer nombre del archivo
                filename = os.path.basename(path)

                # Determinar tipo de contenido, ruta de descarga e icono (igual que get_download_path)
                if ext in EXTENSIONS_TORRENT:
                    content_type = 'torrent'
                    download_path = DOWNLOAD_PATHS["torrent"]
                    icon = TOR_ICO
                elif ext in EXTENSIONS_EBOOK:
                    content_type = 'ebook'
                    download_path = DOWNLOAD_PATHS["ebook"]
                    icon = BOO_ICO
                elif ext in EXTENSIONS_VIDEO:
                    content_type = 'video'
                    download_path = DOWNLOAD_PATHS["video"]
                    icon = VID_ICO
                elif ext in EXTENSIONS_AUDIO:
                    content_type = 'audio'
                    download_path = DOWNLOAD_PATHS["audio"]
                    icon = AUD_ICO
                elif ext in EXTENSIONS_IMAGE:
                    content_type = 'image'
                    download_path = DOWNLOAD_PATHS["photo"]
                    icon = IMG_ICO
                else:
                    # Cualquier otra extensión va a DOWNLOAD_PATH
                    content_type = 'file'
                    download_path = DOWNLOAD_PATH
                    icon = DEF_ICO

                debug(f"[DIRECT_DOWNLOAD] Detected direct download: {filename} (type: {content_type}) -> {download_path}")
                return True, filename, content_type, download_path, icon

        # Si no es directo, intentar HEAD request para verificar Content-Type
        try:
            response = requests.head(url, allow_redirects=True, timeout=5)
            content_type_header = response.headers.get('Content-Type', '').lower()
            content_disposition = response.headers.get('Content-Disposition', '')

            # Si tiene Content-Disposition con filename, es descarga directa
            if 'attachment' in content_disposition or 'filename=' in content_disposition:
                # Extraer filename del header
                filename_match = re.search(r'filename[^;=\n]*=(([\'"]).*?\2|[^;\n]*)', content_disposition)
                if filename_match:
                    filename = filename_match.group(1).strip('\'"')
                else:
                    filename = os.path.basename(path) or 'download'

                # Determinar tipo por Content-Type, ruta de descarga e icono
                if 'application/x-bittorrent' in content_type_header:
                    content_type = 'torrent'
                    download_path = DOWNLOAD_PATHS["torrent"]
                    icon = TOR_ICO
                elif 'video' in content_type_header:
                    content_type = 'video'
                    download_path = DOWNLOAD_PATHS["video"]
                    icon = VID_ICO
                elif 'audio' in content_type_header:
                    content_type = 'audio'
                    download_path = DOWNLOAD_PATHS["audio"]
                    icon = AUD_ICO
                elif 'image' in content_type_header:
                    content_type = 'image'
                    download_path = DOWNLOAD_PATHS["photo"]
                    icon = IMG_ICO
                else:
                    # Cualquier otro tipo va a DOWNLOAD_PATH
                    content_type = 'file'
                    download_path = DOWNLOAD_PATH
                    icon = DEF_ICO

                debug(f"[DIRECT_DOWNLOAD] Detected via headers: {filename} (type: {content_type}) -> {download_path}")
                return True, filename, content_type, download_path, icon
        except:
            pass

        return False, None, None, None, None

    except Exception as e:
        debug(f"[DIRECT_DOWNLOAD] Error detecting direct download: {e}")
        return False, None, None, None, None

def calculate_ytdlp_sleep_interval(playlist_count):
    """
    Calcula automáticamente el delay entre descargas de vídeos para evitar rate-limiting de YouTube.

    YouTube puede bloquear temporalmente (hasta 1 hora) si se hacen demasiadas peticiones seguidas.
    Esta función ajusta el delay basándose en el número de vídeos en la playlist.

    Args:
        playlist_count: Número de vídeos en la playlist (1 para vídeos individuales)

    Returns:
        int: Segundos de delay entre descargas

    Estrategia:
        - 1 vídeo: 0s (sin delay)
        - 2-10 vídeos: 1s (delay mínimo)
        - 11-50 vídeos: 2s (delay bajo)
        - 51-100 vídeos: 5s (delay moderado-bajo)
        - 101-200 vídeos: 8s (delay moderado)
        - 201-300 vídeos: 12s (delay moderado-alto)
        - 301-500 vídeos: 18s (delay alto)
        - >500 vídeos: 25s (delay muy alto)
    """
    if playlist_count <= 1:
        return 0
    elif playlist_count <= 10:
        return 1
    elif playlist_count <= 50:
        return 2
    elif playlist_count <= 100:
        return 5
    elif playlist_count <= 200:
        return 8
    elif playlist_count <= 300:
        return 12
    elif playlist_count <= 500:
        return 18
    else:
        return 25

async def detect_playlist(url):
    """Detecta si una URL es una playlist y retorna (is_playlist, playlist_count, playlist_title)"""
    try:
        cmd = ["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", url]

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
                debug(f"[YT-DLP] Playlist detect stderr: {line}")

        if proc.returncode == 0:
            lines = stdout.decode().splitlines()

            if len(lines) > 1:
                # Múltiples líneas JSON = playlist
                debug(f"[YT-DLP] Playlist detected with {len(lines)} items")

                # Intentar obtener el título de la playlist del primer item
                try:
                    first_item = json.loads(lines[0])
                    playlist_title = first_item.get("playlist_title", first_item.get("playlist", "Playlist"))
                except:
                    playlist_title = "Playlist"

                return True, len(lines), playlist_title
            elif len(lines) == 1:
                # Una sola línea = video individual
                debug(f"[YT-DLP] Single video detected")
                return False, 1, None
        else:
            warning(f"[YT-DLP] Playlist detect failed with code {proc.returncode}")

        return False, 1, None
    except Exception as e:
        error(f"[YT-DLP] Error detecting playlist: {e}")
        return False, 1, None

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
                debug(f"[YT-DLP] Detect stderr: {line}")

        if proc.returncode == 0:
            lines = stdout.decode().splitlines()

            # Analizar primer item para detectar tipo
            if lines:
                data = json.loads(lines[0])
                vcodec = data.get("vcodec")
                acodec = data.get("acodec")
                ext = data.get("ext", "").lower()

                debug(f"[YT-DLP] Content type detected - vcodec: {vcodec}, acodec: {acodec}, ext: {ext}")

                # Detectar tipo de contenido
                if vcodec and vcodec != "none":
                    return "video"  # Tiene video
                elif acodec and acodec != "none":
                    return "audio"  # Solo audio
                elif ext in ["jpg", "jpeg", "png", "gif", "webp", "bmp"]:
                    return "image"  # Solo imagen
            else:
                warning("[YT-DLP] Detect: No JSON received in stdout")
        else:
            warning(f"[YT-DLP] Detect: Failed with code {proc.returncode}")

        return "unknown"
    except Exception as e:
        warning(f"[YT-DLP] Error detecting content type: {e}")
        return "unknown"

@bot.on(events.NewMessage(pattern=r'https?://[^\s]+'))
async def handle_url_link(event):
    if await check_admin_and_warn(event):
        return

    url = event.raw_text.strip()
    url_id = str(event.id)
    # Almacenar URL con información de playlist (se actualizará si es playlist)
    pending_urls[url_id] = {"url": url, "playlist_count": 1}

    # Mostrar mensaje de análisis
    analyzing_msg = await safe_reply(event, get_text("analyzing_url"), wait_for_result=True, parse_mode=PARSE_MODE)

    # Si no se pudo crear el mensaje (timeout en cola), enviar uno nuevo sin esperar
    if analyzing_msg is None:
        analyzing_msg = await safe_reply(event, get_text("analyzing_url"), wait_for_result=False, parse_mode=PARSE_MODE)

    # Primero detectar si es descarga directa
    is_direct, filename, direct_type, download_path, icon = await is_direct_download_url(url)

    if is_direct:
        # Es descarga directa - descargar inmediatamente
        debug(f"[DIRECT_DOWNLOAD] Direct download detected: {filename} (type: {direct_type}) -> {download_path}")
        pending_urls.pop(url_id, None)

        # Editar mensaje de análisis
        if analyzing_msg:
            status_message = await safe_edit(
                analyzing_msg,
                get_text("downloading", icon),
                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                parse_mode=PARSE_MODE,
                wait_for_result=True
            )
        else:
            status_message = await safe_reply(
                event,
                get_text("downloading", icon),
                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                parse_mode=PARSE_MODE,
                wait_for_result=False
            )

        # Iniciar descarga directa
        task = asyncio.create_task(run_direct_download(event, url, filename, status_message, download_path, icon, direct_type))
        active_tasks[event.id] = task
        return

    # No es descarga directa - detectar si es playlist
    is_playlist, playlist_count, playlist_title = await detect_playlist(url)

    if is_playlist:
        debug(f"[PLAYLIST] Detected playlist with {playlist_count} videos: {playlist_title}")
        # Almacenar el playlist_count para usarlo después
        pending_urls[url_id]["playlist_count"] = playlist_count

        # Preguntar al usuario si quiere descargar toda la playlist o solo el primero
        buttons = [
            [Button.inline(get_text("button_download_full_playlist"), data=f"playlist_full:{url_id}")],
            [Button.inline(get_text("button_download_first_only"), data=f"playlist_first:{url_id}")],
            [Button.inline(get_text("button_cancel"), data=f"simplecancel:{url_id}")]
        ]

        message = get_text("playlist_detected", playlist_title, playlist_count)

        if analyzing_msg:
            await safe_edit(analyzing_msg, message, buttons=buttons, parse_mode=PARSE_MODE)
        else:
            await safe_reply(event, message, buttons=buttons, parse_mode=PARSE_MODE)
        return

    # No es descarga directa ni playlist - usar yt-dlp para detectar tipo de contenido
    content_type = await detect_content_type(url)

    # Si AUTO_DOWNLOAD_FORMAT está configurado, descargar automáticamente sin preguntar
    if AUTO_DOWNLOAD_FORMAT in ["VIDEO", "AUDIO"]:
        url_data = pending_urls.pop(url_id, None)
        playlist_count = url_data.get("playlist_count", 1) if url_data else 1
        is_audio = AUTO_DOWNLOAD_FORMAT == "AUDIO"

        format_flag = "bestaudio" if is_audio else "bv*+ba/best"
        final_output_dir = DOWNLOAD_PATHS["url_audio"] if is_audio else DOWNLOAD_PATHS["url_video"]

        # Editar mensaje de análisis o crear uno nuevo si no existe
        if analyzing_msg:
            status_message = await safe_edit(
                analyzing_msg,
                get_text("downloading", AUD_ICO if is_audio else VID_ICO),
                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                parse_mode=PARSE_MODE,
                wait_for_result=True
            )
        else:
            status_message = await safe_reply(
                event,
                get_text("downloading", AUD_ICO if is_audio else VID_ICO),
                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                parse_mode=PARSE_MODE,
                wait_for_result=False
            )

        # Usar timestamp para evitar sobrescribir archivos durante la descarga
        timestamp = int(time.time() * 1000)  # Timestamp en milisegundos
        # Incluir índice de playlist en el template para evitar sobrescrituras
        temp_template = f"%(playlist_index)s-%(title).200s_temp{timestamp}.%(ext)s"

        cmd = [
            "yt-dlp",
            "-f", format_flag,
            "--restrict-filenames",
            "--newline",
            "--progress",  # Forzar mostrar progreso
            "-o", os.path.join(TEMP_DIR, temp_template),  # Descargar a /tmp
            url
        ]

        # Calcular delay automático basado en el número de vídeos
        sleep_interval = calculate_ytdlp_sleep_interval(playlist_count)

        if sleep_interval > 0:
            cmd.extend(["--sleep-interval", str(sleep_interval)])
            debug(f"[URL_DOWNLOAD] Auto-calculated sleep interval: {sleep_interval}s for {playlist_count} video(s)")

        if is_audio:
            cmd.extend(["--extract-audio", "--audio-format", "mp3"])

        task = asyncio.create_task(run_url_download(event, cmd, status_message, final_output_dir, is_full_playlist=False, total_videos=playlist_count))
        active_tasks[event.id] = task
        return

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

    # Editar mensaje de análisis o enviar uno nuevo si no existe
    if analyzing_msg:
        await safe_edit(analyzing_msg, message, buttons=buttons, parse_mode=PARSE_MODE)
    else:
        await safe_reply(event, message, buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"simplecancel:(.+)"))
async def cancel_simple(event):
    if await check_admin_and_warn(event):
        return

    url_id = event.pattern_match.group(1).decode()
    pending_urls.pop(url_id, None)
    debug("[URL_DOWNLOAD] URL download cancelled")
    await safe_delete(event)

@bot.on(events.CallbackQuery(pattern=b"cancelconv:(.+)"))
async def handle_cancel_conversion(event):
    """Cancela una conversión de video en progreso"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    conversion_id = event.pattern_match.group(1).decode()

    debug(f"[CANCEL] User requested to cancel conversion: {conversion_id}")

    # Marcar la conversión como cancelada ANTES de terminar el proceso
    cancelled_conversions.add(conversion_id)
    debug(f"[CANCEL] Conversion marked as cancelled: {conversion_id}")

    # Buscar el proceso de conversión
    proc = active_tasks.get(conversion_id)
    if proc:
        try:
            debug(f"[CANCEL] Process found (PID: {proc.pid}), terminating...")
            # Matar el proceso de ffmpeg
            proc.terminate()
            debug(f"[CANCEL] ✅ SIGTERM signal sent to process {proc.pid}")

            # Actualizar mensaje
            await safe_edit(
                event,
                get_text("conversion_cancelled"),
                parse_mode=PARSE_MODE
            )

            # Limpiar el proceso de active_tasks
            active_tasks.pop(conversion_id, None)
            debug(f"[CANCEL] Process removed from active_tasks")
        except Exception as e:
            error(f"[CANCELAR] ❌ Error cancelando conversión {conversion_id}: {e}")
            await safe_edit(
                event,
                get_text("conversion_cancel_error"),
                parse_mode=PARSE_MODE
            )
    else:
        # La conversión ya terminó o no existe
        debug(f"[CANCEL] ⚠️ Process not found in active_tasks (already finished or does not exist)")
        await safe_edit(
            event,
            get_text("conversion_not_found"),
            parse_mode=PARSE_MODE
        )

@bot.on(events.CallbackQuery(pattern=b"sendoriginal:(.+)"))
async def handle_send_original(event):
    """Maneja la solicitud de enviar el archivo original sin conversión"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    conversion_id = event.pattern_match.group(1).decode()

    debug(f"[SEND_ORIGINAL] User requested to send original file for conversion: {conversion_id}")

    # Marcar la solicitud de envío original
    send_original_requests.add(conversion_id)
    debug(f"[SEND_ORIGINAL] Request marked: {conversion_id}")

    # Cancelar la conversión si está en progreso
    cancelled_conversions.add(conversion_id)

    # Buscar el proceso de conversión y terminarlo
    proc = active_tasks.get(conversion_id)
    if proc:
        try:
            debug(f"[SEND_ORIGINAL] Terminating conversion process (PID: {proc.pid})...")
            proc.terminate()
            debug(f"[SEND_ORIGINAL] ✅ Process terminated")
        except Exception as e:
            warning(f"[SEND_ORIGINAL] Error terminating process: {e}")

    # Actualizar mensaje
    await safe_edit(
        event,
        get_text("preparing_send"),
        parse_mode=PARSE_MODE
    )

@bot.on(events.CallbackQuery(pattern=b"keep:(.+)"))
async def handle_keep_file(event):
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    file_path = event.pattern_match.group(1).decode()
    await safe_edit(event, get_text("extracted", file_path), buttons=None, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"del:(.+)"))
async def handle_delete_file(event):
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
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
                        debug(f"[FILE_DELETE] File {part} - Deleted")
                    except Exception as e:
                        error(f"[FILE_DELETE] Error deleting {file_path}: {e}")
            else:
                os.remove(file_path)
                msg = get_text("extracted_and_deleted", file_path)
                debug(f"[FILE_DELETE] File {file_path} - Deleted")
        elif os.path.isdir(file_path):
            msg = get_text("error_trying_to_delete_folder_user", file_path)
            error(f"[FILE_DELETE] An attempt was made to delete the folder and it should not happen: {file_path}")
        else:
            msg = get_text("error_file_does_not_exist_user")
            error(f"[FILE_DELETE] The file does not exist. Path: {file_path}")

        await safe_edit(event, msg, buttons=None, parse_mode=PARSE_MODE)

    except Exception as e:
        await safe_edit(event, get_text("error_deleting_user", file_path), buttons=None, parse_mode=PARSE_MODE)
        error(f"[FILE_DELETE] Error deleting {file_path}: {e}")

@bot.on(events.CallbackQuery(pattern=b"playlist_(full|first):(.+)"))
async def handle_playlist_selection(event):
    """Maneja la selección de descargar playlist completa o solo primer video"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    playlist_mode = event.pattern_match.group(1).decode()  # "full" o "first"
    url_id = event.pattern_match.group(2).decode()

    url_data = pending_urls.get(url_id)
    if not url_data:
        await safe_edit(event, get_text("error_url_expired"), buttons=None, parse_mode=PARSE_MODE)
        return

    url = url_data["url"]
    playlist_count = url_data.get("playlist_count", 1)

    debug(f"[PLAYLIST] User selected: {playlist_mode} for URL ID: {url_id} (playlist_count: {playlist_count})")

    # Si AUTO_DOWNLOAD_FORMAT está configurado, no preguntar formato
    if AUTO_DOWNLOAD_FORMAT in ["AUDIO", "VIDEO"]:
        debug(f"[PLAYLIST] AUTO_DOWNLOAD_FORMAT={AUTO_DOWNLOAD_FORMAT}, skipping format selection")
        # Simular selección de formato automática
        format_type = AUTO_DOWNLOAD_FORMAT.lower()

        # Llamar directamente a la lógica de descarga
        pending_urls.pop(url_id, None)
        is_audio = format_type == "audio"
        download_full_playlist = playlist_mode == "full"

        debug(f"[PLAYLIST] Downloading {'full playlist' if download_full_playlist else 'first video only'} as {format_type}")

        format_flag = "bestaudio" if is_audio else "bv*+ba/best"
        final_output_dir = DOWNLOAD_PATHS["url_audio"] if is_audio else DOWNLOAD_PATHS["url_video"]

        status_message = await safe_edit(
            event,
            get_text("downloading", AUD_ICO if is_audio else VID_ICO),
            buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
            parse_mode=PARSE_MODE,
            wait_for_result=True
        )

        if status_message is None:
            status_message = await safe_reply(
                event,
                get_text("downloading", AUD_ICO if is_audio else VID_ICO),
                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                parse_mode=PARSE_MODE,
                wait_for_result=False
            )

        # Usar timestamp para evitar sobrescribir archivos durante la descarga
        timestamp = int(time.time() * 1000)
        # Incluir índice de playlist en el template para evitar sobrescrituras
        temp_template = f"%(playlist_index)s-%(title).200s_temp{timestamp}.%(ext)s"

        cmd = [
            "yt-dlp",
            "-f", format_flag,
            "--restrict-filenames",
            "--newline",
            "--progress",
            "-o", os.path.join(TEMP_DIR, temp_template),
            url
        ]

        # Si solo quiere el primer video, agregar --no-playlist
        if not download_full_playlist:
            cmd.insert(1, "--no-playlist")
            debug(f"[PLAYLIST] Added --no-playlist flag")

        # Calcular delay automático basado en el número de vídeos
        # Si download_full_playlist es False, solo descarga 1 vídeo
        videos_to_download = playlist_count if download_full_playlist else 1
        sleep_interval = calculate_ytdlp_sleep_interval(videos_to_download)

        if sleep_interval > 0:
            cmd.extend(["--sleep-interval", str(sleep_interval)])
            debug(f"[URL_DOWNLOAD] Auto-calculated sleep interval: {sleep_interval}s for {videos_to_download} video(s)")

        if is_audio:
            cmd.extend(["--extract-audio", "--audio-format", "mp3"])

        # Pasar el número total de vídeos solo si es playlist completa
        total_vids = playlist_count if download_full_playlist else 1
        task = asyncio.create_task(run_url_download(event, cmd, status_message, final_output_dir, is_full_playlist=download_full_playlist, total_videos=total_vids))
        active_tasks[event.id] = task
        return

    # Si AUTO_DOWNLOAD_FORMAT=ASK, preguntar el formato (audio o video)
    buttons = [
        [Button.inline(get_text("audio", AUD_ICO), data=f"playlistfmt_{playlist_mode}_audio:{url_id}"),
         Button.inline(get_text("video", VID_ICO), data=f"playlistfmt_{playlist_mode}_video:{url_id}")],
        [Button.inline(get_text("button_cancel"), data=f"simplecancel:{url_id}")]
    ]

    await safe_edit(event, get_text("dowload_asking"), buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"playlistfmt_(full|first)_(audio|video):(.+)"))
async def handle_playlist_format_selection(event):
    """Maneja la selección de formato para playlist"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    playlist_mode = event.pattern_match.group(1).decode()  # "full" o "first"
    format_type = event.pattern_match.group(2).decode()  # "audio" o "video"
    url_id = event.pattern_match.group(3).decode()

    url_data = pending_urls.get(url_id)
    if not url_data:
        await safe_edit(event, get_text("error_url_expired"), buttons=None, parse_mode=PARSE_MODE)
        return

    url = url_data["url"]
    playlist_count = url_data.get("playlist_count", 1)
    pending_urls.pop(url_id, None)
    is_audio = format_type == "audio"
    download_full_playlist = playlist_mode == "full"

    debug(f"[PLAYLIST] Downloading {'full playlist' if download_full_playlist else 'first video only'} as {format_type} (playlist_count: {playlist_count})")

    format_flag = "bestaudio" if is_audio else "bv*+ba/best"
    final_output_dir = DOWNLOAD_PATHS["url_audio"] if is_audio else DOWNLOAD_PATHS["url_video"]

    status_message = await safe_edit(
        event,
        get_text("downloading", AUD_ICO if is_audio else VID_ICO),
        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
        parse_mode=PARSE_MODE,
        wait_for_result=True
    )

    if status_message is None:
        status_message = await safe_reply(
            event,
            get_text("downloading", AUD_ICO if is_audio else VID_ICO),
            buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
            parse_mode=PARSE_MODE,
            wait_for_result=False
        )

    # Usar timestamp para evitar sobrescribir archivos durante la descarga
    timestamp = int(time.time() * 1000)
    # Incluir índice de playlist en el template para evitar sobrescrituras
    temp_template = f"%(playlist_index)s-%(title).200s_temp{timestamp}.%(ext)s"

    cmd = [
        "yt-dlp",
        "-f", format_flag,
        "--restrict-filenames",
        "--newline",
        "--progress",
        "-o", os.path.join(TEMP_DIR, temp_template),
        url
    ]

    # Si solo quiere el primer video, agregar --no-playlist
    if not download_full_playlist:
        cmd.insert(1, "--no-playlist")
        debug(f"[PLAYLIST] Added --no-playlist flag")

    # Calcular delay automático basado en el número de vídeos
    # Si download_full_playlist es False, solo descarga 1 vídeo
    videos_to_download = playlist_count if download_full_playlist else 1
    sleep_interval = calculate_ytdlp_sleep_interval(videos_to_download)

    if sleep_interval > 0:
        cmd.extend(["--sleep-interval", str(sleep_interval)])
        debug(f"[URL_DOWNLOAD] Auto-calculated sleep interval: {sleep_interval}s for {videos_to_download} video(s)")

    if is_audio:
        cmd.extend(["--extract-audio", "--audio-format", "mp3"])

    # Pasar el número total de vídeos solo si es playlist completa
    total_vids = playlist_count if download_full_playlist else 1
    task = asyncio.create_task(run_url_download(event, cmd, status_message, final_output_dir, is_full_playlist=download_full_playlist, total_videos=total_vids))
    active_tasks[event.id] = task

@bot.on(events.CallbackQuery(pattern=b"url_(audio|video):(.+)"))
async def handle_format_selection(event):
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    format_type = event.pattern_match.group(1).decode()
    url_id = event.pattern_match.group(2).decode()

    url_data = pending_urls.get(url_id)
    if not url_data:
        await safe_edit(event, get_text("error_url_expired"), buttons=None, parse_mode=PARSE_MODE)
        return

    url = url_data["url"]
    playlist_count = url_data.get("playlist_count", 1)
    pending_urls.pop(url_id, None)
    is_audio = format_type == "audio"

    format_flag = "bestaudio" if is_audio else "bv*+ba/best"
    final_output_dir = DOWNLOAD_PATHS["url_audio"] if is_audio else DOWNLOAD_PATHS["url_video"]

    status_message = await safe_edit(
        event,
        get_text("downloading", AUD_ICO if is_audio else VID_ICO),
        buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
        parse_mode=PARSE_MODE,
        wait_for_result=True
    )

    # Si no se pudo editar el mensaje (timeout en cola), crear uno nuevo
    if status_message is None:
        status_message = await safe_reply(
            event,
            get_text("downloading", AUD_ICO if is_audio else VID_ICO),
            buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
            parse_mode=PARSE_MODE,
            wait_for_result=False
        )

    # Usar timestamp para evitar sobrescribir archivos durante la descarga
    timestamp = int(time.time() * 1000)  # Timestamp en milisegundos
    # Incluir índice de playlist en el template para evitar sobrescrituras
    temp_template = f"%(playlist_index)s-%(title).200s_temp{timestamp}.%(ext)s"

    cmd = [
        "yt-dlp",
        "-f", format_flag,
        "--restrict-filenames",
        "--newline",  # Cada línea de progreso completa (para parsear en tiempo real)
        "--progress",  # Forzar mostrar progreso
        "-o", os.path.join(TEMP_DIR, temp_template),  # Descargar a /tmp
        url
    ]

    # Calcular delay automático basado en el número de vídeos (siempre 1 en este caso)
    sleep_interval = calculate_ytdlp_sleep_interval(playlist_count)

    if sleep_interval > 0:
        cmd.extend(["--sleep-interval", str(sleep_interval)])
        debug(f"[URL_DOWNLOAD] Auto-calculated sleep interval: {sleep_interval}s for {playlist_count} video(s)")

    if is_audio:
        cmd.extend(["--extract-audio", "--audio-format", "mp3"])

    task = asyncio.create_task(run_url_download(event, cmd, status_message, final_output_dir, is_full_playlist=False, total_videos=playlist_count))
    active_tasks[event.id] = task

def parse_progress(line):
    """
    Parsea líneas de progreso de yt-dlp.
    Formato: [download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:30
    O con tamaño aproximado: [download]  13.7% of ~   4.89GiB at   62.33MiB/s ETA 01:10 (frag 42/306)
    Retorna: {"percent": "45.2", "size": "123.45MiB", "speed": "1.23MiB/s", "eta": "00:30"}
    """
    try:
        # Patrón para capturar: porcentaje, tamaño (con ~ opcional), velocidad, ETA
        # El \s* permite espacios extra, el ~? permite el símbolo de aproximado
        pattern = r'\[download\]\s+(\d+\.?\d*)%\s+of\s+~?\s*([\d\.]+\w+)(?:\s+at\s+([\d\.]+\w+/s))?(?:\s+ETA\s+([\d:]+))?'
        match = re.search(pattern, line)

        if match:
            return {
                "percent": match.group(1),
                "size": match.group(2),
                "speed": match.group(3) if match.group(3) else "N/A",
                "eta": match.group(4) if match.group(4) else "N/A"
            }
    except Exception as e:
        warning(f"[URL_DOWNLOAD] Error parsing progress: {e}")
    return None

async def update_progress_message(status_message, progress_info, event, file_name=None, playlist_info=None):
    """Actualiza el mensaje de Telegram con el progreso de descarga

    Args:
        status_message: Mensaje de Telegram a actualizar
        progress_info: Diccionario con percent, size, speed, eta
        event: Evento de Telegram
        file_name: Nombre del archivo actual (opcional)
        playlist_info: Diccionario con {"current": int, "total": int} para playlists (opcional)
    """
    # Si no hay mensaje de estado, no hacer nada
    if status_message is None:
        return

    try:
        percent = progress_info["percent"]
        size = progress_info["size"]
        speed = progress_info["speed"]
        eta = progress_info["eta"]

        # Crear barra de progreso visual del vídeo actual
        bar_length = 20
        filled = int(bar_length * float(percent) / 100)
        bar = "█" * filled + "░" * (bar_length - filled)

        # Si no hay nombre de archivo, intentar extraerlo del progreso o usar placeholder
        if not file_name:
            file_name = progress_info.get("filename", "...")

        # Si es una playlist, añadir información de progreso de la playlist
        if playlist_info:
            current = playlist_info["current"]
            total = playlist_info["total"]
            # Calcular progreso global de la playlist
            playlist_percent = ((current - 1) / total * 100) + (float(percent) / total)
            message = get_text("downloading_progress_playlist",
                             bar, percent, size, speed, eta, file_name,
                             current, total, f"{playlist_percent:.1f}")
        else:
            message = get_text("downloading_progress", bar, percent, size, speed, eta, file_name)

        # Usar edición directa sin cola para actualizaciones de progreso (más rápido)
        try:
            await status_message.edit(
                message,
                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{event.id}")],
                parse_mode=PARSE_MODE
            )
        except Exception as edit_error:
            # Ignorar errores de edición (FloodWaitError, mensaje no modificado, etc.)
            warning(f"[URL_DOWNLOAD] Error editing progress message: {edit_error}")
    except Exception as e:
        # Ignorar errores de actualización (puede ser FloodWaitError)
        warning(f"[URL_DOWNLOAD] Error updating progress: {e}")

async def run_direct_download(event, url, filename, status_message, final_output_dir, icon, content_type):
    """Descarga un archivo directo usando wget"""
    try:
        debug(f"[DIRECT_DOWNLOAD] Starting direct download: {filename}")
        debug(f"[DIRECT_DOWNLOAD] Final output directory: {final_output_dir}")
        debug(f"[DIRECT_DOWNLOAD] Content type: {content_type}, Icon: {icon}")

        # Usar timestamp para evitar sobrescribir archivos durante la descarga
        timestamp = int(time.time() * 1000)
        temp_filename = f"{filename}_temp{timestamp}"
        temp_file_path = os.path.join(TEMP_DIR, temp_filename)

        # Comando wget con progreso
        cmd = [
            "wget",
            "--progress=bar:force",  # Mostrar barra de progreso
            "--show-progress",  # Mostrar progreso detallado
            "-O", temp_file_path,  # Output file
            url
        ]

        debug(f"[DIRECT_DOWNLOAD] Command: {' '.join(cmd)}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        active_tasks[event.id] = proc

        # Variables para control de progreso
        last_update_time = 0
        update_interval = PROGRESS_UPDATE_INTERVAL

        # Leer stderr (wget muestra progreso en stderr)
        async def read_stderr():
            stderr_lines = []
            async for line in proc.stderr:
                line_str = line.decode().strip()
                stderr_lines.append(line_str)

                # Parsear progreso de wget
                # Formato: 45% [=====>     ] 123.45M  1.23MB/s    eta 30s
                if '%' in line_str and status_message:
                    nonlocal last_update_time
                    current_time = time.time()

                    # Detectar si llegamos al 100%
                    percent_match_check = re.search(r'(\d+)%', line_str)
                    is_complete = percent_match_check and int(percent_match_check.group(1)) >= 100
                    should_update = (current_time - last_update_time >= update_interval) or is_complete

                    if should_update:
                        try:
                            # Extraer porcentaje
                            percent_match = re.search(r'(\d+)%', line_str)
                            # Extraer tamaño descargado
                            size_match = re.search(r'\]\s+([\d\.]+[KMG]?)', line_str)
                            # Extraer velocidad
                            speed_match = re.search(r'([\d\.]+[KMG]?B/s)', line_str)
                            # Extraer ETA
                            eta_match = re.search(r'eta\s+([\dhms]+)', line_str)

                            if percent_match:
                                progress_info = {
                                    "percent": percent_match.group(1),
                                    "size": size_match.group(1) if size_match else "N/A",
                                    "speed": speed_match.group(1) if speed_match else "N/A",
                                    "eta": eta_match.group(1) if eta_match else "N/A",
                                    "filename": filename
                                }

                                await update_progress_message(status_message, progress_info, event, filename)
                                last_update_time = current_time
                        except Exception as e:
                            debug(f"[DIRECT_DOWNLOAD] Error parsing wget progress: {e}")

                if line_str:
                    debug(f"[WGET] {line_str}")

            return stderr_lines

        # Leer stdout (normalmente vacío para wget)
        async def read_stdout():
            async for line in proc.stdout:
                line_str = line.decode().strip()
                if line_str:
                    debug(f"[WGET] stdout: {line_str}")

        # Ejecutar lectura en paralelo
        stderr_task = asyncio.create_task(read_stderr())
        stdout_task = asyncio.create_task(read_stdout())

        await stderr_task
        await stdout_task
        await proc.wait()

        debug(f"[DIRECT_DOWNLOAD] Process finished with code {proc.returncode}")

        # Manejar cancelación
        if proc.returncode == -15 or proc.returncode == 143:  # SIGTERM
            await handle_cancel(status_message)
            return

        # Verificar si la descarga fue exitosa
        if proc.returncode == 0 and os.path.exists(temp_file_path):
            # Eliminar mensaje de progreso
            if status_message:
                await safe_delete(status_message)

            # Mover archivo a directorio final
            final_file_path = os.path.join(final_output_dir, filename)

            # Si ya existe, agregar número
            if os.path.exists(final_file_path):
                base, ext = os.path.splitext(filename)
                counter = 1
                while os.path.exists(final_file_path):
                    final_file_path = os.path.join(final_output_dir, f"{base}_{counter}{ext}")
                    counter += 1

            shutil.move(temp_file_path, final_file_path)
            debug(f"[DIRECT_DOWNLOAD] File moved to: {final_file_path}")

            await handle_success(event, final_file_path, icon=icon, content_type=content_type)
        else:
            # Eliminar mensaje de progreso antes de mostrar error
            if status_message:
                await safe_delete(status_message)

            error(f"[DIRECT_DOWNLOAD] Download failed with code {proc.returncode}")
            await safe_reply(event, get_text("error_url_failed_user"), parse_mode=PARSE_MODE)

            # Limpiar archivo temporal si existe
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

    except asyncio.CancelledError:
        await handle_cancel(status_message)
        # Limpiar archivo temporal
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        raise
    except Exception as e:
        # Eliminar mensaje de progreso antes de mostrar error
        if status_message:
            await safe_delete(status_message)

        error(f"[DIRECT_DOWNLOAD] ❌ Error during direct download: {e}")
        error(f"[DIRECT_DOWNLOAD] Exception type: {type(e).__name__}")
        import traceback
        error(f"[DIRECT_DOWNLOAD] Traceback: {traceback.format_exc()}")
        await safe_reply(event, get_text("error_url_failed_user"), parse_mode=PARSE_MODE)
        # Limpiar archivo temporal
        if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
    finally:
        debug(f"[DIRECT_DOWNLOAD] Cleaning up. ID {event.id}")
        active_tasks.pop(event.id, None)

async def run_url_download(event, cmd, status_message, final_output_dir, is_full_playlist=False, total_videos=None):
    try:
        debug("[URL_DOWNLOAD] Creating URL download subprocess...")
        debug(f"[URL_DOWNLOAD] Final output directory: {final_output_dir}")
        debug(f"[URL_DOWNLOAD] Is full playlist: {is_full_playlist}")
        debug(f"[URL_DOWNLOAD] Total videos in playlist: {total_videos}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        active_tasks[event.id] = proc

        # Registrar información de playlist si es necesario
        # IMPORTANTE: Usar status_message.id como clave porque el botón de cancelar usa event.id que coincide con el ID del mensaje
        if is_full_playlist:
            playlist_downloads[event.id] = {
                "is_full_playlist": True,
                "final_output_dir": final_output_dir,
                "downloaded_files": [],
                "total_videos": total_videos
            }
            debug(f"[URL_DOWNLOAD] Playlist download registered with ID {event.id}")

        # Variables para control de progreso
        stdout_lines = []
        last_update_time = 0
        update_interval = PROGRESS_UPDATE_INTERVAL  # Intervalo dinámico basado en PARALLEL_DOWNLOADS
        current_filename = None  # Almacenar el nombre del archivo actual (solo nombre, no path completo)
        current_filepath = None  # Almacenar el path completo del archivo actual
        current_video_index = None  # Índice del vídeo actual en la playlist

        # Leer stdout línea por línea en tiempo real
        async def read_stdout():
            nonlocal last_update_time, current_filename, current_filepath, current_video_index
            async for line in proc.stdout:
                line_str = line.decode().strip()
                stdout_lines.append(line_str)
                debug(f"[YT-DLP] {line_str}")

                # Detectar nombre del archivo: [download] Destination: /path/to/file.mp4
                if "[download] Destination:" in line_str:
                    # Extraer el path completo (ya incluye TEMP_DIR)
                    path_start = line_str.find("Destination:") + len("Destination:")
                    full_path = line_str[path_start:].strip()
                    current_filepath = full_path  # Guardar el path completo para operaciones de archivo
                    current_filename = os.path.basename(full_path)  # Guardar solo el nombre para mostrar al usuario

                    # Extraer índice de playlist del nombre del archivo (formato: 001-nombre.mp4, 016-nombre.mp4, etc.)
                    if is_full_playlist:
                        match = re.match(r'^(\d+)-', current_filename)
                        if match:
                            current_video_index = int(match.group(1))
                            debug(f"[URL_DOWNLOAD] Playlist video index detected: {current_video_index}/{total_videos}")

                    debug(f"[URL_DOWNLOAD] File destination detected: {current_filepath}")
                    debug(f"[URL_DOWNLOAD] Display filename: {current_filename}")

                # Detectar archivo completado: [download] 100% of 123.45MiB in 00:30
                # o [download] <filename> has already been downloaded
                # NOTA: NO mover archivos aquí porque pueden ser archivos parciales (vídeo o audio separados)
                # que yt-dlp aún necesita mergear
                if "[download] 100%" in line_str or "has already been downloaded" in line_str:
                    # Solo registrar para tracking, pero NO mover archivos
                    if is_full_playlist and event.id in playlist_downloads:
                        if current_filepath:
                            debug(f"[URL_DOWNLOAD] File download completed (may need merging): {current_filepath}")

                # Detectar líneas de progreso: [download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:30
                if "[download]" in line_str and "%" in line_str:
                    current_time = asyncio.get_event_loop().time()
                    time_diff = current_time - last_update_time
                    debug(f"[URL_DOWNLOAD] Progress detected. Time since last update: {time_diff:.2f}s (interval: {update_interval}s)")

                    # Detectar si llegamos al 100%
                    is_complete = "[download] 100%" in line_str
                    should_update = (time_diff >= update_interval) or is_complete

                    if should_update:
                        last_update_time = current_time
                        # Parsear y actualizar mensaje
                        progress_info = parse_progress(line_str)
                        if progress_info:
                            debug(f"[URL_DOWNLOAD] Updating progress message: {progress_info}")
                            # Pasar información de playlist si está disponible
                            playlist_info = None
                            if is_full_playlist and current_video_index and total_videos:
                                playlist_info = {"current": current_video_index, "total": total_videos}
                            await update_progress_message(status_message, progress_info, event, current_filename, playlist_info)
                        else:
                            debug(f"[URL_DOWNLOAD] Could not parse progress from: {line_str}")

        # Leer stderr en paralelo
        async def read_stderr():
            stderr_lines = []
            async for line in proc.stderr:
                line_str = line.decode().strip()
                stderr_lines.append(line_str)
                if line_str:
                    debug(f"[YT-DLP] stderr: {line_str}")
            return stderr_lines

        # Ejecutar lectura de stdout y stderr en paralelo
        stderr_task = asyncio.create_task(read_stderr())
        await read_stdout()
        stderr_lines = await stderr_task

        # Esperar a que termine el proceso
        await proc.wait()
        debug(f"[URL_DOWNLOAD] Exiting URL download subprocess. Code {proc.returncode}")

        if proc.returncode == -15:
            await handle_cancel(status_message)
            return

        if status_message:
            await safe_delete(status_message)
        if proc.returncode == 0:
            temp_file_paths = extract_file_paths(stdout_lines)

            if temp_file_paths:
                # Si es playlist completa con múltiples archivos, solo almacenar sin mostrar botones
                if is_full_playlist and len(temp_file_paths) > 1:
                    debug(f"[URL_DOWNLOAD] Full playlist detected with {len(temp_file_paths)} files. Storing without action buttons.")
                    stored_files = []
                    for temp_file_path in temp_file_paths:
                        if os.path.exists(temp_file_path):
                            # Mover archivo de /tmp a carpeta final
                            filename = os.path.basename(temp_file_path)
                            final_file_path = os.path.join(final_output_dir, filename)

                            debug(f"[URL DOWNLOAD] Moving file from temp to final location...")
                            debug(f"[URL DOWNLOAD] Temp: {temp_file_path}")
                            debug(f"[URL DOWNLOAD] Final: {final_file_path}")

                            shutil.move(temp_file_path, final_file_path)
                            debug(f"[URL DOWNLOAD] ✅ File moved to: {final_file_path}")
                            stored_files.append(filename)
                        else:
                            warning(f"[URL_DOWNLOAD] Output file not found: {temp_file_path}")

                    # Mostrar mensaje de éxito sin botones
                    if stored_files:
                        file_ext = os.path.splitext(stored_files[0])[1].lower()
                        icon = get_file_icon(file_ext)
                        message = get_text("playlist_stored", icon, len(stored_files))
                        await safe_reply(event, message, parse_mode=PARSE_MODE)

                    # Limpiar información de playlist
                    if event.id in playlist_downloads:
                        playlist_downloads.pop(event.id, None)
                    # Limpiar información de playlist
                    if event.id in playlist_downloads:
                        playlist_downloads.pop(event.id, None)
                else:
                    # Video individual o primer video de playlist - mostrar botones normales
                    for temp_file_path in temp_file_paths:
                        if os.path.exists(temp_file_path):
                            # Mover archivo de /tmp a carpeta final
                            filename = os.path.basename(temp_file_path)
                            final_file_path = os.path.join(final_output_dir, filename)

                            debug(f"[URL DOWNLOAD] Moving file from temp to final location...")
                            debug(f"[URL DOWNLOAD] Temp: {temp_file_path}")
                            debug(f"[URL DOWNLOAD] Final: {final_file_path}")

                            shutil.move(temp_file_path, final_file_path)
                            debug(f"[URL DOWNLOAD] ✅ File moved to: {final_file_path}")

                            await handle_success(event, final_file_path)
                        else:
                            warning(f"[URL_DOWNLOAD] Output file not found: {temp_file_path}")

                    # Limpiar información de playlist
                    if event.id in playlist_downloads:
                        playlist_downloads.pop(event.id, None)
            else:
                warning("[URL_DOWNLOAD] No downloaded files found in yt-dlp output")
                debug(f"[URL_DOWNLOAD] Command executed: {' '.join(cmd)}")
                debug(f"[URL_DOWNLOAD] Full stdout: {chr(10).join(stdout_lines)}")
                await safe_reply(event, get_text("error_url_failed_user"), parse_mode=PARSE_MODE)
        else:
            stderr_output = "\n".join(stderr_lines)
            error(f"[URL_DOWNLOAD] URL download failed. Error: {stderr_output}")
            await safe_reply(event, get_text("error_url_failed_user"), parse_mode=PARSE_MODE)

    except asyncio.CancelledError:
        # No limpiar playlist_downloads aquí si fue cancelada - se limpia en handle_playlist_cancel_confirmation
        await handle_cancel(status_message)
        raise
    except Exception as e:
        error(f"[URL_DOWNLOAD] ❌ Error during URL download: {e}")
        error(f"[URL_DOWNLOAD] Exception type: {type(e).__name__}")
        import traceback
        error(f"[URL_DOWNLOAD] Traceback: {traceback.format_exc()}")
        # Intentar notificar al usuario
        try:
            if status_message:
                await safe_delete(status_message)
            await safe_reply(event, get_text("error_url_failed_user"), parse_mode=PARSE_MODE)
        except:
            pass  # Si falla la notificación, al menos ya logueamos el error original
    finally:
        debug(f"[URL_DOWNLOAD] Cleaning URL download subprocess. ID {event.id}")
        active_tasks.pop(event.id, None)
        # Limpiar información de playlist si la descarga terminó exitosamente (no fue cancelada)
        if event.id in playlist_downloads:
            debug(f"[URL_DOWNLOAD] Cleaning playlist info for ID {event.id}")
            playlist_downloads.pop(event.id, None)

def extract_file_paths(stdout_lines):
    """Extrae todas las rutas de archivos descargados (soporta playlists/carruseles)"""
    file_paths = []
    current_file = None
    is_partial = False

    for line in stdout_lines:
        debug(f"[YT-DLP] {line}")

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
            debug(f"[URL_DOWNLOAD] Detected merged output file: {merged_path}")
            current_file = merged_path
            is_partial = False  # El archivo mergeado es el final
            # Agregar inmediatamente el archivo mergeado
            if current_file and current_file not in file_paths:
                file_paths.append(current_file)
                debug(f"[URL_DOWNLOAD] File added to download list: {current_file}")

        # Detectar extracción de audio (este es el archivo final)
        elif "[ExtractAudio]" in line and "Destination:" in line:
            audio_path = line.split("Destination:")[-1].strip()
            debug(f"[URL_DOWNLOAD] Detected audio output file: {audio_path}")
            current_file = audio_path
            is_partial = False  # El audio extraído es el final
            # Agregar inmediatamente el audio extraído
            if current_file and current_file not in file_paths:
                file_paths.append(current_file)
                debug(f"[URL_DOWNLOAD] File added to download list: {current_file}")

        # Detectar finalización de descarga (solo agregar si NO es parcial)
        elif "[download] 100%" in line or "has already been downloaded" in line:
            if current_file and not is_partial and current_file not in file_paths:
                file_paths.append(current_file)
                debug(f"[URL_DOWNLOAD] File added to download list: {current_file}")
                current_file = None

    # Limpiar nombres temporales y manejar duplicados
    final_paths = []
    for file_path in file_paths:
        if not os.path.exists(file_path):
            final_paths.append(file_path)
            continue

        directory = os.path.dirname(file_path)
        filename = os.path.basename(file_path)

        # Eliminar el timestamp temporal del nombre: "video_temp1234567890.mp4" -> "video.mp4"
        clean_filename = re.sub(r'_temp\d+\.', '.', filename)

        # Buscar nombre único en el directorio
        unique_filename = get_unique_filename(directory, clean_filename)
        unique_path = os.path.join(directory, unique_filename)

        # Renombrar el archivo temporal al nombre final
        try:
            os.rename(file_path, unique_path)
            if unique_filename != clean_filename:
                debug(f"[URL_DOWNLOAD] File renamed (duplicate): {clean_filename} -> {unique_filename}")
            else:
                debug(f"[URL_DOWNLOAD] File renamed: {filename} -> {unique_filename}")
            final_paths.append(unique_path)
        except Exception as e:
            warning(f"[URL_DOWNLOAD] Error renaming file: {e}")
            final_paths.append(file_path)

    return final_paths

async def get_video_metadata(file_path):
    """Obtiene metadatos del video usando ffprobe"""
    try:
        debug(f"[METADATA] Getting metadata from: {file_path}")

        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path
        ]

        debug(f"[METADATA] Running ffprobe...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            import json
            data = json.loads(stdout.decode())

            # Buscar el stream de video
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    duration = int(float(data.get("format", {}).get("duration", 0)))
                    width = stream.get("width", 0)
                    height = stream.get("height", 0)
                    debug(f"[METADATA] ✅ Metadata obtained: {duration}s, {width}x{height}")
                    return duration, width, height

            warning(f"[METADATA] ⚠️ Video stream not found")
        else:
            error_msg = stderr.decode() if stderr else "Unknown error"
            error(f"[METADATA] ❌ Error running ffprobe (code {proc.returncode}): {error_msg[:200]}")

        return None, None, None
    except Exception as e:
        warning(f"[METADATA] ❌ Exception getting video metadata: {e}")
        return None, None, None

async def get_file_info(file_path):
    """
    Obtiene información detallada de un archivo (tamaño, duración, resolución, formato).
    Retorna un diccionario con la información.
    """
    try:
        import json

        file_size = os.path.getsize(file_path)
        file_ext = os.path.splitext(file_path)[1].lower()

        info = {
            "size": file_size,
            "size_formatted": format_file_size(file_size),
            "extension": file_ext,
            "type": None,
            "duration": None,
            "duration_formatted": None,
            "resolution": None,
            "codec_video": None,
            "codec_audio": None,
            "bitrate": None
        }

        # Detectar tipo de archivo
        if file_ext in EXTENSIONS_VIDEO:
            info["type"] = "video"
        elif file_ext in EXTENSIONS_AUDIO:
            info["type"] = "audio"
        elif file_ext in EXTENSIONS_IMAGE:
            info["type"] = "image"
        else:
            info["type"] = "document"

        # Obtener metadatos con ffprobe para video/audio
        if info["type"] in ("video", "audio"):
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
                data = json.loads(stdout.decode())
                format_data = data.get("format", {})

                # Duración
                duration = float(format_data.get("duration", 0))
                if duration > 0:
                    info["duration"] = int(duration)
                    info["duration_formatted"] = format_duration(int(duration))

                # Bitrate
                bitrate = int(format_data.get("bit_rate", 0))
                if bitrate > 0:
                    info["bitrate"] = f"{bitrate // 1000} kbps"

                # Streams
                for stream in data.get("streams", []):
                    if stream.get("codec_type") == "video" and not info["codec_video"]:
                        info["codec_video"] = stream.get("codec_name", "").upper()
                        width = stream.get("width")
                        height = stream.get("height")
                        if width and height:
                            info["resolution"] = f"{width}x{height}"
                    elif stream.get("codec_type") == "audio" and not info["codec_audio"]:
                        info["codec_audio"] = stream.get("codec_name", "").upper()

        return info
    except Exception as e:
        warning(f"[FILE_INFO] Error getting file information: {e}")
        return {
            "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
            "size_formatted": format_file_size(os.path.getsize(file_path)) if os.path.exists(file_path) else "0 B",
            "extension": os.path.splitext(file_path)[1].lower(),
            "type": "document"
        }

def format_file_size(size_bytes):
    """Formatea el tamaño del archivo en formato legible"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"

def format_duration(seconds):
    """Formatea la duración en formato HH:MM:SS o MM:SS"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes:02d}:{secs:02d}"

async def generate_video_thumbnail(video_path, output_path=None, timestamp="00:00:03"):
    """
    Genera una miniatura de un video en el segundo especificado.
    Retorna la ruta del thumbnail generado o None si falla.
    """
    try:
        debug(f"[THUMBNAIL] Generating thumbnail for: {video_path}")

        if output_path is None:
            # Generar thumbnail en /tmp
            video_filename = os.path.basename(video_path)
            base_name = os.path.splitext(video_filename)[0]
            timestamp_ms = int(time.time() * 1000)
            output_path = os.path.join(TEMP_DIR, f"{base_name}_{timestamp_ms}_thumb.jpg")

        debug(f"[THUMBNAIL] Output path: {output_path}")
        debug(f"[THUMBNAIL] Timestamp: {timestamp}")

        cmd = [
            "ffmpeg",
            "-i", video_path,
            "-ss", timestamp,  # Segundo del video para capturar
            "-vframes", "1",   # Solo 1 frame
            "-vf", "scale=320:-1",  # Escalar a 320px de ancho manteniendo aspecto
            "-y",  # Sobrescribir sin preguntar
            output_path
        ]

        debug(f"[THUMBNAIL] Running ffmpeg command...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        _, stderr = await proc.communicate()

        if proc.returncode == 0 and os.path.exists(output_path):
            thumb_size = os.path.getsize(output_path)
            debug(f"[THUMBNAIL] ✅ Thumbnail generated successfully: {output_path} ({thumb_size} bytes)")
            return output_path
        else:
            error_msg = stderr.decode() if stderr else "Unknown error"
            warning(f"[THUMBNAIL] ❌ Error generating thumbnail (code {proc.returncode}): {error_msg[:200]}")
            return None
    except Exception as e:
        warning(f"[THUMBNAIL] ❌ Exception generating thumbnail: {e}")
        return None

async def convert_video_to_telegram_compatible(input_path, status_message=None):
    """
    Convierte un video a formato compatible con Telegram (MP4 con H.264 + AAC).
    Retorna la ruta del archivo convertido (temporal en /tmp) o el original si falla.
    IMPORTANTE: El archivo convertido debe ser eliminado después de enviarlo.
    """
    try:
        debug(f"[CONVERSION] Starting video conversion: {input_path}")

        # Generar nombre de archivo temporal en /tmp para la conversión
        input_filename = os.path.basename(input_path)
        base_name = os.path.splitext(input_filename)[0]
        # Usar timestamp para evitar colisiones
        timestamp = int(time.time() * 1000)
        output_path = os.path.join(TEMP_DIR, f"{base_name}_{timestamp}_telegram.mp4")

        debug(f"[CONVERSION] Output file: {output_path}")

        # Obtener duración del video para calcular progreso
        duration_seconds = 0
        try:
            debug(f"[CONVERSION] Getting video metadata...")
            duration, _, _ = await get_video_metadata(input_path)
            duration_seconds = duration if duration else 0
            debug(f"[CONVERSION] Video duration: {duration_seconds}s")
        except Exception as e:
            debug(f"[CONVERSION] Could not get video duration: {e}")

        # Generar ID único para esta conversión
        conversion_id = f"conv_{abs(hash(input_path)) % 1000000}"
        debug(f"[CONVERSION] Conversion ID: {conversion_id}")

        # Determinar si es un video largo (>300s = 5 minutos) para mostrar botón de enviar original
        is_long_video = duration_seconds > 300
        if is_long_video:
            debug(f"[CONVERSION] Long video detected ({int(duration_seconds / 60)} minutes), will show send original button")

        if status_message:
            # Preparar botones según si es video largo o no
            if is_long_video:
                buttons = [
                    [Button.inline(get_text("button_cancel_conversion"), data=f"cancelconv:{conversion_id}")],
                    [Button.inline(get_text("button_send_original"), data=f"sendoriginal:{conversion_id}")]
                ]
                # Usar mensaje específico para videos largos con advertencia
                duration_minutes = int(duration_seconds / 60)
                msg = get_text("converting_video_long_progress", duration_minutes)
            else:
                buttons = [Button.inline(get_text("button_cancel_conversion"), data=f"cancelconv:{conversion_id}")]
                msg = get_text("converting_video_progress")

            debug(f"[CONVERSION] Updating status message with cancel button" + (" and send original button" if is_long_video else ""))
            await safe_edit(
                status_message,
                msg,
                buttons=buttons,
                parse_mode=PARSE_MODE
            )

        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-c:v", "libx264",  # Codec de video H.264 (compatible con Telegram)
            "-c:a", "aac",      # Codec de audio AAC (compatible con Telegram)
            "-movflags", "+faststart",  # Optimizar para streaming
            "-progress", "pipe:1",  # Reportar progreso a stdout
            "-y",  # Sobrescribir sin preguntar
            output_path
        ]

        debug(f"[CONVERSIÓN] Ejecutando comando ffmpeg: {' '.join(cmd[:3])}...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        debug(f"[CONVERSION] ffmpeg process started (PID: {proc.pid})")

        # Guardar el proceso para poder cancelarlo
        active_tasks[conversion_id] = proc
        debug(f"[CONVERSION] Process saved in active_tasks with ID: {conversion_id}")

        # Leer progreso en tiempo real
        last_update = 0
        debug(f"[CONVERSION] Starting progress reading...")
        while True:
            # Verificar si la conversión fue cancelada o si se solicitó enviar original
            if conversion_id in cancelled_conversions or conversion_id in send_original_requests:
                debug(f"[CONVERSION] Conversion cancelled or send original requested during progress reading, stopping...")
                break

            line = await proc.stdout.readline()
            if not line:
                break

            line = line.decode().strip()

            # ffmpeg reporta progreso en formato "out_time_ms=XXXXX"
            if line.startswith("out_time_ms="):
                try:
                    time_ms = int(line.split("=")[1])
                    time_seconds = time_ms / 1000000  # Convertir microsegundos a segundos

                    # Calcular porcentaje si conocemos la duración
                    if duration_seconds > 0 and status_message:
                        percentage = min(int((time_seconds / duration_seconds) * 100), 100)

                        # Actualizar cada 5% para no saturar
                        if percentage >= last_update + 5:
                            last_update = percentage
                            debug(f"[CONVERSION] Progress: {percentage}% ({int(time_seconds)}s / {int(duration_seconds)}s)")

                            # Crear barra de progreso
                            bar_length = 20
                            filled = int(bar_length * percentage / 100)
                            bar = "█" * filled + "░" * (bar_length - filled)

                            # Usar mensaje específico para videos largos con advertencia
                            if is_long_video:
                                duration_minutes = int(duration_seconds / 60)
                                msg = get_text("converting_video_long_progress_bar", duration_minutes, bar, percentage, int(time_seconds), int(duration_seconds))
                            else:
                                msg = get_text("converting_video_progress_bar", bar, percentage, int(time_seconds), int(duration_seconds))

                            # Preparar botones según si es video largo o no
                            if is_long_video:
                                buttons = [
                                    [Button.inline(get_text("button_cancel_conversion"), data=f"cancelconv:{conversion_id}")],
                                    [Button.inline(get_text("button_send_original"), data=f"sendoriginal:{conversion_id}")]
                                ]
                            else:
                                buttons = [Button.inline(get_text("button_cancel_conversion"), data=f"cancelconv:{conversion_id}")]

                            try:
                                await safe_edit(
                                    status_message,
                                    msg,
                                    buttons=buttons,
                                    parse_mode=PARSE_MODE
                                )
                            except Exception as e:
                                warning(f"[CONVERSION] Error updating progress message: {e}")
                                # Si falla la edición, puede ser que el mensaje fue eliminado/editado
                                # Verificar si fue cancelado o se solicitó enviar original
                                if conversion_id in cancelled_conversions or conversion_id in send_original_requests:
                                    debug(f"[CONVERSION] Message edit failed because conversion was cancelled or send original requested")
                                    break
                except Exception as e:
                    warning(f"[CONVERSION] Error processing progress line: {e}")

        debug(f"[CONVERSION] Waiting for process completion...")
        await proc.wait()

        debug(f"[CONVERSION] Process finished with code: {proc.returncode}")

        # Limpiar el proceso de active_tasks
        active_tasks.pop(conversion_id, None)
        active_tasks.pop(f"{conversion_id}_original_path", None)
        debug(f"[CONVERSION] Process removed from active_tasks")

        # Verificar si el usuario eligió enviar el archivo original
        if conversion_id in send_original_requests:
            debug(f"[CONVERSION] ✅ User chose to send original file")
            send_original_requests.discard(conversion_id)
            cancelled_conversions.discard(conversion_id)
            # Eliminar archivo de salida parcial si existe
            if os.path.exists(output_path):
                debug(f"[CONVERSION] Deleting partial file: {output_path}")
                try:
                    os.remove(output_path)
                except:
                    pass
            # Retornar el archivo original sin convertir
            return input_path

        # Verificar si la conversión fue cancelada por el usuario
        if conversion_id in cancelled_conversions:
            debug(f"[CONVERSION] ❌ Conversion was cancelled by user")
            # Eliminar de la lista de canceladas
            cancelled_conversions.discard(conversion_id)
            # Eliminar archivo de salida parcial si existe
            if os.path.exists(output_path):
                debug(f"[CONVERSION] Deleting partial file: {output_path}")
                try:
                    os.remove(output_path)
                except:
                    pass
            # Retornar None para indicar cancelación
            return None

        if proc.returncode == 0 and os.path.exists(output_path):
            file_size = os.path.getsize(output_path)
            debug(f"[CONVERSION] ✅ Conversion successful: {output_path} ({file_size} bytes)")
            debug(f"[CONVERSION] Original file preserved: {input_path}")

            # Actualizar mensaje a "Preparando envío..." para evitar condición de carrera
            # con el bucle de progreso que puede seguir intentando actualizar
            if status_message:
                try:
                    await safe_edit(
                        status_message,
                        get_text("preparing_send"),
                        parse_mode=PARSE_MODE
                    )
                    debug(f"[CONVERSION] Status message updated to 'preparing send'")
                except Exception as e:
                    debug(f"[CONVERSION] Could not update status message: {e}")

            # Retornar el archivo convertido (temporal)
            # NOTA: Este archivo debe ser eliminado después de enviarlo
            return output_path
        else:
            # Leer stderr para obtener mensaje de error
            stderr = await proc.stderr.read()
            error_msg = stderr.decode() if stderr else "Unknown error"
            error(f"[CONVERSIÓN] ❌ Error convirtiendo video (código {proc.returncode}): {error_msg[:200]}")
            # Si falla la conversión, eliminar el archivo de salida si existe
            if os.path.exists(output_path):
                warning(f"[CONVERSION] Deleting failed output file: {output_path}")
                os.remove(output_path)
            # Retornar el archivo original sin convertir
            debug(f"[CONVERSION] Returning original unconverted file: {input_path}")
            return input_path

    except asyncio.CancelledError:
        # La tarea fue cancelada (por ejemplo, el usuario canceló la conversión)
        debug(f"[CONVERSION] ❌ Conversion task cancelled")
        # Limpiar el proceso de active_tasks
        active_tasks.pop(conversion_id, None)
        debug(f"[CONVERSION] Process removed from active_tasks after cancellation")
        # Eliminar archivo de salida parcial si existe
        if os.path.exists(output_path):
            debug(f"[CONVERSION] Deleting partial file: {output_path}")
            try:
                os.remove(output_path)
            except:
                pass
        # Retornar None para indicar cancelación
        return None
    except Exception as e:
        error(f"[CONVERSION] ❌ Exception during video conversion: {e}")
        # Limpiar el proceso de active_tasks
        active_tasks.pop(conversion_id, None)
        debug(f"[CONVERSION] Process removed from active_tasks after exception")
        # Eliminar archivo de salida parcial si existe
        if os.path.exists(output_path):
            debug(f"[CONVERSION] Deleting partial file after exception: {output_path}")
            try:
                os.remove(output_path)
            except:
                pass
        # En caso de error, retornar el archivo original
        debug(f"[CONVERSION] Returning original unconverted file after exception: {input_path}")
        return input_path

async def handle_success(event, file_path, show_action_buttons=True, icon=None, content_type=None):
    try:
        debug(f"[SEND_FILE] handle_success called for: {file_path}")

        if not os.path.exists(file_path):
            error(f"[SEND_FILE] File does not exist: {file_path}")
            return

        file_size = os.path.getsize(file_path)
        debug(f"[SEND_FILE] File size: {file_size} bytes")

        # Obtener información detallada del archivo
        debug(f"[SEND_FILE] Getting file info...")
        file_info = await get_file_info(file_path)
        debug(f"[SEND_FILE] File type: {file_info.get('type', 'unknown')}")

        filename = get_filename_from_path(file_path)

        # Si se proporcionó un icono y tipo desde descarga directa, usarlos
        if icon and content_type:
            debug(f"[SEND_FILE] Using provided icon and type: {icon} ({content_type})")
            file_type = content_type
        else:
            # Construir mensaje con información del archivo
            icon_map = {
                "video": VID_ICO,
                "audio": AUD_ICO,
                "image": IMG_ICO,
                "document": DEF_ICO
            }
            icon = icon_map.get(file_info["type"], DEF_ICO)
            file_type = file_info["type"]

        # Mensaje unificado: Descarga completada + información detallada
        info_lines = [
            get_text("downloaded", icon, filename),
            "",
            get_text("file_info_size", file_info['size_formatted'])
        ]

        # Agregar información específica según el tipo
        if file_info["type"] == "video":
            if file_info["duration_formatted"]:
                info_lines.append(get_text("file_info_duration", file_info['duration_formatted']))
            if file_info["resolution"]:
                info_lines.append(get_text("file_info_resolution", file_info['resolution']))
            if file_info["codec_video"]:
                codec_info = file_info["codec_video"]
                if file_info["codec_audio"]:
                    codec_info += f" + {file_info['codec_audio']}"
                info_lines.append(get_text("file_info_codec", codec_info))
        elif file_info["type"] == "audio":
            if file_info["duration_formatted"]:
                info_lines.append(get_text("file_info_duration", file_info['duration_formatted']))
            if file_info["codec_audio"]:
                info_lines.append(get_text("file_info_codec_audio", file_info['codec_audio']))
            if file_info["bitrate"]:
                info_lines.append(get_text("file_info_bitrate", file_info['bitrate']))

        info_message = "\n".join(info_lines)
        debug(f"[SEND_FILE] Message prepared, length: {len(info_message)} chars")
        debug(f"[SEND_FILE] Sending success message to user...")
        debug(f"[SEND_FILE] Calling safe_reply with wait_for_result=True...")

        try:
            reply_result = await safe_reply(event, info_message, parse_mode=PARSE_MODE, wait_for_result=True)
            debug(f"[SEND_FILE] safe_reply returned: {reply_result}")
            debug(f"[SEND_FILE] ✅ Success message sent")
        except asyncio.TimeoutError:
            error(f"[SEND_FILE] ❌ Timeout sending success message (waited 5 minutes)")
            raise
        except Exception as reply_error:
            error(f"[SEND_FILE] ❌ Error sending success message: {reply_error}")
            error(f"[SEND_FILE] Reply error type: {type(reply_error).__name__}")
            raise

        # Solo mostrar botones de acción si se solicita (para descargas de URLs) y solo para video/audio
        debug(f"[SEND_FILE] Checking if action buttons should be shown...")
        debug(f"[SEND_FILE] show_action_buttons={show_action_buttons}, file_type={file_type}")
        if show_action_buttons and file_type in ["video", "audio"]:
            if file_size <= 2 * 1024 * 1024 * 1024:
                pending_files[event.id] = file_path
                buttons = [
                    [
                        Button.inline(get_text("button_send"), data=f"send:{event.id}"),
                        Button.inline(get_text("button_send_and_delete"), data=f"senddelete:{event.id}"),
                    ],
                    [Button.inline(get_text("button_only_in_server"), data=f"nosend:{event.id}")]
                ]
                await safe_reply(event, get_text("upload_asking"), buttons=buttons, parse_mode=PARSE_MODE)
            else:
                debug("[SEND_FILE] File size is too large to send via Telegram. Maximum size is 2GB")
    except Exception as e:
        error(f"[SEND_FILE] ❌ Error in handle_success: {e}")
        error(f"[SEND_FILE] Exception type: {type(e).__name__}")
        import traceback
        error(f"[SEND_FILE] Traceback: {traceback.format_exc()}")
        # Re-lanzar para que el caller pueda manejar el error
        raise


@bot.on(events.CallbackQuery(pattern=b"listcat:(.+)"))
async def handle_list_category(event):
    """Maneja los botones de categorías en /list"""
    debug(f"[LIST] handle_list_category called")

    if await check_admin_and_warn(event):
        debug(f"[LIST] User is not admin, returning")
        return

    await safe_answer(event)
    category = event.pattern_match.group(1).decode()
    debug(f"[LIST] Category selected: {category}")

    # Eliminar TODOS los mensajes anteriores de la lista (si existen)
    user_id = event.sender_id
    if user_id in list_messages:
        for msg in list_messages[user_id]:
            try:
                await safe_delete(msg)
            except:
                pass
        list_messages.pop(user_id, None)

    # También eliminar el mensaje actual (el que tiene los botones)
    await safe_delete(event)

    # Llamar directamente a la lógica de listado con la categoría
    try:
        # Mapear categorías a directorios
        category_map = {
            "all": list(DOWNLOAD_PATHS.values()) + [DOWNLOAD_PATH],
            "video": [DOWNLOAD_PATHS["video"], DOWNLOAD_PATHS["url_video"]],
            "audio": [DOWNLOAD_PATHS["audio"], DOWNLOAD_PATHS["url_audio"]],
            "photo": [DOWNLOAD_PATHS["photo"]],
            "torrent": [DOWNLOAD_PATHS["torrent"]],
            "ebook": [DOWNLOAD_PATHS["ebook"]]
        }

        directories = category_map.get(category, list(DOWNLOAD_PATHS.values()) + [DOWNLOAD_PATH])

        # Eliminar directorios duplicados
        directories = list(set(directories))

        debug(f"[LIST] Category: {category}, Directories to scan: {directories}")

        # Recopilar archivos
        files_info = []
        total_size = 0
        seen_files = set()

        for directory in directories:
            if not os.path.exists(directory):
                debug(f"[LIST] Directory does not exist: {directory}")
                continue

            dir_files = os.listdir(directory)
            debug(f"[LIST] Directory {directory} contains {len(dir_files)} items: {dir_files}")

            for filename in dir_files:
                file_path = os.path.join(directory, filename)

                # Ignorar archivos temporales y ocultos
                if filename.startswith('.') or '_thumb.jpg' in filename:
                    continue

                # Evitar duplicados
                if file_path in seen_files:
                    continue
                seen_files.add(file_path)

                try:
                    is_directory = os.path.isdir(file_path)

                    if is_directory:
                        # Es una carpeta
                        file_size = get_directory_size(file_path)
                        icon = "📁"
                        item_type = "folder"
                    else:
                        # Es un archivo
                        file_size = os.path.getsize(file_path)
                        file_ext = os.path.splitext(filename)[1].lower()
                        icon = get_file_icon(file_ext)
                        item_type = "file"

                    files_info.append({
                        "name": filename,
                        "size": file_size,
                        "size_formatted": format_file_size(file_size),
                        "icon": icon,
                        "path": file_path,
                        "type": item_type
                    })
                    total_size += file_size
                except Exception as e:
                    warning(f"[FILE_LIST] Error processing {filename}: {e}")

        # Ordenar alfabéticamente por nombre
        files_info.sort(key=lambda x: x["name"].lower())

        # Crear botones de categorías (excluyendo la categoría actual)
        category_buttons = get_category_buttons(exclude_category=category)

        if not files_info:
            # Mostrar mensaje vacío pero con botones para cambiar de categoría
            msg = get_text("list_empty")
            sent_msg = await safe_send_message(event.chat_id, msg, buttons=category_buttons, parse_mode=PARSE_MODE, wait_for_result=True)
            if sent_msg:
                list_messages[user_id] = [sent_msg]
            return

        # Construir mensajes (partiendo si es necesario)
        MAX_MESSAGE_LENGTH = 3800

        total_size_formatted = format_file_size(total_size)
        header = f"📂 **Archivos en el servidor**\n\n"

        # Contar archivos y carpetas
        file_count = sum(1 for item in files_info if item["type"] == "file")
        folder_count = sum(1 for item in files_info if item["type"] == "folder")

        if folder_count > 0:
            footer = f"\n\n{get_text('total_files_folders_space', file_count, folder_count, total_size_formatted)}"
        else:
            footer = f"\n\n{get_text('total_files_space', file_count, total_size_formatted)}"

        messages = []
        current_message = ""

        for i, file_info in enumerate(files_info, 1):
            # Truncar nombre si es muy largo
            name = file_info["name"]
            display_name = name
            if len(name) > 40:
                display_name = name[:37] + "..."

            # Crear entrada de archivo o carpeta (sin mostrar la ruta)
            file_entry = f"{i}. {file_info['icon']} `{display_name}`\n   💾 {file_info['size_formatted']}"

            # Calcular longitud del mensaje con header y footer
            test_message = header + current_message + "\n\n" + file_entry + footer

            if len(test_message) > MAX_MESSAGE_LENGTH and current_message:
                # Guardar mensaje actual y empezar uno nuevo
                final_message = header + current_message + footer
                messages.append(final_message)
                current_message = file_entry
            else:
                # Agregar al mensaje actual
                if current_message:
                    current_message += "\n\n" + file_entry
                else:
                    current_message = file_entry

        # Agregar el último mensaje
        if current_message:
            final_message = header + current_message + footer
            messages.append(final_message)

        # Enviar mensaje principal con lista de archivos
        sent_messages = []
        for idx, msg in enumerate(messages, 1):
            if len(messages) > 1:
                # Si hay múltiples mensajes, agregar indicador de página
                msg = msg.replace("📂 **Archivos en el servidor**", f"📂 **Archivos en el servidor** (Parte {idx}/{len(messages)})")

            # Solo agregar botones de categorías al último mensaje
            buttons = category_buttons if idx == len(messages) else None
            sent_msg = await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, wait_for_result=True)
            if sent_msg:
                sent_messages.append(sent_msg)

        # Guardar todos los mensajes enviados para poder borrarlos después
        if sent_messages:
            list_messages[user_id] = sent_messages

    except Exception as e:
        error(f"[FILE_LIST] Error listing files: {e}")
        await safe_send_message(event.chat_id, get_text("error_list_files"), parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"managecat:(.+)"))
async def handle_manage_category(event):
    """Maneja los botones de categorías en /manage"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    category = event.pattern_match.group(1).decode()

    # Eliminar TODOS los mensajes anteriores de la lista (si existen)
    user_id = event.sender_id
    if user_id in list_messages:
        for msg in list_messages[user_id]:
            try:
                await safe_delete(msg)
            except:
                pass
        list_messages.pop(user_id, None)

    # También eliminar el mensaje actual (el que tiene los botones)
    await safe_delete(event)

    try:
        # Parsear el comando para obtener la categoría
        command_parts = ["/manage", category]
        category = command_parts[1] if len(command_parts) > 1 else "all"

        # Mapear categorías a directorios
        category_map = {
            "all": list(DOWNLOAD_PATHS.values()) + [DOWNLOAD_PATH],
            "video": [DOWNLOAD_PATHS["video"], DOWNLOAD_PATHS["url_video"]],
            "audio": [DOWNLOAD_PATHS["audio"], DOWNLOAD_PATHS["url_audio"]],
            "photo": [DOWNLOAD_PATHS["photo"]],
            "torrent": [DOWNLOAD_PATHS["torrent"]],
            "ebook": [DOWNLOAD_PATHS["ebook"]]
        }

        directories = category_map.get(category, list(DOWNLOAD_PATHS.values()) + [DOWNLOAD_PATH])

        # Eliminar directorios duplicados
        directories = list(set(directories))

        # Recopilar archivos
        files_info = []
        total_size = 0
        seen_files = set()
        scanned_dirs = 0
        total_items = 0

        for directory in directories:
            if not os.path.exists(directory):
                continue

            dir_files = os.listdir(directory)
            scanned_dirs += 1
            total_items += len(dir_files)

            for filename in dir_files:
                file_path = os.path.join(directory, filename)

                # Ignorar archivos temporales y ocultos
                if filename.startswith('.') or '_thumb.jpg' in filename:
                    continue

                # Evitar duplicados
                if file_path in seen_files:
                    continue
                seen_files.add(file_path)

                try:
                    is_directory = os.path.isdir(file_path)

                    if is_directory:
                        # Es una carpeta
                        file_size = get_directory_size(file_path)
                        icon = "📁"
                        item_type = "folder"
                    else:
                        # Es un archivo
                        file_size = os.path.getsize(file_path)
                        file_ext = os.path.splitext(filename)[1].lower()
                        icon = get_file_icon(file_ext)
                        item_type = "file"

                    files_info.append({
                        "name": filename,
                        "size": file_size,
                        "size_formatted": format_file_size(file_size),
                        "icon": icon,
                        "path": file_path,
                        "type": item_type
                    })
                    total_size += file_size
                except Exception as e:
                    warning(f"[FILE_MANAGE] Error processing {filename}: {e}")

        # Log consolidado del escaneo
        debug(f"[MANAGE] Category '{category}': scanned {scanned_dirs} directories, found {len(files_info)} items ({total_items} total including hidden/temp)")

        # Ordenar alfabéticamente por nombre
        files_info.sort(key=lambda x: x["name"].lower())

        # Crear botones de categorías (excluyendo la categoría actual)
        categories = get_available_categories()
        category_buttons = []
        row = []
        for cat_id, cat_name in categories:
            if cat_id != category:  # No mostrar la categoría actual
                row.append(Button.inline(cat_name, data=f"managecat:{cat_id}"))
                if len(row) == 3:
                    category_buttons.append(row)
                    row = []

        # Agregar última fila si quedaron botones
        if row:
            category_buttons.append(row)

        # Agregar botón de cerrar
        category_buttons.append([Button.inline("❌ Cerrar", data="close")])

        if not files_info:
            msg = get_text("manage_no_files")
            sent_msg = await safe_send_message(event.chat_id, msg, buttons=category_buttons, parse_mode=PARSE_MODE, wait_for_result=True)
            if sent_msg:
                list_messages[user_id] = [sent_msg]
            return

        # Limitar a 80 items para no saturar (Telegram tiene límite de 100 botones)
        # 80 archivos + ~5 botones de categorías + 1 botón cerrar = ~86 botones (margen de seguridad)
        if len(files_info) > 80:
            file_count = sum(1 for item in files_info if item["type"] == "file")
            folder_count = sum(1 for item in files_info if item["type"] == "folder")

            msg = f"{get_text('manage_too_many_items')}\n\n"
            if folder_count > 0:
                msg += f"{get_text('manage_too_many_files_folders', file_count, folder_count)}\n\n"
            else:
                msg += f"{get_text('manage_too_many_files', file_count)}\n\n"
            msg += get_text('manage_too_many_hint')

            sent_msg = await safe_send_message(event.chat_id, msg, buttons=category_buttons, parse_mode=PARSE_MODE, wait_for_result=True)
            if sent_msg:
                list_messages[user_id] = [sent_msg]
            return

        # Crear botones de acción para cada archivo/carpeta
        file_buttons = []
        for i, file_info in enumerate(files_info, 1):
            # Guardar archivo/carpeta en el diccionario para acciones posteriores
            file_id = f"{i}_{abs(hash(file_info['path'])) % 100000}"
            pending_file_actions[file_id] = file_info["path"]

            # Truncar nombre si es muy largo
            name = file_info["name"]
            button_label = f"{i}. {file_info['icon']} {name[:25]}..." if len(name) > 25 else f"{i}. {file_info['icon']} {name}"
            file_buttons.append([Button.inline(button_label, data=f"fileact:{file_id}")])

        # Agregar botones de navegación al final
        # Agregar los botones de categorías (excluyendo la actual)
        file_buttons.extend(category_buttons)

        # Mensaje con lista de archivos y carpetas
        total_size_formatted = format_file_size(total_size)
        file_count = sum(1 for item in files_info if item["type"] == "file")
        folder_count = sum(1 for item in files_info if item["type"] == "folder")

        msg = f"{get_text('manage_files_title')}\n\n"
        if folder_count > 0:
            msg += f"{get_text('total_files_folders_space', file_count, folder_count, total_size_formatted)}\n\n"
        else:
            msg += f"{get_text('total_files_space', file_count, total_size_formatted)}\n\n"
        msg += get_text('manage_select_item')

        sent_msg = await safe_reply(event, msg, buttons=file_buttons, parse_mode=PARSE_MODE, wait_for_result=True)

        # Guardar el mensaje enviado para poder borrarlo después
        if sent_msg:
            list_messages[user_id] = [sent_msg]

    except Exception as e:
        error(f"[FILE_MANAGE] Error managing files: {e}")
        await safe_reply(event, get_text("error_manage_files"), parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"fileact:(.+)"))
async def handle_file_action(event):
    """Muestra opciones de acción para un archivo específico"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_item_not_found"), parse_mode=PARSE_MODE)
        return

    filename = os.path.basename(file_path)
    is_directory = os.path.isdir(file_path)

    if is_directory:
        # Es una carpeta
        file_size_bytes = get_directory_size(file_path)
        file_size = format_file_size(file_size_bytes)
        icon = "📁"
        item_type = "carpeta"
    else:
        # Es un archivo
        file_size_bytes = os.path.getsize(file_path)
        file_size = format_file_size(file_size_bytes)
        file_ext = os.path.splitext(filename)[1].lower()
        icon = get_file_icon(file_ext)
        item_type = "archivo"

    # Crear mensaje con información del elemento
    msg = f"{get_text('file_actions_title', item_type)}\n\n"
    msg += f"{icon} {get_text('file_actions_name', filename)}\n"
    msg += f"{get_text('file_actions_size', file_size)}\n"
    msg += f"{get_text('file_actions_path', os.path.dirname(file_path))}\n\n"
    msg += get_text('file_actions_what_to_do')

    # Botones de acción
    buttons = []

    # Primera fila: Renombrar y Eliminar
    buttons.append([
        Button.inline("✏️ Renombrar", data=f"rename:{file_id}"),
        Button.inline("🗑️ Eliminar", data=f"delete:{file_id}"),
    ])

    # Segunda fila: Descargar (solo para archivos menores de 2GB)
    if not is_directory:
        MAX_TELEGRAM_SIZE = 2 * 1024 * 1024 * 1024  # 2GB en bytes
        if file_size_bytes < MAX_TELEGRAM_SIZE:
            buttons.append([Button.inline("📥 Descargar a Telegram", data=f"download:{file_id}")])

        # Botón de descomprimir si es un archivo comprimido
        if is_compressed_file(file_path):
            buttons.append([Button.inline("📦 Descomprimir", data=f"extract:{file_id}")])

    # Última fila: Volver y Cerrar
    buttons.append([
        Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
        Button.inline(get_text("button_close"), data="close")
    ])

    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"download:(.+)"))
async def handle_download_file(event):
    """Descarga un archivo del servidor y lo envía a Telegram"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_file_not_found"), parse_mode=PARSE_MODE)
        return

    filename = os.path.basename(file_path)
    file_size_bytes = os.path.getsize(file_path)

    # Verificar tamaño
    MAX_TELEGRAM_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
    if file_size_bytes >= MAX_TELEGRAM_SIZE:
        await safe_edit(event, get_text("error_file_too_large"), parse_mode=PARSE_MODE)
        return

    # Crear un NUEVO mensaje para el progreso de envío (no editar el existente)
    # Esto permite que el usuario abra otros /manage sin borrar el progreso
    sending_msg = await safe_send_message(
        event.chat_id,
        get_text("sending", filename),
        parse_mode=PARSE_MODE,
        wait_for_result=True
    )

    # Si no se pudo crear el mensaje, usar el evento original
    if sending_msg is None:
        sending_msg = event

    try:
        debug(f"[SEND /manage] Preparing file send: {filename}")

        # Determinar si es video para agregar atributos
        file_ext = os.path.splitext(filename)[1].lower()
        is_video = file_ext in EXTENSIONS_VIDEO
        is_audio = file_ext in EXTENSIONS_AUDIO

        debug(f"[SEND /manage] File type: {'video' if is_video else 'audio' if is_audio else 'document'}")

        attributes = [DocumentAttributeFilename(file_name=filename)]
        thumb_path = None
        original_file_path = file_path  # Guardar ruta original
        converted_file_path = None  # Para rastrear si se creó un archivo convertido

        if is_video:
            debug(f"[SEND /manage] Starting video conversion...")
            # Convertir el video a formato compatible con Telegram antes de enviarlo
            converted_file_path = await convert_video_to_telegram_compatible(file_path, sending_msg)

            # Si la conversión fue cancelada (retorna None), salir
            if converted_file_path is None:
                debug("[SEND /manage] ❌ Conversion cancelled, aborting send")
                # El mensaje ya fue actualizado por el handler de cancelación
                # No necesitamos hacer nada más, solo salir
                return

            # Si la conversión creó un archivo diferente, usarlo para enviar
            if converted_file_path != file_path:
                debug(f"[SEND /manage] Using converted file: {converted_file_path}")
                file_path = converted_file_path
                # Actualizar filename para que muestre .mp4 en lugar de la extensión original
                filename = os.path.splitext(filename)[0] + ".mp4"
                debug(f"[SEND /manage] Name updated to: {filename}")
                # Actualizar el atributo de nombre de archivo
                attributes = [DocumentAttributeFilename(file_name=filename)]
            else:
                debug(f"[SEND /manage] Video already compatible, using original")

            # Obtener metadatos del video
            debug(f"[SEND /manage] Getting video metadata...")
            duration, width, height = await get_video_metadata(file_path)
            if duration and width and height:
                debug(f"[SEND /manage] Metadata: {duration}s, {width}x{height}")
                from telethon.tl.types import DocumentAttributeVideo
                attributes.append(DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True
                ))
            else:
                debug(f"[SEND /manage] ⚠️ Could not get video metadata")

            # Generar thumbnail
            debug(f"[SEND /manage] Generating thumbnail...")
            thumb_path = await generate_video_thumbnail(file_path)
            if thumb_path:
                debug(f"[SEND /manage] Thumbnail generated: {thumb_path}")
            else:
                debug(f"[SEND /manage] ⚠️ Could not generate thumbnail")

        elif is_audio:
            debug(f"[SEND /manage] Getting audio metadata...")
            # Obtener metadatos del audio
            duration, _, _ = await get_video_metadata(file_path)
            if duration:
                debug(f"[SEND /manage] Audio duration: {duration}s")
                from telethon.tl.types import DocumentAttributeAudio
                attributes.append(DocumentAttributeAudio(
                    duration=duration
                ))
            else:
                debug(f"[SEND /manage] ⚠️ Could not get audio duration")

        # Crear callback de progreso para el envío
        upload_progress = create_upload_progress_callback(sending_msg, filename)

        debug(f"[SEND /manage] Starting send to Telegram...")
        file_size = os.path.getsize(file_path)
        debug(f"[SEND /manage] File size: {file_size} bytes")

        # Enviar archivo con progreso
        # NO usar wait_for_result=True para no bloquear el event loop
        # Esto permite que el bot siga respondiendo a otros comandos mientras envía
        await bot.send_file(
            event.chat_id,
            file_path,
            attributes=attributes,
            thumb=thumb_path if thumb_path else None,
            supports_streaming=True if is_video else None,
            progress_callback=upload_progress
        )

        debug(f"[SEND /manage] ✅ File sent successfully")

        # Limpiar thumbnail temporal
        if thumb_path and os.path.exists(thumb_path):
            try:
                debug(f"[SEND /manage] Deleting temporary thumbnail: {thumb_path}")
                os.remove(thumb_path)
            except Exception as e:
                warning(f"[SEND /manage] ⚠️ Error deleting thumbnail: {e}")

        # Limpiar archivo convertido temporal si se generó (diferente del original)
        if converted_file_path and converted_file_path != original_file_path and os.path.exists(converted_file_path):
            try:
                debug(f"[SEND /manage] Deleting temporary converted file: {converted_file_path}")
                os.remove(converted_file_path)
                debug(f"[SEND /manage] ✅ Temporary converted file deleted")
            except Exception as e:
                warning(f"[SEND /manage] ⚠️ Error deleting temporary converted file: {e}")

        # Eliminar mensaje de progreso
        if sending_msg and sending_msg != event:
            await safe_delete(sending_msg)

        # Mensaje de éxito
        msg = get_text("file_sent_success", filename)

        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
            Button.inline(get_text("button_close"), data="close")
        ]]

        # Si sending_msg es el evento original, editar; si no, enviar nuevo mensaje
        if sending_msg == event:
            await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)
        else:
            await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE)

    except Exception as e:
        error(f"[ENVÍO /manage] ❌ Error enviando archivo {file_path}: {e}")

        # Limpiar thumbnail temporal si se generó
        if thumb_path and os.path.exists(thumb_path):
            try:
                debug(f"[SEND /manage] Deleting thumbnail after error: {thumb_path}")
                os.remove(thumb_path)
            except Exception as cleanup_error:
                warning(f"[SEND /manage] ⚠️ Error deleting thumbnail after error: {cleanup_error}")

        # Limpiar archivo convertido temporal si se generó (diferente del original)
        if converted_file_path and converted_file_path != original_file_path and os.path.exists(converted_file_path):
            try:
                debug(f"[SEND /manage] Deleting converted file after error: {converted_file_path}")
                os.remove(converted_file_path)
                debug(f"[SEND /manage] ✅ Temporary converted file deleted after error")
            except Exception as cleanup_error:
                warning(f"[SEND /manage] ⚠️ Error deleting temporary converted file after error: {cleanup_error}")

        # Eliminar mensaje de progreso si existe
        if sending_msg and sending_msg != event:
            try:
                await safe_delete(sending_msg)
            except:
                pass

        # Mostrar error
        if sending_msg == event:
            await safe_edit(event, get_text("error_sending_file", str(e)), parse_mode=PARSE_MODE)
        else:
            await safe_send_message(event.chat_id, get_text("error_sending_file", str(e)), parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"close"))
async def handle_close(event):
    """Cierra/elimina el mensaje actual y todos los mensajes relacionados de la lista"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)

    # Eliminar TODOS los mensajes de la lista (si existen)
    user_id = event.sender_id
    if user_id in list_messages:
        for msg in list_messages[user_id]:
            try:
                await safe_delete(msg)
            except:
                pass
        list_messages.pop(user_id, None)

    # También eliminar el mensaje actual
    await safe_delete(event)

@bot.on(events.CallbackQuery(pattern=b"delete:(.+)"))
async def handle_delete_file(event):
    """Confirma y elimina un archivo o carpeta"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_item_not_found_short"), parse_mode=PARSE_MODE)
        return

    filename = os.path.basename(file_path)
    is_directory = os.path.isdir(file_path)
    item_type = "carpeta" if is_directory else "archivo"

    if is_directory:
        icon = "📁"
    else:
        file_ext = os.path.splitext(filename)[1].lower()
        icon = get_file_icon(file_ext)

    # Pedir confirmación
    msg = f"{get_text('confirm_delete_title')}\n\n"
    msg += f"{get_text('confirm_delete_question', item_type)}\n\n"
    msg += f"{icon} `{filename}`\n\n"
    if is_directory:
        msg += f"{get_text('confirm_delete_folder_warning')}\n\n"
    msg += get_text('confirm_delete_no_undo')

    buttons = [
        [
            Button.inline(get_text("button_yes_delete"), data=f"confirmdelete:{file_id}"),
            Button.inline(get_text("button_cancel"), data=f"fileact:{file_id}"),
        ]
    ]

    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"confirmdelete:(.+)"))
async def handle_confirm_delete(event):
    """Elimina el archivo o carpeta confirmado"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_item_not_found_short"), parse_mode=PARSE_MODE)
        return

    filename = os.path.basename(file_path)
    is_directory = os.path.isdir(file_path)
    item_type = "carpeta" if is_directory else "archivo"
    icon = "📁" if is_directory else "📄"

    try:
        if is_directory:
            # Eliminar carpeta y todo su contenido
            shutil.rmtree(file_path)
        else:
            # Eliminar archivo
            os.remove(file_path)

        pending_file_actions.pop(file_id, None)

        msg = f"{get_text('item_deleted_title', item_type.capitalize())}\n\n"
        msg += f"{icon} `{filename}`\n\n"
        msg += get_text('item_deleted_desc', item_type)

        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

        debug(f"[FILE_DELETE] {item_type.capitalize()} deleted by user: {file_path}")
    except Exception as e:
        error(f"[FILE_DELETE] Error deleting {item_type} {file_path}: {e}")
        await safe_edit(event, get_text("error_deleting_item", item_type, str(e)), parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"rename:(.+)"))
async def handle_rename_file(event):
    """Inicia el proceso de renombrado de archivo o carpeta"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_item_not_found_short"), parse_mode=PARSE_MODE)
        return

    filename = os.path.basename(file_path)
    is_directory = os.path.isdir(file_path)
    item_type = "carpeta" if is_directory else "archivo"
    icon = "📁" if is_directory else "📄"

    # Guardar en pending_renames para capturar el siguiente mensaje
    # Usamos una lista para permitir múltiples renombrados simultáneos
    if event.sender_id not in pending_renames:
        pending_renames[event.sender_id] = []

    pending_renames[event.sender_id].append({
        "file_id": file_id,
        "file_path": file_path,
        "original_name": filename,
        "message": event,  # Guardar el mensaje para borrarlo después
        "is_directory": is_directory
    })

    msg = f"{get_text('rename_title', item_type)}\n\n"
    msg += f"{icon} {get_text('rename_current_name', filename)}\n\n"
    msg += get_text('rename_reply_with_new_name', item_type)
    if not is_directory:
        msg += get_text('rename_include_extension')
    msg += f".\n\n"
    msg += get_text('rename_example', 'my_new_folder' if is_directory else 'my_new_video.mp4')

    buttons = [[Button.inline(get_text("button_cancel"), data=f"fileact:{file_id}")]]

    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.NewMessage(func=lambda e: e.sender_id in pending_renames and len(pending_renames.get(e.sender_id, [])) > 0 and not e.raw_text.startswith('/')))
async def handle_rename_input(event):
    """Captura el nuevo nombre del archivo o carpeta y lo renombra"""
    if not is_admin(event.sender_id):
        return

    # Obtener la lista de renombrados pendientes del usuario
    user_renames = pending_renames.get(event.sender_id, [])
    if not user_renames:
        return

    # Buscar el renombrado que corresponde al mensaje al que está respondiendo
    rename_data = None
    rename_index = None

    # Si el usuario está respondiendo a un mensaje, buscar ese mensaje específico
    if event.reply_to_msg_id:
        for i, data in enumerate(user_renames):
            if data.get("message") and data["message"].id == event.reply_to_msg_id:
                rename_data = data
                rename_index = i
                break

    # Si no está respondiendo o no se encontró, tomar el primero (FIFO)
    if rename_data is None:
        rename_data = user_renames[0]
        rename_index = 0

    # Eliminar el renombrado de la lista
    user_renames.pop(rename_index)

    # Si no quedan más renombrados pendientes, eliminar la entrada del usuario
    if not user_renames:
        pending_renames.pop(event.sender_id, None)

    file_path = rename_data["file_path"]
    file_id = rename_data["file_id"]
    original_name = rename_data["original_name"]
    rename_message = rename_data.get("message")  # Mensaje de "Renombrar archivo/carpeta"
    is_directory = rename_data.get("is_directory", False)
    item_type = "carpeta" if is_directory else "archivo"
    icon = "📁" if is_directory else "📄"
    new_name = event.raw_text.strip()

    # Borrar el mensaje del usuario con el nuevo nombre
    try:
        await event.delete()
    except:
        pass  # Si no se puede borrar, continuar de todos modos

    # Borrar el mensaje de "Renombrar archivo/carpeta"
    if rename_message:
        try:
            await safe_delete(rename_message)
        except:
            pass  # Si no se puede borrar, continuar de todos modos

    # Validar el nuevo nombre
    if not new_name or '/' in new_name or '\\' in new_name:
        msg = f"{get_text('rename_invalid_title')}\n\n"
        msg += f"{get_text('rename_invalid_chars')}\n\n"
        msg += get_text('rename_try_again')
        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_reply(event, msg, buttons=buttons, parse_mode=PARSE_MODE)
        return

    # Verificar que el elemento aún existe
    if not os.path.exists(file_path):
        await safe_reply(event, get_text("error_type_not_found", item_type.capitalize(), item_type), parse_mode=PARSE_MODE)
        return

    # Construir nueva ruta
    directory = os.path.dirname(file_path)
    new_path = os.path.join(directory, new_name)

    # Verificar si ya existe un elemento con ese nombre
    if os.path.exists(new_path):
        msg = f"{get_text('rename_already_exists_title', item_type)}\n\n"
        msg += f"{get_text('rename_already_exists_desc', item_type, new_name)}\n\n"
        msg += get_text('rename_choose_another')
        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_reply(event, msg, buttons=buttons, parse_mode=PARSE_MODE)
        return

    try:
        # Renombrar el archivo o carpeta
        os.rename(file_path, new_path)

        # Actualizar en pending_file_actions
        pending_file_actions[file_id] = new_path

        msg = f"{get_text('rename_success_title', item_type.capitalize())}\n\n"
        msg += f"{icon} {get_text('rename_old_name', original_name)}\n"
        msg += f"{icon} {get_text('rename_new_name', new_name)}\n\n"
        msg += get_text('rename_success_desc', item_type)

        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_reply(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

        debug(f"[FILE_RENAME] File renamed: {file_path} → {new_path}")
    except Exception as e:
        error(f"[FILE_RENAME] Error renaming file {file_path}: {e}")
        msg = f"{get_text('rename_error_title')}\n\n"
        msg += get_text('rename_error_desc', str(e))
        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_reply(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"extract:(.+)"))
async def handle_extract_file(event):
    """Descomprime un archivo comprimido"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_file_not_found"), parse_mode=PARSE_MODE)
        return

    filename = os.path.basename(file_path)

    # Verificar que sea un archivo comprimido
    if not is_compressed_file(file_path):
        await safe_edit(event, get_text("error_not_compressed"), parse_mode=PARSE_MODE)
        return

    # Verificar si es un ZIP split (no soportado)
    if is_split_zip(filename):
        await safe_edit(event, get_text("error_split_zip_not_supported"), parse_mode=PARSE_MODE)
        return

    # Determinar la carpeta de extracción
    download_path = os.path.dirname(file_path)
    base_name = os.path.splitext(file_path)[0]

    # Para archivos RAR, limpiar el nombre base
    if rarfile.is_rarfile(file_path):
        base_name = clean_rar_base_name(filename)

    extracted_path = os.path.join(download_path, os.path.basename(base_name))

    # Verificar si la carpeta de destino ya existe
    if os.path.exists(extracted_path):
        await safe_edit(event, get_text("error_extraction_folder_exists", os.path.basename(extracted_path)), parse_mode=PARSE_MODE)
        return

    # Crear carpeta de extracción
    os.makedirs(extracted_path, exist_ok=True)

    # Mostrar mensaje de progreso inicial
    progress_msg = await safe_edit(event, get_text("decompressing_file", filename), parse_mode=PARSE_MODE, wait_for_result=True)

    debug(f"[EXTRACT] Starting extraction of {filename} to {extracted_path}")

    # Extraer archivo en un executor para no bloquear (archivos grandes pueden tardar mucho)
    loop = asyncio.get_event_loop()

    # Tarea para actualizar progreso cada 10 segundos
    extraction_done = asyncio.Event()

    async def update_progress():
        """Actualiza el mensaje cada 10 segundos para mostrar que sigue extrayendo"""
        elapsed = 0
        while not extraction_done.is_set():
            await asyncio.sleep(10)
            if not extraction_done.is_set():
                elapsed += 10
                try:
                    await safe_edit(
                        progress_msg,
                        get_text('decompressing_file_progress', filename, elapsed),
                        parse_mode=PARSE_MODE
                    )
                    debug(f"[EXTRACT] Still extracting {filename} ({elapsed}s elapsed)")
                except Exception as e:
                    debug(f"[EXTRACT] Error updating progress message: {e}")

    # Iniciar tarea de progreso
    progress_task = asyncio.create_task(update_progress())

    try:
        # Ejecutar extracción en thread pool
        extract_result = await loop.run_in_executor(None, extract_file, file_path, extracted_path)
        debug(f"[EXTRACT] Extraction completed for {filename}, result: {extract_result}")
    finally:
        # Detener tarea de progreso
        extraction_done.set()
        await progress_task

    # Usar función unificada para mensajes y botones
    msg, buttons = get_extraction_message_and_buttons(
        extract_result,
        filename,
        extracted_path,
        file_path,
        file_id=file_id,
        from_manage=True
    )

    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

    if extract_result == True:
        debug(f"[EXTRACT] File {filename} - Extracted to {extracted_path}")

@bot.on(events.CallbackQuery(pattern=b"(delcompressed|keepcompressed):(.+)"))
async def handle_compressed_file_action(event):
    """Maneja la acción de eliminar o conservar el archivo comprimido después de extraer"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    action = event.pattern_match.group(1).decode()
    file_id = event.pattern_match.group(2).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_file_not_found"), parse_mode=PARSE_MODE)
        return

    filename = os.path.basename(file_path)

    if action == "delcompressed":
        # Eliminar el archivo comprimido
        try:
            # Si es un archivo RAR multi-parte, eliminar todas las partes
            if rarfile.is_rarfile(file_path):
                dirname = os.path.dirname(file_path)
                filename_lower = filename.lower()
                rar_patterns = [
                    r"(.*)\.part\d+\.rar$",
                    r"(.*)\.r\d{2}$",
                    r"(.*)\.rar$"
                ]

                matched_base = None
                for pattern in rar_patterns:
                    m = re.match(pattern, filename_lower)
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
                        os.remove(part)
                        debug(f"[FILE_DELETE] File {part} - Deleted")

                    msg = f"{get_text('files_deleted_title')}\n\n"
                    msg += get_text('files_deleted_count', len(all_parts))
                else:
                    os.remove(file_path)
                    msg = f"{get_text('file_deleted_title')}\n\n"
                    msg += f"📄 `{filename}`"
                    debug(f"[FILE_DELETE] File {file_path} - Deleted")
            else:
                os.remove(file_path)
                msg = f"{get_text('file_deleted_title')}\n\n"
                msg += f"📄 `{filename}`"
                debug(f"[FILE_DELETE] File {file_path} - Deleted")

            pending_file_actions.pop(file_id, None)

        except Exception as e:
            error(f"[FILE_DELETE] Error deleting compressed file {file_path}: {e}")
            msg = f"{get_text('delete_error_title')}\n\n"
            msg += get_text('delete_error_desc', str(e))

    else:  # keepcompressed
        msg = f"{get_text('file_kept_title')}\n\n"
        msg += f"📄 `{filename}`\n\n"
        msg += get_text('file_kept_desc')

    buttons = [[
        Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
        Button.inline(get_text("button_close"), data="close")
    ]]
    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"(send|senddelete|nosend):(.+)"))
async def handle_send_choice(event):
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    action = event.pattern_match.group(1).decode()
    file_id = int(event.pattern_match.group(2).decode())
    file_path = pending_files.get(file_id)

    if not os.path.exists(file_path):
        await safe_edit(event, get_text("error_file_does_not_exist_user"), parse_mode=PARSE_MODE)
        error(f"[FILE_DELETE] The file does not exist. Path: {file_path}")
        return

    if action in ("send", "senddelete"):
        try:
            sending_msg = await safe_edit(
                event,
                get_text("sending", os.path.basename(file_path)),
                parse_mode=PARSE_MODE,
                wait_for_result=True
            )

            # Si no se pudo editar el mensaje (timeout en cola), crear uno nuevo
            if sending_msg is None:
                sending_msg = await safe_reply(
                    event,
                    get_text("sending", os.path.basename(file_path)),
                    parse_mode=PARSE_MODE,
                    wait_for_result=False
                )

            debug(f"[SEND BUTTON] Preparing file send: {file_path}")

            # Detectar si es un video y obtener metadatos
            is_video = file_path.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv'))
            thumb_path = None
            original_file_path = file_path  # Guardar ruta original
            converted_file_path = None  # Para rastrear si se creó un archivo convertido

            debug(f"[SEND BUTTON] File type: {'video' if is_video else 'document'}")

            # Inicializar attributes con el nombre del archivo original
            original_filename = os.path.basename(file_path)
            display_filename = original_filename  # Por defecto, usar el nombre original
            attributes = [DocumentAttributeFilename(file_name=original_filename)]

            if is_video:
                debug(f"[SEND BUTTON] Starting video conversion...")
                # Convertir el video a formato compatible con Telegram antes de enviarlo
                converted_file_path = await convert_video_to_telegram_compatible(file_path, sending_msg)

                # Si la conversión fue cancelada (retorna None), salir
                if converted_file_path is None:
                    debug("[SEND BUTTON] ❌ Conversion cancelled, aborting send")
                    if sending_msg:
                        await safe_delete(sending_msg)
                    return

                # Si la conversión creó un archivo diferente, usarlo para enviar
                if converted_file_path != file_path:
                    debug(f"[SEND BUTTON] Using converted file: {converted_file_path}")
                    file_path = converted_file_path
                    # Actualizar el nombre del archivo a .mp4 (sin _telegram)
                    display_filename = os.path.splitext(original_filename)[0] + ".mp4"
                    debug(f"[SEND BUTTON] Name updated to: {display_filename}")
                    attributes = [DocumentAttributeFilename(file_name=display_filename)]
                else:
                    debug(f"[SEND BUTTON] Video already compatible, using original")

                debug(f"[SEND BUTTON] Getting video metadata...")
                duration, width, height = await get_video_metadata(file_path)
                if duration and width and height:
                    debug(f"[SEND BUTTON] Metadata: {duration}s, {width}x{height}")
                    from telethon.tl.types import DocumentAttributeVideo
                    attributes.append(DocumentAttributeVideo(
                        duration=duration,
                        w=width,
                        h=height,
                        supports_streaming=True
                    ))
                else:
                    debug(f"[SEND BUTTON] ⚠️ Could not get video metadata")

                # Generar thumbnail del video
                debug(f"[SEND BUTTON] Generating thumbnail...")
                thumb_path = await generate_video_thumbnail(file_path)
                if thumb_path:
                    debug(f"[SEND BUTTON] Thumbnail generated: {thumb_path}")
                else:
                    debug(f"[SEND BUTTON] ⚠️ Could not generate thumbnail")

            # Crear callback de progreso para el envío
            upload_progress = create_upload_progress_callback(sending_msg, display_filename)

            debug(f"[SEND BUTTON] Starting send to Telegram...")
            file_size = os.path.getsize(file_path)
            debug(f"[SEND BUTTON] File size: {file_size} bytes")

            # NO usar wait_for_result=True para no bloquear el event loop
            # Esto permite que el bot siga respondiendo a otros comandos mientras envía
            message = await bot.send_file(
                event.chat_id,
                file_path,
                attributes=attributes,
                thumb=thumb_path if thumb_path else None,
                supports_streaming=True if is_video else None,
                progress_callback=upload_progress
            )

            debug(f"[SEND BUTTON] ✅ File sent successfully")

            # Limpiar thumbnail temporal si se generó
            if thumb_path and os.path.exists(thumb_path):
                try:
                    debug(f"[SEND BUTTON] Deleting temporary thumbnail: {thumb_path}")
                    os.remove(thumb_path)
                except Exception as e:
                    warning(f"[SEND BUTTON] ⚠️ Error deleting temporary thumbnail: {e}")

            # Limpiar archivo convertido temporal si se generó (diferente del original)
            if converted_file_path and converted_file_path != original_file_path and os.path.exists(converted_file_path):
                try:
                    debug(f"[SEND BUTTON] Deleting temporary converted file: {converted_file_path}")
                    os.remove(converted_file_path)
                    debug(f"[SEND BUTTON] ✅ Temporary converted file deleted")
                except Exception as e:
                    warning(f"[SEND BUTTON] ⚠️ Error deleting temporary converted file: {e}")

            debug(f"[SEND BUTTON] ✅ File sent to Telegram: {original_file_path}")
            if sending_msg:
                await safe_delete(sending_msg)

            if action == "senddelete":
                # Eliminar el archivo ORIGINAL, no el convertido
                debug(f"[SEND BUTTON] Deleting original file from server: {original_file_path}")
                os.remove(original_file_path)
                debug(f"[SEND BUTTON] ✅ File sent to Telegram and deleted from server: {original_file_path}")
                await safe_respond(event, get_text("deleted_from_server"), reply_to=message.id, parse_mode=PARSE_MODE)
        except Exception as e:
            error(f"[ENVÍO BOTÓN] ❌ Error enviando archivo {file_path}: {e}")

            # Limpiar thumbnail temporal si se generó
            if thumb_path and os.path.exists(thumb_path):
                try:
                    debug(f"[SEND BUTTON] Deleting thumbnail after error: {thumb_path}")
                    os.remove(thumb_path)
                except Exception as cleanup_error:
                    warning(f"[SEND BUTTON] ⚠️ Error deleting thumbnail after error: {cleanup_error}")

            # Limpiar archivo convertido temporal si se generó (diferente del original)
            if converted_file_path and converted_file_path != original_file_path and os.path.exists(converted_file_path):
                try:
                    debug(f"[SEND BUTTON] Deleting converted file after error: {converted_file_path}")
                    os.remove(converted_file_path)
                    debug(f"[SEND BUTTON] ✅ Temporary converted file deleted after error")
                except Exception as cleanup_error:
                    warning(f"[SEND BUTTON] ⚠️ Error deleting temporary converted file after error: {cleanup_error}")

            # Eliminar mensaje de progreso si existe
            if sending_msg:
                try:
                    await safe_delete(sending_msg)
                except Exception as delete_error:
                    warning(f"[SEND BUTTON] ⚠️ Error deleting progress message: {delete_error}")

            await safe_reply(event, get_text("error_sending_the_file_user"), parse_mode=PARSE_MODE)
            error(f"[SEND BUTTON] Error sending file: {e}")
    else:
        await safe_delete(event)

async def handle_cancel(status_message):
    if status_message:
        await safe_edit(status_message, get_text("cancelled"), buttons=None, parse_mode=PARSE_MODE)
    debug("[URL_DOWNLOAD] URL download cancelled")
    cleanup_partials()

def cleanup_partials():
    pattern = os.path.join(DOWNLOAD_PATHS["url_video"], "*.part*")
    for f in glob.glob(pattern):
        debug(f"[URL_DOWNLOAD] Cleaning partial files from URL download: {f}")
        os.remove(f)

async def send_startup_message():
    admins = TELEGRAM_ADMIN.split(',')
    for admin in admins:
        try:
            await safe_send_message(int(admin), get_text("initial_message", VERSION), parse_mode=PARSE_MODE)
        except Exception as e:
            error(f"[STARTUP] Error sending initial message: {e}")

async def set_commands():
    commands = [
        BotCommand("start", get_text("menu_start")),
        BotCommand("list", get_text("menu_list")),
        BotCommand("manage", get_text("menu_manage")),
        BotCommand("version", get_text("menu_version")),
        BotCommand("donate", get_text("menu_donate")),
        BotCommand("donors", get_text("menu_donors")),
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
        warning(f"[AUTH] User {event.sender_id} (@{username}) is not an admin and tried to use the bot")
        return True
    return False

async def main():
    debug(f"[STARTUP] DropBot v{VERSION}")
    await bot.start()
    await message_queue.start()  # Iniciar la cola de mensajes
    await set_commands()
    await send_startup_message()
    try:
        await bot.run_until_disconnected()
    finally:
        await message_queue.shutdown()  # Detener la cola al finalizar

if __name__ == "__main__":
    bot.loop.run_until_complete(main())