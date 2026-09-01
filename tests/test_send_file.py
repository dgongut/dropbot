"""send_file_to_telegram: metadatos, limpieza de temporales y borrado."""

import asyncio
import os
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo


@pytest.fixture
def sender(quiet_bot, monkeypatch, tmp_path):
    """Prepara send_file_to_telegram con ffmpeg y la subida simuladas.

    Devuelve un objeto con lo que se le pasó a `_send_file_fast` y con puntos
    de control para forzar conversión, thumbnail o fallo de subida.
    """
    class Harness:
        def __init__(self):
            self.uploaded = None
            self.converted_to = None   # ruta que devuelve la conversión
            self.thumbnail = None
            self.duration = (None, None, None)
            self.upload_error = None

        @property
        def attributes(self):
            return self.uploaded["attributes"] if self.uploaded else []

        def attribute(self, cls):
            return next((a for a in self.attributes if isinstance(a, cls)), None)

    harness = Harness()

    async def convert(path, status_message=None):
        return harness.converted_to if harness.converted_to is not None else path

    async def metadata(path):
        return harness.duration

    async def thumbnail(path):
        return harness.thumbnail

    async def upload(entity, path, filename, attributes, thumb, is_video, callback):
        if harness.upload_error is not None:
            raise harness.upload_error
        harness.uploaded = {
            "path": path, "filename": filename, "attributes": attributes,
            "thumb": thumb, "is_video": is_video,
        }
        return MagicMock(id=123)

    monkeypatch.setattr(quiet_bot, "convert_video_to_telegram_compatible", convert)
    monkeypatch.setattr(quiet_bot, "get_video_metadata", metadata)
    monkeypatch.setattr(quiet_bot, "generate_video_thumbnail", thumbnail)
    monkeypatch.setattr(quiet_bot, "_send_file_fast", upload)

    harness.dropbot = quiet_bot
    return harness


class TestMetadatos:
    def test_el_video_lleva_duracion_y_resolucion(
        self, sender, make_event, media_file, run_async
    ):
        sender.duration = (90, 1280, 720)

        run_async(sender.dropbot.send_file_to_telegram(make_event(), media_file("v.mp4")))

        attribute = sender.attribute(DocumentAttributeVideo)
        assert attribute is not None
        assert (attribute.duration, attribute.w, attribute.h) == (90, 1280, 720)
        assert attribute.supports_streaming is True

    def test_el_audio_lleva_duracion(self, sender, make_event, media_file, run_async):
        """Sin el atributo, Telegram lo muestra como adjunto sin reproductor."""
        sender.duration = (240, None, None)

        run_async(sender.dropbot.send_file_to_telegram(make_event(), media_file("a.mp3")))

        attribute = sender.attribute(DocumentAttributeAudio)
        assert attribute is not None and attribute.duration == 240

    def test_el_documento_no_lleva_metadatos_de_medios(
        self, sender, make_event, media_file, run_async
    ):
        run_async(sender.dropbot.send_file_to_telegram(make_event(), media_file("d.pdf")))

        assert sender.attribute(DocumentAttributeVideo) is None
        assert sender.attribute(DocumentAttributeAudio) is None

    def test_el_video_convertido_se_anuncia_como_mp4(
        self, sender, make_event, media_file, run_async
    ):
        sender.converted_to = media_file("convertido.mp4")

        run_async(sender.dropbot.send_file_to_telegram(make_event(), media_file("original.mkv")))

        assert sender.uploaded["filename"] == "original.mp4"


class TestLimpiezaDeTemporales:
    def test_se_limpian_tras_un_envio_correcto(
        self, sender, make_event, media_file, run_async
    ):
        original = media_file("v.mkv")
        sender.converted_to = media_file("v_telegram.mp4")
        sender.thumbnail = media_file("v_thumb.jpg")

        run_async(sender.dropbot.send_file_to_telegram(make_event(), original))

        assert not os.path.exists(sender.converted_to)
        assert not os.path.exists(sender.thumbnail)
        assert os.path.exists(original), "el original no se toca sin delete_after"

    def test_se_limpian_tras_un_error(self, sender, make_event, media_file, run_async):
        original = media_file("v.mkv")
        sender.converted_to = media_file("v_telegram.mp4")
        sender.thumbnail = media_file("v_thumb.jpg")
        sender.upload_error = RuntimeError("la subida falló")

        result = run_async(sender.dropbot.send_file_to_telegram(make_event(), original))

        assert result is None
        assert not os.path.exists(sender.converted_to)
        assert not os.path.exists(sender.thumbnail)
        assert os.path.exists(original)

    def test_se_limpian_tras_una_cancelacion(
        self, sender, make_event, media_file, run_async
    ):
        """CancelledError no es Exception: sin un finally los temporales se
        quedaban en TEMP_DIR cada vez que se cancelaba un envío."""
        original = media_file("v.mkv")
        sender.converted_to = media_file("v_telegram.mp4")
        sender.thumbnail = media_file("v_thumb.jpg")
        sender.upload_error = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            run_async(sender.dropbot.send_file_to_telegram(make_event(), original))

        assert not os.path.exists(sender.converted_to)
        assert not os.path.exists(sender.thumbnail)
        assert os.path.exists(original)

    def test_la_conversion_cancelada_aborta_sin_enviar(
        self, sender, make_event, media_file, run_async, monkeypatch
    ):
        async def cancelled_conversion(path, status_message=None):
            return None

        monkeypatch.setattr(
            sender.dropbot, "convert_video_to_telegram_compatible", cancelled_conversion
        )
        original = media_file("v.mkv")

        result = run_async(sender.dropbot.send_file_to_telegram(make_event(), original))

        assert result is None
        assert sender.uploaded is None
        assert os.path.exists(original), "cancelar la conversión no borra nada"


class TestBorradoTrasEnviar:
    def test_borra_el_original_no_el_convertido(
        self, sender, make_event, media_file, run_async
    ):
        original = media_file("v.mkv")
        sender.converted_to = media_file("v_telegram.mp4")

        run_async(sender.dropbot.send_file_to_telegram(
            make_event(), original, delete_after=True
        ))

        assert not os.path.exists(original)

    def test_un_fallo_al_borrar_no_es_un_fallo_de_envio(
        self, sender, texts, make_event, media_file, run_async, monkeypatch
    ):
        """El fichero ya está en Telegram: decir "error al enviar" es mentira."""
        original = media_file("v.mp4")
        real_remove = os.remove

        def refuse(path, *args, **kwargs):
            if path == original:
                raise PermissionError("read-only file system")
            return real_remove(path, *args, **kwargs)

        monkeypatch.setattr(os, "remove", refuse)

        result = run_async(sender.dropbot.send_file_to_telegram(
            make_event(), original, delete_after=True
        ))

        assert result is not None, "el envío fue correcto, hay que devolver el mensaje"
        assert "no se pudo borrar" in texts()
        assert "Error enviando el fichero" not in texts()
        assert "MISSING" not in texts()

    def test_no_borra_nada_si_el_envio_falla(
        self, sender, make_event, media_file, run_async
    ):
        original = media_file("v.mp4")
        sender.upload_error = RuntimeError("sin conexión")

        run_async(sender.dropbot.send_file_to_telegram(
            make_event(), original, delete_after=True
        ))

        assert os.path.exists(original)
