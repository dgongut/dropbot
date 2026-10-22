"""Infraestructura común para los tests de DropBot.

`dropbot.py` hace bastante trabajo en tiempo de importación: migra los
ajustes, crea las carpetas de descarga, limpia TEMP_DIR y crea el cliente de
Telegram. Para poder importarlo en un test hay que preparar tres cosas antes:

1. Las variables de entorno mínimas, o el módulo llama a `sys.exit(1)`.
2. Las rutas de `config` y de `store`, que por defecto apuntan a `/downloads`,
   `/config` y `/tmp` (pensadas para el contenedor) y que en un portátil no
   son escribibles.
3. `TelegramClient`, que se conectaría de verdad en main() y que, al crearlo,
   ya abre su sesión. Se sustituye por un doble.

Los puntos 1 y 2 se hacen al importar este fichero, no dentro de un fixture:
pytest importa los conftest antes que los módulos de test, y basta con que un
test haga `from basic import ...` en su cabecera para que `config` quede
importado (con las rutas reales) antes de que ningún fixture pueda tocarlo.

Solo se sustituye `TelegramClient`: el resto de telethon es el de verdad,
así que los tests comprueban los atributos reales (`DocumentAttributeAudio`
y compañía) en lugar de mocks.

Tampoco puede contar nada de la máquina que los ejecuta: qué hay montado, el
espacio libre, un proxy en el entorno o los ficheros de un contenedor. En la
CI de GitHub, con /tmp montado aparte, el /config de los tests salía como un
volumen.
"""

