"""Devolver ficheros a Telegram: botones Enviar / Enviar y borrar / Solo en el
servidor, envío automático y el aviso de "Descarga completada".

Complementa test_send_file.py y test_auto_send.py: aquí se recorre el camino
completo desde el botón hasta la subida, y los casos de borde (doble clic,
fichero que ya no existe, cola que no devuelve el mensaje...).
"""

import asyncio
import os
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import DocumentAttributeVideo

from button_data import button_data


class ChoiceEvent:
    """Un CallbackQuery de los botones de envío."""

    def __init__(self, action, file_id, sender_id=999):
        self.sender_id = sender_id
        self.chat_id = 42
        self.id = 1
        self.data = f"{action}:{file_id}".encode()
        self.pattern_match = _Match(action.encode(), file_id.encode())

    async def get_sender(self):
        return None


class _Match:
    def __init__(self, *groups):
        self._groups = (b"",) + groups

    def group(self, index):
        return self._groups[index]


@pytest.fixture
def uploads(quiet_bot, monkeypatch):
    """send_file_to_telegram sustituido: anota (ruta, mensaje, delete_after)."""
    calls = []

    async def send(event, file_path, sending_msg=None, delete_after=False):
        await asyncio.sleep(0)
        calls.append((file_path, sending_msg, delete_after))
        return MagicMock(id=5)

    monkeypatch.setattr(quiet_bot, "send_file_to_telegram", send)
    monkeypatch.setattr(quiet_bot.stats, "count", lambda key: None)
    return calls


def _pending(dropbot, path):
    return dropbot.register_pending_send(path)


class TestBotonesDeEnvio:
    def test_enviar_sube_y_conserva(self, quiet_bot, uploads, media_file, run_async):
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("send", file_id)))

        ((sent_path, sending_msg, delete_after),) = uploads
        assert sent_path == path and delete_after is False
        assert sending_msg is not None, "se reutiliza el mensaje de los botones"
        assert os.path.exists(path)

    def test_enviar_y_borrar(self, quiet_bot, uploads, media_file, run_async):
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("senddelete", file_id)))

        assert uploads[0][2] is True

    def test_solo_en_el_servidor_borra_la_pregunta_y_no_envia(
        self, quiet_bot, uploads, sent_messages, media_file, run_async
    ):
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("nosend", file_id)))

        assert uploads == []
        assert [kind for kind, _, _ in sent_messages] == ["delete"]
        assert os.path.exists(path)
        assert file_id not in quiet_bot.pending_files

    def test_el_mensaje_de_los_botones_pasa_a_enviando(
        self, quiet_bot, uploads, sent_messages, media_file, run_async
    ):
        path = media_file("pelicula.mp4")
        file_id = _pending(quiet_bot, path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("send", file_id)))

        kind, text, _ = sent_messages[0]
        assert kind == "edit" and "Enviando a Telegram" in text and "pelicula.mp4" in text

    def test_doble_clic_envia_una_sola_vez(self, quiet_bot, uploads, media_file, run_async):
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)

        async def double_click():
            await asyncio.gather(
                quiet_bot.handle_send_choice(ChoiceEvent("send", file_id)),
                quiet_bot.handle_send_choice(ChoiceEvent("send", file_id)),
            )

        run_async(double_click())

        assert len(uploads) == 1

    def test_enviar_y_luego_solo_servidor_no_hace_las_dos_cosas(
        self, quiet_bot, uploads, sent_messages, media_file, run_async
    ):
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("senddelete", file_id)))
        run_async(quiet_bot.handle_send_choice(ChoiceEvent("nosend", file_id)))

        assert len(uploads) == 1
        assert "delete" not in [kind for kind, _, _ in sent_messages]

    def test_un_boton_caducado_no_hace_nada(
        self, quiet_bot, uploads, sent_messages, run_async
    ):
        """Tras un reinicio pending_files está vacío."""
        run_async(quiet_bot.handle_send_choice(ChoiceEvent("send", "noexiste")))
        assert uploads == [] and sent_messages == []

    def test_si_el_fichero_ya_no_esta_lo_dice(
        self, quiet_bot, uploads, sent_messages, media_file, run_async
    ):
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)
        os.remove(path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("send", file_id)))

        assert uploads == []
        assert sent_messages[-1][1] == quiet_bot.get_text("error_file_does_not_exist_user")

    def test_si_no_se_puede_editar_crea_un_mensaje_nuevo(
        self, quiet_bot, uploads, sent_messages, media_file, run_async, monkeypatch
    ):
        async def edit_times_out(*args, **kwargs):
            return None

        monkeypatch.setattr(quiet_bot, "safe_edit", edit_times_out)
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("send", file_id)))

        assert sent_messages[0][0] == "reply"
        assert uploads and uploads[0][1] is not None

    def test_si_falla_preparar_el_mensaje_avisa_y_no_envia(
        self, quiet_bot, uploads, sent_messages, media_file, run_async, monkeypatch
    ):
        async def edit_fails(*args, **kwargs):
            raise RuntimeError("cola parada")

        monkeypatch.setattr(quiet_bot, "safe_edit", edit_fails)
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("send", file_id)))

        assert uploads == []
        assert sent_messages[-1][1] == quiet_bot.get_text("error_sending_the_file_user")

    def test_quien_no_es_admin_no_gasta_el_boton(
        self, quiet_bot, uploads, media_file, run_async
    ):
        path = media_file("v.mp4")
        file_id = _pending(quiet_bot, path)

        run_async(quiet_bot.handle_send_choice(ChoiceEvent("send", file_id, sender_id=1)))

        assert uploads == []
        assert quiet_bot.pending_files.get(file_id) == path
        quiet_bot.pending_files.pop(file_id, None)


