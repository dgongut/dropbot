"""Comandos y arranque: /start y compañía, /list, menú de comandos, la
conexión con Telegram y su sesión, heartbeat y el proveedor de PO Token.

El proveedor se prueba con procesos de verdad (`sh`) haciéndose pasar por
`deno`: así se comprueba cómo reacciona el bot cuando el proceso no existe,
muere al arrancar o no responde, sin depender de deno ni de la red.
"""

import asyncio
import os
import signal
import sqlite3
import stat
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types
from telethon.client import TelegramClient
from telethon.crypto import AuthKey
from telethon.errors import (
    AccessTokenInvalidError, AuthKeyDuplicatedError, AuthKeyNotFound, FloodWaitError, InvalidBufferError,
)
from telethon.sessions import SQLiteSession

import basic
import migration
from conftest import ADMIN_ID, FORUM_CHAT_ID, FORUM_TOPIC, forum_reply_to
from utils import telegram_helpers

from button_data import button_data


class CommandEvent:
    """Un NewMessage con un comando de texto."""

    def __init__(self, text, sender_id=999, chat_id=42, is_private=True):
        self.raw_text = text
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.is_private = is_private
        self.id = 1
        self.deleted = 0
        # Lo que se contesta directamente, sin pasar por la cola
        self.responses = []
        command = text.lstrip("/").split()[0].split("@")[0]
        self.pattern_match = _Match(command)

    async def delete(self):
        self.deleted += 1

    async def respond(self, text, **kwargs):
        self.responses.append(text)


def _client(username="DropBot", failure=None):
    """El cliente de Telethon que lleva cada evento, solo con su get_me()."""
    async def get_me():
        if failure is not None:
            raise failure
        return SimpleNamespace(username=username)

    return SimpleNamespace(get_me=get_me)


class _Match:
    def __init__(self, command):
        self._command = command

    def group(self, index):
        return self._command


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    """Nadie avisado todavía: a quién se avisó y cuándo es estado del módulo."""
    monkeypatch.setattr(telegram_helpers, "_not_admin_warned", {})


