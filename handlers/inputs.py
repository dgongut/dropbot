"""Texto que el bot le pide al admin: una ruta, una plantilla de renombrado.

Un botón deja apuntado qué se espera (`ask`), y el siguiente mensaje de texto
de ese usuario en ese chat es la respuesta. Con las mismas reglas que el
renombrado de /manage (ver handlers/manage.py): un texto suelto solo vale un
rato, citar el mensaje que lo pide vale siempre, y fuera del privado solo vale
la cita, porque en un grupo el admin también habla de otras cosas.

Cada usuario tiene como mucho una petición: pedir otra cosa olvida la anterior,
y también el renombrado que tuviera a medias, para que una sola respuesta no
pueda servir a dos preguntas.
"""

import re
import time

from telethon import events
from telethon.tl.types import MessageMediaWebPage

from basic import is_admin
from logger import debug
from state import pending_inputs, pending_renames
from utils.telegram_helpers import get_topic

INPUT_TIMEOUT = 15 * 60

# Los comandos del bot: una ruta también empieza por "/", pero "/list" no es una
COMMAND_RE = re.compile(r"^/(start|donate|version|donors|list|manage|settings|find)(@\w+)?(\s|$)", re.IGNORECASE)

# Qué hacer con la respuesta de cada tipo de petición: {tipo: corrutina}
_handlers = {}


def init(bot):
    bot.on(events.NewMessage(func=is_input_answer))(handle_input)


def register(kind, handler):
    """`handler(event, texto, petición, tema)` atiende las respuestas de `kind`."""
    _handlers[kind] = handler


def ask(event, purpose, /, **data):
    """Deja apuntado que el siguiente texto de quien pulsó el botón es para `purpose`."""
    pending_renames.pop(event.sender_id, None)
    pending_inputs[event.sender_id] = {
        "kind": purpose,
        "data": data,
        # El botón, para repintar su mensaje con el resultado
        "message": event,
        "message_id": getattr(event, "message_id", None),
        "chat_id": event.chat_id,
        "created": time.monotonic(),
    }


def forget(user_id, kind=None):
    """Deja de esperar el texto de ese usuario (de ese tipo, si se dice)."""
    pending = pending_inputs.get(user_id)
    if pending and (kind is None or pending["kind"] == kind):
        pending_inputs.pop(user_id, None)


def forget_message(user_id, chat_id, message_id):
    """Deja de esperar el texto que pedía ese mensaje (se ha cerrado)."""
    pending = pending_inputs.get(user_id)
    if pending and pending["chat_id"] == chat_id and pending["message_id"] == message_id:
        pending_inputs.pop(user_id, None)


def _request(event):
    pending = pending_inputs.get(event.sender_id)
    if not pending or pending["chat_id"] != event.chat_id:
        return None
    quoted = event.reply_to_msg_id
    cites = bool(quoted) and pending["message_id"] == quoted
    if not cites and (not event.is_private or time.monotonic() - pending["created"] >= INPUT_TIMEOUT):
        return None
    return pending


def is_input_answer(event):
    """Si un mensaje es la respuesta a un texto que se ha pedido."""
    if _request(event) is None:
        return False
    text = event.raw_text or ""
    if not text.strip() or "://" in text or COMMAND_RE.match(text.strip()):
        return False
    media = getattr(event, "media", None)
    if media and not isinstance(media, MessageMediaWebPage):
        return False
    return True


async def handle_input(event):
    if not is_admin(event.sender_id):
        return
    pending = _request(event)
    if pending is None:
        return
    pending_inputs.pop(event.sender_id, None)
    handler = _handlers.get(pending["kind"])
    if handler is None:
        debug(f"[INPUT] No handler for {pending['kind']}")
        return
    # Como en el renombrado: lo copiado de un mensaje del bot puede traer el
    # espacio de ancho cero con que md_code separa las comillas invertidas
    text = event.raw_text.strip().replace("`​", "`")
    # El tema, antes de borrar el mensaje
    topic = await get_topic(event)
    try:
        await event.delete()
    except Exception:
        pass
    await handler(event, text, pending, topic)
