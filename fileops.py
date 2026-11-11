"""Lo que /manage hace con los ficheros que ya están en el servidor.

Mover entre carpetas, sugerir un nombre limpio (ver name_parser), renombrar de
una vez todo lo que hay en una carpeta, buscar por nombre y encontrar la
carpeta donde ya está el resto de una serie. Aquí no hay nada de Telegram: son
funciones sobre el disco, bloqueantes, que los handlers llaman con
asyncio.to_thread.

Es la operativa de archivos de Torrent-Controller-Bot traída a ficheros en
disco. Allí cada torrent es la unidad (su nombre da contexto a los ficheros
que lleva dentro); aquí lo es cada elemento que cuelga de una carpeta de
descarga: `/downloads/Mad.Men.S01.1080p/01.mkv` toma de su carpeta lo que le
falta, igual que un fichero de un torrent lo toma del torrent.
"""

import errno
import os
import re
import shutil
import stat
import unicodedata
from collections import namedtuple

import folders
import name_parser
import settings
from basic import MAX_FILENAME_BYTES, has_invisible_chars
from logger import debug, warning
from utils.file_helpers import (
    COPY_CHUNK_BYTES, EXTRACTING_SUFFIX, move_to_unique_path, partial_name,
)

# Hasta dónde se baja al buscar o al mirar qué series hay, y cuánto se mira
# como mucho: una biblioteca enorme en un disco de red no puede dejar al bot
# minutos recorriéndola
SCAN_MAX_DEPTH = 4
SCAN_MAX_ENTRIES = 50000
# Lo más que devuelve una búsqueda
SEARCH_MAX_RESULTS = 200
# Carpetas de la serie que se proponen como mucho: más solo serían ruido
SERIES_SUGGESTIONS_MAX = 2


