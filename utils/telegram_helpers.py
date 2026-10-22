"""
Funciones wrapper para enviar mensajes a Telegram a través de la cola
y evitar rate limiting. Las respuestas a los botones y el aviso a quien no es
admin van directas: no pueden esperar o no deben ocupar la cola.

Las dependencias (message_queue y bot) deben inyectarse llamando a
`init(message_queue, bot)` antes de usar cualquiera de las funciones safe_*.
"""

import asyncio
import time

from telethon import events
from telethon.tl import types

from basic import is_admin
from logger import debug, warning
from translations import PARSE_MODE, get_text

_message_queue = None
_bot = None

# El @ del bot, para reconocer los comandos dirigidos a otro
_bot_username = None

# A quien no es admin se le contesta como mucho una vez cada tanto, y se
# recuerda a un número limitado de personas: si se llena, no se contesta a
# nadie nuevo hasta que venzan los avisos que hay
NOT_ADMIN_WARNING_INTERVAL = 600  # segundos
NOT_ADMIN_WARNING_MAX_USERS = 1000
_not_admin_warned = {}

# Telegram rechaza los mensajes de más de 4096 caracteres (contados en UTF-16,
# ver utf16_len): lo que se parte en varios mensajes deja margen
SAFE_MESSAGE_LENGTH = 3800


def utf16_len(text):
    """Longitud tal como la cuenta Telegram: en unidades UTF-16.

    Con len() un emoji cuenta 1 pero para Telegram son 2, y un listado lleno
    de emoji pasaba del límite de 4096 y se rechazaba entero.
    """
    return len(text.encode("utf-16-le")) // 2


def init(message_queue, bot):
    """Inyecta las dependencias (cola de mensajes e instancia del bot)."""
    global _message_queue, _bot
    _message_queue = message_queue
    _bot = bot


async def safe_edit(message, *args, wait_for_result=False, **kwargs):
    """Edita un mensaje usando la cola para evitar rate limiting"""
    return await _message_queue.add_message(message.edit, *args, wait_for_result=wait_for_result, **kwargs)


async def safe_reply(event, *args, wait_for_result=False, **kwargs):
    """Responde a un evento usando la cola para evitar rate limiting"""
    return await _message_queue.add_message(event.reply, *args, wait_for_result=wait_for_result, **kwargs)


async def safe_respond(event, *args, wait_for_result=False, **kwargs):
    """Responde a un evento usando event.respond y la cola para evitar rate limiting"""
    return await _message_queue.add_message(event.respond, *args, wait_for_result=wait_for_result, **kwargs)


async def safe_answer(event, *args, wait_for_result=False, **kwargs):
    """Responde a un callback query con event.answer, al momento y sin pasar por la cola.

    La respuesta no cuenta para los límites de mensajes de un chat y caduca en
    unos segundos: en la cola, detrás de un FloodWait, el botón se quedaba con
    el reloj, el aviso no salía y Telegram acababa rechazándola. Si falla, no
    pasa nada; `wait_for_result` se acepta como en los demás safe_*, aunque
    aquí siempre se espera.
    """
    try:
        return await event.answer(*args, **kwargs)
    except Exception as e:
        debug(f"[CALLBACK] Could not answer the callback: {e}")
        return None


async def safe_delete(message, *args, wait_for_result=False, **kwargs):
    """Elimina un mensaje usando la cola para evitar rate limiting"""
    return await _message_queue.add_message(message.delete, *args, wait_for_result=wait_for_result, **kwargs)


async def safe_send_message(chat_id, *args, wait_for_result=False, **kwargs):
    """Envía un mensaje usando la cola para evitar rate limiting"""
    return await _message_queue.add_message(_bot.send_message, chat_id, *args, wait_for_result=wait_for_result, **kwargs)


async def safe_send_file(chat_id, *args, wait_for_result=False, **kwargs):
    """Envía un archivo usando la cola para evitar rate limiting"""
    return await _message_queue.add_message(_bot.send_file, chat_id, *args, wait_for_result=wait_for_result, **kwargs)


def message_topic(message):
    """El tema del foro en el que está `message`, o None.

    En un supergrupo con temas, lo que se manda sin reply_to va a General: la
    respuesta a un comando o el fichero pedido en otro tema salían allí. Para
    que vayan a su tema se les pone de reply_to el id del tema, que es el del
    mensaje que lo abrió: un mensaje del tema lo trae en reply_to_top_id si
    contesta a otro, y si no en reply_to_msg_id. Solo con forum_topic: en
    privado, en General y en un grupo sin temas (también en los comentarios
    de un canal, que traen reply_to_top_id) no cambia nada.
    """
    reply_to = getattr(message, "reply_to", None)
    if not isinstance(reply_to, types.MessageReplyHeader) or not reply_to.forum_topic:
        return None
    return reply_to.reply_to_top_id or reply_to.reply_to_msg_id


