"""download_media / limited_download: un fichero mandado al bot hasta el disco.

`bot.download_media` se sustituye por una función que escribe, como Telethon,
en el temporal de verdad que le pasa el bot, así que lo que se comprueba es lo
que queda en disco (carpeta final con sus ocultos, TEMP_DIR) y lo que ve el
usuario.
"""

import asyncio
import errno
import inspect
import itertools
import os
import shutil
from types import SimpleNamespace

import pytest
from button_data import button_data
from telethon import errors
from telethon.errors import InvalidBufferError
from telethon.tl import types
from telethon.tl.functions.upload import GetFileRequest

from proc_helpers import wait_until
from utils.file_helpers import partial_name
from utils.limiter import ResizableLimiter

# La petición que acompaña a los errores de Telegram al descargar: su nombre
# acaba en el texto de todos ellos, "(caused by GetFileRequest)"
GET_FILE = GetFileRequest(
    location=types.InputDocumentFileLocation(id=7, access_hash=1, file_reference=b"", thumb_size=""),
    offset=0, limit=1024 * 1024,
)


async def telethon_write(file, data):
    """Escribe como Telethon en el fichero que le pasa el bot: si write()
    devuelve algo que se puede esperar, lo espera."""
    written = file.write(data)
    if inspect.isawaitable(written):
        await written


def file_message(name, size=1000, attributes=(), doc_id=7, mime_type="application/octet-stream"):
    attrs = [types.DocumentAttributeFilename(file_name=name), *attributes]
    document = types.Document(
        id=doc_id, access_hash=1, file_reference=b"", date=None,
        mime_type=mime_type, size=size, dc_id=1, attributes=attrs,
    )
    return types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="",
                         media=types.MessageMediaDocument(document=document))


def photo_file_message(photo_id=8):
    """Una foto comprimida: sin Document, así que siempre va por la vía estándar."""
    photo = types.Photo(id=photo_id, access_hash=1, file_reference=b"", date=None, dc_id=1,
                        sizes=[types.PhotoSize(type="x", w=10, h=10, size=500)])
    return types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="",
                         media=types.MessageMediaPhoto(photo=photo))


class FakeTelegram:
    """Lo que hace Telegram al descargar: escribe el fichero o falla.

    `failures` es una lista de excepciones que se lanzan, una por intento,
    antes de que empiece a funcionar.
    """

    def __init__(self, content=b"contenido"):
        self.content = content
        self.failures = []
        self.calls = []
        self.write_partial_before_failing = False
        self.hang = None  # asyncio.Event para quedarse esperando

    async def download_media(self, message, file=None, progress_callback=None):
        self.calls.append(file.name)
        if self.hang is not None:
            await telethon_write(file, b"parcial")
            await self.hang.wait()
        if self.failures:
            failure = self.failures.pop(0)
            if self.write_partial_before_failing:
                await telethon_write(file, b"parcial")
            raise failure
        await telethon_write(file, self.content)
        if progress_callback is not None:
            await progress_callback(len(self.content), len(self.content))
        return file


@pytest.fixture
def env(quiet_bot, tmp_path, monkeypatch, config_dir):
    """Carpetas propias, TEMP_DIR vacío y Telegram simulado."""
    dropbot = quiet_bot
    paths = {kind: str(tmp_path / kind) for kind in
             ("audio", "video", "photo", "torrent", "ebook", "url_video", "url_audio")}
    general = str(tmp_path / "downloads")
    for path in (*paths.values(), general):
        os.makedirs(path, exist_ok=True)
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(dropbot, "DOWNLOAD_PATHS", paths)
    monkeypatch.setattr(dropbot, "DOWNLOAD_PATH", general)
    monkeypatch.setattr(dropbot, "TEMP_DIR", str(temp))
    monkeypatch.setattr(dropbot, "RETRY_DELAY_SECONDS", 0)
    # La ruta rápida tiene sus propios tests; aquí, la estándar
    settings_put(dropbot, "downloads.fast_connections", 1)

    telegram = FakeTelegram()
    monkeypatch.setattr(dropbot.bot, "download_media", telegram.download_media)

    async def file_info(path):
        return {"type": "document", "size_formatted": "1 KB", "duration_formatted": None,
                "resolution": None, "codec_video": None, "codec_audio": None, "bitrate": None}

    monkeypatch.setattr(dropbot, "get_file_info", file_info)

    class Env:
        pass

    e = Env()
    e.dropbot, e.telegram, e.paths, e.general, e.temp = dropbot, telegram, paths, general, temp
    e.files = lambda folder: sorted(os.listdir(folder))
    e.temp_files = lambda: sorted(os.listdir(temp))
    return e


def settings_put(dropbot, key, value):
    dropbot.settings.put(key, value)


def _event(make_event, message, event_id=1):
    event = make_event(event_id=event_id)
    event.message = message
    return event


