"""Lo que /manage hace con carpetas y con varios elementos a la vez, y /find.

Entrar en una carpeta y moverse por ella, mover un elemento a otra carpeta,
renombrarlo con la sugerencia, renombrar de una vez todo lo que lleva una
carpeta (con vista previa) y las acciones sobre todo un listado: moverlo,
renombrarlo o borrarlo entero.

Todos los botones llevan `fm:` delante y los atiende un solo handler:

    fm:r                   el explorador: las carpetas por las que se entra
    fm:o:<id>[:<página>]   abre una carpeta
    fm:nf:<id>             crea una carpeta dentro de esa (pide el nombre)
    fm:mv:<id>             mover un elemento (abre el selector de carpeta)
    fm:rs:<id>             renombrar con la sugerencia
    fm:ra:<id>             renombrar una carpeta y todo lo que lleva: vista previa
    fm:ma:<listado>        acciones sobre todo un listado
    fm:mm:<listado>        moverlo todo        fm:mr:<listado>  renombrarlo todo
    fm:md:<listado>        borrarlo todo       fm:mdc:<listado> sí, borrarlo
    fm:rc:<clave>          aplicar el renombrado que se enseñó
    fm:f:<búsqueda>[:<página>]  resultados de /find

Un listado es `q.<búsqueda>` (los resultados de un /find) o el id de una carpeta.

Lo que se confirma es lo que se enseñó: antes de borrar, mover o renombrar
varios elementos se guarda la lista, y la confirmación actúa sobre esa lista y
no sobre lo que haya en la carpeta al pulsar.
"""

import asyncio
import os
import shutil

from telethon import Button, events

import fileops
import folders
import settings
import stats
from basic import MAX_FILENAME_BYTES, has_invisible_chars, md_code
from handlers import inputs, manage, picker
from handlers.navigation import MANAGE_ROOT, exit_row as home_exit_row, home_button
from logger import debug, error, warning
from state import (
    claim, confirmations, find_queries, manage_trails, pending_file_actions, repoint, short_id, unclaim,
    was_claimed,
)
from translations import PARSE_MODE, get_text
from utils.file_helpers import format_file_size
from utils.telegram_helpers import (
    check_admin_and_warn, outlive_handler, safe_answer, safe_edit, safe_send_message,
)

# Líneas de la vista previa de un renombrado, y de los fallos de una acción
PREVIEW_LINES = 10
FAILURE_LINES = 5
# Lo que se enseña de un nombre en esas líneas
PREVIEW_NAME_LENGTH = 60


def init(bot):
    bot.on(events.CallbackQuery(pattern=b"fm:(.+)"))(handle_files_callback)
    inputs.register("manage_new_folder", handle_new_folder_name)
    picker.register("move", _move_chosen, _move_cancelled)
    picker.register("massmove", _mass_move_chosen, _mass_move_cancelled)


async def handle_files_callback(event):
    if await check_admin_and_warn(event):
        return
    action, _, rest = event.pattern_match.group(1).decode(errors="replace").partition(":")
    handler = ACTIONS.get(action)
    # Las confirmaciones contestan ellas: si han caducado, con un aviso
    if action not in SELF_ANSWERED:
        await safe_answer(event)
    if handler is None:
        warning(f"[MANAGE] Unknown action fm:{action}")
        return
    await handler(event, rest)


def _navigation(back):
    return [Button.inline(get_text("button_back_to_manage"), data=back),
            Button.inline(get_text("button_close"), data="close")]


def _short(name):
    name = manage._display_name(name)
    return name if len(name) <= PREVIEW_NAME_LENGTH else name[:PREVIEW_NAME_LENGTH - 1] + "…"


def _path_of(file_id):
    """La ruta de un botón de /manage, si lo que señala sigue existiendo."""
    path = pending_file_actions.get(file_id)
    return path if path and os.path.lexists(path) else None


async def _not_found(event):
    await safe_edit(event, get_text("error_item_not_found"), buttons=manage._exit_row(), parse_mode=PARSE_MODE)


