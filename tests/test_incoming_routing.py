"""A qué carpeta va cada fichero que se manda al bot, y con qué nombre.

Los mensajes son objetos reales de Telethon (`types.Message` con su media),
no mocks: `message.video`, `message.audio` o `message.file.name` se calculan
con el código de verdad de Telethon a partir de los atributos del documento,
que es justo lo que decide la carpeta.
"""

import asyncio

import pytest
from telethon.tl import types


# --- Construcción de mensajes ------------------------------------------------

def _document(attributes, mime="application/octet-stream", size=1000, doc_id=7):
    return types.Document(
        id=doc_id, access_hash=1, file_reference=b"", date=None,
        mime_type=mime, size=size, dc_id=1, attributes=list(attributes),
    )


def document_message(name=None, attributes=(), mime="application/octet-stream",
                     size=1000, doc_id=7):
    attrs = list(attributes)
    if name is not None:
        attrs.insert(0, types.DocumentAttributeFilename(file_name=name))
    media = types.MessageMediaDocument(document=_document(attrs, mime, size, doc_id))
    return types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="", media=media)


def photo_message(photo_id=8):
    photo = types.Photo(
        id=photo_id, access_hash=1, file_reference=b"", date=None, dc_id=1,
        sizes=[types.PhotoSize(type="x", w=10, h=10, size=500)],
    )
    return types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="",
                         media=types.MessageMediaPhoto(photo=photo))


def video_attr(round_message=False):
    return types.DocumentAttributeVideo(duration=5, w=640, h=480,
                                        round_message=round_message)


def audio_attr(voice=False):
    return types.DocumentAttributeAudio(duration=5, voice=voice)


@pytest.fixture
def folders(dropbot, tmp_path, monkeypatch):
    """Una carpeta distinta por tipo, como si todas estuvieran montadas."""
    paths = {kind: str(tmp_path / kind) for kind in
             ("audio", "video", "photo", "torrent", "ebook", "url_video", "url_audio")}
    general = str(tmp_path / "downloads")
    monkeypatch.setattr(dropbot, "DOWNLOAD_PATHS", paths)
    monkeypatch.setattr(dropbot, "DOWNLOAD_PATH", general)
    paths["other"] = general
    return paths


def _event(message, make_event):
    event = make_event()
    event.message = message
    return event


def _route(dropbot, make_event, message):
    return dropbot.get_download_path(_event(message, make_event))


# --- get_download_path -------------------------------------------------------

class TestCarpetaPorExtension:
    @pytest.mark.parametrize("name, kind", [
        ("pelicula.mkv", "video"),
        ("clip.MP4", "video"),
        ("cancion.flac", "audio"),
        ("tema.Mp3", "audio"),
        ("foto.jpg", "photo"),
        ("captura.PNG", "photo"),
        ("linux.torrent", "torrent"),
        ("libro.epub", "ebook"),
        ("manual.pdf", "ebook"),
        ("paquete.zip", "other"),
        ("datos.bin", "other"),
        ("sin_extension", "other"),
    ])
    def test_cada_extension_a_su_carpeta(self, dropbot, folders, make_event, name, kind):
        path, _ = _route(dropbot, make_event, document_message(name))
        assert path == folders[kind]

    def test_cada_tipo_lleva_su_icono(self, dropbot, folders, make_event):
        expected = {
            "a.mkv": dropbot.VID_ICO, "a.mp3": dropbot.AUD_ICO, "a.jpg": dropbot.IMG_ICO,
            "a.torrent": dropbot.TOR_ICO, "a.epub": dropbot.BOO_ICO, "a.zip": dropbot.DEF_ICO,
        }
        for name, icon in expected.items():
            assert _route(dropbot, make_event, document_message(name))[1] == icon, name

    def test_el_torrent_gana_aunque_el_documento_diga_que_es_video(
        self, dropbot, folders, make_event
    ):
        """La extensión manda antes que los atributos del documento."""
        message = document_message("serie.torrent", [video_attr()])
        assert _route(dropbot, make_event, message)[0] == folders["torrent"]

    def test_el_ebook_gana_aunque_el_documento_diga_que_es_audio(
        self, dropbot, folders, make_event
    ):
        message = document_message("audiolibro.pdf", [audio_attr()])
        assert _route(dropbot, make_event, message)[0] == folders["ebook"]


