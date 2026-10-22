"""FastTelethon no puede dejar conexiones abiertas si arrancar falla a medias.

Las conexiones que sí se abrían no llegaban a `senders`, así que nadie las
desconectaba: cada fallo (un DC caído, un FloodWait al exportar la
autorización) dejaba conexiones MTProto vivas que se iban acumulando.
"""

import asyncio

import pytest

from utils.fast_telethon import _ParallelTransferrer


class FakeSender:
    def __init__(self):
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


def test_si_falla_una_conexion_se_cierran_las_demas(run_async):
    transferrer = _ParallelTransferrer.__new__(_ParallelTransferrer)
    transferrer.senders = None
    first, ok = FakeSender(), FakeSender()

    async def create_first():
        return first

    async def create_ok():
        return ok

    async def create_failing():
        raise ConnectionError("DC caído")

    with pytest.raises(ConnectionError):
        run_async(transferrer._open_senders(create_first, [create_ok, create_failing]))

    assert first.disconnected and ok.disconnected
    assert transferrer.senders is None


def test_si_se_cancela_mientras_se_abren_se_cierran_las_ya_abiertas(run_async):
    """El botón Cancelar llega mientras se abren las demás conexiones.

    `return_exceptions` no protege de cancelar el propio gather: este relanzaba
    sin devolver las que ya se habían abierto, y ni ellas ni la primera se
    desconectaban.
    """
    transferrer = _ParallelTransferrer.__new__(_ParallelTransferrer)
    transferrer.senders = None
    first, ok = FakeSender(), FakeSender()

    async def scenario():
        opened = asyncio.Event()

        async def create_first():
            return first

        async def create_ok():
            opened.set()
            return ok

        async def create_slow():
            await asyncio.Event().wait()

        task = asyncio.ensure_future(
            transferrer._open_senders(create_first, [create_ok, create_slow]))
        await opened.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_async(scenario())

    assert first.disconnected and ok.disconnected
    assert transferrer.senders is None