async def _claim_confirmation(event, key):
    """Lo que se enseñó antes de confirmar, o None si ya no vale.

    Contesta al botón: sin nada si se atiende ahora o es el segundo toque de
    un doble toque, y con un aviso si es de antes de reiniciar el bot, que
    antes se quedaba sin hacer nada ni decir por qué.
    """
    key = (event.sender_id, key)
    data = claim(confirmations, key)
    if data is None and not was_claimed(confirmations, key):
        await safe_answer(event, get_text("picker_expired"), alert=True)
    else:
        await safe_answer(event)
    return data


# ---------------------------------------------------------------------------
# Abrir una carpeta
# ---------------------------------------------------------------------------

async def open_folder(event, rest):
    file_id, _, page = rest.partition(":")
    path = _path_of(file_id)
    if path is None or not os.path.isdir(path):
        await _not_found(event)
        return
    # Se sale de lo que hubiera a medias (un renombrado, una ruta por escribir)
    manage._forget_renames(event.sender_id, file_id)
    inputs.forget(event.sender_id)

    try:
        files_info, total_size, _, _ = await asyncio.to_thread(manage._scan_directories, [path], "FILE_MANAGE")
    except OSError as e:
        error(f"[FILE_MANAGE] Cannot read {path}: {e}")
        await safe_edit(event, get_text("error_manage_files"), buttons=[_navigation(manage.nav_back(event, path))],
                        parse_mode=PARSE_MODE)
        return
    files_info.sort(key=lambda item: item["name"].lower())
    page = int(page) if page.isdigit() else 0
    # De aquí en adelante, «Volver» desde lo que se abra regresa a esta
    # carpeta y a esta página, y «Subir» a aquello desde lo que se entró
    manage.nav_visit(event, f"fm:o:{file_id}:{page}", folder_id=file_id)
    text, buttons = _folder_view(file_id, path, files_info, total_size, page, manage.nav_up(event, path))
    await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE)


def _folder_view(file_id, path, files_info, total_size, page, up):
    """El texto y los botones de una carpeta abierta. `up` es a dónde sube."""

    title = get_text("folder_view_title", path)
    navigation = []
    row = []
    # Una carpeta de descarga o favorita no se renombra, mueve ni elimina
    # desde aquí: es donde el bot guarda, y se cambia en /settings
    if not folders.is_root(path):
        row.append(Button.inline(get_text("manage_button_this_folder"), data=f"fileact:{file_id}"))
    # Crear una subcarpeta aquí mismo, sin pasar por «Mover» ni por los ajustes
    if folders.is_allowed(path):
        row.append(Button.inline(get_text("button_picker_new"), data=f"fm:nf:{file_id}"))
    if row:
        navigation.append(row)
    # Desde la única carpeta por la que se entra no hay a dónde subir: al menú
    if up == MANAGE_ROOT and len(manage.top_folders()) <= 1:
        leave = home_button()
    else:
        leave = Button.inline(get_text("button_picker_up"), data=up)
    navigation.append([leave, Button.inline(get_text("button_close"), data="close")])
    if not files_info:
        return f"{title}\n\n{get_text('folder_empty')}", navigation
    text, buttons = manage.build_manage_listing(
        files_info, total_size, page,
        page_data=lambda number: f"fm:o:{file_id}:{number}", mass_ref=file_id, title=title)
    return text, buttons + navigation


# ---------------------------------------------------------------------------
# Nueva carpeta
# ---------------------------------------------------------------------------

def _new_folder_prompt(file_id, path, problem=None):
    text = get_text("manage_new_folder_ask", md_code(path))
    if problem:
        text += f"\n\n{problem}"
    return text, [[Button.inline(get_text("button_back_to_manage"), data=f"fm:o:{file_id}")]]


async def new_folder(event, file_id):
    """Pide el nombre de la carpeta que se va a crear dentro de la abierta."""
    path = _path_of(file_id)
    if path is None or not os.path.isdir(path):
        await _not_found(event)
        return
    inputs.ask(event, "manage_new_folder", file_id=file_id)
    text, buttons = _new_folder_prompt(file_id, path)
    await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE)


