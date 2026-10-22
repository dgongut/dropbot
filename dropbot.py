import os
import re
import sys
import errno
import asyncio
import signal
import mimetypes
import rarfile
import shutil
import stat
import tempfile
import glob
import hashlib
import ipaddress
import itertools
import secrets
import requests
import socket
import threading
import time
import json
import warnings
from urllib.parse import urlparse, unquote, urljoin, urlsplit, urlunsplit
from telethon import TelegramClient, errors, events, functions, types, Button
from telethon.errors import InvalidBufferError
from telethon.tl.types import (
    BotCommand, Document, Photo,
    DocumentAttributeFilename, DocumentAttributeVideo, DocumentAttributeAudio
)
from config import (
    AUD_ICO,
    BOO_ICO, DEF_ICO, DOWNLOAD_PATH,
    DOWNLOAD_PATHS, EXTENSIONS_AUDIO, EXTENSIONS_EBOOK, EXTENSIONS_IMAGE,
    EXTENSIONS_TORRENT, EXTENSIONS_VIDEO, EXTENSIONS_WEBPAGE, FAST_TRANSFER_MIN_BYTES,
    HEARTBEAT_FILE, HEARTBEAT_INTERVAL, IMG_ICO, LEGACY_SESSION_FILE,
    MAX_DOWNLOAD_RETRIES, MAX_TELEGRAM_FILE_SIZE, MESSAGE_QUEUE_DELAY, MESSAGE_QUEUE_MAX_RETRIES,
    POT_PROVIDER_DIR, POT_PROVIDER_PORT, POT_PROVIDER_STARTUP_TIMEOUT,
    RECONNECT_DELAY, RETRY_DELAY_SECONDS, TELEGRAM_API_HASH, TELEGRAM_API_ID,
    TELEGRAM_TOKEN, TEMP_DIR, TOR_ICO, VID_ICO,
    YTDLP_COOKIES_FILE, downloads_persistent,
)
import migration
import settings
import stats
import store
from translations import get_text, PARSE_MODE
from basic import (
    admin_ids, get_filename_from_path, is_admin, md_code, redact_url, redact_urls,
    sanitize_filename, wrong_admin_ids,
)
from message_queue import TelegramMessageQueue
from state import (
    active_tasks, cancelled_conversions, pending_files, pending_urls,
    register_pending_send, send_original_requests,
)
from utils.file_helpers import (
    format_file_size, get_file_icon,
    reserve_unique_path, copy_and_remove, move_to_unique_path,
    PARTIAL_SUFFIX, partial_name, remove_partial_files, remove_partial_extractions,
)
from utils import fast_telethon
from utils.limiter import ResizableLimiter
from utils import telegram_helpers
from utils.telegram_helpers import (
    safe_edit, safe_reply, safe_respond, safe_answer,
    safe_delete, safe_send_message, check_admin_and_warn,
    for_another_bot, get_topic, is_file_message, outlive_handler, warn_not_admin,
)
from handlers import manage as manage_handlers
from handlers import settings as settings_handlers
from handlers.manage import get_available_categories, get_category_buttons
from services.donors_service import print_donors
from services.video_service import (
    get_video_metadata, generate_video_thumbnail, format_duration,
    probe_media, telegram_conversion_plan, plan_is_noop, original_if_converted_too_large
)

# Inicializar sistema de logging profesional
import logger as log_module

# Detectar si estamos en Docker (contenedor)
IS_DOCKER = os.path.exists('/.dockerenv')

# En Docker, los logs van a stdout para `docker logs`
# En desarrollo local, se puede usar archivo con rotación
# En Docker usamos DEBUG en consola para ver toda la actividad en docker logs
logger = log_module.setup_logger(
    name='dropbot',
    log_file=None if IS_DOCKER else '/var/log/dropbot/dropbot.log',
    log_level='DEBUG',
    console_level='DEBUG' if IS_DOCKER else 'INFO',  # DEBUG en Docker para ver actividad
    file_level='DEBUG',
    max_bytes=10_485_760,  # 10MB
    backup_count=5,
    use_colors=True
)

from logger import debug, warning, error

VERSION = "4.0.1"

warnings.filterwarnings('ignore', message='Using async sessions support is an experimental feature')

# Log inicial del sistema
logger.info("=" * 60)
logger.info(f"DropBot v{VERSION} starting...")
logger.info("=" * 60)

# Antes de leer ningún ajuste: en el primer arranque de la 4.x, settings.json
# se siembra con las variables que hubiera en el docker-compose
migration.run()

# Sin token, Telethon lo pedía por teclado: sin teclado en Docker, el
# contenedor se reiniciaba en bucle sin decir qué faltaba
if not TELEGRAM_TOKEN:
    error("[CONFIG] Bot token needs to be configured with the TELEGRAM_TOKEN variable")
    sys.exit(1)

# Lo que va antes de los dos puntos es el id del bot, que da nombre a su sesión
if not re.fullmatch(r"[0-9]+:\S+", TELEGRAM_TOKEN):
    error("[CONFIG] TELEGRAM_TOKEN is not a bot token: it must be the one @BotFather gives, like 123456789:AAH...")
    sys.exit(1)

# Solo ids de persona: con el de un grupo o un canal, cualquiera que escribiera
# en su nombre pasaba por admin. Se ignoran, y el mensaje de arranque lo dice:
# parar el bot por ellos lo dejaba sin contestar también a los admins buenos
wrong_admins = wrong_admin_ids()
if wrong_admins:
    error(f"[CONFIG] {', '.join(wrong_admins)} in TELEGRAM_ADMIN is not a user id and is ignored. You must put your own user id: not the id of a group or a channel, and you cannot be anonymous to control the bot.")

if not admin_ids():
    error("[CONFIG] The chatId of the user who will interact with the bot needs to be configured with the TELEGRAM_ADMIN variable")
    sys.exit(1)

# Los dos de my.telegram.org. Sin el id, Telethon fallaba con un "invalid
# literal for int()" que no decía qué variable faltaba
if not re.fullmatch(r"[0-9]+", TELEGRAM_API_ID):
    error("[CONFIG] The API id needs to be configured with the TELEGRAM_API_ID variable: the number that my.telegram.org gives")
    sys.exit(1)

if not TELEGRAM_API_HASH:
    error("[CONFIG] The API hash needs to be configured with the TELEGRAM_API_HASH variable")
    sys.exit(1)

for path in DOWNLOAD_PATHS.values():
    os.makedirs(path, exist_ok=True)

# Cada tipo va a su carpeta si está montada como volumen; si no, a /downloads
for kind, path in DOWNLOAD_PATHS.items():
    debug(f"[STARTUP] Folder for {kind}: {path}")
if not downloads_persistent():
    warning(f"[CONFIG] {DOWNLOAD_PATH} is not a mapped volume: what is downloaded there will be lost when the container is recreated.")

# Limpiar TODA la carpeta temporal al arrancar (eliminar archivos huérfanos de sesiones anteriores)
debug(f"[STARTUP] Cleaning temporary directory: {TEMP_DIR}...")
try:
    if os.path.exists(TEMP_DIR):
        # Eliminar toda la carpeta y su contenido
        shutil.rmtree(TEMP_DIR)
        debug("[STARTUP] Removed temporary directory and all contents")

    # Recrear la carpeta vacía
    os.makedirs(TEMP_DIR, exist_ok=True)
    debug("[STARTUP] ✅ Temporary directory cleaned and recreated")
except Exception as e:
    warning(f"[STARTUP] Could not clean temporary directory: {e}")
    # Si falla, al menos intentar crear la carpeta
    os.makedirs(TEMP_DIR, exist_ok=True)

# Lo que llega por Telegram, las copias a destino y lo que se descomprime desde
# /manage se escriben en ocultos dentro de cada carpeta de descarga: quitar los
# que dejara una parada a mitad. Solo los de este bot (ver partial_name): los
# de otro que comparta las carpetas pueden estar escribiéndose ahora
for path in set(DOWNLOAD_PATHS.values()) | {DOWNLOAD_PATH}:
    remove_partial_files(path)
    remove_partial_extractions(path)

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

def session_path():
    """Dónde guarda Telethon la sesión del bot, sin la extensión .session.

    La sesión tiene la auth key y hasta dónde se habían leído los mensajes,
    que es lo que deja recuperar al arrancar lo que llegó con el bot parado.
    Vivía en /app, que no es un volumen, y cada actualización la borraba. Va
    en /config/state con el id del bot en el nombre: con la sesión de otro
    bot, Telethon seguiría conectado como aquel aunque cambie TELEGRAM_TOKEN.
    Lleva también un resumen del token: al revocarlo en @BotFather, la auth
    key de la sesión deja de valer y, con ella, el bot no llegaba a entrar con
    el nuevo. Con otro token se empieza una sesión limpia y se borran las del
    anterior. Si ahí no se puede escribir, se queda en el directorio de trabajo.
    """
    bot_id = TELEGRAM_TOKEN.split(':')[0]
    token_hash = hashlib.sha256(TELEGRAM_TOKEN.encode()).hexdigest()[:8]
    path = os.path.join(store.state_dir(), f"dropbot-{bot_id}-{token_hash}")
    session_file = f"{path}.session"
    if os.path.exists(LEGACY_SESSION_FILE) and not os.path.exists(session_file):
        try:
            # Con su -journal si quedó uno a medias, que es parte de la base de datos
            for suffix in ("-journal", ""):
                if os.path.exists(LEGACY_SESSION_FILE + suffix):
                    shutil.move(LEGACY_SESSION_FILE + suffix, session_file + suffix)
            debug(f"[STARTUP] Telegram session moved from {LEGACY_SESSION_FILE} to {session_file}")
        except OSError as e:
            warning(f"[STARTUP] Could not move the Telegram session from {LEGACY_SESSION_FILE}: {e}")
    try:
        # Solo para el dueño: la auth key da el mismo acceso que el token.
        # SQLite crea su -journal con los permisos de la base de datos
        os.close(os.open(session_file, os.O_CREAT | os.O_WRONLY, 0o600))
    except OSError as e:
        warning(f"[STARTUP] Cannot write the Telegram session in {store.state_dir()} ({e}): it stays in {os.getcwd()} and will be lost when the container is recreated")
        return "dropbot"
    try:
        os.chmod(session_file, 0o600)
    except OSError as e:
        warning(f"[STARTUP] Could not restrict the permissions of {session_file}: {e}")
    # Las de un token anterior de este bot, con su -journal o apartadas como
    # .bad (ver exit_if_session_rejected): su auth key ya no vale
    for stale in glob.glob(os.path.join(glob.escape(store.state_dir()), f"dropbot-{bot_id}-*.session*")):
        if not stale.startswith(session_file):
            try:
                os.remove(stale)
                debug(f"[STARTUP] Telegram session of a previous token removed: {stale}")
            except OSError as e:
                warning(f"[STARTUP] Could not remove the Telegram session of a previous token {stale}: {e}")
    return path


# Reintenta conectar sin límite: con los 5 intentos de Telethon, un corte de
# red de unos minutos tumbaba el bot, y con él las descargas en curso. Al
# conectar, catch_up pide lo que llegó con el bot parado (un reinicio, una
# actualización) desde donde se quedó la sesión; antes se perdía sin avisar.
# Se conecta en main(), cuando ya están registrados todos los handlers
bot = TelegramClient(session_path(), TELEGRAM_API_ID, TELEGRAM_API_HASH,
                     connection_retries=-1, retry_delay=5, catch_up=True)

def progress_update_interval():
    """Segundos entre ediciones de un mensaje de progreso.

    Evita anti-spam cuando hay múltiples descargas paralelas: Telegram permite
    ~20 mensajes/minuto en grupos, y con ediciones es más restrictivo. Por eso
    es muy conservador: max(10, descargas simultáneas * 1.5). Ejemplos: 1
    descarga = 10s, 5 descargas = 10s, 10 descargas = 15s.
    """
    return max(10, settings.parallel_downloads() * 1.5)


# El límite se cambia desde /settings sin reiniciar (ver apply_setting)
download_semaphore = ResizableLimiter(settings.parallel_downloads())

# Inicializar cola de mensajes para evitar FloodWaitError
# delay_between_messages: tiempo entre mensajes (configurable, default: 0.5s)
# max_retries: número de reintentos en caso de error (configurable, default: 5)
message_queue = TelegramMessageQueue(
    delay_between_messages=MESSAGE_QUEUE_DELAY,
    max_retries=MESSAGE_QUEUE_MAX_RETRIES
)

# Inyectar dependencias (cola + bot) en el módulo de helpers
telegram_helpers.init(message_queue, bot)

async def handle_list_files(event):
    """`/list <categoría>`: el listado de esa categoría sin pasar por el menú.

    Una categoría desconocida lista todo, como hacía siempre.
    """
    command_parts = event.raw_text.split()
    category = command_parts[1].lower() if len(command_parts) > 1 else "all"
    await manage_handlers.send_file_list(event, category, await get_topic(event))


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
    # Telethon devuelve None en .audio para las notas de voz: van por .voice
    elif file_extension in EXTENSIONS_AUDIO or message.audio or message.voice:
        return DOWNLOAD_PATHS["audio"], AUD_ICO
    elif file_extension in EXTENSIONS_IMAGE or message.photo:
        return DOWNLOAD_PATHS["photo"], IMG_ICO
    return DOWNLOAD_PATH, DEF_ICO


# Para las estadísticas: qué tipo de fichero se recibió, por su icono
FILE_KIND_BY_ICON = {
    TOR_ICO: "torrent", BOO_ICO: "ebook", VID_ICO: "video",
    AUD_ICO: "audio", IMG_ICO: "photo",
}


def task_key(event):
    """La clave de la descarga de `event` en active_tasks, que lleva también su Cancelar.

    Con el chat: cada supergrupo numera sus mensajes por su cuenta, y la
    descarga de un grupo pisaba a la del mismo id en privado, que ya no se
    podía cancelar (su Cancelar paraba la del grupo). En un botón, el id es el
    de la consulta. Con los ids más largos, "cancel:" y la clave ocupan menos
    de 50 de los 64 bytes de un botón.
    """
    return f"{event.chat_id}_{event.id}"


# Lo que lleva el Cancelar de una descarga (ver task_key). Los de antes de
# llevar el chat solo tienen el id: tras reiniciar ya no hay nada en marcha
TASK_KEY_RE = re.compile(r"-?\d+(_-?\d+)?")


async def safe_reply_in_topic(event, *args, **kwargs):
    """Contesta a `event` en su tema del foro, sin citar el mensaje del botón.

    Con un botón, Telethon contesta al mensaje que lo lleva, sin el tema: en
    las descargas de enlaces ese mensaje pasa a ser el de progreso y se borra
    al terminar, y «Descarga completada», sus botones o el «Enviando…» salían
    en General. El tema lo deja guardado start_ytdlp_download. Un mensaje
    (formato automático, ficheros recibidos) sigue ahí y se cita como siempre.
    """
    if isinstance(event, events.CallbackQuery.Event):
        return await safe_send_message(event.chat_id, *args, reply_to=await get_topic(event), **kwargs)
    return await safe_reply(event, *args, **kwargs)


@bot.on(events.NewMessage(func=is_file_message))
async def handle_files(event):
    if await check_admin_and_warn(event):
        return

    try:
        _, icon = get_download_path(event)
        stats.count(f"file_{FILE_KIND_BY_ICON.get(icon, 'other')}")
    except Exception:
        pass

    task = asyncio.create_task(limited_download(event))
    active_tasks[task_key(event)] = task

async def limited_download(event):
    # Si no hay hueco, se avisa ya de que el fichero ha llegado y espera turno,
    # con el botón de cancelar: si no, en cola no se veía nada durante horas
    queued_message = None
    waiting = False
    key = task_key(event)
    try:
        debug(f"[DOWNLOAD] limited_download() called for event.id={event.id}")
        if download_semaphore.active >= download_semaphore.limit:
            waiting = True
            try:
                queued_message = await safe_reply(
                    event,
                    get_text("download_queued"),
                    buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{key}")],
                    parse_mode=PARSE_MODE,
                    wait_for_result=True
                )
            except Exception as e:
                warning(f"[DOWNLOAD] Could not send queued notice for event.id={event.id}: {e}")
        async with download_semaphore:
            waiting = False
            await download_media(event, queued_message)
        debug(f"[DOWNLOAD] limited_download() completed for event.id={event.id}")
    except asyncio.CancelledError:
        # Cancelada en la cola: download_media no ha llegado a empezar y el
        # aviso se quedaría con un Cancelar que ya no cancela nada
        if waiting and queued_message is not None:
            await safe_edit(queued_message, get_text("cancelled"), buttons=None, parse_mode=PARSE_MODE)
        raise
    except Exception as e:
        error(f"[DOWNLOAD] ❌ Unhandled exception in limited_download for event.id={event.id}: {e}")
        error(f"[DOWNLOAD] Exception type: {type(e).__name__}")
        import traceback
        error(f"[DOWNLOAD] Traceback: {traceback.format_exc()}")
        # No re-lanzar para evitar que Telethon capture silenciosamente
    finally:
        # download_media solo la limpia si agota los reintentos; también hay que
        # hacerlo al terminar bien, al cancelar o ante un error no reintentable
        if active_tasks.get(key) is asyncio.current_task():
            active_tasks.pop(key, None)

def _make_transfer_progress_callback(status_message, file_name, text_key, log_tag, buttons=None):
    """
    Crea un callback de progreso reutilizable para descargas y subidas.

    Actualiza `status_message` cada progress_update_interval() segundos (y siempre
    al llegar al 100%) para evitar anti-spam. `text_key` selecciona el texto
    ("downloading_progress"/"uploading_progress"), `log_tag` el prefijo de logs
    y `buttons` los botones a mostrar en cada edición (None = sin botones).

    La transferencia espera al callback entre trozo y trozo, así que la edición
    va en su propia tarea: ante un FloodWait corto Telethon duerme dentro del
    `edit`, y esperarla paraba la descarga o la subida. Quien vaya a dejar el
    mensaje en su estado final llama antes a `progress_callback.cancel_pending()`
    para que una edición rezagada no lo pise.
    """
    last_update_time = [0]  # Lista para poder modificar en closure
    message_deleted = [False]  # Flag para detectar si el mensaje fue eliminado
    pending_edit = [None]  # Edición en curso, que la transferencia no espera

    async def edit_status(message):
        # Usar edición directa sin cola para actualizaciones de progreso (más rápido)
        try:
            if buttons is not None:
                await status_message.edit(message, buttons=buttons, parse_mode=PARSE_MODE)
            else:
                await status_message.edit(message, parse_mode=PARSE_MODE)
        except Exception as edit_error:
            # Si el mensaje fue eliminado o es inválido, marcar como eliminado y dejar de intentar
            error_msg = str(edit_error)
            if "message ID is invalid" in error_msg or "MESSAGE_ID_INVALID" in error_msg:
                debug(f"[{log_tag} PROGRESS] Message was deleted, stopping progress updates")
                message_deleted[0] = True
            else:
                # Otros errores (FloodWait de más de un minuto, mensaje no modificado, etc.) - solo log
                warning(f"[{log_tag}] Error editing progress message: {edit_error}")

    async def progress_callback(current, total):
        # Si no hay mensaje de estado o fue eliminado, no hacer nada
        if status_message is None or message_deleted[0]:
            return
        try:
            current_time = asyncio.get_running_loop().time()
            # Siempre actualizar si llegamos al 100%, sin importar el intervalo
            is_complete = (current >= total)
            should_update = (current_time - last_update_time[0] >= progress_update_interval()) or is_complete

            # Log cuando llegamos al 100%
            if is_complete and not hasattr(progress_callback, 'logged_100'):
                debug(f"[{log_tag} PROGRESS] Reached 100% ({current}/{total} bytes)")
                progress_callback.logged_100 = True

            # La anterior sigue en marcha (dormida por un FloodWait): esta se
            # deja para el siguiente trozo. La del 100% sí va, y la vieja sobra
            if pending_edit[0] is not None and not pending_edit[0].done():
                if not is_complete:
                    return
                pending_edit[0].cancel()

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

                # Calcular velocidad (aproximada basada en el intervalo).
                # Se ignora un bytes_diff negativo (p. ej. al pasar de descarga
                # rápida a estándar, que reinicia el contador en 0).
                bytes_diff = current - getattr(progress_callback, 'last_current', 0)
                time_diff = current_time - getattr(progress_callback, 'last_time', current_time)
                if bytes_diff >= 0 and time_diff > 0:
                    speed = format_bytes(bytes_diff / time_diff) + "/s"
                else:
                    speed = "N/A"

                progress_callback.last_current = current
                progress_callback.last_time = current_time

                # Calcular ETA
                if speed != "N/A" and bytes_diff > 0 and time_diff > 0:
                    remaining_bytes = total - current
                    eta_seconds = int(remaining_bytes / (bytes_diff / time_diff))
                    eta = f"{eta_seconds // 60:02d}:{eta_seconds % 60:02d}"
                else:
                    eta = "N/A"

                # Crear barra de progreso visual
                bar_length = 20
                filled = int(bar_length * percent / 100)
                bar = "█" * filled + "░" * (bar_length - filled)

                message = get_text(text_key, bar, f"{percent:.1f}", f"{size_current}/{size_total}", speed, eta, file_name)

                pending_edit[0] = asyncio.create_task(edit_status(message))
        except Exception as e:
            # Ignorar errores de actualización
            warning(f"[{log_tag}] Error updating progress: {e}")

    def cancel_pending():
        """Descarta la edición en curso para que no pise el mensaje final."""
        if pending_edit[0] is not None:
            pending_edit[0].cancel()

    progress_callback.cancel_pending = cancel_pending
    return progress_callback

