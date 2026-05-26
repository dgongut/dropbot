"""
Funciones wrapper para enviar mensajes a Telegram a través de la cola
y evitar rate limiting.

Las dependencias (message_queue y bot) deben inyectarse llamando a
`init(message_queue, bot)` antes de usar cualquiera de las funciones safe_*.
"""

_message_queue = None
_bot = None


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
    """Responde a un callback query usando event.answer y la cola para evitar rate limiting"""
    return await _message_queue.add_message(event.answer, *args, wait_for_result=wait_for_result, **kwargs)


async def safe_delete(message, *args, wait_for_result=False, **kwargs):
    """Elimina un mensaje usando la cola para evitar rate limiting"""
    return await _message_queue.add_message(message.delete, *args, wait_for_result=wait_for_result, **kwargs)


async def safe_send_message(chat_id, *args, wait_for_result=False, **kwargs):
    """Envía un mensaje usando la cola para evitar rate limiting"""
    return await _message_queue.add_message(_bot.send_message, chat_id, *args, wait_for_result=wait_for_result, **kwargs)


async def safe_send_file(chat_id, *args, wait_for_result=False, **kwargs):
    """Envía un archivo usando la cola para evitar rate limiting"""
    return await _message_queue.add_message(_bot.send_file, chat_id, *args, wait_for_result=wait_for_result, **kwargs)
