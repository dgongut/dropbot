"""Lo que se hace con una descarga cuando acaba de llegar a su carpeta.

Dos tiempos, los dos llamados desde handle_success de dropbot.py:

`arrange` va antes del aviso de «Descarga completada» y hace lo que no pide
permiso, según los ajustes: renombrar con la sugerencia (ver name_parser) y
mandar un episodio directo a la carpeta donde ya está su serie. Devuelve la
ruta en la que ha quedado el fichero, que es con la que sigue todo lo demás
(el aviso, el envío a Telegram).

`offer` va después y es lo que sí pregunta, en un mensaje con botones: mover el
episodio a la carpeta de su serie, deshacer el movimiento que se hizo solo,
sustituir una versión anterior que ya tiene el nombre que le tocaba, o elegir
carpeta si está activado preguntarlo siempre.

Nada de esto puede tumbar una descarga que ya ha terminado bien: cualquier
fallo se queda en el log y el fichero, donde estaba.

Los botones llevan `arr:<acción>:<id>`.
"""

import asyncio
import os
import shutil
from collections import namedtuple

from telethon import Button, events

import fileops
import folders
import name_parser
import settings
import stats
from basic import md_code
from config import EXTENSIONS_TORRENT
from handlers import picker
from logger import debug, warning
from state import arrivals, claim, repoint, short_id, was_claimed
from translations import PARSE_MODE, get_text
from utils.file_helpers import format_file_size
from utils.telegram_helpers import (
    check_admin_and_warn, get_topic, outlive_handler, safe_answer, safe_delete, safe_edit,
    safe_send_message,
)

# path: dónde ha quedado. notes: líneas para el aviso de descarga completada.
# conflict: el nombre que le tocaba y ya tiene otro fichero. undo: la carpeta
# de la que se movió solo. candidates: carpetas de su serie que proponer
Arrangement = namedtuple("Arrangement", "path notes conflict undo candidates")


def init(bot):
    bot.on(events.CallbackQuery(pattern=b"arr:(.+)"))(handle_arrival_callback)
    picker.register("place", _place_chosen, picker.close_message)


def _is_torrent(path):
    # Un .torrent lo recoge el gestor de torrents en cuanto aparece: no se toca
    return os.path.splitext(path)[1].lower() in EXTENSIONS_TORRENT


def _auto_rename(path, notes):
    """Renombra con la sugerencia. Devuelve (ruta, nombre que no se pudo usar)."""
    name = os.path.basename(path)
    suggested = fileops.suggest_name(name) if fileops.is_video(name) else None
    if not suggested:
        return path, None
    target = os.path.join(os.path.dirname(path), suggested)
    if os.path.lexists(target):
        if not settings.auto_replace() or not _replaceable(target, path):
            return path, suggested
        os.remove(target)
        notes.append(get_text("arrival_replaced", suggested))
    new_path, _ = fileops.rename_item(path, suggested)
    repoint(path, new_path)
    notes.append(get_text("arrival_renamed", name))
    debug(f"[ARRIVAL] Renamed {path} -> {new_path}")
    return new_path, None


def _replaceable(target, path):
    """Solo se sustituye un fichero normal que no sea el mismo que llega."""
    try:
        return os.path.isfile(target) and not os.path.islink(target) and not os.path.samefile(target, path)
    except OSError:
        return False