@pytest.fixture
def bot_username(monkeypatch):
    """El @ del bot, como lo deja main() al arrancar."""
    monkeypatch.setattr(telegram_helpers, "_bot_username", "DropBot")


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

        async def donors(chat_id, reply_to=None):
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

    @pytest.mark.parametrize("command", ["/start", "/donate", "/version", "/list", "/manage"])
    def test_en_un_foro_contesta_en_el_tema_del_comando(
        self, quiet_bot, sent_messages, run_async, config_dir, counted, command
    ):
        """En un supergrupo con temas, lo que se manda sin reply_to cae en
        General: el admin escribía /list en un tema y el menú salía en otro."""
        event = CommandEvent(command, chat_id=FORUM_CHAT_ID, is_private=False)
        event.reply_to = forum_reply_to()

        run_async(quiet_bot.handle_start(event))

        assert [kwargs["reply_to"] for _, _, kwargs in sent_messages] == [FORUM_TOPIC]

    def test_en_privado_contesta_como_siempre(self, quiet_bot, sent_messages, run_async, config_dir, counted):
        run_async(quiet_bot.handle_start(CommandEvent("/start")))

        assert sent_messages[0][2]["reply_to"] is None

    def test_donors_en_un_foro_va_al_tema(self, quiet_bot, run_async, monkeypatch, config_dir, counted):
        topics = []

        async def donors(chat_id, reply_to=None):
            topics.append(reply_to)

        monkeypatch.setattr(quiet_bot, "print_donors", donors)
        event = CommandEvent("/donors", chat_id=FORUM_CHAT_ID, is_private=False)
        event.reply_to = forum_reply_to(reply_to_msg_id=90)

        run_async(quiet_bot.handle_start(event))

        assert topics == [FORUM_TOPIC]

    @pytest.mark.parametrize("command", ["/start", "/list", "/manage", "/donors", "/version"])
    def test_quien_no_es_admin_recibe_el_aviso_y_nada_mas(
        self, quiet_bot, sent_messages, run_async, monkeypatch, config_dir, counted, command
    ):
        donors = []
        monkeypatch.setattr(quiet_bot, "print_donors", lambda chat: donors.append(chat))
        event = CommandEvent(command, sender_id=1)

        run_async(quiet_bot.handle_start(event))

        assert event.responses == [quiet_bot.get_text("user_not_admin")]
        # Directo, no por la cola de los mensajes del admin; y su comando no se borra
        assert sent_messages == [] and not event.deleted
        assert not donors and not counted

    def test_a_quien_no_es_admin_se_le_avisa_una_vez_y_no_a_cada_comando(
        self, quiet_bot, sent_messages, run_async, config_dir, counted
    ):
        """Cada aviso iba a la cola de todos los mensajes del bot: repitiendo
        comandos se provocaba un FloodWait que dejaba al admin sin respuesta."""
        events = [CommandEvent("/start", sender_id=1) for _ in range(20)]

        for event in events:
            run_async(quiet_bot.handle_start(event))

        assert sum(len(event.responses) for event in events) == 1
        assert sent_messages == []

    def test_pasado_un_rato_se_le_vuelve_a_avisar(
        self, quiet_bot, run_async, monkeypatch, config_dir, counted
    ):
        clock = SimpleNamespace(now=1000.0)
        monkeypatch.setattr(telegram_helpers, "time", SimpleNamespace(monotonic=lambda: clock.now))
        first, soon, later = (CommandEvent("/start", sender_id=1) for _ in range(3))

        run_async(quiet_bot.handle_start(first))
        clock.now += telegram_helpers.NOT_ADMIN_WARNING_INTERVAL - 1
        run_async(quiet_bot.handle_start(soon))
        clock.now += 1
        run_async(quiet_bot.handle_start(later))

        assert [len(e.responses) for e in (first, soon, later)] == [1, 0, 1]

    def test_en_un_grupo_a_quien_no_es_admin_ni_se_le_contesta_ni_se_le_borra(
        self, quiet_bot, sent_messages, run_async, config_dir, counted
    ):
        event = CommandEvent("/start", sender_id=1, chat_id=-1001234567890, is_private=False)

        run_async(quiet_bot.handle_start(event))

        assert event.responses == [] and sent_messages == []
        assert not event.deleted and not counted

    def test_se_recuerda_a_un_numero_limitado_de_personas(
        self, quiet_bot, run_async, monkeypatch, config_dir, counted
    ):
        """Con la lista llena no se avisa a nadie nuevo hasta que venzan los avisos."""
        clock = SimpleNamespace(now=1000.0)
        monkeypatch.setattr(telegram_helpers, "time", SimpleNamespace(monotonic=lambda: clock.now))
        monkeypatch.setattr(telegram_helpers, "NOT_ADMIN_WARNING_MAX_USERS", 2)
        events = [CommandEvent("/start", sender_id=sender) for sender in (1, 2, 3)]

        for event in events:
            run_async(quiet_bot.handle_start(event))
        clock.now += telegram_helpers.NOT_ADMIN_WARNING_INTERVAL
        late = CommandEvent("/start", sender_id=4)
        run_async(quiet_bot.handle_start(late))

        assert [len(e.responses) for e in events + [late]] == [1, 1, 0, 1]
        assert set(telegram_helpers._not_admin_warned) == {4}

    def test_un_comando_para_otro_bot_ni_se_borra_ni_se_contesta(
        self, quiet_bot, quiet_manage, sent_messages, run_async, config_dir, counted, bot_username
    ):
        """En un grupo con otro bot, el admin que escribía "/list@OtroBot video"
        publicaba en el grupo sus ficheros."""
        events = [CommandEvent("/list@OtroBot video", chat_id=-100123, is_private=False),
                  CommandEvent("/start@OtroBot", sender_id=1, is_private=False)]

        for event in events:
            run_async(quiet_bot.handle_start(event))

        assert sent_messages == [] and not counted
        assert all(not e.deleted and not e.responses for e in events)

    def test_un_comando_con_el_arroba_del_bot_es_para_el(
        self, quiet_bot, sent_messages, run_async, config_dir, counted, bot_username
    ):
        event = CommandEvent("/start@dropbot", chat_id=-100123, is_private=False)

        run_async(quiet_bot.handle_start(event))

        assert event.deleted
        assert [text for _, text, _ in sent_messages] == [quiet_bot.get_text("welcome_message")]

    def test_un_comando_con_el_arroba_del_bot_que_llega_al_conectar_es_para_el(
        self, quiet_bot, quiet_manage, sent_messages, run_async, config_dir, counted, monkeypatch
    ):
        """Lo que llegó con el bot parado (catch_up) se atiende nada más
        conectar, antes de que main() sepa el @ del bot: "/list@DropBot video",
        que es lo que manda el menú de comandos en un grupo, se tomaba por de
        otro bot y se ignoraba sin decir nada."""
        monkeypatch.setattr(telegram_helpers, "_bot_username", None)
        event = CommandEvent("/list@DropBot video", chat_id=-100123, is_private=False)
        event.client = _client("DropBot")

        run_async(quiet_bot.handle_start(event))

        assert event.deleted
        assert sent_messages, "el comando era para este bot"
        assert telegram_helpers._bot_username == "DropBot"

    @pytest.mark.parametrize("client", [
        _client("DropBot"), _client(failure=ConnectionError("Connection to Telegram failed")),
    ], ids=["con_su_arroba", "sin_poder_preguntarlo"])
    def test_un_comando_para_otro_bot_que_llega_al_conectar_no_publica_nada(
        self, quiet_bot, quiet_manage, sent_messages, run_async, config_dir, counted, monkeypatch, client
    ):
        """Sin saber aún el @ del bot, el de otro tampoco se atiende: si no se
        puede preguntar, se trata como de otro, que es lo que no publica nada."""
        monkeypatch.setattr(telegram_helpers, "_bot_username", None)
        event = CommandEvent("/list@OtroBot video", chat_id=-100123, is_private=False)
        event.client = client

        run_async(quiet_bot.handle_start(event))

        assert sent_messages == [] and not counted
        assert not event.deleted and not event.responses

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

    def test_en_un_foro_el_listado_va_al_tema_del_comando(
        self, quiet_bot, folders, sent_messages, run_async, config_dir
    ):
        _touch(folders["video"], "peli.mkv")
        event = ListEvent("/list video")
        event.reply_to = forum_reply_to()

        run_async(quiet_bot.handle_list_files(event))

        assert sent_messages and all(kwargs["reply_to"] == FORUM_TOPIC for _, _, kwargs in sent_messages)

    def test_lista_ordenado_y_sin_ocultos(
        self, quiet_bot, folders, sent_messages, run_async, config_dir
    ):
        _touch(folders["video"], "zeta.mkv")
        _touch(folders["audio"], "Alfa.mp3")
        _touch(folders["ebook"], ".oculto")
        # Es del usuario: las miniaturas del bot van a TEMP_DIR, no aquí
        _touch(folders["video"], "zeta_thumb.jpg")

        run_async(quiet_bot.handle_list_files(ListEvent("/list")))

        (_, text, kwargs), = sent_messages
        assert text.index("Alfa.mp3") < text.index("zeta.mkv")
        assert ".oculto" not in text and "zeta_thumb.jpg" in text
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

    async def send_message(chat_id, text=None, wait_for_result=False, **kwargs):
        sent.append((chat_id, text))
        # Esperando el resultado, la cola devuelve el mensaje enviado
        return SimpleNamespace(id=len(sent)) if wait_for_result else None

    async def get_input_entity(peer):
        return peer

    monkeypatch.setattr(quiet_bot, "safe_send_message", send_message)
    monkeypatch.setattr(quiet_bot.bot, "get_input_entity", get_input_entity)
    monkeypatch.setattr(quiet_bot.store, "is_persistent", lambda: True)
    monkeypatch.setattr(quiet_bot, "downloads_persistent", lambda: True)
    monkeypatch.setattr(quiet_bot.settings, "env_settings_present", lambda: [])
    return sent