def _new_folder_problem(path, name):
    """Por qué no se puede crear `name` dentro de `path`, o None si se ha creado."""
    # Las mismas reglas que al renombrar: ni rutas, ni ocultas, ni invisibles
    if not name or "/" in name or "\\" in name or name.startswith(".") or has_invisible_chars(name):
        return f"{get_text('rename_invalid_title')}\n{get_text('rename_invalid_chars')}"
    if len(name.encode("utf-8")) > MAX_FILENAME_BYTES:
        return f"{get_text('rename_invalid_title')}\n{get_text('rename_too_long')}"
    target = os.path.join(path, name)
    if os.path.lexists(target):
        return f"{get_text('rename_already_exists_title')}\n{get_text('rename_already_exists_desc', md_code(name))}"
    try:
        folders.check(target, create=True)
    except folders.FolderError as e:
        return get_text(f"folder_error_{e.code}")
    debug(f"[FILE_MANAGE] Folder created: {target}")
    return None


async def handle_new_folder_name(event, text, pending, topic):
    """El nombre escrito: se crea y se vuelve a la carpeta, ya con ella dentro."""
    file_id = pending["data"]["file_id"]
    message = pending["message"]
    path = _path_of(file_id)
    if path is None or not os.path.isdir(path):
        await _not_found(message)
        return
    problem = await asyncio.to_thread(_new_folder_problem, path, text)
    if problem:
        # Se sigue esperando: lo normal es corregirlo y volver a mandarlo
        inputs.ask(message, "manage_new_folder", file_id=file_id)
        shown, buttons = _new_folder_prompt(file_id, path, problem)
        await safe_edit(message, shown, buttons=buttons, parse_mode=PARSE_MODE)
        return
    stats.count("btn_manage_new_folder")
    await open_folder(message, file_id)


# ---------------------------------------------------------------------------
# El explorador: por dónde se entra
# ---------------------------------------------------------------------------

async def explorer_root(event, rest=""):
    """La primera pantalla de /manage, en el mensaje del botón: las carpetas
    por las que se entra, o directamente la única que hay."""
    tops = manage.top_folders()
    manage_trails.pop(manage._trail_key(event), None)
    inputs.forget_message(event.sender_id, event.chat_id, getattr(event, "message_id", None))
    if len(tops) == 1:
        await open_folder(event, manage.register_manage_action(tops[0]))
        return
    text, buttons = manage.build_manage_menu()
    manage.nav_visit(event, MANAGE_ROOT)
    await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE)


async def build_explorer():
    """La primera pantalla de /manage para un mensaje nuevo: (texto, botones,
    listado, carpeta). Los dos últimos son lo que hay que apuntar con
    `manage.nav_visit` cuando se sepa en qué mensaje ha quedado."""
    tops = manage.top_folders()
    if len(tops) != 1:
        text, buttons = manage.build_manage_menu()
        return text, buttons, MANAGE_ROOT, None
    path = tops[0]
    file_id = manage.register_manage_action(path)
    try:
        files_info, total_size, _, _ = await asyncio.to_thread(manage._scan_directories, [path], "FILE_MANAGE")
    except OSError as e:
        error(f"[FILE_MANAGE] Cannot read {path}: {e}")
        return get_text("error_manage_files"), [home_exit_row()], None, None
    files_info.sort(key=lambda item: item["name"].lower())
    text, buttons = _folder_view(file_id, path, files_info, total_size, 0, MANAGE_ROOT)
    return text, buttons, f"fm:o:{file_id}:0", file_id


# ---------------------------------------------------------------------------
# Mover un elemento
# ---------------------------------------------------------------------------

def move_error_text(e, name):
    """Por qué no se ha movido algo, para enseñarlo."""
    if isinstance(e, fileops.MoveError):
        return get_text(f"move_error_{e.code}", e.detail or name)
    return get_text("move_error_os", getattr(e, "strerror", None) or e)


