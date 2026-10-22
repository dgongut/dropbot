"""
Funciones de utilidad para manejo de archivos.
"""
import errno
import glob
import os
import shutil
import stat
from basic import MAX_FILENAME_BYTES, _truncate_to_bytes
from config import (
    EXTENSIONS_TORRENT, EXTENSIONS_EBOOK, EXTENSIONS_VIDEO,
    EXTENSIONS_AUDIO, EXTENSIONS_IMAGE, EXTENSIONS_COMPRESSED,
    TOR_ICO, BOO_ICO, VID_ICO, AUD_ICO, IMG_ICO, ZIP_ICO, DEF_ICO,
    INSTANCE_TAG,
)
from logger import debug, warning

# Sufijo de lo que el bot escribe a medias en las carpetas de descarga. Va en
# un nombre oculto (con punto delante), que /list y /manage no muestran, y al
# arrancar se borra lo que dejara una parada a mitad (remove_partial_files)
PARTIAL_SUFFIX = ".dropbot-part"

# Sufijo de la carpeta oculta en la que /manage descomprime antes de darle su
# nombre. También se borra al arrancar (remove_partial_extractions)
EXTRACTING_SUFFIX = ".extracting"

# Lo que se copia de cada vez entre dispositivos (ver move_to_unique_path)
COPY_CHUNK_BYTES = 1024 * 1024


def partial_name(name):
    """El nombre oculto con que este bot escribe `name` a medias en una carpeta de descarga.

    Lleva delante el id del bot (INSTANCE_TAG): dos bots pueden compartir las
    carpetas de descarga, y al arrancar uno borraba lo que el otro estaba
    descargando, copiando o descomprimiendo.
    """
    return f".{INSTANCE_TAG}.{name}"


