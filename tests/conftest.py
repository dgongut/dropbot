"""Infraestructura común para los tests de DropBot.

`dropbot.py` hace bastante trabajo en tiempo de importación: valida la
configuración, crea las carpetas de descarga, limpia TEMP_DIR y abre la
conexión con Telegram. Para poder importarlo en un test hay que preparar
tres cosas antes:

1. Las variables de entorno mínimas, o el módulo llama a `sys.exit(1)`.
2. Las rutas de `config`, que por defecto apuntan a `/downloads` y `/tmp`
   (pensadas para el contenedor) y que en un portátil no son escribibles.
3. `TelegramClient`, que en la línea de creación del bot ya llama a
   `.start()` y se conectaría de verdad. Se sustituye por un doble.

Los puntos 1 y 2 se hacen al importar este fichero, no dentro de un fixture:
pytest importa los conftest antes que los módulos de test, y basta con que un
test haga `from basic import ...` en su cabecera para que `config` quede
importado (con las rutas reales) antes de que ningún fixture pueda tocarlo.

Solo se sustituye `TelegramClient`: el resto de telethon es el de verdad,
así que los tests comprueban los atributos reales (`DocumentAttributeAudio`
y compañía) en lugar de mocks.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# --- Punto 1: entorno mínimo -------------------------------------------------
os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("TELEGRAM_ADMIN", "999")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "testhash")
os.environ.setdefault("LANGUAGE", "ES")
os.environ.setdefault("AUTO_DOWNLOAD_FORMAT", "ASK")
os.environ.setdefault("AUTO_SEND", "ASK")

# --- Punto 2: rutas dentro de un directorio temporal -------------------------
_SANDBOX = Path(tempfile.mkdtemp(prefix="dropbot-tests-"))

import config  # noqa: E402  (tiene que ir después de preparar el entorno)

for _name in ("DOWNLOAD_PATH", "DOWNLOAD_AUDIO", "DOWNLOAD_VIDEO", "DOWNLOAD_PHOTO",
              "DOWNLOAD_TORRENT", "DOWNLOAD_EBOOK", "DOWNLOAD_URL_VIDEO",
              "DOWNLOAD_URL_AUDIO"):
    setattr(config, _name, str(_SANDBOX / _name.lower()))
config.TEMP_DIR = str(_SANDBOX / "temp")


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

    client = MagicMock(name="TelegramClient")
    client.start.return_value = MagicMock(name="bot", on=_recording_decorator)
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


@pytest.fixture
def quiet_bot(dropbot, sent_messages, monkeypatch):
    """Sustituye los envíos a Telegram por capturas en `sent_messages`.

    Devuelve el propio módulo para que el test siga trabajando con él.
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

    async def answer(event, *args, **kwargs):
        return None

    async def allow_admin(event):
        return False

    monkeypatch.setattr(dropbot, "safe_reply", reply)
    monkeypatch.setattr(dropbot, "safe_respond", respond)
    monkeypatch.setattr(dropbot, "safe_edit", edit)
    monkeypatch.setattr(dropbot, "safe_delete", delete)
    monkeypatch.setattr(dropbot, "safe_answer", answer)
    monkeypatch.setattr(dropbot, "check_admin_and_warn", allow_admin)
    return dropbot


@pytest.fixture
def texts(sent_messages):
    """Todo lo enviado al usuario concatenado, para aserciones sobre el texto."""
    return lambda: " | ".join(text for _, text, _ in sent_messages)


class FakeEvent:
    """Mínimo común de un NewMessage y un CallbackQuery para los handlers."""

    def __init__(self, event_id=1, chat_id=42, data=None, groups=()):
        self.id = event_id
        self.chat_id = chat_id
        self.sender_id = 999
        self.data = data
        self.pattern_match = _FakeMatch(groups) if groups else None


class _FakeMatch:
    def __init__(self, groups):
        # group(0) es la coincidencia completa, igual que en `re`
        self._groups = (b"",) + tuple(groups)

    def group(self, index):
        return self._groups[index]


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
