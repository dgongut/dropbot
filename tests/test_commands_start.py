"""Comandos y arranque: /start y compañía, /list, menú de comandos, heartbeat
y el proveedor de PO Token.

El proveedor se prueba con procesos de verdad (`sh`) haciéndose pasar por
`deno`: así se comprueba cómo reacciona el bot cuando el proceso no existe,
muere al arrancar o no responde, sin depender de deno ni de la red.
"""

import asyncio
import os
import sys

import pytest

from conftest import ADMIN_ID

from button_data import button_data


class CommandEvent:
    """Un NewMessage con un comando de texto."""

    def __init__(self, text, sender_id=999, chat_id=42):
        self.raw_text = text
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.id = 1
        self.deleted = 0
        command = text.lstrip("/").split()[0].split("@")[0]
        self.pattern_match = _Match(command)

    async def delete(self):
        self.deleted += 1


class _Match:
    def __init__(self, command):
        self._command = command

    def group(self, index):
        return self._command


@pytest.fixture
def counted(dropbot, monkeypatch):
    keys = []
    monkeypatch.setattr(dropbot.stats, "count", keys.append)
    return keys


def _all_button_texts(buttons):
    return [button.text for row in buttons for button in row]


# --- handle_start ------------------------------------------------------------

class TestComandos:
    def test_start_da_la_bienvenida_y_borra_el_comando(
        self, quiet_bot, sent_messages, run_async, config_dir, counted
    ):
        event = CommandEvent("/start")

        run_async(quiet_bot.handle_start(event))

        assert event.deleted >= 1
        assert [kind for kind, _, _ in sent_messages] == ["send_message"]
        assert sent_messages[0][1] == quiet_bot.get_text("welcome_message")
        assert counted == ["cmd_start"]

    def test_donate(self, quiet_bot, sent_messages, run_async, config_dir, counted):
        run_async(quiet_bot.handle_start(CommandEvent("/donate")))
        assert sent_messages[0][1] == quiet_bot.get_text("donate")

    def test_version_dice_la_version(self, quiet_bot, texts, run_async, config_dir, counted):
        run_async(quiet_bot.handle_start(CommandEvent("/version")))
        assert quiet_bot.VERSION in texts()

    def test_donors_pide_la_lista_al_chat_correcto(
        self, quiet_bot, run_async, monkeypatch, config_dir, counted
    ):
        chats = []

        async def donors(chat_id):
            chats.append(chat_id)

        monkeypatch.setattr(quiet_bot, "print_donors", donors)

        run_async(quiet_bot.handle_start(CommandEvent("/donors", chat_id=77)))

        assert chats == [77]

    def test_list_muestra_el_menu_de_categorias(
        self, quiet_bot, sent_messages, run_async, config_dir, counted
    ):
        run_async(quiet_bot.handle_start(CommandEvent("/list")))

        (kind, text, kwargs), = sent_messages
        assert text == quiet_bot.get_text("list_select_category")
        data = [button_data(b) for row in kwargs["buttons"] for b in row]
        assert b"listcat:all" in data and b"close" in data

    def test_manage_muestra_el_menu_de_gestion(
        self, quiet_bot, sent_messages, run_async, config_dir, counted
    ):
        run_async(quiet_bot.handle_start(CommandEvent("/manage")))

        (kind, text, kwargs), = sent_messages
        assert text == quiet_bot.get_text("manage_select_category")
        data = [button_data(b) for row in kwargs["buttons"] for b in row]
        assert b"managecat:all" in data and data[-1] == b"close"
        assert all(len(row) <= 3 for row in kwargs["buttons"])

    def test_manage_reparte_las_categorias_en_filas_de_tres(
        self, quiet_bot, sent_messages, run_async, monkeypatch, config_dir, counted
    ):
        categories = [(f"c{i}", f"Cat {i}") for i in range(5)]
        monkeypatch.setattr(quiet_bot, "get_available_categories", lambda: categories)

        run_async(quiet_bot.handle_start(CommandEvent("/manage")))

        rows = sent_messages[0][2]["buttons"]
        assert [len(row) for row in rows] == [3, 2, 1]

    def test_quien_no_es_admin_recibe_el_aviso_y_nada_mas(
        self, quiet_bot, sent_messages, run_async, monkeypatch, config_dir, counted
    ):
        donors = []
        monkeypatch.setattr(quiet_bot, "print_donors", lambda chat: donors.append(chat))

        for command in ("/start", "/list", "/manage", "/donors", "/version"):
            run_async(quiet_bot.handle_start(CommandEvent(command, sender_id=1)))

        assert {text for _, text, _ in sent_messages} == {quiet_bot.get_text("user_not_admin")}
        assert not donors and not counted

    def test_si_no_se_puede_borrar_el_comando_responde_igual(
        self, quiet_bot, sent_messages, run_async, config_dir, counted
    ):
        event = CommandEvent("/start")

        async def cannot_delete():
            raise RuntimeError("MESSAGE_DELETE_FORBIDDEN")

        event.delete = cannot_delete

        run_async(quiet_bot.handle_start(event))

        assert len(sent_messages) == 1

    def test_responde_en_el_idioma_elegido(
        self, quiet_bot, texts, run_async, config_dir, counted
    ):
        quiet_bot.settings.put("language", "EN")
        run_async(quiet_bot.handle_start(CommandEvent("/version")))
        assert texts() == quiet_bot.get_text("version", quiet_bot.VERSION)
        assert "versión" not in texts().lower()

    def test_un_comando_con_argumentos_no_se_queda_sin_respuesta(
        self, quiet_bot, quiet_manage, sent_messages, run_async, config_dir, counted
    ):
        event = CommandEvent("/list video")

        run_async(quiet_bot.handle_start(event))

        assert sent_messages, "el mensaje se borró y no hubo respuesta"

    @pytest.mark.parametrize("command", ["/list", "/manage"])
    def test_el_boton_de_cerrar_respeta_el_idioma(
        self, quiet_bot, sent_messages, run_async, config_dir, counted, command
    ):
        quiet_bot.settings.put("language", "EN")

        run_async(quiet_bot.handle_start(CommandEvent(command)))

        assert quiet_bot.get_text("button_close") in _all_button_texts(sent_messages[0][2]["buttons"])