def _arrange(path):
    """La parte bloqueante de arrange."""
    notes = []
    conflict = undo = None
    candidates = []
    if settings.auto_rename():
        try:
            path, conflict = _auto_rename(path, notes)
        except OSError as e:
            warning(f"[ARRIVAL] Could not rename {path}: {e}")
    if settings.series_direct() or settings.series_suggest():
        try:
            found = fileops.series_candidates(path)
        except OSError as e:
            warning(f"[ARRIVAL] Could not look for the series folder of {path}: {e}")
            found = []
        # Directo solo si no hay ninguna duda: una única carpeta con el nombre
        # de la serie. Con un nombre pendiente de sustituir, tampoco: moverlo
        # lo alejaría del fichero al que sustituye
        direct = name_parser.series_auto_dir(found) if settings.series_direct() and not conflict else None
        if direct:
            try:
                previous = os.path.dirname(path)
                new_path = fileops.move_item(path, folders.check(direct, create=True))
                repoint(path, new_path)
                # Sin nota en «Descarga completada»: lo cuenta el aviso que
                # viene detrás con el botón de deshacerlo, y salía dos veces
                path, undo = new_path, previous
                debug(f"[ARRIVAL] Sent to the folder of its series: {new_path}")
            except (fileops.MoveError, folders.FolderError, OSError) as e:
                warning(f"[ARRIVAL] Could not move {path} to {direct}: {e}")
                direct = None
        if not direct and settings.series_suggest():
            candidates = [directory for directory, _ in found][:fileops.SERIES_SUGGESTIONS_MAX]
    return Arrangement(path, notes, conflict, undo, candidates)


async def arrange(file_path):
    """Lo automático (ver el módulo). Nunca lanza: si algo falla, el fichero
    se queda como estaba."""
    nothing = Arrangement(file_path, [], None, None, [])
    try:
        if _is_torrent(file_path) or not os.path.isfile(file_path):
            return nothing
        return await asyncio.to_thread(_arrange, file_path)
    except Exception as e:
        warning(f"[ARRIVAL] Could not arrange {file_path}: {e}")
        return nothing


async def offer(event, arrangement):
    """Lo que se pregunta (ver el módulo). Nunca lanza."""
    try:
        await _offer(event, arrangement)
    except Exception as e:
        warning(f"[ARRIVAL] Could not offer what to do with {arrangement.path}: {e}")


async def _offer(event, arrangement):
    path = arrangement.path
    if _is_torrent(path) or not os.path.isfile(path):
        return
    name = os.path.basename(path)
    topic = await get_topic(event)

    lines = []
    buttons = []
    arrival_id = short_id("a")
    if arrangement.conflict:
        lines.append(get_text("arrival_conflict", arrangement.conflict))
        buttons.append([Button.inline(get_text("arrival_button_replace"), data=f"arr:rep:{arrival_id}"),
                        Button.inline(get_text("arrival_button_keep_both"), data=f"arr:x:{arrival_id}")])
    elif arrangement.undo:
        buttons.append([Button.inline(get_text("arrival_button_undo"), data=f"arr:undo:{arrival_id}"),
                        Button.inline(get_text("button_close"), data=f"arr:x:{arrival_id}")])
        lines.append(get_text("arrival_series_placed", os.path.dirname(path)))
    elif settings.ask_folder():
        # Preguntar siempre: el selector, con la carpeta de la serie delante
        picker_id = picker.open_picker(
            "place", get_text("arrival_ask_title", name), payload={"path": path},
            suggested=arrangement.candidates, exclude=os.path.dirname(path))
        await picker.send(event.chat_id, picker_id, topic)
        return
    elif arrangement.candidates:
        identity = name_parser.series_identity(name) or {}
        if identity.get("title"):
            lines.append(get_text("arrival_series_found", identity["title"]))
        else:
            # No lleva el nombre de su serie: la carpeta sale de la numeración
            # (ver fileops._numbered_candidates) y es una suposición. Se dice
            # así, sin afirmar que es de esa serie
            lines.append(get_text("arrival_series_guess"))
        for index, directory in enumerate(arrangement.candidates):
            buttons.append([Button.inline(get_text("arrival_button_move", picker.short_path(directory, 28)),
                                          data=f"arr:mv:{arrival_id}:{index}")])
            lines.append(f"📂 {md_code(directory)}")
        buttons.append([Button.inline(get_text("arrival_button_stay"), data=f"arr:x:{arrival_id}")])
    if not buttons:
        return
    arrivals[arrival_id] = {
        "path": path,
        "conflict": arrangement.conflict,
        "undo": arrangement.undo,
        "candidates": arrangement.candidates,
    }
    text = "\n".join([get_text("arrival_title", name), ""] + lines)
    await safe_send_message(event.chat_id, text, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)


