import os
import threading
from pyrogram import Client, filters, enums
from datetime import datetime
from config import *
from translations import *
from debug import *
from basic import *
import sys
import time
import re

VERSION = "0.9.0"
     
if LANGUAGE.lower() not in ("es", "en"):
    error("LANGUAGE only can be ES/EN")
    sys.exit(1)
     
load_locale(LANGUAGE.lower())

# Comprobación inicial de variables
if not DEFAULT_DOWNLOAD_PATH or DEFAULT_DOWNLOAD_PATH == DEFAULT_EMPTY_STR:
    error(get_text("error_no_default_path"))
    sys.exit(1)

if DEFAULT_EMPTY_STR == TELEGRAM_TOKEN:
	error(get_text("error_bot_token"))
	sys.exit(1)

if DEFAULT_EMPTY_STR == TELEGRAM_ADMIN:
	error(get_text("error_bot_telegram_admin"))
	sys.exit(1)

if ANONYMOUS_USER_ID == TELEGRAM_ADMIN:
	error(get_text("error_bot_telegram_admin_anonymous"))
	sys.exit(1)

# Definir rutas de descarga con fallback a DEFAULT_DOWNLOAD_PATH si el valor es DEFAULT_EMPTY_STR o vacío
DOWNLOAD_AUDIO = DEFAULT_DOWNLOAD_PATH if not DEFAULT_DOWNLOAD_AUDIO or DEFAULT_DOWNLOAD_AUDIO == DEFAULT_EMPTY_STR else DEFAULT_DOWNLOAD_AUDIO
DOWNLOAD_VIDEO = DEFAULT_DOWNLOAD_PATH if not DEFAULT_DOWNLOAD_VIDEO or DEFAULT_DOWNLOAD_VIDEO == DEFAULT_EMPTY_STR else DEFAULT_DOWNLOAD_VIDEO
DOWNLOAD_PHOTO = DEFAULT_DOWNLOAD_PATH if not DEFAULT_DOWNLOAD_PHOTO or DEFAULT_DOWNLOAD_PHOTO == DEFAULT_EMPTY_STR else DEFAULT_DOWNLOAD_PHOTO
DOWNLOAD_DOCUMENT = DEFAULT_DOWNLOAD_PATH if not DEFAULT_DOWNLOAD_DOCUMENT or DEFAULT_DOWNLOAD_DOCUMENT == DEFAULT_EMPTY_STR else DEFAULT_DOWNLOAD_DOCUMENT
DOWNLOAD_TORRENT = DEFAULT_DOWNLOAD_PATH if not DEFAULT_DOWNLOAD_TORRENT or DEFAULT_DOWNLOAD_TORRENT == DEFAULT_EMPTY_STR else DEFAULT_DOWNLOAD_TORRENT

# Crear carpetas si no existen
for path in [DOWNLOAD_AUDIO, DOWNLOAD_VIDEO, DOWNLOAD_PHOTO, DOWNLOAD_DOCUMENT, DOWNLOAD_TORRENT]:
    os.makedirs(path, exist_ok=True)

# Instanciamos el bot de Pyrogram
app = Client("dropbot", api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH, bot_token=TELEGRAM_TOKEN)
app.set_parse_mode(enums.ParseMode.MARKDOWN)

def get_download_path(message):
    file_name = message.document.file_name if message.document else None
    file_extension = os.path.splitext(file_name)[1].lower() if file_name else ""

    if file_extension in EXTENSIONS_TORRENT:
        return DOWNLOAD_TORRENT, TOR_ICO
    elif file_extension in EXTENSIONS_VIDEO or message.video:
        return DOWNLOAD_VIDEO, VID_ICO
    elif file_extension in EXTENSIONS_AUDIO or message.audio:
        return DOWNLOAD_AUDIO, AUD_ICO
    elif file_extension in EXTENSIONS_IMAGE or message.photo:
        return DOWNLOAD_PHOTO, IMG_ICO
    elif file_extension in EXTENSIONS_DOCUMENT or message.document:
        return DOWNLOAD_DOCUMENT, DOC_ICO
    return DEFAULT_DOWNLOAD_PATH, DEF_ICO

def download_media(client, message):
    media = message.document or message.video or message.audio or message.photo
    if not media:
        return
    
    file_name = (
        media.file_name if hasattr(media, 'file_name')
        else f"{media.file_id}.jpg" if message.photo
        else f"file_{media.file_id}"
    )

    debug(get_text("debug_file_received", file_name))
    download_path, ico = get_download_path(message)
    file_path = os.path.join(download_path, file_name)
    debug(get_text("debug_file_path_selected", file_name, download_path))

    status_message = message.reply(get_text("starting_download"))
    
    client.download_media(message, file_name=file_path, progress=lambda current, total: update_progress(status_message, current, total, ico))
    
    status_message.edit(get_text("downloaded", file_path))
    debug(get_text("debug_file_downloaded", file_name))

def update_progress(status_message, current, total, ico):
    percentage = round((current / total) * 100, 1)
    status_message.edit(get_text("downloading", ico, percentage))
    time.sleep(0.5)

# Punto de entrada general del programa al recibir un fichero
@app.on_message(filters.document | filters.video | filters.audio | filters.photo)
def handle_files(client, message):
    # Comprobación de admin
    if not is_admin(message.from_user.id):
        debug(get_text("warning_not_admin", message.from_user.id, message.from_user.username))
        return

    threading.Thread(target=download_media, args=(client, message)).start()

# Punto de entrada general del programa al recibir un comando
@app.on_message(filters.command(["start", "donate", "version"]))
def handle_start(client, message):
    comando = message.text.split(' ', 1)[0]

    if not is_admin(message.from_user.id):
        debug(get_text("warning_not_admin", message.from_user.id, message.from_user.username))
        response = get_text("user_not_admin")
    elif comando == "/start":
        response = get_text("welcome_message")
    elif comando == "/donate":
        response = get_text("donate")
    elif comando == "/version":
        response = get_text("version", VERSION)

    message.reply(response)

@app.on_message(filters.text)
def handle_message(client, message):
    if not is_admin(message.from_user.id):
        debug(get_text("warning_not_admin", message.from_user.id, message.from_user.username))
        return

    if re.search(URL_PATTERN, message.text):
        message.reply(get_text("error_url"))
        return

if __name__ == "__main__":
    try:
        debug(f"DropBot v{VERSION}")
        app.run()
    except Exception as e:
        error(f"An unexpected error occurred: {e}")
        sys.exit(1)