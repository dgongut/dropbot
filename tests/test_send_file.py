"""send_file_to_telegram: metadatos, limpieza de temporales, borrado y límite de tamaño."""

import asyncio
import math
import os
from unittest.mock import MagicMock

import pytest
from telethon import utils as telethon_utils
from telethon.extensions import markdown
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo, MessageEntityTextUrl

from conftest import FORUM_CHAT_ID, FORUM_TOPIC, forum_reply_to


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

    async def upload(entity, path, filename, attributes, thumb, is_video, callback, reply_to=None):
        if harness.upload_error is not None:
            raise harness.upload_error
        harness.uploaded = {
            "path": path, "filename": filename, "attributes": attributes,
            "thumb": thumb, "is_video": is_video, "reply_to": reply_to,
        }
        return MagicMock(id=123)

    monkeypatch.setattr(quiet_bot, "convert_video_to_telegram_compatible", convert)
    monkeypatch.setattr(quiet_bot, "get_video_metadata", metadata)
    monkeypatch.setattr(quiet_bot, "generate_video_thumbnail", thumbnail)
    monkeypatch.setattr(quiet_bot, "_send_file_fast", upload)

    harness.dropbot = quiet_bot
    return harness


class TestTemaDelForo:
    """Se subía con send_file al chat, sin reply_to: en un supergrupo con temas,
    el progreso salía en el tema del enlace y el fichero en General."""

    def test_el_fichero_va_al_tema_de_la_peticion(self, sender, make_event, media_file, run_async):
        event = make_event(chat_id=FORUM_CHAT_ID)
        event.reply_to = forum_reply_to()

        run_async(sender.dropbot.send_file_to_telegram(event, media_file("d.pdf")))

        assert sender.uploaded["reply_to"] == FORUM_TOPIC

    def test_con_un_boton_va_al_tema_del_mensaje_del_boton(self, sender, forum_callback, media_file, run_async):
        run_async(sender.dropbot.send_file_to_telegram(forum_callback(), media_file("d.pdf")))

        assert sender.uploaded["reply_to"] == FORUM_TOPIC

    def test_en_privado_va_como_siempre(self, sender, make_event, media_file, run_async):
        run_async(sender.dropbot.send_file_to_telegram(make_event(), media_file("d.pdf")))

        assert sender.uploaded["reply_to"] is None

    def test_con_un_boton_el_aviso_de_error_va_al_tema(
        self, sender, sent_messages, forum_callback, media_file, run_async
    ):
        """El mensaje del botón era el del «Enviando…» y ya se ha borrado: el
        aviso, contestado a él, salía sin tema, en General."""
        sender.upload_error = RuntimeError("la subida falló")

        run_async(sender.dropbot.send_file_to_telegram(forum_callback(), media_file("d.pdf")))

        kind, text, kwargs = sent_messages[-1]
        assert text == sender.dropbot.get_text("error_sending_the_file_user")
        assert kind == "send_message" and kwargs["reply_to"] == FORUM_TOPIC


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

    def test_el_error_al_borrar_no_se_lee_como_markdown(
        self, sender, sent_messages, make_event, media_file, run_async, monkeypatch
    ):
        """El error lleva la ruta, y el nombre puede ponerlo el servidor remoto
        (Content-Disposition): un «[Pulsa aquí](evil.com)» salía como enlace."""
        name = "[Pulsa aquí](evil.com) **negrita**.mp4"
        original = media_file(name)

        async def upload(entity, path, *args, **kwargs):
            # Mientras se sube, alguien lo quita del servidor
            os.remove(path)
            return MagicMock(id=123)

        monkeypatch.setattr(sender.dropbot, "_send_file_fast", upload)

        run_async(sender.dropbot.send_file_to_telegram(
            make_event(), original, delete_after=True
        ))

        plain, entities = markdown.parse(sent_messages[-1][1])
        assert name in plain, "el nombre tiene que verse tal cual"
        assert not any(isinstance(e, MessageEntityTextUrl) for e in entities)

    def test_si_otro_fichero_ocupa_su_nombre_mientras_se_envia_no_lo_borra(
        self, sender, texts, make_event, tmp_path, run_async, monkeypatch
    ):
        """Durante la conversión se renombra en /manage y otro fichero pasa a
        llamarse como él: borrar por ruta se llevaba ese otro."""
        original = tmp_path / "video.mp4"
        original.write_bytes(b"el que se envia")
        other = tmp_path / "video (1).mp4"
        other.write_bytes(b"otro video")
        renamed = tmp_path / "Charla.mp4"

        async def convert(path, status_message=None):
            os.rename(original, renamed)
            os.rename(other, original)
            return path

        monkeypatch.setattr(sender.dropbot, "convert_video_to_telegram_compatible", convert)

        result = run_async(sender.dropbot.send_file_to_telegram(
            make_event(), str(original), delete_after=True
        ))

        assert result is not None
        assert original.read_bytes() == b"otro video", "se ha borrado otro fichero"
        assert renamed.exists()
        assert sender.dropbot.get_text("error_deleting_after_send_replaced") in texts()
        assert sender.dropbot.get_text("deleted_from_server") not in texts()

    def test_si_lo_borran_y_otro_reusa_su_nombre_no_lo_borra(
        self, sender, texts, make_event, tmp_path, run_async, monkeypatch
    ):
        """El sistema puede dar el inodo del borrado al fichero nuevo: por eso
        el original se mantiene abierto hasta el final."""
        original = tmp_path / "video.mp4"
        original.write_bytes(b"el que se envia")

        async def convert(path, status_message=None):
            os.remove(original)
            original.write_bytes(b"una descarga nueva")
            return path

        monkeypatch.setattr(sender.dropbot, "convert_video_to_telegram_compatible", convert)

        run_async(sender.dropbot.send_file_to_telegram(
            make_event(), str(original), delete_after=True
        ))

        assert original.read_bytes() == b"una descarga nueva"
        assert sender.dropbot.get_text("error_deleting_after_send_replaced") in texts()


