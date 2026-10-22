from config import ANONYMOUS_USER_ID, CHANNEL_USER_ID, TELEGRAM_ADMIN, EXTENSIONS_COMPRESSED
import re
import unicodedata
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

def _admin_entries():
    """Lo que hay en TELEGRAM_ADMIN, entrada a entrada.

    Sin strip, con TELEGRAM_ADMIN="111, 222" el segundo admin era " 222" y se
    quedaba fuera del bot. Una coma al final deja una entrada vacía, que no es
    nadie: se descarta.
    """
    return [admin.strip() for admin in TELEGRAM_ADMIN.split(',') if admin.strip()]


def _is_user_id(admin):
    return re.fullmatch(r"[0-9]+", admin) is not None and admin not in (ANONYMOUS_USER_ID, CHANNEL_USER_ID)


def admin_ids():
    """Los ids de TELEGRAM_ADMIN que son de una persona, tal y como se comparan.

    Lo demás (ver wrong_admin_ids) no cuenta como admin: el bot arranca con
    estos y avisa de lo ignorado en el log y en el mensaje de arranque.
    """
    return [admin for admin in _admin_entries() if _is_user_id(admin)]


def is_admin(id):
    return str(id) in admin_ids()


def wrong_admin_ids():
    """Lo que hay en TELEGRAM_ADMIN que no es el id de una persona, y se ignora.

    Un id negativo es un grupo o un canal: con él pasaba por admin cualquiera
    que escribiera en nombre de ese chat (un admin anónimo del grupo, el propio
    canal) y podía mandar ficheros y enlaces al servidor. Los usuarios que pone
    la Bot API para los anónimos y los canales tampoco son nadie.
    """
    return [admin for admin in _admin_entries() if not _is_user_id(admin)]


def md_code(text):
    """Pone `text` como código para el parse_mode markdown de Telethon.

    Ese markdown no tiene escapes: una comilla invertida dentro de `...` cierra
    el bloque y el usuario ve el nombre mutilado. Lo que no lleva comillas va
    en línea como siempre; lo que sí, en un bloque ```...```, donde una comilla
    suelta no cierra nada (Telegram lo pinta como bloque aparte, pero entero).
    """
    text = str(text)
    if '`' not in text:
        return f'`{text}`'
    # Dentro del bloque solo cierran tres comillas seguidas, y una comilla al
    # final se juntaría con las del cierre: un espacio de ancho cero las separa
    text = re.sub(r'`(?=`)', '`\u200b', text)
    if text.endswith('`'):
        text += '\u200b'
    return f'```{text}```'

# Lo que no se ve en un nombre o cambia cómo se ve el resto: los de control
# (también DEL y los C1), las marcas bidi y los separadores de línea. Con un
# U+202E en medio, "Factura<U+202E>fdp.exe" se ve como "Facturaexe.pdf" y
# esconde el .exe
_INVISIBLE_CHARS = (r'\x00-\x1F\x7F-\x9F'
                    r'\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069\u2028\u2029')
INVISIBLE_FILENAME_CHARS = re.compile(f'[{_INVISIBLE_CHARS}]')

# Caracteres que no pueden aparecer en un nombre de fichero: separadores de
# ruta, los reservados por Windows/SMB y los invisibles de arriba
FORBIDDEN_FILENAME_CHARS = re.compile(rf'[<>:"/\\|?*{_INVISIBLE_CHARS}]')

# Invisibles que solo se quitan de los extremos: dentro del nombre los usan
# algunos alfabetos. ZWJ y ZWNJ no están porque forman emoji compuestos
INVISIBLE_EDGE_CHARS = '\u200b\u2060\ufeff'


def has_invisible_chars(name):
    """Si `name` lleva algo de lo que sanitize_filename quita por no verse.

    Los INVISIBLE_FILENAME_CHARS en cualquier sitio, y los INVISIBLE_EDGE_CHARS
    en un extremo del nombre o justo delante de la extensión. Es para los
    nombres que escribe el admin, como el de renombrar en /manage, que se
    rechazan en lugar de cambiarlos sin avisar.
    """
    base = os.path.splitext(name)[0]
    return (INVISIBLE_FILENAME_CHARS.search(name) is not None
            or name.strip(INVISIBLE_EDGE_CHARS) != name
            or base.rstrip(INVISIBLE_EDGE_CHARS) != base)