# --- send_file_automatically -------------------------------------------------

class TestEnvioAutomatico:
    def test_send_usa_el_mensaje_de_enviando(
        self, quiet_bot, uploads, sent_messages, media_file, run_async, config_dir
    ):
        quiet_bot.settings.put("urls.auto_send", "SEND")
        path = media_file("a.mp3")

        result = run_async(quiet_bot.send_file_automatically(MagicMock(chat_id=1), path))

        assert result is not None
        assert "Enviando a Telegram" in sent_messages[0][1]
        assert uploads[0][0] == path and uploads[0][2] is False

    def test_send_delete_borra_tras_enviar(
        self, quiet_bot, uploads, media_file, run_async, config_dir
    ):
        quiet_bot.settings.put("urls.auto_send", "SEND_DELETE")

        run_async(quiet_bot.send_file_automatically(MagicMock(), media_file("a.mp3")))

        assert uploads[0][2] is True

    def test_sin_mensaje_de_progreso_envia_igual(
        self, quiet_bot, uploads, media_file, run_async, monkeypatch, config_dir
    ):
        quiet_bot.settings.put("urls.auto_send", "SEND")

        async def no_message(*args, **kwargs):
            return None

        monkeypatch.setattr(quiet_bot, "safe_reply", no_message)

        run_async(quiet_bot.send_file_automatically(MagicMock(), media_file("a.mp3")))

        assert uploads and uploads[0][1] is None

    def test_justo_en_el_limite_no_se_envia(
        self, quiet_bot, uploads, texts, media_file, run_async, config_dir
    ):
        quiet_bot.settings.put("urls.auto_send", "SEND")
        path = media_file("a.mp4", quiet_bot.MAX_TELEGRAM_FILE_SIZE)

        result = run_async(quiet_bot.send_file_automatically(MagicMock(), path))

        assert result is None and uploads == []
        assert "demasiado grande" in texts()

    def test_un_byte_por_debajo_del_limite_si(
        self, quiet_bot, uploads, media_file, run_async, config_dir
    ):
        quiet_bot.settings.put("urls.auto_send", "SEND")
        path = media_file("a.mp4", quiet_bot.MAX_TELEGRAM_FILE_SIZE - 1)

        run_async(quiet_bot.send_file_automatically(MagicMock(), path))

        assert len(uploads) == 1


# --- handle_success ----------------------------------------------------------

def _info(kind, **extra):
    info = {"type": kind, "size_formatted": "3 MB", "duration_formatted": None,
            "resolution": None, "codec_video": None, "codec_audio": None, "bitrate": None}
    info.update(extra)
    return info


@pytest.fixture
def with_info(quiet_bot, monkeypatch):
    def setter(info):
        async def file_info(path):
            return info
        monkeypatch.setattr(quiet_bot, "get_file_info", file_info)
    return setter