def create_upload_progress_callback(status_message, file_name):
    """Callback de progreso para envíos a Telegram (sin botones)."""
    return _make_transfer_progress_callback(
        status_message, file_name, "uploading_progress", "UPLOAD"
    )

def create_progress_callback(status_message, event, file_name):
    """Callback de progreso para descargas desde Telegram (con botón Cancelar)."""
    buttons = [Button.inline(get_text("button_cancel"), data=f"cancel:{task_key(event)}")]
    return _make_transfer_progress_callback(
        status_message, file_name, "downloading_progress", "DOWNLOAD", buttons=buttons
    )

class _ThreadedTempFile:
    """El temporal que se le pasa a la descarga estándar de Telethon.

    Con la ruta, Telethon abre, escribe y cierra el fichero en el event loop, y
    el temporal está en la carpeta de destino, que puede ser un NAS: en NFS o
    SMB el close() espera a que todo lo pendiente llegue al servidor, y el bot
    entero se paraba al final de cada descarga. Con un objeto no lo abre, ni lo
    cierra, ni le cambia el nombre, y espera lo que devuelva write(): aquí, la
    escritura en un hilo, como en la ruta rápida.
    """

    def __init__(self, out):
        self.name = out.name
        self._out = out
        self._written = 0
        # La foto que el mensaje ya trae en caché Telethon la escribe sin
        # esperarla: hay que esperarla antes de cerrar
        self.last_write = None

    def write(self, data):
        self._written += len(data)
        self.last_write = asyncio.get_running_loop().run_in_executor(
            fast_telethon._IO_EXECUTOR, self._out.write, data
        )
        return self.last_write

    def tell(self):
        return self._written


def _empty_temp_file(out):
    """Deja vacío el temporal, para que la descarga estándar empiece de cero."""
    out.seek(0)
    out.truncate()


def _close_temp_file(out):
    """Cierra el temporal y devuelve su os.stat_result, con todo lo escrito."""
    with out:
        out.flush()
        return os.fstat(out.fileno())


def _is_temp_file(path, created):
    """Si `path` sigue siendo el temporal que se creó (`created`, su os.stat_result).

    Sin seguir enlaces: está en la carpeta de descargas, que pueden compartir
    otros, y con ese nombre podría haber ahora algo que no es del bot.
    """
    try:
        return os.path.samestat(os.lstat(path), created)
    except OSError:
        return False


def _remove_temp_file(path, created):
    """Borra el temporal `path` si sigue siendo el que se creó (ver _is_temp_file)."""
    if _is_temp_file(path, created):
        try:
            os.remove(path)
        except OSError:
            pass


async def _discard_temp_file(out, path, created):
    """Cierra el temporal que no ha servido y lo borra (ver _remove_temp_file)."""
    try:
        await asyncio.to_thread(out.close)
    except OSError:
        # Lo que no se haya podido escribir ya da igual: se borra
        pass
    finally:
        _remove_temp_file(path, created)


async def _download_to_file(message, temp_file_path, progress_callback):
    """
    Descarga el media del mensaje a `temp_file_path`, que crea nuevo.

    Intenta primero la descarga paralela (FastTelethon) para ficheros grandes;
    si falla por cualquier motivo, recurre al método estándar de Telethon.
    Devuelve el os.stat_result de lo descargado, ya cerrado, o None si
    Telegram no ha entregado nada. Entonces, o si falla, borra el temporal.

    Está en la carpeta de descargas, que pueden compartir otros contenedores:
    se crea con O_EXCL, que no sigue un enlace que ya tenga ese nombre, y todo
    se escribe por ese descriptor. Antes se volvía a abrir por su nombre al
    pasar a la estándar y en cada reintento, y entretanto otro podía dejar ahí
    un enlace a cualquier fichero, donde el bot (que va como root) escribía lo
    descargado.
    """
    document = message.document
    file_size = message.file.size if message.file and message.file.size else 0
    connections = settings.fast_connections()
    use_fast = (
        connections > 1
        and document is not None
        and file_size > FAST_TRANSFER_MIN_BYTES
    )

    # Abrir y cerrar también en un hilo: en un NAS, close() espera a que todo
    # lo escrito llegue al servidor (ver _ThreadedTempFile)
    out = await asyncio.to_thread(open, temp_file_path, "xb")
    created = os.fstat(out.fileno())
    try:
        if use_fast:
            try:
                conns = fast_telethon.connection_count(file_size, connections)
                debug(f"[DOWNLOAD] Parallel download: {conns} connection(s) for this file")
                await fast_telethon.download_file(
                    bot, document, out, connections, progress_callback
                )
            except asyncio.CancelledError:
                raise
            except Exception as fast_err:
                # Sin espacio, la estándar volvería a bajarlo entero para fallar igual
                if isinstance(fast_err, OSError) and fast_err.errno == errno.ENOSPC:
                    raise
                warning(f"[DOWNLOAD] ⚠️ Parallel download failed ({fast_err}); falling back to standard download")
                await asyncio.to_thread(_empty_temp_file, out)
            else:
                return await asyncio.to_thread(_close_temp_file, out)

        temp_file = _ThreadedTempFile(out)
        await bot.download_media(message, file=temp_file, progress_callback=progress_callback)
        if temp_file.last_write is not None:
            await temp_file.last_write
        if temp_file.tell():
            return await asyncio.to_thread(_close_temp_file, out)
    except BaseException:
        await _discard_temp_file(out, temp_file_path, created)
        raise
    # Sin nada que descargar Telethon vuelve sin escribir: con la ruta no
    # llegaba a crear el fichero, y download_media lo da por fallido
    await _discard_temp_file(out, temp_file_path, created)
    return None


# Una descarga se da por colgada si pasa este tiempo sin que llegue nada. No hay
# plazo total: con una línea lenta o varias descargas a la vez, un fichero
# grande puede tardar horas y sigue siendo una descarga que avanza
DOWNLOAD_STALL_TIMEOUT = 300


async def _download_unless_stalled(message, temp_file_path, progress_callback):
    """`_download_to_file`, cortada con asyncio.TimeoutError si deja de avanzar.

    Cada trozo que llega aplaza el corte otros DOWNLOAD_STALL_TIMEOUT segundos,
    así que la descarga estándar, si la rápida falla, no hereda lo que sobre de
    un plazo común: tiene el suyo mientras siga recibiendo.
    """
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(DOWNLOAD_STALL_TIMEOUT) as deadline:
        async def on_progress(current, total):
            deadline.reschedule(loop.time() + DOWNLOAD_STALL_TIMEOUT)
            await progress_callback(current, total)

        return await _download_to_file(message, temp_file_path, on_progress)


async def _send_file_fast(entity, file_path, filename, attributes, thumb_path, is_video, progress_callback, reply_to=None):
    """
    Envía un fichero a Telegram.

    Para ficheros grandes usa subida paralela (FastTelethon) y, si falla por
    cualquier motivo, recurre al método estándar de Telethon. Devuelve el
    mensaje enviado. `reply_to` es el tema del foro al que va (ver get_topic).
    """
    file_size = os.path.getsize(file_path)

    # Parámetros de envío comunes a ambas rutas (rápida y estándar) para que el
    # fichero llegue idéntico a Telegram independientemente de por dónde se envíe.
    ext = os.path.splitext(filename)[1].lower()
    is_audio = ext in EXTENSIONS_AUDIO
    mime_type = mimetypes.guess_type(filename)[0]
    if not mime_type:
        mime_type = "video/mp4" if is_video else "application/octet-stream"
    force_document = not (is_video or is_audio)
    supports_streaming = True if is_video else None

    connections = settings.fast_connections()
    use_fast = connections > 1 and file_size > FAST_TRANSFER_MIN_BYTES

    try:
        if use_fast:
            try:
                conns = fast_telethon.connection_count(file_size, connections)
                debug(f"[UPLOAD] Parallel upload: {conns} connection(s) for {filename}")
                with open(file_path, "rb") as f:
                    handle = await fast_telethon.upload_file(
                        bot, f, file_size, connections, progress_callback
                    )
                return await bot.send_file(
                    entity,
                    file=handle,
                    attributes=attributes,
                    thumb=thumb_path if thumb_path else None,
                    mime_type=mime_type,
                    supports_streaming=supports_streaming,
                    force_document=force_document,
                    progress_callback=None,
                    reply_to=reply_to,
                )
            except asyncio.CancelledError:
                raise
            except Exception as fast_err:
                warning(f"[UPLOAD] ⚠️ Parallel upload failed ({fast_err}); falling back to standard send")

        # Subido aparte y con el nombre que se enseña, como en la rápida: con
        # la ruta, Telethon le pone al fichero subido el nombre del disco, y
        # uno que no es UTF-8 ("caf\udce9.mp3") no se puede mandar a Telegram
        handle = await bot.upload_file(file_path, file_name=filename, progress_callback=progress_callback)
        return await bot.send_file(
            entity,
            file=handle,
            attributes=attributes,
            thumb=thumb_path if thumb_path else None,
            mime_type=mime_type,
            supports_streaming=supports_streaming,
            force_document=force_document,
            progress_callback=None,
            reply_to=reply_to,
        )
    finally:
        # Una edición de progreso que siga pendiente (dormida por un FloodWait)
        # no puede pisar lo que haga quien llama al terminar: borrar el mensaje
        # o dejarlo con el resultado del envío
        cancel_pending = getattr(progress_callback, "cancel_pending", None)
        if cancel_pending is not None:
            cancel_pending()


# Lo más que se espera a que pase un FloodWait de Telegram para volver a
# intentar una descarga (los de hasta un minuto ya los duerme Telethon)
FLOOD_WAIT_MAX_SECONDS = 900

# Una foto que ha esperado mucho en la cola puede llegar con la referencia
# caducada, y Telethon solo la renueva en los documentos
FILE_REFERENCE_ERRORS = (errors.FileReferenceExpiredError, errors.FileReferenceInvalidError)


class MessageGoneError(Exception):
    """Al renovar la referencia, el mensaje ya no está (o ya no trae el fichero)."""


def is_transient_download_error(e):
    """True si el fallo es de la red o del servidor de Telegram y otro intento puede ir bien.

    Se decide por el tipo y no por el texto: el de un OSError lleva las rutas, y
    el de cualquier error de Telegram el nombre de la petición, así que antes un
    fichero con "network" en el nombre o un error permanente se reintentaban.
    """
    if isinstance(e, (ConnectionError, TimeoutError, errors.ServerError, errors.TimedOutError)):
        return True
    # Telegram corta la conexión por un flood a nivel de transporte (-429), y
    # main() vuelve a conectar
    if isinstance(e, InvalidBufferError) and e.code == 429:
        return True
    # Telethon lo lanza cuando agota sus propios reintentos de una petición
    return isinstance(e, ValueError) and "Request was unsuccessful" in str(e)


async def download_media(event, status_message=None):
    """Descarga el fichero de `event` en su carpeta, con reintentos y progreso.

    `status_message` es el aviso de "en cola" de limited_download, si lo hubo:
    pasa a ser el de la descarga en vez de mandar otro.
    """
    debug(f"[DOWNLOAD] download_media() called for event.id={event.id}")
    message = event.message
    media = message.document or message.video or message.audio or message.photo
    if not media:
        debug("[DOWNLOAD] No media found in message, returning")
        return
    file_name = get_file_name(media)
    debug(f"[DOWNLOAD] File {file_name} - Received, starting download")
    download_path, ico = get_download_path(event)

    # Descargar primero a un oculto en la propia carpeta de destino: no aparece
    # en /list mientras se descarga, no ocupa el disco del contenedor (TEMP_DIR
    # está en la capa de Docker) y al terminar basta con renombrarlo. Su nombre
    # no sale del original, que con un sufijo podría pasar de 255 bytes, y es
    # otro en cada intento (ver el bucle). El nombre final no se elige hasta
    # terminar: otra descarga con el mismo nombre puede acabar antes y ocuparlo
    downloaded = False

    cancel_buttons = [Button.inline(get_text("button_cancel"), data=f"cancel:{task_key(event)}")]
    try:
        if status_message is not None:
            await safe_edit(status_message, get_text("downloading", ico), buttons=cancel_buttons, parse_mode=PARSE_MODE)
        else:
            status_message = await safe_reply(
                event,
                get_text("downloading", ico),
                buttons=cancel_buttons,
                parse_mode=PARSE_MODE,
                wait_for_result=True
            )
    except Exception as e:
        # Un chat donde el bot no puede escribir, o Telegram fallando más de lo
        # que aguanta la cola: el fichero se descarga igual
        warning(f"[DOWNLOAD] Error creating status message for {file_name}: {e}")
        status_message = None

    # Si no se pudo crear el mensaje de estado (timeout en cola o el error de
    # arriba), continuar sin él
    if status_message is None:
        warning(f"[DOWNLOAD] Could not create status message for {file_name} - continuing without progress updates")

    # Crear callback de progreso: sin mensaje no edita nada, pero el vigilante
    # de _download_unless_stalled tiene que ver avanzar la descarga igual
    progress_callback = create_progress_callback(status_message, event, file_name)

    # Espera antes de cada reintento: la de siempre, la que pida un FloodWait o
    # ninguna si basta con renovar la referencia del fichero
    retry_delay = 0
    reference_renewed = False
    renew_reference = False

    # Intentar descargar con reintentos
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        # Solo es nuestra la ruta reservada en este intento: si falló al
        # moverlo, copy_and_remove ya la borró y otra descarga puede haberla ocupado
        final_file_path = None
        # True cuando el fichero ya se bajó entero: si falla al moverlo (disco
        # lleno, permisos) no se vuelve a descargar
        moving = False
        # Un temporal nuevo en cada intento, con un nombre que no se puede
        # adivinar (ver _download_to_file). Si falla al bajarlo, lo borra
        # _download_to_file; bajado, `written` es lo que se ha escrito
        temp_file_path = os.path.join(download_path, partial_name(f"dl_{secrets.token_hex(8)}{PARTIAL_SUFFIX}"))
        written = None
        try:
            # La espera entre intentos va dentro del try y no en el except del
            # fallo: así, si se cancela mientras tanto, también pasa por el
            # except de la cancelación y el mensaje acaba en "cancelado"
            if attempt > 1:
                await asyncio.sleep(retry_delay)

            # Lo mismo con la petición del mensaje para renovar la referencia,
            # que con un FloodWait puede tardar un minuto. Si falla de la red,
            # se vuelve a pedir en el siguiente intento
            if renew_reference:
                fresh = await bot.get_messages(event.chat_id, ids=event.id)
                if fresh is None or not fresh.media:
                    raise MessageGoneError(f"Message {event.id} is no longer available")
                message = fresh
                renew_reference = False

            debug(f"[DOWNLOAD] Starting download to: {temp_file_path}")

            try:
                written = await _download_unless_stalled(message, temp_file_path, progress_callback)
            except asyncio.CancelledError:
                # Al desconectarse (un corte con 429), Telethon cancela las
                # peticiones en vuelo de las conexiones con otros DC. Eso no
                # lo ha pedido nadie: la descarga acababa en «Cancelada»,
                # como si se hubiera pulsado Cancelar, y no se reintentaba
                if asyncio.current_task().cancelling():
                    raise
                raise ConnectionError("Telegram disconnected in the middle of the download") from None
            finally:
                # Lo que se diga ahora (éxito, reintento, error o cancelación)
                # no puede quedar pisado por una edición de progreso rezagada
                progress_callback.cancel_pending()

            debug("[DOWNLOAD] ✅ bot.download_media() completed successfully")
            debug("[DOWNLOAD] Checking if temp file exists...")

            # Mover el temporal a su nombre final
            moving = True
            debug("[DOWNLOAD] Moving file from temp to final location...")
            debug(f"[DOWNLOAD] Source: {temp_file_path}")

            try:
                # Verificar que el archivo temporal existe antes de mover, y que
                # es el descargado: otro que comparta la carpeta podría haber
                # puesto algo suyo con ese nombre, y acabaría con el nombre final
                if written is None or not _is_temp_file(temp_file_path, written):
                    error(f"[DOWNLOAD] ❌ Temporary file not found: {temp_file_path}")
                    raise FileNotFoundError(f"Temporary file not found: {temp_file_path}")

                debug("[DOWNLOAD] ✅ Temporary file exists")
                temp_size = written.st_size
                debug(f"[DOWNLOAD] Temporary file size: {temp_size} bytes ({temp_size / (1024*1024):.2f} MB)")

                # Verificar permisos de lectura
                if not os.access(temp_file_path, os.R_OK):
                    error(f"[DOWNLOAD] ❌ No read permission for temp file: {temp_file_path}")
                    raise PermissionError("No read permission for temp file")

                debug("[DOWNLOAD] ✅ Temp file is readable")

                # Verificar que el directorio de destino existe
                dest_dir = download_path
                if not os.path.exists(dest_dir):
                    error(f"[DOWNLOAD] ❌ Destination directory does not exist: {dest_dir}")
                    raise FileNotFoundError(f"Destination directory not found: {dest_dir}")

                debug(f"[DOWNLOAD] ✅ Destination directory exists: {dest_dir}")

                # Verificar permisos de escritura en destino
                if not os.access(dest_dir, os.W_OK):
                    error(f"[DOWNLOAD] ❌ No write permission for destination directory: {dest_dir}")
                    raise PermissionError("No write permission for destination directory")

                debug("[DOWNLOAD] ✅ Destination directory is writable")

                # Reservar el nombre final de forma atómica y mover en un hilo
                # para no bloquear el event loop
                final_file_path = reserve_unique_path(dest_dir, file_name)
                if os.path.basename(final_file_path) != file_name:
                    debug(f"[DOWNLOAD] Duplicate file detected. Renaming: {file_name} -> {os.path.basename(final_file_path)}")
                debug(f"[DOWNLOAD] Destination: {final_file_path}")
                await asyncio.to_thread(copy_and_remove, temp_file_path, final_file_path)
                debug("[DOWNLOAD] ✅ File moved successfully")

                # Verificar que el archivo final existe
                # Para archivos .torrent, el gestor puede procesarlos inmediatamente
                is_torrent = final_file_path.lower().endswith('.torrent')
                file_exists = os.path.exists(final_file_path)

                if not file_exists:
                    if is_torrent:
                        debug("[DOWNLOAD] Torrent file was processed by torrent manager (expected behavior)")
                        # Continuar normalmente, el archivo fue procesado correctamente
                    else:
                        error(f"[DOWNLOAD] ❌ Final file not found after move: {final_file_path}")
                        raise FileNotFoundError(f"Final file not found after move: {final_file_path}")

                if file_exists:
                    debug("[DOWNLOAD] ✅ Final file exists")
                    final_size = os.path.getsize(final_file_path)
                    debug(f"[DOWNLOAD] Final file size: {final_size} bytes ({final_size / (1024*1024):.2f} MB)")

                    # Verificar que los tamaños coinciden
                    if temp_size != final_size:
                        warning(f"[DOWNLOAD] ⚠️ File size mismatch! Temp: {temp_size}, Final: {final_size}")
                    else:
                        debug("[DOWNLOAD] ✅ File sizes match")

            except Exception as move_error:
                error(f"[DOWNLOAD] ❌ Error moving file: {move_error}")
                error(f"[DOWNLOAD] Error type: {type(move_error).__name__}")
                import traceback
                error(f"[DOWNLOAD] Traceback: {traceback.format_exc()}")
                # Re-lanzar la excepción para que se maneje en el except general
                raise

            # Descarga exitosa - borrar mensaje de progreso. Sin esperar a que
            # salga: la descarga sigue con su hueco del semáforo hasta volver,
            # y detrás de un FloodWait la cola tarda minutos
            debug("[DOWNLOAD] Deleting progress message...")
            if status_message:
                try:
                    await safe_delete(status_message)
                except Exception as delete_error:
                    # Continuar aunque falle el borrado
                    warning(f"[DOWNLOAD] ⚠️ Could not delete progress message: {delete_error}")
            else:
                debug("[DOWNLOAD] No status message to delete")

            debug(f"[DOWNLOAD] ✅ File {file_name} - Downloaded successfully")

            # Salir del bucle si la descarga fue exitosa
            debug("[DOWNLOAD] Breaking from retry loop")
            downloaded = True
            break

        except asyncio.CancelledError:
            if status_message:
                await safe_edit(status_message, get_text("cancelled"), buttons=None, parse_mode=PARSE_MODE)
            # Limpiar archivo temporal si ya se había bajado (a medias lo ha
            # borrado _download_to_file)
            if written is not None:
                _remove_temp_file(temp_file_path, written)
                debug(f"[DOWNLOAD] Temporary file deleted after cancellation: {temp_file_path}")
            # Limpiar archivo final si se movió (la ruta es nuestra: se reservó para esta descarga)
            if final_file_path and os.path.exists(final_file_path):
                os.remove(final_file_path)
                debug(f"[DOWNLOAD] Final file deleted after cancellation: {final_file_path}")
            debug(f"[DOWNLOAD] File {file_name} - Cancelled")
            raise

        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__

            # Loguear SIEMPRE el error antes de decidir qué hacer
            error(f"[DOWNLOAD] ❌ Exception caught in download loop: {error_type} - {error_msg}")
            import traceback
            error(f"[DOWNLOAD] Traceback: {traceback.format_exc()}")

            # Sin espacio no sirve de nada reintentar, y el aviso dice dónde falta
            no_space = isinstance(e, OSError) and e.errno == errno.ENOSPC
            # DOWNLOAD_STALL_TIMEOUT segundos sin recibir nada (o un timeout de la red)
            stalled = isinstance(e, TimeoutError)
            # Telegram pide esperar más del minuto que Telethon duerme por su cuenta
            flood_wait = (isinstance(e, (errors.FloodWaitError, errors.FloodPremiumWaitError))
                          and e.seconds <= FLOOD_WAIT_MAX_SECONDS)

            # Con la referencia caducada se vuelve a pedir el mensaje, una vez,
            # y se reintenta sin esperar. Se pide al empezar el siguiente
            # intento: aquí, una cancelación mientras tanto no pasaba por el
            # except de la cancelación y el mensaje se quedaba en "Descargando..."
            expired = not moving and isinstance(e, FILE_REFERENCE_ERRORS) and not reference_renewed
            if expired:
                reference_renewed = True
                renew_reference = True
            # Al volver a pedirlo ya no estaba: hay que reenviarlo
            message_gone = isinstance(e, MessageGoneError)

            # Solo se reintenta lo que falla al bajarlo y es de la red o de Telegram
            should_retry = not moving and (is_transient_download_error(e) or flood_wait or expired)

            debug(f"[DOWNLOAD] should_retry={should_retry} for error type {error_type}")

            if should_retry:
                # Lo bajado a medias ya lo ha borrado _download_to_file, y el
                # siguiente intento escribe en un temporal nuevo. Si aún quedan
                # intentos, reintentar
                if attempt < MAX_DOWNLOAD_RETRIES:
                    debug(f"[DOWNLOAD] Retrying download of {file_name} (attempt {attempt + 1} of {MAX_DOWNLOAD_RETRIES}) after error: {error_msg}")
                    if flood_wait:
                        retry_delay = e.seconds
                        retry_text = get_text("warning_flood_wait_download", e.seconds, attempt + 1, MAX_DOWNLOAD_RETRIES)
                    else:
                        retry_delay = 0 if expired else RETRY_DELAY_SECONDS
                        retry_text = get_text("warning_retrying_download", attempt + 1, MAX_DOWNLOAD_RETRIES)
                    if status_message:
                        try:
                            await safe_edit(
                                status_message,
                                retry_text,
                                buttons=cancel_buttons,
                                parse_mode=PARSE_MODE
                            )
                        except Exception as msg_error:
                            error(f"[DOWNLOAD] Error updating status message: {msg_error}")
                else:
                    # Último intento fallido
                    error(f"[DOWNLOAD] Download of {file_name} failed after {MAX_DOWNLOAD_RETRIES} attempts: {error_msg}")
                    if status_message:
                        try:
                            await safe_edit(
                                status_message,
                                get_text("error_download_stalled_user" if stalled else "error_telegram_timeout_user", file_name),
                                buttons=None,
                                parse_mode=PARSE_MODE
                            )
                        except Exception as msg_error:
                            error(f"[DOWNLOAD] Error updating status message: {msg_error}")
            else:
                # Error que NO debe reintentar - limpiar, avisar y re-lanzar
                error(f"[DOWNLOAD] ❌ Non-retryable error, re-raising: {error_type} - {error_msg}")
                # Sin reintento nadie va a usar el temporal: si no se borra, el
                # fichero entero se queda oculto en la carpeta hasta el siguiente arranque
                if written is not None:
                    _remove_temp_file(temp_file_path, written)
                    debug(f"[DOWNLOAD] Temp file deleted after non-retryable error: {temp_file_path}")
                # Quien llama solo lo loguea: sin esto el usuario se queda con
                # "Descargando..." y un Cancelar que ya no cancela nada
                if no_space:
                    failure_text = get_text("error_download_no_space_user", file_name, download_path)
                elif message_gone:
                    failure_text = get_text("error_download_message_gone_user", file_name)
                else:
                    failure_text = get_text("error_download_failed_user", file_name)
                if status_message:
                    try:
                        await safe_edit(
                            status_message,
                            failure_text,
                            buttons=None,
                            parse_mode=PARSE_MODE
                        )
                    except Exception as msg_error:
                        error(f"[DOWNLOAD] Error updating status message: {msg_error}")
                raise

    debug(f"[DOWNLOAD] Exited retry loop for {file_name}")

    # Mostrar información detallada del archivo descargado (sin botones de
    # acción). Va fuera del bucle de reintentos: el fichero ya está en su
    # carpeta y un fallo al avisar (p. ej. timeout de la cola) no puede
    # volver a descargarlo
    if downloaded:
        debug(f"[DOWNLOAD] Calling handle_success for: {final_file_path}")
        try:
            await handle_success(event, final_file_path, show_action_buttons=False)
            debug("[DOWNLOAD] ✅ handle_success completed")
        except Exception as success_error:
            error(f"[DOWNLOAD] ❌ Error in handle_success: {success_error}")
            error(f"[DOWNLOAD] Success error type: {type(success_error).__name__}")
            import traceback
            error(f"[DOWNLOAD] Traceback: {traceback.format_exc()}")

    # Limpiar tareas activas, si sigue siendo la suya (como en limited_download)
    if active_tasks.get(task_key(event)) is asyncio.current_task():
        active_tasks.pop(task_key(event), None)
        debug(f"[DOWNLOAD] Cleaned up active task for event.id={event.id}")

