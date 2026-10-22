"""Menú de acciones de /manage: ver, descargar a Telegram, borrar, renombrar y cerrar.

Cada test registra sus ficheros en `pending_file_actions` como lo haría
/manage y pulsa los botones llamando al handler. Se comprueba lo que queda en
disco, lo que se le enseña al usuario (con el Markdown ya interpretado por
Telethon) y el estado que se deja atrás.

El pipeline de envío a Telegram (`_send_file_fast`, la conversión, los
metadatos y la miniatura) se sustituye por dobles que anotan sus llamadas.
"""

import asyncio
import errno
import os
import shutil
import threading

import pytest
from telethon.extensions import markdown
from telethon.tl.types import (
    DocumentAttributeAudio, DocumentAttributeFilename, DocumentAttributeVideo,
    MessageEntityTextUrl, MessageMediaWebPage, WebPagePending,
)

import settings
from button_data import button_data
from conftest import FORUM_CHAT_ID, FORUM_TOPIC, forum_reply_to

TELEGRAM_TEXT_LIMIT = 4096
CALLBACK_DATA_LIMIT = 64
ADMIN = 999


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _plain(text):
    return markdown.parse(text)[0]


def _payloads(sent_messages):
    return [button_data(button)
            for _, _, kwargs in sent_messages
            for row in kwargs.get("buttons") or []
            for button in row]


def _texts(sent_messages, kinds=None):
    return [text for kind, text, _ in sent_messages if kinds is None or kind in kinds]


class _Callback:
    """Un CallbackQuery como los de Telethon: `id` es el de la consulta y
    `message_id` el del mensaje que tiene el botón."""

    def __init__(self, groups, sender_id=ADMIN, query_id=10_000, message_id=1, chat_id=42):
        self.id = query_id
        self.message_id = message_id
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.data = None
        self._groups = (b"",) + tuple(groups)
        self.pattern_match = self

    def group(self, index):
        return self._groups[index]

    async def get_sender(self):
        return None


class _Reply:
    """El mensaje de texto con el que el usuario contesta al renombrado.

    Por defecto, en el chat privado con el bot (el 42, como los `_Callback`).
    """

    def __init__(self, text, sender_id=ADMIN, reply_to_msg_id=None, chat_id=42, is_private=True):
        self.raw_text = text
        self.sender_id = sender_id
        self.reply_to_msg_id = reply_to_msg_id
        self.id = 500
        self.chat_id = chat_id
        self.is_private = is_private
        self.deleted = False

    async def delete(self):
        self.deleted = True

    async def get_sender(self):
        return None


@pytest.fixture
def manage(quiet_manage):
    for state in (quiet_manage.pending_file_actions, quiet_manage.pending_renames,
                  quiet_manage.list_messages, quiet_manage.sending_files,
                  quiet_manage.deleted_file_ids):
        state.clear()
    yield quiet_manage
    for state in (quiet_manage.pending_file_actions, quiet_manage.pending_renames,
                  quiet_manage.list_messages, quiet_manage.sending_files,
                  quiet_manage.deleted_file_ids):
        state.clear()


