import os
import re

from utils.mounts import is_mounted

# Constants
ANONYMOUS_USER_ID = "1087968824"
DEFAULT_EMPTY_STR = "abc"
DONORS_URL = "https://donate.dgongut.com/donors.json"
MAX_DOWNLOAD_RETRIES = 3
RETRY_DELAY_SECONDS = 5
URL_PATTERN = re.compile(r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*(),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+")
EXTENSIONS_VIDEO = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpeg", ".3gp", ".mts",
    ".m2ts", ".ts", ".divx", ".vob", ".m4v", ".f4v", ".rm", ".rmvb"
}
EXTENSIONS_AUDIO = {
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus",
    ".mid", ".midi", ".aiff", ".amr", ".mp2", ".ra", ".ac3"
}
EXTENSIONS_IMAGE = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".svg", ".ico", 
    ".tga", ".dds", ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw", ".orf", ".rw2",
    ".emf", ".wmf"
}
EXTENSIONS_TORRENT = {".torrent"}
EXTENSIONS_EBOOK = {
    ".azw", ".azw3", ".azw4", ".cbz", ".cbr", ".cb7", ".cbt", ".cba", ".cbdf",
    ".djvu", ".docx", ".epub", ".fb2", ".htm", ".html", ".ibooks", ".lit",
    ".md", ".mobi", ".nfo", ".odt", ".opf", ".pdf", ".pdb", ".prc",
    ".ps", ".rtf", ".txt", ".xps"
}
# Páginas web: un .html que llega por Telegram se guarda como ebook, pero un
# enlace que acaba en .html es una página (una noticia con vídeo, p. ej.) y lo
# tiene que analizar yt-dlp en vez de bajarse como fichero
EXTENSIONS_WEBPAGE = {".htm", ".html"}
EXTENSIONS_COMPRESSED = ['.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz', '.rar']
IMG_ICO = "🌅"
VID_ICO = "📽️"
AUD_ICO = "🎶"
TOR_ICO = "🧲"
BOO_ICO = "📚"
ZIP_ICO = "📦"
DEF_ICO = "📥"
DOWNLOAD_PATH = "/downloads"
DOWNLOAD_AUDIO = "/audio"
DOWNLOAD_VIDEO = "/video"
DOWNLOAD_PHOTO = "/photo"
DOWNLOAD_TORRENT = "/torrent"
DOWNLOAD_EBOOK = "/ebook"

# Carpeta temporal para archivos de conversión, descargas, thumbnails, etc.
TEMP_DIR = "/tmp/dropbot_conversions"

# Fichero de heartbeat para healthcheck de Docker (mtime actualizado periódicamente)
HEARTBEAT_FILE = "/tmp/dropbot_heartbeat"
HEARTBEAT_INTERVAL = 15  # segundos entre escrituras

# Proveedor de PO Token (bgutil) que yt-dlp necesita para descargar de YouTube
# sin recibir HTTP 403. Se arranca como servidor HTTP local dentro del contenedor
POT_PROVIDER_DIR = "/opt/bgutil-ytdlp-pot-provider/server"
POT_PROVIDER_PORT = 4416
POT_PROVIDER_STARTUP_TIMEOUT = 30  # segundos máximos de espera a que responda

# VARIABLES DE ENTORNO
#
# Solo lo que el bot necesita antes de poder leer sus propios ajustes: cómo
# llegar a Telegram y quién puede hablarle. Todo lo demás es un ajuste, que se
# cambia desde /settings (ver settings.py). La línea divisoria es que un valor
# equivocado en cualquiera de estas te deja sin acceso al bot, y lo que impide
# que el chat funcione no se puede arreglar desde el chat.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", DEFAULT_EMPTY_STR)
TELEGRAM_ADMIN = os.environ.get("TELEGRAM_ADMIN", DEFAULT_EMPTY_STR)
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", DEFAULT_EMPTY_STR)
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", DEFAULT_EMPTY_STR)