def get_extraction_message_and_buttons(extract_result, filename, extracted_path, file_id):
    """
    Genera mensajes y botones consistentes para los resultados de extracción.

    Args:
        extract_result: Resultado de extract_file() (True, False, "missing_parts",
            "corrupted", "password", "no_space")
        filename: Nombre del archivo comprimido
        extracted_path: Ruta donde se extrajo (o intentó extraer)
        file_id: ID del archivo en pending_file_actions (el de /manage)

    Returns:
        tuple: (mensaje, botones)
    """
    if extract_result == True:
        # Extracción exitosa
        msg = f"✅ **{get_text('extraction_success_title')}**\n\n"
        msg += f"📄 **{get_text('extraction_file')}:** {md_code(filename)}\n"
        msg += f"📁 **{get_text('extraction_folder')}:** {md_code(os.path.basename(extracted_path))}\n\n"
        msg += get_text('extraction_ask_delete')

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
        return msg, buttons

    if extract_result == "missing_parts":
        # Faltan partes del archivo RAR
        msg = f"❌ **{get_text('extraction_missing_parts_title')}**\n\n"
        msg += f"📄 {md_code(filename)}\n\n"
        msg += get_text('extraction_missing_parts_desc')

    elif extract_result == "corrupted":
        # Archivo corrupto o incompleto
        msg = f"❌ **{get_text('extraction_corrupted_title')}**\n\n"
        msg += f"📄 {md_code(filename)}\n\n"
        msg += get_text('extraction_corrupted_desc')

    elif extract_result == "password":
        # Cifrado: el bot no tiene forma de pedir la contraseña
        msg = f"❌ **{get_text('extraction_password_title')}**\n\n"
        msg += f"📄 {md_code(filename)}\n\n"
        msg += get_text('extraction_password_desc')

    elif extract_result == "no_space":
        # Lo descomprimido no cabe en el disco
        msg = f"❌ **{get_text('extraction_no_space_title')}**\n\n"
        msg += f"📄 {md_code(filename)}\n\n"
        msg += get_text('extraction_no_space_desc')

    else:
        # Error general en la extracción
        msg = f"❌ **{get_text('extraction_error_title')}**\n\n"
        msg += f"📄 {md_code(filename)}\n\n"
        msg += get_text('extraction_error_desc')

    # Tras un fallo el comprimido sigue ahí: se puede volver a su ficha
    buttons = [[
        Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
        Button.inline(get_text("button_close"), data="close")
    ]]
    return msg, buttons

# Extensión de un audio que llega sin nombre, según su tipo MIME. Las notas de
# voz son audio/ogg (Opus) y se guardaban como .mp3. Solo extensiones de
# EXTENSIONS_AUDIO: con la .oga que da mimetypes, /manage lo enviaría como
# documento. Lo que no está aquí sigue siendo .mp3
AUDIO_EXTENSIONS_BY_MIME = {
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/wav": ".wav", "audio/x-wav": ".wav",
}

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
                extension = AUDIO_EXTENSIONS_BY_MIME.get((media.mime_type or "").lower(), ".mp3")
                return f"audio_{media.id}{extension}"
            return f"file_{media.id}"
        return file_name
    elif isinstance(media, Photo):
        return f"photo_{media.id}.jpg"
    else:
        return f"file_{media.id}"



# Anclado: el comando, opcionalmente con @bot, y fin o argumentos. Sin anclar,
# "/listado" o "/list video" entraban aquí, no casaban con ningún comando y el
# mensaje del usuario se borraba sin respuesta. Un fichero cuyo pie empieza por
# un comando es de handle_files: sin el filtro se borraba mientras se descargaba
# (lo mismo con /settings, que se registra en handlers/settings.py)
@bot.on(events.NewMessage(pattern=r"^/(start|donate|version|donors|list|manage)(?:@\w+)?(?:\s|$)",
                          func=lambda e: not is_file_message(e)))
async def handle_start(event):
    # "/list@OtroBot" es para otro bot del grupo: ni se borra ni se contesta
    if await for_another_bot(event):
        return

    if not is_admin(event.sender_id):
        debug(f"[AUTH] User {event.sender_id} is not an admin and tried to use the bot")
        await warn_not_admin(event)
        return

    # Borrar el comando del usuario para mantener el chat limpio. Solo el del
    # admin: el de otro, en un grupo, no es cosa del bot
    try:
        await event.delete()
    except:
        pass

    command = event.pattern_match.group(1)
    stats.count(f"cmd_{command}")
    # Lo que va tras el comando (solo /list lo usa: /list <categoría>)
    has_args = len(event.raw_text.split()) > 1
    # En un foro, la respuesta va al tema del comando y no a General
    topic = await get_topic(event)
    if command == "start":
        response = get_text("welcome_message")
        await safe_send_message(event.chat_id, response, parse_mode=PARSE_MODE, reply_to=topic)
    elif command == "donate":
        response = get_text("donate")
        await safe_send_message(event.chat_id, response, parse_mode=PARSE_MODE, reply_to=topic)
    elif command == "version":
        response = get_text("version", VERSION)
        await safe_send_message(event.chat_id, response, parse_mode=PARSE_MODE, reply_to=topic)
    elif command == "donors":
        await print_donors(event.chat_id, reply_to=topic)
    elif command == "list" and has_args:
        # /list video: directamente el listado de esa categoría
        debug(f"[LIST] /list command received with arguments: {event.raw_text}")
        await handle_list_files(event)
    elif command == "list":
        debug("[LIST] /list command received")

        # Mostrar menú de categorías
        buttons = get_category_buttons()
        debug(f"[LIST] Category buttons: {buttons}")
        msg = get_text("list_select_category")
        await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)
        debug("[LIST] Menu sent")
    elif command == "manage":
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
        buttons.append([Button.inline(get_text("button_close"), data="close")])

        msg = get_text("manage_select_category")
        await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)

@bot.on(events.CallbackQuery(data=lambda data: data.startswith(b"cancel:")))
async def cancel_download(event):
    if await check_admin_and_warn(event):
        return

    # Los botones del bot siempre llevan una clave (ver TASK_KEY_RE); un data
    # fabricado a mano ("cancel:abc", bytes que no son UTF-8, el id de una
    # conversión) no tiene nada que cancelar. UnicodeDecodeError también es
    # un ValueError
    try:
        key = event.data.decode().split(":", 1)[1]
    except ValueError:
        key = ""
    if not TASK_KEY_RE.fullmatch(key):
        await safe_answer(event)
        return
    task = active_tasks.get(key)

    # Solo se para la tarea: quien la lanzó ve la cancelación y limpia sus
    # temporales (y, en una playlist, rescata los vídeos ya terminados)
    if isinstance(task, asyncio.subprocess.Process) and task.returncode is None:
        try:
            if os.getpgid(task.pid) == task.pid:
                # yt-dlp va en su propio grupo (ver stop_ytdlp): se para con
                # el ffmpeg que haya lanzado, que si no seguiría grabando
                os.killpg(task.pid, signal.SIGTERM)
            else:
                task.terminate()
        except ProcessLookupError:
            # Terminó justo antes de llegar aquí
            await safe_delete(event)
            return
        await safe_answer(event, get_text("cancelling"))
    elif isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        await safe_answer(event, get_text("cancelling"))
    else:
        # Ya fue cancelada o terminada, borrar el mensaje
        await safe_delete(event)