async def get_topic(event):
    """El tema del que viene `event` (ver message_topic), para pasarlo como reply_to.

    En un botón es el del mensaje que lo lleva, que hay que pedirle a Telegram
    (solo en un supergrupo: en otro chat no hay temas). Telethon lo guarda la
    primera vez, así que quien vaya a borrar ese mensaje lo pide antes. Si no
    se puede saber, va a General, como antes.
    """
    if not isinstance(event, events.CallbackQuery.Event):
        return message_topic(event)
    if not event.is_channel:
        return None
    try:
        return message_topic(await event.get_message())
    except Exception as e:
        debug(f"[TOPIC] Could not get the message of the button: {e}")
        return None


# Lo que corre en outlive_handler: asyncio solo guarda una referencia débil a
# sus tareas, y una sin otra puede desaparecer a medias
_outliving_tasks = set()


async def outlive_handler(coro):
    """Ejecuta `coro` en su propia tarea y la espera, sin que muera con el handler.

    Si Telegram corta la conexión, run_until_disconnected desconecta y Telethon
    cancela los handlers que siguen en marcha: main() vuelve a conectar, pero
    el envío, la conversión o la descompresión que corría en uno se perdía,
    con su mensaje parado en «Convirtiendo…». Así acaba el handler y el
    trabajo sigue, como el de las descargas, que ya va en su propia tarea.
    """
    task = asyncio.ensure_future(coro)
    _outliving_tasks.add(task)
    task.add_done_callback(_outliving_tasks.discard)
    return await asyncio.shield(task)


def set_bot_username(username):
    """Guarda el @ del bot: main() lo pide con get_me() al arrancar (y
    for_another_bot, si le llega antes un comando con @)."""
    global _bot_username
    _bot_username = username


async def for_another_bot(event):
    """True si el comando lleva el @ de otro bot, como "/list@OtroBot".

    En un grupo con varios bots ese comando es para el otro: no se borra ni se
    contesta. Antes se atendía como propio, y el admin que escribía
    "/list@OtroBot video" publicaba en el grupo sus ficheros.

    Lo que llegó con el bot parado (catch_up) se atiende nada más conectar,
    antes de que main() sepa el @: entonces se pide aquí. Sin él, un
    "/list@DropBot" se tomaba por de otro bot y se ignoraba. Si no se puede
    saber, se trata como de otro, que es lo que no publica nada.
    """
    command = event.raw_text.split(maxsplit=1)[0]
    _, _, username = command.partition("@")
    if not username:
        return False
    if _bot_username is None:
        try:
            set_bot_username((await event.client.get_me()).username)
        except Exception as e:
            warning(f"[AUTH] Could not get the bot username: {e}")
            return True
    return username.lower() != (_bot_username or "").lower()


def is_file_message(event):
    """True si el mensaje trae un fichero que hay que descargar.

    Telethon devuelve en .photo/.document también la imagen de la vista previa
    de un enlace (MessageMediaWebPage): sin excluirla, pegar un enlace con
    preview descargaba además su miniatura como si fuera una foto enviada.

    Aquí y no en dropbot.py porque también filtra el /settings de
    handlers/settings.py, que no puede importar dropbot.
    """
    if isinstance(event.media, types.MessageMediaWebPage):
        return False
    return bool(event.document or event.video or event.audio or event.photo)


async def warn_not_admin(event):
    """Le dice a quien no es admin que el bot no es suyo, sin dejar que lo atasque.

    Solo en privado y como mucho una vez cada NOT_ADMIN_WARNING_INTERVAL por
    persona. Antes se contestaba a cada comando, también en grupos, y por la
    cola de todos los mensajes: alguien que repitiera /start@bot en un grupo
    provocaba un FloodWait que dejaba al admin sin respuesta. Por eso va
    directo, sin cola ni reintentos: si no sale, no pasa nada.
    """
    if not event.is_private:
        return
    now = time.monotonic()
    last = _not_admin_warned.get(event.sender_id)
    if last is not None and now - last < NOT_ADMIN_WARNING_INTERVAL:
        return
    if len(_not_admin_warned) >= NOT_ADMIN_WARNING_MAX_USERS:
        # Los avisos vencidos ya no frenan nada: se olvidan
        for sender_id, when in list(_not_admin_warned.items()):
            if now - when >= NOT_ADMIN_WARNING_INTERVAL:
                del _not_admin_warned[sender_id]
        if len(_not_admin_warned) >= NOT_ADMIN_WARNING_MAX_USERS:
            return
    _not_admin_warned[event.sender_id] = now
    try:
        await event.respond(get_text("user_not_admin"), parse_mode=PARSE_MODE)
    except Exception as e:
        debug(f"[AUTH] Could not warn user {event.sender_id}: {e}")


async def check_admin_and_warn(event):
    """Devuelve True (y avisa en el log) si quien escribe no es administrador.

    Vive aquí y no en dropbot.py porque no depende de nada del bot: así los
    módulos de handlers pueden importarla sin ciclos.
    """
    if not is_admin(event.sender_id):
        sender = await event.get_sender()
        username = sender.username if sender else None
        warning(f"[AUTH] User {event.sender_id} (@{username}) is not an admin and tried to use the bot")
        return True
    return False