class TestDescargaCompletada:
    def test_el_audio_lleva_duracion_codec_y_bitrate(
        self, quiet_bot, with_info, sent_messages, make_event, media_file, run_async, config_dir
    ):
        with_info(_info("audio", duration_formatted="3:25", codec_audio="mp3",
                        bitrate="320 kbps"))
        quiet_bot.settings.put("urls.auto_send", "STORE")

        run_async(quiet_bot.handle_success(make_event(), media_file("tema.mp3")))

        text = sent_messages[0][1]
        for piece in ("tema.mp3", "3 MB", "3:25", "mp3", "320 kbps", quiet_bot.AUD_ICO):
            assert piece in text

    def test_el_video_lleva_resolucion_y_codecs(
        self, quiet_bot, with_info, sent_messages, make_event, media_file, run_async, config_dir
    ):
        with_info(_info("video", duration_formatted="1:00", resolution="1920x1080",
                        codec_video="h264", codec_audio="aac"))
        quiet_bot.settings.put("urls.auto_send", "STORE")

        run_async(quiet_bot.handle_success(make_event(), media_file("v.mp4")))

        text = sent_messages[0][1]
        assert "1920x1080" in text and "h264 + aac" in text

    def test_el_icono_de_una_descarga_directa_manda(
        self, quiet_bot, with_info, sent_messages, make_event, media_file, run_async, config_dir
    ):
        with_info(_info("document"))

        run_async(quiet_bot.handle_success(
            make_event(), media_file("x.bin"), icon="🧪", content_type="document"
        ))

        assert "🧪" in sent_messages[0][1]

    def test_un_torrent_que_ya_se_llevo_el_gestor(
        self, quiet_bot, sent_messages, make_event, tmp_path, run_async, config_dir
    ):
        run_async(quiet_bot.handle_success(make_event(), str(tmp_path / "ubuntu.torrent")))

        (_, text, _), = sent_messages
        assert "ubuntu.torrent" in text and quiet_bot.TOR_ICO in text
        assert "Unknown" not in text

    def test_un_fichero_que_no_existe_no_se_anuncia(
        self, quiet_bot, sent_messages, make_event, tmp_path, run_async
    ):
        run_async(quiet_bot.handle_success(make_event(), str(tmp_path / "nada.mp4")))
        assert sent_messages == []

    def test_si_falla_el_aviso_se_propaga(
        self, quiet_bot, with_info, make_event, media_file, run_async, monkeypatch
    ):
        """Quien llama decide qué hacer; no se puede tragar en silencio."""
        with_info(_info("document"))

        async def broken(*args, **kwargs):
            raise RuntimeError("cola parada")

        monkeypatch.setattr(quiet_bot, "safe_reply", broken)

        with pytest.raises(RuntimeError):
            run_async(quiet_bot.handle_success(make_event(), media_file("a.pdf")))

    def test_los_botones_de_ask_llevan_al_envio_de_ese_fichero(
        self, quiet_bot, with_info, uploads, sent_messages, make_event, media_file,
        run_async, config_dir
    ):
        """Del aviso con botones al envío: el id del botón resuelve a la ruta."""
        with_info(_info("video"))
        quiet_bot.settings.put("urls.auto_send", "ASK")
        path = media_file("v.mp4")

        run_async(quiet_bot.handle_success(make_event(), path))

        buttons = sent_messages[-1][2]["buttons"]
        data = [button_data(b).decode() for row in buttons for b in row]
        assert [d.split(":")[0] for d in data] == ["send", "senddelete", "nosend"]
        assert len({d.split(":")[1] for d in data}) == 1, "los tres botones, el mismo fichero"

        action, file_id = data[1].split(":")
        run_async(quiet_bot.handle_send_choice(ChoiceEvent(action, file_id)))

        assert uploads[0][0] == path and uploads[0][2] is True

    def test_dos_ficheros_de_una_misma_url_tienen_botones_distintos(
        self, quiet_bot, with_info, uploads, sent_messages, make_event, media_file,
        run_async, config_dir
    ):
        with_info(_info("audio"))
        quiet_bot.settings.put("urls.auto_send", "ASK")
        event = make_event()
        first, second = media_file("1.mp3"), media_file("2.mp3")

        run_async(quiet_bot.handle_success(event, first))
        run_async(quiet_bot.handle_success(event, second))

        asks = [kwargs["buttons"] for _, _, kwargs in sent_messages if kwargs.get("buttons")]
        ids = [button_data(row[0]).decode().split(":")[1] for row in (b[0] for b in asks)]
        assert quiet_bot.pending_files[ids[0]] == first
        assert quiet_bot.pending_files[ids[1]] == second


