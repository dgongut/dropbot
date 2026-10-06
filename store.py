"""Almacenamiento persistente de lo que escribe el bot.

Todo vive en un único volumen, montado en /config:

    /config/settings.json          ajustes del usuario (desde /settings)
    /config/state/telemetry.json   estado de las estadísticas (regenerable)

Los ajustes y el estado nunca comparten fichero: el documento entero se
reescribe en cada cambio, y mezclarlos supondría reescribir los ajustes del
usuario cada vez que cambia algo interno, arriesgándolos a un corte a mitad.

Cada escritura va a un fichero temporal que se mueve a su sitio con
os.replace, atómico en POSIX: nadie lee nunca un documento a medio escribir.
"""

import json
import os
import threading
from contextlib import contextmanager

from logger import debug, error, warning
from utils.mounts import is_mounted, is_persisted

CONFIG_ROOT = "/config"
SETTINGS_FILE = "settings.json"
STATE_DIR = "state"

DEFAULTS = {
    "language": "ES",
    "downloads": {
        # Ficheros que se transfieren a la vez
        "parallel": 2,
        # Conexiones por fichero (estilo FastTelethon). 1 = método estándar
        "fast_connections": 8,
    },
    "urls": {
        # ASK / VIDEO / AUDIO
        "auto_format": "ASK",
        # ASK / SEND / SEND_DELETE / STORE
        "auto_send": "ASK",
        # MAX o la resolución máxima (2160, 1440, 1080, 720, 480, 360)
        "video_quality": "MAX",
        # STANDARD / HIGH / BEST
        "audio_quality": "STANDARD",
        # Pedir H.264 + AAC, que Telegram reproduce sin convertir
        "prefer_compatible": True,
        # MP3 / M4A (sin recodificar)
        "audio_format": "MP3",
        # Título, artista y carátula dentro del fichero de audio
        "audio_tags": True,
        # OFF / SPONSOR / PROMO / ALL: qué tramos quita SponsorBlock
        "sponsorblock": "OFF",
        # ASK / FULL / FIRST: qué hacer con una playlist
        "playlist": "ASK",
        # Tope de vídeos de una playlist entera. 0 = sin tope
        "playlist_limit": 0,
        # Tamaño máximo de una descarga en MB. 0 = sin límite
        "max_size_mb": 0,
    },
    "video": {
        # NONE / VAAPI / NVENC / QSV
        "hw": "NONE",
        # 1-51, o None para el valor por defecto de cada encoder
        "quality": None,
    },
    "extract": {
        # ASK / DELETE / KEEP: qué hacer con el comprimido tras descomprimirlo
        "after": "ASK",
    },
    # Estadísticas anónimas, una vez al día. Ver telemetry.py
    "telemetry": True,
}

_lock = threading.RLock()
_root = None
_persistent = False
_settings = None
# Por qué no se pudo leer settings.json, si no se pudo. Mientras esté puesto
# el fichero no se escribe: una errata editándolo a mano no puede acabar con
# todos los ajustes sustituidos por los valores por defecto.
_settings_unreadable = None
_batch_depth = 0
_batch_dirty = False


def init(root=None):
    """Fija la carpeta de trabajo y la crea. Se puede llamar más de una vez.

    `root` es para los tests; en el contenedor siempre es CONFIG_ROOT.
    """
    global _root, _persistent, _settings, _settings_unreadable

    with _lock:
        if _root is not None and root is None:
            return _root
        if root is not None:
            _settings = None
            _settings_unreadable = None
        _root = root or CONFIG_ROOT
        _persistent = is_mounted(_root) or is_persisted(_root)
        try:
            os.makedirs(os.path.join(_root, STATE_DIR), exist_ok=True)
        except OSError as e:
            error(f"[CONFIG] Cannot create the storage directory {_root}: {e}")
        if not _persistent:
            warning(f"[CONFIG] {_root} is not a mapped volume: settings will be lost when the container is recreated.")
        return _root


