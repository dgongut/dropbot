"""download_media / limited_download: un fichero mandado al bot hasta el disco.

`bot.download_media` se sustituye por una función que escribe un fichero de
verdad en la ruta temporal que le pide el bot, así que lo que se comprueba es
lo que queda en disco (carpeta final, TEMP_DIR) y lo que ve el usuario.
"""

import asyncio
import itertools
import os
from types import SimpleNamespace

import pytest
from button_data import button_data
from telethon.tl import types

from utils.limiter import ResizableLimiter


def file_message(name, size=1000, attributes=(), doc_id=7):
    attrs = [types.DocumentAttributeFilename(file_name=name), *attributes]
    document = types.Document(
        id=doc_id, access_hash=1, file_reference=b"", date=None,
        mime_type="application/octet-stream", size=size, dc_id=1, attributes=attrs,
    )
    return types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="",
                         media=types.MessageMediaDocument(document=document))


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
        self.calls.append(file)
        if self.hang is not None:
            with open(file, "wb") as out:
                out.write(b"parcial")
            await self.hang.wait()
        if self.failures:
            failure = self.failures.pop(0)
            if self.write_partial_before_failing:
                with open(file, "wb") as out:
                    out.write(b"parcial")
            raise failure
        with open(file, "wb") as out:
            out.write(self.content)
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

    def test_se_descarga_primero_en_temp(self, env, make_event, run_async):
        """Así el fichero no aparece en /list mientras se descarga."""
        run_async(env.dropbot.download_media(_event(make_event, file_message("a.pdf"))))

        (temp_path,) = env.telegram.calls
        assert os.path.dirname(temp_path) == str(env.temp)

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
        assert button_data(buttons[0]) == b"cancel:321"

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
        RuntimeError("Telegram internal error"),
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

    def test_agotados_los_reintentos_avisa_y_limpia(
        self, env, sent_messages, make_event, run_async
    ):
        env.dropbot.active_tasks[77] = object()
        env.telegram.failures = [ConnectionError("network down")] * env.dropbot.MAX_DOWNLOAD_RETRIES
        env.telegram.write_partial_before_failing = True

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"), 77)))

        assert len(env.telegram.calls) == env.dropbot.MAX_DOWNLOAD_RETRIES
        last_kind, last_text, last_kwargs = sent_messages[-1]
        assert last_kind == "edit" and "problemas internos" in last_text
        assert last_kwargs.get("buttons") is None, "ya no hay nada que cancelar"
        assert env.temp_files() == []
        assert env.files(env.paths["video"]) == []
        assert 77 not in env.dropbot.active_tasks

    def test_el_timeout_de_la_descarga_se_reintenta(
        self, env, texts, make_event, run_async, monkeypatch
    ):
        """asyncio.wait_for corta las descargas colgadas y se vuelve a probar."""
        calls = []
        real_wait_for = asyncio.wait_for

        async def impatient(coro, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                coro.close()
                raise asyncio.TimeoutError()
            return await real_wait_for(coro, timeout)

        monkeypatch.setattr(env.dropbot.asyncio, "wait_for", impatient)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert len(calls) == 2
        assert "Reintentando descarga" in texts()
        assert env.files(env.paths["video"]) == ["a.mkv"]

    def test_timeouts_en_todos_los_intentos(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        async def always_timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(env.dropbot.asyncio, "wait_for", always_timeout)

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert "problemas internos" in sent_messages[-1][1]
        assert env.temp_files() == []

    def test_el_timeout_crece_con_el_tamano(
        self, env, make_event, run_async, monkeypatch
    ):
        timeouts = []
        real_wait_for = asyncio.wait_for

        async def spy(coro, timeout):
            timeouts.append(timeout)
            return await real_wait_for(coro, timeout)

        monkeypatch.setattr(env.dropbot.asyncio, "wait_for", spy)
        big = 1000 * 1024 * 1024

        run_async(env.dropbot.download_media(_event(make_event, file_message("a.bin"))))
        run_async(env.dropbot.download_media(_event(make_event, file_message("b.bin", size=big))))

        assert timeouts[1] > timeouts[0] >= 600


class TestErroresNoReintentables:
    def _missing_destination(self, env, monkeypatch):
        """La carpeta de destino ha desaparecido (volumen desmontado)."""
        missing = os.path.join(env.general, "no-existe")
        paths = dict(env.paths, video=missing)
        monkeypatch.setattr(env.dropbot, "DOWNLOAD_PATHS", paths)

    def test_no_reintenta_un_error_que_no_es_transitorio(
        self, env, make_event, run_async, monkeypatch
    ):
        self._missing_destination(env, monkeypatch)

        with pytest.raises(FileNotFoundError):
            run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert len(env.telegram.calls) == 1

    def test_un_error_no_reintentable_no_deja_el_temporal(
        self, env, make_event, run_async, monkeypatch
    ):
        self._missing_destination(env, monkeypatch)

        with pytest.raises(FileNotFoundError):
            run_async(env.dropbot.download_media(_event(make_event, file_message("a.mkv"))))

        assert env.temp_files() == []

    def test_un_error_no_reintentable_se_le_dice_al_usuario(
        self, env, sent_messages, make_event, run_async, monkeypatch
    ):
        env.telegram.failures = [RuntimeError("FILE_REFERENCE_EXPIRED")]

        run_async(env.dropbot.limited_download(_event(make_event, file_message("a.mkv"))))

        last_kind, last_text, last_kwargs = sent_messages[-1]
        assert last_kind == "edit", "el mensaje 'Descargando...' tiene que cambiar"
        assert not last_kwargs.get("buttons")


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
            env.dropbot.active_tasks[event_id] = task
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
        assert 56 not in env.dropbot.active_tasks

    def test_cancelar_durante_la_copia_no_deja_nada(
        self, env, make_event, run_async, monkeypatch
    ):
        """Si se cancela con el nombre final ya reservado, también se borra."""
        event = _event(make_event, file_message("a.mkv"), 57)

        async def slow_to_thread(func, *args):
            await asyncio.Event().wait()

        monkeypatch.setattr(env.dropbot.asyncio, "to_thread", slow_to_thread)

        async def scenario():
            task = asyncio.create_task(env.dropbot.limited_download(event))
            env.dropbot.active_tasks[57] = task
            while not os.listdir(env.paths["video"]):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run_async(scenario())

        assert env.files(env.paths["video"]) == []
        assert env.temp_files() == []
        assert 57 not in env.dropbot.active_tasks


class TestLimitedDownload:
    def test_quita_la_tarea_al_terminar_bien(self, env, make_event, run_async):
        event = _event(make_event, file_message("a.pdf"), 60)

        async def scenario():
            task = asyncio.create_task(env.dropbot.limited_download(event))
            env.dropbot.active_tasks[60] = task
            await task

        run_async(scenario())
        assert 60 not in env.dropbot.active_tasks

    def test_un_error_inesperado_no_se_escapa(self, env, make_event, run_async, monkeypatch):
        async def explode(event):
            raise RuntimeError("algo raro")

        monkeypatch.setattr(env.dropbot, "download_media", explode)
        event = _event(make_event, file_message("a.pdf"), 62)

        async def scenario():
            task = asyncio.create_task(env.dropbot.limited_download(event))
            env.dropbot.active_tasks[62] = task
            await task

        run_async(scenario())
        assert 62 not in env.dropbot.active_tasks

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
            with open(file, "wb") as out:
                out.write(b"x")
            running[0] -= 1

        monkeypatch.setattr(env.dropbot.bot, "download_media", download)

        async def scenario():
            nonlocal release
            release = asyncio.Event()
            tasks = [asyncio.create_task(env.dropbot.limited_download(
                _event(make_event, file_message(f"f{i}.pdf"), 100 + i))) for i in range(5)]
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
            with open(file, "wb") as out:
                out.write(b"x")
            running[0] -= 1

        monkeypatch.setattr(env.dropbot.bot, "download_media", download)

        async def scenario():
            nonlocal release
            release = asyncio.Event()
            tasks = [asyncio.create_task(env.dropbot.limited_download(
                _event(make_event, file_message(f"g{i}.pdf"), 200 + i))) for i in range(4)]
            for _ in range(30):
                await asyncio.sleep(0)
            assert running[0] == 1

            settings_put(env.dropbot, "downloads.parallel", 3)
            await env.dropbot.apply_setting("downloads.parallel")
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

        async def first_fails(event):
            calls.append(event.id)
            if len(calls) == 1:
                return await explode(event)
            return await real(event)

        monkeypatch.setattr(env.dropbot, "download_media", first_fails)

        async def scenario():
            await env.dropbot.limited_download(_event(make_event, file_message("a.pdf"), 300))
            await asyncio.wait_for(
                env.dropbot.limited_download(_event(make_event, file_message("b.pdf"), 301)),
                timeout=5,
            )

        run_async(scenario())
        assert env.dropbot.download_semaphore.active == 0
        assert env.files(env.paths["ebook"]) == ["b.pdf"]
