"""
Servicio de descompresión de archivos (ZIP, TAR, RAR).

`extract_file()` solo devuelve True si lo extraído está completo. Si no, quita
lo que haya dejado a medias y devuelve el motivo: "missing_parts" (faltan
volúmenes de un RAR), "corrupted", "password" (cifrado: el bot no tiene forma
de pedir la contraseña), "no_space" o False (cualquier otro error).
"""
import errno
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
import tarfile
import rarfile

from logger import debug, warning, error

# Lo que se deja libre en el disco al descomprimir: un comprimido que cabe por
# poco no debe dejar sin sitio a las descargas en curso ni a los ajustes
FREE_SPACE_MARGIN = 100 * 1024 * 1024

# Códigos de salida de unrar (rar.txt)
UNRAR_CRC_ERROR = 3
UNRAR_WRITE_ERROR = 5
UNRAR_WRONG_PASSWORD = 11

# Los comodines de unrar, para cambiarlos en los nombres que se le pasan
_UNRAR_WILDCARDS = str.maketrans("?*", "__")


def _safe_member_path(extract_to, member_name):
    """Devuelve la ruta destino si el miembro no puede escapar del destino."""
    destination = os.path.abspath(extract_to)
    target = os.path.abspath(os.path.join(destination, member_name))

    try:
        is_inside = os.path.commonpath((destination, target)) == destination
    except ValueError:
        # Puede ocurrir en Windows si las rutas están en unidades distintas.
        is_inside = False

    if not is_inside:
        raise ValueError(f"Unsafe archive member path: {member_name!r}")
    return target


def _validate_tar_members(tar_ref, extract_to):
    for member in tar_ref.getmembers():
        _safe_member_path(extract_to, member.name)
        if member.issym() or member.islnk():
            raise ValueError(f"Links are not allowed in TAR files: {member.name!r}")


class _TarInfoUntilEnd(tarfile.TarInfo):
    """Apunta en el TarFile si la lectura llegó a la marca de fin del TAR.

    tarfile da por acabado el archivo, sin ningún error, también cuando se
    encuentra una cabecera vacía, cortada o dañada después del primer miembro.
    Un .tar cortado justo ahí se extraía a medias y se daba por bueno.
    """

    @classmethod
    def fromtarfile(cls, tar_ref):
        try:
            return super().fromtarfile(tar_ref)
        except tarfile.EOFHeaderError:
            tar_ref.reached_end_marker = True
            raise


def _fits_on_disk(extract_to, size):
    """Si caben `size` bytes en el disco de `extract_to`, dejando el margen."""
    path = os.path.abspath(extract_to)
    # La carpeta de destino puede no existir todavía: vale la de encima
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return size + FREE_SPACE_MARGIN <= shutil.disk_usage(path).free


def _remove_empty_folder(extract_to):
    """Quita `extract_to` si se ha quedado vacía porque no se llegó a extraer nada.

    Es parte del contrato de extract_file: si falla, no deja nada a medias. Con
    rmdir no se borra una carpeta que ya tuviera contenido, por si quien llama
    pasa una existente. En el bot, `extract_to` es la carpeta oculta de
    trabajo, que el handler borra además con rmtree.
    """
    try:
        if os.path.isdir(extract_to) and not os.listdir(extract_to):
            os.rmdir(extract_to)
            debug(f"[EXTRACT] Deleted empty folder: {extract_to}")
    except OSError as cleanup_error:
        warning(f"[EXTRACT] Error deleting empty folder {extract_to}: {cleanup_error}")


def _remove_partial_extraction(extract_to):
    """Borra lo que haya dejado una extracción que no ha terminado bien."""
    if os.path.exists(extract_to):
        try:
            shutil.rmtree(extract_to)
            debug(f"[EXTRACT] Deleted folder: {extract_to}")
        except Exception as cleanup_error:
            warning(f"[EXTRACT] Error deleting folder {extract_to}: {cleanup_error}")


def _no_space(file_path, extract_to, size):
    warning(f"[EXTRACT] File {file_path} - Not enough disk space to extract {size} bytes")
    _remove_empty_folder(extract_to)
    return "no_space"


def _unknown_size(info):
    """Si un miembro de un RAR no dice cuánto ocupa descomprimido.

    unrar entonces no hace caso del tamaño de la cabecera y descomprime hasta
    el final de los datos: un RAR de pocos KB que declaraba 0 bytes pasaba la
    comprobación de espacio y llenaba el disco. En RAR 5 lo dice una marca; en
    los anteriores, un tamaño de 0xFFFFFFFF sin la parte alta
    """
    if isinstance(info, rarfile.Rar5Info):
        return bool(info.file_flags & rarfile.RAR5_FILE_FLAG_UNKNOWN_SIZE)
    return not info.flags & rarfile.RAR_FILE_LARGE and info.file_size == 0xFFFFFFFF