class TestDescargaCorrecta:
    def test_el_fichero_acaba_en_su_carpeta_y_no_en_temp(
        self, env, make_event, run_async
    ):
        run_async(env.dropbot.download_media(_event(make_event, file_message("peli.mkv"))))

        assert env.files(env.paths["video"]) == ["peli.mkv"]
        with open(os.path.join(env.paths["video"], "peli.mkv"), "rb") as fh:
            assert fh.read() == b"contenido"
        assert env.temp_files() == []
        assert env.files(env.general) == []

    def test_se_descarga_a_un_oculto_en_su_carpeta(self, env, make_event, run_async):
        """No aparece en /list mientras se descarga ni llena el disco del contenedor.
        Con el id del bot: si otro comparte la carpeta, al arrancar no la borra."""
        run_async(env.dropbot.download_media(_event(make_event, file_message("a.pdf"))))

        (temp_path,) = env.telegram.calls
        assert os.path.dirname(temp_path) == env.paths["ebook"]
        assert os.path.basename(temp_path).startswith(partial_name(""))

    def test_en_su_carpeta_basta_con_renombrar(
        self, env, make_event, run_async, monkeypatch
    ):
        """Ni se escribe dos veces ni ocupa el doble mientras se mueve."""
        def no_copy(*args, **kwargs):
            raise AssertionError("no hace falta copiar")

        monkeypatch.setattr(shutil, "copyfile", no_copy)
        monkeypatch.setattr(shutil, "copyfileobj", no_copy)

        run_async(env.dropbot.download_media(_event(make_event, file_message("peli.mkv"))))

        assert env.files(env.paths["video"]) == ["peli.mkv"]

    @pytest.mark.parametrize("name, mime_type", [
        ("LICENSE", "text/plain"),
        ("contrato", "application/pdf"),
    ])
    def test_un_fichero_sin_extension_con_tipo_mime_se_guarda(
        self, env, make_event, run_async, name, mime_type
    ):
        """Telethon añade la extensión del tipo MIME a una ruta que no la tiene."""
        run_async(env.dropbot.download_media(
            _event(make_event, file_message(name, mime_type=mime_type))
        ))

        assert env.files(env.general) == [name]

    def test_un_nombre_de_255_bytes_se_descarga_y_su_repetido_tambien(
        self, env, make_event, run_async
    ):
        """El temporal no lleva el nombre, y el sufijo " (1)" recorta lo que sobra."""
        name = "я" * 125 + "x.pdf"  # 255 bytes, lo más que deja sanitize_filename

        run_async(env.dropbot.download_media(_event(make_event, file_message(name))))
        run_async(env.dropbot.download_media(_event(make_event, file_message(name))))

        files = env.files(env.paths["ebook"])
        assert name in files and len(files) == 2
        (second,) = [f for f in files if f != name]
        assert second.endswith(" (1).pdf") and len(second.encode("utf-8")) <= 255

    def test_avisa_al_empezar_y_al_terminar(
        self, env, sent_messages, texts, make_event, run_async
    ):
        run_async(env.dropbot.download_media(_event(make_event, file_message("a.pdf"))))

        kinds = [kind for kind, _, _ in sent_messages]
        assert kinds[0] == "reply" and "Descargando" in sent_messages[0][1]
        assert "delete" in kinds, "el mensaje de progreso se borra al acabar"
        assert "Descarga completada" in texts() and "a.pdf" in texts()

    def test_el_boton_de_cancelar_apunta_a_esta_descarga(
        self, env, sent_messages, make_event, run_async
    ):
        run_async(env.dropbot.download_media(
            _event(make_event, file_message("a.pdf"), event_id=321)
        ))

        buttons = sent_messages[0][2]["buttons"]
        assert button_data(buttons[0]) == b"cancel:42_321"

    def test_nombre_repetido_no_pisa_el_existente(self, env, make_event, run_async):
        existing = os.path.join(env.paths["ebook"], "libro.epub")
        with open(existing, "wb") as fh:
            fh.write(b"el de antes")

        run_async(env.dropbot.download_media(_event(make_event, file_message("libro.epub"))))

        assert env.files(env.paths["ebook"]) == ["libro (1).epub", "libro.epub"]
        with open(existing, "rb") as fh:
            assert fh.read() == b"el de antes"

    def _two_at_once(self, env, make_event, run_async, monkeypatch, clock):
        monkeypatch.setattr(env.dropbot, "time", SimpleNamespace(time=clock))

        async def both():
            return await asyncio.gather(
                env.dropbot.download_media(_event(make_event, file_message("x.mp3"), 1)),
                env.dropbot.download_media(_event(make_event, file_message("x.mp3"), 2)),
                return_exceptions=True,
            )

        return run_async(both())

    def test_dos_descargas_con_el_mismo_nombre_no_se_pisan(
        self, env, make_event, run_async, monkeypatch
    ):
        ticks = itertools.count(1_700_000_000)
        results = self._two_at_once(env, make_event, run_async, monkeypatch,
                                    clock=lambda: next(ticks))

        assert results == [None, None]
        assert env.files(env.paths["audio"]) == ["x (1).mp3", "x.mp3"]
        assert env.temp_files() == []

    def test_dos_descargas_en_el_mismo_milisegundo_no_comparten_temporal(
        self, env, make_event, run_async, monkeypatch
    ):
        results = self._two_at_once(env, make_event, run_async, monkeypatch,
                                    clock=lambda: 1_700_000_000.0)

        assert results == [None, None]
        assert env.files(env.paths["audio"]) == ["x (1).mp3", "x.mp3"]

    def test_un_comprimido_se_guarda_tal_cual_en_la_general(
        self, env, make_event, run_async
    ):
        """Lo que se manda por Telegram no se descomprime solo."""
        run_async(env.dropbot.download_media(_event(make_event, file_message("fotos.zip"))))

        assert env.files(env.general) == ["fotos.zip"]

    def test_un_torrent_que_el_gestor_se_lleva_al_instante_no_es_un_error(
        self, env, texts, make_event, run_async, monkeypatch
    ):
        """El cliente de torrents vigila la carpeta y puede borrarlo enseguida."""
        real = env.dropbot.copy_and_remove

        def move_and_vanish(src, dst):
            real(src, dst)
            os.remove(dst)

        monkeypatch.setattr(env.dropbot, "copy_and_remove", move_and_vanish)

        run_async(env.dropbot.download_media(_event(make_event, file_message("ubuntu.torrent"))))

        assert "Descarga completada" in texts()
        assert len(env.telegram.calls) == 1, "no debe reintentar"
        assert env.temp_files() == []

    def test_sin_mensaje_de_estado_descarga_igual(
        self, env, make_event, run_async, monkeypatch
    ):
        """Si la cola no devuelve el mensaje, se descarga sin barra de progreso."""
        replies = []

        async def reply(event, text=None, **kwargs):
            replies.append(text)
            return None

        monkeypatch.setattr(env.dropbot, "safe_reply", reply)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.pdf"))))

        assert env.files(env.paths["ebook"]) == ["a.pdf"]

    @pytest.mark.parametrize("failure", [
        errors.ChatWriteForbiddenError(request=None),
        ValueError("Request was unsuccessful 5 time(s)"),
    ])
    def test_si_no_se_puede_mandar_el_aviso_descarga_igual(
        self, env, make_event, run_async, monkeypatch, failure
    ):
        """Un grupo donde el bot no puede escribir, o Telegram fallando más de lo
        que aguanta la cola: el aviso no sale, pero el fichero sí se guarda."""
        async def reply(event, text=None, **kwargs):
            raise failure

        monkeypatch.setattr(env.dropbot, "safe_reply", reply)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.pdf"))))

        assert env.files(env.paths["ebook"]) == ["a.pdf"]

    def test_una_edicion_de_progreso_dormida_ni_para_la_descarga_ni_pisa_el_final(
        self, env, make_event, run_async, monkeypatch
    ):
        """Ante un FloodWait de menos de un minuto Telethon duerme dentro del edit."""
        edits = []
        gate = None

        class Status:
            async def edit(self, text, **kwargs):
                await gate.wait()
                edits.append(text)

        async def reply(event, text=None, **kwargs):
            return Status()

        monkeypatch.setattr(env.dropbot, "safe_reply", reply)

        async def scenario():
            nonlocal gate
            gate = asyncio.Event()
            await asyncio.wait_for(
                env.dropbot.download_media(_event(make_event, file_message("a.pdf"))),
                timeout=30,
            )
            gate.set()
            for _ in range(3):
                await asyncio.sleep(0)

        run_async(scenario())

        assert env.files(env.paths["ebook"]) == ["a.pdf"]
        assert edits == [], "la barra no vuelve sobre el mensaje ya borrado o cambiado"

    @pytest.mark.parametrize("failure", [asyncio.TimeoutError(), RuntimeError("MESSAGE_ID_INVALID")])
    def test_si_no_se_puede_borrar_el_progreso_termina_igual(
        self, env, texts, make_event, run_async, monkeypatch, failure
    ):
        async def delete(message, *args, **kwargs):
            raise failure

        monkeypatch.setattr(env.dropbot, "safe_delete", delete)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.pdf"))))

        assert env.files(env.paths["ebook"]) == ["a.pdf"]
        assert "Descarga completada" in texts()

    def test_si_telegram_no_entrega_nada_no_aparece_un_fichero_vacio(
        self, env, make_event, run_async, monkeypatch
    ):
        """download_media de Telethon puede volver sin escribir (media no descargable)."""
        async def nothing(message, file=None, progress_callback=None):
            return None

        monkeypatch.setattr(env.dropbot.bot, "download_media", nothing)

        with pytest.raises(FileNotFoundError):
            run_async(env.dropbot.download_media(_event(make_event, file_message("a.pdf"))))

        assert env.files(env.paths["ebook"]) == []

    def test_un_mensaje_sin_media_no_hace_nada(self, env, sent_messages, make_event, run_async):
        message = types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="hola")
        run_async(env.dropbot.download_media(_event(make_event, message)))
        assert sent_messages == [] and env.telegram.calls == []


