"""AUTO_SEND: qué hace el bot con un vídeo/audio recién bajado de una URL."""

from unittest.mock import MagicMock

import pytest

MAX_SIZE = 2 * 1024 * 1024 * 1024


@pytest.fixture
def downloaded(quiet_bot, monkeypatch):
    """handle_success con el análisis del fichero y el envío simulados.

    Devuelve `(dropbot, sent_paths)`, donde `sent_paths` recoge lo que se
    habría subido a Telegram.
    """
    sent = []

    async def file_info(path):
        return {
            "type": "video", "size_formatted": "1 MB",
            "duration_formatted": "0:10", "resolution": "640x480",
            "codec_video": "h264", "codec_audio": "aac", "bitrate": None,
        }

    async def send(event, file_path, sending_msg=None, delete_after=False):
        sent.append((file_path, delete_after))
        return MagicMock(id=1)

    monkeypatch.setattr(quiet_bot, "get_file_info", file_info)
    monkeypatch.setattr(quiet_bot, "send_file_to_telegram", send)
    return quiet_bot, sent


def _has_buttons(sent_messages):
    return any(kwargs.get("buttons") for _, _, kwargs in sent_messages)


class TestModos:
    def test_ask_pregunta_con_botones(
        self, downloaded, sent_messages, make_event, media_file, run_async, monkeypatch
    ):
        dropbot, sent = downloaded
        monkeypatch.setattr(dropbot, "AUTO_SEND", "ASK")

        run_async(dropbot.handle_success(make_event(), media_file("v.mp4")))

        assert not sent, "en ASK no debe enviarse nada por su cuenta"
        assert _has_buttons(sent_messages), "en ASK deben salir los botones"

    def test_send_envia_y_conserva(
        self, downloaded, sent_messages, make_event, media_file, run_async, monkeypatch
    ):
        dropbot, sent = downloaded
        monkeypatch.setattr(dropbot, "AUTO_SEND", "SEND")
        path = media_file("v.mp4")

        run_async(dropbot.handle_success(make_event(), path))

        assert sent == [(path, False)]
        assert not _has_buttons(sent_messages), "en SEND no deben salir botones"

    def test_send_delete_envia_y_borra(
        self, downloaded, make_event, media_file, run_async, monkeypatch
    ):
        dropbot, sent = downloaded
        monkeypatch.setattr(dropbot, "AUTO_SEND", "SEND_DELETE")
        path = media_file("v.mp4")

        run_async(dropbot.handle_success(make_event(), path))

        assert sent == [(path, True)]

    def test_store_no_envia_ni_pregunta(
        self, downloaded, sent_messages, make_event, media_file, run_async, monkeypatch
    ):
        dropbot, sent = downloaded
        monkeypatch.setattr(dropbot, "AUTO_SEND", "STORE")

        run_async(dropbot.handle_success(make_event(), media_file("v.mp4")))

        assert not sent
        assert not _has_buttons(sent_messages)


class TestLimiteDeTamano:
    @pytest.mark.parametrize("mode", ["SEND", "SEND_DELETE"])
    def test_avisa_en_lugar_de_callar(
        self, downloaded, texts, make_event, media_file, run_async, monkeypatch, mode
    ):
        """Con envío automático, un fichero que no cabe tiene que decirse."""
        dropbot, sent = downloaded
        monkeypatch.setattr(dropbot, "AUTO_SEND", mode)

        run_async(dropbot.handle_success(make_event(), media_file("grande.mp4", MAX_SIZE + 1)))

        assert not sent, "no debe intentar enviar algo que Telegram rechaza"
        assert "demasiado grande" in texts()
        assert "MISSING" not in texts(), "la clave de traducción debe existir"

    def test_ask_no_ofrece_botones_inutiles(
        self, downloaded, sent_messages, make_event, media_file, run_async, monkeypatch
    ):
        dropbot, _ = downloaded
        monkeypatch.setattr(dropbot, "AUTO_SEND", "ASK")

        run_async(dropbot.handle_success(make_event(), media_file("grande.mp4", MAX_SIZE + 1)))

        assert not _has_buttons(sent_messages)


class TestAlcance:
    def test_no_aplica_a_ficheros_enviados_al_bot(
        self, downloaded, sent_messages, make_event, media_file, run_async, monkeypatch
    ):
        """AUTO_SEND es para descargas de URL; devolver lo que te acaban de
        mandar por Telegram no tendría sentido."""
        dropbot, sent = downloaded
        monkeypatch.setattr(dropbot, "AUTO_SEND", "SEND")

        run_async(dropbot.handle_success(
            make_event(), media_file("v.mp4"), show_action_buttons=False
        ))

        assert not sent
        assert not _has_buttons(sent_messages)

    def test_no_aplica_a_imagenes_ni_documentos(
        self, quiet_bot, sent_messages, make_event, media_file, run_async, monkeypatch
    ):
        dropbot = quiet_bot
        sent = []

        async def file_info(path):
            return {
                "type": "document", "size_formatted": "1 MB",
                "duration_formatted": None, "resolution": None,
                "codec_video": None, "codec_audio": None, "bitrate": None,
            }

        async def send(event, file_path, sending_msg=None, delete_after=False):
            sent.append(file_path)
            return MagicMock(id=1)

        monkeypatch.setattr(dropbot, "get_file_info", file_info)
        monkeypatch.setattr(dropbot, "send_file_to_telegram", send)
        monkeypatch.setattr(dropbot, "AUTO_SEND", "SEND")

        run_async(dropbot.handle_success(make_event(), media_file("doc.pdf")))

        assert not sent


class TestPlaylists:
    def test_la_rama_de_playlist_aplica_auto_send(self, dropbot):
        """Las playlists completas no pasan por handle_success.

        Tienen su propia rama en run_url_download, así que si AUTO_SEND no se
        aplica ahí a mano las playlists se quedan siempre solo almacenadas.
        """
        import ast
        import pathlib

        source = pathlib.Path(dropbot.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            segment = ast.get_source_segment(source, node) or ""
            head = segment.split("\n", 1)[0]
            if "is_full_playlist" in head and "temp_file_paths" in head:
                assert "send_file_automatically" in segment
                return

        pytest.fail("no se encontró la rama de playlist completa")
