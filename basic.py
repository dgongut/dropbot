from config import TELEGRAM_ADMIN, EXTENSIONS_COMPRESSED
import re
import unicodedata
import os
from pathlib import Path

def is_admin(id):
    admins = TELEGRAM_ADMIN.split(',')
    return str(id) in admins

# Caracteres que no pueden aparecer en un nombre de fichero: separadores de
# ruta, los reservados por Windows/SMB y los de control
FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')

# Los sistemas de ficheros limitan el nombre en bytes, no en caracteres
MAX_FILENAME_BYTES = 255


def _truncate_to_bytes(text, max_bytes):
    """Recorta `text` para que quepa en `max_bytes` codificado en UTF-8.

    El recorte se hace sobre los bytes porque es lo que limita el sistema de
    ficheros: un nombre en japonés ocupa hasta 3 bytes por carácter. El
    `errors="ignore"` del decode descarta el carácter que quede partido.
    """
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode('utf-8', 'ignore')


def sanitize_filename(filename):
    """Deja un nombre de fichero seguro conservando el nombre original.

    Solo se quitan los caracteres que no puede haber en un nombre. Los acentos
    y los alfabetos no latinos se conservan: los sistemas de ficheros y Telegram
    manejan UTF-8, y romperlos convertía "Canción.mp3" en "Cancion.mp3" y un
    título en japonés entero en "archivo.mp3".
    """
    # Se sanea la cadena completa antes de separar la extensión: os.path.splitext
    # puede dejar separadores dentro de `ext`, que así se quedaban sin sanear
    filename = FORBIDDEN_FILENAME_CHARS.sub('_', filename)
    # NFC deja una forma canónica única para los acentos (macOS usa NFD)
    filename = unicodedata.normalize('NFC', filename)

    base, ext = os.path.splitext(filename)
    base = re.sub(r'_+', '_', base)
    base = base.strip('_') or 'archivo'
    base = _truncate_to_bytes(base, MAX_FILENAME_BYTES - len(ext.encode('utf-8')))
    return (base or 'archivo') + ext

def clean_youtube_link(url):
    if 'youtu.be/' in url:
        video_id = url.split('youtu.be/')[1].split('?')[0]
        return f'https://www.youtube.com/watch?v={video_id}'

    elif 'youtube.com/watch' in url:
        parts = url.split('?')
        if len(parts) > 1:
            for part in parts[1].split('&'):
                if part.startswith('v='):
                    video_id = part.split('=')[1]
                    return f'https://www.youtube.com/watch?v={video_id}'

    elif 'youtube.com/shorts/' in url:
        video_id = url.split('youtube.com/shorts/')[1].split('?')[0]
        return f'https://www.youtube.com/watch?v={video_id}'

    return url

def is_compressed_file(file_path):
    lower = file_path.lower()
    
    if any(lower.endswith(ext) for ext in EXTENSIONS_COMPRESSED):
        return True
    
    if re.search(r'\.z\d{2,}$', lower):
        return True
    if re.search(r'\.r\d{2,}$', lower):
        return True
    if re.search(r'\.part\d+\.rar$', lower):
        return True

    return False

def get_filename_from_path(path):
    return Path(path).name

def is_split_zip(file_name):
    lower = file_name.lower()
    base = None
    if re.match(r'.*\.z\d{2,}$', lower):
        base = re.sub(r'\.z\d{2,}$', '', file_name)
    elif lower.endswith('.zip'):
        base = file_name[:-4]
    else:
        return False
    for i in range(1, 100):
        part = f"{base}.z{str(i).zfill(2)}"
        if os.path.exists(part):
            return True
    return False

def clean_rar_base_name(filename):
    name = filename.lower()
    name = os.path.splitext(name)[0]
    name = re.sub(r'(\.part\d+|\.r\d+)$', '', name)
    return name