import asyncio
import functools
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telethon import events
from telethon.tl.types import (
    InputPeerChannel, Message, MessageReplyHeader, PeerChannel, UpdateBotCallbackQuery,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# --- Punto 1: entorno mínimo -------------------------------------------------
ADMIN_ID = 999

os.environ.update({
    "TELEGRAM_TOKEN": "123456:test-token",
    "TELEGRAM_ADMIN": str(ADMIN_ID),
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "testhash",
    # Ni un envío de estadísticas desde los tests
    "TELEMETRY": "false",
})
# Las variables que en la 4.0.0 pasaron a ser ajustes no deben colarse desde el
# entorno de quien ejecuta los tests: sembrarían el settings.json de pruebas
for _variable in ("LANGUAGE", "PARALLEL_DOWNLOADS", "FAST_CONNECTIONS", "AUTO_DOWNLOAD_FORMAT",
                  "AUTO_SEND", "FFMPEG_HW", "FFMPEG_QUALITY"):
    os.environ.pop(_variable, None)
# Ningún test sale a la red, pero varios hablan con un servidor en 127.0.0.1:
# con un proxy heredado del entorno (un runner propio, un portátil de empresa)
# esas peticiones se iban al proxy. no_proxy, además, hace que urllib no mire
# el proxy del sistema en macOS
for _variable in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy"):
    os.environ.pop(_variable, None)
    os.environ.pop(_variable.upper(), None)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

# --- Punto 2: rutas dentro de un directorio temporal -------------------------
_SANDBOX = Path(tempfile.mkdtemp(prefix="dropbot-tests-"))

import config  # noqa: E402  (tiene que ir después de preparar el entorno)

for _name in ("DOWNLOAD_PATH", "DOWNLOAD_AUDIO", "DOWNLOAD_VIDEO", "DOWNLOAD_PHOTO",
              "DOWNLOAD_TORRENT", "DOWNLOAD_EBOOK", "DOWNLOAD_URL_VIDEO",
              "DOWNLOAD_URL_AUDIO"):
    setattr(config, _name, str(_SANDBOX / _name.lower()))
config.TEMP_DIR = str(_SANDBOX / "temp")
# Lo que el bot busca en el contenedor fuera de esas carpetas. En la máquina de
# los tests puede existir (la sesión de una 4.0.0, unas cookies montadas en el
# contenedor de desarrollo): al importar dropbot, la sesión se movía al sandbox
# y se perdía, y las cookies se colaban en los comandos de yt-dlp
config.LEGACY_SESSION_FILE = str(_SANDBOX / "app" / "dropbot.session")
config.YTDLP_COOKIES_FILE = str(_SANDBOX / "app" / "cookies" / "cookies.txt")
config.HEARTBEAT_FILE = str(_SANDBOX / "heartbeat")
config.POT_PROVIDER_DIR = str(_SANDBOX / "pot-provider")
# Y lo que wget lee de ~/.wgetrc, que puede llevar un proxy
os.environ["WGETRC"] = str(_SANDBOX / "wgetrc")
(_SANDBOX / "wgetrc").touch()

# Qué está montado como volumen lo dicen los tests, no la máquina: con /tmp
# montado aparte (un tmpfs, como en la CI de GitHub, u otro disco) todo lo que
# hay bajo tmp_path contaba como volumen, y un /config de prueba salía
# persistente. Como si solo estuvieran montadas /url_video y /url_audio, que
# los tests de URLs usan por separado; un test monta algo más con `volumes`
MOUNTED = {config.DOWNLOAD_URL_VIDEO, config.DOWNLOAD_URL_AUDIO}


def _mounted(path):
    return path in MOUNTED


# DOWNLOAD_PATHS se calcula al importar config a partir de las rutas de arriba,
# asi que hay que recalcularlo despues de sustituirlas
config.DOWNLOAD_PATHS = config.resolve_download_paths(mounted=_mounted)
# Con el detector como argumento por defecto: quien lo pase (test_config) manda.
# dropbot y handlers.settings lo importan por su nombre, así que tiene que estar
# sustituido antes de que se importen
config.downloads_persistent = functools.partial(config.downloads_persistent, mounted=_mounted)

import store  # noqa: E402
from utils import mounts  # noqa: E402

# store tiene los dos importados por su nombre, y el `mounted` por defecto de
# is_persisted ya está ligado al detector de verdad: hay que cambiar ambos
store.is_mounted = _mounted
store.is_persisted = functools.partial(mounts.is_persisted, mounted=_mounted)

store.init(str(_SANDBOX / "config"))


def pytest_sessionfinish(session, exitstatus):
    """El sandbox no se queda en /tmp después de cada pasada."""
    shutil.rmtree(_SANDBOX, ignore_errors=True)


# Cada `@bot.on(evento)` que se evalúe al importar deja aquí (evento, función)
REGISTERED_HANDLERS = []


def _recording_decorator(event, *args, **kwargs):
    """Sustituto de `bot.on(...)`: anota el registro y devuelve la función intacta.

    Con un MagicMock, `@bot.on(...)` reemplazaría cada handler por un mock y los
    tests no podrían invocarlos. Anotar el registro permite además comprobar
    que ningún handler se queda sin registrar o sin control de admin.
    """
    def decorator(func):
        REGISTERED_HANDLERS.append((event, func))
        return func
    return decorator


@pytest.fixture(scope="session")
def dropbot():
    """Importa `dropbot` una sola vez, con el cliente de Telegram simulado."""
    import telethon

    client = MagicMock(name="bot", on=_recording_decorator)
    original = telethon.TelegramClient
    telethon.TelegramClient = MagicMock(return_value=client)
    try:
        import dropbot as module
    finally:
        telethon.TelegramClient = original

    return module


@pytest.fixture(scope="session")
def registered_handlers(dropbot):
    """Los `(evento, función)` que el bot registró al importarse."""
    assert REGISTERED_HANDLERS, "no se ha registrado ningún handler"
    return list(REGISTERED_HANDLERS)


@pytest.fixture
def sent_messages():
    """Acumula lo que el bot le diría al usuario.

    Cada entrada es `(tipo, texto, kwargs)`, con tipo en
    reply / respond / edit / delete.
    """
    return []


def _patch_messaging(module, sent_messages, monkeypatch):
    """Sustituye en `module` los envíos a Telegram por capturas.

    Se aplica por módulo porque cada uno importa los `safe_*` por su cuenta, así
    que parchear uno no afecta a los demás.
    """
    async def reply(event, text=None, **kwargs):
        sent_messages.append(("reply", str(text), kwargs))
        return MagicMock(id=len(sent_messages))

    async def respond(event, text=None, **kwargs):
        sent_messages.append(("respond", str(text), kwargs))
        return MagicMock(id=len(sent_messages))

    async def edit(message, text=None, **kwargs):
        sent_messages.append(("edit", str(text), kwargs))
        return MagicMock(id=len(sent_messages))

    async def delete(message, *args, **kwargs):
        sent_messages.append(("delete", "", {}))
        return None

    async def send_message(chat_id, text=None, **kwargs):
        sent_messages.append(("send_message", str(text), kwargs))
        return MagicMock(id=len(sent_messages))

    async def answer(event, *args, **kwargs):
        return None

    for name, double in (("safe_reply", reply), ("safe_respond", respond),
                         ("safe_edit", edit), ("safe_delete", delete),
                         ("safe_send_message", send_message), ("safe_answer", answer)):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, double)
    return module


@pytest.fixture
def quiet_bot(dropbot, sent_messages, monkeypatch):
    """`dropbot` con los envíos a Telegram capturados en `sent_messages`."""
    # La cola solo arranca en main(), así que cualquier safe_* que se escape del
    # doble se quedaría 300s esperando su Future en lugar de fallar
    async def unstarted_queue(*args, **kwargs):
        raise AssertionError(
            "un envío a Telegram se ha escapado del doble: la cola no está "
            "arrancada en los tests y bloquearía 300s"
        )

    monkeypatch.setattr(dropbot.message_queue, "add_message", unstarted_queue)
    return _patch_messaging(dropbot, sent_messages, monkeypatch)