class TestCarpetaPorTipoDeMensaje:
    def test_una_foto_va_a_fotos(self, dropbot, folders, make_event):
        assert _route(dropbot, make_event, photo_message()) == (folders["photo"], dropbot.IMG_ICO)

    def test_un_video_sin_nombre_va_a_videos(self, dropbot, folders, make_event):
        message = document_message(None, [video_attr()], mime="video/mp4")
        assert _route(dropbot, make_event, message)[0] == folders["video"]

    def test_un_video_con_extension_rara_va_a_videos(self, dropbot, folders, make_event):
        message = document_message("grabacion.xyz", [video_attr()])
        assert _route(dropbot, make_event, message)[0] == folders["video"]

    def test_un_audio_sin_nombre_va_a_audios(self, dropbot, folders, make_event):
        message = document_message(None, [audio_attr()], mime="audio/mpeg")
        assert _route(dropbot, make_event, message)[0] == folders["audio"]

    def test_un_video_redondo_va_a_videos(self, dropbot, folders, make_event):
        message = document_message(None, [video_attr(round_message=True)], mime="video/mp4")
        assert _route(dropbot, make_event, message)[0] == folders["video"]

    def test_una_nota_de_voz_va_a_audios(self, dropbot, folders, make_event):
        message = document_message(None, [audio_attr(voice=True)], mime="audio/ogg")
        assert _route(dropbot, make_event, message)[0] == folders["audio"]

    def test_un_documento_cualquiera_va_a_la_general(self, dropbot, folders, make_event):
        message = document_message("notas.xyz")
        assert _route(dropbot, make_event, message) == (folders["other"], dropbot.DEF_ICO)

    def test_sin_carpetas_propias_todo_va_a_downloads(self, dropbot, make_event, monkeypatch):
        """Es lo que pasa cuando el compose solo monta /downloads."""
        general = "/downloads-de-prueba"
        monkeypatch.setattr(dropbot, "DOWNLOAD_PATH", general)
        monkeypatch.setattr(dropbot, "DOWNLOAD_PATHS", {
            kind: general for kind in
            ("audio", "video", "photo", "torrent", "ebook", "url_video", "url_audio")
        })
        for message in (photo_message(), document_message("a.mkv"),
                        document_message("a.mp3"), document_message("a.epub"),
                        document_message("a.torrent"), document_message("a.zip")):
            assert _route(dropbot, make_event, message)[0] == general


# --- is_file_message ---------------------------------------------------------

class TestEsUnFichero:
    def test_un_documento_real_es_un_fichero(self, dropbot):
        assert dropbot.is_file_message(document_message("a.pdf"))

    def test_una_foto_real_es_un_fichero(self, dropbot):
        assert dropbot.is_file_message(photo_message())

    def test_un_mensaje_de_texto_real_no_es_un_fichero(self, dropbot):
        message = types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="hola")
        assert not dropbot.is_file_message(message)

    def test_un_enlace_con_vista_previa_con_foto_no_es_un_fichero(self, dropbot):
        page = types.WebPage(
            id=1, url="https://example.com", display_url="example.com", hash=0,
            photo=types.Photo(id=9, access_hash=1, file_reference=b"", date=None,
                              dc_id=1, sizes=[]),
        )
        message = types.Message(id=1, peer_id=types.PeerUser(1), date=None,
                                message="https://example.com",
                                media=types.MessageMediaWebPage(webpage=page))
        assert not dropbot.is_file_message(message)


# --- get_file_name -----------------------------------------------------------