# --- send_file_to_telegram: lo que no cubre test_send_file.py ---------------

@pytest.fixture
def upload_spy(quiet_bot, monkeypatch):
    calls = []

    async def convert(path, status_message=None):
        calls.append(("convert", path))
        return path

    async def metadata(path):
        return (10, 640, 480)

    async def thumbnail(path):
        return None

    async def upload(entity, path, filename, attributes, thumb, is_video, callback):
        calls.append(("upload", entity, path, filename, attributes, is_video, callback))
        return MagicMock(id=321)

    monkeypatch.setattr(quiet_bot, "convert_video_to_telegram_compatible", convert)
    monkeypatch.setattr(quiet_bot, "get_video_metadata", metadata)
    monkeypatch.setattr(quiet_bot, "generate_video_thumbnail", thumbnail)
    monkeypatch.setattr(quiet_bot, "_send_file_fast", upload)
    return calls


class TestEnvioATelegram:
    def test_sube_al_chat_del_evento_y_borra_el_mensaje_de_enviando(
        self, quiet_bot, upload_spy, sent_messages, make_event, media_file, run_async
    ):
        sending = MagicMock(name="enviando")

        run_async(quiet_bot.send_file_to_telegram(make_event(chat_id=77), media_file("d.pdf"), sending))

        upload = [c for c in upload_spy if c[0] == "upload"][0]
        assert upload[1] == 77 and upload[3] == "d.pdf"
        assert callable(upload[6])
        assert [kind for kind, _, _ in sent_messages] == ["delete"]

    def test_borrar_tras_enviar_lo_dice_en_respuesta_al_fichero(
        self, quiet_bot, upload_spy, sent_messages, make_event, media_file, run_async
    ):
        path = media_file("d.pdf")

        run_async(quiet_bot.send_file_to_telegram(make_event(), path, delete_after=True))

        assert not os.path.exists(path)
        kind, text, kwargs = sent_messages[-1]
        assert text == quiet_bot.get_text("deleted_from_server")
        assert kwargs["reply_to"] == 321

    def test_si_falla_borra_el_mensaje_de_enviando_y_avisa(
        self, quiet_bot, upload_spy, sent_messages, make_event, media_file, run_async, monkeypatch
    ):
        async def upload(*args):
            raise RuntimeError("se cortó")

        monkeypatch.setattr(quiet_bot, "_send_file_fast", upload)

        result = run_async(quiet_bot.send_file_to_telegram(
            make_event(), media_file("d.pdf"), MagicMock()
        ))

        assert result is None
        assert [kind for kind, _, _ in sent_messages] == ["delete", "reply"]
        assert sent_messages[-1][1] == quiet_bot.get_text("error_sending_the_file_user")

    @pytest.mark.parametrize("name", ["v.mp4", "v.MKV", "v.webm", "v.mov"])
    def test_los_videos_pasan_por_la_conversion(
        self, quiet_bot, upload_spy, make_event, media_file, run_async, name
    ):
        run_async(quiet_bot.send_file_to_telegram(make_event(), media_file(name)))

        assert upload_spy[0][0] == "convert"
        upload = upload_spy[-1]
        assert upload[5] is True
        assert any(isinstance(a, DocumentAttributeVideo) for a in upload[4])

    @pytest.mark.parametrize("name", ["v.m4v", "v.ts"])
    def test_cualquier_extension_de_video_se_envia_como_video(
        self, quiet_bot, upload_spy, make_event, media_file, run_async, name
    ):
        run_async(quiet_bot.send_file_to_telegram(make_event(), media_file(name)))

        upload = upload_spy[-1]
        assert upload[5] is True
        assert any(isinstance(a, DocumentAttributeVideo) for a in upload[4])
