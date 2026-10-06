"""Transferencias con Telegram: ruta rápida (FastTelethon) o estándar, y progreso.

`_download_to_file` y `_send_file_fast` eligen entre la descarga/subida en
paralelo de `utils.fast_telethon` y la de Telethon según
`downloads.fast_connections` y el tamaño (`FAST_TRANSFER_MIN_BYTES`), y si la
rápida falla tienen que caer a la estándar sin que el usuario lo note.
"""

import asyncio
import os

import pytest
from telethon.tl import types

MB = 1024 * 1024


def file_message(name="a.bin", size=1000):
    document = types.Document(
        id=7, access_hash=1, file_reference=b"", date=None,
        mime_type="application/octet-stream", size=size, dc_id=1,
        attributes=[types.DocumentAttributeFilename(file_name=name)],
    )
    return types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="",
                         media=types.MessageMediaDocument(document=document))


def photo_message(size):
    photo = types.Photo(id=8, access_hash=1, file_reference=b"", date=None, dc_id=1,
                        sizes=[types.PhotoSize(type="x", w=10, h=10, size=size)])
    return types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="",
                         media=types.MessageMediaPhoto(photo=photo))


class Transfers:
    """Registra por qué ruta fue cada transferencia."""

    def __init__(self):
        self.fast_downloads = []
        self.standard_downloads = []
        self.fast_uploads = []
        self.sent = []
        self.fast_error = None
        self.send_errors = []

    # --- descarga
    async def fast_download(self, client, document, out, connections, progress):
        self.fast_downloads.append(connections)
        out.write(b"medio")
        if self.fast_error is not None:
            raise self.fast_error
        out.write(b" y fin")

    async def standard_download(self, message, file=None, progress_callback=None):
        self.standard_downloads.append(file)
        with open(file, "wb") as out:
            out.write(b"estandar")

    # --- subida
    async def fast_upload(self, client, f, size, connections, progress):
        self.fast_uploads.append((size, connections))
        f.read()
        if self.fast_error is not None:
            raise self.fast_error
        return "HANDLE"

    async def send_file(self, entity, file=None, **kwargs):
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append((entity, file, kwargs))
        return "MENSAJE"


@pytest.fixture
def transfers(dropbot, monkeypatch, config_dir):
    t = Transfers()
    monkeypatch.setattr(dropbot.fast_telethon, "download_file", t.fast_download)
    monkeypatch.setattr(dropbot.fast_telethon, "upload_file", t.fast_upload)
    monkeypatch.setattr(dropbot.bot, "download_media", t.standard_download)
    monkeypatch.setattr(dropbot.bot, "send_file", t.send_file)
    return t


def _connections(dropbot, value):
    dropbot.settings.put("downloads.fast_connections", value)


# --- _download_to_file -------------------------------------------------------