class TestNombreDelFichero:
    def test_usa_el_nombre_del_documento(self, dropbot):
        doc = _document([types.DocumentAttributeFilename(file_name="Informe 2024.pdf")])
        assert dropbot.get_file_name(doc) == "Informe 2024.pdf"

    def test_limpia_el_nombre_de_rutas(self, dropbot):
        """Un nombre con ../ no puede sacar el fichero de su carpeta."""
        doc = _document([types.DocumentAttributeFilename(file_name="../../etc/passwd")])
        name = dropbot.get_file_name(doc)
        assert "/" not in name and ".." not in name.split("/")

    def test_video_sin_nombre(self, dropbot):
        assert dropbot.get_file_name(_document([video_attr()], doc_id=55)) == "video_55.mp4"

    def test_audio_sin_nombre(self, dropbot):
        assert dropbot.get_file_name(_document([audio_attr()], doc_id=56)) == "audio_56.mp3"

    def test_documento_sin_nombre(self, dropbot):
        assert dropbot.get_file_name(_document([], doc_id=57)) == "file_57"

    def test_nombre_que_queda_vacio_al_limpiarlo(self, dropbot):
        """Si sanitize_filename no deja nada, se usa el nombre genérico."""
        doc = _document([types.DocumentAttributeFilename(file_name=""), video_attr()],
                        doc_id=58)
        assert dropbot.get_file_name(doc) == "video_58.mp4"

    def test_foto(self, dropbot):
        assert dropbot.get_file_name(photo_message(photo_id=77).photo) == "photo_77.jpg"

    def test_otro_tipo_de_media(self, dropbot):
        other = types.PhotoEmpty(id=78)
        assert dropbot.get_file_name(other) == "file_78"


# --- handle_files ------------------------------------------------------------

@pytest.fixture
def counted(dropbot, monkeypatch):
    keys = []
    monkeypatch.setattr(dropbot.stats, "count", keys.append)
    return keys


class TestHandleFiles:
    def test_lanza_la_descarga_en_segundo_plano_y_la_registra(
        self, quiet_bot, folders, make_event, run_async, monkeypatch, counted
    ):
        started = []

        async def fake_limited(event):
            started.append(event.id)

        monkeypatch.setattr(quiet_bot, "limited_download", fake_limited)
        event = _event(document_message("a.mkv"), make_event)
        event.id = 4242

        async def scenario():
            await quiet_bot.handle_files(event)
            task = quiet_bot.active_tasks[event.id]
            assert isinstance(task, asyncio.Task)
            await task

        try:
            run_async(scenario())
        finally:
            quiet_bot.active_tasks.pop(4242, None)
        assert started == [4242]

    @pytest.mark.parametrize("message, key", [
        (photo_message(), "file_photo"),
        (document_message("a.mkv"), "file_video"),
        (document_message("a.mp3"), "file_audio"),
        (document_message("a.torrent"), "file_torrent"),
        (document_message("a.epub"), "file_ebook"),
        (document_message("a.zip"), "file_other"),
    ])
    def test_cuenta_el_tipo_de_fichero(
        self, quiet_bot, folders, make_event, run_async, monkeypatch, counted, message, key
    ):
        async def fake_limited(event):
            return None

        monkeypatch.setattr(quiet_bot, "limited_download", fake_limited)
        event = _event(message, make_event)
        event.id = 4243

        async def scenario():
            await quiet_bot.handle_files(event)
            await quiet_bot.active_tasks.pop(event.id)

        run_async(scenario())
        assert counted == [key]

    def test_si_falla_al_clasificar_descarga_igual(
        self, quiet_bot, make_event, run_async, monkeypatch, counted
    ):
        """Las estadísticas nunca pueden impedir una descarga."""
        started = []

        async def fake_limited(event):
            started.append(event.id)

        def broken(event):
            raise RuntimeError("no se pudo clasificar")

        monkeypatch.setattr(quiet_bot, "limited_download", fake_limited)
        monkeypatch.setattr(quiet_bot, "get_download_path", broken)
        event = _event(document_message("a.mkv"), make_event)
        event.id = 4244

        async def scenario():
            await quiet_bot.handle_files(event)
            await quiet_bot.active_tasks.pop(event.id)

        run_async(scenario())
        assert started == [4244]

    def test_quien_no_es_admin_no_descarga_nada(
        self, quiet_bot, make_event, run_async, monkeypatch, counted
    ):
        started = []

        async def fake_limited(event):
            started.append(event.id)

        async def get_sender():
            return None

        monkeypatch.setattr(quiet_bot, "limited_download", fake_limited)
        event = _event(document_message("a.mkv"), make_event)
        event.id = 4245
        event.sender_id += 1  # cualquiera que no sea el admin
        event.get_sender = get_sender

        run_async(quiet_bot.handle_files(event))

        assert 4245 not in quiet_bot.active_tasks
        assert not started and not counted
