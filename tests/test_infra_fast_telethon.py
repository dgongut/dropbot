"""Transferencia paralela (FastTelethon) contra un cliente de Telegram falso.

El cliente falso implementa justo lo que usa `utils.fast_telethon`: la
sesión, `_get_dc`, `_connection`, `_call` con GetFile/SaveFilePart y la
exportación de la autorización; `MTProtoSender` se sustituye por un doble.
Así se comprueba, sin red, que lo descargado es byte a byte el original y que
lo subido llega en las partes correctas.
"""

import asyncio
import hashlib
import io
from types import SimpleNamespace

import pytest
from telethon.tl import types
from telethon.tl.functions.upload import (
    GetFileRequest, SaveBigFilePartRequest, SaveFilePartRequest,
)

from utils import fast_telethon
from utils.fast_telethon import connection_count

MB = 1024 * 1024


class FakeSender:
    """Sustituto de MTProtoSender: anota su vida y la autorización."""

    instances = []

    def __init__(self, auth_key, loggers=None):
        self.auth_key = auth_key
        self.connected = False
        self.disconnected = False
        self.sent = []
        FakeSender.instances.append(self)

    async def connect(self, connection):
        self.connected = True

    async def send(self, request):
        self.sent.append(request)
        self.auth_key = "clave-exportada"

    async def disconnect(self):
        self.disconnected = True


class FakeClient:
    def __init__(self, data=b"", dc_id=2, short_after=None, fail_part=None):
        self.data = data
        self.session = SimpleNamespace(dc_id=dc_id, auth_key="clave-principal")
        self._log = {}
        self._proxy = None
        self._local_addr = None
        self._init_request = SimpleNamespace(query=None)
        self.exported = 0
        self.parts = []
        self.short_after = short_after
        self.fail_part = fail_part

    async def _get_dc(self, dc_id):
        return SimpleNamespace(ip_address="127.0.0.1", port=443, id=dc_id)

    def _connection(self, *args, **kwargs):
        return "conexion"

    async def __call__(self, request):
        self.exported += 1
        return SimpleNamespace(id=1, bytes=b"auth")

    async def _call(self, sender, request):
        await asyncio.sleep(0)
        if isinstance(request, GetFileRequest):
            if self.short_after is not None and request.offset >= self.short_after:
                return SimpleNamespace(bytes=b"")
            return SimpleNamespace(bytes=self.data[request.offset:request.offset + request.limit])
        if isinstance(request, (SaveFilePartRequest, SaveBigFilePartRequest)):
            if request.file_part == self.fail_part:
                raise ConnectionError(f"parte {request.file_part} perdida")
            self.parts.append((type(request).__name__, request.file_part, bytes(request.bytes)))
            return True
        raise AssertionError(f"petición inesperada: {request!r}")


@pytest.fixture(autouse=True)
def fake_mtproto(monkeypatch):
    FakeSender.instances = []
    monkeypatch.setattr(fast_telethon, "MTProtoSender", FakeSender)
    return FakeSender


