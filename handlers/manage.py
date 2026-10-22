"""Handlers de /list y /manage: listar, ver acciones, renombrar, borrar, extraer.

Este grupo se separó de dropbot.py, que tenía los 24 handlers y todo lo demás
en un solo fichero de 4.500 líneas.

Los cuerpos de las funciones están tal cual estaban: lo único que cambia es de
dónde vienen sus dependencias. El estado compartido llega por `state`, los
envíos por `utils.telegram_helpers`, y las cuatro funciones del pipeline de
envío que siguen en dropbot.py se inyectan con `init()` para no crear un ciclo
de imports (dropbot importa este módulo, no al revés).
"""

import asyncio
import hashlib
import os
import re
import shutil
import stat
import time

import rarfile
from telethon import Button, events
from telethon.tl.types import DocumentAttributeFilename, MessageMediaWebPage

from basic import (
    MAX_FILENAME_BYTES, clean_rar_base_name, has_invisible_chars, is_admin, is_compressed_file,
    is_split_zip, md_code,
)
import settings
import stats
from config import (
    AUD_ICO, BOO_ICO, DOWNLOAD_PATH, DOWNLOAD_PATHS,
    EXTENSIONS_AUDIO, EXTENSIONS_VIDEO, IMG_ICO, MAX_TELEGRAM_FILE_SIZE, TOR_ICO,
    VID_ICO, has_own_folder,
)
from logger import debug, error, warning
from services.extraction_service import extract_file
from services.video_service import (
    generate_video_thumbnail, get_video_metadata, original_if_converted_too_large,
)
from state import (
    extractions_in_progress, list_messages, pending_file_actions, pending_renames,
    register_manage_action,
)
from translations import PARSE_MODE, get_text
from utils.file_helpers import EXTRACTING_SUFFIX, format_file_size, get_directory_size, get_file_icon, partial_name
from utils.telegram_helpers import (
    SAFE_MESSAGE_LENGTH, check_admin_and_warn, get_topic, outlive_handler, safe_answer,
    safe_delete, safe_edit, safe_send_message, utf16_len,
)

# Inyectadas por init() desde dropbot.py: son el pipeline de envío a Telegram,
# que sigue viviendo allí porque lo comparte con las descargas de URL
convert_video_to_telegram_compatible = None
create_upload_progress_callback = None
get_extraction_message_and_buttons = None
_send_file_fast = None


def init(bot, **dependencies):
    """Inyecta las dependencias de dropbot.py y registra los handlers en `bot`."""
    globals().update(dependencies)

    missing = [name for name in ("convert_video_to_telegram_compatible",
                                 "create_upload_progress_callback",
                                 "get_extraction_message_and_buttons",
                                 "_send_file_fast") if globals()[name] is None]
    if missing:
        raise RuntimeError(f"handlers.manage.init() sin dependencias: {missing}")

    for event, handler in _registrations():
        bot.on(event)(handler)


def _registrations():
    """Los eventos de Telegram que atiende este módulo."""
    return [
        (events.CallbackQuery(pattern=b"listcat:(.+)"), handle_list_category),
        (events.CallbackQuery(pattern=b"managecat:(.+)"), handle_manage_category),
        (events.CallbackQuery(pattern=b"fileact:(.+)"), handle_file_action),
        (events.CallbackQuery(pattern=b"download:(.+)"), handle_download_file),
        (events.CallbackQuery(pattern=b"close"), handle_close),
        (events.CallbackQuery(pattern=b"delete:(.+)"), handle_delete_file),
        (events.CallbackQuery(pattern=b"confirmdelete:(.+)"), handle_confirm_delete),
        (events.CallbackQuery(pattern=b"rename:(.+)"), handle_rename_file),
        (events.NewMessage(func=is_rename_answer), handle_rename_input),
        (events.CallbackQuery(pattern=b"extract:(.+)"), handle_extract_file),
        (events.CallbackQuery(pattern=b"(delcompressed|keepcompressed):(.+)"), handle_compressed_file_action),
    ]


def _display_name(name):
    """El nombre de un fichero tal como se puede enseñar en Telegram.

    Un nombre que no es UTF-8 en disco (un ZIP de Windows descomprimido en el
    host, un FTP con otro juego de caracteres) llega de os.listdir con
    sustitutos que no se pueden codificar, y el listado entero fallaba al
    enviarse. Esos bytes se enseñan como "\\xe9"; para operar con el fichero
    se sigue usando la ruta de verdad.
    """
    return os.fsencode(name).decode("utf-8", "backslashreplace")


# Lo que se espera el nombre nuevo después de pulsar Renombrar: pasado ese
# tiempo, un texto suelto ya no se toma por la respuesta a uno olvidado. Una
# respuesta que cita el mensaje de Renombrar vale siempre (ver _rename_requests)
RENAME_TIMEOUT = 15 * 60


def _rename_requests(event):
    """Las peticiones de renombrado a las que puede contestar `event`.

    Solo las de su chat, y un texto suelto solo las que no han caducado: si
    no, un "hola" horas después, o en un grupo, renombraba el fichero. Citar
    el mensaje de Renombrar no deja dudas, y vale aunque haya pasado el
    tiempo: el mensaje sigue pidiendo que se le responda, y la respuesta se
    perdía sin decir nada. Fuera del privado solo vale esa cita, porque allí
    el admin también habla de otras cosas.
    """
    now = time.monotonic()
    quoted = event.reply_to_msg_id
    requests = [data for data in pending_renames.get(event.sender_id, [])
                if data["chat_id"] == event.chat_id
                and (now - data["created"] < RENAME_TIMEOUT or (quoted and data["message_id"] == quoted))]
    if requests and not event.is_private:
        requests = [data for data in requests if quoted and data["message_id"] == quoted]
    return requests


def is_rename_answer(event):
    """Si un mensaje es la respuesta a un renombrado pendiente."""
    if not _rename_requests(event):
        return False
    text = event.raw_text or ""
    # Ni comandos, ni ficheros (llegan sin texto o con su pie), ni enlaces:
    # los atienden sus handlers, y tomarlos por un nombre gastaba la petición
    # y borraba el mensaje del usuario
    if not text.strip() or text.startswith('/') or '://' in text:
        return False
    # Un texto con vista previa de enlace también trae media ("Serie.tv" es un
    # dominio), pero es texto: descartarlo dejaba el renombrado sin respuesta
    media = getattr(event, "media", None)
    if media and not isinstance(media, MessageMediaWebPage):
        return False
    return True