# Los sistemas de ficheros limitan el nombre en bytes, no en caracteres
MAX_FILENAME_BYTES = 255


def _truncate_to_bytes(text, max_bytes):
    """Recorta `text` para que quepa en `max_bytes` codificado en UTF-8.

    El recorte se hace sobre los bytes porque es lo que limita el sistema de
    ficheros: un nombre en japonés ocupa hasta 3 bytes por carácter. El
    `errors="ignore"` del decode descarta el carácter que quede partido.
    """
    # Negativo, `encoded[:max_bytes]` quitaría bytes del final y el resultado
    # seguiría sin caber
    max_bytes = max(0, max_bytes)
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
    filename = filename.strip(INVISIBLE_EDGE_CHARS)
    # "." y ".." no tienen caracteres prohibidos pero, unidos a un directorio,
    # apuntan a él mismo o a su padre
    if filename.strip('.') == '':
        filename = 'archivo'

    base, ext = os.path.splitext(filename)
    # Un nombre que empieza por punto queda oculto: /list y /manage no lo
    # muestran y desde el bot ya no se puede enviar ni borrar. En ".mp4" o
    # ".env" splitext no ve extensión, pero lo que sigue al punto lo es
    if not ext and base.startswith('.'):
        base = 'archivo.' + base.lstrip('.')
    base = re.sub(r'_+', '_', base)
    edges = '_' + INVISIBLE_EDGE_CHARS
    # Delante tampoco puede quedar un punto: "_.notas.pdf" daba ".notas.pdf"
    base = base.lstrip('.' + edges).rstrip(edges) or 'archivo'
    truncated = _truncate_to_bytes(base, MAX_FILENAME_BYTES - len(ext.encode('utf-8')))
    if not truncated:
        # Lo que sigue al último punto no era una extensión ("1. Introducción…"
        # sin más puntos) y no deja sitio para la base: con 'archivo' delante
        # pasaba de 255 bytes y el nombre no se podía crear. Se recorta entero
        return _truncate_to_bytes(base + ext, MAX_FILENAME_BYTES)
    return truncated + ext

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

def redact_url(url):
    """`url` tal como puede ir al log, sin lo que puede ser una credencial.

    En Docker el log entero va a `docker logs` y de ahí a Dozzle, Loki o
    Portainer: el `usuario:contraseña@` de un enlace al NAS o la firma de un
    enlace prefirmado quedaban ahí a la vista. Se quitan el usuario y la
    contraseña y se tapan los valores de la query y el fragmento; el esquema,
    el host, la ruta y el nombre de cada parámetro se quedan, que es lo que
    sirve para depurar. Solo para el log: wget y yt-dlp reciben la URL entera.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        # Un host mal formado ("http://[::1/x"): de lo demás no se fía nada
        return url.split('://', 1)[0] + '://***'
    netloc = parts.netloc.rpartition('@')[2]
    query = '&'.join(f"{param.partition('=')[0]}=***" if '=' in param else '***'
                     for param in parts.query.split('&')) if parts.query else ''
    return urlunsplit((parts.scheme, netloc, parts.path, query, '***' if parts.fragment else ''))

def redact_urls(text):
    """`text` con cada URL pasada por redact_url: para las líneas de wget y
    yt-dlp, y los comandos, antes de ir al log."""
    return re.sub(r'https?://\S+', lambda match: redact_url(match.group(0)), str(text))

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
    # Sin pasar a minúsculas: es el nombre de la carpeta en la que se extrae,
    # y "Mi Serie.rar" debe ir a "Mi Serie/" como hace un ZIP
    name = os.path.splitext(filename)[0]
    name = re.sub(r'(\.part\d+|\.r\d+)$', '', name, flags=re.IGNORECASE)
    return name