class TestReintentos:
    @pytest.mark.parametrize("failure", [
        ConnectionError("Connection reset by peer"),
        TimeoutError("read timed out"),
        ValueError("Request was unsuccessful 6 time(s)"),
        errors.RpcCallFailError(request=GET_FILE),
        errors.TimedOutError(request=GET_FILE, message="Timeout"),
        # Telegram corta la conexión por un flood a nivel de transporte
        InvalidBufferError((-429).to_bytes(4, "little", signed=True)),
    ])
    def test_un_error_transitorio_se_reintenta(
        self, env, texts, make_event, run_async, failure
    ):
        env.telegram.failures = [failure]
        env.telegram.write_partial_before_failing = True

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert len(env.telegram.calls) == 2
        assert "Reintentando descarga" in texts() and "intento 2 de 3" in texts()
        assert env.files(env.paths["video"]) == ["a.mkv"]
        assert env.temp_files() == []

    def test_una_peticion_que_telethon_cancela_al_desconectarse_se_reintenta(
        self, env, texts, make_event, run_async, monkeypatch
    ):
        """Al desconectarse (un corte con 429), Telethon cancela las peticiones
        en vuelo de las conexiones con otros DC: la descarga acababa en
        «Cancelada», como si se hubiera pulsado Cancelar, y no se reintentaba."""
        download = env.telegram.download_media

        async def cut_once(message, file=None, progress_callback=None):
            if not env.telegram.calls:
                env.telegram.calls.append(file.name)
                request = asyncio.get_running_loop().create_future()
                request.cancel()  # lo que hace disconnect() con lo que estaba en vuelo
                await request
            return await download(message, file=file, progress_callback=progress_callback)

        monkeypatch.setattr(env.dropbot.bot, "download_media", cut_once)

        run_async(env.dropbot.limited_download(_event(make_event, file_message("a.mkv"))))

        assert len(env.telegram.calls) == 2
        assert "Reintentando descarga" in texts() and "cancelada" not in texts()
        assert env.files(env.paths["video"]) == ["a.mkv"]

    def test_agotados_los_reintentos_avisa_y_limpia(
        self, env, sent_messages, make_event, run_async
    ):
        env.telegram.failures = [ConnectionError("network down")] * env.dropbot.MAX_DOWNLOAD_RETRIES
        env.telegram.write_partial_before_failing = True

        async def scenario():
            # La tarea de limited_download, que es la que la registra
            env.dropbot.active_tasks["42_77"] = asyncio.current_task()
            await env.dropbot.download_media(_event(make_event, file_message("a.mkv"), 77))

        run_async(scenario())

        assert len(env.telegram.calls) == env.dropbot.MAX_DOWNLOAD_RETRIES
        last_kind, last_text, last_kwargs = sent_messages[-1]
        assert last_kind == "edit" and "problemas internos" in last_text
        assert last_kwargs.get("buttons") is None, "ya no hay nada que cancelar"
        assert env.temp_files() == []
        assert env.files(env.paths["video"]) == []
        assert "42_77" not in env.dropbot.active_tasks

    def test_un_floodwait_espera_lo_que_pide_telegram_antes_de_reintentar(
        self, env, texts, make_event, run_async, monkeypatch
    ):
        """Los de más de un minuto Telethon los lanza en vez de dormirlos; a los
        5 s de siempre seguirían vigentes y se gastarían los tres intentos."""
        env.telegram.failures = [errors.FloodWaitError(request=GET_FILE, capture=300)]
        waits = []
        real_sleep = asyncio.sleep

        async def sleep(delay, *args, **kwargs):
            waits.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(env.dropbot.asyncio, "sleep", sleep)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert 300 in waits
        assert "Telegram pide esperar 300 s" in texts()
        assert env.files(env.paths["video"]) == ["a.mkv"]