class MoveError(Exception):
    """Un movimiento que no se hace. `code` es el sufijo de su texto (move_error_<code>)."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Mover
# ---------------------------------------------------------------------------

def tree_size(path):
    """Lo que ocupa un fichero o una carpeta entera, sin seguir enlaces."""
    try:
        if not os.path.isdir(path) or os.path.islink(path):
            return os.lstat(path).st_size
    except OSError:
        return 0
    total = 0
    for directory, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def _is_inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _check_movable(path):
    """Solo se mueven ficheros, carpetas y enlaces: un dispositivo o una
    tubería no se puede copiar a otro disco, y se perdería al borrar el origen."""
    for directory, names, files in os.walk(path):
        for name in names + files:
            mode = os.lstat(os.path.join(directory, name)).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise MoveError("special", name)


def _copy_tree(source, target):
    """Copia una carpeta a otro dispositivo: solo el contenido, sin permisos ni
    metadatos, que entre sistemas de ficheros distintos fallan."""
    os.mkdir(target)
    with os.scandir(source) as entries:
        for entry in entries:
            destination = os.path.join(target, entry.name)
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), destination)
            elif entry.is_dir(follow_symlinks=False):
                _copy_tree(entry.path, destination)
            else:
                with open(entry.path, "rb") as reader, open(destination, "wb") as writer:
                    shutil.copyfileobj(reader, writer, COPY_CHUNK_BYTES)
                    writer.flush()
                    os.fsync(writer.fileno())


def _same_device(source, target_dir):
    """Si mover `source` a `target_dir` es un simple renombrado."""
    return os.lstat(source).st_dev == os.stat(target_dir).st_dev


def move_item(source, target_dir):
    """Mueve un fichero o una carpeta a `target_dir` con su mismo nombre.

    Devuelve la ruta nueva. Nunca pisa nada: si en el destino ya hay algo que
    se llama igual, no se mueve (MoveError "exists"). En el mismo sistema de
    ficheros es un renombrado; entre dispositivos se copia a un oculto del
    destino, que no aparece en los listados, y solo cuando está entero toma su
    nombre y se borra el original: una parada a mitad deja el original intacto.
    Es bloqueante: llamarla con asyncio.to_thread.
    """
    if not os.path.lexists(source):
        raise MoveError("gone")
    name = os.path.basename(source.rstrip("/"))
    source_real = os.path.realpath(source)
    target_real = os.path.realpath(target_dir)
    if not os.path.isdir(target_real):
        raise MoveError("no_target")
    if os.path.dirname(source_real) == target_real:
        raise MoveError("same")
    is_directory = os.path.isdir(source) and not os.path.islink(source)
    if is_directory and _is_inside(target_real, source_real):
        raise MoveError("inside")
    target = os.path.join(target_dir, name)
    if os.path.lexists(target):
        raise MoveError("exists", name)

    if _same_device(source, target_dir):
        try:
            os.rename(source, target)
        except OSError as e:
            # Dos volúmenes del mismo disco (dos montajes de Docker) dicen ser
            # el mismo dispositivo, pero entre montajes no se puede renombrar:
            # el sistema contesta EXDEV, y hay que copiar como entre discos
            if e.errno != errno.EXDEV:
                raise
        else:
            debug(f"[MOVE] {source} -> {target}")
            return target

    # Otro dispositivo: hay que copiarlo entero, y tiene que caber
    size = tree_size(source)
    if size > shutil.disk_usage(target_dir).free:
        raise MoveError("space")
    if not is_directory:
        if not stat.S_ISREG(os.lstat(source).st_mode):
            raise MoveError("special", name)
        moved = move_to_unique_path(source, target_dir, name)
        debug(f"[MOVE] {source} -> {moved} (copied across devices)")
        return moved

    _check_movable(source)
    # Con el sufijo de las extracciones a medias: si el bot se para a mitad de
    # la copia, el arranque borra el oculto (ver remove_partial_extractions)
    hidden = os.path.join(target_dir, partial_name(f"move-{os.getpid()}-{abs(hash(source)) % 100000}{EXTRACTING_SUFFIX}"))
    try:
        _copy_tree(source, hidden)
        if os.path.lexists(target):
            raise MoveError("exists", name)
        os.rename(hidden, target)
    except BaseException:
        shutil.rmtree(hidden, ignore_errors=True)
        raise
    shutil.rmtree(source, ignore_errors=True)
    if os.path.lexists(source):
        warning(f"[MOVE] {source} was copied to {target} but could not be removed completely")
    debug(f"[MOVE] {source} -> {target} (copied across devices)")
    return target


# ---------------------------------------------------------------------------
# Sugerir un nombre
# ---------------------------------------------------------------------------

def _extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_video(name):
    return _extension(name) in name_parser.VALID_EXTENSIONS


def is_subtitle(name):
    return _extension(name) in name_parser.SUBTITLE_EXTENSIONS


def naming():
    """Las plantillas del usuario y el prefijo de temporada de su idioma."""
    return {
        "template_movie": settings.rename_template("movie"),
        "template_series": settings.rename_template("series"),
        "template_season": settings.rename_template("season"),
        "season_prefix": "T" if settings.language() == "ES" else "S",
    }


def usable_name(name):
    """Si `name` vale como nombre de fichero o de carpeta."""
    return bool(name) and "/" not in name and "\\" not in name and not name.startswith(".") \
        and not has_invisible_chars(name) and len(name.encode("utf-8")) <= MAX_FILENAME_BYTES


def _usable(suggested, current):
    return suggested if suggested and suggested != current and usable_name(suggested) else None


def suggest_name(name):
    """La sugerencia para un nombre suelto (lo que llega, o lo que cuelga de
    una carpeta de descarga), o None si no hay nada que sugerir."""
    return _usable(name_parser.suggest_name(name, **naming()), name)


def top_item(path):
    """El elemento que cuelga de una carpeta de descarga y contiene a `path`
    (él mismo, si cuelga directamente). Lo que no está bajo ninguna carpeta
    conocida es su propio contexto."""
    root = folders.root_of(os.path.dirname(path))
    if root is None:
        return path
    relative = os.path.relpath(path, root)
    return os.path.join(root, relative.split(os.sep)[0])


def _video_count(top, limit=2):
    """Cuántos vídeos lleva `top`, sin contar las muestras, hasta `limit`."""
    count = 0
    for directory, names, files in os.walk(top):
        names[:] = [name for name in names if not name.startswith(".")]
        for name in files:
            relative = os.path.relpath(os.path.join(directory, name), top)
            if is_video(name) and not name_parser.is_sample_file(relative):
                count += 1
                if count >= limit:
                    return count
    return count


def _suggest_file(path, top, single_video):
    """La sugerencia para un vídeo de dentro de `top`, que le da contexto."""
    name = os.path.basename(path)
    if not is_video(name):
        return None
    parent = os.path.dirname(path)
    folder_name = os.path.basename(parent) if parent != top else None
    if name_parser.is_sample_file(os.path.relpath(path, top)):
        return None
    suggested = name_parser.suggest_file_name(
        name, parent_name=os.path.basename(top), single_video=single_video, folder_name=folder_name, **naming())
    return _usable(suggested, name)


def _suggest_folder(path, top):
    """La sugerencia para una subcarpeta de `top` (una de temporada)."""
    name = os.path.basename(path)
    return _usable(name_parser.suggest_folder_name(name, parent_name=os.path.basename(top), **naming()), name)


def suggest_for(path):
    """La sugerencia para un fichero o una carpeta del servidor, o None."""
    name = os.path.basename(path)
    top = top_item(path)
    is_directory = os.path.isdir(path)
    if top == path or not os.path.isdir(top):
        if not is_directory and not is_video(name):
            return None
        return suggest_name(name)
    if is_directory:
        return _suggest_folder(path, top)
    return _suggest_file(path, top, _video_count(top) == 1)


# ---------------------------------------------------------------------------
# Subtítulos que acompañan a un vídeo
# ---------------------------------------------------------------------------

def _stem_length(name):
    return len(name.rpartition(".")[0])


def subtitle_renames(video_path, new_name, siblings=None):
    """Los renombrados que hacen falta para que los subtítulos de un vídeo le
    sigan emparejados cuando pase a llamarse `new_name`: [(ruta, nombre nuevo)].

    Un subtítulo que valdría para dos vídeos de la carpeta va con el de nombre
    más largo ("Alien Resurrection.es.srt" es de "Alien Resurrection.mkv", no
    de "Alien.mkv"), así que nunca se renombra dos veces.
    """
    folder = os.path.dirname(video_path)
    video = os.path.basename(video_path)
    if siblings is None:
        try:
            siblings = os.listdir(folder)
        except OSError:
            return []
    videos = [name for name in siblings if is_video(name)]
    renames = []
    for name in siblings:
        if not is_subtitle(name) or name == video:
            continue
        companion = name_parser.companion_subtitle_name(name, video, new_name)
        if not companion or companion == name or not usable_name(companion):
            continue
        owners = [other for other in videos if name_parser.companion_subtitle_name(name, other, "x") is not None]
        if max(owners, key=_stem_length, default=None) == video:
            renames.append((os.path.join(folder, name), companion))
    return renames


def rename_item(path, new_name):
    """Renombra un fichero o carpeta y, si es un vídeo, sus subtítulos con él.

    Devuelve (ruta nueva, subtítulos renombrados). No pisa nada: lanza
    FileExistsError si el nombre ya lo tiene otro.
    """
    folder = os.path.dirname(path)
    target = os.path.join(folder, new_name)
    if os.path.lexists(target):
        raise FileExistsError(new_name)
    companions = []
    if os.path.isfile(path) and is_video(os.path.basename(path)):
        companions = subtitle_renames(path, new_name)
    os.rename(path, target)
    renamed = 0
    for old_path, companion in companions:
        destination = os.path.join(folder, companion)
        try:
            if not os.path.lexists(destination):
                os.rename(old_path, destination)
                renamed += 1
        except OSError as e:
            warning(f"[RENAME] Could not rename the subtitle {old_path}: {e}")
    return target, renamed


# ---------------------------------------------------------------------------
# Renombrar todo lo que hay en una carpeta
# ---------------------------------------------------------------------------

# files y folders son listas de (ruta, nombre nuevo). Las carpetas, de la más
# honda a la de arriba: renombrar una cambia la ruta de todo lo que lleva
# dentro, y así cada renombrado encuentra aún la ruta con la que se planeó.
# skipped son los nombres que no se tocan porque chocarían con otro
RenamePlan = namedtuple("RenamePlan", "files folders skipped")


def _walk(top):
    """(carpeta, subcarpetas, ficheros) de todo lo visible bajo `top`."""
    for directory, names, files in os.walk(top):
        names[:] = sorted(name for name in names if not name.startswith("."))
        yield directory, names, sorted(name for name in files if not name.startswith("."))


def build_plan(top, include_top=True):
    """El plan para renombrar de una vez una carpeta: ella misma (con
    `include_top`), sus subcarpetas de temporada y los vídeos, cada uno con
    sus subtítulos. Para un fichero suelto, él y sus subtítulos."""
    if not os.path.isdir(top):
        suggested = suggest_for(top)
        if not suggested:
            return RenamePlan([], [], 0)
        if os.path.lexists(os.path.join(os.path.dirname(top), suggested)):
            return RenamePlan([], [], 1)
        return RenamePlan([(top, suggested)] + subtitle_renames(top, suggested), [], 0)

    files_plan = []
    folders_plan = []
    skipped = 0
    single_video = _video_count(top) == 1
    for directory, names, files in _walk(top):
        taken = set(names) | set(files)
        for name in files:
            path = os.path.join(directory, name)
            suggested = _suggest_file(path, top, single_video)
            if not suggested:
                continue
            taken.discard(name)
            if suggested in taken:
                skipped += 1
                taken.add(name)
                continue
            taken.add(suggested)
            files_plan.append((path, suggested))
            for subtitle_path, companion in subtitle_renames(path, suggested, files):
                subtitle = os.path.basename(subtitle_path)
                if companion in taken:
                    skipped += 1
                    continue
                taken.discard(subtitle)
                taken.add(companion)
                files_plan.append((subtitle_path, companion))
        for name in names:
            suggested = _suggest_folder(os.path.join(directory, name), top)
            if not suggested:
                continue
            taken.discard(name)
            if suggested in taken:
                skipped += 1
                taken.add(name)
                continue
            taken.add(suggested)
            folders_plan.append((os.path.join(directory, name), suggested))
    folders_plan.sort(key=lambda item: item[0].count(os.sep), reverse=True)
    if include_top:
        suggested = suggest_name(os.path.basename(top))
        if suggested:
            if os.path.lexists(os.path.join(os.path.dirname(top), suggested)):
                skipped += 1
            else:
                folders_plan.append((top, suggested))
    return RenamePlan(files_plan, folders_plan, skipped)


def merge_plans(plans):
    """Un solo plan con los de varios elementos (los de un listado)."""
    files, plan_folders, skipped = [], [], 0
    for plan in plans:
        files += plan.files
        plan_folders += plan.folders
        skipped += plan.skipped
    plan_folders.sort(key=lambda item: item[0].count(os.sep), reverse=True)
    return RenamePlan(files, plan_folders, skipped)


def apply_plan(plan, on_renamed=None):
    """Aplica un plan: primero los ficheros y luego las carpetas, de la más
    honda a la de arriba. Devuelve (ficheros, carpetas, errores).
    `on_renamed(ruta vieja, ruta nueva)` se llama tras cada renombrado."""
    def rename_all(items):
        done = 0
        for path, new_name in items:
            target = os.path.join(os.path.dirname(path), new_name)
            try:
                if os.path.lexists(target):
                    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))
                os.rename(path, target)
                done += 1
                if on_renamed:
                    on_renamed(path, target)
            except OSError as e:
                errors.append(f"{os.path.basename(path)}: {e.strerror or e}")
        return done

    errors = []
    files_done = rename_all(plan.files)
    folders_done = rename_all(plan.folders)
    return files_done, folders_done, errors


# ---------------------------------------------------------------------------
# Buscar
# ---------------------------------------------------------------------------

def _plain(text):
    """En minúsculas y sin tildes, para comparar."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def parse_query(query):
    """Los términos de una búsqueda: (palabras, frases exactas, rutas).

    Cada palabra puede ir en cualquier orden; lo que va entre comillas, tal
    cual; y lo que empieza por "/" tiene que aparecer así en la ruta.
    """
    words, phrases, paths = [], [], []
    rest = query or ""
    while '"' in rest:
        before, _, after = rest.partition('"')
        phrase, closed, after = after.partition('"')
        if not closed:
            rest = before + " " + phrase
            break
        if phrase.strip():
            phrases.append(_plain(phrase.strip()))
        rest = before + " " + after
    for term in rest.split():
        if term.startswith("/") and len(term) > 1:
            paths.append(_plain(term.rstrip("/")))
        else:
            words += name_parser.comparable_text(term).split()
    return words, phrases, paths