# Estadísticas anónimas. Activadas por defecto; se desactivan desde /settings o
# con TELEMETRY=false, que lee el propio telemetry.py: es la misma variable en
# todos los proyectos que comparten ese cliente.
# Solo para desarrollo, sin documentar: envía un minuto después de cada
# arranque en vez de una vez al día, y a un servidor en la misma máquina salvo
# que se diga otra cosa, nunca al real, donde contaría como una instalación.
TELEMETRY_DEBUG = os.environ.get("TELEMETRY_DEBUG", "0").strip().lower() in ("1", "true", "yes")
TELEMETRY_ENDPOINT = os.environ.get(
    "TELEMETRY_ENDPOINT",
    "http://host.docker.internal:8000/v1/ping" if TELEMETRY_DEBUG else "https://telemetry.dgongut.com/v1/ping")

# Transferencia paralela (estilo FastTelethon): tamaño mínimo (bytes) para
# activarla. Por debajo de este umbral el coste de abrir varias conexiones no
# compensa. El número de conexiones es un ajuste (downloads.fast_connections).
FAST_TRANSFER_MIN_BYTES = 10 * 1024 * 1024  # 10 MB

# Carpetas separadas para lo descargado desde URLs (YouTube, Instagram...)
DOWNLOAD_URL_VIDEO = "/url_video"
DOWNLOAD_URL_AUDIO = "/url_audio"

# Cookies opcionales para sitios que requieren una sesión autenticada
YTDLP_COOKIES_FILE = "/app/cookies/cookies.txt"

# Hasta la 3.x cada carpeta se activaba con su variable además de montarla.
# Desde la 4.0.0 basta con montarla; si siguen en el compose solo se avisa.
DEPRECATED_FOLDER_FILTERS = (
    "FILTER_PHOTO", "FILTER_AUDIO", "FILTER_VIDEO", "FILTER_TORRENT",
    "FILTER_EBOOK", "FILTER_URL_VIDEO", "FILTER_URL_AUDIO",
)

# Tamaño máximo que Telegram acepta en una subida (2 GiB)
MAX_TELEGRAM_FILE_SIZE = 2 * 1024 * 1024 * 1024

# Configuración interna de la cola de mensajes para evitar FloodWaitError
# Valores conservadores para evitar problemas con la API de Telegram
MESSAGE_QUEUE_DELAY = 0.5  # Delay entre mensajes en segundos
MESSAGE_QUEUE_MAX_RETRIES = 5  # Número máximo de reintentos

def resolve_download_paths(mounted=is_mounted):
    """La carpeta de destino de cada tipo de contenido.

    Un tipo tiene carpeta propia cuando esa carpeta está montada como volumen:
    montar /video en el docker-compose es lo que dice que los vídeos van
    aparte. Si no está montada, va a /downloads, que es lo único que se monta
    siempre. Escribir en una carpeta sin volumen sería escribir dentro del
    contenedor, y perderlo en la siguiente actualización.

    Lo descargado desde URLs va a /url_video o /url_audio si están montadas, y
    si no, donde vaya el resto del vídeo o del audio.
    """
    def own(path):
        return path if mounted(path) else None

    video = own(DOWNLOAD_VIDEO) or DOWNLOAD_PATH
    audio = own(DOWNLOAD_AUDIO) or DOWNLOAD_PATH
    return {
        "audio": audio,
        "video": video,
        "photo": own(DOWNLOAD_PHOTO) or DOWNLOAD_PATH,
        "torrent": own(DOWNLOAD_TORRENT) or DOWNLOAD_PATH,
        "ebook": own(DOWNLOAD_EBOOK) or DOWNLOAD_PATH,
        "url_video": own(DOWNLOAD_URL_VIDEO) or video,
        "url_audio": own(DOWNLOAD_URL_AUDIO) or audio,
    }


DOWNLOAD_PATHS = resolve_download_paths()


def has_own_folder(*kinds):
    """True si alguno de esos tipos no va a la carpeta general."""
    return any(DOWNLOAD_PATHS[kind] != DOWNLOAD_PATH for kind in kinds)