def content_disposition_filename(content_disposition):
    """El nombre de fichero de una cabecera Content-Disposition, o None.

    `filename*=UTF-8''Informe%20final.pdf` (RFC 6266/5987) va codificado y,
    si viene, manda sobre `filename=`, que los servidores suelen dejar como
    alternativa en ASCII para clientes antiguos.
    """
    extended = re.search(r"filename\*\s*=\s*([\w!#$&+.^`|~-]+)'[^']*'([^;\s]+)", content_disposition, re.IGNORECASE)
    if extended:
        charset, value = extended.groups()
        try:
            return unquote(value, encoding=charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            return unquote(value)
    plain = re.search(r'filename\s*=\s*("([^"]*)"|[^;\n]*)', content_disposition, re.IGNORECASE)
    if plain:
        name = plain.group(2) if plain.group(2) is not None else plain.group(1)
        name = name.strip().strip('\'"')
        # Muchos servidores mandan aquí el UTF-8 en bruto y requests lo lee
        # como latin-1 ("CanciÃ³n.pdf"): se reinterpreta como los navegadores.
        # Un latin-1 de verdad ("Canci\xf3n") no es UTF-8 válido y se queda igual
        try:
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return name or None
    return None


# Saltos de redirección que se siguen, como mucho, al analizar o descargar un
# enlace directo
MAX_REDIRECTS = 10
# Tope total del HEAD de is_direct_download_url, redirecciones incluidas
HEAD_TIMEOUT = 15


class PrivateRedirectError(Exception):
    """Una redirección lleva de un enlace público a la red local, o a un host
    que no se puede comprobar (ver checked_redirect)."""


# Un host ya pasado a ASCII: letras, números, puntos, guiones y los dos puntos
# de una IPv6. Un % que quede es un doble codificado (%252E), y una @ o una /
# cambiarían a qué equipo va la petición
SAFE_HOST_RE = re.compile(r'[0-9a-z._:-]+')


def _host_is_private(host):
    """True si `host` resuelve a alguna dirección que no es pública: la red
    local, loopback o link-local (los metadatos de la nube). Si no resuelve,
    OSError o UnicodeError."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return any(not ipaddress.ip_address(info[4][0]).is_global for info in infos)


def resolves_to_private(url):
    """_host_is_private con el host de `url`, el enlace que pega el admin."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        return _host_is_private(host)
    except (OSError, UnicodeError):
        # Si no resuelve, la petición fallará por su cuenta
        return False


def checked_redirect(chain_private, target):
    """Comprueba un salto de redirección antes de seguirlo.

    El admin puede pegar una dirección de su red (su NAS, p. ej.), pero las
    redirecciones las pone el servidor: un enlace público que lleva al router
    o a otro equipo de la LAN haría que el bot le mandase la petición desde
    dentro. Un destino privado solo se sigue si toda la cadena hasta él
    (`chain_private`) lo era; si no, PrivateRedirectError.

    Se comprueba el mismo host que se va a pedir: wget y requests decodifican
    los %XX del host y getaddrinfo no, así que "192%2E168%2E1%2E1" no resolvía,
    pasaba por público y wget acababa en 192.168.1.1. Por eso devuelve la URL
    con el host decodificado y en ASCII, que es la que hay que pedir, y si la
    cadena sigue siendo privada con este salto. Con la cadena pública, un host
    que no resuelve tampoco se sigue: no se sabe adónde acabaría yendo.
    """
    try:
        parts = urlsplit(target)
        host = unquote(parts.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        port = parts.port
    except (UnicodeError, ValueError):
        raise PrivateRedirectError(target)
    if not SAFE_HOST_RE.fullmatch(host):
        raise PrivateRedirectError(target)
    netloc = f'[{host}]' if ':' in host else host
    if port is not None:
        netloc += f':{port}'
    userinfo = parts.netloc.rpartition('@')[0]
    if userinfo:
        netloc = f'{userinfo}@{netloc}'
    url = urlunsplit(parts._replace(netloc=netloc))
    try:
        target_private = _host_is_private(host)
    except (OSError, UnicodeError):
        if not chain_private:
            raise PrivateRedirectError(target)
        target_private = False
    if target_private and not chain_private:
        raise PrivateRedirectError(target)
    return url, target_private


def head_following_redirects(url):
    """requests.head siguiendo a mano las redirecciones, cada una comprobada.

    Todos los saltos van en una misma sesión, como cuando requests las seguía
    por su cuenta: la cookie que pone una redirección llega al siguiente.
    """
    chain_private = None
    with requests.Session() as session:
        for _ in range(MAX_REDIRECTS + 1):
            response = session.head(url, allow_redirects=False, timeout=5)
            if not response.is_redirect:
                return response
            # http.client lee las cabeceras como latin-1: un Location con el
            # UTF-8 en bruto ("/files/Canción.pdf") llegaba como "CanciÃ³n" y
            # el siguiente salto daba 404. Se reinterpreta como hacía requests
            # siguiéndolas por su cuenta (ver content_disposition_filename)
            location = response.headers["Location"]
            try:
                location = location.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            target = urljoin(url, location)
            if chain_private is None:
                chain_private = resolves_to_private(url)
            url, chain_private = checked_redirect(chain_private, target)
    raise requests.TooManyRedirects(f"More than {MAX_REDIRECTS} redirects")


async def run_in_daemon_thread(func, *args, timeout):
    """`func(*args)` en un hilo propio, esperando como mucho `timeout` segundos.

    El timeout de requests vale para cada lectura, no para la respuesta
    entera: un servidor que manda las cabeceras a goteo dejaba el HEAD
    esperando para siempre. Con asyncio.to_thread ese hilo era del executor
    por defecto, el que usan las copias, las extracciones y otras llamadas
    bloqueantes (FastTelethon tiene su propio _IO_EXECUTOR), y la parada del
    bot se quedaba esperándolo. Un hilo daemon no retiene nada: al vencer el
    plazo se abandona y salta TimeoutError.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(setter, value):
        if not future.done():
            setter(value)

    def worker():
        try:
            outcome = (future.set_result, func(*args))
        except Exception as e:
            outcome = (future.set_exception, e)
        try:
            loop.call_soon_threadsafe(settle, *outcome)
        except RuntimeError:
            pass  # El bucle ya se cerró: nadie espera el resultado

    threading.Thread(target=worker, daemon=True).start()
    return await asyncio.wait_for(future, timeout)


async def head_unless_unanswered(url):
    """El HEAD de `url` (ver head_following_redirects), o None si no contesta.

    Lanza PrivateRedirectError si una redirección lleva a la red local. Un
    servidor lento, o que no acepta HEAD, no quiere decir que el enlace no
    valga: con None sigue hacia yt-dlp. Al log va el tipo del error y no su
    texto, que repite la URL sin pasar por redact_url.
    """
    try:
        return await run_in_daemon_thread(head_following_redirects, url, timeout=HEAD_TIMEOUT)
    except (TimeoutError, requests.RequestException) as e:
        debug(f"[DIRECT_DOWNLOAD] No answer to HEAD ({type(e).__name__}), leaving it to yt-dlp: {redact_url(url)}")
        return None


async def is_direct_download_url(url):
    """
    Detecta si una URL es un enlace directo a un archivo descargable.
    Retorna (is_direct, filename, content_type, download_path, icon) donde:
    - is_direct: True si es descarga directa
    - filename: Nombre del archivo extraído de la URL
    - content_type: Tipo de contenido detectado
    - download_path: Ruta de descarga según el tipo
    - icon: Icono según el tipo de archivo
    Lanza PrivateRedirectError si el enlace redirige a la red local: tampoco
    hay que pasárselo a yt-dlp, que seguiría la misma redirección.
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

        # Una página web nunca es descarga directa: aunque .html cuente como
        # ebook al llegar por Telegram, aquí es una página (una noticia con
        # vídeo, p. ej.) y la tiene que analizar yt-dlp
        path_lower = path.lower()
        if path_lower.endswith(tuple(EXTENSIONS_WEBPAGE)):
            debug(f"[DIRECT_DOWNLOAD] Web page, not a direct download: {redact_url(url)}")
            # Sus redirecciones sí se comprueban: sin el HEAD, a un enlace le
            # bastaba acabar en .html para que yt-dlp siguiera la suya a la
            # red local
            await head_unless_unanswered(url)
            return False, None, None, None, None

        # Verificar si termina con una extensión conocida
        for ext in all_extensions:
            if path_lower.endswith(ext):
                # Extraer nombre del archivo
                filename = sanitize_filename(os.path.basename(path))

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

        # Si no es directo, intentar HEAD request para verificar Content-Type.
        # Si no responde a tiempo, no es descarga directa y sigue yt-dlp
        try:
            response = await head_unless_unanswered(url)
            if response is None:
                return False, None, None, None, None
            content_type_header = response.headers.get('Content-Type', '').lower()
            content_disposition = response.headers.get('Content-Disposition', '')

            # Si tiene Content-Disposition con filename, es descarga directa
            if 'attachment' in content_disposition or 'filename' in content_disposition:
                filename = content_disposition_filename(content_disposition) or os.path.basename(path) or 'download'
                # El nombre lo elige el servidor: sin sanear, "../../x" o "/x"
                # escribirían fuera de la carpeta de descargas
                filename = sanitize_filename(os.path.basename(filename.replace('\\', '/')) or 'download')

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
        except PrivateRedirectError:
            raise
        except Exception:
            # Sin el except desnudo de antes: se tragaba también la cancelación,
            # y Cancelar durante el HEAD seguía con yt-dlp como si nada
            pass

        return False, None, None, None, None

    except PrivateRedirectError:
        raise
    except Exception as e:
        debug(f"[DIRECT_DOWNLOAD] Error detecting direct download: {redact_urls(e)}")
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


# Copias de cookies.txt que tienen los yt-dlp en marcha, cada una con la firma
# (mtime, tamaño) que tenía el original al copiarlo
_ytdlp_cookie_copies = {}


def add_ytdlp_cookies(cmd):
    """Añade cookies al comando yt-dlp si existe el fichero configurado.

    yt-dlp no recibe el fichero del admin sino una copia propia, que hay que
    soltar con release_ytdlp_cookies cuando termine. yt-dlp reescribe al salir
    el fichero que se le pasa: con el original, los que iban a la vez se
    pisaban entre sí y pisaban un cookies.txt recién exportado, y en un volumen
    de solo lectura salían con error aunque la extracción hubiera ido bien.
    """
    if not os.path.isfile(YTDLP_COOKIES_FILE):
        return cmd

    try:
        with open(YTDLP_COOKIES_FILE, "rb") as original:
            signature = os.fstat(original.fileno())
            fd, cookies_copy = tempfile.mkstemp(prefix="cookies_", suffix=".txt", dir=TEMP_DIR)
            with os.fdopen(fd, "wb") as copy:
                shutil.copyfileobj(original, copy)
    except OSError as e:
        # Sin copia, sin cookies: el original no se le da nunca a yt-dlp
        warning(f"[YT-DLP] Could not copy cookies file, going on without cookies: {e}")
        return cmd

    _ytdlp_cookie_copies[cookies_copy] = (signature.st_mtime_ns, signature.st_size)
    debug(f"[YT-DLP] Using cookies file: {YTDLP_COOKIES_FILE} (copy: {cookies_copy})")
    return [cmd[0], "--cookies", cookies_copy, *cmd[1:]]


def release_ytdlp_cookies(cmd, save_renewed=False):
    """Borra la copia de cookies que add_ytdlp_cookies puso en `cmd`.

    Con `save_renewed`, antes pasa al original lo que haya renovado yt-dlp,
    pero solo si se puede escribir (en un volumen de solo lectura no) y nadie
    lo ha cambiado desde que se copió: un cookies.txt recién exportado manda
    sobre lo que guarda una descarga que empezó antes. Se escribe encima, como
    hacía yt-dlp, para no cambiarle dueño ni permisos, y sin awaits por medio,
    así que ninguna otra copia ni otro volcado se cuela a la mitad.
    """
    cookies_copy = next((arg for arg in cmd if arg in _ytdlp_cookie_copies), None)
    if cookies_copy is None:
        return

    signature = _ytdlp_cookie_copies.pop(cookies_copy)
    if save_renewed:
        try:
            current = os.stat(YTDLP_COOKIES_FILE)
            if not os.access(YTDLP_COOKIES_FILE, os.W_OK):
                debug("[YT-DLP] Cookies file is read-only, renewed cookies are not saved")
            elif (current.st_mtime_ns, current.st_size) != signature:
                debug("[YT-DLP] Cookies file changed during the download, keeping it")
            else:
                shutil.copyfile(cookies_copy, YTDLP_COOKIES_FILE)
                debug("[YT-DLP] Renewed cookies saved")
        except OSError as e:
            warning(f"[YT-DLP] Could not save renewed cookies: {e}")
    try:
        os.remove(cookies_copy)
    except OSError as e:
        warning(f"[YT-DLP] Could not remove cookies copy {cookies_copy}: {e}")


# Tope del análisis de un enlace con yt-dlp. Su timeout de red es por lectura:
# un servidor que suelta un byte de vez en cuando lo tenía esperando para
# siempre. Es holgado porque recorrer un canal enorme lleva minutos, y para
# cortar antes está el Cancelar del mensaje de análisis
YTDLP_DETECT_TIMEOUT = 600


async def stop_ytdlp(proc, *readers):
    """Mata un yt-dlp que siga vivo, con todo lo que haya lanzado, y espera.

    yt-dlp se lanza en su propio grupo de procesos (start_new_session): con
    kill() solo moría él, y el ffmpeg de un directo o de un HLS, que hereda sus
    tuberías, seguía grabando y la lectura no acababa nunca. `readers` son las
    tareas que aún leen su salida: se paran antes de vaciar las tuberías, que
    hay que vaciar porque, con el búfer lleno y nadie leyendo, asyncio deja de
    mirarlas, no ve que se cierran y wait() no vuelve.
    """
    readers = [reader for reader in readers if reader is not None]
    for reader in readers:
        reader.cancel()
    if readers:
        await asyncio.wait(readers)
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await proc.communicate()


async def detect_playlist(url):
    """Detecta si una URL es una playlist y retorna (is_playlist, playlist_count, playlist_title)

    Si yt-dlp no acaba en YTDLP_DETECT_TIMEOUT, lanza TimeoutError: tomarla
    por un vídeo suelto bajaría la playlist entera, sin preguntar ni tope.
    """
    proc = None
    try:
        cmd = ["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", url]
        # Con el modo fijo en /settings no se enseña el total, y recorrer un
        # canal enorme lleva minutos: basta con contar hasta pasarse del tope
        mode, limit = settings.playlist_mode(), settings.playlist_limit()
        if mode == "FIRST" or (mode == "FULL" and limit):
            cmd[-1:-1] = ["--playlist-end", str(2 if mode == "FIRST" else limit + 1)]
        cmd = add_ytdlp_cookies(cmd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # Ver stop_ytdlp
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), YTDLP_DETECT_TIMEOUT)

        # Mostrar stderr si hay contenido
        stderr_output = stderr.decode().strip()
        if stderr_output:
            for line in stderr_output.splitlines():
                debug(f"[YT-DLP] Playlist detect stderr: {redact_urls(line)}")

        if proc.returncode == 0:
            lines = stdout.decode().splitlines()

            if len(lines) > 1:
                # Múltiples líneas JSON = playlist
                debug(f"[YT-DLP] Playlist detected with {len(lines)} items")

                # Intentar obtener el título de la playlist del primer item
                try:
                    first_item = json.loads(lines[0])
                    playlist_title = first_item.get("playlist_title") or first_item.get("playlist") or "Playlist"
                except:
                    playlist_title = "Playlist"
                # Lo pone quien creó la playlist: sin tope, uno enorme pasaba
                # de los 4096 caracteres y Telegram rechazaba la pregunta
                playlist_title = str(playlist_title)[:200]

                return True, len(lines), playlist_title
            elif len(lines) == 1:
                # Una sola línea = video individual
                debug("[YT-DLP] Single video detected")
                return False, 1, None
        else:
            warning(f"[YT-DLP] Playlist detect failed with code {proc.returncode}")

        return False, 1, None
    except TimeoutError:
        warning(f"[YT-DLP] Playlist detect took more than {YTDLP_DETECT_TIMEOUT}s, stopped")
        raise
    except Exception as e:
        error(f"[YT-DLP] Error detecting playlist: {e}")
        return False, 1, None
    finally:
        # Al vencer el tiempo o al cancelar el análisis, yt-dlp sigue vivo
        await stop_ytdlp(proc)
        release_ytdlp_cookies(cmd)

async def detect_content_type(url):
    """Detecta el tipo de contenido sin descargarlo usando yt-dlp --dump-json"""
    proc = None
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--skip-download", "--playlist-items", "1", url]
        cmd = add_ytdlp_cookies(cmd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # Ver stop_ytdlp
        )

        # Si vence el tiempo, el except lo da por desconocido
        stdout, stderr = await asyncio.wait_for(proc.communicate(), YTDLP_DETECT_TIMEOUT)

        # Mostrar stderr si hay contenido
        stderr_output = stderr.decode().strip()
        if stderr_output:
            for line in stderr_output.splitlines():
                debug(f"[YT-DLP] Detect stderr: {redact_urls(line)}")

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
        warning(f"[YT-DLP] Error detecting content type: {e!r}")
        return "unknown"
    finally:
        await stop_ytdlp(proc)
        release_ytdlp_cookies(cmd)

def ytdlp_settings_args(is_audio):
    """Las opciones de yt-dlp que salen de /settings: formato, calidad,
    etiquetas, SponsorBlock y tamaño máximo.

    El vídeo se limita con `-S res:N` y no con un filtro `[height<=N]`: res
    es el lado corto, así que un vídeo vertical de 1080x1920 cuenta como
    1080p, y si no hay nada por debajo del límite yt-dlp baja lo más pequeño
    que haya en vez de fallar.

    Preferir lo compatible pide H.264 y AAC, que Telegram reproduce y yt-dlp
    junta directamente en un MP4: sin eso, YouTube da AV1 o VP9 y cada vídeo
    pasa por la conversión completa. El orden importa: el códec de vídeo va
    antes que la resolución (YouTube no tiene H.264 por encima de 1080p, y es
    lo que se acepta a cambio), y la resolución antes que el audio, porque si
    no gana el único formato que trae AAC dentro, el combinado de 360p.

    El audio acaba siempre en `/best`: hay sitios que solo sirven vídeo con
    el audio dentro, y `bestaudio` a secas no acepta esos formatos y falla.
    Así se baja el combinado y se le saca el audio.
    """
    if is_audio:
        if settings.audio_format() == "M4A":
            # El AAC que ya trae el vídeo, sin recodificar si lo hay
            args = ["-f", "bestaudio[ext=m4a]/bestaudio/best", "--extract-audio", "--audio-format", "m4a"]
        else:
            args = ["-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3"]
            bitrate = settings.AUDIO_BITRATES[settings.audio_quality()]
            if bitrate is not None:
                args += ["--audio-quality", bitrate]
        if settings.audio_tags():
            # La miniatura de YouTube suele ser WebP, que un MP3 no admite
            args += ["--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg"]
    else:
        args = ["-f", "bv*+ba/best"]
        quality = settings.video_quality()
        compatible = settings.prefer_compatible()
        order = []
        if compatible:
            order.append("vcodec:h264")
        if quality != "MAX":
            order.append(f"res:{quality}")
        elif compatible:
            order.append("res")
        if compatible:
            order.append("acodec:aac")
        if order:
            args += ["-S", ",".join(order)]

    categories = settings.SPONSORBLOCK_CATEGORIES[settings.sponsorblock()]
    if categories:
        args += ["--sponsorblock-remove", categories]
    max_size = settings.max_size_mb()
    if max_size:
        args += ["--max-filesize", f"{max_size}M"]
    return args


def ytdlp_output_template(timestamp):
    """Plantilla `-o` para yt-dlp, con la marca temporal que se limpia después.

    El índice de playlist va con la sintaxis condicional de yt-dlp
    (`%(campo&valor|alternativa)s`): si el campo no existe no escribe nada. Con
    `%(playlist_index)s` a secas, yt-dlp resuelve el campo ausente como "NA" y
    cualquier vídeo suelto se descargaba como "NA-Título.mp4".
    """
    return f"%(playlist_index&{{}}-|)s%(title).200s_temp{timestamp}.%(ext)s"


def ytdlp_temp_marker(timestamp):
    """Marca que llevan todos los ficheros de una descarga en TEMP_DIR.

    Identifica los temporales de esa descarga sin tocar los de otras que
    estén en curso a la vez.
    """
    return f"_temp{timestamp}"


# Lo que imprime yt-dlp (--print after_move) cuando ha acabado del todo con un
# vídeo de una playlist entera: ya mezclado, convertido y con su ruta final
YTDLP_DONE_PREFIX = "DROPBOT_DONE:"


# Ficheros intermedios de yt-dlp: formatos sueltos antes de mezclar, que van
# justo tras la marca temporal (titulo_temp123.f137.mp4,
# titulo_temp123.fdash-video=1.mp4), descargas a medias (.part, .part-Frag3),
# estado de reanudación (.ytdl) y temporales del merger (.temp.mp4)
YTDLP_PARTIAL_RE = re.compile(r'_temp\d+\.f[\w=+-]+\.\w+$|\.part(-Frag\d+)?$|\.ytdl$|\.temp\.\w+$')


def is_ytdlp_partial(path):
    return bool(YTDLP_PARTIAL_RE.search(os.path.basename(path)))


def is_ytdlp_output(path, marker):
    """Si `path` es un fichero terminado de la descarga con esa marca.

    Tiene que estar en TEMP_DIR, llevar la marca, no ser un intermedio y ser
    un fichero normal (no un enlace). Las rutas salen de lo que imprime
    yt-dlp, que también repite texto de un tercero, como el título de una
    playlist: con uno que imitara la línea del Merger, el bot movía a la
    carpeta de descargas (y borraba) cualquier fichero del contenedor.
    """
    if not marker or marker not in os.path.basename(path) or is_ytdlp_partial(path):
        return False
    try:
        temp_dir = os.path.realpath(TEMP_DIR)
        if os.path.commonpath([os.path.realpath(path), temp_dir]) != temp_dir:
            return False
        return stat.S_ISREG(os.lstat(path).st_mode)
    except (OSError, ValueError):
        # Una ruta con un NUL (ValueError) tampoco es un fichero de esta
        # descarga: la excepción llegaba hasta run_url_download, que daba la
        # descarga por fallida y borraba de TEMP_DIR los vídeos buenos
        return False


def clean_temp_filename(filename):
    """Quita la marca temporal del nombre: "video_temp123.mp4" -> "video.mp4".

    Solo la última: el título puede contener algo parecido ("Prueba_temp2.final")
    y eso es parte del nombre.
    """
    return re.sub(r'_temp\d+(\.[^.]*)$', r'\1', filename)


async def move_download_to(temp_file_path, final_output_dir):
    """Mueve un fichero de TEMP_DIR a su carpeta final con un nombre libre.

    El nombre se busca en la carpeta final (no en TEMP_DIR) y no se ocupa hasta
    tener el contenido entero (ver move_to_unique_path). Va en un hilo: entre
    dispositivos copia todo el contenido y bloquearía el bot.
    """
    final_file_path = await asyncio.to_thread(
        move_to_unique_path, temp_file_path, final_output_dir,
        clean_temp_filename(os.path.basename(temp_file_path)),
    )
    debug(f"[URL DOWNLOAD] ✅ File moved: {temp_file_path} -> {final_file_path}")
    return final_file_path


async def rescue_completed_playlist_files(marker, final_output_dir):
    """Al cancelar una playlist, mueve a su destino los vídeos ya terminados.

    Solo mira los ficheros de esta descarga (`marker`) y descarta los
    intermedios de yt-dlp. Se llama con yt-dlp ya parado, para no mover nada
    que siga escribiendo. Devuelve cuántos ficheros se han movido.

    Un audio a medio convertir deja juntos el original y el que escribe
    ffmpeg (sin .temp), con el mismo nombre y otra extensión: yt-dlp borra el
    original en cuanto ffmpeg termina. No se rescata ninguno de los dos, que
    serían un audio cortado y un original que no se ha pedido.
    """
    pattern = os.path.join(TEMP_DIR, f"*{glob.escape(marker)}*")
    candidates = [
        path for path in glob.glob(pattern)
        if is_ytdlp_output(path, marker)
        and path.lower().endswith(('.mp4', '.webm', '.mkv', '.m4a', '.mp3', '.opus'))
    ]
    stems = [os.path.splitext(path)[0] for path in candidates]
    completed = sorted(path for path, stem in zip(candidates, stems) if stems.count(stem) == 1)
    moved = 0
    for path in completed:
        try:
            await move_download_to(path, final_output_dir)
            moved += 1
        except Exception as e:
            error(f"[CANCEL] Error moving file {path}: {e}")
    debug(f"[CANCEL] Rescued {moved} completed playlist file(s)")
    return moved


def remove_temp_files(marker):
    """Borra de TEMP_DIR los ficheros de una descarga (los que llevan `marker`)."""
    for path in glob.glob(os.path.join(TEMP_DIR, f"*{glob.escape(marker)}*")):
        try:
            if os.path.isfile(path):
                os.remove(path)
                debug(f"[URL_DOWNLOAD] Temporary file removed: {path}")
        except OSError as e:
            warning(f"[URL_DOWNLOAD] Could not remove temporary file {path}: {e}")


# Lo que se analiza va al principio de la línea (prefijos, rutas acotadas por
# %(title).200s): una línea enorme, como el <title> de una página, se recorta
PROGRESS_LINE_MAX = 64 * 1024


async def iter_progress_lines(stream, chunk_size=4096, max_line=PROGRESS_LINE_MAX):
    """Recorre la salida de un subproceso partiendo por \n y también por \r.

    wget dibuja la barra de progreso reescribiendo la misma línea con \r, sin
    saltos de línea. Leyendo con readline() el progreso no llegaba hasta el
    final y, pasados unos minutos, la "línea" superaba el límite de 64 KB del
    StreamReader: la lectura fallaba y wget se quedaba bloqueado.

    Cada línea se queda en sus primeros `max_line` bytes y lo demás se descarta
    hasta el siguiente salto. Los saltos se buscan solo en lo recién leído:
    volviendo a partir todo lo acumulado, una línea de varios MB sin saltos
    tenía el event loop ocupado durante minutos.
    """
    pending = bytearray()
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break
        *complete, rest = re.split(rb"[\r\n]", chunk)
        for part in complete:
            pending += part[:max_line - len(pending)]
            yield pending.decode(errors="replace")
            pending.clear()
        pending += rest[:max_line - len(pending)]
    if pending:
        yield pending.decode(errors="replace")


# Del stderr de ffmpeg se guarda el principio (donde falla la GPU o el códec)
# y el final (por qué ha terminado). Un vídeo dañado escribe varias líneas de
# error por fotograma: guardarlo entero eran cientos de MB en memoria y en el log
FFMPEG_STDERR_HEAD_BYTES = 16 * 1024
FFMPEG_STDERR_TAIL_BYTES = 64 * 1024


async def accumulate_process_stderr(proc, chunks):
    """Drena el stderr de un subproceso ffmpeg en segundo plano.

    Hay que llamarlo nada más crear el subproceso y esperar la tarea cuando el
    proceso termine. Si nadie lee stderr y ffmpeg vuelca más de lo que cabe en
    la tubería (~64 KiB), se queda bloqueado escribiendo para siempre sin
    fallar.

    Se lee hasta el final, pero en `chunks` solo quedan el principio y el
    final: lo de en medio se cambia por una línea con los bytes omitidos.
    """
    head = bytearray()
    tail = bytearray()
    skipped = 0
    try:
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                break
            room = FFMPEG_STDERR_HEAD_BYTES - len(head)
            if room > 0:
                head += chunk[:room]
                chunk = chunk[room:]
            tail += chunk
            if len(tail) > FFMPEG_STDERR_TAIL_BYTES:
                excess = len(tail) - FFMPEG_STDERR_TAIL_BYTES
                del tail[:excess]
                skipped += excess
    except Exception as exc:
        warning(f"[CONVERSION] Error draining ffmpeg stderr: {exc}")
    if head:
        chunks.append(bytes(head))
    if skipped:
        chunks.append(f"\n[... {skipped} bytes skipped ...]\n".encode())
    if tail:
        chunks.append(bytes(tail))


def build_ffmpeg_conversion_command(input_path, output_path, hardware=None, quality=None, plan=None):
    """Construye el comando FFmpeg para convertir vídeo a formato Telegram.

    Con aceleración hardware el pipeline es completo en GPU (decodificado y
    codificado). Si la GPU no soporta el códec de entrada, ffmpeg falla y la
    llamada se reintenta en software.

    Si `plan` (de telegram_conversion_plan) dice que el vídeo ya es H.264
    válido, no se recodifica: se copia la pista de vídeo y como mucho se
    convierte el audio a AAC, lo que tarda segundos en vez de minutos.

    Siempre con -nostats: el progreso llega por -progress y las estadísticas
    solo engordarían el stderr, que se guarda para el log si algo falla.
    """
    if plan is not None and plan["video"] == "copy":
        audio_codec = "aac" if plan["audio"] == "encode" else "copy"
        command = [
            "ffmpeg",
            "-nostats",
            "-i", input_path,
            "-map", "0:V:0",
            "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", audio_codec,
        ]
        if plan.get("hvc1"):
            command.extend(["-tag:v", "hvc1"])
        command.extend([
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-y",
            output_path,
        ])
        return command

    hardware = (hardware or settings.ffmpeg_hw()).strip().upper()
    hw_profiles = {
        "NONE": ("libx264", []),
        "VAAPI": ("h264_vaapi", [
            "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
            "-vaapi_device", "/dev/dri/renderD128",
        ]),
        "NVENC": ("h264_nvenc", [
            "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
        ]),
        "QSV": ("h264_qsv", [
            "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
            "-qsv_device", "/dev/dri/renderD128",
        ]),
    }
    try:
        video_encoder, hw_decode_args = hw_profiles[hardware]
    except KeyError:
        raise ValueError(f"Unknown FFmpeg hardware mode: {hardware}")

    command = ["ffmpeg", "-nostats"]
    command.extend(hw_decode_args)

    command.extend(["-i", input_path])

    command.extend(["-c:v", video_encoder])
    if hardware == "NONE":
        # Telegram solo reproduce H.264 4:2:0 de 8 bits: sin forzarlo, libx264
        # conserva el 4:4:4 o los 10 bits del origen (High 4:4:4 / High 10).
        # El 4:2:0 exige ancho y alto pares, así que se redondean (un 161x121
        # haría fallar la conversión). Solo en software: con GPU el frame está
        # en memoria de vídeo y estos filtros no valen; si el encoder hardware
        # no acepta el formato del origen, ffmpeg falla y se reintenta aquí
        command.extend([
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-pix_fmt", "yuv420p",
        ])
    command.extend([
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-y",
        output_path,
    ])

    if quality is not None and str(quality).strip() != "":
        quality = str(quality).strip()
        quality_args_map = {
            "NONE": ["-crf", quality],
            "VAAPI": ["-qp", quality],
            "NVENC": ["-rc:v", "vbr", "-cq", quality, "-b:v", "0"],
            "QSV": ["-global_quality", quality],
        }
        try:
            quality_args = quality_args_map[hardware]
        except KeyError:
            raise ValueError(f"Unknown FFmpeg hardware mode: {hardware}")
        # Los parámetros de calidad son opciones de salida y deben ir antes
        # del fichero de destino.
        command[-1:-1] = quality_args

    return command


URL_IN_TEXT_RE = re.compile(r'https?://\S+')


# Un fichero cuyo pie empieza por un enlace es de handle_files: sin el filtro,
# el pie lanzaba otra descarga que pisaba la suya en active_tasks
@bot.on(events.NewMessage(pattern=URL_IN_TEXT_RE, func=lambda e: not is_file_message(e)))
async def handle_url_link(event):
    if await check_admin_and_warn(event):
        return

    # Solo el enlace: el patrón acepta un mensaje que empieza por la URL y
    # sigue con texto ("https://... mira esto"), y ese texto no puede acabar
    # dentro de la URL que se le pasa a la detección y a yt-dlp
    url = URL_IN_TEXT_RE.search(event.raw_text).group(0)
    # La misma clave que su descarga, con el chat (ver task_key): la llevan
    # los botones del enlace y cabe en sus 64 bytes
    url_id = task_key(event)
    claimed_urls.discard(url_id)
    # Almacenar URL con información de playlist (se actualizará si es playlist)
    pending_urls[url_id] = {"url": url, "playlist_count": 1}

    # El análisis puede tardar (yt-dlp recorre la playlist entera): mientras
    # dura, el Cancelar de su mensaje para este handler, como a una descarga
    key = url_id
    active_tasks[key] = asyncio.current_task()
    analyzing_msg = None
    try:
        # Mostrar mensaje de análisis
        buttons = [Button.inline(get_text("button_cancel"), data=f"cancel:{key}")]
        analyzing_msg = await safe_reply(event, get_text("analyzing_url"), buttons=buttons, wait_for_result=True, parse_mode=PARSE_MODE)

        # Si la cola no lo ha enviado en su tiempo, se sigue sin él. Otro
        # mandado sin esperar llegaría cuando la cola se recupere, sin que
        # nadie guarde su referencia, y se quedaría para siempre en el chat
        if analyzing_msg is None:
            warning("[URL_DOWNLOAD] Could not create the analysis message - continuing without it")

        await analyze_url(event, url, url_id, analyzing_msg)
    except asyncio.CancelledError:
        # El yt-dlp que analizaba ya lo ha parado su detect_*
        pending_urls.pop(url_id, None)
        await handle_cancel(analyzing_msg)
        raise
    finally:
        # Si ha lanzado una descarga, la entrada ya es la de la descarga
        if active_tasks.get(key) is asyncio.current_task():
            active_tasks.pop(key, None)


async def analyze_url(event, url, url_id, analyzing_msg):
    """Lo que sigue al mensaje de análisis: según el enlace, lo descarga ya o
    pregunta qué bajar editando `analyzing_msg`."""
    # Primero detectar si es descarga directa
    try:
        is_direct, filename, direct_type, download_path, icon = await is_direct_download_url(url)
    except PrivateRedirectError as e:
        warning(f"[DIRECT_DOWNLOAD] Blocked redirect to the local network: {redact_urls(e)}")
        pending_urls.pop(url_id, None)
        if analyzing_msg:
            await safe_edit(analyzing_msg, get_text("error_url_private_redirect"), buttons=None, parse_mode=PARSE_MODE)
        else:
            await safe_reply(event, get_text("error_url_private_redirect"), parse_mode=PARSE_MODE)
        return

    if is_direct:
        # Es descarga directa - descargar inmediatamente
        debug(f"[DIRECT_DOWNLOAD] Direct download detected: {filename} (type: {direct_type}) -> {download_path}")
        pending_urls.pop(url_id, None)

        # Editar mensaje de análisis. Sin él (timeout en cola) se descarga sin
        # mensaje de progreso, como en handle_url_link
        status_message = None
        if analyzing_msg:
            status_message = await safe_edit(
                analyzing_msg,
                get_text("downloading", icon),
                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{task_key(event)}")],
                parse_mode=PARSE_MODE,
                wait_for_result=True
            )

        # Iniciar descarga directa
        stats.count("url_direct")
        task = asyncio.create_task(run_direct_download(event, url, filename, status_message, download_path, icon, direct_type))
        active_tasks[task_key(event)] = task
        return

    # No es descarga directa - detectar si es playlist
    try:
        is_playlist, playlist_count, playlist_title = await detect_playlist(url)
    except TimeoutError:
        pending_urls.pop(url_id, None)
        if analyzing_msg:
            await safe_edit(analyzing_msg, get_text("analyzing_url_timeout"), buttons=None, parse_mode=PARSE_MODE)
        else:
            await safe_reply(event, get_text("analyzing_url_timeout"), parse_mode=PARSE_MODE)
        return

    if is_playlist:
        debug(f"[PLAYLIST] Detected playlist with {playlist_count} videos: {playlist_title}")
        # Almacenar el playlist_count para usarlo después, si la solicitud
        # sigue pendiente: si ya la ha reclamado un botón, es él quien sigue
        url_data = pending_urls.get(url_id)
        if url_data is None:
            return
        url_data["playlist_count"] = playlist_count

        # Con un modo fijo en /settings, seguir sin preguntar
        playlist_mode = settings.playlist_mode()
        if playlist_mode != "ASK":
            stats.count("url_playlist")
            await continue_playlist(event, analyzing_msg, url_id, playlist_mode.lower())
            return

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

    # Con un formato automático en /settings, descargar sin preguntar
    auto_format = settings.auto_format()
    if auto_format in ["VIDEO", "AUDIO"]:
        url_data = pending_urls.pop(url_id, None)
        playlist_count = url_data.get("playlist_count", 1) if url_data else 1
        stats.count(f"url_{auto_format.lower()}")
        await start_ytdlp_download(event, analyzing_msg, url, auto_format == "AUDIO", playlist_count)
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
    # Ya la ha reclamado un clic en Audio o Vídeo, y la edición que pone el
    # progreso (con su Cancelar) espera en la cola: borrar ahora el mensaje
    # dejaba la descarga en marcha sin nada a la vista para pararla
    if url_id in claimed_urls:
        await safe_answer(event, get_text("download_already_started"), alert=True)
        return
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

    # Buscar el proceso de conversión. Solo se marca si sigue en curso: una
    # marca sin conversión que la recoja se quedaría para siempre
    proc = active_tasks.get(conversion_id)
    if proc and proc.returncode is None:
        # Marcar la conversión como cancelada ANTES de terminar el proceso
        cancelled_conversions.add(conversion_id)
        debug(f"[CANCEL] Conversion marked as cancelled: {conversion_id}")
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

        except Exception as e:
            error(f"[CANCELAR] ❌ Error cancelando conversión {conversion_id}: {e}")
            await safe_edit(
                event,
                get_text("conversion_cancel_error"),
                parse_mode=PARSE_MODE
            )
    else:
        # La conversión ya terminó o no existe
        debug("[CANCEL] ⚠️ Process not found in active_tasks (already finished or does not exist)")
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

    proc = active_tasks.get(conversion_id)
    if not proc or proc.returncode is not None:
        # La conversión ya terminó: no dejar una marca que nadie va a recoger
        debug("[SEND_ORIGINAL] ⚠️ Conversion not found (already finished)")
        await safe_edit(event, get_text("conversion_not_found"), parse_mode=PARSE_MODE)
        return

    # Marcar la solicitud de envío original y cancelar la conversión
    send_original_requests.add(conversion_id)
    cancelled_conversions.add(conversion_id)
    debug(f"[SEND_ORIGINAL] Request marked: {conversion_id}")

    # Terminar el proceso de conversión
    if proc:
        try:
            debug(f"[SEND_ORIGINAL] Terminating conversion process (PID: {proc.pid})...")
            proc.terminate()
            debug("[SEND_ORIGINAL] ✅ Process terminated")
        except Exception as e:
            warning(f"[SEND_ORIGINAL] Error terminating process: {e}")

    # Actualizar mensaje
    await safe_edit(
        event,
        get_text("preparing_send"),
        parse_mode=PARSE_MODE
    )

# Solicitudes que ya ha reclamado un botón (su descarga está en marcha o ha
# terminado). Un segundo clic sobre el mismo mensaje no las encuentra en
# pending_urls, pero no ha caducado: el mensaje ya muestra el progreso de la
# descarga y no hay que taparlo con "solicitud expirada". Son ids de mensaje
# (con su chat), así que ocupan poco aunque no se vacíe.
claimed_urls = set()


def claim_pending_url(url_id):
    """Saca la solicitud de pending_urls (atómico) y la marca como reclamada."""
    url_data = pending_urls.pop(url_id, None)
    if url_data is not None:
        claimed_urls.add(url_id)
    return url_data


async def answer_unclaimable_url(target, url_id):
    """Un botón cuya solicitud ya no está en pending_urls.

    Si la reclamó otro clic (doble clic), no se dice nada: el clic ya se ha
    respondido y el mensaje es el del progreso. Si no, ha caducado (p. ej.
    tras reiniciar el bot) y se avisa.
    """
    if url_id in claimed_urls:
        debug(f"[URL_DOWNLOAD] Request {url_id} already claimed, ignoring repeated click")
        return
    if target is not None:
        await safe_edit(target, get_text("error_url_expired"), buttons=None, parse_mode=PARSE_MODE)


@bot.on(events.CallbackQuery(pattern=b"playlist_(full|first):(.+)"))
async def handle_playlist_selection(event):
    """Maneja la selección de descargar playlist completa o solo primer video"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    stats.count("url_playlist")
    playlist_mode = event.pattern_match.group(1).decode()  # "full" o "first"
    url_id = event.pattern_match.group(2).decode()

    debug(f"[PLAYLIST] User selected: {playlist_mode} for URL ID: {url_id}")
    await continue_playlist(event, event, url_id, playlist_mode)


async def continue_playlist(event, target, url_id, playlist_mode):
    """Sigue con una playlist una vez decidido si va entera ("full") o solo el
    primero ("first"): descarga ya si hay formato automático, o pregunta el
    formato editando `target` (el mensaje con los botones, o el de análisis).
    """
    url_data = pending_urls.get(url_id)
    if not url_data:
        await answer_unclaimable_url(target, url_id)
        return

    # Con un formato automático en /settings, no preguntar formato
    auto_format = settings.auto_format()
    if auto_format in ["AUDIO", "VIDEO"]:
        debug(f"[PLAYLIST] auto format={auto_format}, skipping format selection")
        # Reclamar atómicamente para evitar doble descarga por doble click:
        # el segundo click obtiene None y sale (el branch ASK no necesita esto)
        if claim_pending_url(url_id) is None:
            return
        await start_ytdlp_download(event, target, url_data["url"], auto_format == "AUDIO",
                                   url_data.get("playlist_count", 1), playlist_mode)
        return

    # Si el formato automático es ASK, preguntar el formato (audio o video)
    buttons = [
        [Button.inline(get_text("audio", AUD_ICO), data=f"playlistfmt_{playlist_mode}_audio:{url_id}"),
         Button.inline(get_text("video", VID_ICO), data=f"playlistfmt_{playlist_mode}_video:{url_id}")],
        [Button.inline(get_text("button_cancel"), data=f"simplecancel:{url_id}")]
    ]
    if target is not None:
        await safe_edit(target, get_text("dowload_asking"), buttons=buttons, parse_mode=PARSE_MODE)
    else:
        await safe_reply(event, get_text("dowload_asking"), buttons=buttons, parse_mode=PARSE_MODE)

@bot.on(events.CallbackQuery(pattern=b"playlistfmt_(full|first)_(audio|video):(.+)"))
async def handle_playlist_format_selection(event):
    """Maneja la selección de formato para playlist"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    playlist_mode = event.pattern_match.group(1).decode()  # "full" o "first"
    format_type = event.pattern_match.group(2).decode()  # "audio" o "video"
    url_id = event.pattern_match.group(3).decode()

    # pop atómico para evitar dobles ejecuciones por doble click (doble descarga)
    url_data = claim_pending_url(url_id)
    if not url_data:
        await answer_unclaimable_url(event, url_id)
        return

    url = url_data["url"]
    playlist_count = url_data.get("playlist_count", 1)
    is_audio = format_type == "audio"
    download_full_playlist = playlist_mode == "full"

    debug(f"[PLAYLIST] Downloading {'full playlist' if download_full_playlist else 'first video only'} as {format_type} (playlist_count: {playlist_count})")
    await start_ytdlp_download(event, event, url, is_audio, playlist_count, playlist_mode)

@bot.on(events.CallbackQuery(pattern=b"url_(audio|video):(.+)"))
async def handle_format_selection(event):
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    format_type = event.pattern_match.group(1).decode()
    url_id = event.pattern_match.group(2).decode()

    # pop atómico para evitar dobles ejecuciones por doble click (doble descarga)
    url_data = claim_pending_url(url_id)
    if not url_data:
        await answer_unclaimable_url(event, url_id)
        return
    stats.count(f"url_{format_type}")

    url = url_data["url"]
    playlist_count = url_data.get("playlist_count", 1)
    await start_ytdlp_download(event, event, url, format_type == "audio", playlist_count)


async def start_ytdlp_download(event, target, url, is_audio, playlist_count=1, playlist_mode=None):
    """Lanza yt-dlp para un enlace con las opciones de /settings.

    `target` es el mensaje que pasa a ser el de progreso (el de análisis o el
    que tenía los botones). Si no lo hay, o la edición no sale a tiempo, se
    descarga sin mensaje de progreso (ver el comentario de abajo).
    `playlist_mode` es None para un vídeo suelto, "first" para quedarse con el
    primero de una playlist o "full" para bajarla entera, hasta el tope
    configurado.
    """
    final_output_dir = DOWNLOAD_PATHS["url_audio"] if is_audio else DOWNLOAD_PATHS["url_video"]
    text = get_text("downloading", AUD_ICO if is_audio else VID_ICO)
    buttons = [Button.inline(get_text("button_cancel"), data=f"cancel:{task_key(event)}")]

    # El tema del foro al que se envía lo descargado (ver send_file_to_telegram)
    # sale del mensaje del botón, que pasa a ser el de progreso y se borra
    # antes de enviar: se pide ahora, que Telethon lo guarda. Antes de mostrar
    # el Cancelar, porque es una petición a Telegram (hasta un minuto con un
    # FloodWait) y un Cancelar pulsado mientras tanto no encontraba la tarea:
    # borraba el mensaje y yt-dlp arrancaba igual
    await get_topic(event)

    # Sin mensaje que editar, o si la edición no sale en su tiempo (timeout en
    # cola), se descarga sin mensaje de progreso: uno nuevo mandado sin esperar
    # saldría tarde, sin referencia, y se quedaría para siempre en «Descargando…»
    status_message = None
    if target is not None:
        status_message = await safe_edit(target, text, buttons=buttons, parse_mode=PARSE_MODE, wait_for_result=True)

    # Usar timestamp para evitar sobrescribir archivos durante la descarga
    timestamp = int(time.time() * 1000)
    cmd = [
        "yt-dlp",
        *ytdlp_settings_args(is_audio),
        "--restrict-filenames",
        "--newline",  # Cada línea de progreso completa (para parsear en tiempo real)
        "--progress",  # Forzar mostrar progreso
        # Incluye el índice de playlist para evitar sobrescrituras
        "-o", os.path.join(TEMP_DIR, ytdlp_output_template(timestamp)),  # Descargar a /tmp
        url
    ]
    cmd = add_ytdlp_cookies(cmd)

    full_playlist = playlist_mode == "full"
    total_videos = playlist_count
    if playlist_mode == "first":
        # --no-playlist escoge el vídeo en los enlaces mixtos (watch?v=X&list=Y),
        # pero en una lista pura (playlist?list=, un canal) no hace nada y se
        # bajaba entera: --playlist-items 1 la corta en el primero
        cmd[1:1] = ["--no-playlist", "--playlist-items", "1"]
        total_videos = 1
    elif full_playlist:
        # En playlists completas, omitir vídeos no disponibles para no abortar el resto
        cmd.insert(1, "--ignore-errors")
        # Avisar de cada vídeo terminado, que run_url_download lleva ya a su
        # carpeta. --print implica --quiet, que callaría el progreso
        cmd[1:1] = ["--print", f"after_move:{YTDLP_DONE_PREFIX}%(filepath)s", "--no-quiet"]
        limit = settings.playlist_limit()
        if limit:
            cmd[1:1] = ["--playlist-end", str(limit)]
            total_videos = min(playlist_count, limit) if playlist_count else limit
    debug(f"[URL_DOWNLOAD] playlist mode={playlist_mode}, videos={total_videos}")

    # Calcular delay automático basado en el número de vídeos
    sleep_interval = calculate_ytdlp_sleep_interval(total_videos)
    if sleep_interval > 0:
        cmd.extend(["--sleep-interval", str(sleep_interval)])
        debug(f"[URL_DOWNLOAD] Auto-calculated sleep interval: {sleep_interval}s for {total_videos} video(s)")

    task = asyncio.create_task(run_url_download(event, cmd, status_message, final_output_dir, is_full_playlist=full_playlist, total_videos=total_videos, temp_marker=ytdlp_temp_marker(timestamp)))
    active_tasks[task_key(event)] = task

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

# Lo que yt-dlp repite sin parar mientras descarga y, como la barra de wget,
# no va al log: su progreso ("[download]  45.2% of 10.00MiB at 1.23MiB/s ETA
# 00:30" o, sin tamaño, "[download]  12.34MiB at 1.23MiB/s"), que con
# --newline sale varias veces por segundo, y las estadísticas del ffmpeg de un
# directo ("frame=  250 fps= 25 ... speed=1.0x"). Con ellos, el log de Docker
# crecía cientos de MB al mes. La línea con la que acaba cada fichero
# ("[download] 100% of 10.00MiB in 00:00:08") sí va: es una por fichero
YTDLP_PROGRESS_RE = re.compile(r'\[download\]\s+(\d+\.\d%|[\d.]+\w*B\s+at\s)|(frame|size)=.*\bspeed=')

# Lo más que espera una edición de progreso. Mientras, nadie lee la salida de
# la descarga: con Telegram sin responder, Telethon retiene la petición hasta
# reconectar (minutos), la tubería se llenaba y yt-dlp se quedaba parado. Tiene
# que ser menor que progress_update_interval(), o tocaría editar otra vez nada
# más acabar y apenas se leería
PROGRESS_EDIT_TIMEOUT = 5


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

        # Crear barra de progreso visual del vídeo actual (acotada: el
        # porcentaje viene de la salida de wget o yt-dlp)
        bar_length = 20
        filled = int(bar_length * min(max(float(percent), 0), 100) / 100)
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
            await asyncio.wait_for(status_message.edit(
                message,
                buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{task_key(event)}")],
                parse_mode=PARSE_MODE
            ), PROGRESS_EDIT_TIMEOUT)
        except TimeoutError:
            # También un FloodWait de menos de un minuto: Telethon no lo lanza,
            # duerme dentro del edit, y el plazo lo corta
            warning(f"[URL_DOWNLOAD] Telegram did not answer the progress edit in {PROGRESS_EDIT_TIMEOUT}s, skipped")
        except Exception as edit_error:
            # Ignorar errores de edición (FloodWait de más de un minuto, mensaje
            # no modificado, etc.)
            warning(f"[URL_DOWNLOAD] Error editing progress message: {edit_error}")
    except Exception as e:
        # Ignorar errores al montar el texto (un progreso con un valor raro):
        # los de Telegram ya los recoge el except de la edición
        warning(f"[URL_DOWNLOAD] Error updating progress: {e}")

# Lo que se lee de la salida de wget. La barra, con tamaño conocido
# ("45%[===>    ]  1.20M") o sin él ("[  <=>   ]  1.20M"), se redibuja varias
# veces por segundo
WGET_BAR_RE = re.compile(r'\[[ =<>+-]*\]\s+[\d.,]+[KMGT]?(\s|$)')
# El porcentaje real va pegado a la barra. Delante desfila el nombre del
# fichero, que puede llevar su propio "100%": vale la última coincidencia
WGET_PERCENT_RE = re.compile(r'(\d{1,3})%\s*\[')
WGET_LENGTH_RE = re.compile(r'^Length:\s+(\d+)')
# wget escapa los bytes que no son ASCII pero deja los espacios: un servidor
# que no codifica el Location manda "/files/Mi peli.mkv"
WGET_LOCATION_RE = re.compile(r'^Location:\s+(.+?)\s+\[following\]$')
WGET_RETRY_RE = re.compile(r'\(try:\s*(\d+)\)')
# Cada corte de conexión gasta un intento aunque luego se reanude: con muy
# pocos fallarían descargas grandes por enlaces inestables
WGET_TRIES = 5


async def run_direct_download(event, url, filename, status_message, final_output_dir, icon, content_type):
    """Descarga un archivo directo usando wget"""
    # Un nombre propio y corto para el temporal: con el del fichero más un
    # sufijo, uno largo pasaba de los 255 bytes del sistema y wget no podía
    # abrirlo. Lleva el chat, como la clave en active_tasks. Fuera del try:
    # los except limpian este fichero
    key = task_key(event)
    timestamp = int(time.time() * 1000)
    temp_filename = f"wget_{key}_{timestamp}"
    temp_file_path = os.path.join(TEMP_DIR, temp_filename)
    proc = None
    cookie_jar = None

    try:
        debug(f"[DIRECT_DOWNLOAD] Starting direct download: {filename}")
        debug(f"[DIRECT_DOWNLOAD] Final output directory: {final_output_dir}")
        debug(f"[DIRECT_DOWNLOAD] Content type: {content_type}, Icon: {icon}")

        # Variables para control de progreso
        last_update_time = 0
        update_interval = progress_update_interval()
        # Tamaño máximo de /settings (0 = sin límite). wget no tiene una opción
        # para un solo fichero: se mira el Length que anuncia el servidor y, por
        # si no lo manda o miente, lo que ya ocupa el temporal
        max_size = settings.max_size_mb()
        too_large = False
        # Destino de una redirección, que wget no sigue (--max-redirect=0)
        redirect = None

        async def show_progress(message):
            """Edita el mensaje de progreso con un texto sin barra.

            Con el mismo tope que update_progress_message: se espera dentro
            del bucle que lee la salida de wget, y con Telegram sin responder
            la tubería se llenaba y wget se paraba.
            """
            try:
                await asyncio.wait_for(status_message.edit(
                    message,
                    buttons=[Button.inline(get_text("button_cancel"), data=f"cancel:{key}")],
                    parse_mode=PARSE_MODE
                ), PROGRESS_EDIT_TIMEOUT)
            except TimeoutError:
                warning(f"[DIRECT_DOWNLOAD] Telegram did not answer the progress edit in {PROGRESS_EDIT_TIMEOUT}s, skipped")
            except Exception as edit_error:
                warning(f"[DIRECT_DOWNLOAD] Error editing progress message: {edit_error}")

        # Leer stderr (wget muestra progreso en stderr)
        async def read_stderr():
            nonlocal last_update_time, too_large, redirect
            async for line in iter_progress_lines(proc.stderr):
                line_str = line.strip()

                if max_size and not too_large:
                    length = WGET_LENGTH_RE.match(line_str)
                    try:
                        current_size = int(length.group(1)) if length else os.path.getsize(temp_file_path)
                    except OSError:
                        current_size = 0
                    if current_size > max_size * 1024 * 1024:
                        too_large = True
                        _stop_process(proc)

                location = WGET_LOCATION_RE.match(line_str)
                if location:
                    redirect = location.group(1)
                    # wget la imprime tal como llega: si es relativa,
                    # redact_urls no la reconoce y la firma iba entera al log
                    debug(f"[WGET] Location: {redact_url(redirect)} [following]")
                    continue

                # Los reintentos no se ven en la barra: sin esto parecía parada
                retry = WGET_RETRY_RE.search(line_str)
                if retry and status_message:
                    await show_progress(get_text("downloading_retrying", retry.group(1), WGET_TRIES, filename))
                    last_update_time = time.time()

                # La barra se redibuja varias veces por segundo: ni se guarda
                # ni va al log
                if not WGET_BAR_RE.search(line_str):
                    if line_str:
                        debug(f"[WGET] {redact_urls(line_str)}")
                    continue

                # Parsear progreso de wget
                # Formato: 45%[=====>     ] 123.45M  1.23MB/s    eta 30s
                # Sin Content-Length no hay porcentaje: [  <=>   ] 123.45M  1.23MB/s
                if status_message:
                    current_time = time.time()

                    # Detectar si llegamos al 100%
                    percents = WGET_PERCENT_RE.findall(line_str)
                    is_complete = bool(percents) and int(percents[-1]) >= 100
                    should_update = (current_time - last_update_time >= update_interval) or is_complete

                    if should_update:
                        try:
                            # Extraer tamaño descargado
                            size_match = re.search(r'\]\s+([\d\.]+[KMG]?)', line_str)
                            # Extraer velocidad
                            speed_match = re.search(r'([\d\.]+[KMG]?B/s)', line_str)
                            # Extraer ETA
                            eta_match = re.search(r'eta\s+([\dhms]+)', line_str)
                            size = size_match.group(1) if size_match else "N/A"
                            speed = speed_match.group(1) if speed_match else "N/A"

                            if percents:
                                progress_info = {
                                    "percent": percents[-1],
                                    "size": size,
                                    "speed": speed,
                                    "eta": eta_match.group(1) if eta_match else "N/A",
                                    "filename": filename
                                }

                                await update_progress_message(status_message, progress_info, event, filename)
                            else:
                                await show_progress(get_text("downloading_progress_unknown", size, speed, filename))
                            last_update_time = current_time
                        except Exception as e:
                            debug(f"[DIRECT_DOWNLOAD] Error parsing wget progress: {e}")

        # Leer stdout (normalmente vacío para wget)
        async def read_stdout():
            async for line in proc.stdout:
                line_str = line.decode().strip()
                if line_str:
                    debug(f"[WGET] stdout: {redact_urls(line_str)}")

        # Las cookies que pone una redirección, para el salto siguiente: con un
        # wget por salto se perdían, y los gestores de descargas que marcan la
        # sesión antes de redirigir al fichero contestaban 403. Empieza vacío y
        # solo para el dueño, y con el nombre del temporal: remove_temp_files
        # también lo borra
        fd, cookie_jar = tempfile.mkstemp(prefix=temp_filename, suffix=".cookies", dir=TEMP_DIR)
        os.close(fd)

        # wget no sigue las redirecciones: cada salto se comprueba (ver
        # checked_redirect) y se vuelve a lanzar contra su destino
        current_url = url
        chain_private = None
        for _ in range(MAX_REDIRECTS + 1):
            # Comando wget con progreso
            cmd = [
                "wget",
                "--progress=bar:force",  # Mostrar barra de progreso
                "--show-progress",  # Mostrar progreso detallado
                # Que un servidor que deja de enviar no la congele: por defecto
                # wget espera 15 min a cada lectura y hace 20 intentos
                "--connect-timeout=30",
                "--read-timeout=60",
                f"--tries={WGET_TRIES}",
                "--waitretry=5",
                "--max-redirect=0",
                "--load-cookies", cookie_jar, "--save-cookies", cookie_jar, "--keep-session-cookies",
                "-O", temp_file_path,  # Output file
                current_url
            ]

            debug(f"[DIRECT_DOWNLOAD] Command: {redact_urls(' '.join(cmd))}")

            redirect = None
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            active_tasks[key] = proc

            # Ejecutar lectura en paralelo
            stderr_task = asyncio.create_task(read_stderr())
            stdout_task = asyncio.create_task(read_stdout())

            await stderr_task
            await stdout_task
            await proc.wait()

            debug(f"[DIRECT_DOWNLOAD] Process finished with code {proc.returncode}")

            if redirect is None or too_large or proc.returncode in (-15, 143):
                break
            # Mientras se comprueba no hay wget que parar: Cancelar va a la tarea
            active_tasks[key] = asyncio.current_task()
            # Con los espacios codificados, como hacía wget al seguirla por su
            # cuenta: los %XX que ya trae se quedan como están
            target = urljoin(current_url, requests.utils.requote_uri(redirect))
            if chain_private is None:
                chain_private = await asyncio.to_thread(resolves_to_private, current_url)
            current_url, chain_private = await asyncio.to_thread(checked_redirect, chain_private, target)

        # Manejar cancelación
        if proc.returncode == -15 or proc.returncode == 143:  # SIGTERM
            await handle_cancel(status_message, temp_filename)
            return

        if too_large:
            if status_message:
                await safe_delete(status_message)
            warning(f"[DIRECT_DOWNLOAD] Over the maximum size of {max_size} MB, stopped: {filename}")
            await safe_reply(event, get_text("error_url_too_large", settings.size_label(max_size)), parse_mode=PARSE_MODE)
            remove_temp_files(temp_filename)
            return

        # Verificar si la descarga fue exitosa
        if proc.returncode == 0 and os.path.exists(temp_file_path):
            # Eliminar mensaje de progreso
            if status_message:
                await safe_delete(status_message)

            # Mover archivo a directorio final con un nombre libre, fuera del
            # event loop. El nombre no se ocupa hasta tener el contenido entero
            final_file_path = await asyncio.to_thread(move_to_unique_path, temp_file_path, final_output_dir, filename)
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

    except PrivateRedirectError as e:
        if status_message:
            await safe_delete(status_message)
        warning(f"[DIRECT_DOWNLOAD] Blocked redirect to the local network: {redact_urls(e)}")
        await safe_reply(event, get_text("error_url_private_redirect"), parse_mode=PARSE_MODE)
        remove_temp_files(temp_filename)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            _stop_process(proc)
            await proc.wait()
        await handle_cancel(status_message, temp_filename)
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
        # Que wget no siga escribiendo un temporal que se va a borrar
        _stop_process(proc)
        remove_temp_files(temp_filename)
    finally:
        debug(f"[DIRECT_DOWNLOAD] Cleaning up. ID {event.id}")
        # Solo si sigue siendo la suya, como en run_url_download
        if active_tasks.get(key) in (proc, asyncio.current_task()):
            active_tasks.pop(key, None)
        if cookie_jar is not None:
            try:
                os.remove(cookie_jar)
            except OSError:
                pass

async def run_url_download(event, cmd, status_message, final_output_dir, is_full_playlist=False, total_videos=None, temp_marker=None):
    """Ejecuta yt-dlp y mueve lo descargado a `final_output_dir`.

    `temp_marker` (ver ytdlp_temp_marker) identifica en TEMP_DIR los ficheros de
    esta descarga, para limpiarlos o rescatarlos al cancelar. En una playlist
    entera, cada vídeo se mueve en cuanto yt-dlp avisa de que lo ha terminado
    (ver YTDLP_DONE_PREFIX): esperando al final, una playlist de horas se
    quedaba entera en TEMP_DIR, en el disco del contenedor, y un reinicio a
    mitad la perdía, porque al arrancar se vacía.
    """
    proc = None
    stderr_task = None
    # El último traslado de un vídeo terminado (ver move_finished)
    moving = None
    try:
        debug("[URL_DOWNLOAD] Creating URL download subprocess...")
        debug(f"[URL_DOWNLOAD] Final output directory: {final_output_dir}")
        debug(f"[URL_DOWNLOAD] Is full playlist: {is_full_playlist}")
        debug(f"[URL_DOWNLOAD] Total videos in playlist: {total_videos}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # Ver stop_ytdlp
        )
        active_tasks[task_key(event)] = proc

        # Variables para control de progreso
        stdout_lines = []
        last_update_time = 0
        update_interval = progress_update_interval()  # Intervalo dinámico según las descargas simultáneas
        current_filename = None  # Almacenar el nombre del archivo actual (solo nombre, no path completo)
        current_filepath = None  # Almacenar el path completo del archivo actual
        current_video_index = None  # Índice del vídeo actual en la playlist
        # Vídeos de la playlist ya llevados a su carpeta: ruta en TEMP_DIR -> ruta final
        moved = {}

        async def move_finished(path, previous):
            """Lleva a su carpeta un vídeo que yt-dlp ya ha terminado.

            Va en su propia tarea, detrás del traslado anterior: esperándolo en
            la lectura de stdout, una copia larga entre dispositivos dejaba la
            tubería llena y a yt-dlp parado.
            """
            if previous is not None:
                try:
                    await previous
                except Exception:
                    # Ya lo ha contado su tarea: que uno falle no deja sin
                    # mover a los que vienen detrás
                    pass
            try:
                # La ruta sale de la salida de yt-dlp, como las del final (ver
                # is_ytdlp_output): una línea imitada no mueve nada de fuera
                if not is_ytdlp_output(path, temp_marker):
                    warning(f"[URL_DOWNLOAD] Ignoring output that is not a finished file of this download: {path}")
                    return
                moved[path] = await move_download_to(path, final_output_dir)
            except Exception as e:
                # Si sigue en TEMP_DIR, se vuelve a intentar al acabar
                error(f"[URL_DOWNLOAD] Error moving finished file {path}: {e}")

        # Leer stdout línea por línea en tiempo real. Una línea de más de 64 KiB
        # (un título enorme) se recorta: con readline() cortaba la lectura y
        # yt-dlp seguía descargando sin que nadie lo leyera
        async def read_stdout():
            nonlocal last_update_time, current_filename, current_filepath, current_video_index, moving
            async for line in iter_progress_lines(proc.stdout):
                line_str = line.strip()
                stdout_lines.append(line_str)
                # El progreso hace falta leerlo, pero en el log serían varias
                # líneas por segundo de descarga (ver YTDLP_PROGRESS_RE)
                if not YTDLP_PROGRESS_RE.match(line_str):
                    debug(f"[YT-DLP] {redact_urls(line_str)}")

                # Un vídeo de la playlist que yt-dlp ya ha terminado del todo
                if is_full_playlist and line_str.startswith(YTDLP_DONE_PREFIX):
                    moving = asyncio.create_task(move_finished(line_str.removeprefix(YTDLP_DONE_PREFIX), moving))

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
                    if is_full_playlist and current_filepath:
                        debug(f"[URL_DOWNLOAD] File download completed (may need merging): {current_filepath}")

                # Detectar líneas de progreso: [download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:30
                if "[download]" in line_str and "%" in line_str:
                    current_time = asyncio.get_running_loop().time()
                    time_diff = current_time - last_update_time

                    # Detectar si llegamos al 100%
                    is_complete = "[download] 100%" in line_str
                    should_update = (time_diff >= update_interval) or is_complete

                    if should_update:
                        debug(f"[URL_DOWNLOAD] Progress detected. Time since last update: {time_diff:.2f}s (interval: {update_interval}s)")
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
                            debug(f"[URL_DOWNLOAD] Could not parse progress from: {redact_urls(line_str)}")

        # Leer stderr en paralelo (las estadísticas del ffmpeg de un directo
        # van con \r, sin saltos de línea, y ni se guardan ni van al log)
        async def read_stderr():
            stderr_lines = []
            async for line in iter_progress_lines(proc.stderr):
                line_str = line.strip()
                if YTDLP_PROGRESS_RE.match(line_str):
                    continue
                stderr_lines.append(line_str)
                if line_str:
                    debug(f"[YT-DLP] stderr: {redact_urls(line_str)}")
            return stderr_lines

        # Ejecutar lectura de stdout y stderr en paralelo
        stderr_task = asyncio.create_task(read_stderr())
        await read_stdout()
        stderr_lines = await stderr_task

        # Esperar a que termine el proceso
        await proc.wait()
        debug(f"[URL_DOWNLOAD] Exiting URL download subprocess. Code {proc.returncode}")
        # Y a los traslados pendientes, antes de contar lo que se ha guardado
        if moving is not None:
            await moving

        if proc.returncode == -15:
            # En una playlist completa, los vídeos ya terminados se conservan:
            # los que ya se movieron y los que quedaran en TEMP_DIR
            if is_full_playlist and temp_marker:
                rescued = len(moved) + await rescue_completed_playlist_files(temp_marker, final_output_dir)
                if rescued:
                    remove_temp_files(temp_marker)
                    if status_message:
                        await safe_edit(
                            status_message,
                            get_text("cancelled_playlist_partial", rescued, total_videos or rescued),
                            buttons=None,
                            parse_mode=PARSE_MODE
                        )
                    return
            await handle_cancel(status_message, temp_marker)
            return

        if status_message:
            await safe_delete(status_message)
        # yt-dlp devuelve código != 0 si algún vídeo de una playlist no está
        # disponible, aunque el resto se hayan descargado correctamente.
        # Recuperamos los archivos realmente descargados y solo reportamos
        # error si no hay ninguno (descarga parcial = éxito parcial).
        # Solo cuenta lo que esta descarga ha dejado de verdad en TEMP_DIR: un
        # merge que falla anuncia un fichero que no llega a existir, y sin
        # nada que mover hay que decir que ha fallado
        temp_file_paths = []
        # Los audios que no se pudieron convertir: se entrega el original, avisando
        unconverted = set()
        for path in extract_file_paths(stdout_lines, unconverted):
            if path in moved or is_ytdlp_output(path, temp_marker):
                temp_file_paths.append(path)
            else:
                warning(f"[URL_DOWNLOAD] Ignoring output that is not a finished file of this download: {path}")
        # Lo ya movido cuenta aunque el resto de la salida no lo nombre igual
        temp_file_paths += [path for path in moved if path not in temp_file_paths]

        async def final_path_of(temp_file_path):
            """Su ruta en la carpeta final, moviéndolo si no se movió ya al terminarlo."""
            if temp_file_path in moved:
                return moved[temp_file_path]
            if os.path.exists(temp_file_path):
                return await move_download_to(temp_file_path, final_output_dir)
            warning(f"[URL_DOWNLOAD] Output file not found: {temp_file_path}")
            return None

        if proc.returncode == 0 or temp_file_paths:
            if proc.returncode != 0:
                warning(f"[URL_DOWNLOAD] yt-dlp exited with code {proc.returncode} but {len(temp_file_paths)} file(s) downloaded. Processing partial result.")

            if temp_file_paths:
                # Si es playlist completa, solo almacenar sin mostrar botones.
                # También si de una playlist de varios solo ha quedado uno: hay
                # que avisar de los que faltan (X/Y), no darlo por un vídeo suelto
                if is_full_playlist and (len(temp_file_paths) > 1 or (total_videos or 0) > 1):
                    debug(f"[URL_DOWNLOAD] Full playlist detected with {len(temp_file_paths)} files. Storing without action buttons.")
                    stored_files = []
                    stored_paths = []
                    stored_unconverted = 0
                    for temp_file_path in temp_file_paths:
                        final_file_path = await final_path_of(temp_file_path)
                        if final_file_path:
                            stored_files.append(os.path.basename(final_file_path))
                            stored_paths.append(final_file_path)
                            stored_unconverted += temp_file_path in unconverted

                    # Mostrar mensaje de éxito sin botones
                    if stored_files:
                        file_ext = os.path.splitext(stored_files[0])[1].lower()
                        icon = get_file_icon(file_ext)
                        # Si se descargaron menos vídeos de los esperados (algún
                        # ítem no disponible), mostrar el conteo parcial X/Y
                        if total_videos and len(stored_files) < total_videos:
                            message = get_text("playlist_stored_partial", icon, len(stored_files), total_videos)
                        else:
                            message = get_text("playlist_stored", icon, len(stored_files))
                        if stored_unconverted:
                            message += "\n\n" + get_text("playlist_audio_conversion_failed", stored_unconverted, settings.audio_format())
                        await safe_reply_in_topic(event, message, parse_mode=PARSE_MODE)

                        # Esta rama no pasa por handle_success, así que el envío
                        # automático hay que aplicarlo aquí o las playlists se
                        # quedarían siempre almacenadas sin enviar
                        auto_send = settings.auto_send()
                        if auto_send in ("SEND", "SEND_DELETE"):
                            debug(f"[URL_DOWNLOAD] auto send={auto_send}, sending {len(stored_paths)} playlist file(s)")
                            for stored_path in stored_paths:
                                stored_ext = os.path.splitext(stored_path)[1].lower()
                                if stored_ext not in EXTENSIONS_VIDEO and stored_ext not in EXTENSIONS_AUDIO:
                                    debug(f"[URL_DOWNLOAD] Skipping non media file: {stored_path}")
                                    continue
                                await send_file_automatically(event, stored_path)

                else:
                    # Video individual o primer video de playlist - mostrar botones normales
                    for temp_file_path in temp_file_paths:
                        final_file_path = await final_path_of(temp_file_path)
                        if final_file_path:
                            await handle_success(event, final_file_path)
                            if temp_file_path in unconverted:
                                await safe_reply_in_topic(event, get_text("audio_conversion_failed", settings.audio_format(),
                                                                          md_code(os.path.basename(final_file_path))),
                                                          parse_mode=PARSE_MODE)

            else:
                warning("[URL_DOWNLOAD] No downloaded files found in yt-dlp output")
                debug(f"[URL_DOWNLOAD] Command executed: {redact_urls(' '.join(cmd))}")
                output = "\n".join(line for line in stdout_lines if not YTDLP_PROGRESS_RE.match(line))
                debug(f"[URL_DOWNLOAD] Full stdout, without progress: {redact_urls(output)}")
                await safe_reply_in_topic(event, url_failure_message(stdout_lines), parse_mode=PARSE_MODE)
        else:
            stderr_output = "\n".join(stderr_lines)
            error(f"[URL_DOWNLOAD] URL download failed. Error: {redact_urls(stderr_output)}")
            await safe_reply_in_topic(event, url_failure_message(stdout_lines + stderr_lines), parse_mode=PARSE_MODE)

    except asyncio.CancelledError:
        await stop_ytdlp(proc, stderr_task)
        await handle_cancel(status_message, temp_marker)
        raise
    except Exception as e:
        error(f"[URL_DOWNLOAD] ❌ Error during URL download: {e}")
        error(f"[URL_DOWNLOAD] Exception type: {type(e).__name__}")
        import traceback
        error(f"[URL_DOWNLOAD] Traceback: {traceback.format_exc()}")
        # Que yt-dlp no siga vivo: nadie leería ya su salida y se quedaría
        # bloqueado escribiendo, con la descarga a medias en un TEMP_DIR que
        # se limpia en el finally
        await stop_ytdlp(proc, stderr_task)
        # Intentar notificar al usuario
        try:
            if status_message:
                await safe_delete(status_message)
            await safe_reply_in_topic(event, get_text("error_url_failed_user"), parse_mode=PARSE_MODE)
        except:
            pass  # Si falla la notificación, al menos ya logueamos el error original
    finally:
        debug(f"[URL_DOWNLOAD] Cleaning URL download subprocess. ID {event.id}")
        # Solo si sigue siendo la suya, como en limited_download: otra tarea
        # lanzada por el mismo mensaje la habría pisado y no se podría cancelar
        if active_tasks.get(task_key(event)) in (proc, asyncio.current_task()):
            active_tasks.pop(task_key(event), None)
        # Si se ha cancelado o ha fallado, no se sigue moviendo nada
        if moving is not None:
            moving.cancel()
        # Restos que yt-dlp no llegó a mezclar o borrar (formatos sueltos, .part)
        if temp_marker:
            remove_temp_files(temp_marker)
        # Lo que yt-dlp haya renovado de las cookies solo vale si ha ido bien
        release_ytdlp_cookies(cmd, save_renewed=proc is not None and proc.returncode == 0)

def url_failure_message(output_lines):
    """El mensaje para una descarga de enlace que no dejó ningún fichero.

    Si fue el tamaño máximo de /settings, se dice: el error genérico haría
    pensar que el enlace no funciona, cuando es el bot el que no ha querido.
    """
    if any("larger than max-filesize" in line for line in output_lines):
        return get_text("error_url_too_large", settings.size_label(settings.max_size_mb()))
    return get_text("error_url_failed_user")


def extract_file_paths(stdout_lines, unconverted=None):
    """Extrae todas las rutas de archivos descargados (soporta playlists/carruseles)

    Solo cuentan las líneas que empiezan como las de yt-dlp: por en medio va
    también texto de un tercero, como el título de una playlist ("[download]
    Downloading playlist: <título>"). Un título con un salto de línea podría
    imitar una línea entera, así que antes de mover nada hay que comprobar
    cada ruta con is_ytdlp_output.

    En `unconverted` (un set), si se da, deja los originales de audio que
    yt-dlp empezó a convertir y no terminó: se entregan tal cual, y hay que
    decir que no son el formato pedido.
    """
    file_paths = []
    current_file = None
    is_partial = False
    # Audios extraídos que aún no cuentan, por el original del que salen
    extracting = {}

    for line in stdout_lines:
        # Detectar archivo de destino inicial
        if line.startswith("[download] Destination: "):
            possible_path = line.removeprefix("[download] Destination: ").strip()
            if possible_path:
                current_file = possible_path
                # Detectar si es un archivo parcial (será mergeado después)
                is_partial = is_ytdlp_partial(possible_path)

        # Detectar merge de formatos (este es el archivo final)
        elif line.startswith("[Merger] Merging formats into "):
            merged_path = line.removeprefix("[Merger] Merging formats into ").strip().strip('"')
            debug(f"[URL_DOWNLOAD] Detected merged output file: {merged_path}")
            current_file = merged_path
            is_partial = False  # El archivo mergeado es el final
            # Agregar inmediatamente el archivo mergeado
            if current_file and current_file not in file_paths:
                file_paths.append(current_file)
                debug(f"[URL_DOWNLOAD] File added to download list: {current_file}")

        # Detectar extracción de audio (este es el archivo final)
        elif line.startswith("[ExtractAudio] Destination: "):
            audio_path = line.removeprefix("[ExtractAudio] Destination: ").strip()
            debug(f"[URL_DOWNLOAD] Detected audio output file: {audio_path}")
            current_file = None
            # Sale del original con el mismo nombre y otra extensión, y no cuenta
            # hasta que yt-dlp borra ese original, que es justo cuando ffmpeg
            # termina bien. Si falla a medias, el audio queda cortado y lo que se
            # entrega es el original, que está entero
            stem = os.path.splitext(audio_path)[0]
            original = next((path for path in file_paths
                             if path != audio_path and os.path.splitext(path)[0] == stem), None)
            if original:
                extracting[original] = audio_path
            elif audio_path not in file_paths:
                file_paths.append(audio_path)
                debug(f"[URL_DOWNLOAD] File added to download list: {audio_path}")

        # yt-dlp borra el original de un audio extraído cuando ya está convertido
        elif line.startswith("Deleting original file "):
            original = line.removeprefix("Deleting original file ").removesuffix(" (pass -k to keep)")
            if audio_path := extracting.pop(original, None):
                file_paths[file_paths.index(original)] = audio_path
                debug(f"[URL_DOWNLOAD] File added to download list: {audio_path}")

        # Un fichero que ya estaba en disco: yt-dlp no imprime Destination,
        # la ruta viene en la propia línea
        elif already := re.match(r'\[download\] (.+) has already been downloaded', line):
            current_file = already.group(1).strip()
            is_partial = is_ytdlp_partial(current_file)
            if not is_partial and current_file not in file_paths:
                file_paths.append(current_file)
                debug(f"[URL_DOWNLOAD] File added to download list: {current_file}")
            current_file = None

        # Detectar finalización de descarga (solo agregar si NO es parcial)
        elif line.startswith("[download] 100%"):
            if current_file and not is_partial and current_file not in file_paths:
                file_paths.append(current_file)
                debug(f"[URL_DOWNLOAD] File added to download list: {current_file}")
                current_file = None

    if unconverted is not None:
        unconverted.update(extracting)
    # La marca temporal se quita al mover a la carpeta final (move_download_to),
    # que es donde hay que buscar un nombre libre
    return file_paths

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
        elif file_ext in EXTENSIONS_TORRENT:
            info["type"] = "torrent"
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

# Ids de conversión: únicos por conversión, no por fichero. Con un hash de la
# ruta, convertir dos veces el mismo fichero reutilizaba el id y una marca de
# cancelación antigua cortaba la conversión nueva nada más empezar.
# El contador vuelve a 1 en cada arranque y los botones de una conversión que
# se cortó al reiniciar siguen en el chat: con el prefijo aleatorio de cada
# arranque, uno de esos botones no corta la conversión nueva con su número
_conversion_run = secrets.token_hex(3)
_conversion_ids = itertools.count(1)


def _stop_process(proc):
    """Mata un subproceso si sigue vivo (sin fallar si acaba de terminar)."""
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def convert_video_to_telegram_compatible(input_path, status_message=None, _force_software=False):
    """
    Convierte un video a formato compatible con Telegram (MP4 con H.264 + AAC).
    Retorna la ruta del archivo convertido (temporal en /tmp) o el original si falla.
    IMPORTANTE: El archivo convertido debe ser eliminado después de enviarlo.
    """
    drain_task = None
    proc = None
    conversion_id = f"conv_{_conversion_run}_{next(_conversion_ids)}"
    output_path = None
    try:
        debug(f"[CONVERSION] Starting video conversion: {input_path}")

        # Si Telegram ya puede reproducirlo, no se toca. Si solo falla el
        # contenedor o el audio, se copia el vídeo sin recodificar.
        plan = None
        if not _force_software:
            plan = telegram_conversion_plan(input_path, await probe_media(input_path))
            debug(f"[CONVERSION] Plan: {plan}")
            if plan_is_noop(plan):
                debug("[CONVERSION] ✅ Video already Telegram-compatible, skipping conversion")
                return input_path

        # Generar nombre de archivo temporal en /tmp para la conversión. Sin el
        # nombre del vídeo: el sufijo no cabría en los 255 bytes de un nombre
        # que ya los ocupa casi todos, y el nombre con el que se envía va aparte
        timestamp = int(time.time() * 1000)
        output_path = os.path.join(TEMP_DIR, f"{conversion_id}_{timestamp}_telegram.mp4")

        debug(f"[CONVERSION] Output file: {output_path}")

        # Obtener duración del video para calcular progreso
        duration_seconds = 0
        try:
            debug("[CONVERSION] Getting video metadata...")
            duration, _, _ = await get_video_metadata(input_path)
            duration_seconds = duration if duration else 0
            debug(f"[CONVERSION] Video duration: {duration_seconds}s")
        except Exception as e:
            debug(f"[CONVERSION] Could not get video duration: {e}")

        debug(f"[CONVERSION] Conversion ID: {conversion_id}")

        # Determinar si es un video largo (>300s = 5 minutos) para mostrar botón de enviar original
        # Copiar pistas tarda segundos aunque el vídeo sea largo: sin aviso
        is_long_video = duration_seconds > 300 and not (plan and plan["video"] == "copy")
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

            debug("[CONVERSION] Updating status message with cancel button" + (" and send original button" if is_long_video else ""))
            await safe_edit(
                status_message,
                msg,
                buttons=buttons,
                parse_mode=PARSE_MODE
            )

        selected_hw = "NONE" if _force_software else settings.ffmpeg_hw()
        quality = settings.ffmpeg_quality()
        cmd = build_ffmpeg_conversion_command(
            input_path, output_path, selected_hw, quality, plan
        )
        stream_copy = plan is not None and plan["video"] == "copy"
        stats.count("convert_remux" if stream_copy else f"convert_{selected_hw.lower()}")

        encoder = cmd[cmd.index("-c:v") + 1]
        debug(f"[CONVERSION] Encoder mode: {selected_hw}; video encoder: {encoder}")
        debug(f"[CONVERSION] Quality: {quality or 'encoder default'}")
        debug(f"[CONVERSION] Full FFmpeg command: {' '.join(cmd)}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        debug(f"[CONVERSION] ffmpeg process started (PID: {proc.pid})")

        # Guardar el proceso para poder cancelarlo
        active_tasks[conversion_id] = proc
        debug(f"[CONVERSION] Process saved in active_tasks with ID: {conversion_id}")

        # Drenar stderr en paralelo: si ffmpeg vuelca mucho diagnóstico (p. ej.
        # un decode que falla) y nadie lo lee, la tubería se llena y ffmpeg se
        # queda bloqueado para siempre sin fallar, con lo que el reintento en
        # software nunca saltaría.
        stderr_chunks: list[bytes] = []
        drain_task = asyncio.ensure_future(accumulate_process_stderr(proc, stderr_chunks))

        # Leer progreso en tiempo real
        last_update = 0
        debug("[CONVERSION] Starting progress reading...")
        while True:
            # Verificar si la conversión fue cancelada o si se solicitó enviar original
            if conversion_id in cancelled_conversions or conversion_id in send_original_requests:
                debug("[CONVERSION] Conversion cancelled or send original requested during progress reading, stopping...")
                # Nadie va a seguir leyendo stdout: si ffmpeg siguiera vivo,
                # llenaría la tubería y proc.wait() no volvería nunca
                _stop_process(proc)
                break

            line = await proc.stdout.readline()
            if not line:
                break

            line = line.decode().strip()

            # ffmpeg reporta progreso en formato "out_time_ms=XXXXX". Al
            # principio, antes del primer fotograma, manda "out_time_ms=N/A":
            # no es un error, simplemente aún no hay progreso
            if line.startswith("out_time_ms=") and line != "out_time_ms=N/A":
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
                                    debug("[CONVERSION] Message edit failed because conversion was cancelled or send original requested")
                                    _stop_process(proc)
                                    break
                except Exception as e:
                    warning(f"[CONVERSION] Error processing progress line: {e}")

        debug("[CONVERSION] Waiting for process completion...")
        await proc.wait()

        debug(f"[CONVERSION] Process finished with code: {proc.returncode}")

        # El proceso ya terminó: stderr llegó a EOF y el drenado acaba solo
        await drain_task

        # Limpiar el proceso de active_tasks
        active_tasks.pop(conversion_id, None)
        active_tasks.pop(f"{conversion_id}_original_path", None)
        debug("[CONVERSION] Process removed from active_tasks")

        # Verificar si el usuario eligió enviar el archivo original
        if conversion_id in send_original_requests:
            debug("[CONVERSION] ✅ User chose to send original file")
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
            debug("[CONVERSION] ❌ Conversion was cancelled by user")
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
                    debug("[CONVERSION] Status message updated to 'preparing send'")
                except Exception as e:
                    debug(f"[CONVERSION] Could not update status message: {e}")

            # Retornar el archivo convertido (temporal)
            # NOTA: Este archivo debe ser eliminado después de enviarlo
            return output_path
        else:
            # Leer stderr para obtener mensaje de error (principio y final, ver
            # accumulate_process_stderr)
            stderr = b"".join(stderr_chunks)
            error_msg = stderr.decode(errors="replace") if stderr else "Unknown error"
            error(f"[CONVERSIÓN] ❌ Error convirtiendo video (código {proc.returncode}): {error_msg[:200]}")
            debug(f"[CONVERSION] FFmpeg stderr: {error_msg}")
            # Si falla la conversión, eliminar el archivo de salida si existe
            if os.path.exists(output_path):
                warning(f"[CONVERSION] Deleting failed output file: {output_path}")
                os.remove(output_path)
            # Si la copia de pistas falla (o el encoder hardware no está
            # disponible), reintentar con conversión completa en software
            # para no romper el envío.
            if (stream_copy or selected_hw != "NONE") and not _force_software:
                stats.count("convert_fallback")
                failed_mode = "Stream copy" if stream_copy else selected_hw
                warning(f"[CONVERSION] {failed_mode} failed; retrying with libx264")
                return await convert_video_to_telegram_compatible(
                    input_path, status_message, _force_software=True
                )

            # Retornar el archivo original sin convertir
            debug(f"[CONVERSION] Returning original unconverted file: {input_path}")
            return input_path

    except asyncio.CancelledError:
        # Se canceló la tarea que envía (no el botón de cancelar conversión):
        # ffmpeg no puede quedarse vivo escribiendo un fichero que se va a borrar
        debug("[CONVERSION] ❌ Conversion task cancelled")
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
        if proc is not None and proc.returncode is None:
            _stop_process(proc)
            await proc.wait()
        if output_path and os.path.exists(output_path):
            debug(f"[CONVERSION] Deleting partial file: {output_path}")
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise
    except Exception as e:
        error(f"[CONVERSION] ❌ Exception during video conversion: {e}")
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
        _stop_process(proc)
        # Eliminar archivo de salida parcial si existe
        if output_path and os.path.exists(output_path):
            debug(f"[CONVERSION] Deleting partial file after exception: {output_path}")
            try:
                os.remove(output_path)
            except:
                pass
        # En caso de error, retornar el archivo original
        debug(f"[CONVERSION] Returning original unconverted file after exception: {input_path}")
        return input_path
    finally:
        # Las marcas de esta conversión ya no sirven: el id no se reutiliza
        active_tasks.pop(conversion_id, None)
        cancelled_conversions.discard(conversion_id)
        send_original_requests.discard(conversion_id)

async def send_file_to_telegram(event, file_path, sending_msg=None, delete_after=False):
    """Envía un archivo a Telegram (convirtiendo vídeo si hace falta).

    Si delete_after=True, elimina el original del servidor tras un envío correcto.
    Devuelve el mensaje enviado, o None si falló o se canceló la conversión.
    """
    thumb_path = None
    original_file_path = file_path
    converted_file_path = None
    # Con delete_after, lo que se borra al final tiene que ser lo que se ha
    # enviado: entre la conversión y la subida pasan minutos, y si en /manage
    # se renombra y otro fichero ocupa su nombre, por ruta se borraría ese otro.
    # El descriptor abierto hasta el final impide además que, si se borra, su
    # inodo pase a un fichero nuevo con el mismo nombre
    original_fd = None

    def cleanup_temp_files():
        """Borra el thumbnail y el fichero convertido temporales.

        Se llama desde un `finally` para que los temporales no se queden en
        TEMP_DIR ni cuando la tarea se cancela a mitad del envío (CancelledError
        no lo captura el `except Exception`).
        """
        if thumb_path and os.path.exists(thumb_path):
            try:
                debug(f"[SEND] Deleting temporary thumbnail: {thumb_path}")
                os.remove(thumb_path)
            except Exception as cleanup_error:
                warning(f"[SEND] ⚠️ Error deleting temporary thumbnail: {cleanup_error}")

        if converted_file_path and converted_file_path != original_file_path and os.path.exists(converted_file_path):
            try:
                debug(f"[SEND] Deleting temporary converted file: {converted_file_path}")
                os.remove(converted_file_path)
                debug("[SEND] ✅ Temporary converted file deleted")
            except Exception as cleanup_error:
                warning(f"[SEND] ⚠️ Error deleting temporary converted file: {cleanup_error}")

    try:
        debug(f"[SEND] Preparing file send: {file_path}")
        if delete_after:
            original_fd = os.open(original_file_path, os.O_RDONLY)

        # Las mismas extensiones que get_file_info y /manage usan para ofrecerlo
        # como vídeo: con una lista propia, un .m4v o un .ts se ofrecía como
        # vídeo pero se enviaba como documento
        file_ext = os.path.splitext(file_path)[1].lower()
        is_video = file_ext in EXTENSIONS_VIDEO
        is_audio = file_ext in EXTENSIONS_AUDIO
        original_filename = os.path.basename(file_path)
        display_filename = original_filename
        attributes = [DocumentAttributeFilename(file_name=original_filename)]
        sent_unconverted = False

        debug(f"[SEND] File type: {'video' if is_video else 'audio' if is_audio else 'document'}")

        if is_video:
            debug("[SEND] Starting video conversion...")
            converted_file_path = await convert_video_to_telegram_compatible(file_path, sending_msg)

            if converted_file_path is None:
                debug("[SEND] ❌ Conversion cancelled, aborting send")
                if sending_msg:
                    await safe_delete(sending_msg)
                return None

            if converted_file_path != file_path:
                converted_file_path = original_if_converted_too_large(file_path, converted_file_path)
                sent_unconverted = converted_file_path == file_path

            if converted_file_path != file_path:
                debug(f"[SEND] Using converted file: {converted_file_path}")
                file_path = converted_file_path
                display_filename = os.path.splitext(original_filename)[0] + ".mp4"
                debug(f"[SEND] Name updated to: {display_filename}")
                attributes = [DocumentAttributeFilename(file_name=display_filename)]
            else:
                debug("[SEND] Video already compatible, using original")

            debug("[SEND] Getting video metadata...")
            duration, width, height = await get_video_metadata(file_path)
            if duration and width and height:
                debug(f"[SEND] Metadata: {duration}s, {width}x{height}")
                attributes.append(DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True
                ))
            else:
                debug("[SEND] ⚠️ Could not get video metadata")

            debug("[SEND] Generating thumbnail...")
            thumb_path = await generate_video_thumbnail(file_path)
            if thumb_path:
                debug(f"[SEND] Thumbnail generated: {thumb_path}")
            else:
                debug("[SEND] ⚠️ Could not generate thumbnail")
        elif is_audio:
            # Sin la duración, Telegram muestra el audio sin barra de reproducción
            debug("[SEND] Getting audio metadata...")
            duration, _, _ = await get_video_metadata(file_path)
            if duration:
                debug(f"[SEND] Audio duration: {duration}s")
                attributes.append(DocumentAttributeAudio(duration=duration))
            else:
                debug("[SEND] ⚠️ Could not get audio duration")

        upload_progress = create_upload_progress_callback(sending_msg, display_filename)

        debug("[SEND] Starting send to Telegram...")
        file_size = os.path.getsize(file_path)
        debug(f"[SEND] File size: {file_size} bytes")

        # Antes de llegar aquí se mide el original, y si el convertido no cabe
        # se sube el original: solo pasa de aquí si el original ha cambiado
        # entretanto. Telegram lo rechazaría tras intentarlo por las dos vías.
        # Con el nombre: en una playlist no se sabría cuál ha sido
        if file_size >= MAX_TELEGRAM_FILE_SIZE:
            warning(f"[SEND] {display_filename} exceeds Telegram's upload limit ({file_size} bytes), not sending")
            if sending_msg:
                await safe_delete(sending_msg)
            await safe_reply_in_topic(event, get_text("error_file_too_large_named", original_filename), parse_mode=PARSE_MODE)
            return None

        # En un foro, al tema del que salió la petición y no a General
        message = await _send_file_fast(
            event.chat_id,
            file_path,
            display_filename,
            attributes,
            thumb_path,
            is_video,
            upload_progress,
            reply_to=await get_topic(event),
        )

        debug(f"[SEND] ✅ File sent to Telegram: {original_file_path}")
        if sending_msg:
            await safe_delete(sending_msg)

        if sent_unconverted:
            await safe_respond(event, get_text("file_sent_unconverted", original_filename), reply_to=message.id, parse_mode=PARSE_MODE)

        if delete_after:
            debug(f"[SEND] Deleting original file from server: {original_file_path}")
            # En su propio try: el fichero ya está enviado, así que un fallo al
            # borrarlo no debe reportarse como error de envío
            try:
                # Por ruta, solo si en ella sigue el fichero que se ha enviado
                same_file = os.path.samestat(os.stat(original_file_path), os.fstat(original_fd))
                if same_file:
                    os.remove(original_file_path)
            except Exception as delete_error:
                error(f"[SEND] ❌ File sent but could not be deleted from server: {delete_error}")
                # Como código: el error lleva la ruta, y el nombre (que puede
                # venir del servidor remoto) no debe leerse como markdown
                await safe_respond(event, get_text("error_deleting_after_send", md_code(delete_error)), reply_to=message.id, parse_mode=PARSE_MODE)
            else:
                if same_file:
                    debug(f"[SEND] ✅ File sent to Telegram and deleted from server: {original_file_path}")
                    await safe_respond(event, get_text("deleted_from_server"), reply_to=message.id, parse_mode=PARSE_MODE)
                else:
                    warning(f"[SEND] ⚠️ {original_file_path} was replaced by another file during the send, not deleting it")
                    await safe_respond(event, get_text("error_deleting_after_send_replaced"), reply_to=message.id, parse_mode=PARSE_MODE)

        return message
    except Exception as e:
        error(f"[SEND] ❌ Error sending file {file_path}: {e}")

        if sending_msg:
            try:
                await safe_delete(sending_msg)
            except Exception as delete_error:
                warning(f"[SEND] ⚠️ Error deleting progress message: {delete_error}")

        await safe_reply_in_topic(event, get_text("error_sending_the_file_user"), parse_mode=PARSE_MODE)
        return None
    finally:
        cleanup_temp_files()
        if original_fd is not None:
            os.close(original_fd)


async def send_file_automatically(event, file_path):
    """Envía un fichero sin preguntar, según el envío automático (SEND o SEND_DELETE).

    Comprueba antes el límite de subida de Telegram: con el envío automático el
    usuario espera recibir el fichero, así que si no cabe hay que decírselo en
    lugar de dejarlo pasar en silencio.

    Devuelve el mensaje enviado, o None si no se pudo enviar.
    """
    filename = os.path.basename(file_path)

    # En una playlist los ficheros se envían uno tras otro y ya están a la vista
    # en /manage: si entretanto se borra o renombra uno, se avisa de ese y se
    # sigue con los demás en lugar de cortar el envío con una excepción
    try:
        file_size = os.path.getsize(file_path)
    except OSError as e:
        warning(f"[AUTO_SEND] {filename} is no longer available, not sending it: {e}")
        await safe_reply_in_topic(event, get_text("error_file_does_not_exist_named_user", filename), parse_mode=PARSE_MODE)
        return None

    if file_size >= MAX_TELEGRAM_FILE_SIZE:
        debug(f"[AUTO_SEND] {filename} exceeds Telegram's upload limit, storing only")
        # Con el nombre: en una playlist no se sabría cuál de ellos es
        await safe_reply_in_topic(event, get_text("error_file_too_large_named", filename), parse_mode=PARSE_MODE)
        return None

    auto_send = settings.auto_send()
    delete_after = auto_send == "SEND_DELETE"
    stats.count("send_auto")
    debug(f"[AUTO_SEND] auto send={auto_send}, sending {filename} (delete_after={delete_after})")

    sending_msg = await safe_reply_in_topic(
        event,
        get_text("sending", filename),
        parse_mode=PARSE_MODE,
        wait_for_result=True
    )

    # Si la cola dio timeout no tenemos el mensaje para editarlo, así que el
    # envío va sin barra de progreso ni botón de cancelar conversión. No se
    # reintenta porque el mensaje original sigue en la cola y saldría duplicado
    if sending_msg is None:
        warning(f"[AUTO_SEND] No progress message available for {filename}, sending without progress")

    return await send_file_to_telegram(event, file_path, sending_msg, delete_after=delete_after)


async def handle_success(event, file_path, show_action_buttons=True, icon=None, content_type=None):
    try:
        debug(f"[SEND_FILE] handle_success called for: {file_path}")

        # Para archivos .torrent, el gestor de torrents puede procesarlos inmediatamente
        # (renombrarlos o eliminarlos), lo cual es comportamiento esperado
        is_torrent = file_path.lower().endswith('.torrent')
        file_exists = os.path.exists(file_path)

        if not file_exists and not is_torrent:
            error(f"[SEND_FILE] File does not exist: {file_path}")
            return

        # Obtener información del archivo
        filename = get_filename_from_path(file_path)

        if file_exists:
            file_size = os.path.getsize(file_path)
            debug(f"[SEND_FILE] File size: {file_size} bytes")
            debug("[SEND_FILE] Getting file info...")
            file_info = await get_file_info(file_path)
            debug(f"[SEND_FILE] File type: {file_info.get('type', 'unknown')}")
        else:
            # Archivo .torrent ya procesado por el gestor
            debug("[SEND_FILE] Torrent file already processed by torrent manager (expected behavior)")
            file_info = {
                'type': 'torrent',
                'size_formatted': 'Unknown',
                'duration_formatted': None,
                'resolution': None,
                'codec_video': None,
                'codec_audio': None,
                'bitrate': None
            }

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
                "torrent": TOR_ICO,
                "document": DEF_ICO
            }
            icon = icon_map.get(file_info["type"], DEF_ICO)
            file_type = file_info["type"]

        # Mensaje unificado: Descarga completada + información detallada
        info_lines = [
            get_text("downloaded", icon, filename),
            ""
        ]

        # Solo agregar tamaño si está disponible
        if file_info['size_formatted'] and file_info['size_formatted'] != 'Unknown':
            info_lines.append(get_text("file_info_size", file_info['size_formatted']))

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
        debug("[SEND_FILE] Sending success message to user...")

        # Con botones o envío detrás, se espera a que salga para que no se le
        # adelanten. Sin nada detrás (lo que llega por Telegram) no: la descarga
        # aún tiene su hueco del semáforo, y detrás de un FloodWait lo retenía
        # minutos sin bajar nada
        try:
            reply_result = await safe_reply_in_topic(event, info_message, parse_mode=PARSE_MODE,
                                                     wait_for_result=show_action_buttons)
            debug(f"[SEND_FILE] safe_reply returned: {reply_result}")
            debug("[SEND_FILE] ✅ Success message sent")
        except asyncio.TimeoutError:
            error("[SEND_FILE] ❌ Timeout sending success message (waited 5 minutes)")
            raise
        except Exception as reply_error:
            error(f"[SEND_FILE] ❌ Error sending success message: {reply_error}")
            error(f"[SEND_FILE] Reply error type: {type(reply_error).__name__}")
            raise

        # Solo mostrar botones de acción si se solicita (para descargas de URLs) y solo para video/audio
        debug("[SEND_FILE] Checking if action buttons should be shown...")
        auto_send = settings.auto_send()
        debug(f"[SEND_FILE] show_action_buttons={show_action_buttons}, file_type={file_type}, auto send={auto_send}")
        if show_action_buttons and file_type in ["video", "audio"]:
            if auto_send == "STORE":
                debug("[SEND_FILE] auto send=STORE, keeping file on server without asking")
            elif auto_send in ("SEND", "SEND_DELETE"):
                await send_file_automatically(event, file_path)
            elif file_size >= MAX_TELEGRAM_FILE_SIZE:
                debug("[SEND_FILE] File size is too large to send via Telegram. Maximum size is 2000 MiB")
            else:
                # Indexado por fichero y no por evento: una sola URL puede
                # producir varios ficheros y con la clave del evento cada vuelta
                # sobrescribía la anterior, dejando botones muertos
                send_id = register_pending_send(file_path)
                buttons = [
                    [
                        Button.inline(get_text("button_send"), data=f"send:{send_id}"),
                        Button.inline(get_text("button_send_and_delete"), data=f"senddelete:{send_id}"),
                    ],
                    [Button.inline(get_text("button_only_in_server"), data=f"nosend:{send_id}")]
                ]
                await safe_reply_in_topic(event, get_text("upload_asking"), buttons=buttons, parse_mode=PARSE_MODE)
    except Exception as e:
        error(f"[SEND_FILE] ❌ Error in handle_success: {e}")
        error(f"[SEND_FILE] Exception type: {type(e).__name__}")
        import traceback
        error(f"[SEND_FILE] Traceback: {traceback.format_exc()}")
        # Re-lanzar para que el caller pueda manejar el error
        raise