def root():
    return init()


def state_dir():
    return os.path.join(root(), STATE_DIR)


def settings_path():
    return os.path.join(root(), SETTINGS_FILE)


def settings_exists():
    return os.path.exists(settings_path())


def is_persistent():
    """False cuando nada de lo que se escriba sobrevivirá a recrear el contenedor."""
    init()
    return _persistent


# ---------------------------------------------------------------------------
# Lectura y escritura
# ---------------------------------------------------------------------------

def _copy(value):
    return json.loads(json.dumps(value))


def _merge(base, stored):
    """Superpone `stored` a `base` de forma recursiva, conservando claves desconocidas."""
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


def write_document(path, document):
    """Escribe `document` de forma atómica.

    El flush y el fsync son los que hacen atómico el rename en la práctica: sin
    ellos los metadatos pueden llegar al disco antes que el contenido, y un
    corte de luz entre medias deja un settings.json que existe y está vacío.
    """
    temporary = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=4, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception as e:
        error(f"[CONFIG] Cannot write {path}: {e}")
        try:
            os.remove(temporary)
        except OSError:
            pass


def _document():
    global _settings, _settings_unreadable

    if _settings is not None:
        return _settings
    _settings = _copy(DEFAULTS)
    path = settings_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return _settings
    except Exception as e:
        stored = None
        _settings_unreadable = str(e)
    else:
        if not isinstance(stored, dict):
            stored = None
            _settings_unreadable = "not a JSON object"
    if stored is None:
        error(f"[CONFIG] {path} cannot be read ({_settings_unreadable}). Running on defaults and "
              f"leaving the file untouched: fix it and restart the container.")
        return _settings
    return _merge(_settings, stored)


def _flush():
    global _batch_dirty

    if _batch_depth:
        _batch_dirty = True
        return
    if _settings_unreadable is not None:
        debug(f"[CONFIG] Not writing {SETTINGS_FILE}: it could not be read, and writing would replace it")
        return
    write_document(settings_path(), _settings)


@contextmanager
def batch():
    """Agrupa las escrituras hechas dentro del bloque en una sola."""
    global _batch_depth, _batch_dirty

    with _lock:
        _batch_depth += 1
    try:
        yield
    finally:
        with _lock:
            _batch_depth -= 1
            if not _batch_depth and _batch_dirty:
                _batch_dirty = False
                _flush()


def settings_unreadable():
    """Por qué no se pudo leer settings.json, o None si se leyó bien."""
    with _lock:
        _document()
        return _settings_unreadable


def _walk(document, dotted_key, create=False):
    segments = dotted_key.split(".")
    container = document
    for segment in segments[:-1]:
        nested = container.get(segment)
        if not isinstance(nested, dict):
            if not create:
                return None, None
            nested = {}
            container[segment] = nested
        container = nested
    return container, segments[-1]


def default(dotted_key):
    container, last = _walk(DEFAULTS, dotted_key)
    return None if container is None else _copy(container.get(last))


def get(dotted_key):
    """Lee un ajuste por su ruta, p. ej. get("urls.auto_send").

    Lo que falte cae en DEFAULTS, así que un settings.json escrito por una
    versión anterior nunca deja un ajuste nuevo sin definir. Validar el valor
    es cosa de `settings`, que sabe qué admite cada uno.
    """
    with _lock:
        container, last = _walk(_document(), dotted_key)
        if container is None or last not in container:
            return default(dotted_key)
        value = container[last]
        return _copy(value) if isinstance(value, (dict, list)) else value


def set(dotted_key, value):
    """Escribe un ajuste y lo guarda en disco."""
    with _lock:
        container, last = _walk(_document(), dotted_key, create=True)
        container[last] = value
        _flush()
        return value


def reload():
    """Olvida lo que hay en memoria: la próxima lectura va al disco."""
    global _settings, _settings_unreadable

    with _lock:
        _settings = None
        _settings_unreadable = None