def _forget_renames(user_id, file_id):
    """Deja de esperar el nombre nuevo de `file_id` (al cancelar o volver)."""
    user_renames = pending_renames.get(user_id)
    if not user_renames:
        return
    user_renames[:] = [data for data in user_renames if data["file_id"] != file_id]
    if not user_renames:
        pending_renames.pop(user_id, None)


def _item_key(key, is_directory):
    """La variante de una clave para carpeta o para archivo.

    Son claves separadas y no un "$1" con el tipo: en español cambia el género
    ("esta carpeta", "este archivo") y en inglés no debe colarse "archivo".
    """
    return f"{key}_folder" if is_directory else f"{key}_file"


# Comprimidos con doble extensión: "copia.tar.gz" se extrae en "copia/"
DOUBLE_EXTENSIONS = ('.tar.gz', '.tar.bz2', '.tar.xz')


def extraction_folder_name(file_path):
    """Nombre de la carpeta en la que se extrae un comprimido."""
    filename = os.path.basename(file_path)
    # Las partes de un RAR (.partN.rar, .rNN) van a la carpeta del juego
    if rarfile.is_rarfile(file_path):
        return clean_rar_base_name(filename)
    lower = filename.lower()
    for extension in DOUBLE_EXTENSIONS:
        if lower.endswith(extension):
            return filename[:-len(extension)]
    return os.path.splitext(filename)[0]


def get_available_categories():
    """Retorna las categorías que tienen carpeta propia (montada como volumen)"""
    categories = []

    # Siempre está disponible la carpeta principal
    categories.append(("all", get_text("category_all")))

    # Una categoría solo tiene sentido si su carpeta no es la general
    if has_own_folder("video", "url_video"):
        categories.append(("video", f"{VID_ICO} {get_text('category_video')}"))
    if has_own_folder("audio", "url_audio"):
        categories.append(("audio", f"{AUD_ICO} {get_text('category_audio')}"))
    if has_own_folder("photo"):
        categories.append(("photo", f"{IMG_ICO} {get_text('category_photo')}"))
    if has_own_folder("torrent"):
        categories.append(("torrent", f"{TOR_ICO} {get_text('category_torrent')}"))
    if has_own_folder("ebook"):
        categories.append(("ebook", f"{BOO_ICO} {get_text('category_ebook')}"))

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
    buttons.append([Button.inline(get_text("button_close"), data="close")])

    return buttons


async def handle_list_category(event):
    """Maneja los botones de categorías en /list"""
    debug("[LIST] handle_list_category called")

    if await check_admin_and_warn(event):
        debug("[LIST] User is not admin, returning")
        return

    await safe_answer(event)
    category = event.pattern_match.group(1).decode()
    debug(f"[LIST] Category selected: {category}")
    # El tema del foro sale del mensaje del botón: antes de borrarlo
    topic = await get_topic(event)

    # Eliminar TODOS los mensajes anteriores de la lista (si existen)
    user_id = event.sender_id
    if user_id in list_messages:
        for msg in list_messages[user_id]:
            try:
                await safe_delete(msg)
            except:
                pass
        list_messages.pop(user_id, None)

    # El aviso de Renombrar es uno de esos mensajes: borrado, la petición se
    # quedaba esperando y el siguiente texto del usuario renombraba el fichero
    pending_renames.pop(user_id, None)

    # También eliminar el mensaje actual (el que tiene los botones)
    await safe_delete(event)

    await send_file_list(event, category, topic)


def _scan_directories(directories, log_tag):
    """Lo que hay en las carpetas de un listado de /list o /manage.

    Devuelve (elementos, tamaño total, carpetas leídas, entradas vistas). Es
    bloqueante, y no poco: el tamaño de cada carpeta sale de recorrerla entera,
    y en una biblioteca grande o en un volumen de red son segundos en los que
    el bot no atendía nada más. Llamarla con asyncio.to_thread.
    """
    files_info = []
    total_size = 0
    seen_files = set()

    unreadable = None
    readable_dirs = 0
    total_items = 0
    for directory in directories:
        if not os.path.exists(directory):
            debug(f"[{log_tag}] Directory does not exist: {directory}")
            continue

        try:
            dir_files = os.listdir(directory)
        except OSError as e:
            # Una carpeta sin permisos no tira el listado de las demás
            warning(f"[{log_tag}] Cannot read {directory}: {e}")
            unreadable = e
            continue
        readable_dirs += 1
        total_items += len(dir_files)
        # Solo cuántos: con miles de ficheros, la lista entera era una línea
        # de cientos de KB en cada listado y en cada vuelta a /manage
        debug(f"[{log_tag}] Directory {directory} contains {len(dir_files)} items")

        for filename in dir_files:
            file_path = os.path.join(directory, filename)

            # Ignorar los ocultos. Las miniaturas del bot se generan en
            # TEMP_DIR, nunca aquí: saltarse también lo que llevara
            # "_thumb.jpg" solo escondía ficheros del usuario
            if filename.startswith('.'):
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
                warning(f"[{log_tag}] Error processing {filename}: {e}")

    # Si no se ha podido leer ninguna, se avisa del error en vez de
    # decir que no hay archivos
    if unreadable is not None and not readable_dirs:
        raise unreadable

    return files_info, total_size, readable_dirs, total_items