async def start_move(event, file_id):
    path = _path_of(file_id)
    if path is None:
        await _not_found(event)
        return
    stats.count("btn_manage_move")
    # Si es un episodio, la carpeta donde ya están los demás va la primera
    suggested = []
    if settings.series_suggest():
        suggested = await asyncio.to_thread(fileops.series_folders, path)
    picker_id = picker.open_picker(
        "move", get_text("move_title", manage._display_name(os.path.basename(path))),
        payload={"file_id": file_id}, suggested=suggested, exclude=os.path.dirname(path))
    await picker.show(event, picker_id)


async def _move_cancelled(event, chosen):
    await manage.show_file_actions(event, chosen["payload"]["file_id"])


async def _move_chosen(event, chosen, target):
    file_id = chosen["payload"]["file_id"]
    path = _path_of(file_id)
    if path is None:
        await _not_found(event)
        return
    name = manage._display_name(os.path.basename(path))
    await safe_edit(event, get_text("moving", name), parse_mode=PARSE_MODE)

    async def move():
        # Si falla, a la ficha, que sigue valiendo; si va bien, como tras
        # renombrar o eliminar, al listado del que se venía
        buttons = [_navigation(f"fileact:{file_id}")]
        try:
            new_path = await asyncio.to_thread(fileops.move_item, path, target)
        except (fileops.MoveError, OSError) as e:
            warning(f"[MOVE] Could not move {path} to {target}: {e}")
            text = f"{get_text('move_error_title')}\n\n{move_error_text(e, name)}"
        else:
            repoint(path, new_path)
            text = get_text("move_done", name, target)
            buttons = [_navigation(manage.nav_back(event, path))]
        await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE)

    # Fuera del handler: entre dispositivos es una copia, que puede tardar
    await outlive_handler(move())


# ---------------------------------------------------------------------------
# Renombrar con la sugerencia
# ---------------------------------------------------------------------------

async def rename_suggested(event, file_id):
    path = _path_of(file_id)
    if path is None:
        await _not_found(event)
        return
    manage._forget_renames(event.sender_id, file_id)
    stats.count("btn_manage_rename_suggested")
    original = manage._display_name(os.path.basename(path))
    is_directory = os.path.isdir(path)
    suggested = await asyncio.to_thread(fileops.suggest_for, path)
    back = [_navigation(f"fileact:{file_id}")]
    if not suggested:
        await safe_edit(event, get_text("rename_all_nothing"), buttons=back, parse_mode=PARSE_MODE)
        return
    try:
        new_path, subtitles = await asyncio.to_thread(fileops.rename_item, path, suggested)
    except FileExistsError:
        text = f"{get_text('rename_already_exists_title')}\n\n{get_text('rename_already_exists_desc', md_code(suggested))}"
        await safe_edit(event, text, buttons=back, parse_mode=PARSE_MODE)
        return
    except OSError as e:
        error(f"[FILE_RENAME] Error renaming {path}: {e}")
        text = f"{get_text('rename_error_title')}\n\n{get_text('rename_error_desc', md_code(e.strerror or e))}"
        await safe_edit(event, text, buttons=back, parse_mode=PARSE_MODE)
        return
    repoint(path, new_path)
    debug(f"[FILE_RENAME] Renamed with the suggestion: {path} -> {new_path}")
    await safe_edit(event, manage.rename_success_text(original, suggested, is_directory, subtitles),
                    buttons=[_navigation(manage.nav_back(event, new_path))], parse_mode=PARSE_MODE)


# ---------------------------------------------------------------------------
# Listados: de qué elementos se habla
# ---------------------------------------------------------------------------

def _children(directories):
    """Lo visible que cuelga de esas carpetas, sin repetir y por nombre."""
    found = []
    for directory in directories:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            if not name.startswith(".") and path not in found:
                found.append(path)
    return sorted(found, key=lambda path: os.path.basename(path).lower())


def listing_items(ref):
    """Los elementos de un listado (ver el módulo), o None si ya no existe. Bloqueante."""
    if ref.startswith("q."):
        query = find_queries.get(ref[2:])
        return None if query is None else fileops.search(query, folders.known())
    path = _path_of(ref)
    return _children([path]) if path and os.path.isdir(path) else None