@pytest.fixture
def quiet_manage(quiet_bot, sent_messages, monkeypatch):
    """`handlers.manage` con sus envíos capturados y sus dependencias puestas.

    Importa el módulo a través de `quiet_bot` para que dropbot ya le haya
    inyectado el pipeline de envío con `init()`.
    """
    from handlers import manage

    return _patch_messaging(manage, sent_messages, monkeypatch)


@pytest.fixture
def texts(sent_messages):
    """Todo lo enviado al usuario concatenado, para aserciones sobre el texto."""
    return lambda: " | ".join(text for _, text, _ in sent_messages)


class FakeEvent:
    """Mínimo común de un NewMessage y un CallbackQuery para los handlers."""

    def __init__(self, event_id=1, chat_id=42, data=None, groups=()):
        self.id = event_id
        self.chat_id = chat_id
        self.sender_id = ADMIN_ID
        self.data = data
        self.pattern_match = _FakeMatch(groups) if groups else None


class _FakeMatch:
    def __init__(self, groups):
        # group(0) es la coincidencia completa, igual que en `re`
        self._groups = (b"",) + tuple(groups)

    def group(self, index):
        return self._groups[index]


# Un supergrupo con temas (un foro), su id como lo da Telethon y uno de sus temas
FORUM_CHANNEL = PeerChannel(1234567890)
FORUM_CHAT_ID = -1001234567890
FORUM_TOPIC = 77


def forum_reply_to(topic=FORUM_TOPIC, reply_to_msg_id=None):
    """El reply_to de un mensaje escrito en un tema de un foro.

    Directamente en el tema, apunta al mensaje que lo abrió; si contesta a
    otro mensaje del tema, apunta a ese y el tema va en reply_to_top_id.
    """
    if reply_to_msg_id is None:
        return MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic)
    return MessageReplyHeader(forum_topic=True, reply_to_msg_id=reply_to_msg_id, reply_to_top_id=topic)


@pytest.fixture
def forum_callback():
    """Un CallbackQuery de Telethon pulsado en un tema de un foro.

    `get_message()` es el de verdad: le pide el mensaje del botón a un cliente
    falso (`._client`), que cuenta las peticiones en `.requests`. Con su
    `.message = None`, Telegram ya no lo da (se ha borrado).
    """
    def factory(groups=(), topic=FORUM_TOPIC, message_id=50, query_id=10_000):
        query = UpdateBotCallbackQuery(query_id=query_id, user_id=ADMIN_ID, peer=FORUM_CHANNEL,
                                       msg_id=message_id, chat_instance=0, data=b"")
        event = events.CallbackQuery.Event(query, FORUM_CHANNEL, message_id)
        event.pattern_match = _FakeMatch(groups) if groups else None
        client = SimpleNamespace(requests=0, message=Message(
            id=message_id, peer_id=FORUM_CHANNEL, date=None, message="", reply_to=forum_reply_to(topic)))

        async def get_messages(chat, ids):
            client.requests += 1
            return client.message

        client.get_messages = get_messages
        event._client = client
        event._input_chat = InputPeerChannel(channel_id=FORUM_CHANNEL.channel_id, access_hash=1)
        return event
    return factory


@pytest.fixture
def config_dir(tmp_path):
    """Un /config vacío para el test, y el de la sesión de vuelta al acabar."""
    root = tmp_path / "config"
    store.init(str(root))
    yield root
    store.init(str(_SANDBOX / "config"))


@pytest.fixture
def volumes():
    """Las rutas que el test da por montadas como volumen, solo durante el test.

    Tras añadir una hay que volver a llamar a lo que lo mira al arrancar, como
    `store.init`.
    """
    before = set(MOUNTED)
    yield MOUNTED
    MOUNTED.clear()
    MOUNTED.update(before)


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    """Espacio libre de sobra, salvo que el test diga otra cosa.

    El de verdad depende del disco de la máquina: en un tmp con menos de
    100 MiB libres (un tmpfs pequeño, un disco de CI casi lleno) no se extraía
    ningún comprimido. Los tests de espacio lo vuelven a sustituir.
    """
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=2**41, used=0, free=2**40))


@pytest.fixture
def make_event():
    return FakeEvent


@pytest.fixture
def media_file(tmp_path):
    """Crea un fichero de prueba del tamaño pedido (disperso si es grande)."""
    def factory(name, size=1024):
        path = tmp_path / name
        with open(path, "wb") as handle:
            if size > 1024 * 1024:
                handle.truncate(size)
            else:
                handle.write(b"\0" * size)
        return str(path)
    return factory


@pytest.fixture
def run_async():
    """Ejecuta una corrutina sin depender de pytest-asyncio."""
    def runner(coro):
        return asyncio.run(coro)
    return runner