# --- handle_list_files -------------------------------------------------------
#
# `/list <categoría>` reutiliza el listado de handlers/manage.py, así que los
# envíos se capturan allí (quiet_manage) y las carpetas se cambian en los dos
# módulos.

def _use_paths(dropbot, monkeypatch, paths):
    from handlers import manage

    monkeypatch.setattr(dropbot, "DOWNLOAD_PATHS", paths)
    monkeypatch.setattr(manage, "DOWNLOAD_PATHS", paths)


@pytest.fixture
def folders(dropbot, quiet_manage, tmp_path, monkeypatch):
    paths = {kind: str(tmp_path / kind) for kind in
             ("audio", "video", "photo", "torrent", "ebook", "url_video", "url_audio")}
    for path in paths.values():
        os.makedirs(path)
    _use_paths(dropbot, monkeypatch, paths)
    return paths


def _touch(folder, name, size=10):
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(b"x" * size)


class ListEvent:
    def __init__(self, text):
        self.raw_text = text
        self.chat_id = 42
        self.id = 1
        self.sender_id = ADMIN_ID


class TestListarFicheros:
    def test_sin_ficheros_lo_dice(self, quiet_bot, folders, sent_messages, run_async, config_dir):
        run_async(quiet_bot.handle_list_files(ListEvent("/list")))
        assert sent_messages[0][1] == quiet_bot.get_text("list_empty")

    def test_lista_ordenado_y_sin_ocultos_ni_miniaturas(
        self, quiet_bot, folders, sent_messages, run_async, config_dir
    ):
        _touch(folders["video"], "zeta.mkv")
        _touch(folders["audio"], "Alfa.mp3")
        _touch(folders["ebook"], ".oculto")
        _touch(folders["video"], "zeta_thumb.jpg")

        run_async(quiet_bot.handle_list_files(ListEvent("/list")))

        (_, text, kwargs), = sent_messages
        assert text.index("Alfa.mp3") < text.index("zeta.mkv")
        assert ".oculto" not in text and "_thumb" not in text
        assert kwargs["buttons"], "el último mensaje lleva las categorías"

    def test_filtra_por_categoria(self, quiet_bot, folders, sent_messages, run_async, config_dir):
        _touch(folders["video"], "peli.mkv")
        _touch(folders["url_video"], "youtube.mp4")
        _touch(folders["audio"], "tema.mp3")

        run_async(quiet_bot.handle_list_files(ListEvent("/list video")))

        text = sent_messages[0][1]
        assert "peli.mkv" in text and "youtube.mp4" in text
        assert "tema.mp3" not in text

    def test_cuenta_carpetas_aparte(self, quiet_bot, folders, sent_messages, run_async, config_dir):
        os.makedirs(os.path.join(folders["video"], "Serie"))
        _touch(os.path.join(folders["video"], "Serie"), "cap1.mkv", 100)
        _touch(folders["video"], "peli.mkv", 50)

        run_async(quiet_bot.handle_list_files(ListEvent("/list video")))

        text = sent_messages[0][1]
        assert "📁" in text and "Serie" in text
        assert quiet_bot.get_text(
            "total_files_folders_space", 1, 1, quiet_bot.format_file_size(150)
        ) in text

    def test_una_carpeta_compartida_no_duplica(
        self, quiet_bot, quiet_manage, tmp_path, sent_messages, run_async, monkeypatch, config_dir
    ):
        """Sin carpetas propias todas las categorías apuntan a la misma."""
        shared = str(tmp_path / "downloads")
        os.makedirs(shared)
        _use_paths(quiet_bot, monkeypatch, {
            kind: shared for kind in
            ("audio", "video", "photo", "torrent", "ebook", "url_video", "url_audio")
        })
        _touch(shared, "unico.pdf")

        run_async(quiet_bot.handle_list_files(ListEvent("/list")))

        assert sent_messages[0][1].count("unico.pdf") == 1

    def test_muchos_ficheros_se_parten_en_varios_mensajes(
        self, quiet_bot, folders, sent_messages, run_async, config_dir
    ):
        for i in range(120):
            _touch(folders["ebook"], f"documento_con_nombre_largo_{i:03d}.pdf")

        run_async(quiet_bot.handle_list_files(ListEvent("/list")))

        assert len(sent_messages) > 1
        assert all(len(text) <= 4096 for _, text, _ in sent_messages)
        assert all(kwargs["buttons"] is None for _, _, kwargs in sent_messages[:-1])
        assert sent_messages[-1][2]["buttons"]
        listed = " ".join(text for _, text, _ in sent_messages)
        assert all(f"_{i:03d}.pdf" in listed for i in range(120))

    def test_los_nombres_largos_se_recortan(
        self, quiet_bot, folders, sent_messages, run_async, config_dir
    ):
        _touch(folders["ebook"], "a" * 60 + ".pdf")
        run_async(quiet_bot.handle_list_files(ListEvent("/list")))
        assert "a" * 37 + "..." in sent_messages[0][1]

    def test_un_error_se_le_dice_al_usuario(
        self, quiet_bot, quiet_manage, folders, sent_messages, run_async, monkeypatch, config_dir
    ):
        _touch(folders["ebook"], "a.pdf")

        def broken(*args, **kwargs):
            raise RuntimeError("roto")

        monkeypatch.setattr(quiet_manage, "get_category_buttons", broken)

        run_async(quiet_bot.handle_list_files(ListEvent("/list")))

        assert sent_messages[-1][1] == quiet_bot.get_text("error_list_files")