def _payload(size):
    pattern = bytes(range(251))  # primo: ninguna parte se parece a la anterior
    return (pattern * (size // len(pattern) + 1))[:size]


def _document(size, dc_id=2):
    return types.Document(id=1, access_hash=2, file_reference=b"ref", date=None,
                          mime_type="video/mp4", size=size, dc_id=dc_id, attributes=[])


class TestConexiones:
    @pytest.mark.parametrize("size, maximum, expected", [
        (0, 8, 1),
        (1, 8, 1),
        (12 * MB, 8, 1),
        (13 * MB, 8, 2),
        (50 * MB, 8, 4),
        (100 * MB, 8, 8),
        (100 * MB + 1, 8, 8),
        (5000 * MB, 8, 8),
        (50 * MB, 1, 1),
        (99 * MB, 20, 20),
    ])
    def test_escalan_con_el_tamano(self, size, maximum, expected):
        assert connection_count(size, maximum) == expected

    def test_full_size_configurable(self):
        assert connection_count(10, 4, full_size=10) == 4
        assert connection_count(5, 4, full_size=10) == 2

    def test_nunca_supera_el_maximo(self):
        for size in range(0, 300 * MB, 7 * MB):
            assert 1 <= connection_count(size, 6) <= 6


class TestDescarga:
    @pytest.mark.parametrize("size, maximum", [
        (0, 8),
        (1000, 8),
        (128 * 1024, 8),          # justo una parte
        (3 * MB + 17, 8),         # una conexión, última parte corta
        (40 * MB + 333, 8),       # cuatro conexiones, reparto desigual
    ])
    def test_reconstruye_el_fichero_byte_a_byte(self, run_async, size, maximum):
        data = _payload(size)
        client = FakeClient(data)
        out = io.BytesIO()
        progress = []

        run_async(fast_telethon.download_file(client, _document(size), out, maximum,
                                              lambda done, total: progress.append((done, total))))

        assert out.getvalue() == data
        if size:
            assert progress[-1] == (size, size)
            assert [done for done, _ in progress] == sorted(done for done, _ in progress)
        senders = FakeSender.instances
        assert len(senders) == connection_count(size, maximum)
        assert all(s.disconnected for s in senders)

    def test_un_progress_asincrono_se_espera(self, run_async):
        size = 300 * 1024
        seen = []

        async def progress(done, total):
            seen.append(done)

        run_async(fast_telethon.download_file(FakeClient(_payload(size)), _document(size), io.BytesIO(), 4, progress))
        assert seen[-1] == size

    def test_en_el_mismo_dc_reutiliza_la_autorizacion(self, run_async):
        size = 40 * MB
        client = FakeClient(_payload(size), dc_id=2)
        run_async(fast_telethon.download_file(client, _document(size, dc_id=2), io.BytesIO(), 8))
        assert client.exported == 0
        assert {s.auth_key for s in FakeSender.instances} == {"clave-principal"}

    def test_en_otro_dc_exporta_la_autorizacion_una_vez(self, run_async):
        size = 40 * MB
        client = FakeClient(_payload(size), dc_id=2)
        run_async(fast_telethon.download_file(client, _document(size, dc_id=4), io.BytesIO(), 8))

        assert client.exported == 1
        first, *rest = FakeSender.instances
        assert len(first.sent) == 1
        assert all(s.auth_key == "clave-exportada" and not s.sent for s in rest)

    def test_si_la_autorizacion_falla_la_conexion_se_cierra(self, run_async):
        size = 40 * MB
        client = FakeClient(_payload(size), dc_id=2)

        class RefusingClient(FakeClient):
            async def __call__(self, request):
                raise PermissionError("DC_AUTH_FAILED")

        client.__class__ = RefusingClient

        with pytest.raises(PermissionError):
            run_async(fast_telethon.download_file(client, _document(size, dc_id=4), io.BytesIO(), 8))
        assert len(FakeSender.instances) == 1
        assert FakeSender.instances[0].disconnected

    def test_un_fallo_a_mitad_se_propaga_y_cierra_todo(self, run_async):
        size = 40 * MB
        client = FakeClient(_payload(size))
        real_call = client._call
        calls = {"n": 0}

        async def flaky(sender, request):
            calls["n"] += 1
            if calls["n"] == 20:
                raise ConnectionResetError("conexión perdida")
            return await real_call(sender, request)

        client._call = flaky
        with pytest.raises(ConnectionResetError):
            run_async(fast_telethon.download_file(client, _document(size), io.BytesIO(), 8))
        assert all(s.disconnected for s in FakeSender.instances)

    def test_si_faltan_bytes_falla_en_vez_de_guardar_truncado(self, run_async):
        """Telegram devuelve la última parte más corta de lo anunciado."""
        size = 300 * 1024
        data = _payload(size - 10)  # el fichero real es 10 bytes más corto
        with pytest.raises(ValueError, match="Incomplete"):
            run_async(fast_telethon.download_file(FakeClient(data), _document(size), io.BytesIO(), 1))

    def test_una_parte_vacia_antes_de_tiempo_falla_sin_colgarse(self, run_async):
        size = 1 * MB
        data = _payload(size)
        client = FakeClient(data, short_after=512 * 1024)

        async def scenario():
            await asyncio.wait_for(
                fast_telethon.download_file(client, _document(size), io.BytesIO(), 1), timeout=0.5)

        with pytest.raises(ValueError, match="Incomplete"):
            run_async(scenario())


class TestSubida:
    @staticmethod
    def _rebuild(parts):
        return b"".join(chunk for _, _, chunk in sorted(parts, key=lambda p: p[1]))

    @pytest.mark.parametrize("size, maximum", [
        (1000, 8),
        (512 * 1024, 8),
        (3 * MB + 5, 8),
    ])
    def test_un_fichero_pequeno_lleva_md5_y_partes_en_orden(self, run_async, size, maximum):
        data = _payload(size)
        client = FakeClient()
        progress = []

        handle = run_async(fast_telethon.upload_file(
            client, io.BytesIO(data), size, maximum, lambda done, total: progress.append(done)))

        assert isinstance(handle, types.InputFile)
        assert handle.md5_checksum == hashlib.md5(data).hexdigest()
        assert handle.parts == len(client.parts)
        assert [p[1] for p in client.parts] == list(range(handle.parts))
        assert {p[0] for p in client.parts} == {"SaveFilePartRequest"}
        assert self._rebuild(client.parts) == data
        assert progress[-1] == size
        assert all(s.disconnected for s in FakeSender.instances)

    def test_un_fichero_grande_usa_partes_grandes_y_varias_conexiones(self, run_async):
        size = 30 * MB + 1234
        data = _payload(size)
        client = FakeClient()

        handle = run_async(fast_telethon.upload_file(client, io.BytesIO(data), size, 8))

        assert isinstance(handle, types.InputFileBig)
        assert {p[0] for p in client.parts} == {"SaveBigFilePartRequest"}
        assert sorted(p[1] for p in client.parts) == list(range(handle.parts))
        assert self._rebuild(client.parts) == data
        assert len(FakeSender.instances) == connection_count(size, 8)
        assert all(s.disconnected for s in FakeSender.instances)

    def test_un_stream_que_entrega_trozos_cortos_se_reagrupa(self, run_async):
        """Un pipe o un bind mount lento devuelven menos de lo pedido."""
        size = 3 * MB + 7
        data = _payload(size)

        class Trickle(io.BytesIO):
            def read(self, n=-1):
                return super().read(min(n, 100_000))

        client = FakeClient()
        handle = run_async(fast_telethon.upload_file(client, Trickle(data), size, 8))

        part_size = 128 * 1024
        assert all(len(chunk) == part_size for _, _, chunk in client.parts[:-1])
        assert self._rebuild(client.parts) == data
        assert handle.md5_checksum == hashlib.md5(data).hexdigest()

    def test_un_progress_asincrono_se_espera(self, run_async):
        seen = []

        async def progress(done, total):
            seen.append(done)

        run_async(fast_telethon.upload_file(FakeClient(), io.BytesIO(b"x" * 1000), 1000, 2, progress))
        assert seen == [1000]

    def test_un_fallo_a_mitad_se_propaga_y_cierra_todo(self, run_async):
        size = 3 * MB
        client = FakeClient(fail_part=3)
        with pytest.raises(ConnectionError):
            run_async(fast_telethon.upload_file(client, io.BytesIO(_payload(size)), size, 8))

    @pytest.mark.parametrize("fail_part", [3, "ultima"])
    def test_una_subida_fallida_cierra_sus_conexiones(self, run_async, fail_part):
        size = 3 * MB
        part_count = -(-size // (128 * 1024))
        client = FakeClient(fail_part=part_count - 1 if fail_part == "ultima" else fail_part)
        with pytest.raises(ConnectionError):
            run_async(fast_telethon.upload_file(client, io.BytesIO(_payload(size)), size, 8))
        assert FakeSender.instances
        assert all(s.disconnected for s in FakeSender.instances)

    def test_si_falla_la_ultima_parte_la_subida_falla(self, run_async):
        size = 3 * MB
        part_count = -(-size // (128 * 1024))
        client = FakeClient(fail_part=part_count - 1)
        with pytest.raises(ConnectionError):
            run_async(fast_telethon.upload_file(client, io.BytesIO(_payload(size)), size, 8))