def _clear_special_modes(root):
    """Quita los bits setuid, setgid y sticky de los ficheros extraídos en `root`.

    unrar deja a cada fichero los permisos que trae el RAR, también esos: con
    el bot como root, un RAR ajeno dejaba en la carpeta del host un ejecutable
    setuid de root. rarfile, con el que se extraía antes, y el filtro "data" de
    los TAR también los quitan. Las carpetas no se tocan: en una carpeta esos
    bits no dan permisos a nadie, y el setgid lo hereda de la de encima (una
    carpeta de descargas compartida) para que lo de dentro sea de su grupo
    """
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            mode = os.lstat(path).st_mode
            # Un enlace ya se ha rechazado antes, pero chmod lo seguiría
            if stat.S_ISREG(mode) and stat.S_IMODE(mode) & ~0o777:
                os.chmod(path, stat.S_IMODE(mode) & 0o777)


def _extract_rar(file_path, extract_to):
    """Extrae un RAR con una sola llamada a unrar y dice cómo ha ido.

    rarfile solo se usa para leer el índice. Su extractall saca los miembros
    de uno en uno con "unrar p archivo nombre": en un RAR sólido cada llamada
    vuelve a descomprimir todo lo anterior (horas con miles de ficheros), y
    unrar toma ese nombre por una lista si empieza por "@" o por una máscara si
    lleva "?" o "*". "unrar x" del archivo entero no recibe ningún nombre, y
    comprueba el CRC de cada fichero: si algo falla, lo dice su código de salida.
    """
    try:
        with rarfile.RarFile(file_path) as rar_ref:
            # Si falta un volumen o una parte está cortada, rarfile no lanza
            # nada: lo apunta aquí y se queda con lo que ha podido leer
            problem = rar_ref.strerror()
            needs_password = rar_ref.needs_password()
            members = rar_ref.infolist()
            volumes = rar_ref.volumelist()
    except rarfile.NeedFirstVolume:
        warning(f"[EXTRACT] File {file_path} - Missing RAR parts: it is not the first volume")
        _remove_empty_folder(extract_to)
        return "missing_parts"

    debug(f"[EXTRACT] RAR contains {len(members)} files")
    if problem:
        _remove_empty_folder(extract_to)
        if problem.startswith(("Cannot open next volume", "Invalid volume sig")):
            warning(f"[EXTRACT] File {file_path} - Missing RAR parts: {problem}")
            return "missing_parts"
        error(f"[EXTRACT] File {file_path} - Corrupted or incomplete RAR file: {problem}")
        return "corrupted"
    # Con las cabeceras cifradas el índice sale vacío, y extraer "nada" se
    # daba por bueno
    if needs_password:
        warning(f"[EXTRACT] File {file_path} - RAR file is password protected")
        _remove_empty_folder(extract_to)
        return "password"
    for info in members:
        _safe_member_path(extract_to, info.filename)
        # Como en los TAR, ni enlaces simbólicos ni duros. Las copias de un
        # fichero idéntico ("rar -oi") sí, que salen como un fichero más
        hard_link = info.file_redir and info.file_redir[0] == rarfile.RAR5_XREDIR_HARD_LINK
        if info.is_symlink() or hard_link:
            raise ValueError(f"Links are not allowed in RAR files: {info.filename!r}")
        # Sin su tamaño no hay forma de saber si cabe en el disco
        if info.is_file() and _unknown_size(info):
            raise ValueError(f"RAR member without a declared size: {info.filename!r}")
    size = sum(info.file_size for info in members if info.is_file())
    if not _fits_on_disk(extract_to, size):
        return _no_space(file_path, extract_to, size)

    # unrar también toma por una máscara el nombre del archivo: con "?" o "*"
    # en la ruta abriría además otros RAR que encajen. Entonces se le pasa a
    # través de enlaces sin esos caracteres, uno por volumen para que siga
    # encontrando las partes
    archive = file_path
    links = None
    if any(char in os.path.abspath(file_path) for char in "?*"):
        links = tempfile.mkdtemp(prefix="dropbot-unrar-")
    try:
        if links:
            for volume in volumes:
                os.symlink(os.path.abspath(volume),
                           os.path.join(links, os.path.basename(volume).translate(_UNRAR_WILDCARDS)))
            archive = os.path.join(links, os.path.basename(file_path).translate(_UNRAR_WILDCARDS))
        # Sin consola: con stdin cerrado y -y, unrar no se queda esperando a
        # que alguien conteste
        result = subprocess.run(
            [rarfile.UNRAR_TOOL, "x", "-idq", "-cfg-", "-o-", "-p-", "-y", "--",
             archive, os.path.join(extract_to, "")],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
        )
    except OSError as e:
        error(f"[EXTRACT] RAR extraction tool could not be run: {e}")
        _remove_empty_folder(extract_to)
        return False
    finally:
        if links:
            shutil.rmtree(links, ignore_errors=True)

    if result.returncode == 0:
        _clear_special_modes(extract_to)
        return True

    output = (result.stdout + result.stderr).strip()
    _remove_partial_extraction(extract_to)
    if "cannot find volume" in output.lower():
        warning(f"[EXTRACT] File {file_path} - Missing RAR parts: {output}")
        return "missing_parts"
    if result.returncode == UNRAR_WRONG_PASSWORD:
        warning(f"[EXTRACT] File {file_path} - RAR file is password protected: {output}")
        return "password"
    if result.returncode == UNRAR_CRC_ERROR:
        error(f"[EXTRACT] File {file_path} - Corrupted or incomplete RAR file: {output}")
        return "corrupted"
    # unrar no dice bien por qué no pudo escribir (con el disco lleno puede
    # contar "Inappropriate ioctl for device"), pero casi siempre es el sitio
    if result.returncode == UNRAR_WRITE_ERROR:
        warning(f"[EXTRACT] File {file_path} - Write error, probably no space left: {output}")
        return "no_space"
    error(f"[EXTRACT] File {file_path} - {rarfile.UNRAR_TOOL} exited with code {result.returncode}: {output}")
    return False