def _matches(path, words, phrases, paths):
    name = os.path.basename(path)
    plain_name = _plain(name)
    plain_path = _plain(path)
    tokens = name_parser.comparable_text(name)
    spaced = " ".join(plain_name.replace(".", " ").replace("_", " ").split())
    return (all(word in tokens for word in words)
            and all(phrase in plain_name or phrase in spaced for phrase in phrases)
            and all(term in plain_path for term in paths))


def _scan(roots, max_depth=SCAN_MAX_DEPTH, limit=SCAN_MAX_ENTRIES):
    """Lo visible bajo `roots`, hasta `max_depth` niveles: (ruta, es carpeta)."""
    seen = set()
    count = 0
    pending = [(root, 0) for root in roots]
    while pending:
        directory, depth = pending.pop(0)
        real = os.path.realpath(directory)
        if real in seen:
            continue
        seen.add(real)
        try:
            with os.scandir(directory) as entries:
                listed = sorted(entries, key=lambda entry: entry.name.lower())
        except OSError:
            continue
        for entry in listed:
            if entry.name.startswith("."):
                continue
            try:
                is_directory = entry.is_dir()
            except OSError:
                continue
            yield entry.path, is_directory
            count += 1
            if count >= limit:
                return
            if is_directory and depth + 1 < max_depth and not entry.is_symlink():
                pending.append((entry.path, depth + 1))