# --- send_startup_message ----------------------------------------------------

@pytest.fixture
def startup(quiet_bot, monkeypatch, config_dir):
    sent = []

    async def send_message(chat_id, text=None, **kwargs):
        sent.append((chat_id, text))

    monkeypatch.setattr(quiet_bot, "safe_send_message", send_message)
    monkeypatch.setattr(quiet_bot.store, "is_persistent", lambda: True)
    monkeypatch.setattr(quiet_bot.settings, "env_settings_present", lambda: [])
    return sent


class TestMensajeDeArranque:
    def test_avisa_a_cada_admin(self, quiet_bot, startup, run_async, monkeypatch):
        monkeypatch.setattr(quiet_bot, "TELEGRAM_ADMIN", "999,1000")

        run_async(quiet_bot.send_startup_message())

        assert [chat for chat, _ in startup] == [999, 1000]
        assert all(text == quiet_bot.get_text("initial_message", quiet_bot.VERSION)
                   for _, text in startup)

    def test_avisa_si_config_no_es_un_volumen(self, quiet_bot, startup, run_async, monkeypatch):
        monkeypatch.setattr(quiet_bot.store, "is_persistent", lambda: False)

        run_async(quiet_bot.send_startup_message())

        assert quiet_bot.get_text("startup_not_persistent") in startup[0][1]

    def test_avisa_de_las_variables_importadas(self, quiet_bot, startup, run_async, monkeypatch):
        monkeypatch.setattr(quiet_bot.settings, "env_settings_present", lambda: ["AUTO_SEND"])

        run_async(quiet_bot.send_startup_message(seeded=True))

        assert quiet_bot.get_text("startup_settings_imported") in startup[0][1]

    def test_sin_importar_nada_no_habla_de_importaciones(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        monkeypatch.setattr(quiet_bot.settings, "env_settings_present", lambda: ["AUTO_SEND"])

        run_async(quiet_bot.send_startup_message(seeded=False))

        assert quiet_bot.get_text("startup_settings_imported") not in startup[0][1]

    def test_el_aviso_de_volumen_tiene_prioridad(self, quiet_bot, startup, run_async, monkeypatch):
        """Si no hay volumen, lo importado tampoco se va a guardar: se dice eso."""
        monkeypatch.setattr(quiet_bot.store, "is_persistent", lambda: False)
        monkeypatch.setattr(quiet_bot.settings, "env_settings_present", lambda: ["AUTO_SEND"])

        run_async(quiet_bot.send_startup_message(seeded=True))

        text = startup[0][1]
        assert quiet_bot.get_text("startup_not_persistent") in text
        assert quiet_bot.get_text("startup_settings_imported") not in text

    def test_un_admin_que_falla_no_deja_sin_aviso_a_los_demas(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        monkeypatch.setattr(quiet_bot, "TELEGRAM_ADMIN", "999,1000")
        delivered = []

        async def flaky(chat_id, text=None, **kwargs):
            if chat_id == 999:
                raise RuntimeError("USER_IS_BLOCKED")
            delivered.append(chat_id)

        monkeypatch.setattr(quiet_bot, "safe_send_message", flaky)

        run_async(quiet_bot.send_startup_message())

        assert delivered == [1000]


# --- set_commands / apply_setting --------------------------------------------

class FakeClient:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)


class TestMenuDeComandos:
    def test_registra_los_comandos_para_todos_los_idiomas(
        self, dropbot, run_async, monkeypatch, config_dir
    ):
        client = FakeClient()
        monkeypatch.setattr(dropbot, "bot", client)

        run_async(dropbot.set_commands())

        assert [r.lang_code for r in client.requests] == ["", "es", "en"]
        names = [c.command for c in client.requests[0].commands]
        assert names == ["start", "list", "manage", "settings", "version", "donate", "donors"]

    def test_cambiar_el_idioma_traduce_el_menu_en_caliente(
        self, dropbot, run_async, monkeypatch, config_dir
    ):
        client = FakeClient()
        monkeypatch.setattr(dropbot, "bot", client)

        run_async(dropbot.apply_setting("language"))
        dropbot.settings.put("language", "EN")
        run_async(dropbot.apply_setting("language"))

        spanish = client.requests[0].commands[0].description
        english = client.requests[-1].commands[0].description
        assert spanish != english
        assert english == "Main menu"

    def test_otros_ajustes_no_tocan_telegram(self, dropbot, run_async, monkeypatch, config_dir):
        client = FakeClient()
        monkeypatch.setattr(dropbot, "bot", client)

        run_async(dropbot.apply_setting("urls.auto_send"))

        assert client.requests == []

    def test_bajar_las_descargas_simultaneas_no_corta_las_que_van(
        self, dropbot, run_async, monkeypatch, config_dir
    ):
        from utils.limiter import ResizableLimiter

        limiter = ResizableLimiter(3)
        monkeypatch.setattr(dropbot, "download_semaphore", limiter)
        dropbot.settings.put("downloads.parallel", 1)

        async def scenario():
            async with limiter:
                async with limiter:
                    await dropbot.apply_setting("downloads.parallel")
                    assert limiter.active == 2
            waiting = asyncio.create_task(limiter.__aenter__())
            await asyncio.sleep(0)
            assert waiting.done(), "con todo libre la siguiente entra"
            await limiter.__aexit__(None, None, None)

        run_async(scenario())
        assert limiter.limit == 1


# --- heartbeat_writer --------------------------------------------------------

class _Stop(Exception):
    pass


def _sleep_n_times(n, real_sleep=asyncio.sleep):
    calls = []

    async def fake_sleep(seconds, *args, **kwargs):
        calls.append(seconds)
        if len(calls) >= n:
            raise _Stop()
        await real_sleep(0)

    return fake_sleep, calls


class TestHeartbeat:
    def test_escribe_la_hora_cada_intervalo(
        self, dropbot, tmp_path, run_async, monkeypatch
    ):
        target = tmp_path / "heartbeat"
        monkeypatch.setattr(dropbot, "HEARTBEAT_FILE", str(target))
        fake_sleep, calls = _sleep_n_times(3)
        monkeypatch.setattr(dropbot.asyncio, "sleep", fake_sleep)

        with pytest.raises(_Stop):
            run_async(dropbot.heartbeat_writer())

        assert calls == [dropbot.HEARTBEAT_INTERVAL] * 3
        assert abs(int(target.read_text()) - int(dropbot.time.time())) < 5

    def test_si_no_puede_escribir_sigue_latiendo(
        self, dropbot, tmp_path, run_async, monkeypatch
    ):
        monkeypatch.setattr(dropbot, "HEARTBEAT_FILE", str(tmp_path / "no" / "existe"))
        fake_sleep, calls = _sleep_n_times(2)
        monkeypatch.setattr(dropbot.asyncio, "sleep", fake_sleep)

        with pytest.raises(_Stop):
            run_async(dropbot.heartbeat_writer())

        assert len(calls) == 2


# --- start_pot_provider ------------------------------------------------------

@pytest.fixture
def pot(dropbot, tmp_path, monkeypatch):
    """Proveedor instalado en tmp_path, 'deno' sustituido por `sh`."""
    server = tmp_path / "server"
    (server / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(dropbot, "POT_PROVIDER_DIR", str(server))
    monkeypatch.setattr(dropbot, "POT_PROVIDER_STARTUP_TIMEOUT", 3)

    real_exec = asyncio.create_subprocess_exec
    real_sleep = asyncio.sleep

    class Pot:
        script = "exit 0"
        launches = []
        pings = []
        responds = False

    async def fake_exec(program, *args, **kwargs):
        Pot.launches.append((program, args, kwargs))
        if Pot.script is None:
            raise FileNotFoundError(2, "No such file or directory", program)
        return await real_exec("/bin/sh", "-c", Pot.script,
                               stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"))

    def fake_get(url, timeout=None):
        Pot.pings.append(url)
        if not Pot.responds:
            raise ConnectionError("refused")
        return object()

    async def quick_sleep(seconds, *args, **kwargs):
        await real_sleep(min(seconds, 0.02))

    monkeypatch.setattr(dropbot.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(dropbot.requests, "get", fake_get)
    monkeypatch.setattr(dropbot.asyncio, "sleep", quick_sleep)
    Pot.server = server
    return Pot


@pytest.mark.skipif(sys.platform == "win32", reason="usa /bin/sh")
class TestProveedorPoToken:
    def test_sin_instalar_no_arranca_nada(self, dropbot, pot, run_async):
        os.rmdir(pot.server / "node_modules")

        assert run_async(dropbot.start_pot_provider()) is None
        assert pot.launches == []

    def test_sin_deno_devuelve_none(self, dropbot, pot, run_async):
        pot.script = None

        assert run_async(dropbot.start_pot_provider()) is None
        assert pot.launches[0][0] == "deno"

    def test_lanza_deno_en_su_carpeta_y_puerto(self, dropbot, pot, run_async):
        pot.script = "sleep 5"
        pot.responds = True

        async def scenario():
            proc = await dropbot.start_pot_provider()
            try:
                return proc, proc.returncode
            finally:
                proc.kill()
                await proc.wait()

        proc, returncode = run_async(scenario())

        assert proc is not None and returncode is None
        program, args, kwargs = pot.launches[0]
        assert kwargs["cwd"] == str(pot.server / "node_modules")
        assert str(dropbot.POT_PROVIDER_PORT) in args
        assert pot.pings == [f"http://127.0.0.1:{dropbot.POT_PROVIDER_PORT}/ping"]

    def test_si_deno_muere_al_arrancar_devuelve_none(self, dropbot, pot, run_async):
        pot.script = "exit 3"

        async def scenario():
            started = asyncio.get_running_loop().time()
            result = await dropbot.start_pot_provider()
            return result, asyncio.get_running_loop().time() - started

        result, elapsed = run_async(scenario())

        assert result is None
        assert elapsed < 2.5, "no tiene que agotar el tiempo si el proceso ya murió"

    def test_si_no_responde_a_tiempo_devuelve_el_proceso_vivo(
        self, dropbot, pot, run_async, monkeypatch
    ):
        """main() lo necesita para pararlo al salir: si no, quedaría huérfano."""
        monkeypatch.setattr(dropbot, "POT_PROVIDER_STARTUP_TIMEOUT", 0.3)
        pot.script = "sleep 5"

        async def scenario():
            proc = await dropbot.start_pot_provider()
            alive = proc is not None and proc.returncode is None
            if proc is not None:
                proc.kill()
                await proc.wait()
            return alive

        assert run_async(scenario()) is True
        assert len(pot.pings) >= 2, "tiene que seguir preguntando hasta el límite"