async def send_file_list(event, category, topic):
    """Envía el listado de una categoría ("all", "video"...) al chat de `event`.

    Lo usan los botones de /list y también `/list video`, escrito a mano: una
    sola implementación, para que los dos caminos se vean y fallen igual.
    `topic` es el tema del foro al que va (ver get_topic), o None.
    """
    user_id = event.sender_id
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

        # Recopilar archivos (en otro hilo: con carpetas grandes tarda)
        files_info, total_size, _, _ = await asyncio.to_thread(_scan_directories, directories, "FILE_LIST")

        # Ordenar alfabéticamente por nombre
        files_info.sort(key=lambda x: x["name"].lower())

        # Crear botones de categorías (excluyendo la categoría actual)
        category_buttons = get_category_buttons(exclude_category=category)

        if not files_info:
            # Mostrar mensaje vacío pero con botones para cambiar de categoría
            msg = get_text("list_empty")
            sent_msg = await safe_send_message(event.chat_id, msg, buttons=category_buttons, parse_mode=PARSE_MODE, wait_for_result=True, reply_to=topic)
            if sent_msg:
                list_messages[user_id] = [sent_msg]
            return

        # Construir mensajes (partiendo si es necesario)
        total_size_formatted = format_file_size(total_size)
        header = f"{get_text('list_title')}\n\n"

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
            name = _display_name(file_info["name"])
            display_name = name
            if len(name) > 40:
                display_name = name[:37] + "..."

            # Crear entrada de archivo o carpeta (sin mostrar la ruta)
            file_entry = f"{i}. {file_info['icon']} {md_code(display_name)}\n   💾 {file_info['size_formatted']}"

            # Calcular longitud del mensaje con header y footer
            test_message = header + current_message + "\n\n" + file_entry + footer

            if utf16_len(test_message) > SAFE_MESSAGE_LENGTH and current_message:
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
                msg = msg.replace(header, f"{get_text('list_title_part', idx, len(messages))}\n\n", 1)

            # Solo agregar botones de categorías al último mensaje
            buttons = category_buttons if idx == len(messages) else None
            sent_msg = await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, wait_for_result=True, reply_to=topic)
            if sent_msg:
                sent_messages.append(sent_msg)

        # Guardar todos los mensajes enviados para poder borrarlos después
        if sent_messages:
            list_messages[user_id] = sent_messages

    except Exception as e:
        error(f"[FILE_LIST] Error listing files: {e}")
        await safe_send_message(event.chat_id, get_text("error_list_files"), parse_mode=PARSE_MODE, reply_to=topic)


async def handle_manage_category(event):
    """Maneja los botones de categorías en /manage"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    category = event.pattern_match.group(1).decode()
    # El tema del foro sale del mensaje del botón: antes de borrarlo
    topic = await get_topic(event)

    # Eliminar TODOS los mensajes anteriores de la lista (si existen)
    user_id = event.sender_id
    if user_id in list_messages:
        for msg in list_messages[user_id]:
            try:
                await safe_delete(msg)
            except:
                pass
        list_messages.pop(user_id, None)

    # El aviso de Renombrar es uno de esos mensajes: borrado, la petición se
    # quedaba esperando y el siguiente texto del usuario renombraba el fichero
    pending_renames.pop(user_id, None)

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

        # Recopilar archivos (en otro hilo: con carpetas grandes tarda)
        files_info, total_size, scanned_dirs, total_items = await asyncio.to_thread(
            _scan_directories, directories, "FILE_MANAGE")

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
        category_buttons.append([Button.inline(get_text("button_close"), data="close")])

        if not files_info:
            msg = get_text("manage_no_files")
            sent_msg = await safe_send_message(event.chat_id, msg, buttons=category_buttons, parse_mode=PARSE_MODE, wait_for_result=True, reply_to=topic)
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

            sent_msg = await safe_send_message(event.chat_id, msg, buttons=category_buttons, parse_mode=PARSE_MODE, wait_for_result=True, reply_to=topic)
            if sent_msg:
                list_messages[user_id] = [sent_msg]
            return

        # Crear botones de acción para cada archivo/carpeta
        file_buttons = []
        for i, file_info in enumerate(files_info, 1):
            # Guardar archivo/carpeta en el diccionario para acciones posteriores
            file_id = register_manage_action(file_info["path"])

            # Truncar nombre si es muy largo
            name = _display_name(file_info["name"])
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

        # Como los de arriba, y no en respuesta al mensaje del botón, que ya
        # se ha borrado: en un foro, la respuesta a un mensaje borrado no
        # sabría a qué tema ir
        sent_msg = await safe_send_message(event.chat_id, msg, buttons=file_buttons, parse_mode=PARSE_MODE, wait_for_result=True, reply_to=topic)

        # Guardar el mensaje enviado para poder borrarlo después
        if sent_msg:
            list_messages[user_id] = [sent_msg]

    except Exception as e:
        error(f"[FILE_MANAGE] Error managing files: {e}")
        await safe_send_message(event.chat_id, get_text("error_manage_files"), parse_mode=PARSE_MODE, reply_to=topic)


async def handle_file_action(event):
    """Muestra opciones de acción para un archivo específico"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    file_id = event.pattern_match.group(1).decode()

    # Aquí llegan el Cancelar y el Volver del renombrado: si no se olvida la
    # petición, el siguiente texto que escriba el usuario renombra el fichero
    _forget_renames(event.sender_id, file_id)

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_item_not_found"), parse_mode=PARSE_MODE)
        return

    filename = _display_name(os.path.basename(file_path))
    is_directory = os.path.isdir(file_path)

    if is_directory:
        # Es una carpeta (su tamaño, en otro hilo: hay que recorrerla entera)
        file_size_bytes = await asyncio.to_thread(get_directory_size, file_path)
        file_size = format_file_size(file_size_bytes)
        icon = "📁"
    else:
        # Es un archivo
        file_size_bytes = os.path.getsize(file_path)
        file_size = format_file_size(file_size_bytes)
        file_ext = os.path.splitext(filename)[1].lower()
        icon = get_file_icon(file_ext)

    # Crear mensaje con información del elemento
    msg = f"{get_text(_item_key('file_actions_title', is_directory))}\n\n"
    msg += f"{icon} {get_text('file_actions_name', md_code(filename))}\n"
    msg += f"{get_text('file_actions_size', file_size)}\n"
    msg += f"{get_text('file_actions_path', md_code(os.path.dirname(file_path)))}\n\n"
    msg += get_text('file_actions_what_to_do')

    # Botones de acción
    buttons = []

    # Primera fila: Renombrar y Eliminar
    buttons.append([
        Button.inline(get_text("manage_button_rename"), data=f"rename:{file_id}"),
        Button.inline(get_text("manage_button_delete"), data=f"delete:{file_id}"),
    ])

    # Segunda fila: Descargar (solo si cabe en Telegram, menos de 2000 MiB)
    if not is_directory:
        if file_size_bytes < MAX_TELEGRAM_FILE_SIZE:
            buttons.append([Button.inline(get_text("manage_button_download"), data=f"download:{file_id}")])

        # Botón de descomprimir si es un archivo comprimido
        if is_compressed_file(file_path):
            buttons.append([Button.inline(get_text("manage_button_extract"), data=f"extract:{file_id}")])

    # Última fila: Volver y Cerrar
    buttons.append([
        Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
        Button.inline(get_text("button_close"), data="close")
    ])

    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)


