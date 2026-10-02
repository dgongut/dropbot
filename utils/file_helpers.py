"""
Funciones de utilidad para manejo de archivos.
"""
import os
import shutil
from config import (
    EXTENSIONS_TORRENT, EXTENSIONS_EBOOK, EXTENSIONS_VIDEO,
    EXTENSIONS_AUDIO, EXTENSIONS_IMAGE, EXTENSIONS_COMPRESSED,
    TOR_ICO, BOO_ICO, VID_ICO, AUD_ICO, IMG_ICO, ZIP_ICO, DEF_ICO
)
from logger import warning


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


def reserve_unique_path(directory, filename):
    """
    Reserva un nombre libre en `directory` creando el fichero vacío y devuelve
    su ruta. Si el nombre existe, prueba con sufijos (1), (2), etc.

    A diferencia de get_unique_filename, la comprobación y la creación son una
    sola operación atómica (O_EXCL): dos descargas simultáneas con el mismo
    nombre nunca reciben la misma ruta.
    """
    base_name, extension = os.path.splitext(filename)
    counter = 0
    while True:
        name = filename if counter == 0 else f"{base_name} ({counter}){extension}"
        path = os.path.join(directory, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            counter += 1
            continue
        os.close(fd)
        return path


def copy_and_remove(src, dst):
    """
    Mueve `src` a `dst` copiando solo el contenido (sin permisos ni metadatos,
    que fallan entre dispositivos) y borra el origen. Si la copia falla, borra
    el destino a medias. Es bloqueante: llamarla con asyncio.to_thread.
    """
    try:
        shutil.copyfile(src, dst)
    except BaseException:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise
    os.remove(src)


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