class TestDescargaRapidaOEstandar:
    def test_un_fichero_grande_va_por_la_rapida(
        self, dropbot, transfers, tmp_path, run_async
    ):
        _connections(dropbot, 8)
        target = tmp_path / "out"

        run_async(dropbot._download_to_file(file_message(size=50 * MB), str(target), None))

        assert transfers.fast_downloads == [8]
        assert transfers.standard_downloads == []
        assert target.read_bytes() == b"medio y fin"

    def test_un_fichero_pequeno_va_por_la_estandar(
        self, dropbot, transfers, tmp_path, run_async
    ):
        _connections(dropbot, 8)
        size = dropbot.FAST_TRANSFER_MIN_BYTES  # el límite no entra en la rápida

        run_async(dropbot._download_to_file(file_message(size=size), str(tmp_path / "o"), None))

        assert transfers.fast_downloads == []
        assert len(transfers.standard_downloads) == 1

    def test_con_una_conexion_siempre_la_estandar(
        self, dropbot, transfers, tmp_path, run_async
    ):
        _connections(dropbot, 1)

        run_async(dropbot._download_to_file(file_message(size=500 * MB), str(tmp_path / "o"), None))

        assert transfers.fast_downloads == []
        assert len(transfers.standard_downloads) == 1

    def test_una_foto_siempre_la_estandar(self, dropbot, transfers, tmp_path, run_async):
        """La ruta rápida trabaja con Document; una foto no lo es."""
        _connections(dropbot, 8)

        run_async(dropbot._download_to_file(photo_message(50 * MB), str(tmp_path / "o"), None))

        assert transfers.fast_downloads == []
        assert len(transfers.standard_downloads) == 1

    def test_si_la_rapida_falla_cae_a_la_estandar_sin_restos(
        self, dropbot, transfers, tmp_path, run_async
    ):
        _connections(dropbot, 8)
        transfers.fast_error = ConnectionError("DC caído")
        target = tmp_path / "out"

        run_async(dropbot._download_to_file(file_message(size=50 * MB), str(target), None))

        assert transfers.fast_downloads and transfers.standard_downloads == [str(target)]
        assert target.read_bytes() == b"estandar", "no puede quedar lo de la rápida"

    def test_cancelar_la_rapida_no_cae_a_la_estandar(
        self, dropbot, transfers, tmp_path, run_async
    ):
        _connections(dropbot, 8)
        transfers.fast_error = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            run_async(dropbot._download_to_file(
                file_message(size=50 * MB), str(tmp_path / "o"), None
            ))

        assert transfers.standard_downloads == []


# --- _send_file_fast ---------------------------------------------------------

class TestSubidaRapidaOEstandar:
    def _send(self, dropbot, run_async, path, filename=None, is_video=False, progress=None):
        return run_async(dropbot._send_file_fast(
            42, str(path), filename or os.path.basename(path), ["ATTR"], None,
            is_video, progress,
        ))

    def test_un_fichero_grande_va_por_la_rapida(
        self, dropbot, transfers, media_file, run_async
    ):
        _connections(dropbot, 4)
        path = media_file("grande.bin", 20 * MB)

        result = self._send(dropbot, run_async, path)

        assert result == "MENSAJE"
        assert transfers.fast_uploads == [(20 * MB, 4)]
        (entity, file, kwargs), = transfers.sent
        assert entity == 42 and file == "HANDLE"
        assert kwargs["progress_callback"] is None, "el progreso ya lo da la subida"

    def test_un_fichero_pequeno_va_por_la_estandar(
        self, dropbot, transfers, media_file, run_async
    ):
        _connections(dropbot, 4)
        path = media_file("p.bin", 1000)

        self._send(dropbot, run_async, path, progress="CB")

        assert transfers.fast_uploads == []
        (_, file, kwargs), = transfers.sent
        assert file == path and kwargs["progress_callback"] == "CB"

    def test_con_una_conexion_siempre_la_estandar(
        self, dropbot, transfers, media_file, run_async
    ):
        _connections(dropbot, 1)
        path = media_file("grande.bin", 20 * MB)

        self._send(dropbot, run_async, path)

        assert transfers.fast_uploads == []
        assert transfers.sent[0][1] == path

    def test_si_la_rapida_falla_cae_a_la_estandar_con_los_mismos_parametros(
        self, dropbot, transfers, media_file, run_async
    ):
        _connections(dropbot, 4)
        transfers.fast_error = ConnectionError("FloodWait exportando la autorización")
        path = media_file("grande.mp4", 20 * MB)

        result = self._send(dropbot, run_async, path, is_video=True, progress="CB")

        assert result == "MENSAJE"
        (_, file, kwargs), = transfers.sent
        assert file == path
        assert kwargs["mime_type"] == "video/mp4"
        assert kwargs["supports_streaming"] is True
        assert kwargs["force_document"] is False
        assert kwargs["attributes"] == ["ATTR"]
        assert kwargs["progress_callback"] == "CB"

    def test_si_falla_el_envio_tras_la_subida_rapida_reintenta_por_la_estandar(
        self, dropbot, transfers, media_file, run_async
    ):
        _connections(dropbot, 4)
        transfers.send_errors = [RuntimeError("FILE_PARTS_INVALID")]
        path = media_file("grande.bin", 20 * MB)

        assert self._send(dropbot, run_async, path) == "MENSAJE"
        assert transfers.sent[0][1] == path

    def test_cancelar_la_subida_rapida_no_cae_a_la_estandar(
        self, dropbot, transfers, media_file, run_async
    ):
        _connections(dropbot, 4)
        transfers.fast_error = asyncio.CancelledError()
        path = media_file("grande.bin", 20 * MB)

        with pytest.raises(asyncio.CancelledError):
            self._send(dropbot, run_async, path)

        assert transfers.sent == []

    @pytest.mark.parametrize("name, is_video, mime, document, streaming", [
        ("v.mp4", True, "video/mp4", False, True),
        ("a.mp3", False, "audio/mpeg", False, None),
        ("d.pdf", False, "application/pdf", True, None),
        ("raro.zzz", False, "application/octet-stream", True, None),
        ("sin_ext", True, "video/mp4", False, True),
    ])
    def test_tipo_mime_y_forma_de_envio(
        self, dropbot, transfers, media_file, run_async,
        name, is_video, mime, document, streaming
    ):
        """Vídeo y audio con reproductor; el resto como documento."""
        _connections(dropbot, 1)
        path = media_file(name, 100)

        self._send(dropbot, run_async, path, is_video=is_video)

        kwargs = transfers.sent[0][2]
        assert kwargs["mime_type"] == mime
        assert kwargs["force_document"] is document
        assert kwargs["supports_streaming"] is streaming

    def test_pasa_la_miniatura(self, dropbot, transfers, media_file, run_async):
        _connections(dropbot, 1)
        path = media_file("v.mp4", 100)

        run_async(dropbot._send_file_fast(1, path, "v.mp4", [], "/tmp/t.jpg", True, None))
        run_async(dropbot._send_file_fast(1, path, "v.mp4", [], "", True, None))

        assert transfers.sent[0][2]["thumb"] == "/tmp/t.jpg"
        assert transfers.sent[1][2]["thumb"] is None