class TestCarpetaCompartida:
    """El temporal está en la carpeta de descargas, que pueden ver y escribir
    otros contenedores (un cliente de torrents, una carpeta de Unraid en 0777)."""

    def test_un_enlace_con_el_nombre_del_temporal_no_recibe_el_reintento(
        self, env, make_event, run_async, tmp_path, monkeypatch
    ):
        """Cada intento volvía a abrir el mismo nombre: en la espera, otro podía
        dejar con él un enlace a cualquier fichero, y el bot (que va como root)
        escribía la descarga en su destino."""
        victim = tmp_path / "dropbot.py"
        victim.write_bytes(b"codigo")
        download = env.telegram.download_media

        async def cut_then_plant(message, file=None, progress_callback=None):
            if not env.telegram.calls:
                env.telegram.calls.append(file.name)
                await telethon_write(file, b"parcial")
                raise ConnectionError("network down")
            # En la espera, el otro contenedor pone un enlace con el nombre que ha visto
            os.symlink(victim, env.telegram.calls[0])
            return await download(message, file=file, progress_callback=progress_callback)

        monkeypatch.setattr(env.dropbot.bot, "download_media", cut_then_plant)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        first, second = env.telegram.calls
        assert first != second, "cada intento, un temporal nuevo"
        assert victim.read_bytes() == b"codigo"
        assert env.files(env.paths["video"]) == sorted(["a.mkv", os.path.basename(first)])
        with open(os.path.join(env.paths["video"], "a.mkv"), "rb") as fh:
            assert fh.read() == b"contenido"

    def test_lo_que_otro_pone_en_lugar_del_temporal_no_se_guarda_como_la_descarga(
        self, env, texts, make_event, run_async, monkeypatch
    ):
        """Con el nombre del temporal se renombraba lo que hubiera en ese
        momento: algo de otro acababa en la carpeta como el fichero mandado."""
        download = env.telegram.download_media

        async def swap(message, file=None, progress_callback=None):
            result = await download(message, file=file, progress_callback=progress_callback)
            os.remove(file.name)
            with open(file.name, "wb") as other:
                other.write(b"de otro")
            return result

        monkeypatch.setattr(env.dropbot.bot, "download_media", swap)

        with pytest.raises(FileNotFoundError):
            run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        (name,) = env.files(env.paths["video"])
        assert name != "a.mkv", "lo suyo se queda con su nombre, sin tocar"
        with open(os.path.join(env.paths["video"], name), "rb") as fh:
            assert fh.read() == b"de otro"
        assert "a.mkv" in texts() and "Descarga completada" not in texts()