# Ficheros que se están enviando desde /manage: {(chat, ruta real), ...}
sending_files = set()


async def handle_download_file(event):
    """Descarga un archivo del servidor y lo envía a Telegram"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    stats.count("btn_manage_download")
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_file_not_found"), parse_mode=PARSE_MODE)
        return

    filename = _display_name(os.path.basename(file_path))
    file_size_bytes = os.path.getsize(file_path)

    # Verificar tamaño
    if file_size_bytes >= MAX_TELEGRAM_FILE_SIZE:
        await safe_edit(event, get_text("error_file_too_large"), parse_mode=PARSE_MODE)
        return

    # El botón Descargar sigue ahí durante la conversión y la subida, y otro
    # toque convertía y enviaba el fichero dos veces. Se reclama sin ningún
    # await entre mirarlo y apuntarlo, por la ruta real (un fichero puede tener
    # más de un id), y si ya se está enviando a este chat no se dice nada: su
    # progreso ya está a la vista
    sending_key = (event.chat_id, os.path.realpath(file_path))
    if sending_key in sending_files:
        debug(f"[SEND /manage] {filename} is already being sent to this chat, ignoring repeated click")
        return
    sending_files.add(sending_key)

    async def send():
        try:
            await _send_managed_file(event, file_path, filename)
        finally:
            sending_files.discard(sending_key)

    # Fuera del handler: con un corte de Telegram, Telethon lo cancela y la
    # conversión, que puede durar horas, moría con él
    await outlive_handler(send())


async def _send_managed_file(event, file_path, filename):
    """Envía a Telegram un fichero de /manage, con su progreso y su resultado."""
    # En un foro, todo va al tema de la ficha y no a General
    topic = await get_topic(event)
    # Crear un NUEVO mensaje para el progreso de envío (no editar el existente)
    # Esto permite que el usuario abra otros /manage sin borrar el progreso
    sending_msg = await safe_send_message(
        event.chat_id,
        get_text("sending", filename),
        parse_mode=PARSE_MODE,
        wait_for_result=True,
        reply_to=topic
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
        original_filename = filename
        converted_file_path = None  # Para rastrear si se creó un archivo convertido
        sent_unconverted = False

        if is_video:
            debug("[SEND /manage] Starting video conversion...")
            # Convertir el video a formato compatible con Telegram antes de enviarlo
            converted_file_path = await convert_video_to_telegram_compatible(file_path, sending_msg)

            # Si la conversión fue cancelada (retorna None), salir
            if converted_file_path is None:
                debug("[SEND /manage] ❌ Conversion cancelled, aborting send")
                # El mensaje ya fue actualizado por el handler de cancelación
                # No necesitamos hacer nada más, solo salir
                return

            if converted_file_path != file_path:
                converted_file_path = original_if_converted_too_large(file_path, converted_file_path)
                sent_unconverted = converted_file_path == file_path

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
                debug("[SEND /manage] Video already compatible, using original")

            # Obtener metadatos del video
            debug("[SEND /manage] Getting video metadata...")
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
                debug("[SEND /manage] ⚠️ Could not get video metadata")

            # Generar thumbnail
            debug("[SEND /manage] Generating thumbnail...")
            thumb_path = await generate_video_thumbnail(file_path)
            if thumb_path:
                debug(f"[SEND /manage] Thumbnail generated: {thumb_path}")
            else:
                debug("[SEND /manage] ⚠️ Could not generate thumbnail")

        elif is_audio:
            debug("[SEND /manage] Getting audio metadata...")
            # Obtener metadatos del audio
            duration, _, _ = await get_video_metadata(file_path)
            if duration:
                debug(f"[SEND /manage] Audio duration: {duration}s")
                from telethon.tl.types import DocumentAttributeAudio
                attributes.append(DocumentAttributeAudio(
                    duration=duration
                ))
            else:
                debug("[SEND /manage] ⚠️ Could not get audio duration")

        # Crear callback de progreso para el envío
        upload_progress = create_upload_progress_callback(sending_msg, filename)

        debug("[SEND /manage] Starting send to Telegram...")
        file_size = os.path.getsize(file_path)
        debug(f"[SEND /manage] File size: {file_size} bytes")

        # Antes de llegar aquí se mide el original, y si el convertido no cabe
        # se sube el original: solo pasa de aquí si el original ha cambiado
        # entretanto. Telegram lo rechazaba tras intentarlo por las dos vías
        too_large = file_size >= MAX_TELEGRAM_FILE_SIZE
        if too_large:
            warning(f"[SEND /manage] {filename} exceeds Telegram's upload limit ({file_size} bytes), not sending")
        else:
            # Enviar archivo con progreso
            # NO usar wait_for_result=True para no bloquear el event loop
            # Esto permite que el bot siga respondiendo a otros comandos mientras envía
            await _send_file_fast(
                event.chat_id,
                file_path,
                filename,
                attributes,
                thumb_path,
                is_video,
                upload_progress,
                reply_to=topic
            )

            debug("[SEND /manage] ✅ File sent successfully")

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
                debug("[SEND /manage] ✅ Temporary converted file deleted")
            except Exception as e:
                warning(f"[SEND /manage] ⚠️ Error deleting temporary converted file: {e}")

        # Eliminar mensaje de progreso
        if sending_msg and sending_msg != event:
            await safe_delete(sending_msg)

        # Mensaje con el resultado
        if too_large:
            msg = get_text("error_file_too_large_named", original_filename)
        elif sent_unconverted:
            msg = get_text("file_sent_unconverted", filename)
        else:
            msg = get_text("file_sent_success", filename)

        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
            Button.inline(get_text("button_close"), data="close")
        ]]

        # Si sending_msg es el evento original, editar; si no, enviar nuevo mensaje
        if sending_msg == event:
            await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)
        else:
            await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)

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
                debug("[SEND /manage] ✅ Temporary converted file deleted after error")
            except Exception as cleanup_error:
                warning(f"[SEND /manage] ⚠️ Error deleting temporary converted file after error: {cleanup_error}")

        # Eliminar mensaje de progreso si existe
        if sending_msg and sending_msg != event:
            try:
                await safe_delete(sending_msg)
            except:
                pass

        # Mostrar error. Lleva la ruta: en un bloque de código, para que un
        # nombre como "[Factura](web.com).mp4" no se tome por un enlace
        if sending_msg == event:
            await safe_edit(event, get_text("error_sending_file", md_code(e)), parse_mode=PARSE_MODE)
        else:
            await safe_send_message(event.chat_id, get_text("error_sending_file", md_code(e)), parse_mode=PARSE_MODE, reply_to=topic)


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

    # El aviso de Renombrar es uno de esos mensajes: borrado, la petición se
    # quedaba esperando y el siguiente texto del usuario renombraba el fichero
    pending_renames.pop(user_id, None)

    # También eliminar el mensaje actual
    await safe_delete(event)


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

    filename = _display_name(os.path.basename(file_path))
    is_directory = os.path.isdir(file_path)

    if is_directory:
        icon = "📁"
    else:
        file_ext = os.path.splitext(filename)[1].lower()
        icon = get_file_icon(file_ext)

    # Pedir confirmación
    msg = f"{get_text('confirm_delete_title')}\n\n"
    msg += f"{get_text(_item_key('confirm_delete_question', is_directory))}\n\n"
    msg += f"{icon} {md_code(filename)}\n\n"
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


# Ids de /manage cuyo «Sí, eliminar» ya se ha atendido (o se está atendiendo),
# con el (chat, mensaje) del botón que lo hizo
deleted_file_ids = {}


async def handle_confirm_delete(event):
    """Elimina el archivo o carpeta confirmado"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    stats.count("btn_manage_delete")
    file_id = event.pattern_match.group(1).decode()
    # El mensaje que tiene el botón: en un CallbackQuery, .id es el de la consulta
    clicked = (event.chat_id, getattr(event, "message_id", None))

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        # Un doble toque en «Sí, eliminar»: el primero lo borra (o lo está
        # borrando) y deja en el mensaje el resultado con sus botones, que el
        # «no encontrado» del segundo tapaba. Solo en ese mismo mensaje: el id
        # de un fichero es el mismo en todos los listados, y la confirmación
        # de ese fichero en otro mensaje se quedaba preguntando sin respuesta
        if file_path is None and deleted_file_ids.get(file_id) == clicked:
            debug(f"[FILE_DELETE] {file_id} already deleted, ignoring repeated click")
            return
        await safe_edit(event, get_text("error_item_not_found_short"), parse_mode=PARSE_MODE)
        return

    filename = _display_name(os.path.basename(file_path))
    is_directory = os.path.isdir(file_path)
    item_type = "folder" if is_directory else "file"
    icon = "📁" if is_directory else "📄"

    # Se reclama antes de borrar: una carpeta se borra en otro hilo, y un
    # segundo toque mientras tanto no debe lanzar otro borrado
    pending_file_actions.pop(file_id, None)
    deleted_file_ids[file_id] = clicked

    try:
        if is_directory:
            # Eliminar carpeta y todo su contenido, en otro hilo: con muchos
            # ficheros (o en un volumen de red) el bot no atendía nada más
            await asyncio.to_thread(shutil.rmtree, file_path)
        else:
            # Eliminar archivo
            os.remove(file_path)

        msg = f"{get_text(_item_key('item_deleted_title', is_directory))}\n\n"
        msg += f"{icon} {md_code(filename)}\n\n"
        msg += get_text(_item_key('item_deleted_desc', is_directory))

        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

        debug(f"[FILE_DELETE] {item_type.capitalize()} deleted by user: {file_path}")
    except Exception as e:
        # No se ha borrado: el botón vuelve a valer para reintentarlo
        pending_file_actions[file_id] = file_path
        deleted_file_ids.pop(file_id, None)
        error(f"[FILE_DELETE] Error deleting {item_type} {file_path}: {e}")
        # El error lleva la ruta: en un bloque de código, para que un "__" del
        # nombre no se tome por cursiva
        await safe_edit(event, get_text(_item_key("error_deleting", is_directory), md_code(e)), parse_mode=PARSE_MODE)