@bot.on(events.CallbackQuery(pattern=b"(send|senddelete|nosend):(.+)"))
async def handle_send_choice(event):
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    action = event.pattern_match.group(1).decode()
    file_id = event.pattern_match.group(2).decode()
    stats.count(f"btn_{action}")

    # Extraer y eliminar atómicamente la entrada para evitar dobles ejecuciones
    # por doble click (condición de carrera): el segundo click obtiene None y sale
    file_path = pending_files.pop(file_id, None)

    if file_path is None:
        # Ya procesado por otro click (doble click) o entrada inexistente
        return

    if not os.path.exists(file_path):
        await safe_edit(event, get_text("error_file_does_not_exist_user"), parse_mode=PARSE_MODE)
        error(f"[FILE_DELETE] The file does not exist. Path: {file_path}")
        return

    if action in ("send", "senddelete"):
        # El try cubre también la creación del mensaje de progreso: si la cola
        # falla ahí, el usuario debe enterarse igual que si fallara el envío
        try:
            sending_msg = await safe_edit(
                event,
                get_text("sending", os.path.basename(file_path)),
                parse_mode=PARSE_MODE,
                wait_for_result=True
            )

            # Si no se pudo editar el mensaje (timeout en cola), se envía sin
            # barra de progreso, como en send_file_automatically: uno nuevo
            # mandado sin esperar llegaría sin referencia y nadie lo borraría
            if sending_msg is None:
                warning(f"[SEND BUTTON] No progress message available for {os.path.basename(file_path)}, sending without progress")
        except Exception as e:
            error(f"[SEND BUTTON] ❌ Error preparing the progress message: {e}")
            await safe_reply_in_topic(event, get_text("error_sending_the_file_user"), parse_mode=PARSE_MODE)
            return

        # Fuera del handler: con un corte de Telegram, Telethon lo cancela y
        # la conversión, que puede durar horas, moría con él
        await outlive_handler(send_file_to_telegram(
            event,
            file_path,
            sending_msg,
            delete_after=(action == "senddelete")
        ))
    else:
        await safe_delete(event)