def extract_file(file_path, extract_to):
    try:
        filename = os.path.basename(file_path)
        if file_path.lower().endswith('.zip'):
            debug(f"[EXTRACT] Extracting ZIP file: {filename}")
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                file_count = len(zip_ref.namelist())
                debug(f"[EXTRACT] ZIP contains {file_count} files")
                # Lo que ocupará descomprimido. zipfile no escribe de un
                # miembro más de lo que declara, así que es un techo fiable
                size = sum(info.file_size for info in zip_ref.infolist())
                if not _fits_on_disk(extract_to, size):
                    return _no_space(file_path, extract_to, size)
                zip_ref.extractall(extract_to)
            debug(f"[EXTRACT] ZIP extraction completed: {filename}")
        elif any(file_path.lower().endswith(ext) for ext in ['.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz']):
            debug(f"[EXTRACT] Extracting TAR file: {filename}")
            with tarfile.open(file_path, 'r:*', tarinfo=_TarInfoUntilEnd) as tar_ref:
                file_count = len(tar_ref.getmembers())
                debug(f"[EXTRACT] TAR contains {file_count} files")
                if not getattr(tar_ref, "reached_end_marker", False):
                    error(f"[EXTRACT] File {file_path} - TAR file ends before its end marker: truncated or damaged")
                    _remove_empty_folder(extract_to)
                    return "corrupted"
                _validate_tar_members(tar_ref, extract_to)
                size = sum(member.size for member in tar_ref.getmembers() if member.isreg())
                if not _fits_on_disk(extract_to, size):
                    return _no_space(file_path, extract_to, size)
                # `filter="data"` añade protección contra archivos
                # especiales en las versiones de Python que lo soportan.
                try:
                    tar_ref.extractall(extract_to, filter="data")
                except TypeError:
                    tar_ref.extractall(extract_to)
            debug(f"[EXTRACT] TAR extraction completed: {filename}")
        elif rarfile.is_rarfile(file_path):
            debug(f"[EXTRACT] Extracting RAR file: {filename}")
            result = _extract_rar(file_path, extract_to)
            if result is not True:
                return result
            debug(f"[EXTRACT] RAR extraction completed: {filename}")
        else:
            # Formato que no reconocemos (un .z01, un .rar falso...): no se ha
            # extraído nada, así que solo se quita la carpeta si está vacía
            warning(f"[EXTRACT] Unsupported compressed format: {filename}")
            _remove_empty_folder(extract_to)
            return False
        return True

    except Exception as e:
        error(f"[EXTRACT] Error extracting file {file_path}: {e}")
        _remove_partial_extraction(extract_to)
        # Quedarse sin sitio no es un archivo corrupto: que el mensaje lo diga
        if isinstance(e, OSError) and e.errno == errno.ENOSPC:
            return "no_space"
        return False