class TestDescargaParada:
    """Se corta la descarga que deja de recibir, no la que tarda en total.

    Con un plazo total, una línea lenta o varias descargas a la vez cortaban
    ficheros grandes que avanzaban bien, y los tres intentos fallaban igual.
    """

    @pytest.fixture(autouse=True)
    def short_stall(self, env, monkeypatch):
        # Entre dos avances hay escrituras y aperturas en hilos: con 0,2 s, un
        # parón de la máquina cortaba la descarga que sí avanzaba, o el intento
        # se cortaba antes de llegar a pedírselo a Telegram
        monkeypatch.setattr(env.dropbot, "DOWNLOAD_STALL_TIMEOUT", 1)

    def _slow(self, env, monkeypatch, chunks=60, pause=0.02):
        """Una línea lenta: un trozo cada `pause` s, y en total más que el plazo."""
        async def download(message, file=None, progress_callback=None):
            for done in range(1, chunks + 1):
                await asyncio.sleep(pause)
                await telethon_write(file, b"x")
                await progress_callback(done, chunks)
            return file

        monkeypatch.setattr(env.dropbot.bot, "download_media", download)

    def _stalls(self, env, monkeypatch, times):
        """Las `times` primeras descargas dejan de recibir tras el primer trozo."""
        calls = []

        async def download(message, file=None, progress_callback=None):
            calls.append(file.name)
            await telethon_write(file, b"parcial")
            await progress_callback(7, 100)
            if len(calls) <= times:
                await asyncio.Event().wait()
            await telethon_write(file, b"contenido")
            return file

        monkeypatch.setattr(env.dropbot.bot, "download_media", download)
        return calls

    def test_una_descarga_lenta_que_avanza_no_se_corta(
        self, env, texts, make_event, run_async, monkeypatch
    ):
        self._slow(env, monkeypatch)

        run_async(env.dropbot.download_media(_event(make_event, file_message("peli.mkv"))))

        assert env.files(env.paths["video"]) == ["peli.mkv"]
        assert "Reintentando" not in texts()

    def test_sin_mensaje_de_estado_tambien_ve_que_avanza(
        self, env, make_event, run_async, monkeypatch
    ):
        """El vigilante recibe el progreso aunque no haya barra que editar."""
        async def reply(event, text=None, **kwargs):
            return None

        monkeypatch.setattr(env.dropbot, "safe_reply", reply)
        self._slow(env, monkeypatch)

        run_async(env.dropbot.download_media(_event(make_event, file_message("peli.mkv"))))

        assert env.files(env.paths["video"]) == ["peli.mkv"]

    def test_una_descarga_parada_se_corta_y_se_reintenta(
        self, env, texts, make_event, run_async, monkeypatch
    ):
        calls = self._stalls(env, monkeypatch, times=1)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert len(calls) == 2
        assert "Reintentando descarga" in texts()
        assert env.files(env.paths["video"]) == ["a.mkv"]

    def test_si_se_para_en_todos_los_intentos_lo_dice_y_limpia(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        # Cada intento parado espera el plazo entero: con dos ya se ve que se
        # agotan todos, y con los de config eran 3 s
        monkeypatch.setattr(env.dropbot, "MAX_DOWNLOAD_RETRIES", 2)
        calls = self._stalls(env, monkeypatch, times=env.dropbot.MAX_DOWNLOAD_RETRIES)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert len(calls) == env.dropbot.MAX_DOWNLOAD_RETRIES
        last_text = sent_messages[-1][1]
        assert "se ha quedado parada" in last_text and "problemas internos" not in last_text
        assert env.files(env.paths["video"]) == []


class TestErroresNoReintentables:
    def test_sin_espacio_no_reintenta_y_dice_donde_falta(
        self, env, sent_messages, make_event, run_async
    ):
        env.telegram.failures = [OSError(errno.ENOSPC, "No space left on device")]
        env.telegram.write_partial_before_failing = True

        run_async(env.dropbot.limited_download(_event(make_event, file_message("a.mkv"))))

        assert len(env.telegram.calls) == 1
        last_text = sent_messages[-1][1]
        assert "no queda espacio" in last_text and env.paths["video"] in last_text
        assert env.files(env.paths["video"]) == []

    def _missing_destination(self, env, monkeypatch):
        """La carpeta de destino ha desaparecido (volumen desmontado)."""
        missing = os.path.join(env.general, "no-existe")
        paths = dict(env.paths, video=missing)
        monkeypatch.setattr(env.dropbot, "DOWNLOAD_PATHS", paths)

    def test_no_reintenta_un_error_que_no_es_transitorio(
        self, env, texts, make_event, run_async, monkeypatch
    ):
        self._missing_destination(env, monkeypatch)

        with pytest.raises(FileNotFoundError):
            run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        # Sin la carpeta no se puede abrir el temporal: ni se le pide a Telegram
        assert env.telegram.calls == []
        assert "Reintentando" not in texts()

    def test_un_error_no_reintentable_no_deja_el_temporal(
        self, env, make_event, run_async
    ):
        env.telegram.failures = [RuntimeError("FILE_REFERENCE_EXPIRED")]
        env.telegram.write_partial_before_failing = True

        with pytest.raises(RuntimeError):
            run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert env.files(env.paths["video"]) == []

    def test_un_error_no_reintentable_se_le_dice_al_usuario(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        env.telegram.failures = [RuntimeError("FILE_REFERENCE_EXPIRED")]

        run_async(env.dropbot.limited_download(_event(make_event, file_message("a.mkv"))))

        last_kind, last_text, last_kwargs = sent_messages[-1]
        assert last_kind == "edit", "el mensaje 'Descargando...' tiene que cambiar"
        assert not last_kwargs.get("buttons")

    @pytest.mark.parametrize("failure", [
        errors.FileIdInvalidError(request=GET_FILE),
        RuntimeError("Telegram internal error: check the network connection"),
    ])
    def test_se_decide_por_el_tipo_del_error_y_no_por_su_texto(
        self, env, sent_messages, make_event, run_async, failure
    ):
        """Todo error de Telegram dice "(caused by GetFileRequest)", y antes eso
        bastaba para reintentarlo, igual que un "network" o un "internal"."""
        env.telegram.failures = [failure]

        run_async(env.dropbot.limited_download(_event(make_event, file_message("a.mkv"))))

        assert len(env.telegram.calls) == 1
        assert "Consulta el log" in sent_messages[-1][1]

    def test_un_floodwait_de_mas_del_tope_no_se_reintenta(
        self, env, sent_messages, make_event, run_async
    ):
        seconds = env.dropbot.FLOOD_WAIT_MAX_SECONDS + 1
        env.telegram.failures = [errors.FloodWaitError(request=GET_FILE, capture=seconds)]

        run_async(env.dropbot.limited_download(_event(make_event, file_message("a.mkv"))))

        assert len(env.telegram.calls) == 1
        assert "Consulta el log" in sent_messages[-1][1]

    def test_si_falla_al_moverlo_no_se_vuelve_a_descargar(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        """El error de mover lleva las rutas: con "Internal" o "Network" en el
        nombre se bajaba entero otra vez, tres veces."""
        def denied(src, dst):
            os.remove(dst)  # lo que hace copy_and_remove cuando falla
            raise PermissionError(errno.EACCES, "Permission denied", src, None, dst)

        monkeypatch.setattr(env.dropbot, "copy_and_remove", denied)

        run_async(env.dropbot.limited_download(
            _event(make_event, file_message("Internal Affairs (2023).mkv"))
        ))

        assert len(env.telegram.calls) == 1
        assert "Consulta el log" in sent_messages[-1][1]
        assert env.files(env.paths["video"]) == []


class TestReferenciaCaducada:
    """Una foto que espera mucho en la cola puede llegar con la referencia
    caducada, y Telethon solo la renueva por su cuenta en los documentos."""

    def _downloads(self, env, monkeypatch, failures):
        seen = []

        async def download(message, file=None, progress_callback=None):
            seen.append(message)
            if failures:
                raise failures.pop(0)
            await telethon_write(file, b"foto")
            return file

        monkeypatch.setattr(env.dropbot.bot, "download_media", download)
        return seen

    def _message_again(self, env, monkeypatch, message):
        asked = []

        async def get_messages(chat_id, ids=None):
            asked.append((chat_id, ids))
            return message

        monkeypatch.setattr(env.dropbot.bot, "get_messages", get_messages)
        return asked

    def test_se_vuelve_a_pedir_el_mensaje_y_se_descarga_sin_esperar(
        self, env, make_event, run_async, monkeypatch
    ):
        # Si esperara entre intentos se vería: el plazo queda muy por debajo
        monkeypatch.setattr(env.dropbot, "RETRY_DELAY_SECONDS", 300)
        seen = self._downloads(env, monkeypatch, [errors.FileReferenceExpiredError(request=GET_FILE)])
        fresh = photo_file_message()
        asked = self._message_again(env, monkeypatch, fresh)
        event = _event(make_event, photo_file_message(), event_id=70)

        run_async(asyncio.wait_for(env.dropbot.download_media(event), timeout=30))

        assert asked == [(event.chat_id, 70)]
        assert seen[1] is fresh
        assert env.files(env.paths["photo"]) == ["photo_8.jpg"]

    def test_si_el_mensaje_ya_no_esta_pide_reenviarlo(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        seen = self._downloads(env, monkeypatch, [errors.FileReferenceExpiredError(request=GET_FILE)])
        self._message_again(env, monkeypatch, None)

        run_async(env.dropbot.limited_download(_event(make_event, photo_file_message(), 71)))

        assert len(seen) == 1
        assert "ya no está disponible" in sent_messages[-1][1]
        assert env.files(env.paths["photo"]) == []

    def test_solo_se_renueva_una_vez(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        seen = self._downloads(env, monkeypatch, [
            errors.FileReferenceExpiredError(request=GET_FILE),
            errors.FileReferenceExpiredError(request=GET_FILE),
        ])
        self._message_again(env, monkeypatch, photo_file_message())

        run_async(env.dropbot.limited_download(_event(make_event, photo_file_message(), 72)))

        assert len(seen) == 2
        assert "Consulta el log" in sent_messages[-1][1]
        assert env.files(env.paths["photo"]) == []

    def test_cancelar_mientras_se_pide_el_mensaje_avisa_y_no_deja_el_temporal(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        """Se pedía dentro del except del fallo, y la cancelación no pasaba por
        la suya: quedaban «Descargando...» con un Cancelar que ya no paraba
        nada y el temporal oculto en la carpeta."""
        self._downloads(env, monkeypatch, [errors.FileReferenceExpiredError(request=GET_FILE)])
        asked = asyncio.Event()

        async def get_messages(chat_id, ids=None):
            asked.set()
            await asyncio.Event().wait()  # Telethon durmiendo un FloodWait

        monkeypatch.setattr(env.dropbot.bot, "get_messages", get_messages)
        event = _event(make_event, photo_file_message(), 73)

        async def scenario():
            await env.dropbot.handle_files(event)
            task = env.dropbot.active_tasks["42_73"]
            await asyncio.wait_for(asked.wait(), timeout=30)
            cancel = button_data(sent_messages[0][2]["buttons"][0])
            await env.dropbot.cancel_download(make_event(data=cancel))
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=30)

        run_async(scenario())

        last_kind, last_text, last_kwargs = sent_messages[-1]
        assert last_kind == "edit" and "Descarga cancelada" in last_text
        assert not last_kwargs.get("buttons")
        assert env.files(env.paths["photo"]) == []
        assert "42_73" not in env.dropbot.active_tasks


class TestFalloTrasMover:
    def test_si_falla_el_aviso_final_no_se_vuelve_a_descargar(
        self, env, make_event, run_async, monkeypatch
    ):
        real_reply = env.dropbot.safe_reply

        async def reply(event, text=None, **kwargs):
            if "buttons" not in kwargs:  # el aviso de "Descarga completada"
                raise asyncio.TimeoutError()
            return await real_reply(event, text, **kwargs)

        monkeypatch.setattr(env.dropbot, "safe_reply", reply)

        try:
            run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))
        except asyncio.TimeoutError:
            pass

        assert len(env.telegram.calls) == 1
        assert env.files(env.paths["video"]) == ["a.mkv"]


class TestCancelacion:
    def _cancel_midway(self, env, make_event, run_async, event_id=55):
        env.telegram.hang = asyncio.Event()
        event = _event(make_event, file_message("a.mkv"), event_id)

        async def scenario():
            task = asyncio.create_task(env.dropbot.limited_download(event))
            env.dropbot.active_tasks[f"42_{event_id}"] = task
            while not env.telegram.calls:
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run_async(scenario())

    def test_cancelar_borra_el_temporal_y_avisa(
        self, env, sent_messages, make_event, run_async
    ):
        self._cancel_midway(env, make_event, run_async)

        assert env.temp_files() == []
        assert env.files(env.paths["video"]) == []
        assert sent_messages[-1][0] == "edit"
        assert "Descarga cancelada" in sent_messages[-1][1]

    def test_cancelar_libera_la_tarea(self, env, make_event, run_async):
        self._cancel_midway(env, make_event, run_async, event_id=56)
        assert "42_56" not in env.dropbot.active_tasks

    def test_el_mismo_id_en_otro_chat_no_pisa_su_cancelar(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        """Cada supergrupo numera sus mensajes por su cuenta: con solo el id, el
        fichero de un grupo pisaba en active_tasks al del mismo id en privado,
        y el Cancelar de este paraba la descarga del grupo."""
        monkeypatch.setattr(env.dropbot, "download_semaphore", ResizableLimiter(2))
        env.telegram.hang = asyncio.Event()
        private = _event(make_event, file_message("privado.mkv"), 430)
        group = _event(make_event, file_message("grupo.mkv"), 430)
        group.chat_id = -1001234567890

        async def scenario():
            await env.dropbot.handle_files(private)
            await env.dropbot.handle_files(group)
            downloads = asyncio.all_tasks() - {asyncio.current_task()}
            while len(env.telegram.calls) < 2:
                await asyncio.sleep(0)
            # El Cancelar del progreso del privado, que es el primero en salir
            cancel = button_data(sent_messages[0][2]["buttons"][0])
            await env.dropbot.cancel_download(make_event(data=cancel))
            env.telegram.hang.set()
            await asyncio.wait(downloads, timeout=30)
            return cancel

        cancel = run_async(scenario())

        assert cancel == b"cancel:42_430"
        assert env.files(env.paths["video"]) == ["grupo.mkv"]
        assert not {"42_430", "-1001234567890_430"} & env.dropbot.active_tasks.keys()

    def test_cancelar_durante_la_copia_no_deja_nada(
        self, env, make_event, run_async, monkeypatch
    ):
        """Si se cancela con el nombre final ya reservado, también se borra."""
        event = _event(make_event, file_message("a.mkv"), 57)
        real_to_thread = asyncio.to_thread

        async def slow_to_thread(func, *args):
            # Solo la copia: el temporal también se abre y se cierra en un hilo
            if func is env.dropbot.copy_and_remove:
                await asyncio.Event().wait()
            return await real_to_thread(func, *args)

        monkeypatch.setattr(env.dropbot.asyncio, "to_thread", slow_to_thread)

        async def scenario():
            task = asyncio.create_task(env.dropbot.limited_download(event))
            env.dropbot.active_tasks["42_57"] = task
            # El temporal oculto aparece antes: se espera a la reserva
            while "a.mkv" not in os.listdir(env.paths["video"]):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run_async(scenario())

        assert env.files(env.paths["video"]) == []
        assert env.temp_files() == []
        assert "42_57" not in env.dropbot.active_tasks

    def test_si_la_copia_falla_no_borra_el_fichero_de_otra_descarga(
        self, env, make_event, run_async, monkeypatch
    ):
        """Si la copia falla, su reserva ya no es nuestra: otra descarga con el
        mismo nombre puede ocuparla. Y no se vuelve a bajar de Telegram."""
        theirs = os.path.join(env.paths["video"], "a.mkv")

        def fails(src, dst):
            os.remove(dst)  # lo que hace copy_and_remove cuando falla
            # Mientras tanto, otra descarga guarda su a.mkv con el nombre libre
            with open(theirs, "wb") as fh:
                fh.write(b"de B")
            raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")

        monkeypatch.setattr(env.dropbot, "copy_and_remove", fails)

        run_async(env.dropbot.limited_download(_event(make_event, file_message("a.mkv"), 58)))

        assert len(env.telegram.calls) == 1
        assert env.files(env.paths["video"]) == ["a.mkv"]
        with open(theirs, "rb") as fh:
            assert fh.read() == b"de B"

    def test_cancelar_mientras_espera_para_reintentar_deja_el_mensaje_en_cancelado(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        """La espera entre intentos tiene que pasar por el except de la cancelación."""
        monkeypatch.setattr(env.dropbot, "RETRY_DELAY_SECONDS", 30)
        env.telegram.failures = [ConnectionError("Connection reset by peer")]
        event = _event(make_event, file_message("a.mkv"), 59)

        async def scenario():
            task = asyncio.create_task(env.dropbot.limited_download(event))
            env.dropbot.active_tasks["42_59"] = task
            while not any("Reintentando" in text for _, text, _ in sent_messages):
                await asyncio.sleep(0)
            for _ in range(5):  # ya dentro de la espera de 30 s
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run_async(scenario())

        assert len(env.telegram.calls) == 1
        last_kind, last_text, last_kwargs = sent_messages[-1]
        assert last_kind == "edit" and "Descarga cancelada" in last_text
        assert last_kwargs.get("buttons") is None, "el Cancelar ya no cancela nada"
        assert env.files(env.paths["video"]) == []
        assert "42_59" not in env.dropbot.active_tasks


class TestLimitedDownload:
    def test_quita_la_tarea_al_terminar_bien(self, env, make_event, run_async):
        event = _event(make_event, file_message("a.pdf"), 60)

        async def scenario():
            task = asyncio.create_task(env.dropbot.limited_download(event))
            env.dropbot.active_tasks["42_60"] = task
            await task

        run_async(scenario())
        assert "42_60" not in env.dropbot.active_tasks

    def test_al_acabar_no_quita_la_tarea_que_la_ha_pisado(self, env, make_event, run_async):
        """Otra descarga con la misma clave pisa la entrada: al acabar, la
        primera se llevaba la de la otra, que ya no se podía cancelar."""
        env.telegram.hang = asyncio.Event()
        other = object()

        async def scenario():
            task = asyncio.create_task(env.dropbot.download_media(_event(make_event, file_message("a.pdf"), 63)))
            env.dropbot.active_tasks["42_63"] = task
            while not env.telegram.calls:
                await asyncio.sleep(0)
            env.dropbot.active_tasks["42_63"] = other
            env.telegram.hang.set()
            await task

        run_async(scenario())
        assert env.dropbot.active_tasks.pop("42_63") is other

    def test_un_error_inesperado_no_se_escapa(self, env, make_event, run_async, monkeypatch):
        async def explode(event, status_message=None):
            raise RuntimeError("algo raro")

        monkeypatch.setattr(env.dropbot, "download_media", explode)
        event = _event(make_event, file_message("a.pdf"), 62)

        async def scenario():
            task = asyncio.create_task(env.dropbot.limited_download(event))
            env.dropbot.active_tasks["42_62"] = task
            await task

        run_async(scenario())
        assert "42_62" not in env.dropbot.active_tasks

    def test_respeta_el_limite_de_descargas_simultaneas(
        self, env, make_event, run_async, monkeypatch
    ):
        monkeypatch.setattr(env.dropbot, "download_semaphore", ResizableLimiter(2))
        running, peak = [0], [0]
        release = None

        async def download(message, file=None, progress_callback=None):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await release.wait()
            await telethon_write(file, b"x")
            running[0] -= 1

        monkeypatch.setattr(env.dropbot.bot, "download_media", download)

        async def scenario():
            nonlocal release
            release = asyncio.Event()
            tasks = [asyncio.create_task(env.dropbot.limited_download(
                _event(make_event, file_message(f"f{i}.pdf"), 100 + i))) for i in range(5)]
            # El temporal se abre en un hilo: no basta con unas vueltas del bucle.
            # Con >= y no ==: si deja pasar a más, el assert de abajo lo dice
            # en el acto; con == se pasaba de largo y esperaba el plazo entero
            await wait_until(lambda: running[0] >= 2)
            for _ in range(50):
                await asyncio.sleep(0)
            assert running[0] == 2, "solo dos a la vez"
            release.set()
            await asyncio.gather(*tasks)

        run_async(scenario())
        assert peak[0] == 2
        assert len(os.listdir(env.paths["ebook"])) == 5

    def test_subir_el_limite_en_caliente_deja_pasar_a_las_que_esperan(
        self, env, make_event, run_async, monkeypatch
    ):
        """/settings cambia downloads.parallel y apply_setting lo aplica ya."""
        settings_put(env.dropbot, "downloads.parallel", 1)
        monkeypatch.setattr(env.dropbot, "download_semaphore", ResizableLimiter(1))
        running = [0]
        release = None

        async def download(message, file=None, progress_callback=None):
            running[0] += 1
            await release.wait()
            await telethon_write(file, b"x")
            running[0] -= 1

        monkeypatch.setattr(env.dropbot.bot, "download_media", download)

        async def scenario():
            nonlocal release
            release = asyncio.Event()
            tasks = [asyncio.create_task(env.dropbot.limited_download(
                _event(make_event, file_message(f"g{i}.pdf"), 200 + i))) for i in range(4)]
            await wait_until(lambda: running[0] >= 1)
            for _ in range(30):
                await asyncio.sleep(0)
            assert running[0] == 1

            settings_put(env.dropbot, "downloads.parallel", 3)
            await env.dropbot.apply_setting("downloads.parallel")
            await wait_until(lambda: running[0] >= 3)
            for _ in range(30):
                await asyncio.sleep(0)
            assert running[0] == 3
            assert env.dropbot.download_semaphore.limit == 3

            release.set()
            await asyncio.gather(*tasks)

        run_async(scenario())
        assert len(os.listdir(env.paths["ebook"])) == 4

    def test_una_descarga_que_falla_libera_su_hueco(
        self, env, make_event, run_async, monkeypatch
    ):
        monkeypatch.setattr(env.dropbot, "download_semaphore", ResizableLimiter(1))

        async def explode(event):
            raise RuntimeError("fallo")

        real = env.dropbot.download_media
        calls = []

        async def first_fails(event, status_message=None):
            calls.append(event.id)
            if len(calls) == 1:
                return await explode(event)
            return await real(event, status_message)

        monkeypatch.setattr(env.dropbot, "download_media", first_fails)

        async def scenario():
            await env.dropbot.limited_download(_event(make_event, file_message("a.pdf"), 300))
            await asyncio.wait_for(
                env.dropbot.limited_download(_event(make_event, file_message("b.pdf"), 301)),
                timeout=30,
            )

        run_async(scenario())
        assert env.dropbot.download_semaphore.active == 0
        assert env.files(env.paths["ebook"]) == ["b.pdf"]


class TestEnCola:
    """Lo que espera hueco se ve, y se puede cancelar, desde que llega."""

    @pytest.fixture
    def one_slot(self, env, monkeypatch):
        """Un solo hueco; cada descarga se queda bajando hasta abrir `release`."""
        monkeypatch.setattr(env.dropbot, "download_semaphore", ResizableLimiter(1))
        state = SimpleNamespace(release=None, started=[])

        async def download(message, file=None, progress_callback=None):
            state.started.append(message.file.name)
            await state.release.wait()
            await telethon_write(file, b"x")

        monkeypatch.setattr(env.dropbot.bot, "download_media", download)
        return state

    def _start(self, env, make_event, name, event_id):
        task = asyncio.create_task(env.dropbot.limited_download(
            _event(make_event, file_message(name), event_id)))
        env.dropbot.active_tasks[f"42_{event_id}"] = task
        return task

    @staticmethod
    async def _settle():
        for _ in range(20):
            await asyncio.sleep(0)

    def test_si_no_hay_hueco_avisa_ya_con_su_boton_de_cancelar(
        self, env, one_slot, sent_messages, make_event, run_async
    ):
        async def scenario():
            one_slot.release = asyncio.Event()
            first = self._start(env, make_event, "a.pdf", 400)
            second = self._start(env, make_event, "b.pdf", 401)
            await self._settle()

            queued = [(kind, kwargs) for kind, text, kwargs in sent_messages if "En cola" in text]
            assert len(queued) == 1 and queued[0][0] == "reply"
            assert [button_data(b) for b in queued[0][1]["buttons"]] == [b"cancel:42_401"]

            one_slot.release.set()
            await asyncio.gather(first, second)

        run_async(scenario())

        downloading = [kind for kind, text, _ in sent_messages if "Descargando" in text]
        assert downloading == ["reply", "edit"], "la segunda reutiliza el aviso de la cola"
        assert env.files(env.paths["ebook"]) == ["a.pdf", "b.pdf"]

    def test_con_hueco_no_hay_aviso_de_cola(
        self, env, one_slot, sent_messages, make_event, run_async
    ):
        async def scenario():
            one_slot.release = asyncio.Event()
            one_slot.release.set()
            await self._start(env, make_event, "a.pdf", 410)

        run_async(scenario())

        assert not any("En cola" in text for _, text, _ in sent_messages)

    def test_una_descarga_terminada_no_retiene_su_hueco_mientras_la_cola_espera(
        self, env, one_slot, sent_messages, make_event, run_async, monkeypatch
    ):
        """Detrás de un FloodWait la cola tarda minutos. El borrado del progreso
        y el aviso de «descargado» se esperaban con el hueco cogido, y las que
        estaban en cola no empezaban hasta que salieran."""
        real_reply, real_delete = env.dropbot.safe_reply, env.dropbot.safe_delete

        # La cola, parada para lo que llega después de descargar
        async def reply(event, text=None, wait_for_result=False, **kwargs):
            if wait_for_result and "Descarga completada" in text:
                await asyncio.Event().wait()
            return await real_reply(event, text, wait_for_result=wait_for_result, **kwargs)

        async def delete(message, *args, wait_for_result=False, **kwargs):
            if wait_for_result:
                await asyncio.Event().wait()
            return await real_delete(message, *args, **kwargs)

        monkeypatch.setattr(env.dropbot, "safe_reply", reply)
        monkeypatch.setattr(env.dropbot, "safe_delete", delete)

        async def scenario():
            one_slot.release = asyncio.Event()
            first = self._start(env, make_event, "a.pdf", 440)
            second = self._start(env, make_event, "b.pdf", 441)
            await wait_until(lambda: one_slot.started)
            await self._settle()
            assert one_slot.started == ["a.pdf"]

            one_slot.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=30)

        run_async(scenario())

        assert one_slot.started == ["a.pdf", "b.pdf"]
        assert env.files(env.paths["ebook"]) == ["a.pdf", "b.pdf"]
        assert sum("Descarga completada" in text for _, text, _ in sent_messages) == 2
        assert [kind for kind, _, _ in sent_messages].count("delete") == 2

    def test_cancelar_en_la_cola_no_la_descarga_y_deja_el_aviso_en_cancelado(
        self, env, one_slot, sent_messages, make_event, run_async
    ):
        async def scenario():
            one_slot.release = asyncio.Event()
            first = self._start(env, make_event, "a.pdf", 420)
            second = self._start(env, make_event, "b.pdf", 421)
            await self._settle()

            await env.dropbot.cancel_download(make_event(data=b"cancel:42_421"))
            with pytest.raises(asyncio.CancelledError):
                await second

            last_kind, last_text, last_kwargs = sent_messages[-1]
            assert last_kind == "edit" and "Descarga cancelada" in last_text
            assert last_kwargs.get("buttons") is None

            one_slot.release.set()
            await first

        run_async(scenario())

        assert one_slot.started == ["a.pdf"]
        assert "42_421" not in env.dropbot.active_tasks
        assert env.dropbot.download_semaphore.active == 0
        assert env.files(env.paths["ebook"]) == ["a.pdf"]
