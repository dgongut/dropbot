"""Las carpetas en las que el bot puede guardar y a las que puede mover.

Hasta la 4.0.x la carpeta de cada tipo la decidía solo qué volúmenes había
montados. Ahora se puede elegir desde /settings, y un fichero se puede mover a
otra carpeta, pero la regla de fondo no cambia: el bot solo escribe dentro de
un volumen. Una ruta que no esté en uno vive en la capa del contenedor, y lo
que se guarde ahí se pierde en la siguiente actualización sin avisar.

Lo que vale como destino:

- /downloads y lo que cuelgue de ella, esté montada o no: es la carpeta general
  de siempre, y si falta su volumen ya lo avisa el arranque.
- Cualquier ruta bajo un volumen montado (`/video`, `/media/series`...), salvo
  las del propio bot (/config, /app) y las del sistema.

Las rutas se resuelven antes de mirarlas: un enlace simbólico dentro de un
volumen que apunta fuera no sirve para salir de él.
"""

import os

import config
import settings
import store
from basic import MAX_FILENAME_BYTES, has_invisible_chars
from logger import debug, warning
from utils import mounts

# Sustituible en los tests, que deciden qué está montado (ver conftest)
is_mounted = mounts.is_mounted

# Donde nunca se guarda nada aunque sea un volumen: lo del sistema y lo del bot
SYSTEM_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/etc", "/app", "/bin", "/sbin", "/lib", "/usr", "/boot")

# Lo más larga que puede ser una ruta que se escribe a mano
MAX_PATH_BYTES = 1024