@pytest.fixture
def downloads(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    return root


@pytest.fixture
def register(manage):
    """Registra una ruta con un id, como hace /manage al listar."""
    def factory(path, file_id="f1"):
        manage.pending_file_actions[file_id] = str(path)
        return file_id.encode()
    return factory


def _press(manage, run_async, handler, file_id, **kwargs):
    event = _Callback((file_id,), **kwargs)
    run_async(getattr(manage, handler)(event))
    return event


def _not_utf8(directory, raw_name=b"caf\xe9.mp3"):
    """Crea un fichero con el nombre en Latin-1, que no es UTF-8, como los de un
    ZIP de Windows descomprimido en el host. Devuelve su ruta tal como la ve
    Python, con sustitutos ("caf\\udce9.mp3")."""
    path = os.path.join(os.fsencode(directory), raw_name)
    try:
        with open(path, "wb") as handle:
            handle.write(b"\0")
    except OSError:
        pytest.skip("este sistema de ficheros no admite nombres que no son UTF-8")
    return os.fsdecode(path)


# ---------------------------------------------------------------------------
# Acciones de un elemento
# ---------------------------------------------------------------------------

class TestAcciones:
    def test_un_fichero_muestra_nombre_tamano_ruta_y_acciones(
        self, manage, downloads, register, sent_messages, run_async
    ):
        target = downloads / "informe_final_2026.pdf"
        target.write_bytes(b"\0" * 2048)
        file_id = register(target)

        _press(manage, run_async, "handle_file_action", file_id)

        [text] = _texts(sent_messages)
        plain = _plain(text)
        assert "informe_final_2026.pdf" in plain
        assert "2.00 KB" in plain
        assert str(downloads) in plain
        assert _payloads(sent_messages) == [
            b"rename:f1", b"delete:f1", b"download:f1", b"managecat:all", b"close"]

    def test_una_carpeta_muestra_el_tamano_de_su_contenido(
        self, manage, downloads, register, sent_messages, run_async
    ):
        folder = downloads / "Temporada 1"
        (folder / "sub").mkdir(parents=True)
        (folder / "a.mkv").write_bytes(b"\0" * 1024)
        (folder / "sub" / "b.mkv").write_bytes(b"\0" * 1024)
        file_id = register(folder)

        _press(manage, run_async, "handle_file_action", file_id)

        [text] = _texts(sent_messages)
        assert "📁" in text and "2.00 KB" in _plain(text)
        assert _payloads(sent_messages) == [b"rename:f1", b"delete:f1", b"managecat:all", b"close"]

    def test_el_tamano_de_una_carpeta_se_calcula_fuera_del_event_loop(
        self, manage, downloads, register, sent_messages, run_async, monkeypatch
    ):
        """Hay que recorrerla entera, y con una biblioteca grande (o en un
        volumen de red) el bot dejaba de atender botones, la cola y el progreso
        de las descargas mientras tanto."""
        folder = downloads / "Temporada 1"
        folder.mkdir()
        (folder / "a.mkv").write_bytes(b"\0" * 1024)
        file_id = register(folder)
        threads = []
        real_size = manage.get_directory_size

        def get_directory_size(path):
            threads.append(threading.get_ident())
            return real_size(path)

        monkeypatch.setattr(manage, "get_directory_size", get_directory_size)
        _press(manage, run_async, "handle_file_action", file_id)

        assert threads and threading.get_ident() not in threads
        assert "1.00 KB" in _plain(_texts(sent_messages)[0])

    def test_un_comprimido_de_mas_de_2gb_ofrece_descomprimir_pero_no_descargar(
        self, manage, downloads, register, sent_messages, run_async
    ):
        archive = downloads / "enorme.zip"
        with open(archive, "wb") as handle:
            handle.write(b"PK\x03\x04")
            handle.truncate(manage.MAX_TELEGRAM_FILE_SIZE)
        file_id = register(archive)

        _press(manage, run_async, "handle_file_action", file_id)

        payloads = _payloads(sent_messages)
        assert b"extract:f1" in payloads
        assert b"download:f1" not in payloads

    def test_un_elemento_borrado_por_fuera_avisa(
        self, manage, downloads, register, sent_messages, run_async
    ):
        target = downloads / "se_fue.txt"
        target.write_text("x")
        file_id = register(target)
        target.unlink()

        _press(manage, run_async, "handle_file_action", file_id)

        assert _texts(sent_messages) == [manage.get_text("error_item_not_found")]

    def test_un_no_admin_no_ve_nada(self, manage, downloads, register, sent_messages, run_async):
        target = downloads / "a.txt"
        target.write_text("x")
        file_id = register(target)

        _press(manage, run_async, "handle_file_action", file_id, sender_id=1)

        assert sent_messages == []

    def test_en_ingles_las_acciones_salen_en_ingles(
        self, manage, downloads, register, sent_messages, run_async, config_dir
    ):
        settings.put("language", "EN")
        target = downloads / "a.zip"
        target.write_bytes(b"PK\x03\x04")
        file_id = register(target)

        _press(manage, run_async, "handle_file_action", file_id)

        [(_, text, kwargs)] = sent_messages
        labels = [b.text for row in kwargs["buttons"] for b in row]
        assert "archivo" not in text
        for spanish in ("Renombrar", "Eliminar", "Descargar", "Descomprimir"):
            assert not any(spanish in label for label in labels), labels


@pytest.mark.parametrize("handler", ["handle_file_action", "handle_delete_file", "handle_rename_file"])
def test_una_comilla_invertida_en_el_nombre_no_rompe_el_mensaje(
    manage, downloads, register, sent_messages, run_async, handler
):
    name = "La `versión` buena.mkv"
    target = downloads / name
    target.write_bytes(b"\0")
    file_id = register(target)

    _press(manage, run_async, handler, file_id)

    [text] = _texts(sent_messages)
    assert name in _plain(text)


@pytest.mark.parametrize("name", ["con_guion_bajo.mkv", "doble__guion__bajo.mkv",
                                  "*negrita*.mkv", "[enlace](x.com).mkv"])
@pytest.mark.parametrize("handler", ["handle_file_action", "handle_delete_file", "handle_rename_file"])
def test_los_caracteres_de_markdown_en_el_nombre_se_ven_tal_cual(
    manage, downloads, register, sent_messages, run_async, handler, name
):
    target = downloads / name
    target.write_bytes(b"\0")
    file_id = register(target)

    _press(manage, run_async, handler, file_id)

    [text] = _texts(sent_messages)
    assert name in _plain(text)


@pytest.mark.parametrize("handler", ["handle_file_action", "handle_delete_file", "handle_rename_file",
                                     "handle_confirm_delete"])
def test_un_nombre_que_no_es_utf8_se_ensena_sin_romper_el_mensaje(
    manage, downloads, register, sent_messages, run_async, handler
):
    """Telethon no puede codificar los sustitutos del nombre y el mensaje no
    llegaba: desde el bot no había forma de borrar o renombrar el fichero."""
    file_id = register(_not_utf8(downloads))

    _press(manage, run_async, handler, file_id)

    [text] = _texts(sent_messages)
    text.encode("utf-8")  # lo que hace Telethon al enviarlo
    assert "caf\\xe9.mp3" in _plain(text)


@pytest.mark.parametrize("name", [
    "normal.mkv", "a`b.mkv", "`empieza.mkv", "acaba`", "dos``seguidas", "tres```seguidas",
    "mi__fichero__raro.txt", "`",
])
def test_md_code_deja_el_nombre_entero_y_en_codigo(name):
    from basic import md_code

    text, entities = markdown.parse(f"antes {md_code(name)} después `otro`")

    assert text.replace("\u200b", "").startswith(f"antes {name}")
    assert len(entities) == 2, "ni se come el código de después ni crea otros"


# ---------------------------------------------------------------------------
# Descargar a Telegram
# ---------------------------------------------------------------------------

class _Pipeline:
    """Dobles del pipeline de envío, con lo que se les pidió."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.sent = []
        self.converted = []
        self.converted_to = None      # None: cancelada; "same": ya compatible; o una ruta
        self.metadata = (12, 1920, 1080)
        self.thumbnail = None
        self.fail_with = None

    async def convert(self, path, message):
        self.converted.append(path)
        if self.converted_to == "same":
            return path
        return self.converted_to

    def progress(self, message, filename):
        return "progreso"

    async def send(self, chat_id, path, filename, attributes, thumb, is_video, progress, reply_to=None):
        if self.fail_with:
            raise self.fail_with
        self.sent.append({"path": path, "exists": os.path.exists(path), "filename": filename,
                          "attributes": attributes, "thumb": thumb, "is_video": is_video,
                          "progress": progress, "reply_to": reply_to})

    async def get_metadata(self, path):
        return self.metadata

    async def make_thumbnail(self, path):
        if self.thumbnail is None:
            return None
        self.thumbnail.write_bytes(b"jpg")
        return str(self.thumbnail)


@pytest.fixture
def pipeline(manage, tmp_path, monkeypatch):
    double = _Pipeline(tmp_path)
    monkeypatch.setattr(manage, "convert_video_to_telegram_compatible", double.convert)
    monkeypatch.setattr(manage, "create_upload_progress_callback", double.progress)
    monkeypatch.setattr(manage, "_send_file_fast", double.send)
    monkeypatch.setattr(manage, "get_video_metadata", double.get_metadata)
    monkeypatch.setattr(manage, "generate_video_thumbnail", double.make_thumbnail)
    return double


class TestDescargar:
    def test_en_un_foro_todo_va_al_tema_de_la_ficha(
        self, manage, pipeline, downloads, register, sent_messages, forum_callback, run_async
    ):
        """En un supergrupo con temas, lo que se manda sin reply_to cae en
        General: el «Enviando…», el fichero y el resultado salían allí."""
        target = downloads / "notas.txt"
        target.write_text("hola")
        file_id = register(target)

        run_async(manage.handle_download_file(forum_callback((file_id,))))

        [sent] = pipeline.sent
        assert sent["reply_to"] == FORUM_TOPIC
        assert [kwargs["reply_to"] for kind, _, kwargs in sent_messages if kind == "send_message"] == [FORUM_TOPIC] * 2

    def test_un_documento_se_envia_tal_cual(
        self, manage, pipeline, downloads, register, sent_messages, run_async
    ):
        target = downloads / "notas.txt"
        target.write_text("hola")
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        [sent] = pipeline.sent
        assert sent["path"] == str(target)
        assert sent["filename"] == "notas.txt"
        assert sent["is_video"] is False and sent["thumb"] is None
        assert [type(a) for a in sent["attributes"]] == [DocumentAttributeFilename]
        assert pipeline.converted == [], "un documento no se convierte"
        assert target.exists()
        kinds = [kind for kind, _, _ in sent_messages]
        # Progreso nuevo, se borra, y mensaje de éxito nuevo
        assert kinds == ["send_message", "delete", "send_message"]
        assert sent_messages[-1][1] == manage.get_text("file_sent_success", "notas.txt")
        assert _payloads(sent_messages) == [b"managecat:all", b"close"]

    def test_un_nombre_que_no_es_utf8_se_pasa_a_la_subida_con_un_nombre_que_telegram_admite(
        self, manage, pipeline, downloads, register, sent_messages, run_async
    ):
        """Aquí, lo que /manage le pasa a `_send_file_fast` y lo que dice: que
        la subida use ese nombre lo prueba TestNombreQueNoEsUtf8 de
        test_incoming_transfer.py, con el cliente de Telethon de verdad."""
        real_path = _not_utf8(downloads, b"notas\xe9.txt")
        file_id = register(real_path)

        _press(manage, run_async, "handle_download_file", file_id)

        [sent] = pipeline.sent
        assert sent["path"] == real_path and sent["exists"]
        assert sent["filename"] == "notas\\xe9.txt"
        bytes(sent["attributes"][0])  # Telethon serializa así el atributo al enviarlo
        for text in _texts(sent_messages):
            text.encode("utf-8")

    def test_un_video_convertido_se_envia_como_mp4_y_se_limpia(
        self, manage, pipeline, downloads, register, sent_messages, run_async, tmp_path
    ):
        target = downloads / "pelicula.avi"
        target.write_bytes(b"\0" * 64)
        converted = tmp_path / "temp_pelicula.mp4"
        converted.write_bytes(b"\0" * 32)
        pipeline.converted_to = str(converted)
        pipeline.thumbnail = tmp_path / "pelicula_thumb.jpg"
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        [sent] = pipeline.sent
        assert sent["path"] == str(converted) and sent["exists"]
        assert sent["filename"] == "pelicula.mp4"
        assert sent["is_video"] is True
        assert sent["thumb"] == str(pipeline.thumbnail)
        video = [a for a in sent["attributes"] if isinstance(a, DocumentAttributeVideo)]
        assert video and (video[0].duration, video[0].w, video[0].h) == (12, 1920, 1080)
        names = [a.file_name for a in sent["attributes"] if isinstance(a, DocumentAttributeFilename)]
        assert names == ["pelicula.mp4"]
        assert target.exists(), "el original no se toca"
        assert not converted.exists(), "el temporal convertido se borra"
        assert not pipeline.thumbnail.exists(), "la miniatura temporal se borra"
        assert sent_messages[-1][1] == manage.get_text("file_sent_success", "pelicula.mp4")

    def test_un_video_ya_compatible_no_se_borra(
        self, manage, pipeline, downloads, register, run_async
    ):
        target = downloads / "clip.mp4"
        target.write_bytes(b"\0" * 64)
        pipeline.converted_to = "same"
        pipeline.metadata = (None, None, None)
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        [sent] = pipeline.sent
        assert sent["path"] == str(target) and sent["filename"] == "clip.mp4"
        assert not any(isinstance(a, DocumentAttributeVideo) for a in sent["attributes"])
        assert target.exists()

    def test_si_se_cancela_la_conversion_no_se_envia_nada(
        self, manage, pipeline, downloads, register, sent_messages, run_async
    ):
        target = downloads / "pelicula.avi"
        target.write_bytes(b"\0" * 64)
        pipeline.converted_to = None
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        assert pipeline.sent == []
        assert target.exists()
        assert manage.get_text("file_sent_success", "pelicula.avi") not in _texts(sent_messages)

    def test_un_audio_lleva_su_duracion(
        self, manage, pipeline, downloads, register, run_async
    ):
        target = downloads / "cancion.mp3"
        target.write_bytes(b"\0" * 64)
        pipeline.metadata = (215, None, None)
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        [sent] = pipeline.sent
        audio = [a for a in sent["attributes"] if isinstance(a, DocumentAttributeAudio)]
        assert audio and audio[0].duration == 215
        assert pipeline.converted == []

    def test_un_audio_sin_duracion_se_envia_igual(
        self, manage, pipeline, downloads, register, run_async
    ):
        target = downloads / "cancion.mp3"
        target.write_bytes(b"\0" * 64)
        pipeline.metadata = (None, None, None)
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        [sent] = pipeline.sent
        assert not any(isinstance(a, DocumentAttributeAudio) for a in sent["attributes"])

    def test_de_2gb_o_mas_no_se_intenta(
        self, manage, pipeline, downloads, register, sent_messages, run_async
    ):
        target = downloads / "enorme.mkv"
        with open(target, "wb") as handle:
            handle.truncate(manage.MAX_TELEGRAM_FILE_SIZE)
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        assert pipeline.sent == [] and pipeline.converted == []
        assert _texts(sent_messages) == [manage.get_text("error_file_too_large")]

    def test_uno_que_ya_no_existe_avisa(
        self, manage, pipeline, downloads, register, sent_messages, run_async
    ):
        file_id = register(downloads / "fantasma.txt")

        _press(manage, run_async, "handle_download_file", file_id)

        assert pipeline.sent == []
        assert _texts(sent_messages) == [manage.get_text("error_file_not_found")]

    def test_un_id_desconocido_avisa(self, manage, pipeline, sent_messages, run_async):
        _press(manage, run_async, "handle_download_file", b"999_12345")

        assert _texts(sent_messages) == [manage.get_text("error_file_not_found")]

    def test_si_falla_el_envio_avisa_y_limpia_los_temporales(
        self, manage, pipeline, downloads, register, sent_messages, run_async, tmp_path
    ):
        target = downloads / "pelicula.avi"
        target.write_bytes(b"\0" * 64)
        converted = tmp_path / "temp.mp4"
        converted.write_bytes(b"\0")
        pipeline.converted_to = str(converted)
        pipeline.thumbnail = tmp_path / "t_thumb.jpg"
        pipeline.fail_with = ConnectionError("se cayó la red")
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        assert target.exists()
        assert not converted.exists() and not pipeline.thumbnail.exists()
        assert sent_messages[-1][0] == "send_message"
        assert sent_messages[-1][1] == manage.get_text("error_sending_file", manage.md_code("se cayó la red"))

    def test_la_ruta_del_error_de_envio_no_se_toma_por_markdown(
        self, manage, pipeline, downloads, register, sent_messages, run_async
    ):
        """El error lleva la ruta, y el nombre puede venir de fuera (un reenvío,
        un título de yt-dlp): "[Factura](evil.com)" salía como un enlace."""
        target = downloads / "[Factura](evil.com).pdf"
        target.write_text("x")
        pipeline.fail_with = FileNotFoundError(errno.ENOENT, "No such file or directory", str(target))
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        text, entities = markdown.parse(sent_messages[-1][1])
        assert f"'{target}'" in text
        assert not any(isinstance(entity, MessageEntityTextUrl) for entity in entities)

    def test_si_el_convertido_pasa_del_limite_se_envia_el_original(
        self, manage, pipeline, downloads, register, sent_messages, run_async, tmp_path
    ):
        """El límite se comprueba con el original, pero lo que se sube es el
        convertido, que recodificado (un AV1 a H.264) puede pesar más: tras la
        conversión no se enviaba nada, aunque el original cabía, y el aviso de
        «más de 2000 MB» contradecía la ficha. Cada Descargar acababa igual."""
        target = downloads / "serie.mkv"
        target.write_bytes(b"\0" * 64)
        converted = tmp_path / "temp_serie.mp4"
        with open(converted, "wb") as handle:
            handle.truncate(manage.MAX_TELEGRAM_FILE_SIZE)
        pipeline.converted_to = str(converted)
        pipeline.thumbnail = tmp_path / "serie_thumb.jpg"
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id)

        [sent] = pipeline.sent
        assert sent["path"] == str(target) and sent["filename"] == "serie.mkv"
        names = [a.file_name for a in sent["attributes"] if isinstance(a, DocumentAttributeFilename)]
        assert names == ["serie.mkv"]
        assert target.exists()
        assert not converted.exists() and not pipeline.thumbnail.exists()
        assert [kind for kind, _, _ in sent_messages] == ["send_message", "delete", "send_message"]
        assert sent_messages[-1][1] == manage.get_text("file_sent_unconverted", "serie.mkv")

    def test_si_tampoco_cabe_el_original_no_se_sube_y_se_dice_cual(
        self, manage, pipeline, downloads, register, sent_messages, run_async, tmp_path, monkeypatch
    ):
        """Solo pasa si el original crece mientras se convierte."""
        target = downloads / "serie.mkv"
        target.write_bytes(b"\0" * 64)
        converted = tmp_path / "temp_serie.mp4"
        with open(converted, "wb") as handle:
            handle.truncate(manage.MAX_TELEGRAM_FILE_SIZE)
        pipeline.converted_to = str(converted)
        file_id = register(target)
        convert = pipeline.convert

        async def growing_convert(path, message):
            with open(path, "r+b") as handle:
                handle.truncate(manage.MAX_TELEGRAM_FILE_SIZE)
            return await convert(path, message)

        monkeypatch.setattr(manage, "convert_video_to_telegram_compatible", growing_convert)

        _press(manage, run_async, "handle_download_file", file_id)

        assert pipeline.sent == []
        assert target.exists() and not converted.exists()
        assert sent_messages[-1][1] == manage.get_text("error_file_too_large_named", "serie.mkv")

    def test_otro_toque_mientras_se_envia_no_lo_envia_dos_veces(
        self, manage, pipeline, downloads, register, run_async, monkeypatch
    ):
        """El botón Descargar sigue en la ficha durante la conversión y la
        subida: otro toque convertía y enviaba el vídeo una segunda vez."""
        target = downloads / "serie.mkv"
        target.write_bytes(b"\0" * 64)
        file_id = register(target)

        async def scenario():
            conversion_done = asyncio.Event()

            async def slow_convert(path, message):
                pipeline.converted.append(path)
                await conversion_done.wait()
                return path

            monkeypatch.setattr(manage, "convert_video_to_telegram_compatible", slow_convert)
            first = asyncio.create_task(manage.handle_download_file(_Callback((file_id,))))
            while not pipeline.converted:
                await asyncio.sleep(0)
            second = asyncio.create_task(
                manage.handle_download_file(_Callback((file_id,), query_id=10_001)))
            for _ in range(10):
                await asyncio.sleep(0)
            conversion_done.set()
            await asyncio.gather(first, second)

        run_async(scenario())

        assert len(pipeline.converted) == 1
        assert len(pipeline.sent) == 1

    def test_si_telethon_cancela_el_handler_el_envio_sigue(
        self, manage, pipeline, downloads, register, sent_messages, run_async, monkeypatch
    ):
        """Con un corte de Telegram, disconnect() cancela los handlers en
        marcha: la conversión, que puede durar horas, moría con el suyo y el
        mensaje se quedaba en «Convirtiendo…»."""
        target = downloads / "serie.mkv"
        target.write_bytes(b"\0" * 64)
        file_id = register(target)

        async def scenario():
            converting, finish = asyncio.Event(), asyncio.Event()

            async def slow_convert(path, message):
                converting.set()
                await finish.wait()
                return path

            monkeypatch.setattr(manage, "convert_video_to_telegram_compatible", slow_convert)
            handler = asyncio.create_task(manage.handle_download_file(_Callback((file_id,))))
            await converting.wait()
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
            assert manage.sending_files, "sigue enviándose: otro toque no lo repetiría"
            finish.set()
            while manage.sending_files:
                await asyncio.sleep(0.01)

        run_async(asyncio.wait_for(scenario(), 5))

        [sent] = pipeline.sent
        assert sent["path"] == str(target)
        assert sent_messages[-1][1] == manage.get_text("file_sent_success", "serie.mkv")

    def test_terminado_un_envio_se_puede_volver_a_descargar(
        self, manage, pipeline, downloads, register, run_async
    ):
        target = downloads / "notas.txt"
        target.write_text("hola")
        file_id = register(target)
        pipeline.fail_with = RuntimeError("fallo")

        _press(manage, run_async, "handle_download_file", file_id)
        pipeline.fail_with = None
        _press(manage, run_async, "handle_download_file", file_id)
        _press(manage, run_async, "handle_download_file", file_id)

        assert len(pipeline.sent) == 2, "ni un envío fallido ni uno terminado lo bloquean"
        assert manage.sending_files == set()

    def test_sin_mensaje_de_progreso_edita_el_original(
        self, manage, pipeline, downloads, register, sent_messages, run_async, monkeypatch
    ):
        """Si no se pudo crear el mensaje de progreso, el resultado va al del botón."""
        target = downloads / "notas.txt"
        target.write_text("hola")
        file_id = register(target)

        async def no_message(chat_id, text=None, **kwargs):
            sent_messages.append(("send_message", str(text), kwargs))
            return None

        monkeypatch.setattr(manage, "safe_send_message", no_message)
        _press(manage, run_async, "handle_download_file", file_id)

        assert [kind for kind, _, _ in sent_messages] == ["send_message", "edit"]
        assert sent_messages[-1][1] == manage.get_text("file_sent_success", "notas.txt")

    def test_sin_mensaje_de_progreso_el_error_tambien_edita_el_original(
        self, manage, pipeline, downloads, register, sent_messages, run_async, monkeypatch
    ):
        target = downloads / "notas.txt"
        target.write_text("hola")
        pipeline.fail_with = RuntimeError("fallo")
        file_id = register(target)

        async def no_message(chat_id, text=None, **kwargs):
            sent_messages.append(("send_message", str(text), kwargs))
            return None

        monkeypatch.setattr(manage, "safe_send_message", no_message)
        _press(manage, run_async, "handle_download_file", file_id)

        assert sent_messages[-1][:2] == ("edit", manage.get_text("error_sending_file", manage.md_code("fallo")))

    @pytest.mark.parametrize("fail", [False, True])
    def test_si_no_se_pueden_borrar_los_temporales_el_resultado_llega(
        self, manage, pipeline, downloads, register, sent_messages, run_async, tmp_path,
        monkeypatch, fail
    ):
        target = downloads / "pelicula.avi"
        target.write_bytes(b"\0" * 64)
        converted = tmp_path / "temp.mp4"
        converted.write_bytes(b"\0")
        pipeline.converted_to = str(converted)
        pipeline.thumbnail = tmp_path / "t_thumb.jpg"
        if fail:
            pipeline.fail_with = RuntimeError("fallo")
        file_id = register(target)

        def busy(path, *args, **kwargs):
            raise OSError(16, "Device or resource busy", str(path))

        async def flaky_delete(message, *args, **kwargs):
            raise RuntimeError("no se pudo borrar")

        monkeypatch.setattr(manage.os, "remove", busy)
        if fail:
            # En el camino de error el borrado del progreso va protegido
            monkeypatch.setattr(manage, "safe_delete", flaky_delete)
        _press(manage, run_async, "handle_download_file", file_id)

        assert target.exists()
        expected = (manage.get_text("error_sending_file", manage.md_code("fallo")) if fail
                    else manage.get_text("file_sent_success", "pelicula.mp4"))
        assert sent_messages[-1][1] == expected

    def test_un_no_admin_no_recibe_nada(
        self, manage, pipeline, downloads, register, sent_messages, run_async
    ):
        target = downloads / "secreto.txt"
        target.write_text("x")
        file_id = register(target)

        _press(manage, run_async, "handle_download_file", file_id, sender_id=1)

        assert pipeline.sent == [] and sent_messages == []


# ---------------------------------------------------------------------------
# Borrar
# ---------------------------------------------------------------------------

class TestBorrar:
    def test_pedir_borrar_una_carpeta_avisa_de_su_contenido(
        self, manage, downloads, register, sent_messages, run_async
    ):
        folder = downloads / "Fotos viaje"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\0")
        file_id = register(folder)

        _press(manage, run_async, "handle_delete_file", file_id)

        [text] = _texts(sent_messages)
        assert manage.get_text("confirm_delete_folder_warning") in text
        assert folder.exists() and (folder / "a.jpg").exists()
        assert _payloads(sent_messages) == [b"confirmdelete:f1", b"fileact:f1"]

    def test_pedir_borrar_algo_que_ya_no_esta_avisa(
        self, manage, downloads, register, sent_messages, run_async
    ):
        file_id = register(downloads / "nada.txt")

        _press(manage, run_async, "handle_delete_file", file_id)

        assert _texts(sent_messages) == [manage.get_text("error_item_not_found_short")]

    def test_cancelar_vuelve_a_las_acciones_sin_borrar(
        self, manage, downloads, register, sent_messages, run_async
    ):
        target = downloads / "a.txt"
        target.write_text("x")
        file_id = register(target)

        _press(manage, run_async, "handle_delete_file", file_id)
        _press(manage, run_async, "handle_file_action", file_id)

        assert target.exists()
        assert manage.pending_file_actions["f1"] == str(target)

    def test_confirmar_borra_solo_ese_fichero(
        self, manage, downloads, register, sent_messages, run_async
    ):
        target = downloads / "borrar.txt"
        target.write_text("x")
        sibling = downloads / "borrar.txt.bak"
        sibling.write_text("y")
        file_id = register(target)

        _press(manage, run_async, "handle_confirm_delete", file_id)

        assert not target.exists() and sibling.exists()
        assert "f1" not in manage.pending_file_actions
        assert "borrar.txt" in _plain(_texts(sent_messages)[0])
        assert _payloads(sent_messages) == [b"managecat:all", b"close"]

    def test_confirmar_dos_veces_no_borra_otra_cosa(
        self, manage, downloads, register, sent_messages, run_async
    ):
        target = downloads / "borrar.txt"
        target.write_text("x")
        file_id = register(target)

        _press(manage, run_async, "handle_confirm_delete", file_id)
        target.write_text("vuelve a existir")
        _press(manage, run_async, "handle_confirm_delete", file_id)

        assert target.exists(), "el id ya se olvidó: un segundo toque no puede borrar nada"
        # Ni tapa el resultado del primero con un «no encontrado» sin botones
        assert len(_texts(sent_messages)) == 1
        assert "borrar.txt" in _plain(_texts(sent_messages)[0])

    def test_un_doble_toque_deja_el_resultado_del_borrado(
        self, manage, downloads, register, sent_messages, run_async
    ):
        """El segundo toque llegaba con el fichero ya borrado y su «no
        encontrado» tapaba el resultado y se llevaba Volver y Cerrar."""
        folder = downloads / "Serie"
        (folder / "T1").mkdir(parents=True)
        (folder / "T1" / "cap.mkv").write_bytes(b"\0")
        file_id = register(folder)

        async def double_tap():
            await asyncio.gather(
                manage.handle_confirm_delete(_Callback((file_id,))),
                manage.handle_confirm_delete(_Callback((file_id,), query_id=10_001)),
            )

        run_async(double_tap())

        assert not folder.exists()
        [text] = _texts(sent_messages)
        assert "Serie" in _plain(text)
        assert _payloads(sent_messages) == [b"managecat:all", b"close"]

    def test_la_confirmacion_de_otro_mensaje_avisa_de_que_ya_no_esta(
        self, manage, downloads, sent_messages, run_async
    ):
        """Una ruta lleva el mismo id en todos los listados: con /manage abierto
        dos veces (o por dos admins), el «Sí, eliminar» del otro mensaje se
        tomaba por un doble toque, y se quedaba preguntando sin respuesta."""
        target = downloads / "informe.pdf"
        target.write_bytes(b"\0")
        file_id = manage.register_manage_action(str(target))
        assert manage.register_manage_action(str(target)) == file_id

        run_async(manage.handle_confirm_delete(_Callback((file_id.encode(),), message_id=1)))
        run_async(manage.handle_confirm_delete(_Callback((file_id.encode(),), message_id=2, chat_id=43)))

        assert not target.exists()
        first, second = _texts(sent_messages)
        assert "informe.pdf" in _plain(first)
        assert second == manage.get_text("error_item_not_found_short")

    def test_un_boton_de_antes_de_reiniciar_avisa(self, manage, sent_messages, run_async):
        _press(manage, run_async, "handle_confirm_delete", b"m7_12345")

        assert _texts(sent_messages) == [manage.get_text("error_item_not_found_short")]

    def test_una_carpeta_se_borra_fuera_del_event_loop(
        self, manage, downloads, register, sent_messages, run_async, monkeypatch
    ):
        """Con muchos ficheros (o en un volumen de red) borrarla tarda, y en el
        loop el bot dejaba de atender todo lo demás mientras tanto."""
        folder = downloads / "Serie"
        (folder / "T1").mkdir(parents=True)
        (folder / "T1" / "cap.mkv").write_bytes(b"\0")
        file_id = register(folder)
        threads = []
        real_rmtree = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            threads.append(threading.get_ident())
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(manage.shutil, "rmtree", rmtree)
        _press(manage, run_async, "handle_confirm_delete", file_id)

        assert threads and threading.get_ident() not in threads
        assert not folder.exists()
        assert "f1" not in manage.pending_file_actions

    def test_confirmar_una_carpeta_la_borra_entera_y_nada_mas(
        self, manage, downloads, register, run_async
    ):
        folder = downloads / "Serie"
        (folder / "T1").mkdir(parents=True)
        (folder / "T1" / "cap.mkv").write_bytes(b"\0")
        keep = downloads / "Serie2"
        keep.mkdir()
        file_id = register(folder)

        _press(manage, run_async, "handle_confirm_delete", file_id)

        assert not folder.exists() and keep.exists()

    def test_un_error_de_permisos_avisa_y_no_olvida_el_fichero(
        self, manage, downloads, register, sent_messages, run_async, monkeypatch
    ):
        target = downloads / "protegido.txt"
        target.write_text("x")
        file_id = register(target)

        def denied(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(manage.os, "remove", denied)
        _press(manage, run_async, "handle_confirm_delete", file_id)

        assert target.exists()
        assert manage.pending_file_actions["f1"] == str(target)
        [text] = _texts(sent_messages)
        assert "Permission denied" in text

    def test_un_error_de_permisos_en_una_carpeta_avisa(
        self, manage, downloads, register, sent_messages, run_async, monkeypatch
    ):
        folder = downloads / "protegida"
        folder.mkdir()
        file_id = register(folder)
        real_rmtree = shutil.rmtree

        def denied(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(manage.shutil, "rmtree", denied)
        _press(manage, run_async, "handle_confirm_delete", file_id)

        assert folder.exists()
        assert "Permission denied" in _texts(sent_messages)[0]

        # El botón sigue valiendo para reintentarlo
        monkeypatch.setattr(manage.shutil, "rmtree", real_rmtree)
        _press(manage, run_async, "handle_confirm_delete", file_id)
        assert not folder.exists()

    def test_el_error_muestra_la_ruta_tal_cual(
        self, manage, downloads, register, sent_messages, run_async, monkeypatch
    ):
        target = downloads / "mi__fichero__raro.txt"
        target.write_text("x")
        file_id = register(target)

        def denied(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(manage.os, "remove", denied)
        _press(manage, run_async, "handle_confirm_delete", file_id)

        assert "mi__fichero__raro.txt" in _plain(_texts(sent_messages)[0])

    def test_un_no_admin_no_borra(self, manage, downloads, register, sent_messages, run_async):
        target = downloads / "a.txt"
        target.write_text("x")
        file_id = register(target)

        _press(manage, run_async, "handle_delete_file", file_id, sender_id=1)
        _press(manage, run_async, "handle_confirm_delete", file_id, sender_id=1)

        assert target.exists() and sent_messages == []

    def test_la_confirmacion_de_un_fichero_concuerda_en_espanol(
        self, manage, downloads, register, sent_messages, run_async, config_dir
    ):
        target = downloads / "a.txt"
        target.write_text("x")
        file_id = register(target)

        _press(manage, run_async, "handle_delete_file", file_id)

        assert "esta archivo" not in _texts(sent_messages)[0]

    def test_el_borrado_de_una_carpeta_concuerda_en_espanol(
        self, manage, downloads, register, sent_messages, run_async, config_dir
    ):
        folder = downloads / "c"
        folder.mkdir()
        file_id = register(folder)

        _press(manage, run_async, "handle_confirm_delete", file_id)

        text = _texts(sent_messages)[0]
        assert "Carpeta eliminado" not in text and "El carpeta" not in text

    def test_en_ingles_el_borrado_sale_en_ingles(
        self, manage, downloads, register, sent_messages, run_async, config_dir
    ):
        settings.put("language", "EN")
        target = downloads / "a.txt"
        target.write_text("x")
        file_id = register(target)

        _press(manage, run_async, "handle_delete_file", file_id)
        _press(manage, run_async, "handle_confirm_delete", file_id)

        for text in _texts(sent_messages):
            assert "archivo" not in text.lower()


# ---------------------------------------------------------------------------
# Renombrar
# ---------------------------------------------------------------------------

def _ask_rename(manage, run_async, file_id, **kwargs):
    return _press(manage, run_async, "handle_rename_file", file_id, **kwargs)


def _answer(manage, run_async, text, **kwargs):
    reply = _Reply(text, **kwargs)
    run_async(manage.handle_rename_input(reply))
    return reply


def _rename_filter(manage):
    """El filtro con el que se registra handle_rename_input."""
    return next(event.func for event, handler in manage._registrations()
                if handler is manage.handle_rename_input)


class TestRenombrar:
    @pytest.fixture
    def target(self, downloads, register):
        path = downloads / "original.mkv"
        path.write_bytes(b"contenido")
        register(path)
        return path

    def test_la_peticion_explica_que_hay_que_incluir_la_extension(
        self, manage, target, sent_messages, run_async
    ):
        _ask_rename(manage, run_async, b"f1")

        [text] = _texts(sent_messages)
        assert manage.get_text("rename_include_extension") in text
        assert "original.mkv" in _plain(text)
        assert _payloads(sent_messages) == [b"fileact:f1"]

    def test_la_peticion_de_una_carpeta_no_habla_de_extension(
        self, manage, downloads, register, sent_messages, run_async
    ):
        folder = downloads / "Carpeta"
        folder.mkdir()
        file_id = register(folder, "d1")

        _ask_rename(manage, run_async, file_id)

        assert manage.get_text("rename_include_extension") not in _texts(sent_messages)[0]

    def test_pedir_renombrar_algo_que_no_esta_avisa(
        self, manage, downloads, register, sent_messages, run_async
    ):
        file_id = register(downloads / "nada")

        _ask_rename(manage, run_async, file_id)

        assert _texts(sent_messages) == [manage.get_text("error_item_not_found_short")]
        assert manage.pending_renames == {}

    def test_renombra_borra_los_mensajes_y_limpia_la_espera(
        self, manage, target, sent_messages, run_async
    ):
        _ask_rename(manage, run_async, b"f1")
        sent_messages.clear()

        reply = _answer(manage, run_async, "  nuevo nombre.mkv  ")

        renamed = target.parent / "nuevo nombre.mkv"
        assert renamed.read_bytes() == b"contenido" and not target.exists()
        assert manage.pending_file_actions["f1"] == str(renamed)
        assert manage.pending_renames == {}
        assert reply.deleted, "el mensaje con el nombre se borra"
        assert sent_messages[0][0] == "delete", "y también la petición de renombrado"
        assert "nuevo nombre.mkv" in _plain(sent_messages[-1][1])
        assert _payloads(sent_messages) == [b"managecat:all", b"close"]

    def test_se_puede_cambiar_o_quitar_la_extension(self, manage, target, run_async):
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, "sin_extension")

        assert (target.parent / "sin_extension").exists()

    def test_una_carpeta_se_renombra_con_su_contenido(
        self, manage, downloads, register, run_async
    ):
        folder = downloads / "Vieja"
        folder.mkdir()
        (folder / "dentro.txt").write_text("x")
        register(folder, "d1")
        _ask_rename(manage, run_async, b"d1")

        _answer(manage, run_async, "Nueva")

        assert (downloads / "Nueva" / "dentro.txt").exists() and not folder.exists()

    @pytest.mark.parametrize("name", [
        "", "   ", "a/b.mkv", "../../etc/x", "a\\b.mkv", "/abs.mkv",
        # Los listados se saltan lo que empieza por punto: el fichero
        # desaparecería del bot
        ".Serie.S01E01.mkv", "...And Justice for All.flac",
        # El nombre pegado en dos líneas, un tabulador o un nulo
        "Serie S01E01\n.mkv", "con\ttabulador.mkv", "con\x00nulo.mkv",
    ])
    def test_un_nombre_invalido_se_rechaza_sin_tocar_nada(
        self, manage, target, downloads, sent_messages, run_async, name
    ):
        _ask_rename(manage, run_async, b"f1")
        before = sorted(os.listdir(downloads))

        _answer(manage, run_async, name)

        assert sorted(os.listdir(downloads)) == before
        assert target.read_bytes() == b"contenido"
        assert manage.get_text("rename_invalid_title") in sent_messages[-1][1]
        assert manage.get_text("rename_invalid_chars") in sent_messages[-1][1]
        assert manage.pending_renames == {}

    @pytest.mark.parametrize("name", [
        # Se ve como "Facturaexe.pdf" en /manage, en los mensajes y por SMB
        "Factura\N{RIGHT-TO-LEFT OVERRIDE}fdp.exe",
        "a\N{LEFT-TO-RIGHT MARK}b.mkv", "a\N{LEFT-TO-RIGHT ISOLATE}b.mkv",
        "a\N{LINE SEPARATOR}b.mkv", "con\x85nel.mkv", "con\x7fdel.mkv",
        # Invisibles en un extremo o delante de la extensión: dos nombres que
        # se ven iguales
        "\N{ZERO WIDTH NO-BREAK SPACE}peli.mkv", "peli\N{ZERO WIDTH SPACE}.mkv",
        "peli.mkv\N{WORD JOINER}",
    ])
    def test_un_nombre_con_invisibles_se_rechaza_como_los_que_llegan(
        self, manage, target, downloads, sent_messages, run_async, name
    ):
        """El renombrado no pasaba por sanitize_filename: aceptaba las marcas
        bidi y los invisibles que esta quita de los nombres que llegan."""
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, name)

        assert sorted(os.listdir(downloads)) == ["original.mkv"]
        assert manage.get_text("rename_invalid_chars") in sent_messages[-1][1]

    @pytest.mark.parametrize("name", [
        "👨\N{ZERO WIDTH JOINER}👩\N{ZERO WIDTH JOINER}👧 familia.jpg",
        "ข่าว\N{ZERO WIDTH SPACE}วันนี้.mkv",
        "Star Wars: Episodio IV.mkv",
    ])
    def test_los_invisibles_que_conserva_sanitize_filename_se_aceptan(
        self, manage, target, downloads, run_async, name
    ):
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, name)

        assert sorted(os.listdir(downloads)) == [name]

    @pytest.mark.parametrize("name", [".", ".."])
    def test_punto_y_dos_puntos_no_salen_de_la_carpeta(
        self, manage, target, downloads, tmp_path, sent_messages, run_async, name
    ):
        _ask_rename(manage, run_async, b"f1")

        outside = sorted(os.listdir(tmp_path))

        _answer(manage, run_async, name)

        assert target.read_bytes() == b"contenido"
        assert sorted(os.listdir(downloads)) == ["original.mkv"]
        assert sorted(os.listdir(tmp_path)) == outside, "nada se mueve fuera de la carpeta"
        assert "MISSING" not in sent_messages[-1][1]

    def test_no_pisa_un_fichero_que_ya_existe(
        self, manage, target, downloads, sent_messages, run_async
    ):
        other = downloads / "ocupado.mkv"
        other.write_bytes(b"otro")
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, "ocupado.mkv")

        assert target.read_bytes() == b"contenido"
        assert other.read_bytes() == b"otro"
        assert "ocupado.mkv" in _plain(sent_messages[-1][1])
        assert _payloads(sent_messages)[-2:] == [b"fileact:f1", b"close"]

    def test_no_pisa_una_carpeta_que_ya_existe(
        self, manage, target, downloads, run_async
    ):
        (downloads / "Carpeta").mkdir()
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, "Carpeta")

        assert target.exists() and (downloads / "Carpeta").is_dir()
        assert list((downloads / "Carpeta").iterdir()) == []

    def test_si_desaparecio_mientras_tanto_avisa(
        self, manage, target, sent_messages, run_async
    ):
        _ask_rename(manage, run_async, b"f1")
        target.unlink()

        _answer(manage, run_async, "otro.mkv")

        assert not (target.parent / "otro.mkv").exists()
        assert sent_messages[-1][1] == manage.get_text("error_file_gone")

    @pytest.mark.parametrize("name", [
        "x" * 300 + ".mkv",
        # 128 caracteres, pero 256 bytes: el límite es en bytes
        "ñ" * 128,
        # Un texto largo pegado por error: el error del sistema, con las dos
        # rutas, pasaba de 4096 y Telegram no dejaba llegar el aviso
        "palabra " * 500,
    ], ids=["300_caracteres", "128_enes_256_bytes", "texto_pegado"])
    def test_un_nombre_demasiado_largo_avisa_sin_intentarlo(
        self, manage, target, downloads, sent_messages, run_async, name
    ):
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, name)

        assert target.read_bytes() == b"contenido"
        assert sorted(os.listdir(downloads)) == ["original.mkv"]
        text = sent_messages[-1][1]
        assert manage.get_text("rename_too_long") in text
        assert len(_plain(text).encode("utf-16-le")) // 2 <= TELEGRAM_TEXT_LIMIT
        assert manage.pending_renames == {}

    def test_un_nombre_de_255_bytes_justos_se_acepta(self, manage, target, run_async):
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, "x" * 251 + ".mkv")

        assert (target.parent / ("x" * 251 + ".mkv")).read_bytes() == b"contenido"

    def test_un_nombre_que_el_sistema_rechaza_avisa_sin_las_rutas(
        self, manage, target, sent_messages, run_async, monkeypatch
    ):
        def refuse(src, dst):
            raise OSError(errno.EACCES, "Permission denied", src, None, dst)

        monkeypatch.setattr(manage.os, "rename", refuse)
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, "nuevo.mkv")

        assert target.read_bytes() == b"contenido"
        text = sent_messages[-1][1]
        assert manage.get_text("rename_error_title") in text
        assert "Permission denied" in _plain(text)
        assert str(target.parent) not in _plain(text), "las rutas pueden ser muy largas"
        assert manage.pending_renames == {}

    def test_otro_usuario_no_puede_contestar(self, manage, target, run_async):
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, "robado.mkv", sender_id=12345)

        assert target.exists()
        assert len(manage.pending_renames[ADMIN]) == 1, "la petición del admin sigue esperando"

    def test_sin_peticion_pendiente_no_hace_nada(self, manage, target, sent_messages, run_async):
        reply = _answer(manage, run_async, "lo que sea")

        assert target.exists() and sent_messages == [] and not reply.deleted

    def test_dos_peticiones_sin_responder_a_ninguna_van_en_orden(
        self, manage, downloads, register, run_async
    ):
        first = downloads / "uno.txt"
        second = downloads / "dos.txt"
        first.write_text("1")
        second.write_text("2")
        register(first, "a")
        register(second, "b")
        _ask_rename(manage, run_async, b"a", query_id=1, message_id=101)
        _ask_rename(manage, run_async, b"b", query_id=2, message_id=102)

        _answer(manage, run_async, "uno_nuevo.txt")
        _answer(manage, run_async, "dos_nuevo.txt")

        assert (downloads / "uno_nuevo.txt").read_text() == "1"
        assert (downloads / "dos_nuevo.txt").read_text() == "2"
        assert manage.pending_renames == {}

    def test_contestar_a_una_peticion_renombra_ese_fichero(
        self, manage, downloads, register, run_async
    ):
        first = downloads / "uno.txt"
        second = downloads / "dos.txt"
        first.write_text("1")
        second.write_text("2")
        register(first, "a")
        register(second, "b")
        _ask_rename(manage, run_async, b"a", query_id=7_000_001, message_id=101)
        _ask_rename(manage, run_async, b"b", query_id=7_000_002, message_id=102)

        _answer(manage, run_async, "elegido.txt", reply_to_msg_id=102)

        assert (downloads / "elegido.txt").read_text() == "2"
        assert first.exists()

    def test_contestar_por_id_cuando_coincide_elige_esa_peticion(
        self, manage, downloads, register, run_async
    ):
        """La búsqueda por respuesta funciona cuando el id coincide."""
        first = downloads / "uno.txt"
        second = downloads / "dos.txt"
        first.write_text("1")
        second.write_text("2")
        register(first, "a")
        register(second, "b")
        _ask_rename(manage, run_async, b"a", message_id=101)
        _ask_rename(manage, run_async, b"b", message_id=102)

        _answer(manage, run_async, "elegido.txt", reply_to_msg_id=102)

        assert (downloads / "elegido.txt").read_text() == "2"
        assert len(manage.pending_renames[ADMIN]) == 1

    def test_cancelar_el_renombrado_deja_de_esperar_el_nombre(
        self, manage, target, run_async
    ):
        _ask_rename(manage, run_async, b"f1")

        _press(manage, run_async, "handle_file_action", b"f1")
        reply = _answer(manage, run_async, "hola")

        assert target.exists(), "tras cancelar, un texto cualquiera no renombra nada"
        assert not reply.deleted
        assert ADMIN not in manage.pending_renames

    @pytest.mark.parametrize("handler, groups", [
        ("handle_manage_category", (b"all",)),
        ("handle_list_category", (b"all",)),
        ("handle_close", ()),
    ])
    def test_al_borrar_el_aviso_deja_de_esperar_el_nombre(
        self, manage, target, run_async, handler, groups
    ):
        """El aviso de Renombrar es el mensaje del listado: abrir otra categoría
        o cerrar lo borra sin pulsar Cancelar, y un "hola" horas después
        renombraba el fichero."""
        _ask_rename(manage, run_async, b"f1")
        manage.list_messages[ADMIN] = ["aviso de renombrar"]

        run_async(getattr(manage, handler)(_Callback(groups)))
        reply = _answer(manage, run_async, "hola")

        assert target.read_bytes() == b"contenido" and not reply.deleted
        assert not (target.parent / "hola").exists()
        assert ADMIN not in manage.pending_renames

    def test_tras_abrir_otro_listado_el_nombre_va_al_fichero_elegido_despues(
        self, manage, downloads, register, run_async
    ):
        first = downloads / "A.mkv"
        second = downloads / "B.mkv"
        first.write_text("A")
        second.write_text("B")
        register(first, "a")
        register(second, "b")
        _ask_rename(manage, run_async, b"a", message_id=101)
        manage.list_messages[ADMIN] = ["aviso de A"]

        # Sin cancelar, vuelve a /manage y pide renombrar B
        run_async(manage.handle_manage_category(_Callback((b"all",))))
        _ask_rename(manage, run_async, b"b", message_id=102)
        _answer(manage, run_async, "B_final.mkv")

        assert first.read_text() == "A"
        assert (downloads / "B_final.mkv").read_text() == "B"
        assert manage.pending_renames == {}

    def test_un_renombrado_olvidado_caduca(self, manage, target, sent_messages, run_async):
        _ask_rename(manage, run_async, b"f1", message_id=77)
        rename_filter = _rename_filter(manage)
        # Como si hubiera pasado el tiempo de espera desde que se pidió
        manage.pending_renames[ADMIN][0]["created"] -= manage.RENAME_TIMEOUT + 1
        sent_messages.clear()

        late = _Reply("hola")
        assert rename_filter(late) is False
        run_async(manage.handle_rename_input(late))

        assert target.read_bytes() == b"contenido"
        assert sent_messages == [] and not late.deleted

        # Citando el mensaje de Renombrar no hay duda, y el mensaje sigue
        # pidiendo que se le responda: antes se perdía sin decir nada
        quoted = _Reply("nuevo.mkv", reply_to_msg_id=77)
        assert rename_filter(quoted) is True
        run_async(manage.handle_rename_input(quoted))

        assert (target.parent / "nuevo.mkv").read_bytes() == b"contenido"
        assert quoted.deleted and manage.pending_renames == {}

    def test_en_un_grupo_la_respuesta_al_aviso_vale_aunque_haya_pasado_el_tiempo(
        self, manage, target, run_async
    ):
        group = -100123
        _ask_rename(manage, run_async, b"f1", chat_id=group, message_id=77)
        manage.pending_renames[ADMIN][0]["created"] -= manage.RENAME_TIMEOUT + 1

        reply = _answer(manage, run_async, "nuevo.mkv", chat_id=group, is_private=False,
                        reply_to_msg_id=77)

        assert (target.parent / "nuevo.mkv").read_bytes() == b"contenido"
        assert reply.deleted and manage.pending_renames == {}

    def test_una_cita_al_aviso_desde_otro_chat_no_renombra(
        self, manage, target, sent_messages, run_async
    ):
        """Los ids de mensaje son de cada chat: el 77 de un grupo no es el aviso."""
        _ask_rename(manage, run_async, b"f1", message_id=77)
        rename_filter = _rename_filter(manage)
        manage.pending_renames[ADMIN][0]["created"] -= manage.RENAME_TIMEOUT + 1
        sent_messages.clear()

        elsewhere = _Reply("nuevo.mkv", chat_id=-100123, is_private=False, reply_to_msg_id=77)
        assert rename_filter(elsewhere) is False
        run_async(manage.handle_rename_input(elsewhere))

        assert target.read_bytes() == b"contenido"
        assert sent_messages == [] and not elsewhere.deleted

    def test_un_texto_en_otro_chat_no_contesta_al_renombrado(
        self, manage, target, sent_messages, run_async
    ):
        """Pedido en el privado, el admin escribe después en un grupo en el que
        también está el bot: renombraba el fichero y lo contaba en el grupo."""
        _ask_rename(manage, run_async, b"f1")
        rename_filter = _rename_filter(manage)
        sent_messages.clear()

        in_group = _Reply("vale, ahora voy", chat_id=-100123, is_private=False)
        assert rename_filter(in_group) is False
        run_async(manage.handle_rename_input(in_group))

        assert target.read_bytes() == b"contenido"
        assert sent_messages == [] and not in_group.deleted
        assert len(manage.pending_renames[ADMIN]) == 1, "sigue esperando en el privado"

    def test_en_un_grupo_solo_vale_responder_al_aviso(
        self, manage, target, run_async
    ):
        group = -100123
        _ask_rename(manage, run_async, b"f1", chat_id=group, message_id=77)
        rename_filter = _rename_filter(manage)

        assert rename_filter(_Reply("hola a todos", chat_id=group, is_private=False)) is False
        assert rename_filter(_Reply("a otro mensaje", chat_id=group, is_private=False,
                                    reply_to_msg_id=5)) is False
        run_async(manage.handle_rename_input(_Reply("hola a todos", chat_id=group, is_private=False)))
        assert target.exists()

        reply = _answer(manage, run_async, "nuevo.mkv", chat_id=group, is_private=False,
                        reply_to_msg_id=77)

        assert (target.parent / "nuevo.mkv").read_bytes() == b"contenido"
        assert reply.deleted and manage.pending_renames == {}

    @pytest.mark.parametrize("answer, outcome", [
        ("nuevo.mkv", "rename_success_desc_file"),
        (".oculto.mkv", "rename_invalid_chars"),
    ])
    def test_en_un_foro_el_resultado_va_al_tema(
        self, manage, target, sent_messages, run_async, answer, outcome
    ):
        """El nombre que escribe el usuario se borra antes de contestarle, y
        la respuesta a un mensaje borrado salía en General."""
        _ask_rename(manage, run_async, b"f1", chat_id=FORUM_CHAT_ID, message_id=88)
        sent_messages.clear()
        reply = _Reply(answer, chat_id=FORUM_CHAT_ID, is_private=False, reply_to_msg_id=88)
        reply.reply_to = forum_reply_to(reply_to_msg_id=88)

        run_async(manage.handle_rename_input(reply))

        assert reply.deleted
        [(kind, text, kwargs)] = [message for message in sent_messages if message[0] != "delete"]
        assert manage.get_text(outcome) in text
        assert kind == "send_message" and kwargs["reply_to"] == FORUM_TOPIC

    def test_si_no_se_pueden_borrar_los_mensajes_renombra_igual(
        self, manage, target, run_async, monkeypatch
    ):
        _ask_rename(manage, run_async, b"f1")

        async def flaky_delete(message, *args, **kwargs):
            raise RuntimeError("no se pudo borrar")

        class Undeletable(_Reply):
            async def delete(self):
                raise RuntimeError("no se pudo borrar")

        monkeypatch.setattr(manage, "safe_delete", flaky_delete)
        run_async(manage.handle_rename_input(Undeletable("renombrado.mkv")))

        assert (target.parent / "renombrado.mkv").exists()

    def test_el_filtro_no_captura_comandos(self, manage, target, run_async):
        _ask_rename(manage, run_async, b"f1")
        rename_filter = next(event.func for event, handler in manage._registrations()
                             if handler is manage.handle_rename_input)

        assert rename_filter(_Reply("nuevo.mkv")) is True
        assert rename_filter(_Reply("/start")) is False
        assert rename_filter(_Reply("nuevo.mkv", sender_id=1)) is False

    def test_el_filtro_no_captura_ficheros_ni_enlaces(self, manage, target, run_async):
        """Un fichero o un enlace enviados con un renombrado pendiente van a
        sus handlers: tomarlos por el nombre borraba el mensaje del usuario."""
        _ask_rename(manage, run_async, b"f1")
        rename_filter = next(event.func for event, handler in manage._registrations()
                             if handler is manage.handle_rename_input)

        with_file = _Reply("")
        with_file.media = object()
        with_caption = _Reply("pie de foto")
        with_caption.media = object()

        assert rename_filter(with_file) is False
        assert rename_filter(with_caption) is False
        assert rename_filter(_Reply("https://youtu.be/abc123")) is False
        assert len(manage.pending_renames[ADMIN]) == 1

    def test_un_nombre_con_vista_previa_de_enlace_cuenta_como_respuesta(
        self, manage, target, run_async
    ):
        """Un nombre como "Serie.tv" es también un dominio: Telegram le pone
        vista previa y el mensaje llega con media, pero es el texto del nombre."""
        _ask_rename(manage, run_async, b"f1")
        rename_filter = _rename_filter(manage)
        reply = _Reply("Serie.tv")
        reply.media = MessageMediaWebPage(webpage=WebPagePending(id=1, date=None))

        assert rename_filter(reply) is True
        run_async(manage.handle_rename_input(reply))

        assert (target.parent / "Serie.tv").read_bytes() == b"contenido"

    def test_pedir_dos_veces_el_mismo_renombrado_solo_espera_un_nombre(
        self, manage, target, run_async
    ):
        _ask_rename(manage, run_async, b"f1", message_id=101)
        _ask_rename(manage, run_async, b"f1", message_id=102)

        assert [data["message_id"] for data in manage.pending_renames[ADMIN]] == [102]

    def test_un_nombre_con_comilla_invertida_se_ve_bien(
        self, manage, target, sent_messages, run_async
    ):
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, "a `b` c.mkv")

        assert (target.parent / "a `b` c.mkv").exists()
        assert "a `b` c.mkv" in _plain(sent_messages[-1][1])

    def test_un_nombre_que_no_es_utf8_se_puede_renombrar(
        self, manage, downloads, register, sent_messages, run_async
    ):
        register(_not_utf8(downloads))
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, "café.mp3")

        assert os.listdir(downloads) == ["café.mp3"]
        text = sent_messages[-1][1]
        text.encode("utf-8")  # lo que hace Telethon al enviarlo
        assert "caf\\xe9.mp3" in _plain(text) and "café.mp3" in _plain(text)

    @pytest.mark.parametrize("name", ["notas``final`.txt", "tres```seguidas.txt", "acaba`"])
    def test_un_nombre_copiado_del_mensaje_no_arrastra_espacios_invisibles(
        self, manage, target, downloads, run_async, name
    ):
        """md_code separa algunas comillas invertidas con un espacio de ancho
        cero: al copiar el nombre del mensaje viaja con él y acababa en el disco."""
        from basic import md_code

        _ask_rename(manage, run_async, b"f1")
        copied = _plain(md_code(name))
        assert "\u200b" in copied

        _answer(manage, run_async, copied)

        assert os.listdir(downloads) == [name]

    def test_renombrar_una_carpeta_concuerda_en_espanol(
        self, manage, downloads, register, sent_messages, run_async, config_dir
    ):
        folder = downloads / "Vieja"
        folder.mkdir()
        register(folder, "d1")
        _ask_rename(manage, run_async, b"d1")

        _answer(manage, run_async, "Nueva")

        text = sent_messages[-1][1]
        assert "Carpeta renombrado" not in text and "El carpeta" not in text

    def test_en_ingles_el_renombrado_sale_en_ingles(
        self, manage, target, sent_messages, run_async, config_dir
    ):
        settings.put("language", "EN")
        _ask_rename(manage, run_async, b"f1")
        _answer(manage, run_async, "nuevo.mkv")

        for text in _texts(sent_messages):
            assert "archivo" not in text.lower()

    def test_un_no_admin_no_puede_pedir_renombrar(
        self, manage, target, sent_messages, run_async
    ):
        _ask_rename(manage, run_async, b"f1", sender_id=12345)

        assert manage.pending_renames == {} and sent_messages == []


# ---------------------------------------------------------------------------
# Cerrar
# ---------------------------------------------------------------------------

class TestCerrar:
    def test_borra_el_listado_entero_y_el_mensaje_del_boton(
        self, manage, sent_messages, run_async
    ):
        manage.list_messages[ADMIN] = ["parte1", "parte2", "parte3"]

        run_async(manage.handle_close(_Callback(())))

        assert [kind for kind, _, _ in sent_messages] == ["delete"] * 4
        assert ADMIN not in manage.list_messages

    def test_sin_listado_solo_borra_el_mensaje(self, manage, sent_messages, run_async):
        run_async(manage.handle_close(_Callback(())))

        assert [kind for kind, _, _ in sent_messages] == ["delete"]

    def test_si_un_borrado_falla_sigue_con_el_resto(
        self, manage, sent_messages, run_async, monkeypatch
    ):
        manage.list_messages[ADMIN] = ["roto", "bueno"]
        deleted = []

        async def flaky_delete(message, *args, **kwargs):
            if message == "roto":
                raise RuntimeError("ya no existe")
            deleted.append(message)

        monkeypatch.setattr(manage, "safe_delete", flaky_delete)
        run_async(manage.handle_close(_Callback(())))

        assert "bueno" in deleted and len(deleted) == 2
        assert ADMIN not in manage.list_messages

    def test_no_toca_los_listados_de_otro_usuario(self, manage, run_async):
        manage.list_messages[ADMIN] = ["mio"]
        manage.list_messages[555] = ["suyo"]

        run_async(manage.handle_close(_Callback(())))

        assert manage.list_messages == {555: ["suyo"]}

    def test_un_no_admin_no_cierra_nada(self, manage, sent_messages, run_async):
        manage.list_messages[ADMIN] = ["mio"]

        run_async(manage.handle_close(_Callback((), sender_id=1)))

        assert sent_messages == [] and manage.list_messages == {ADMIN: ["mio"]}


# ---------------------------------------------------------------------------
# callback_data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Ñandú " + "á" * 120 + ".zip",
    "日本語" * 25 + ".zip",
    "🎬" * 60 + ".zip",
    "x" * 250 + ".zip",
])
def test_ningun_boton_de_las_acciones_pasa_de_64_bytes(
    manage, downloads, register, sent_messages, run_async, name
):
    target = downloads / name
    target.write_bytes(b"PK\x03\x04")
    file_id = register(target, "80_99999")

    for handler in ("handle_file_action", "handle_delete_file", "handle_rename_file",
                    "handle_confirm_delete"):
        _press(manage, run_async, handler, file_id)

    payloads = _payloads(sent_messages)
    assert b"extract:80_99999" in payloads
    for payload in payloads:
        assert len(payload) <= CALLBACK_DATA_LIMIT, payload