async def handle_cancel(status_message, temp_marker=None):
    """Avisa de la cancelación y borra los temporales de esa descarga.

    `temp_marker` es la parte del nombre que solo llevan los ficheros de esta
    descarga en TEMP_DIR (ver ytdlp_temp_marker). Antes se borraba `*.part*` de
    la carpeta de destino, que no es donde se descarga y donde sí hay ficheros
    del usuario como "Pelicula.part1.rar".
    """
    if status_message:
        await safe_edit(status_message, get_text("cancelled"), buttons=None, parse_mode=PARSE_MODE)
    debug("[URL_DOWNLOAD] URL download cancelled")
    if temp_marker:
        remove_temp_files(temp_marker)

async def send_startup_message():
    message = get_text("initial_message", VERSION)
    # Lo que hay que arreglar fuera del bot (el compose, el volumen, un
    # settings.json roto) se dice aquí, no solo en el log. Si /config no
    # guarda nada, lo importado tampoco: se dice eso y el aviso queda pendiente
    import_notice = False
    if not store.is_persistent():
        message += "\n\n" + get_text("startup_not_persistent")
    elif store.write_error():
        message += "\n\n" + get_text("startup_not_writable")
    elif migration.import_notice_pending():
        message += "\n\n" + get_text("startup_settings_imported")
        import_notice = True
    if store.settings_unreadable():
        message += "\n\n" + get_text("settings_unreadable", store.settings_path())
    if not downloads_persistent():
        message += "\n\n" + get_text("startup_downloads_not_persistent")
    ignored_admins = wrong_admin_ids()
    if ignored_admins:
        message += "\n\n" + get_text("startup_admin_ignored", ", ".join(md_code(admin) for admin in ignored_admins))
    for admin in admin_ids():
        try:
            # Un bot no puede escribir primero: si este admin nunca le ha
            # abierto el chat, Telegram no lo encuentra. Se mira aquí porque en
            # la cola el envío se reintentaba y la paraba unos 10 s
            await bot.get_input_entity(int(admin))
        except Exception as e:
            warning(f"[STARTUP] Admin {admin} cannot receive the initial message, they must open the chat with the bot first: {e}")
            continue
        # El aviso de la importación se da una vez: se espera a que llegue, y
        # si no le llega a nadie se repite en el siguiente arranque
        try:
            sent = await safe_send_message(int(admin), message, parse_mode=PARSE_MODE,
                                           wait_for_result=import_notice)
        except Exception as e:
            warning(f"[STARTUP] Could not send the initial message to admin {admin}: {e}")
            continue
        if import_notice and sent:
            migration.import_notice_sent()
            import_notice = False