def listing_back(ref, event=None):
    """El botón que vuelve a ese listado; con `event`, a la página en la que
    estaba en ese mensaje."""
    if ref.startswith("q."):
        back = f"fm:f:{ref[2:]}"
    else:
        back = f"fm:o:{ref}"
    if event is not None:
        shown = manage.nav_back(event)
        if shown.startswith(back + ":"):
            return shown
    return back


async def mass_menu(event, ref):
    items = await asyncio.to_thread(listing_items, ref)
    if not items:
        await safe_edit(event, get_text("mass_nothing"), buttons=[_navigation(listing_back(ref, event))], parse_mode=PARSE_MODE)
        return
    buttons = [
        [Button.inline(get_text("mass_button_move"), data=f"fm:mm:{ref}")],
        [Button.inline(get_text("mass_button_rename"), data=f"fm:mr:{ref}")],
        [Button.inline(get_text("mass_button_delete"), data=f"fm:md:{ref}")],
        _navigation(listing_back(ref, event)),
    ]
    await safe_edit(event, get_text("mass_title", len(items)), buttons=buttons, parse_mode=PARSE_MODE)


# ---------------------------------------------------------------------------
# Mover todo
# ---------------------------------------------------------------------------

async def mass_move(event, ref):
    items = await asyncio.to_thread(listing_items, ref)
    if not items:
        await mass_menu(event, ref)
        return
    stats.count("btn_manage_mass_move")
    confirmations[(event.sender_id, f"mm:{ref}")] = items
    folder = _path_of(ref)
    picker_id = picker.open_picker("massmove", get_text("mass_move_title", len(items)),
                                   payload={"ref": ref}, exclude=folder)
    await picker.show(event, picker_id)


async def _mass_move_cancelled(event, chosen):
    ref = chosen["payload"]["ref"]
    confirmations.pop((event.sender_id, f"mm:{ref}"), None)
    await mass_menu(event, ref)


def _move_all(items, target):
    """Mueve cada elemento; devuelve (movidos, [(nombre, error)])."""
    moved = 0
    failed = []
    for path in items:
        try:
            repoint(path, fileops.move_item(path, target))
            moved += 1
        except (fileops.MoveError, OSError) as e:
            failed.append((os.path.basename(path), e))
    return moved, failed


def _failure_lines(failed):
    lines = [f"• {move_error_text(e, name)}" for name, e in failed[:FAILURE_LINES]]
    if len(failed) > FAILURE_LINES:
        lines.append(get_text("and_more", len(failed) - FAILURE_LINES))
    return lines


async def _mass_move_chosen(event, chosen, target):
    ref = chosen["payload"]["ref"]
    items = confirmations.pop((event.sender_id, f"mm:{ref}"), None)
    if not items:
        await mass_menu(event, ref)
        return
    await safe_edit(event, get_text("mass_moving", len(items)), parse_mode=PARSE_MODE)

    async def move():
        moved, failed = await asyncio.to_thread(_move_all, items, target)
        lines = [get_text("mass_move_done", moved, len(items), target)]
        if failed:
            lines += [""] + _failure_lines(failed)
        await safe_edit(event, "\n".join(lines), buttons=[_navigation(listing_back(ref, event))], parse_mode=PARSE_MODE)

    await outlive_handler(move())


# ---------------------------------------------------------------------------
# Borrar todo
# ---------------------------------------------------------------------------

async def mass_delete(event, ref):
    items = await asyncio.to_thread(listing_items, ref)
    if not items:
        await mass_menu(event, ref)
        return
    confirmations[(event.sender_id, f"md:{ref}")] = items
    unclaim(confirmations, (event.sender_id, f"md:{ref}"))
    size = await asyncio.to_thread(lambda: sum(fileops.tree_size(path) for path in items))
    text = "\n\n".join([
        get_text("confirm_delete_title"),
        get_text("mass_delete_question", len(items), format_file_size(size)),
        get_text("confirm_delete_no_undo"),
    ])
    buttons = [[
        Button.inline(get_text("button_yes_delete"), data=f"fm:mdc:{ref}"),
        Button.inline(get_text("button_cancel"), data=f"fm:ma:{ref}"),
    ]]
    await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE)