def search(query, roots=None, limit=SEARCH_MAX_RESULTS):
    """Los ficheros y carpetas cuyo nombre (o ruta) casa con `query`."""
    words, phrases, paths = parse_query(query)
    if not (words or phrases or paths):
        return []
    found = []
    for path, _ in _scan(roots if roots is not None else folders.roots()):
        if _matches(path, words, phrases, paths):
            found.append(path)
            if len(found) >= limit:
                break
    return found


# ---------------------------------------------------------------------------
# La carpeta de la serie
# ---------------------------------------------------------------------------

def library(exclude=None):
    """Lo que hay en las carpetas conocidas, como lo quiere name_parser para
    buscar una serie: (nombre, carpeta que lo contiene, fecha)."""
    items = []
    for path, _ in _scan(folders.known()):
        if path == exclude:
            continue
        try:
            modified = os.lstat(path).st_mtime
        except OSError:
            modified = 0
        items.append((os.path.basename(path), os.path.dirname(path), modified))
    return items


def series_candidates(path):
    """Las carpetas donde ya está el resto de la serie de `path`, la mejor
    primero, como [(carpeta, si lleva el nombre de la serie)].

    Las carpetas de descarga son donde cae todo antes de ordenarlo: solo
    cuentan si llevan el nombre de la serie. Nunca se propone una carpeta
    que no valga como destino.
    """
    name = os.path.basename(path)
    identity = name_parser.series_identity(name)
    if identity is None:
        return _numbered_candidates(path)
    candidates = name_parser.series_dir_candidates(
        name,
        library(exclude=path),
        known_dirs=folders.favorites(),
        current_dir=os.path.dirname(path),
        generic_dirs=folders.roots(),
    )
    candidates = [(directory, named) for directory, named in candidates if folders.is_allowed(directory)]
    # Una sola carpeta, que no se llama como la serie pero en la que no hay
    # más que episodios suyos ("Breakingg BAAAD" con dos capítulos de Breaking
    # Bad): es la carpeta de la serie aunque el nombre no lo diga, y vale como
    # tal. Con algo más dentro puede ser un «Pendientes», y solo se propone
    if len(candidates) == 1 and not candidates[0][1] and _only_holds(candidates[0][0], identity["key"], path):
        candidates = [(candidates[0][0], True)]
    return candidates


