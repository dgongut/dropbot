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
import os
import re
import shutil

import rarfile
from telethon import Button, events
from telethon.tl.types import DocumentAttributeFilename

from basic import (
    clean_rar_base_name, is_admin, is_compressed_file, is_split_zip,
)
from config import (
    AUD_ICO, BOO_ICO, DOWNLOAD_PATH, DOWNLOAD_PATHS,
    EXTENSIONS_AUDIO, EXTENSIONS_VIDEO, FILTER_AUDIO, FILTER_EBOOK,
    FILTER_PHOTO, FILTER_TORRENT, FILTER_URL_AUDIO, FILTER_URL_VIDEO,
    FILTER_VIDEO, IMG_ICO, MAX_TELEGRAM_FILE_SIZE, TOR_ICO,
    VID_ICO,
)
from logger import debug, error, warning
from services.extraction_service import extract_file
from services.video_service import generate_video_thumbnail, get_video_metadata
from state import (
    list_messages, pending_file_actions, pending_renames,
)
from translations import PARSE_MODE, get_text
from utils.file_helpers import format_file_size, get_directory_size, get_file_icon
from utils.telegram_helpers import (
    check_admin_and_warn, safe_answer, safe_delete, safe_edit, safe_reply,
    safe_send_message,
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
        (events.NewMessage(func=lambda e: e.sender_id in pending_renames and len(pending_renames.get(e.sender_id, [])) > 0 and not e.raw_text.startswith('/')), handle_rename_input),
        (events.CallbackQuery(pattern=b"extract:(.+)"), handle_extract_file),
        (events.CallbackQuery(pattern=b"(delcompressed|keepcompressed):(.+)"), handle_compressed_file_action),
    ]


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


async def handle_list_category(event):
    """Maneja los botones de categorías en /list"""
    debug("[LIST] handle_list_category called")

    if await check_admin_and_warn(event):
        debug("[LIST] User is not admin, returning")
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
        header = "📂 **Archivos en el servidor**\n\n"

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
        if file_size_bytes < MAX_TELEGRAM_FILE_SIZE:
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
    if file_size_bytes >= MAX_TELEGRAM_FILE_SIZE:
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
            debug("[SEND /manage] Starting video conversion...")
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
            upload_progress
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
                debug("[SEND /manage] ✅ Temporary converted file deleted after error")
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
    msg += ".\n\n"
    msg += get_text('rename_example', 'my_new_folder' if is_directory else 'my_new_video.mp4')

    buttons = [[Button.inline(get_text("button_cancel"), data=f"fileact:{file_id}")]]

    await safe_edit(event, msg, buttons=buttons, parse_mode=PARSE_MODE)


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
    loop = asyncio.get_running_loop()

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
