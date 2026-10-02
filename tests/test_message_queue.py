"""La cola de mensajes no reintenta lo que no tiene arreglo ni lo abandonado.

El worker es único: cada reintento inútil (1+2+3+4 s) paraba todos los envíos
y hacía caducar las respuestas a callbacks pendientes. Y un elemento cuyo
llamador ya había dejado de esperar se enviaba igual, duplicando el mensaje.
"""

import asyncio

from telethon.errors import MessageIdInvalidError

from message_queue import TelegramMessageQueue


def test_un_error_permanente_no_se_reintenta(run_async):
    calls = []

    async def edit():
        calls.append(1)
        raise MessageIdInvalidError(request=None)

    async def scenario():
        queue = TelegramMessageQueue(delay_between_messages=0, max_retries=5)
        future = asyncio.get_running_loop().create_future()
        await queue._execute_message({"func": edit, "args": (), "kwargs": {}, "result_future": future})
        return future

    future = run_async(scenario())
    assert len(calls) == 1
    assert isinstance(future.exception(), MessageIdInvalidError)


def test_lo_que_nadie_espera_ya_no_se_envia(run_async):
    calls = []

    async def send():
        calls.append(1)

    async def scenario():
        queue = TelegramMessageQueue(delay_between_messages=0)
        future = asyncio.get_running_loop().create_future()
        future.cancel()  # lo que hace wait_for() al agotar su timeout
        await queue._execute_message({"func": send, "args": (), "kwargs": {}, "result_future": future})

    run_async(scenario())
    assert calls == []