# --- Callbacks de progreso ---------------------------------------------------

class StatusMessage:
    def __init__(self, error=None):
        self.edits = []
        self.error = error

    async def edit(self, text, **kwargs):
        if self.error is not None:
            raise self.error
        self.edits.append((text, kwargs))


@pytest.fixture
def clock(monkeypatch):
    """Reloj del loop controlado por el test."""
    now = [1000.0]

    class Loop:
        def time(self):
            return now[0]

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: Loop())
    return now


class TestProgreso:
    def _drive(self, run_async, callback, steps):
        """Llama al callback con (current, total) avanzando el reloj."""
        async def go():
            for item in steps:
                await callback(*item)
        run_async(go())

    def test_no_edita_mas_a_menudo_que_el_intervalo(
        self, dropbot, run_async, clock, config_dir
    ):
        dropbot.settings.put("downloads.parallel", 2)  # intervalo de 10 s
        status = StatusMessage()
        callback = dropbot.create_upload_progress_callback(status, "f.bin")

        async def go():
            await callback(10, 100)        # t=1000 -> edita
            clock[0] += 5
            await callback(20, 100)        # +5 s -> no
            clock[0] += 4.9
            await callback(30, 100)        # +9.9 s -> no
            clock[0] += 0.2
            await callback(40, 100)        # +10.1 s -> edita

        run_async(go())
        assert len(status.edits) == 2
        assert "40.0" in status.edits[-1][0]

    def test_el_intervalo_crece_con_muchas_descargas(
        self, dropbot, run_async, clock, config_dir
    ):
        dropbot.settings.put("downloads.parallel", 20)  # intervalo de 30 s
        assert dropbot.progress_update_interval() == 30
        status = StatusMessage()
        callback = dropbot.create_upload_progress_callback(status, "f.bin")

        async def go():
            await callback(10, 100)
            clock[0] += 20
            await callback(20, 100)

        run_async(go())
        assert len(status.edits) == 1

    def test_el_100_por_cien_se_muestra_siempre(self, dropbot, run_async, clock):
        status = StatusMessage()
        callback = dropbot.create_upload_progress_callback(status, "f.bin")

        async def go():
            await callback(10, 100)
            clock[0] += 1
            await callback(100, 100)

        run_async(go())
        assert len(status.edits) == 2
        assert "100.0" in status.edits[-1][0]

    def test_muestra_velocidad_y_tiempo_restante(self, dropbot, run_async, clock):
        status = StatusMessage()
        callback = dropbot.create_upload_progress_callback(status, "f.bin")

        async def go():
            await callback(0, 100 * MB)
            clock[0] += 10
            await callback(10 * MB, 100 * MB)   # 1 MB/s, quedan 90 MB

        run_async(go())
        text = status.edits[-1][0]
        assert "1.0MB/s" in text
        assert "01:30" in text

    def test_si_el_contador_vuelve_a_cero_no_inventa_velocidad(
        self, dropbot, run_async, clock
    ):
        """Pasar de la descarga rápida a la estándar reinicia el contador."""
        status = StatusMessage()
        callback = dropbot.create_upload_progress_callback(status, "f.bin")

        async def go():
            await callback(50, 100)
            clock[0] += 20
            await callback(5, 100)

        run_async(go())
        assert "N/A" in status.edits[-1][0]

    def test_la_descarga_lleva_boton_de_cancelar_y_la_subida_no(
        self, dropbot, make_event, run_async, clock
    ):
        down, up = StatusMessage(), StatusMessage()
        download_cb = dropbot.create_progress_callback(down, make_event(event_id=9), "f")
        upload_cb = dropbot.create_upload_progress_callback(up, "f")

        run_async(download_cb(1, 2))
        run_async(upload_cb(1, 2))

        assert "buttons" in down.edits[0][1] and "buttons" not in up.edits[0][1]
        assert "Descargando" in down.edits[0][0]
        assert "Enviando" in up.edits[0][0]

    def test_si_el_mensaje_se_borro_deja_de_editar(self, dropbot, run_async, clock):
        status = StatusMessage(error=RuntimeError("The specified message ID is invalid"))
        callback = dropbot.create_upload_progress_callback(status, "f")
        attempts = []
        real_edit = status.edit

        async def counting_edit(text, **kwargs):
            attempts.append(text)
            return await real_edit(text, **kwargs)

        status.edit = counting_edit

        async def go():
            await callback(10, 100)
            clock[0] += 60
            await callback(50, 100)
            await callback(100, 100)

        run_async(go())
        assert len(attempts) == 1, "tras MESSAGE_ID_INVALID no se vuelve a intentar"

    def test_otros_errores_al_editar_no_paran_el_progreso(self, dropbot, run_async, clock):
        status = StatusMessage(error=RuntimeError("FloodWait 3s"))
        callback = dropbot.create_upload_progress_callback(status, "f")
        attempts = []
        real_edit = status.edit

        async def counting_edit(text, **kwargs):
            attempts.append(text)
            return await real_edit(text, **kwargs)

        status.edit = counting_edit

        async def go():
            await callback(10, 100)
            clock[0] += 60
            await callback(50, 100)

        run_async(go())
        assert len(attempts) == 2

    def test_sin_mensaje_de_estado_no_hace_nada(self, dropbot, run_async, clock):
        callback = dropbot.create_upload_progress_callback(None, "f")
        run_async(callback(1, 2))  # no revienta

    def test_un_total_cero_no_revienta(self, dropbot, run_async, clock):
        status = StatusMessage()
        callback = dropbot.create_upload_progress_callback(status, "f")
        run_async(callback(0, 0))

    def test_cada_transferencia_lleva_su_propio_reloj(self, dropbot, run_async, clock):
        """Dos descargas en paralelo no se roban el turno de edición."""
        first, second = StatusMessage(), StatusMessage()
        cb1 = dropbot.create_upload_progress_callback(first, "a")
        cb2 = dropbot.create_upload_progress_callback(second, "b")

        async def go():
            await cb1(10, 100)
            await cb2(10, 100)

        run_async(go())
        assert len(first.edits) == 1 and len(second.edits) == 1