def _only_holds(directory, series_key, exclude=None):
    """Si todos los vídeos de `directory` (alguno tiene que haber) son
    episodios de esa serie, y no es una carpeta de descarga."""
    if directory in folders.roots():
        return False
    try:
        names = [entry.name for entry in os.scandir(directory)
                 if not entry.name.startswith(".") and entry.path != exclude and entry.is_file()]
    except OSError:
        return False
    videos = [name for name in names if is_video(name)]
    return bool(videos) and all((name_parser.series_identity(name) or {}).get("key") == series_key for name in videos)


# Un episodio que solo lleva su número y su título, sin el nombre de la serie:
# "002 - Pink Pajamas (1964).mkv". Dos o tres cifras: cuatro son un año
NUMBERED_EPISODE_RE = re.compile(r"^(\d{2,3})\s*[-–.]\s*\S")


def _numbered_candidates(path):
    """La carpeta de la serie de un episodio que no dice de qué serie es.

    Hay series (dibujos, anime) que se numeran de corrido y cuyos ficheros solo
    llevan el número y el título del capítulo: por el nombre no hay forma de
    saber de cuál son. Lo que sí se puede ver es dónde están sus vecinos: una
    carpeta con vídeos numerados igual (las mismas cifras) a la que le falta
    justo ese número. Primero la que tiene el número más cercano.

    Nunca se da por segura: es una suposición por la numeración, no por el
    nombre, y cualquier "004 - lo que sea.mkv" encajaría igual. Solo se
    propone, y se mueve si el usuario lo pide; «Directo a la carpeta de la
    serie» no se aplica aquí. Las carpetas de descarga no cuentan: es donde
    cae todo.
    """
    name = os.path.basename(path)
    match = NUMBERED_EPISODE_RE.match(name)
    if not match or not is_video(name):
        return []
    number, width = int(match.group(1)), len(match.group(1))
    current = os.path.dirname(path)
    generic = set(folders.roots())
    numbers = {}
    for other, directory, _ in library(exclude=path):
        if directory == current or directory in generic or not is_video(other):
            continue
        found = NUMBERED_EPISODE_RE.match(other)
        if found and len(found.group(1)) == width:
            numbers.setdefault(directory, set()).add(int(found.group(1)))
    ranked = sorted(
        (min(abs(number - other) for other in seen), -len(seen), directory)
        for directory, seen in numbers.items()
        # Si ya tiene ese número es otra serie (o ya lo tiene)
        if number not in seen and folders.is_allowed(directory))
    return [(directory, False) for _, _, directory in ranked]


def series_folders(path):
    """Las carpetas que se proponen para `path` (ver series_candidates)."""
    return [directory for directory, _ in series_candidates(path)][:SERIES_SUGGESTIONS_MAX]
