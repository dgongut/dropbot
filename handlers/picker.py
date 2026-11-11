"""El selector de carpeta: elegir a dónde va algo, navegando con botones.

Lo usan mover un fichero, mover todos los de un listado, elegir la carpeta de
un tipo en /settings, añadir una favorita y elegir dónde se guarda una descarga
recién llegada. Cada uno abre un selector con su propósito (`open_picker`) y
registra qué hacer cuando se elige una carpeta o se cancela (`register`).

La primera pantalla ofrece las carpetas conocidas: las de la serie si las hay
(✨), las favoritas (⭐) y las de descarga (📂). Al entrar en una se ven sus
subcarpetas, y se puede seguir bajando, subir, crear una nueva o quedarse con
la que se está viendo. Solo se llega a carpetas que valen como destino (ver
`folders`): lo que no está dentro de un volumen ni se ofrece ni se acepta
escrito a mano.

Los botones llevan `pk:<id>:<operación>`; la ruta nunca viaja en el botón.
"""

import os

from telethon import Button, events

import folders
from basic import md_code
from handlers import inputs
from logger import debug
from state import pickers, short_id
from translations import PARSE_MODE, get_text
from utils.telegram_helpers import check_admin_and_warn, safe_answer, safe_delete, safe_edit, safe_send_message

# Carpetas por pantalla
PAGE_SIZE = 8
# Lo que cabe de una ruta en un botón
LABEL_LENGTH = 34

# Qué hacer al elegir y al cancelar en cada propósito: {propósito: (elegir, cancelar)}
_purposes = {}


def init(bot):
    bot.on(events.CallbackQuery(pattern=b"pk:(.+)"))(handle_picker)
    inputs.register("picker_new", handle_new_folder)


def register(purpose, on_choose, on_cancel):
    """`on_choose(event, selector, ruta)` y `on_cancel(event, selector)`."""
    _purposes[purpose] = (on_choose, on_cancel)


def open_picker(purpose, title, payload=None, suggested=(), exclude=None):
    """Abre un selector y devuelve su id.

    `title` es lo que se pregunta; `suggested`, las carpetas que van las
    primeras (las de la serie); `exclude`, la carpeta donde ya está lo que se
    mueve, que no se puede elegir.
    """
    picker_id = short_id("p")
    pickers[picker_id] = {
        "purpose": purpose,
        "title": title,
        "payload": payload or {},
        "suggested": list(suggested),
        "exclude": exclude,
        "current": None,
        "page": 0,
        "entries": [],
    }
    return picker_id


def short_path(path, length=LABEL_LENGTH):
    """Una ruta recortada por delante: el final es lo que la distingue."""
    return path if len(path) <= length else "…" + path[-(length - 1):]


def page_row(page, pages, data):
    """La fila de páginas: anterior, dónde estás y siguiente."""
    row = []
    if page > 0:
        row.append(Button.inline("«", data=data(page - 1)))
    row.append(Button.inline(f"{page + 1}/{pages}", data="pk:noop"))
    if page < pages - 1:
        row.append(Button.inline("»", data=data(page + 1)))
    return row