class FolderError(Exception):
    """Una carpeta que no vale. `code` es el sufijo de su texto (folder_error_<code>)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _is_inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def normalize(path):
    """La ruta absoluta y sin vueltas de `path`, o None si no es una ruta que valga."""
    path = (path or "").strip()
    if not path.startswith("/") or "\0" in path or has_invisible_chars(path):
        return None
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        return None
    path = os.path.normpath(path)
    # normpath deja "//x" como está: POSIX lo permite, pero aquí es "/x"
    path = "/" + path.lstrip("/")
    for segment in path.split("/")[1:]:
        # Una carpeta oculta desaparecería de los listados del bot
        if segment.startswith(".") or len(segment.encode("utf-8")) > MAX_FILENAME_BYTES:
            return None
    return path


def _forbidden_roots():
    return SYSTEM_PREFIXES + (store.root(), config.TEMP_DIR)


def is_allowed(path):
    """Si `path` es un sitio donde el bot puede guardar (ver el módulo)."""
    path = normalize(path)
    if path is None or path == "/":
        return False
    # Resuelta: la parte que ya existe puede ser un enlace hacia fuera
    real = os.path.realpath(path)
    for candidate in {path, real}:
        if any(_is_inside(candidate, root) for root in _forbidden_roots()):
            return False
    base = os.path.realpath(config.DOWNLOAD_PATH)
    if _is_inside(real, base):
        return True
    return mounts.is_persisted(real, is_mounted)


def check(path, create=False):
    """La ruta normalizada de una carpeta de destino. Lanza FolderError si no vale.

    Con `create`, la crea si no existe; sin él, tiene que existir ya.
    """
    normalized = normalize(path)
    if normalized is None:
        raise FolderError("invalid")
    if not is_allowed(normalized):
        raise FolderError("outside")
    if os.path.exists(normalized):
        if not os.path.isdir(normalized):
            raise FolderError("not_folder")
        return normalized
    if not create:
        raise FolderError("missing")
    try:
        os.makedirs(normalized, exist_ok=True)
    except OSError as e:
        warning(f"[FOLDERS] Cannot create {normalized}: {e}")
        raise FolderError("create")
    # Creada, se vuelve a mirar: un enlace en mitad de la ruta puede haberla
    # llevado fuera del volumen
    if not is_allowed(normalized):
        raise FolderError("outside")
    debug(f"[FOLDERS] Created {normalized}")
    return normalized


# ---------------------------------------------------------------------------
# La carpeta de cada tipo
# ---------------------------------------------------------------------------

def default_paths():
    """A dónde iría cada tipo sin nada elegido: lo que digan los volúmenes."""
    return config.resolve_download_paths(mounted=is_mounted)


def chosen_paths():
    """Las carpetas elegidas en /settings que siguen valiendo, {tipo: ruta}.

    Una que ya no está en un volumen (se quitó del compose) se ignora, con un
    aviso: guardar ahí sería guardar dentro del contenedor.
    """
    chosen = {}
    for kind, path in settings.folder_paths().items():
        if is_allowed(path):
            chosen[kind] = normalize(path)
        else:
            warning(f"[FOLDERS] The folder chosen for {kind}, {path}, is not inside a mapped volume: using the default one")
    return chosen


def apply():
    """Recalcula config.DOWNLOAD_PATHS con las carpetas elegidas.

    En el mismo diccionario: todos los módulos lo tienen importado por su
    nombre, y así ven el cambio sin reiniciar. Devuelve el diccionario.
    """
    paths = config.resolve_download_paths(mounted=is_mounted, overrides=chosen_paths())
    for path in set(paths.values()):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            warning(f"[FOLDERS] Cannot create {path}: {e}")
    config.DOWNLOAD_PATHS.clear()
    config.DOWNLOAD_PATHS.update(paths)
    return config.DOWNLOAD_PATHS


def set_kind_folder(kind, path):
    """Elige la carpeta de un tipo (None = la de por defecto) y la aplica."""
    if kind not in settings.FOLDER_KINDS:
        raise FolderError("invalid")
    chosen = settings.folder_paths()
    if path is None:
        chosen.pop(kind, None)
    else:
        chosen[kind] = check(path, create=True)
    settings.put("folders.paths", chosen)
    apply()


def is_chosen(kind):
    """Si la carpeta de ese tipo se eligió en /settings (y sigue valiendo)."""
    return kind in chosen_paths()


# ---------------------------------------------------------------------------
# Las carpetas que se ofrecen
# ---------------------------------------------------------------------------

def roots():
    """Las carpetas de descarga, sin repetir: la general y la de cada tipo."""
    found = [config.DOWNLOAD_PATH]
    for kind in settings.FOLDER_KINDS:
        path = config.DOWNLOAD_PATHS[kind]
        if path not in found:
            found.append(path)
    return found


def favorites():
    """Las carpetas favoritas que siguen valiendo."""
    return [path for path in settings.favorite_folders() if is_allowed(path)]


def add_favorite(path):
    """Añade una favorita. Devuelve False si ya lo era."""
    path = check(path, create=True)
    stored = settings.favorite_folders()
    if path in stored:
        return False
    settings.put("folders.favorites", stored + [path])
    return True


def remove_favorite(path):
    stored = settings.favorite_folders()
    if path in stored:
        stored.remove(path)
        settings.put("folders.favorites", stored)


def known():
    """Lo que se ofrece al elegir carpeta: favoritas primero, luego las de descarga."""
    found = []
    for path in favorites() + roots():
        if path not in found:
            found.append(path)
    return found


def root_of(path):
    """La carpeta conocida (de descarga o favorita) más profunda que contiene
    a `path`, o None si no está bajo ninguna."""
    best = None
    for root in known():
        if _is_inside(path, root) and (best is None or len(root) > len(best)):
            best = root
    return best


def is_root(path):
    return path in known()


def subfolders(path):
    """Las subcarpetas visibles de `path`, por orden alfabético."""
    found = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if not entry.name.startswith(".") and entry.is_dir():
                        found.append(entry.path)
                except OSError:
                    continue
    except OSError as e:
        warning(f"[FOLDERS] Cannot read {path}: {e}")
    return sorted(found, key=lambda item: os.path.basename(item).lower())


def parent_of(path):
    """La carpeta de encima, si también vale como destino; si no, None."""
    parent = os.path.dirname(path.rstrip("/"))
    if parent and parent != path and is_allowed(parent):
        return parent
    return None