def format_file_size(size_bytes):
    """Formatea el tamaño del archivo en formato legible"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def get_directory_size(directory):
    """Calcula el tamaño total de una carpeta recursivamente"""
    total_size = 0
    try:
        for dirpath, _, filenames in os.walk(directory):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.exists(filepath):
                    total_size += os.path.getsize(filepath)
    except Exception as e:
        warning(f"[FILE_SIZE] Error calculating folder size {directory}: {e}")
    return total_size


def get_unique_filename(directory, filename):
    """
    Genera un nombre de archivo único en el directorio especificado.
    Si el archivo existe, agrega un sufijo (1), (2), etc.
    """
    file_path = os.path.join(directory, filename)

    # Si no existe, retornar el nombre original
    if not os.path.exists(file_path):
        return filename

    # Separar nombre base y extensión
    base_name, extension = os.path.splitext(filename)
    counter = 1

    # Buscar un nombre único
    while True:
        new_filename = f"{base_name} ({counter}){extension}"
        new_path = os.path.join(directory, new_filename)
        if not os.path.exists(new_path):
            return new_filename
        counter += 1


def reserve_unique_path(directory, filename, keep_open=False):
    """
    Reserva un nombre libre en `directory` creando el fichero vacío y devuelve
    su ruta. Si el nombre existe, prueba con sufijos (1), (2), etc.

    A diferencia de get_unique_filename, la comprobación y la creación son una
    sola operación atómica (O_EXCL): dos descargas simultáneas con el mismo
    nombre nunca reciben la misma ruta.

    Con `keep_open` devuelve también el descriptor, abierto para escribir: lo
    que se escriba en el fichero tiene que ir por él (ver move_to_unique_path).
    """
    base_name, extension = os.path.splitext(filename)
    counter = 0
    while True:
        if counter == 0:
            name = filename
        else:
            # Con el sufijo, un nombre que ya rozaba los 255 bytes no cabría
            suffix = f" ({counter}){extension}"
            base = _truncate_to_bytes(base_name, MAX_FILENAME_BYTES - len(suffix.encode('utf-8')))
            if not base:
                # Lo que sigue al último punto no era una extensión ("1. Introducción…"
                # sin más puntos) y no deja sitio para la base: con ella detrás
                # del sufijo pasaba de 255 bytes y el nombre no se podía crear.
                # El sufijo va al final del nombre entero
                suffix = f" ({counter})"
                base = _truncate_to_bytes(filename, MAX_FILENAME_BYTES - len(suffix.encode('utf-8')))
            name = base + suffix
        path = os.path.join(directory, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            counter += 1
            continue
        if keep_open:
            return path, fd
        os.close(fd)
        return path


def copy_and_remove(src, dst):
    """
    Mueve `src` a `dst` (la ruta que reservó reserve_unique_path) renombrándolo.
    Solo vale dentro del mismo sistema de ficheros, como el temporal de
    download_media, que se descarga en la propia carpeta; para llevar un
    fichero a otra, move_to_unique_path. Si algo falla, borra el destino.
    """
    try:
        # Lo que se mueve es un fichero descargado: nunca un enlace ni un
        # dispositivo como /dev/zero
        if not stat.S_ISREG(os.lstat(src).st_mode):
            raise shutil.SpecialFileError(f"Not a regular file: {src}")
        os.replace(src, dst)
    except BaseException:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise


def move_to_unique_path(src, directory, filename):
    """
    Mueve `src` a `directory` con un nombre libre (ver reserve_unique_path) y
    devuelve la ruta final. El nombre se reserva lo último: el contenido va
    antes a un oculto de esa carpeta y solo después se reserva el nombre y se
    renombra. Reservado antes de copiar, una parada a mitad dejaba un fichero
    vacío con el nombre final. En el mismo sistema de ficheros basta con
    renombrar; entre dispositivos copia solo el contenido (sin permisos ni
    metadatos, que fallan). Si algo falla, no deja nada en la carpeta. Es
    bloqueante: llamarla con asyncio.to_thread.
    """
    # Lo que se mueve es un fichero descargado: nunca un enlace ni un
    # dispositivo como /dev/zero
    if not stat.S_ISREG(os.lstat(src).st_mode):
        raise shutil.SpecialFileError(f"Not a regular file: {src}")
    part, part_fd = reserve_unique_path(directory, partial_name(f"move{PARTIAL_SUFFIX}"), keep_open=True)
    final_path = None
    copied = False
    try:
        with open(part_fd, "wb") as written:
            try:
                os.replace(src, part)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                # Por el descriptor de la reserva, sin volver a abrir su nombre:
                # la carpeta de descargas puede ser compartida, y entretanto
                # otro podía cambiarlo por un enlace a cualquier fichero, donde
                # el bot (que va como root) escribía la copia
                with open(src, "rb") as source:
                    shutil.copyfileobj(source, written, COPY_CHUNK_BYTES)
                written.flush()
                # En disco antes de darle el nombre final
                os.fsync(written.fileno())
                # Lo que se renombra tiene que ser la copia, no algo puesto en su lugar
                if not os.path.samestat(os.lstat(part), os.fstat(written.fileno())):
                    raise FileNotFoundError(f"Partial copy was replaced: {part}")
                copied = True
        final_path = reserve_unique_path(directory, filename)
        os.replace(part, final_path)
    except BaseException:
        for leftover in (part, final_path):
            if leftover is not None:
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        raise
    if copied:
        # Al cancelar, remove_temp_files puede haberlo borrado ya mientras se
        # copiaba: la copia está entera y ya tiene su nombre
        try:
            os.remove(src)
        except FileNotFoundError:
            pass
    return final_path


def remove_partial_files(directory):
    """Borra de `directory` lo que una parada de este bot dejó a medio escribir (ver PARTIAL_SUFFIX)."""
    for path in glob.glob(os.path.join(glob.escape(directory), glob.escape(partial_name("")) + f"*{PARTIAL_SUFFIX}")):
        try:
            if os.path.isfile(path):
                os.remove(path)
                debug(f"[CLEANUP] Partial file removed: {path}")
        except OSError as e:
            warning(f"[CLEANUP] Could not remove partial file {path}: {e}")


def remove_partial_extractions(directory):
    """Borra de `directory` las extracciones que una parada de este bot dejó a medias (ver EXTRACTING_SUFFIX).

    Antes solo se borraban al volver a descomprimir ese mismo fichero: si no se
    repetía, la carpeta oculta se quedaba ocupando sitio sin que nadie la viera.
    """
    for path in glob.glob(os.path.join(glob.escape(directory), glob.escape(partial_name("")) + f"*{EXTRACTING_SUFFIX}")):
        try:
            # Solo carpetas de verdad: un enlace o un fichero con ese nombre no
            # los ha creado el bot
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
                debug(f"[CLEANUP] Unfinished extraction removed: {path}")
        except OSError as e:
            warning(f"[CLEANUP] Could not remove unfinished extraction {path}: {e}")


def get_file_icon(file_extension):
    """Determina el icono según la extensión del archivo usando las constantes de config.py"""
    if file_extension in EXTENSIONS_TORRENT:
        return TOR_ICO
    elif file_extension in EXTENSIONS_EBOOK:
        return BOO_ICO
    elif file_extension in EXTENSIONS_VIDEO:
        return VID_ICO
    elif file_extension in EXTENSIONS_AUDIO:
        return AUD_ICO
    elif file_extension in EXTENSIONS_IMAGE:
        return IMG_ICO
    elif file_extension in EXTENSIONS_COMPRESSED:
        return ZIP_ICO
    else:
        return DEF_ICO