def _delete_all(items):
    deleted = 0
    failed = []
    for path in items:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            deleted += 1
        except FileNotFoundError:
            # Ya no está: nada que borrar, y nada que contar como fallo
            pass
        except OSError as e:
            failed.append((os.path.basename(path), e))
    return deleted, failed


async def mass_delete_confirmed(event, ref):
    # Sin ningún await entre mirarlo y quitarlo: un doble toque no borra dos veces
    items = await _claim_confirmation(event, f"md:{ref}")
    if items is None:
        return
    stats.count("btn_manage_mass_delete")
    await safe_edit(event, get_text("mass_deleting", len(items)), parse_mode=PARSE_MODE)

    async def delete():
        deleted, failed = await asyncio.to_thread(_delete_all, items)
        debug(f"[FILE_DELETE] {deleted} of {len(items)} items deleted at once")
        lines = [get_text("mass_delete_done", deleted, len(items))]
        if failed:
            lines += [""] + _failure_lines(failed)
        await safe_edit(event, "\n".join(lines), buttons=[_navigation(listing_back(ref, event))], parse_mode=PARSE_MODE)

    await outlive_handler(delete())


# ---------------------------------------------------------------------------
# Renombrar todo
# ---------------------------------------------------------------------------

def _listing_plan(ref):
    """El plan de renombrado de todo un listado, o None si ya no existe."""
    if not ref.startswith("q."):
        # Una carpeta abierta: lo que lleva dentro, con ella de contexto
        path = _path_of(ref)
        return fileops.build_plan(path, include_top=False) if path and os.path.isdir(path) else None
    items = listing_items(ref)
    if items is None:
        return None
    return fileops.merge_plans([fileops.build_plan(path) for path in items])


async def rename_folder_preview(event, file_id):
    path = _path_of(file_id)
    if path is None:
        await _not_found(event)
        return
    plan = await asyncio.to_thread(fileops.build_plan, path)
    await _preview(event, f"i.{file_id}", plan, f"fileact:{file_id}")


async def mass_rename_preview(event, ref):
    plan = await asyncio.to_thread(_listing_plan, ref)
    if plan is None:
        await mass_menu(event, ref)
        return
    await _preview(event, f"l.{ref}", plan, f"fm:ma:{ref}")


async def _preview(event, key, plan, back):
    """Enseña lo que se va a renombrar y pide confirmarlo."""
    renames = plan.folders[::-1] + plan.files
    if not renames:
        lines = [get_text("rename_all_nothing")]
        if plan.skipped:
            lines.append(get_text("rename_all_skipped", plan.skipped))
        await safe_edit(event, "\n\n".join(lines), buttons=[_navigation(back)], parse_mode=PARSE_MODE)
        return
    confirmations[(event.sender_id, f"rn:{key}")] = plan
    unclaim(confirmations, (event.sender_id, f"rn:{key}"))
    lines = [get_text("rename_all_title"), "", get_text("rename_all_count", len(plan.files), len(plan.folders)), ""]
    for path, new_name in renames[:PREVIEW_LINES]:
        icon = "📁" if os.path.isdir(path) else "📄"
        lines.append(f"{icon} {get_text('rename_all_line', _short(os.path.basename(path)), _short(new_name))}")
    if len(renames) > PREVIEW_LINES:
        lines.append(get_text("and_more", len(renames) - PREVIEW_LINES))
    if plan.skipped:
        lines += ["", get_text("rename_all_skipped", plan.skipped)]
    buttons = [[
        Button.inline(get_text("button_rename_all_confirm"), data=f"fm:rc:{key}"),
        Button.inline(get_text("button_cancel"), data=back),
    ]]
    await safe_edit(event, "\n".join(lines), buttons=buttons, parse_mode=PARSE_MODE)


