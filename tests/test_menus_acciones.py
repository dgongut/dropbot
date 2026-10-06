"""Menú de acciones de /manage: ver, descargar a Telegram, borrar, renombrar y cerrar.

Cada test registra sus ficheros en `pending_file_actions` como lo haría
/manage y pulsa los botones llamando al handler. Se comprueba lo que queda en
disco, lo que se le enseña al usuario (con el Markdown ya interpretado por
Telethon) y el estado que se deja atrás.

El pipeline de envío a Telegram (`_send_file_fast`, la conversión, los
metadatos y la miniatura) se sustituye por dobles que anotan sus llamadas.
"""

import os

import pytest
from telethon.extensions import markdown
from telethon.tl.types import (
    DocumentAttributeAudio, DocumentAttributeFilename, DocumentAttributeVideo,
)

import settings
from button_data import button_data

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

    def __init__(self, groups, sender_id=ADMIN, query_id=10_000, message_id=1):
        self.id = query_id
        self.message_id = message_id
        self.chat_id = 42
        self.sender_id = sender_id
        self.data = None
        self._groups = (b"",) + tuple(groups)
        self.pattern_match = self

    def group(self, index):
        return self._groups[index]

    async def get_sender(self):
        return None


class _Reply:
    """El mensaje de texto con el que el usuario contesta al renombrado."""

    def __init__(self, text, sender_id=ADMIN, reply_to_msg_id=None):
        self.raw_text = text
        self.sender_id = sender_id
        self.reply_to_msg_id = reply_to_msg_id
        self.id = 500
        self.chat_id = 42
        self.deleted = False

    async def delete(self):
        self.deleted = True

    async def get_sender(self):
        return None


@pytest.fixture
def manage(quiet_manage):
    for state in (quiet_manage.pending_file_actions, quiet_manage.pending_renames,
                  quiet_manage.list_messages):
        state.clear()
    yield quiet_manage
    for state in (quiet_manage.pending_file_actions, quiet_manage.pending_renames,
                  quiet_manage.list_messages):
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

    async def send(self, chat_id, path, filename, attributes, thumb, is_video, progress):
        if self.fail_with:
            raise self.fail_with
        self.sent.append({"path": path, "exists": os.path.exists(path), "filename": filename,
                          "attributes": attributes, "thumb": thumb, "is_video": is_video,
                          "progress": progress})

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
        assert sent_messages[-1][1] == manage.get_text("error_sending_file", "se cayó la red")

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

        assert sent_messages[-1][:2] == ("edit", manage.get_text("error_sending_file", "fallo"))

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
        expected = (manage.get_text("error_sending_file", "fallo") if fail
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
        assert _texts(sent_messages)[-1] == manage.get_text("error_item_not_found_short")

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

        def denied(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(manage.shutil, "rmtree", denied)
        _press(manage, run_async, "handle_confirm_delete", file_id)

        assert folder.exists()
        assert "Permission denied" in _texts(sent_messages)[0]

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

    @pytest.mark.parametrize("name", ["", "   ", "a/b.mkv", "../../etc/x", "a\\b.mkv", "/abs.mkv"])
    def test_un_nombre_invalido_se_rechaza_sin_tocar_nada(
        self, manage, target, downloads, sent_messages, run_async, name
    ):
        _ask_rename(manage, run_async, b"f1")
        before = sorted(os.listdir(downloads))

        _answer(manage, run_async, name)

        assert sorted(os.listdir(downloads)) == before
        assert target.read_bytes() == b"contenido"
        assert manage.get_text("rename_invalid_title") in sent_messages[-1][1]
        assert manage.pending_renames == {}

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

    @pytest.mark.parametrize("name", ["con\x00nulo.mkv", "x" * 300 + ".mkv"])
    def test_un_nombre_que_el_sistema_rechaza_avisa_sin_romper(
        self, manage, target, sent_messages, run_async, name
    ):
        _ask_rename(manage, run_async, b"f1")

        _answer(manage, run_async, name)

        assert target.read_bytes() == b"contenido"
        assert manage.get_text("rename_error_title") in sent_messages[-1][1]
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