async def set_commands():
    commands = [
        BotCommand("start", get_text("menu_start")),
        BotCommand("list", get_text("menu_list")),
        BotCommand("manage", get_text("menu_manage")),
        BotCommand("settings", get_text("menu_settings")),
        BotCommand("version", get_text("menu_version")),
        BotCommand("donate", get_text("menu_donate")),
        BotCommand("donors", get_text("menu_donors")),
    ]
    # Para todos los idiomas del cliente, y también para "es" y "en", que es
    # donde los dejaban las versiones anteriores: si no se pisan, quien tenga
    # Telegram en ese idioma seguiría viendo la lista vieja, sin /settings
    for lang_code in ("", "es", "en"):
        await bot(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code=lang_code,
            commands=commands
        ))


async def apply_setting(key):
    """Lo que hay que hacer al cambiar un ajuste desde /settings.

    La mayoría se leen en el momento de usarlos y no necesitan nada. Estos
    dos viven en otro sitio: los comandos del menú, en Telegram, y el límite
    de descargas, en el semáforo que ya está creado.
    """
    if key == "language":
        await set_commands()
    elif key == "downloads.parallel":
        await download_semaphore.set_limit(settings.parallel_downloads())

async def heartbeat_writer():
    while True:
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(int(time.time())))
        except Exception as e:
            error(f"[HEARTBEAT] Failed to write {HEARTBEAT_FILE}: {e}")
        await asyncio.sleep(HEARTBEAT_INTERVAL)

