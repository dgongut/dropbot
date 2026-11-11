"""El menú principal: lo que enseña /start, con un botón para cada cosa.

Cada botón pinta su pantalla en el mismo mensaje, y las pantallas de primer
nivel (los ajustes, los listados, la búsqueda) llevan uno para volver aquí, así
que se puede ir de una a otra sin escribir comandos ni acumular mensajes.

    home:main      el menú
    home:manage    /manage (el explorador de archivos)
    home:find      pide qué buscar (lo que se escriba, como /find)
    home:about     versión y donaciones

Los ajustes se abren con su propio botón (`cfg:main`, ver handlers/settings.py).
"""

from telethon import Button, events

import stats
from handlers import files, inputs, manage
from handlers.navigation import HOME, exit_row, home_button
from state import manage_trails
from translations import PARSE_MODE, get_text
from utils.telegram_helpers import (
    check_admin_and_warn, safe_answer, safe_edit,
)


# La versión del bot, para «Acerca de»: vive en dropbot.py, que la pasa en init()
_version = ""


def init(bot, version=""):
    global _version
    _version = version
    bot.on(events.CallbackQuery(pattern=b"home:(.+)"))(handle_home)
    inputs.register("find", handle_find_input)


def build_menu():
    buttons = [
        [Button.inline(get_text("home_button_manage"), data="home:manage"),
         Button.inline(get_text("home_button_find"), data="home:find")],
        [Button.inline(get_text("home_button_settings"), data="cfg:main"),
         Button.inline(get_text("home_button_about"), data="home:about")],
        [Button.inline(get_text("button_close"), data="close")],
    ]
    return get_text("welcome_message"), buttons


async def _leave(event):
    """Lo que el mensaje tuviera a medias se suelta al cambiar de pantalla."""
    user_id = event.sender_id
    own_message = getattr(event, "message_id", None)
    manage._forget_renames_of_message(user_id, event.chat_id, own_message)
    inputs.forget_message(user_id, event.chat_id, own_message)
    manage_trails.pop(manage._trail_key(event), None)


async def handle_home(event):
    if await check_admin_and_warn(event):
        return
    await safe_answer(event)
    action = event.pattern_match.group(1).decode(errors="replace")
    await _leave(event)
    stats.count(f"btn_home_{action}" if action in ("manage", "find", "about") else "btn_home_main")

    if action == "manage":
        # El explorador: las carpetas del servidor, o la única que haya
        await files.explorer_root(event)
    elif action == "find":
        inputs.ask(event, "find")
        await safe_edit(event, get_text("find_prompt"), buttons=[[home_button()]], parse_mode=PARSE_MODE)
    elif action == "about":
        text = f'{get_text("version", _version)}\n\n{get_text("donate")}'
        await safe_edit(event, text, buttons=[exit_row()], parse_mode=PARSE_MODE, link_preview=False)
    else:
        text, buttons = build_menu()
        await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE)


async def handle_find_input(event, text, pending, topic):
    """Lo que se busca, escrito tras pulsar Buscar: los resultados van en ese mensaje."""
    await files.show_find(pending["message"], text)


__all__ = ["HOME", "build_menu", "handle_home", "init"]