def _import_from_compose(dropbot, monkeypatch):
    """El primer arranque de la 4.x con AUTO_SEND en el compose: siembra settings.json."""
    monkeypatch.setattr(dropbot.settings, "env_settings_present", lambda: ["AUTO_SEND"])
    assert migration.run() is True


class TestMensajeDeArranque:
    def test_avisa_a_cada_admin(self, quiet_bot, startup, run_async, monkeypatch):
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "999, 1000,")

        run_async(quiet_bot.send_startup_message())

        assert [chat for chat, _ in startup] == [999, 1000]
        assert all(text == quiet_bot.get_text("initial_message", quiet_bot.VERSION)
                   for _, text in startup)

    def test_avisa_si_config_no_es_un_volumen(self, quiet_bot, startup, run_async, monkeypatch):
        monkeypatch.setattr(quiet_bot.store, "is_persistent", lambda: False)

        run_async(quiet_bot.send_startup_message())

        assert quiet_bot.get_text("startup_not_persistent") in startup[0][1]

    def test_avisa_de_las_variables_importadas(self, quiet_bot, startup, run_async, monkeypatch):
        _import_from_compose(quiet_bot, monkeypatch)

        run_async(quiet_bot.send_startup_message())

        assert quiet_bot.get_text("startup_settings_imported") in startup[0][1]

    def test_si_el_arranque_que_importa_se_cae_avisa_el_siguiente(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        """El aviso dependía de una variable del proceso que sembró: si se caía
        antes del mensaje de arranque (sin red, por ejemplo), el siguiente ya
        no sembraba y el aviso no llegaba nunca."""
        _import_from_compose(quiet_bot, monkeypatch)
        # El reinicio: lo que había en memoria se pierde y ya no se siembra
        quiet_bot.store.reload()
        assert migration.run() is False

        run_async(quiet_bot.send_startup_message())
        run_async(quiet_bot.send_startup_message())

        imported = quiet_bot.get_text("startup_settings_imported")
        assert imported in startup[0][1]
        assert imported not in startup[1][1], "una vez recibido no se repite"

    def test_si_el_aviso_no_se_puede_enviar_se_repite_en_el_siguiente_arranque(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        """Solo se encolaba: si el envío fallaba después, se perdía igual."""
        _import_from_compose(quiet_bot, monkeypatch)
        delivered = quiet_bot.safe_send_message

        async def failing(chat_id, text=None, **kwargs):
            raise ConnectionError("Cannot send requests while disconnected")

        monkeypatch.setattr(quiet_bot, "safe_send_message", failing)
        run_async(quiet_bot.send_startup_message())
        monkeypatch.setattr(quiet_bot, "safe_send_message", delivered)
        run_async(quiet_bot.send_startup_message())

        assert quiet_bot.get_text("startup_settings_imported") in startup[0][1]

    def test_sin_importar_nada_no_habla_de_importaciones(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        """Una instalación ya migrada con las variables todavía en el compose."""
        quiet_bot.store.set("settings_version", 1)
        monkeypatch.setattr(quiet_bot.settings, "env_settings_present", lambda: ["AUTO_SEND"])
        assert migration.run() is False

        run_async(quiet_bot.send_startup_message())

        assert quiet_bot.get_text("startup_settings_imported") not in startup[0][1]

    def test_el_aviso_de_volumen_tiene_prioridad(self, quiet_bot, startup, run_async, monkeypatch):
        """Si no hay volumen, lo importado tampoco se va a guardar: se dice eso."""
        monkeypatch.setattr(quiet_bot.store, "is_persistent", lambda: False)
        _import_from_compose(quiet_bot, monkeypatch)

        run_async(quiet_bot.send_startup_message())

        text = startup[0][1]
        assert quiet_bot.get_text("startup_not_persistent") in text
        assert quiet_bot.get_text("startup_settings_imported") not in text
        assert migration.import_notice_pending()

    def test_si_no_se_puede_escribir_en_config_lo_dice_y_el_aviso_espera(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        """Con /config montado como solo lectura, un NFS con root_squash o el
        disco lleno no se avisaba de nada: los cambios de /settings parecían
        guardados y volvían atrás en el siguiente reinicio."""
        monkeypatch.setattr(quiet_bot.store, "write_error", lambda: "[Errno 30] Read-only file system")
        _import_from_compose(quiet_bot, monkeypatch)

        run_async(quiet_bot.send_startup_message())

        text = startup[0][1]
        assert quiet_bot.get_text("startup_not_writable") in text
        assert quiet_bot.get_text("startup_settings_imported") not in text
        assert migration.import_notice_pending()

    def test_avisa_si_settings_json_no_se_puede_leer(self, quiet_bot, startup, run_async, config_dir):
        """Con una errata editándolo a mano se usan los valores por defecto, y
        solo lo decía el log."""
        (config_dir / "settings.json").write_text('{"language": "EN",\n}', encoding="utf-8")
        quiet_bot.store.reload()

        run_async(quiet_bot.send_startup_message())

        assert quiet_bot.get_text("settings_unreadable", quiet_bot.store.settings_path()) in startup[0][1]

    def test_avisa_si_downloads_no_es_un_volumen_aunque_haya_otros_avisos(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        """Con /config montado y /downloads olvidado en el compose no había
        ningún aviso: lo descargado vivía dentro del contenedor y se perdía
        al recrearlo en la siguiente actualización."""
        monkeypatch.setattr(quiet_bot, "downloads_persistent", lambda: False)
        _import_from_compose(quiet_bot, monkeypatch)

        run_async(quiet_bot.send_startup_message())

        text = startup[0][1]
        assert quiet_bot.get_text("startup_downloads_not_persistent") in text
        assert quiet_bot.get_text("startup_settings_imported") in text

    def test_avisa_de_los_admins_ignorados_y_no_les_escribe(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        """Con el id de un grupo en TELEGRAM_ADMIN el bot ya no se para: lo
        ignora, y el admin bueno se entera aquí y no solo en docker logs."""
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "999,-1001234567890")

        run_async(quiet_bot.send_startup_message())

        assert [chat for chat, _ in startup] == [999]
        assert quiet_bot.get_text("startup_admin_ignored", "`-1001234567890`") in startup[0][1]

    def test_un_admin_que_nunca_abrio_el_chat_no_pasa_por_la_cola(
        self, quiet_bot, startup, run_async, monkeypatch
    ):
        """Telethon no encuentra a quien nunca le ha escrito al bot, y en la
        cola ese envío se reintentaba y la paraba unos 10 s. Los demás admins
        reciben su aviso igual."""
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "999,1000")
        warnings = []
        monkeypatch.setattr(quiet_bot, "warning", warnings.append)

        async def get_input_entity(peer):
            if peer == 999:
                raise ValueError("Could not find the input entity for PeerUser(user_id=999) (PeerUser)")
            return peer

        monkeypatch.setattr(quiet_bot.bot, "get_input_entity", get_input_entity)

        run_async(quiet_bot.send_startup_message())

        assert [chat for chat, _ in startup] == [1000]
        assert len(warnings) == 1 and "999" in warnings[0]


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


# --- main --------------------------------------------------------------------

class FakeBot:
    def __init__(self):
        self.calls = []
        self.token = None
        # Lo que lanza cada vuelta de run_until_disconnected, por orden. Sin
        # nada que lanzar termina, como tras un disconnect()
        self.disconnections = []
        # El fichero de la sesión, para los tests en que Telegram la rechaza
        self.session = SimpleNamespace(filename=None)

    async def start(self, bot_token=None):
        self.calls.append("start")
        self.token = bot_token

    async def connect(self):
        self.calls.append("connect")

    def is_connected(self):
        return True

    async def get_me(self):
        return SimpleNamespace(username="DropBot")

    async def run_until_disconnected(self):
        self.calls.append("run")
        if self.disconnections:
            raise self.disconnections.pop(0)

    async def disconnect(self):
        self.calls.append("disconnect")


def _docker_stop():
    """Lo que hace `docker stop`: un SIGTERM al proceso.

    Sin un handler puesto mataría a pytest entero, así que antes se comprueba
    que lo hay y, si no, el test falla como cualquier otro.
    """
    assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL, "nadie atiende SIGTERM"
    os.kill(os.getpid(), signal.SIGTERM)


async def _forever(*args, **kwargs):
    """Sin red, Telethon reintenta conectar sin fin."""
    await asyncio.Event().wait()


@pytest.fixture
def started(dropbot, monkeypatch):
    """main() con todo lo que habla con fuera sustituido; devuelve lo que hizo."""
    bot = FakeBot()

    async def nothing(*args, **kwargs):
        return None

    async def startup_message():
        bot.calls.append("startup_message")

    monkeypatch.setattr(dropbot, "bot", bot)
    monkeypatch.setattr(dropbot, "set_commands", nothing)
    monkeypatch.setattr(dropbot, "RECONNECT_DELAY", 0)
    monkeypatch.setattr(dropbot, "start_pot_provider", nothing)
    monkeypatch.setattr(dropbot, "heartbeat_writer", nothing)
    monkeypatch.setattr(dropbot, "send_startup_message", startup_message)
    monkeypatch.setattr(dropbot.message_queue, "start", nothing)
    monkeypatch.setattr(dropbot.message_queue, "shutdown", nothing)
    monkeypatch.setattr(dropbot.stats, "start", lambda version: bot.calls.append("stats"))
    monkeypatch.setattr(telegram_helpers, "_bot_username", None)
    return bot.calls


class RejectedSession:
    """La sesión de un bot que Telegram ya no acepta, y lo que el bot deja en el log."""

    NAME = "dropbot-123456-abc.session"

    def __init__(self, state, errors):
        self.state = state
        self.errors = errors

    def files(self):
        return sorted(os.listdir(self.state))

    def assert_moved_aside(self):
        """Apartada con su -journal, para que el siguiente arranque cree otra, y dicho en el log."""
        assert self.files() == [f"{self.NAME}-journal.bad", f"{self.NAME}.bad"]
        assert (self.state / f"{self.NAME}.bad").read_bytes() == b"auth key"
        assert any("[CONFIG]" in line and f"{self.NAME}.bad" in line for line in self.errors)

    def real(self):
        """Una sesión de Telethon de verdad, como la deja un `docker stop`: con
        su auth key y hasta dónde se había leído. Devuelve su ruta sin .session."""
        for name in self.files():
            os.remove(self.state / name)
        path = str(self.state / "dropbot-123456-real")
        session = SQLiteSession(path)
        session.set_dc(2, "149.154.167.51", 443)
        session.auth_key = AuthKey(b"k" * 256)
        session.set_update_state(0, types.updates.State(
            pts=100, qts=0, date=datetime.now(timezone.utc), seq=1, unread_count=0))
        session.save()
        session.close()
        return path


@pytest.fixture
def rejected_session(dropbot, started, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / RejectedSession.NAME).write_bytes(b"auth key")
    (state / f"{RejectedSession.NAME}-journal").write_bytes(b"journal")
    dropbot.bot.session.filename = str(state / RejectedSession.NAME)
    errors_logged = []
    monkeypatch.setattr(dropbot, "error", errors_logged.append)
    return RejectedSession(state, errors_logged)


class RevokedTelegram:
    """El MTProtoSender de Telethon frente a un Telegram que ya no reconoce la
    auth key de la sesión: conecta, pero contesta 401 a todo menos a entrar con
    el token. Las respuestas llegan un poco después, como por la red."""

    def __init__(self, auth_key):
        self.auth_key = auth_key
        self._connected = False
        self._pending = []
        self._disconnected = None

    async def connect(self, connection):
        self._connected = True
        self._disconnected = asyncio.get_running_loop().create_future()
        return True

    def is_connected(self):
        return self._connected

    @property
    def disconnected(self):
        return asyncio.shield(self._disconnected)

    def send(self, request, ordered=False):
        if not self._connected:
            raise ConnectionError("Cannot send requests while disconnected")
        answer = asyncio.get_running_loop().create_future()
        self._pending.append(answer)
        asyncio.get_running_loop().call_later(0.01, self._answer, answer, request)
        return answer

    def _answer(self, answer, request):
        if answer.done():
            return
        self._pending.remove(answer)
        if isinstance(request, functions.InvokeWithLayerRequest):
            answer.set_result(None)
        elif isinstance(request, functions.auth.ImportBotAuthorizationRequest):
            bot = types.User(id=123456, access_hash=1, bot=True, username="DropBot")
            answer.set_result(types.auth.Authorization(user=bot))
        else:
            answer.set_exception(errors.rpc_message_to_error(types.RpcError(401, "AUTH_KEY_UNREGISTERED"), request))

    async def disconnect(self):
        self._connected = False
        for answer in self._pending:
            answer.cancel()
        self._pending.clear()
        if self._disconnected is not None and not self._disconnected.done():
            self._disconnected.set_result(None)


class TestMain:
    def test_al_arrancar_guarda_el_arroba_del_bot(self, dropbot, started, run_async, monkeypatch):
        async def set_commands():
            pass

        monkeypatch.setattr(dropbot, "set_commands", set_commands)

        run_async(dropbot.main())

        assert telegram_helpers._bot_username == "DropBot"
        assert started == ["start", "startup_message", "stats", "run"]

    def test_si_telegram_no_acepta_el_menu_de_comandos_arranca_igual(
        self, dropbot, started, run_async, monkeypatch
    ):
        """El menú es cosmético: un FloodWait largo al ponerlo tumbaba el bot,
        y Docker lo reiniciaba una y otra vez hasta que pasaba la espera."""
        async def set_commands():
            raise FloodWaitError(request=None, capture=120)

        monkeypatch.setattr(dropbot, "set_commands", set_commands)

        run_async(dropbot.main())

        assert started == ["start", "startup_message", "stats", "run"]

    def test_si_no_puede_saber_su_arroba_al_arrancar_arranca_igual(
        self, dropbot, started, run_async, monkeypatch
    ):
        """El get_me() de main() iba fuera de todo try: un FloodWait o un error
        del servidor ahí tumbaba el arranque. El @ lo pide luego
        for_another_bot cuando le hace falta."""
        async def get_me():
            raise FloodWaitError(request=None, capture=120)

        monkeypatch.setattr(dropbot.bot, "get_me", get_me)

        run_async(dropbot.main())

        assert started == ["start", "startup_message", "stats", "run"]
        assert telegram_helpers._bot_username is None

    def test_conecta_en_main_con_el_token_y_no_al_importar(self, dropbot, started, run_async):
        """Al conectar llega lo pendiente (catch_up), y al importar aún no
        estaban registrados los handlers: se volvía a perder."""
        dropbot.TelegramClient.return_value.start.assert_not_called()

        run_async(dropbot.main())

        assert dropbot.bot.token == dropbot.TELEGRAM_TOKEN

    def test_si_telegram_corta_y_telethon_no_reintenta_vuelve_a_conectar(
        self, dropbot, started, run_async
    ):
        """Tras un corte de unos minutos, Telethon se rendía y el bot salía
        con código 1, matando las descargas en curso. Un flood a nivel de
        transporte (429) tampoco lo reintenta Telethon. Volver a conectar
        salva lo que va en su propia tarea; lo que corre en un handler, ver
        test_un_envio_en_marcha_sigue_tras_un_corte_que_vuelve_a_conectar."""
        dropbot.bot.disconnections = [
            ConnectionError("Connection to Telegram failed 5 time(s)"),
            InvalidBufferError((-429).to_bytes(4, "little", signed=True)),
        ]

        run_async(dropbot.main())

        assert started == ["start", "startup_message", "stats",
                           "run", "connect", "run", "connect", "run"]

    def test_un_envio_en_marcha_sigue_tras_un_corte_que_vuelve_a_conectar(
        self, dropbot, quiet_bot, started, make_event, media_file, run_async, monkeypatch
    ):
        """Al salir, run_until_disconnected desconecta, y disconnect() cancela
        los handlers que siguen en marcha: main() volvía a conectar, pero la
        conversión del Enviar que corría en uno ya había muerto, con su mensaje
        parado en «Convirtiendo…»."""
        uploaded = []
        path = media_file("serie.mkv")
        file_id = dropbot.register_pending_send(path)
        state = SimpleNamespace(handler=None, converting=None, finish=None)

        async def slow_conversion(input_path, status_message=None):
            state.converting.set()
            await state.finish.wait()
            return input_path

        async def nothing(file_path):
            return None

        async def no_metadata(file_path):
            return None, None, None

        async def upload(entity, file_path, *args, **kwargs):
            uploaded.append(file_path)
            return SimpleNamespace(id=1)

        monkeypatch.setattr(dropbot, "convert_video_to_telegram_compatible", slow_conversion)
        monkeypatch.setattr(dropbot, "get_video_metadata", no_metadata)
        monkeypatch.setattr(dropbot, "generate_video_thumbnail", nothing)
        monkeypatch.setattr(dropbot, "_send_file_fast", upload)
        monkeypatch.setattr(dropbot.stats, "count", lambda key: None)

        async def run_until_disconnected():
            started.append("run")
            if state.handler is None:
                state.converting, state.finish = asyncio.Event(), asyncio.Event()
                click = make_event(groups=(b"send", file_id.encode()))
                state.handler = asyncio.create_task(dropbot.handle_send_choice(click))
                await state.converting.wait()
                try:
                    raise InvalidBufferError((-429).to_bytes(4, "little", signed=True))
                finally:
                    # Como Telethon: disconnect() cancela los handlers en marcha
                    state.handler.cancel()
                    await asyncio.gather(state.handler, return_exceptions=True)
            # Ya ha vuelto a conectar: la conversión termina
            state.finish.set()
            for _ in range(100):
                if uploaded:
                    break
                await asyncio.sleep(0.01)

        dropbot.bot.run_until_disconnected = run_until_disconnected

        run_async(asyncio.wait_for(dropbot.main(), timeout=5))

        assert started[-3:] == ["run", "connect", "run"]
        assert uploaded == [path]

    @pytest.mark.parametrize("rejection", [
        AuthKeyNotFound(),
        # Dos contenedores con el mismo /config
        AuthKeyDuplicatedError(request=None),
    ])
    def test_si_telegram_no_conoce_la_sesion_la_aparta_y_sale(
        self, dropbot, started, run_async, rejected_session, rejection
    ):
        """Docker lo reiniciaba en bucle con la misma sesión y, desde que vive
        en /config, ni recrear el contenedor lo arreglaba."""
        dropbot.bot.disconnections = [rejection]

        with pytest.raises(SystemExit) as exited:
            run_async(dropbot.main())

        assert exited.value.code == 1
        assert "connect" not in started, "volver a conectar con ella no sirve"
        rejected_session.assert_moved_aside()

    def test_si_telegram_no_conoce_la_sesion_al_arrancar_la_aparta_y_sale(
        self, dropbot, started, run_async, rejected_session
    ):
        async def start(bot_token=None):
            raise AuthKeyNotFound()

        dropbot.bot.start = start

        with pytest.raises(SystemExit):
            run_async(dropbot.main())

        rejected_session.assert_moved_aside()
        assert "startup_message" not in started

    def test_un_token_que_telegram_no_acepta_se_dice_claro(
        self, dropbot, started, run_async, rejected_session
    ):
        """Sin esto salía un traceback de AccessTokenInvalidError."""
        async def start(bot_token=None):
            raise AccessTokenInvalidError(request=None)

        dropbot.bot.start = start

        with pytest.raises(SystemExit):
            run_async(dropbot.main())

        assert any("[CONFIG]" in line and "TELEGRAM_TOKEN" in line for line in rejected_session.errors)
        assert rejected_session.files() == ["dropbot-123456-abc.session", "dropbot-123456-abc.session-journal"], \
            "la sesión no tiene la culpa"

    def test_si_telegram_revoca_la_sesion_al_ponerse_al_dia_la_aparta_y_sale(
        self, dropbot, started, run_async, rejected_session, monkeypatch
    ):
        """El cliente de Telethon de verdad, con lo que había leído guardado en
        la sesión. Al conectar pide lo que llegó con el bot parado a la vez que
        entra con el token. Si Telegram ya no reconoce la auth key, Telethon se
        desconecta él solo: lo que salía de start() era un CancelledError o un
        ConnectionError, y el bot no arrancaba nunca."""
        path = rejected_session.real()

        async def scenario():
            client = TelegramClient(path, 1, "hash", catch_up=True)
            client._sender = RevokedTelegram(client.session.auth_key)
            monkeypatch.setattr(dropbot, "bot", client)
            await dropbot.main()

        with pytest.raises(SystemExit):
            run_async(scenario())

        with closing(sqlite3.connect(f"{path}.session.bad")) as bad:
            assert bad.execute("select auth_key from sessions").fetchone() == (b"k" * 256,)
        assert any("[CONFIG]" in line and f"{path}.session.bad" in line for line in rejected_session.errors)
        assert "startup_message" not in started

    def test_sin_red_docker_stop_para_el_bot_mientras_intenta_conectar(
        self, dropbot, started, run_async
    ):
        """Conectar se reintenta sin fin y disconnect() no lo corta: sin el
        SIGTERM atendido desde antes de conectar, docker stop acababa en SIGKILL."""
        async def offline_start(bot_token=None):
            started.append("start")
            _docker_stop()
            await _forever()

        dropbot.bot.start = offline_start

        run_async(asyncio.wait_for(dropbot.main(), timeout=5))

        assert started == ["start"]

    def test_docker_stop_para_el_bot_mientras_vuelve_a_conectar(self, dropbot, started, run_async):
        dropbot.bot.disconnections = [ConnectionError("Connection to Telegram failed 5 time(s)")]

        async def offline_connect():
            started.append("connect")
            _docker_stop()
            await _forever()

        dropbot.bot.connect = offline_connect

        run_async(asyncio.wait_for(dropbot.main(), timeout=5))

        assert started[-2:] == ["run", "connect"]

    def test_docker_stop_con_el_bot_en_marcha_lo_desconecta(self, dropbot, started, run_async):
        bot = dropbot.bot
        disconnected = asyncio.Event()

        async def online():
            started.append("run")
            _docker_stop()
            await disconnected.wait()

        async def disconnect():
            started.append("disconnect")
            disconnected.set()

        bot.run_until_disconnected = online
        bot.disconnect = disconnect

        run_async(asyncio.wait_for(dropbot.main(), timeout=5))

        assert started[-2:] == ["run", "disconnect"]


# --- Cliente de Telegram y su sesión -----------------------------------------

@pytest.fixture
def session_dir(dropbot, config_dir, tmp_path, monkeypatch):
    """/config/state vacío, y dónde dejaba la sesión la 4.0.0 (/app)."""
    legacy = tmp_path / "app" / "dropbot.session"
    monkeypatch.setattr(dropbot, "LEGACY_SESSION_FILE", str(legacy))
    return config_dir / "state", legacy


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class TestSesionDeTelegram:
    def test_reintenta_conectar_sin_limite_y_se_pone_al_dia(self, dropbot):
        """Con los 5 intentos de Telethon, un corte de red de unos minutos
        tumbaba el bot. Y sin catch_up cada arranque empezaba desde ese
        momento: lo que el admin mandó durante un reinicio o una actualización
        no se procesaba nunca, y nada lo decía."""
        _, options = dropbot.TelegramClient.call_args

        assert options["connection_retries"] < 0
        assert options["catch_up"] is True

    def test_se_guarda_en_config_con_el_id_del_bot_y_solo_para_el_dueno(
        self, dropbot, session_dir
    ):
        """En /app se perdía al recrear el contenedor: cada actualización era
        un login nuevo, y lo que llegó mientras tanto no se recuperaba."""
        state, _ = session_dir

        path = dropbot.session_path()

        assert os.path.dirname(path) == str(state)
        assert os.path.basename(path).startswith("dropbot-123456-")
        assert "test-token" not in path
        assert _mode(f"{path}.session") == 0o600

    def test_con_otro_token_empieza_una_sesion_limpia_y_borra_la_del_anterior(
        self, dropbot, session_dir, monkeypatch
    ):
        """Al revocar el token en @BotFather, la auth key de la sesión deja de
        valer. Con ella, al ponerse al día, Telethon se desconectaba solo
        mientras entraba con el token nuevo, y el bot no arrancaba nunca."""
        state, _ = session_dir
        old = dropbot.session_path()
        for leftover in (f"{old}.session-journal", f"{old}.session.bad"):
            Path(leftover).write_bytes(b"x")
        another_bot = state / "dropbot-654321-abcdef01.session"
        another_bot.write_bytes(b"otro bot")
        monkeypatch.setattr(dropbot, "TELEGRAM_TOKEN", "123456:token-nuevo")

        new = dropbot.session_path()

        assert new != old and os.path.basename(new).startswith("dropbot-123456-")
        assert sorted(os.listdir(state)) == sorted([os.path.basename(f"{new}.session"), another_bot.name])

    def test_la_sesion_que_estaba_en_app_se_mueve_a_config(self, dropbot, session_dir):
        _, legacy = session_dir
        legacy.parent.mkdir()
        legacy.write_bytes(b"auth key")
        legacy.chmod(0o644)

        path = dropbot.session_path()

        assert not legacy.exists()
        assert Path(f"{path}.session").read_bytes() == b"auth key"
        assert _mode(f"{path}.session") == 0o600

    def test_si_en_config_no_se_puede_escribir_sigue_en_el_directorio_de_trabajo(
        self, dropbot, session_dir, tmp_path, monkeypatch
    ):
        not_a_folder = tmp_path / "fichero"
        not_a_folder.write_text("")
        dropbot.store.init(str(not_a_folder / "config"))
        warnings = []
        monkeypatch.setattr(dropbot, "warning", warnings.append)

        assert dropbot.session_path() == "dropbot"
        assert len(warnings) == 1


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