class TestLimiteDeTamano:
    def test_el_mayor_fichero_que_se_deja_subir_cabe_en_4000_partes(self, dropbot):
        """Sin Premium, y un bot nunca lo tiene, Telegram acepta como mucho
        4000 partes: con un límite de 2 GiB se dejaban pasar hasta 4096."""
        largest = dropbot.MAX_TELEGRAM_FILE_SIZE - 1
        part_size = telethon_utils.get_appropriated_part_size(largest) * 1024
        assert math.ceil(largest / part_size) <= 4000

    def test_si_el_convertido_no_cabe_se_sube_el_original(
        self, sender, texts, make_event, media_file, run_async, monkeypatch
    ):
        """Antes se mide el original, pero al recodificar (un AV1 a H.264) el
        vídeo puede crecer y pasar del límite: tras horas de conversión no se
        enviaba ninguno de los dos, aunque el original cabía, y el aviso decía
        que pesaba más de 2000 MB sin decir cuál."""
        original = media_file("serie.mkv")
        converted = media_file("convertido.mp4", sender.dropbot.MAX_TELEGRAM_FILE_SIZE + 1)
        sender.converted_to = converted
        upload = sender.dropbot._send_file_fast
        while_uploading = []

        async def watched_upload(entity, path, *args, **kwargs):
            while_uploading.append(os.path.exists(converted))
            return await upload(entity, path, *args, **kwargs)

        monkeypatch.setattr(sender.dropbot, "_send_file_fast", watched_upload)

        result = run_async(sender.dropbot.send_file_to_telegram(make_event(), original))

        assert result is not None
        assert sender.uploaded["path"] == original
        assert sender.uploaded["filename"] == "serie.mkv"
        assert while_uploading == [False], "el convertido no ocupa TEMP_DIR durante la subida"
        assert sender.dropbot.get_text("file_sent_unconverted", "serie.mkv") in texts()
        assert "demasiado grande" not in texts()
        assert os.path.exists(original)

    def test_con_enviar_y_borrar_el_original_sin_convertir_se_borra_tras_subirlo(
        self, sender, make_event, media_file, run_async, monkeypatch
    ):
        original = media_file("serie.mkv")
        sender.converted_to = media_file("convertido.mp4", sender.dropbot.MAX_TELEGRAM_FILE_SIZE + 1)
        upload = sender.dropbot._send_file_fast
        while_uploading = []

        async def watched_upload(entity, path, *args, **kwargs):
            while_uploading.append(os.path.exists(original))
            return await upload(entity, path, *args, **kwargs)

        monkeypatch.setattr(sender.dropbot, "_send_file_fast", watched_upload)

        run_async(sender.dropbot.send_file_to_telegram(make_event(), original, delete_after=True))

        assert sender.uploaded["path"] == original
        assert while_uploading == [True]
        assert not os.path.exists(original)

    def test_si_tampoco_cabe_el_original_no_se_sube_y_se_dice_cual(
        self, sender, texts, make_event, media_file, run_async
    ):
        """Solo pasa si el original cambia mientras se convierte. En una
        playlist el aviso iba sin nombre y no se sabía qué vídeo había fallado."""
        original = media_file("serie.mkv", sender.dropbot.MAX_TELEGRAM_FILE_SIZE)
        sender.converted_to = media_file(
            "convertido.mp4", sender.dropbot.MAX_TELEGRAM_FILE_SIZE + 1
        )

        result = run_async(sender.dropbot.send_file_to_telegram(
            make_event(), original, delete_after=True
        ))

        assert result is None
        assert sender.uploaded is None
        assert sender.dropbot.get_text("error_file_too_large_named", "serie.mkv") in texts()
        assert "Error enviando el fichero" not in texts()
        assert not os.path.exists(sender.converted_to)
        assert os.path.exists(original)