async def start_pot_provider():
    """Arranca el servidor local de PO Token que yt-dlp usa para YouTube.

    Sin este proveedor, YouTube devuelve HTTP 403 en los formatos de alta calidad.
    Devuelve el proceso lanzado, o None si no está disponible o no arrancó.
    """
    node_modules = os.path.join(POT_PROVIDER_DIR, "node_modules")
    if not os.path.isdir(node_modules):
        warning(f"[POT] Provider not found at {POT_PROVIDER_DIR}, YouTube downloads may fail with HTTP 403")
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            "deno", "run", "--allow-env", "--allow-net", "--allow-ffi=.", "--allow-read=.",
            os.path.join("..", "src", "main.ts"), "--port", str(POT_PROVIDER_PORT),
            cwd=node_modules,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
    except Exception as e:
        error(f"[POT] Failed to launch PO Token provider: {e}")
        return None

    ping_url = f"http://127.0.0.1:{POT_PROVIDER_PORT}/ping"
    deadline = time.time() + POT_PROVIDER_STARTUP_TIMEOUT
    while time.time() < deadline:
        if proc.returncode is not None:
            error(f"[POT] PO Token provider exited with code {proc.returncode}")
            return None
        try:
            await asyncio.to_thread(requests.get, ping_url, timeout=2)
            debug(f"[POT] PO Token provider ready on port {POT_PROVIDER_PORT}")
            return proc
        except Exception:
            await asyncio.sleep(1)

    warning(f"[POT] PO Token provider did not respond in {POT_PROVIDER_STARTUP_TIMEOUT}s")
    return proc

# Registrar los handlers de /list y /manage e inyectarles el pipeline de envio
manage_handlers.init(
    bot,
    convert_video_to_telegram_compatible=convert_video_to_telegram_compatible,
    create_upload_progress_callback=create_upload_progress_callback,
    get_extraction_message_and_buttons=get_extraction_message_and_buttons,
    _send_file_fast=_send_file_fast,
)
settings_handlers.init(bot, on_change=apply_setting)


# Lo que contesta Telegram cuando ya no acepta la auth key de la sesión: el
# token se revocó en @BotFather (401), olvidó la clave (-404) o la usan dos
# conexiones a la vez (AUTH_KEY_DUPLICATED: dos contenedores con un /config)
SESSION_REJECTED_ERRORS = (errors.UnauthorizedError, errors.AuthKeyError, errors.AuthKeyNotFound)


def session_rejection(e):
    """El error con que Telegram rechaza la sesión, si es lo que ha cortado `e`; si no, None.

    Si lo ve al ponerse al día (catch_up), Telethon se desconecta él solo y
    lo que sale es la petición que iba en paralelo, cancelada o sin conexión:
    el motivo queda en _updates_error.
    """
    if isinstance(e, SESSION_REJECTED_ERRORS):
        return e
    reason = getattr(bot, "_updates_error", None)
    if isinstance(e, (asyncio.CancelledError, ConnectionError)) and isinstance(reason, SESSION_REJECTED_ERRORS):
        return reason
    return None


async def exit_if_session_rejected(e):
    """Si `e` es que Telegram no acepta el token o la sesión, lo dice claro y sale.

    Con una sesión que ya no vale, el bot no arrancaba nunca: Docker lo
    reiniciaba en bucle con la misma, el log solo traía un traceback y,
    desde que la sesión vive en /config, ni recrear el contenedor lo
    arreglaba. Para un bot, una nueva solo cuesta volver a entrar con el
    token: esta se aparta como .bad y el siguiente arranque crea otra.
    """
    if isinstance(e, (errors.AccessTokenInvalidError, errors.AccessTokenExpiredError)):
        error("[CONFIG] Telegram does not accept TELEGRAM_TOKEN: it is wrong or was revoked. Put the one @BotFather gives now")
        sys.exit(1)
    reason = session_rejection(e)
    if reason is None:
        return
    await bot.disconnect()
    session_file = bot.session.filename
    try:
        # Con su -journal, que es parte de la base de datos
        for leftover in (session_file, f"{session_file}-journal"):
            if os.path.exists(leftover):
                os.replace(leftover, f"{leftover}.bad")
    except OSError as move_error:
        error(f"[CONFIG] Telegram does not accept the saved session ({type(reason).__name__}: token revoked, key forgotten or session used twice) and it could not be moved aside ({move_error}): delete {session_file} to start a new one")
    else:
        error(f"[CONFIG] Telegram does not accept the saved session ({type(reason).__name__}: token revoked, key forgotten or session used twice): it was moved to {session_file}.bad and a new one will be created on the next start")
    sys.exit(1)


async def main():
    debug(f"[STARTUP] DropBot v{VERSION}")
    debug(f"[STARTUP] Settings: {store.settings_path()}")
    debug(f"[STARTUP] auto format={settings.auto_format()}, auto send={settings.auto_send()}")
    debug(f"[STARTUP] ffmpeg hw={settings.ffmpeg_hw()}, quality={settings.ffmpeg_quality()}")
    debug(f"[STARTUP] parallel downloads={settings.parallel_downloads()}, fast connections={settings.fast_connections()}")

    # docker stop manda SIGTERM, que tini le pasa al bot: sin handler el proceso
    # moriría sin cerrar nada (y sin tini, como PID 1, el kernel lo ignoraría y
    # acabaría en SIGKILL / exit 137). Al recibirlo, desconectar el bot para que
    # run_until_disconnected() retorne y se ejecute el cierre.
    # Se pone antes de conectar: sin red, la conexión se reintenta sin fin y
    # disconnect() no lo corta, así que lo que se cancela es la conexión
    loop = asyncio.get_running_loop()
    stopping = False
    connecting = None

    def request_shutdown(sig):
        nonlocal stopping
        debug(f"[SHUTDOWN] {sig.name} received, stopping...")
        stopping = True
        if connecting is not None:
            connecting.cancel()
        # Sin conexión no hay nada que cortar, y disconnect() guardaría en la
        # sesión un estado vacío: el siguiente arranque no se pondría al día
        elif bot.is_connected():
            asyncio.ensure_future(bot.disconnect())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_shutdown, sig)

    async def until_connected(connect):
        """Espera a `connect()` salvo que llegue SIGTERM. True si ha conectado."""
        nonlocal connecting
        if stopping:
            return False
        connecting = asyncio.ensure_future(connect())
        try:
            await connecting
        except asyncio.CancelledError:
            if not stopping:
                raise
        finally:
            connecting = None
        return not stopping

    async def reconnect():
        # Telethon reconecta solo y sin límite, salvo tras algunos cortes,
        # como un flood a nivel de transporte (429): esos se reintentan aquí
        while True:
            await asyncio.sleep(RECONNECT_DELAY)
            try:
                await bot.connect()
                return
            except (ConnectionError, InvalidBufferError) as e:
                warning(f"[NETWORK] Could not reconnect to Telegram: {e}")

    pot_proc = await start_pot_provider()
    try:
        connected = await until_connected(lambda: bot.start(bot_token=TELEGRAM_TOKEN))
    except (Exception, asyncio.CancelledError) as e:
        await exit_if_session_rejected(e)
        raise
    if not connected:
        if pot_proc and pot_proc.returncode is None:
            pot_proc.terminate()
        return
    await message_queue.start()  # Iniciar la cola de mensajes
    # Para no atender los comandos dirigidos a otro bot ("/list@OtroBot"). Si
    # no sale ahora, lo pide for_another_bot con el primer comando que lleve
    # @: un error aquí tumbaba el arranque
    try:
        telegram_helpers.set_bot_username((await bot.get_me()).username)
    except Exception as e:
        warning(f"[STARTUP] Could not get the bot username: {e}")
    # El menú de comandos es cosmético: si Telegram no lo acepta ahora (un
    # FloodWait largo, un error del servidor), el bot arranca igual
    try:
        await set_commands()
    except Exception as e:
        warning(f"[STARTUP] Could not set bot commands: {e}")
    await send_startup_message()
    stats.start(VERSION)
    heartbeat_task = asyncio.create_task(heartbeat_writer())

    try:
        while True:
            try:
                await bot.run_until_disconnected()
                break
            except (ConnectionError, InvalidBufferError) as e:
                # Volver a conectar no sirve si Telegram ya no acepta la sesión
                if session_rejection(e):
                    raise
                # Salir mataba las descargas en curso y, al rearrancar, se
                # borraban sus temporales. Lo que corría dentro de un handler
                # no se salva así: al desconectar, Telethon los cancela, y por
                # eso enviar, convertir y descomprimir van aparte (ver
                # outlive_handler)
                warning(f"[NETWORK] Disconnected from Telegram ({e}), reconnecting in {RECONNECT_DELAY}s...")
            if not await until_connected(reconnect):
                break
    except (Exception, asyncio.CancelledError) as e:
        # El token se ha revocado, o Telegram ha olvidado la sesión, con el
        # bot en marcha
        await exit_if_session_rejected(e)
        raise
    finally:
        heartbeat_task.cancel()
        try:
            await asyncio.wait_for(message_queue.shutdown(), timeout=10)  # Detener la cola al finalizar
        except asyncio.TimeoutError:
            warning("[SHUTDOWN] Message queue did not stop in 10s")
        if pot_proc and pot_proc.returncode is None:
            pot_proc.terminate()

if __name__ == "__main__":
    bot.loop.run_until_complete(main())