async def rename_confirmed(event, key):
    plan = await _claim_confirmation(event, f"rn:{key}")
    if plan is None:
        return
    stats.count("btn_manage_rename_all")
    kind, _, ref = key.partition(".")
    files_done, folders_done, errors = await asyncio.to_thread(fileops.apply_plan, plan, repoint)
    debug(f"[FILE_RENAME] Renamed at once: {files_done} files and {folders_done} folders, {len(errors)} errors")
    lines = [get_text("rename_all_done", files_done, folders_done)]
    if errors:
        lines += [""] + [f"• `{_short(line)}`" for line in errors[:FAILURE_LINES]]
        if len(errors) > FAILURE_LINES:
            lines.append(get_text("and_more", len(errors) - FAILURE_LINES))
    back = manage.nav_back(event, _path_of(ref)) if kind == "i" else listing_back(ref, event)
    await safe_edit(event, "\n".join(lines), buttons=[_navigation(back)], parse_mode=PARSE_MODE)


# ---------------------------------------------------------------------------
# /find
# ---------------------------------------------------------------------------

def _search_infos(query):
    infos = []
    for path in fileops.search(query, folders.known()):
        try:
            infos.append(manage.item_info(path))
        except OSError:
            continue
    return infos


async def _find_screen(query_id, page):
    query = find_queries.get(query_id)
    close = home_exit_row()
    if query is None:
        return get_text("picker_expired"), [close]
    infos = await asyncio.to_thread(_search_infos, query)
    if not infos:
        return get_text("find_none", query), [close]
    title = get_text("find_title", query)
    if len(infos) >= fileops.SEARCH_MAX_RESULTS:
        title += f"\n{get_text('find_truncated', fileops.SEARCH_MAX_RESULTS)}"
    text, buttons = manage.build_manage_listing(
        infos, sum(info["size"] for info in infos), page,
        page_data=lambda number: f"fm:f:{query_id}:{number}", mass_ref=f"q.{query_id}", title=title)
    return text, buttons + [close]


async def send_find(event, query, topic):
    """`/find texto`: lo que hay en el servidor con ese nombre."""
    if not query:
        await safe_send_message(event.chat_id, get_text("find_usage"), parse_mode=PARSE_MODE, reply_to=topic)
        return
    query_id = short_id("q")
    find_queries[query_id] = query
    text, buttons = await _find_screen(query_id, 0)
    sent = await safe_send_message(event.chat_id, text, buttons=buttons, parse_mode=PARSE_MODE,
                                   wait_for_result=True, reply_to=topic)
    # Para que «Volver» desde un resultado regrese a la búsqueda
    if sent is not None:
        manage.nav_visit(event, f"fm:f:{query_id}:0", message_id=sent.id)


async def show_find(message, query):
    """Los resultados de una búsqueda, pintados en `message` (el botón Buscar del menú)."""
    query_id = short_id("q")
    find_queries[query_id] = query
    text, buttons = await _find_screen(query_id, 0)
    manage.nav_visit(message, f"fm:f:{query_id}:0")
    await safe_edit(message, text, buttons=buttons, parse_mode=PARSE_MODE)


async def find_page(event, rest):
    query_id, _, page = rest.partition(":")
    page = int(page) if page.isdigit() else 0
    text, buttons = await _find_screen(query_id, page)
    manage.nav_visit(event, f"fm:f:{query_id}:{page}")
    await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE)


ACTIONS = {
    "r": explorer_root,
    "o": open_folder,
    "nf": new_folder,
    "mv": start_move,
    "rs": rename_suggested,
    "ra": rename_folder_preview,
    "ma": mass_menu,
    "mm": mass_move,
    "mr": mass_rename_preview,
    "md": mass_delete,
    "mdc": mass_delete_confirmed,
    "rc": rename_confirmed,
    "f": find_page,
}

# Las que contestan ellas al botón (ver _claim_confirmation)
SELF_ANSWERED = {"mdc", "rc"}