def low_space_note(directory, size):
    """El aviso de que lo que llega puede no caber en `directory`, o None."""
    if not size or not settings.low_space_warning():
        return None
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return None
    if size <= free:
        return None
    return get_text("low_space_warning", format_file_size(size), format_file_size(free), directory)


# ---------------------------------------------------------------------------
# Botones
# ---------------------------------------------------------------------------

async def handle_arrival_callback(event):
    if await check_admin_and_warn(event):
        return
    parts = event.pattern_match.group(1).decode(errors="replace").split(":")
    action = parts[0]
    arrival_id = parts[1] if len(parts) > 1 else ""
    # Se reclama sin ningún await por medio: un doble toque no mueve dos veces
    arrival = claim(arrivals, arrival_id)
    if arrival is None:
        # Un segundo toque no dice nada; un botón de antes de reiniciar el
        # bot, que ha caducado: antes se quedaba mudo
        if was_claimed(arrivals, arrival_id):
            await safe_answer(event)
        else:
            await safe_answer(event, get_text("picker_expired"), alert=True)
        return
    await safe_answer(event)
    if action == "x":
        await safe_delete(event)
        return
    path = arrival["path"]
    if not os.path.isfile(path):
        await safe_edit(event, get_text("error_file_does_not_exist_user"), parse_mode=PARSE_MODE)
        return

    if action == "rep" and arrival["conflict"]:
        stats.count("btn_arrival_replace")
        await _replace(event, path, arrival["conflict"])
    elif action == "undo" and arrival["undo"]:
        stats.count("btn_arrival_undo")
        await outlive_handler(_move(event, path, arrival["undo"], "arrival_undone"))
    elif action == "mv" and len(parts) > 2 and parts[2].isdigit() and int(parts[2]) < len(arrival["candidates"]):
        stats.count("btn_arrival_move")
        await outlive_handler(_move(event, path, arrival["candidates"][int(parts[2])], "move_done"))


def _replace_previous(path, new_name):
    target = os.path.join(os.path.dirname(path), new_name)
    if os.path.lexists(target):
        if not _replaceable(target, path):
            raise FileExistsError(new_name)
        os.remove(target)
    new_path, _ = fileops.rename_item(path, new_name)
    repoint(path, new_path)
    return new_path


async def _replace(event, path, new_name):
    try:
        await asyncio.to_thread(_replace_previous, path, new_name)
    except OSError as e:
        warning(f"[ARRIVAL] Could not replace {new_name}: {e}")
        text = f"{get_text('rename_error_title')}\n\n{get_text('rename_error_desc', md_code(e.strerror or e))}"
        await safe_edit(event, text, parse_mode=PARSE_MODE)
        return
    await safe_edit(event, get_text("arrival_replace_done", new_name), parse_mode=PARSE_MODE)


async def _move(event, path, target, done_key):
    name = os.path.basename(path)
    try:
        directory = await asyncio.to_thread(folders.check, target, True)
        new_path = await asyncio.to_thread(fileops.move_item, path, directory)
    except folders.FolderError as e:
        await safe_edit(event, get_text(f"folder_error_{e.code}"), parse_mode=PARSE_MODE)
        return
    except (fileops.MoveError, OSError) as e:
        warning(f"[ARRIVAL] Could not move {path} to {target}: {e}")
        if isinstance(e, fileops.MoveError):
            reason = get_text(f"move_error_{e.code}", e.detail or name)
        else:
            reason = get_text("move_error_os", e.strerror or e)
        await safe_edit(event, f"{get_text('move_error_title')}\n\n{reason}", parse_mode=PARSE_MODE)
        return
    repoint(path, new_path)
    await safe_edit(event, get_text(done_key, name, directory), parse_mode=PARSE_MODE)


async def _place_chosen(event, chosen, target):
    path = chosen["payload"]["path"]
    if not os.path.isfile(path):
        await safe_edit(event, get_text("error_file_does_not_exist_user"), parse_mode=PARSE_MODE)
        return
    stats.count("btn_arrival_place")
    await safe_edit(event, get_text("moving", os.path.basename(path)), parse_mode=PARSE_MODE)
    await outlive_handler(_move(event, path, target, "move_done"))
