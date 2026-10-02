"""FastTelethon no puede dejar conexiones abiertas si arrancar falla a medias.

Las conexiones que sí se abrían no llegaban a `senders`, así que nadie las
desconectaba: cada fallo (un DC caído, un FloodWait al exportar la
autorización) dejaba conexiones MTProto vivas que se iban acumulando.
"""

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