def build(picker_id):
    """El texto y los botones de la pantalla en la que está el selector."""
    picker = pickers[picker_id]
    current = picker["current"]
    lines = [picker["title"], ""]
    buttons = []

    if current is None:
        entries = []
        for path in picker["suggested"]:
            if path not in [entry for entry, _ in entries]:
                entries.append((path, "✨"))
        favorites = folders.favorites()
        for path in folders.known():
            if path not in [entry for entry, _ in entries]:
                entries.append((path, "⭐" if path in favorites else "📂"))
        lines.append(get_text("picker_choose"))
    else:
        entries = [(path, "📁") for path in folders.subfolders(current)]
        lines.append(get_text("picker_current", md_code(current)))
        if current == picker["exclude"]:
            lines.append(get_text("picker_already_here"))
        elif not os.path.isdir(current):
            # Una carpeta de la serie que aún no existe (la temporada siguiente)
            lines.append(get_text("picker_will_create"))
        else:
            lines.append(get_text("picker_subfolders" if entries else "picker_no_subfolders"))
        if current != picker["exclude"]:
            buttons.append([Button.inline(get_text("button_picker_here"), data=f"pk:{picker_id}:ok")])

    picker["entries"] = [path for path, _ in entries]
    pages = max(1, -(-len(entries) // PAGE_SIZE))
    page = max(0, min(picker["page"], pages - 1))
    picker["page"] = page
    start = page * PAGE_SIZE
    for index, (path, icon) in enumerate(entries[start:start + PAGE_SIZE], start):
        label = short_path(path) if current is None else short_path(os.path.basename(path))
        buttons.append([Button.inline(f"{icon} {label}", data=f"pk:{picker_id}:o:{index}")])
    if pages > 1:
        buttons.append(page_row(page, pages, lambda number: f"pk:{picker_id}:g:{number}"))

    if current is None:
        buttons.append([Button.inline(get_text("button_picker_write"), data=f"pk:{picker_id}:n")])
    else:
        buttons.append([
            Button.inline(get_text("button_picker_up"), data=f"pk:{picker_id}:u"),
            Button.inline(get_text("button_picker_new"), data=f"pk:{picker_id}:n"),
        ])
    buttons.append([Button.inline(get_text("button_cancel"), data=f"pk:{picker_id}:x")])
    return "\n".join(lines), buttons


async def show(event, picker_id):
    """Pinta el selector en el mensaje del botón."""
    text, buttons = build(picker_id)
    await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE)


async def send(chat_id, picker_id, topic=None):
    """Manda el selector en un mensaje nuevo."""
    text, buttons = build(picker_id)
    await safe_send_message(chat_id, text, buttons=buttons, parse_mode=PARSE_MODE, reply_to=topic)


async def handle_picker(event):
    if await check_admin_and_warn(event):
        return
    parts = event.pattern_match.group(1).decode(errors="replace").split(":")
    if parts[0] == "noop":
        await safe_answer(event)
        return
    picker = pickers.get(parts[0])
    if picker is None or len(parts) < 2:
        # Un selector de antes de reiniciar, o ya usado
        await safe_answer(event, get_text("picker_expired"), alert=True)
        return
    picker_id, operation = parts[0], parts[1]
    argument = parts[2] if len(parts) > 2 else ""
    # Cualquier botón deja de esperar la ruta escrita a mano
    inputs.forget(event.sender_id, "picker_new")

    if operation == "x":
        await safe_answer(event)
        pickers.pop(picker_id, None)
        await _purposes[picker["purpose"]][1](event, picker)
        return

    if operation == "ok":
        current = picker["current"]
        if current is None or current == picker["exclude"]:
            await safe_answer(event)
            return
        try:
            # Se vuelve a mirar al elegir: entretanto pueden haberla borrado,
            # o cambiado por un enlace que sale del volumen
            chosen = folders.check(current, create=True)
        except folders.FolderError as e:
            await safe_answer(event, get_text(f"folder_error_{e.code}"), alert=True)
            return
        await safe_answer(event)
        pickers.pop(picker_id, None)
        debug(f"[PICKER] {picker['purpose']}: chosen {chosen}")
        await _purposes[picker["purpose"]][0](event, picker, chosen)
        return

    if operation == "n":
        await safe_answer(event)
        inputs.ask(event, "picker_new", picker_id=picker_id)
        current = picker["current"]
        text = f'{picker["title"]}\n\n'
        text += get_text("picker_write_path") if current is None else get_text("picker_write_name", current)
        await safe_edit(event, text, buttons=[[Button.inline(get_text("button_back"), data=f"pk:{picker_id}:b")]],
                        parse_mode=PARSE_MODE)
        return

    await safe_answer(event)
    if operation == "o" and argument.isdigit() and int(argument) < len(picker["entries"]):
        target = picker["entries"][int(argument)]
        # Una propuesta puede no existir aún; lo demás tiene que seguir ahí
        if folders.is_allowed(target) and (os.path.isdir(target) or target in picker["suggested"]):
            picker["current"] = target
            picker["page"] = 0
    elif operation == "u":
        current = picker["current"]
        # De una carpeta conocida se sube a la primera pantalla, no a su padre:
        # es por donde se entró
        picker["current"] = None if current is None or folders.is_root(current) else folders.parent_of(current)
        picker["page"] = 0
    elif operation == "g" and argument.isdigit():
        picker["page"] = int(argument)
    await show(event, picker_id)


async def handle_new_folder(event, text, pending, topic):
    """La ruta (o el nombre de la subcarpeta) escrita a mano."""
    picker_id = pending["data"]["picker_id"]
    picker = pickers.get(picker_id)
    if picker is None:
        return
    current = picker["current"]
    path = text if text.startswith("/") or current is None else os.path.join(current, text)
    notice = None
    try:
        picker["current"] = folders.check(path, create=True)
        picker["page"] = 0
    except folders.FolderError as e:
        notice = get_text(f"folder_error_{e.code}")
    shown, buttons = build(picker_id)
    if notice:
        shown = f"{shown}\n\n{notice}"
    await safe_edit(pending["message"], shown, buttons=buttons, parse_mode=PARSE_MODE)


async def close_message(event, picker):
    """Un cancelar que solo quita el mensaje."""
    await safe_delete(event)
