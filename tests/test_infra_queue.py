"""Cola de mensajes a Telegram y los `safe_*` que la usan.

El worker es único y todo lo que el bot dice pasa por él: un reintento de más
bloquea a todos, uno de menos pierde un mensaje, y ejecutar algo que nadie
espera ya lo duplica. El paso del tiempo se simula sustituyendo
`asyncio.sleep`, así que las esperas de minutos se comprueban al instante.
"""

import asyncio
from types import SimpleNamespace

import pytest
from telethon.errors import (
    BadRequestError, FloodWaitError, ForbiddenError, UnauthorizedError,
)

import basic
import message_queue as mq
from message_queue import PERMANENT_ERRORS, TelegramMessageQueue
from utils import telegram_helpers


@pytest.fixture
def slept(monkeypatch):
    """Sustituye `asyncio.sleep` por uno que anota la espera y no duerme."""
    real_sleep = asyncio.sleep
    waits = []

    async def fake_sleep(delay, result=None):
        waits.append(delay)
        await real_sleep(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return waits


@pytest.fixture
def short_wait(monkeypatch):
    """Convierte los 300 s que espera `add_message` en unas centésimas."""
    real_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, 0.05 if timeout == 300 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", wait_for)


def _item(func, *args, future=None, **kwargs):
    return {"func": func, "args": args, "kwargs": kwargs, "result_future": future}


class Flaky:
    """Una llamada que falla con `errors` en orden y después devuelve "ok"."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


async def _execute(queue, func):
    future = asyncio.get_running_loop().create_future()
    await queue._execute_message(_item(func, future=future))
    return future


class TestReintentos:
    def test_un_error_cualquiera_se_reintenta_con_espera_lineal(self, run_async, slept):
        func = Flaky(*[RuntimeError("caído")] * 5)

        async def scenario():
            future = await _execute(TelegramMessageQueue(max_retries=5), func)
            return future.exception()

        assert isinstance(run_async(scenario()), RuntimeError)
        assert func.calls == 5
        # 1+2+3+4: tras el último intento ya no se espera
        assert slept == [1, 2, 3, 4]

    def test_si_un_reintento_sale_bien_devuelve_su_resultado(self, run_async, slept):
        func = Flaky(RuntimeError("una vez"))

        async def scenario():
            return (await _execute(TelegramMessageQueue(), func)).result()

        assert run_async(scenario()) == "ok"
        assert func.calls == 2

    def test_flood_wait_espera_lo_que_pide_telegram(self, run_async, slept):
        func = Flaky(FloodWaitError(request=None, capture=37))

        async def scenario():
            return (await _execute(TelegramMessageQueue(), func)).result()

        assert run_async(scenario()) == "ok"
        assert slept == [37]

    def test_flood_wait_persistente_acaba_en_error_sin_reintentar_mas(self, run_async, slept):
        func = Flaky(*[FloodWaitError(request=None, capture=5)] * 10)

        async def scenario():
            return (await _execute(TelegramMessageQueue(max_retries=3), func)).exception()

        assert isinstance(run_async(scenario()), FloodWaitError)
        assert func.calls == 3
        assert slept == [5, 5]

    def test_un_flood_sin_segundos_usa_backoff_exponencial(self, run_async, slept):
        func = Flaky(RuntimeError("Flood control exceeded"), RuntimeError("flood again"))

        async def scenario():
            return (await _execute(TelegramMessageQueue(), func)).result()

        assert run_async(scenario()) == "ok"
        assert slept == [2, 4]

    def test_un_flood_cuyos_segundos_fallan_usa_backoff(self, run_async, slept):
        class FloodWaitError(Exception):
            @property
            def seconds(self):
                raise ValueError("sin segundos")

        func = Flaky(FloodWaitError("espera"))

        async def scenario():
            return (await _execute(TelegramMessageQueue(), func)).result()

        assert run_async(scenario()) == "ok"
        assert slept == [2]

    @pytest.mark.parametrize("message", ["HTTP 429", "Too Many Requests"])
    def test_un_429_usa_backoff_exponencial(self, run_async, slept, message):
        func = Flaky(*[RuntimeError(message)] * 5)

        async def scenario():
            return (await _execute(TelegramMessageQueue(max_retries=5), func)).exception()

        assert isinstance(run_async(scenario()), RuntimeError)
        assert func.calls == 5
        assert slept == [2, 4, 8, 16]

    @pytest.mark.parametrize("error", [
        BadRequestError(request=None, message="MESSAGE_NOT_MODIFIED"),
        ForbiddenError(request=None, message="CHAT_WRITE_FORBIDDEN"),
        UnauthorizedError(request=None, message="AUTH_KEY_UNREGISTERED"),
    ])
    def test_los_errores_permanentes_no_se_reintentan_ni_esperan(self, run_async, slept, error):
        assert isinstance(error, PERMANENT_ERRORS)
        func = Flaky(error)

        async def scenario():
            return (await _execute(TelegramMessageQueue(), func)).exception()

        assert run_async(scenario()) is error
        assert func.calls == 1
        assert slept == []

    def test_si_el_llamador_deja_de_esperar_durante_un_reintento_no_se_repite(self, run_async, monkeypatch):
        """El future se cancela mientras el worker duerme entre intentos."""
        real_sleep = asyncio.sleep
        state = {}

        async def sleep_and_give_up(delay, result=None):
            state["future"].cancel()
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", sleep_and_give_up)
        func = Flaky(RuntimeError("caído"))

        async def scenario():
            state["future"] = asyncio.get_running_loop().create_future()
            await TelegramMessageQueue()._execute_message(_item(func, future=state["future"]))

        run_async(scenario())
        assert func.calls == 1

    def test_sin_future_un_error_final_no_lanza(self, run_async, slept):
        func = Flaky(*[RuntimeError("caído")] * 2)
        result = run_async(TelegramMessageQueue(max_retries=2)._execute_message(_item(func)))
        assert result is None
        assert func.calls == 2


class TestWorker:
    def test_procesa_en_orden_con_retraso_entre_mensajes(self, run_async, slept):
        done = []

        async def send(text):
            done.append(text)
            return text

        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0.5)
            await queue.start()
            for text in ("uno", "dos", "tres"):
                await queue.add_message(send, text)
            last = await queue.add_message(send, "cuatro", wait_for_result=True)
            await queue.shutdown()
            return last

        assert run_async(scenario()) == "cuatro"
        assert done == ["uno", "dos", "tres", "cuatro"]
        assert slept == [0.5] * 4

    def test_pasa_args_y_kwargs_a_la_funcion(self, run_async, slept):
        async def send(*args, **kwargs):
            return args, kwargs

        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            await queue.start()
            result = await queue.add_message(send, 1, 2, wait_for_result=True, parse_mode="html")
            await queue.shutdown()
            return result

        assert run_async(scenario()) == ((1, 2), {"parse_mode": "html"})

    def test_sin_esperar_resultado_devuelve_none_al_momento(self, run_async, slept):
        async def send():
            return "enviado"

        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            result = await queue.add_message(send)
            return result, queue.queue.qsize()

        assert run_async(scenario()) == (None, 1)

    def test_esperar_resultado_propaga_el_error(self, run_async, slept):
        async def edit():
            raise BadRequestError(request=None, message="MESSAGE_ID_INVALID")

        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            await queue.start()
            try:
                await queue.add_message(edit, wait_for_result=True)
            finally:
                await queue.shutdown()

        with pytest.raises(BadRequestError):
            run_async(scenario())

    def test_un_timeout_esperando_devuelve_none_y_no_se_envia_despues(self, run_async, slept, short_wait):
        """Sin worker, el llamador se cansa; al arrancar, ese mensaje ya no sale."""
        calls = []

        async def send():
            calls.append(1)
            return "tarde"

        async def marker():
            return "fin"

        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            result = await queue.add_message(send, wait_for_result=True)
            await queue.start()
            assert await queue.add_message(marker, wait_for_result=True) == "fin"
            await queue.shutdown()
            return result

        assert run_async(scenario()) is None
        assert calls == []

    def test_un_elemento_roto_no_para_el_worker(self, run_async, slept):
        async def send():
            return "ok"

        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            await queue.start()
            await queue.queue.put({"args": ()})  # sin 'func': KeyError en el worker
            result = await queue.add_message(send, wait_for_result=True)
            await queue.shutdown()
            return result

        assert run_async(scenario()) == "ok"

    def test_start_dos_veces_no_crea_dos_workers(self, run_async):
        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            await queue.start()
            first = queue.worker_task
            await queue.start()
            same = queue.worker_task is first
            await queue.shutdown()
            return same, first.done()

        assert run_async(scenario()) == (True, True)

    def test_shutdown_sin_arrancar_no_se_cuelga(self, run_async):
        async def scenario():
            queue = TelegramMessageQueue()
            await asyncio.wait_for(queue.shutdown(), timeout=1)
            return queue.running

        assert run_async(scenario()) is False

    def test_shutdown_dos_veces_no_se_cuelga(self, run_async):
        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            await queue.start()
            await asyncio.wait_for(queue.shutdown(), timeout=1)
            await asyncio.wait_for(queue.shutdown(), timeout=1)

        run_async(scenario())

    def test_shutdown_con_la_cola_llena_para_sin_esperar_a_todo(self, run_async, slept):
        """Parar no puede depender de vaciar una cola de cientos de envíos:
        dropbot le da 10 s y el bot ya está desconectado."""
        done = []
        gate = {}

        async def send(index):
            if index == 0:
                await gate["release"].wait()
            done.append(index)

        async def scenario():
            gate["release"] = asyncio.Event()
            queue = TelegramMessageQueue(delay_between_messages=0)
            await queue.start()
            for index in range(100):
                await queue.add_message(send, index)
            await asyncio.sleep(0)  # el worker coge el primero y se queda esperando
            stopping = asyncio.ensure_future(queue.shutdown())
            await asyncio.sleep(0)
            gate["release"].set()
            await asyncio.wait_for(stopping, timeout=1)
            return queue.worker_task.done()

        assert run_async(scenario()) is True
        assert done[0] == 0
        assert len(done) < 100

    def test_parar_y_volver_a_arrancar_sigue_enviando(self, run_async, slept, short_wait):
        async def send():
            return "ok"

        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            await queue.start()
            await queue.shutdown()
            await queue.start()
            try:
                return await queue.add_message(send, wait_for_result=True)
            finally:
                await queue.shutdown()

        assert run_async(scenario()) == "ok"

    def test_shutdown_libera_a_quien_esperaba_un_mensaje_sin_enviar(self, run_async, slept):
        """Lo que queda en la cola al parar no se envía: quien esperaba su
        resultado recibe None en el acto en lugar de esperar 300 s."""
        sent = []
        gate = {}

        async def blocking():
            await gate["release"].wait()

        async def send():
            sent.append(1)
            return "ok"

        async def scenario():
            gate["release"] = asyncio.Event()
            queue = TelegramMessageQueue(delay_between_messages=0)
            await queue.start()
            await queue.add_message(blocking)
            waiting = asyncio.ensure_future(queue.add_message(send, wait_for_result=True))
            await asyncio.sleep(0)  # el worker se queda en el primero
            stopping = asyncio.ensure_future(queue.shutdown())
            await asyncio.sleep(0)
            gate["release"].set()
            await asyncio.wait_for(stopping, timeout=1)
            return await asyncio.wait_for(waiting, timeout=1), queue.queue.empty()

        assert run_async(scenario()) == (None, True)
        assert sent == []

    def test_cancelar_a_quien_espera_sigue_propagandose(self, run_async):
        """La cancelación de la propia tarea que espera no se convierte en None."""
        async def never():
            await asyncio.Event().wait()

        async def scenario():
            queue = TelegramMessageQueue(delay_between_messages=0)
            waiting = asyncio.ensure_future(queue.add_message(never, wait_for_result=True))
            await asyncio.sleep(0)
            waiting.cancel()
            try:
                await waiting
            except asyncio.CancelledError:
                return "cancelada"
            return "devolvió"

        assert run_async(scenario()) == "cancelada"


class FakeQueue:
    def __init__(self):
        self.calls = []

    async def add_message(self, func, *args, wait_for_result=False, **kwargs):
        self.calls.append((func, args, wait_for_result, kwargs))
        return "resultado"


class FakeTarget:
    """Un mensaje, un evento o el bot: solo importa qué método se encola."""

    async def edit(self, *a, **k): ...
    async def reply(self, *a, **k): ...
    async def respond(self, *a, **k): ...
    async def answer(self, *a, **k): ...
    async def delete(self, *a, **k): ...
    async def send_message(self, *a, **k): ...
    async def send_file(self, *a, **k): ...


class TestSafeHelpers:
    @pytest.fixture
    def queue(self, monkeypatch):
        fake, bot = FakeQueue(), FakeTarget()
        # monkeypatch guarda lo que había; init() pone lo del test
        monkeypatch.setattr(telegram_helpers, "_message_queue", None)
        monkeypatch.setattr(telegram_helpers, "_bot", None)
        telegram_helpers.init(fake, bot)
        fake.bot = bot
        return fake

    @pytest.mark.parametrize("helper, method", [
        ("safe_edit", "edit"), ("safe_reply", "reply"), ("safe_respond", "respond"),
        ("safe_answer", "answer"), ("safe_delete", "delete"),
    ])
    def test_cada_safe_encola_el_metodo_de_su_objeto(self, queue, run_async, helper, method):
        target = FakeTarget()
        result = run_async(getattr(telegram_helpers, helper)(target, "texto", wait_for_result=True, buttons=1))

        assert result == "resultado"
        func, args, wait, kwargs = queue.calls[0]
        assert func == getattr(target, method)
        assert args == ("texto",)
        assert wait is True
        assert kwargs == {"buttons": 1}

    @pytest.mark.parametrize("helper, method", [
        ("safe_send_message", "send_message"), ("safe_send_file", "send_file"),
    ])
    def test_los_envios_a_un_chat_usan_el_bot_inyectado(self, queue, run_async, helper, method):
        run_async(getattr(telegram_helpers, helper)(42, "x", parse_mode="html"))

        func, args, wait, kwargs = queue.calls[0]
        assert func == getattr(queue.bot, method)
        assert args == (42, "x")
        assert wait is False
        assert kwargs == {"parse_mode": "html"}


class TestAdmins:
    @pytest.fixture
    def warnings(self, monkeypatch):
        logged = []
        monkeypatch.setattr(telegram_helpers, "warning", logged.append)
        return logged

    @staticmethod
    def _event(sender_id, sender):
        async def get_sender():
            return sender
        return SimpleNamespace(sender_id=sender_id, get_sender=get_sender)

    def test_un_admin_pasa_sin_avisos(self, run_async, monkeypatch, warnings):
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "111,222")
        event = self._event(222, None)
        assert run_async(telegram_helpers.check_admin_and_warn(event)) is False
        assert warnings == []

    def test_otro_usuario_queda_fuera_y_se_avisa(self, run_async, monkeypatch, warnings):
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "111,222")
        event = self._event(333, SimpleNamespace(username="intruso"))
        assert run_async(telegram_helpers.check_admin_and_warn(event)) is True
        assert "333" in warnings[0] and "@intruso" in warnings[0]

    def test_sin_sender_tambien_queda_fuera(self, run_async, monkeypatch, warnings):
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "111")
        event = self._event(333, None)
        assert run_async(telegram_helpers.check_admin_and_warn(event)) is True
        assert "@None" in warnings[0]

    def test_un_id_que_solo_coincide_en_parte_no_es_admin(self, monkeypatch):
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "12345")
        assert not basic.is_admin(1234)
        assert not basic.is_admin(123456)
        assert basic.is_admin("12345")

    def test_varios_admins_con_espacios_tras_la_coma(self, monkeypatch):
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "111, 222")
        assert basic.is_admin(222)


def test_el_modulo_de_la_cola_usa_asyncio_sleep_del_modulo(slept):
    """Garantía para los tests de arriba: la cola duerme con `asyncio.sleep`."""
    assert mq.asyncio.sleep is asyncio.sleep