async def handle_rename_file(event):
    """Inicia el proceso de renombrado de archivo o carpeta"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    stats.count("btn_manage_rename")
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_item_not_found_short"), parse_mode=PARSE_MODE)
        return

    filename = _display_name(os.path.basename(file_path))
    is_directory = os.path.isdir(file_path)
    icon = "📁" if is_directory else "📄"

    # Guardar en pending_renames para capturar el siguiente mensaje
    # Usamos una lista para permitir múltiples renombrados simultáneos
    # (pulsar dos veces Renombrar en el mismo elemento no lo pide dos veces)
    _forget_renames(event.sender_id, file_id)
    if event.sender_id not in pending_renames:
        pending_renames[event.sender_id] = []

    pending_renames[event.sender_id].append({
        "file_id": file_id,
        "file_path": file_path,
        "original_name": filename,
        "message": event,  # Guardar el mensaje para borrarlo después
        # En un CallbackQuery, .id es el de la consulta: el del mensaje con
        # el botón (al que contesta el usuario) es .message_id
        "message_id": getattr(event, "message_id", None),
        # La respuesta solo vale en este chat, y la que no lo cita solo
        # durante RENAME_TIMEOUT
        "chat_id": event.chat_id,
        "created": time.monotonic(),
        "is_directory": is_directory
    })

    msg = f"{get_text(_item_key('rename_title', is_directory))}\n\n"
    msg += f"{icon} {get_text('rename_current_name', md_code(filename))}\n\n"
    msg += get_text(_item_key('rename_reply_with_new_name', is_directory))
    if not is_directory:
        msg += get_text('rename_include_extension')
    msg += ".\n\n"
    msg += get_text('rename_example', 'my_new_folder' if is_directory else 'my_new_video.mp4')

    buttons = [[Button.inline(get_text("button_cancel"), data=f"fileact:{file_id}")]]

    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)


async def handle_rename_input(event):
    """Captura el nuevo nombre del archivo o carpeta y lo renombra"""
    if not is_admin(event.sender_id):
        return

    # Obtener los renombrados pendientes del usuario a los que puede contestar
    # este mensaje (ver _rename_requests)
    user_renames = _rename_requests(event)
    if not user_renames:
        return

    # Buscar el renombrado que corresponde al mensaje al que está respondiendo
    rename_data = None

    # Si el usuario está respondiendo a un mensaje, buscar ese mensaje específico
    if event.reply_to_msg_id:
        for data in user_renames:
            if data.get("message_id") == event.reply_to_msg_id:
                rename_data = data
                break

    # Si no está respondiendo o no se encontró, tomar el primero (FIFO)
    if rename_data is None:
        rename_data = user_renames[0]

    # Eliminar el renombrado de los pendientes (si no quedan más, también la
    # entrada del usuario)
    _forget_renames(event.sender_id, rename_data["file_id"])

    file_path = rename_data["file_path"]
    file_id = rename_data["file_id"]
    original_name = rename_data["original_name"]
    rename_message = rename_data.get("message")  # Mensaje de "Renombrar archivo/carpeta"
    is_directory = rename_data.get("is_directory", False)
    icon = "📁" if is_directory else "📄"
    # md_code separa con un espacio de ancho cero las comillas invertidas que
    # cerrarían el bloque: si el usuario copia el nombre actual del mensaje,
    # viaja con el texto y acababa, invisible, en el nombre del fichero
    new_name = event.raw_text.strip().replace("`\u200b", "`")

    # Lo que se conteste va a su tema del foro: el mensaje se borra ahora, y
    # una respuesta a un mensaje borrado salía en General
    topic = await get_topic(event)

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
    invalid = None
    # Con un punto al principio se renombraba bien, pero los listados se
    # saltan los ocultos y el fichero desaparecía del bot. Un salto de línea
    # (el nombre pegado en dos líneas) se quedaba dentro del nombre. Y con una
    # marca bidi, "Factura<U+202E>fdp.exe" se veía como "Facturaexe.pdf": se
    # rechazan los mismos invisibles que sanitize_filename quita de lo que llega
    if (not new_name or '/' in new_name or '\\' in new_name or new_name.startswith('.')
            or has_invisible_chars(new_name)):
        invalid = 'rename_invalid_chars'
    # El sistema de ficheros limita el nombre en bytes, y su error repetía las
    # dos rutas: con un texto largo pegado por error, el aviso pasaba del
    # límite de Telegram y no llegaba
    elif len(new_name.encode('utf-8')) > MAX_FILENAME_BYTES:
        invalid = 'rename_too_long'
    if invalid:
        msg = f"{get_text('rename_invalid_title')}\n\n"
        msg += f"{get_text(invalid)}\n\n"
        msg += get_text('rename_try_again')
        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)
        return

    # Verificar que el elemento aún existe
    if not os.path.exists(file_path):
        await safe_send_message(event.chat_id, get_text("error_folder_gone" if is_directory else "error_file_gone"),
                                parse_mode=PARSE_MODE, reply_to=topic)
        return

    # Construir nueva ruta
    directory = os.path.dirname(file_path)
    new_path = os.path.join(directory, new_name)

    # Verificar si ya existe un elemento con ese nombre
    if os.path.exists(new_path):
        # Lo que ya existe puede ser de otro tipo (un fichero que se llama
        # como una carpeta), así que el texto no dice cuál
        msg = f"{get_text('rename_already_exists_title')}\n\n"
        msg += f"{get_text('rename_already_exists_desc', md_code(new_name))}\n\n"
        msg += get_text('rename_choose_another')
        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)
        return

    try:
        # Renombrar el archivo o carpeta
        os.rename(file_path, new_path)

        # Actualizar en pending_file_actions
        pending_file_actions[file_id] = new_path

        msg = f"{get_text(_item_key('rename_success_title', is_directory))}\n\n"
        msg += f"{icon} {get_text('rename_old_name', md_code(original_name))}\n"
        msg += f"{icon} {get_text('rename_new_name', md_code(new_name))}\n\n"
        msg += get_text(_item_key('rename_success_desc', is_directory))

        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)

        debug(f"[FILE_RENAME] File renamed: {file_path} → {new_path}")
    except Exception as e:
        error(f"[FILE_RENAME] Error renaming file {file_path}: {e}")
        msg = f"{get_text('rename_error_title')}\n\n"
        # Sin las rutas que lleva el error del sistema: con un nombre largo
        # el aviso pasaba del límite de Telegram
        msg += get_text('rename_error_desc', md_code(getattr(e, 'strerror', None) or e))
        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_send_message(event.chat_id, msg, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)


async def handle_extract_file(event):
    """Descomprime un archivo comprimido"""
    if await check_admin_and_warn(event):
        return

    await safe_answer(event)
    stats.count("btn_manage_extract")
    file_id = event.pattern_match.group(1).decode()

    file_path = pending_file_actions.get(file_id)
    if not file_path or not os.path.exists(file_path):
        await safe_edit(event, get_text("error_file_not_found"), parse_mode=PARSE_MODE)
        return

    # Verificar que sea un archivo comprimido
    if not is_compressed_file(file_path):
        await safe_edit(event, get_text("error_not_compressed"), parse_mode=PARSE_MODE)
        return

    # Verificar si es un ZIP split (no soportado). Con la ruta completa: con
    # el nombre suelto buscaba las partes en el directorio de trabajo
    if is_split_zip(file_path):
        await safe_edit(event, get_text("error_split_zip_not_supported"), parse_mode=PARSE_MODE)
        return

    # Determinar la carpeta de extracción
    download_path = os.path.dirname(file_path)
    extracted_path = os.path.join(download_path, extraction_folder_name(file_path))

    # Verificar si la carpeta de destino ya existe
    if os.path.exists(extracted_path):
        await safe_edit(event, get_text("error_extraction_folder_exists", _display_name(os.path.basename(extracted_path))), parse_mode=PARSE_MODE)
        return

    # O si ya va otra extracción camino de esa carpeta: una segunda pulsación
    # de Descomprimir, u otro comprimido que se extrae con el mismo nombre
    if extracted_path in extractions_in_progress:
        await safe_edit(event, get_text("error_extraction_in_progress", _display_name(os.path.basename(extracted_path))), parse_mode=PARSE_MODE)
        return

    # Sin ningún await entre las comprobaciones y el registro: dos pulsaciones
    # seguidas no pueden pasar las dos
    extractions_in_progress.add(extracted_path)

    async def extract():
        try:
            await _extract_and_report(event, file_id, file_path, extracted_path)
        finally:
            extractions_in_progress.discard(extracted_path)

    # Fuera del handler, como el envío: con un corte de Telegram, la
    # extracción seguía, pero nadie la acababa ni contaba cómo había ido
    await outlive_handler(extract())


def _extraction_work_path(extracted_path):
    """La carpeta oculta en la que se descomprime lo que acabará en `extracted_path`.

    Siempre la misma para un mismo destino: así, al repetir una extracción, se
    limpia lo que dejó la que se cortó. Lleva el id del bot y el sufijo, y con
    un nombre largo no cabía en los 255 bytes de un nombre: os.makedirs fallaba
    y la ficha se quedaba en «Descomprimiendo…». Entonces se recorta, y un trozo
    del hash del nombre entero evita que dos destinos que empiezan igual
    compartan carpeta, y que al empezar una extracción se borre la de la otra.
    """
    folder, name = os.path.split(extracted_path)
    work = partial_name(f"{name}{EXTRACTING_SUFFIX}")
    if len(os.fsencode(work)) > MAX_FILENAME_BYTES:
        raw = os.fsencode(name)
        tail = f".{hashlib.sha1(raw).hexdigest()[:8]}{EXTRACTING_SUFFIX}"
        room = MAX_FILENAME_BYTES - len(partial_name(tail).encode())
        # Sobre los bytes y sin dejar un carácter partido (ni lo que no sea
        # UTF-8, que en un nombre oculto no hace falta)
        work = partial_name(raw[:room].decode("utf-8", "ignore") + tail)
    return os.path.join(folder, work)


async def _extract_and_report(event, file_id, file_path, extracted_path):
    """Extrae `file_path` en `extracted_path` y cuenta el resultado en `event`."""
    filename = _display_name(os.path.basename(file_path))
    # Con extract.after=DELETE el comprimido se borra al acabar, por su ruta:
    # si durante la extracción se renombra y otro fichero ocupa su nombre, se
    # borraría ese otro sin haberlo extraído
    archive_stat = os.stat(file_path)
    # Se extrae en una carpeta oculta junto a la de destino, y solo pasa a
    # llamarse como toca al acabar bien. Mientras dura, /list y /manage no la
    # muestran: no se puede borrar ni renombrar a medias
    work_path = _extraction_work_path(extracted_path)

    # Mostrar mensaje de progreso inicial. Si ya no se puede (la ficha se cerró
    # justo antes de pulsar), no se empieza: no quedaría dónde contar cómo acaba
    try:
        progress_msg = await safe_edit(event, get_text("decompressing_file", filename), parse_mode=PARSE_MODE, wait_for_result=True)
    except Exception as e:
        warning(f"[EXTRACT] Could not show the progress of {filename}, not extracting: {e}")
        return

    try:
        # Lo que dejara una extracción que no llegó a terminar (un reinicio a
        # mitad) es nuestro, y unrar no sobrescribe lo que encuentra
        shutil.rmtree(work_path, ignore_errors=True)
        os.makedirs(work_path)
        # Abierta hasta acabar, para saber entonces si sigue siendo esta: si alguien
        # la borra a mitad, lo que se extrae después la vuelve a crear sin lo de
        # antes. Mientras esté abierta, su inodo no se reutiliza
        work_fd = os.open(work_path, os.O_RDONLY)
    except OSError as e:
        # Sin carpeta en la que descomprimir (sin permisos, o una que no se
        # pudo borrar), se cuenta como un fallo: la excepción dejaba la ficha
        # en «Descomprimiendo…» para siempre
        warning(f"[EXTRACT] Could not create {work_path}, not extracting: {e}")
        shutil.rmtree(work_path, ignore_errors=True)
        msg, buttons = get_extraction_message_and_buttons(False, filename, _display_name(extracted_path), file_id)
        await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)
        return

    # Cerrada al grupo y a los demás mientras se extrae: unrar deja a cada
    # fichero los permisos que trae el RAR, también un setuid, y no se le
    # quitan hasta que termina. Al acabar bien vuelve a tener los de siempre
    work_mode = stat.S_IMODE(os.fstat(work_fd).st_mode)
    _set_folder_mode(work_fd, work_mode & ~0o077)

    debug(f"[EXTRACT] Starting extraction of {filename} to {extracted_path}")

    # Extraer archivo en un executor para no bloquear (archivos grandes pueden tardar mucho)
    loop = asyncio.get_running_loop()

    # Tarea para actualizar progreso cada 10 segundos
    extraction_done = asyncio.Event()

    async def update_progress():
        """Actualiza el mensaje cada 10 segundos para mostrar que sigue extrayendo"""
        elapsed = 0
        while not extraction_done.is_set():
            # Esperando al evento y no con sleep(10): con un sleep, el resultado
            # de una extracción de una décima tardaba 10 segundos en llegar
            try:
                await asyncio.wait_for(extraction_done.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
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
        extract_result = await loop.run_in_executor(None, extract_file, file_path, work_path)
        debug(f"[EXTRACT] Extraction completed for {filename}, result: {extract_result}")
        # Incompleta: no se puede dar por buena ni, con "Borrar", llevarse el comprimido
        if extract_result == True and not _is_same_folder(work_path, work_fd):
            warning(f"[EXTRACT] {work_path} was removed while extracting: discarding the extraction")
            extract_result = False
        if extract_result == True:
            _set_folder_mode(work_fd, work_mode)
    finally:
        os.close(work_fd)
        # Detener tarea de progreso
        extraction_done.set()
        await progress_task

    if extract_result != True:
        # Lo que quedara de la extracción fallida está oculto: nadie lo borraría
        shutil.rmtree(work_path, ignore_errors=True)
    elif not _rename_extraction(work_path, extracted_path):
        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data=f"fileact:{file_id}"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        await safe_edit(event, get_text("error_extraction_folder_exists", _display_name(os.path.basename(extracted_path))),
                        buttons=buttons, parse_mode=PARSE_MODE)
        return

    # Usar función unificada para mensajes y botones
    msg, buttons = get_extraction_message_and_buttons(extract_result, filename, _display_name(extracted_path), file_id)

    # Con un "qué hacer después" fijo en /settings, se hace sin preguntar
    after = settings.extract_after()
    if extract_result == True and after != "ASK":
        msg = msg.replace(get_text("extraction_ask_delete"), "").rstrip()
        if after == "DELETE":
            msg += "\n\n" + delete_compressed(file_path, archive_stat)
            pending_file_actions.pop(file_id, None)
        else:
            msg += "\n\n" + kept_message(filename)
        buttons = [[
            Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
            Button.inline(get_text("button_close"), data="close")
        ]]
        debug(f"[EXTRACT] extract after={after} for {filename}")

    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)

    if extract_result == True:
        debug(f"[EXTRACT] File {filename} - Extracted to {extracted_path}")


def _set_folder_mode(fd, mode):
    """Cambia los permisos de la carpeta abierta en `fd`, si el disco lo deja.

    En FAT y exFAT no hay permisos que cambiar (ni setuid del que protegerse),
    y según cómo estén montados chmod falla: se sigue sin cambiarlos.
    """
    try:
        os.fchmod(fd, mode)
    except OSError as e:
        debug(f"[EXTRACT] Could not set the permissions of the extraction folder to {mode:o}: {e}")


def _is_same_folder(path, fd):
    """True si `path` sigue siendo la carpeta abierta en `fd`."""
    try:
        return os.path.samestat(os.stat(path), os.fstat(fd))
    except OSError:
        return False


def _rename_extraction(work_path, extracted_path):
    """Da a la extracción terminada su nombre definitivo, si sigue libre.

    Si mientras tanto ha aparecido algo con ese nombre no se pisa ni se mezcla:
    lo extraído se descarta y el comprimido sigue ahí para repetirlo. Se mira
    antes porque os.rename sustituiría sin avisar una carpeta vacía.
    """
    if os.path.lexists(extracted_path):
        warning(f"[EXTRACT] {extracted_path} appeared while extracting: discarding the extraction")
    else:
        try:
            os.rename(work_path, extracted_path)
            return True
        except OSError as e:
            warning(f"[EXTRACT] Could not rename {work_path} to {extracted_path}: {e}")
    shutil.rmtree(work_path, ignore_errors=True)
    return False


def kept_message(filename):
    return f"{get_text('file_kept_title')}\n\n📄 {md_code(filename)}\n\n{get_text('file_kept_desc')}"


# Las partes de un RAR que se nombran al borrarlo. Con todas, un juego de 100
# volúmenes con nombres largos pasaba de los 4096 caracteres de Telegram: el
# aviso no llegaba y el mensaje se quedaba en «Descomprimiendo…» o con los
# botones de algo ya borrado. Diez, aun con el nombre más largo y la cabecera
# del éxito de la extracción delante, caben
MAX_LISTED_PARTS = 10


def delete_compressed(file_path, expected_stat=None):
    """Borra un comprimido (todas sus partes, si es un RAR multiparte) y
    devuelve el mensaje que lo cuenta.

    Con `expected_stat` (un os.stat del comprimido) solo se borra si en esa
    ruta sigue ese mismo fichero."""
    filename = _display_name(os.path.basename(file_path))
    try:
        if expected_stat is not None and not os.path.samestat(os.stat(file_path), expected_stat):
            warning(f"[FILE_DELETE] {file_path} was replaced by another file, not deleting it")
            return f"{get_text('delete_error_title')}\n\n" + get_text('delete_error_replaced', filename)

        parts = _rar_volumes(file_path) if rarfile.is_rarfile(file_path) else [file_path]
        if len(parts) > 1:
            for part in parts:
                os.remove(part)
                debug(f"[FILE_DELETE] File {part} - Deleted")

            msg = f"{get_text('files_deleted_title')}\n\n"
            msg += get_text('files_deleted_count', len(parts))
            # Con sus nombres, para que se vea qué se ha llevado. Solo los
            # primeros (ver MAX_LISTED_PARTS)
            names = [f"📄 {md_code(_display_name(os.path.basename(part)))}" for part in parts[:MAX_LISTED_PARTS]]
            if len(parts) > MAX_LISTED_PARTS:
                names.append(get_text('files_deleted_more', len(parts) - MAX_LISTED_PARTS))
            msg += "\n\n" + "\n".join(names)
        else:
            os.remove(file_path)
            msg = f"{get_text('file_deleted_title')}\n\n"
            msg += f"📄 {md_code(filename)}"
            debug(f"[FILE_DELETE] File {file_path} - Deleted")
    except Exception as e:
        error(f"[FILE_DELETE] Error deleting compressed file {file_path}: {e}")
        msg = f"{get_text('delete_error_title')}\n\n"
        msg += get_text('delete_error_desc', md_code(e))
    return msg


def _rar_volumes(file_path):
    """Los volúmenes que hay en disco del RAR al que pertenece `file_path`.

    Son los que encadena el propio archivo, no los que se llamen parecido: con
    "serie.rar" y "serie.part1.rar" en la misma carpeta, borrar uno no se lleva
    el otro. Desde una parte que no es la primera, que rarfile no abre, se
    buscan por nombre, pero solo las del mismo esquema (base.partN.rar, o
    base.rar y base.rNN) y con la misma base: las mayúsculas solo dan igual en
    la extensión.
    """
    try:
        with rarfile.RarFile(file_path) as rar_ref:
            return [part for part in rar_ref.volumelist() if os.path.isfile(part)]
    except (rarfile.Error, OSError):
        pass

    dirname, filename = os.path.split(file_path)
    m = re.fullmatch(r"(.*)\.part\d+\.rar", filename, re.IGNORECASE)
    if m:
        same_set = re.compile(re.escape(m.group(1)) + r"\.(?i:part\d+\.rar)")
    else:
        m = re.fullmatch(r"(.*)\.(?:rar|r\d{2,})", filename, re.IGNORECASE)
        if not m:
            return [file_path]
        same_set = re.compile(re.escape(m.group(1)) + r"\.(?i:rar|r\d{2,})")
    return [os.path.join(dirname, f) for f in sorted(os.listdir(dirname))
            if same_set.fullmatch(f) and os.path.isfile(os.path.join(dirname, f))]


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

    filename = _display_name(os.path.basename(file_path))

    if action == "delcompressed":
        msg = delete_compressed(file_path)
        pending_file_actions.pop(file_id, None)
    else:  # keepcompressed
        msg = kept_message(filename)

    buttons = [[
        Button.inline(get_text("button_back_to_manage"), data="managecat:all"),
        Button.inline(get_text("button_close"), data="close")
    ]]
    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)
