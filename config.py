import os
import re

# Constants
ANONYMOUS_USER_ID = "1087968824"
DEFAULT_EMPTY_STR = "abc"
DONORS_URL = "https://donate.dgongut.com/donors.json"
MAX_DOWNLOAD_RETRIES = 3
RETRY_DELAY_SECONDS = 5
URL_PATTERN = re.compile(r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+")
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
EXTENSIONS_COMPRESSED = ['.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz', '.rar']
IMG_ICO = "🌅"
VID_ICO = "📽️"
AUD_ICO = "🎶"
TOR_ICO = "🧲"
BOO_ICO = "📚"
DEF_ICO = "📥"
DOWNLOAD_PATH = "/downloads"
DOWNLOAD_AUDIO = "/audio"
DOWNLOAD_VIDEO = "/video"
DOWNLOAD_PHOTO = "/photo"
DOWNLOAD_TORRENT = "/torrent"
DOWNLOAD_EBOOK = "/ebook"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", DEFAULT_EMPTY_STR)
TELEGRAM_ADMIN = os.environ.get("TELEGRAM_ADMIN", DEFAULT_EMPTY_STR)
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", DEFAULT_EMPTY_STR)
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", DEFAULT_EMPTY_STR)
LANGUAGE = os.environ.get("LANGUAGE", "ES")
FILTER_PHOTO = bool(int(os.environ.get("FILTER_PHOTO", 0)))
FILTER_AUDIO = bool(int(os.environ.get("FILTER_AUDIO", 0)))
FILTER_VIDEO = bool(int(os.environ.get("FILTER_VIDEO", 0)))
FILTER_TORRENT = bool(int(os.environ.get("FILTER_TORRENT", 0)))
FILTER_EBOOK = bool(int(os.environ.get("FILTER_EBOOK", 0)))
PARALLEL_DOWNLOADS = int(os.environ.get("PARALLEL_DOWNLOADS", 2))

# Rutas y filtros para descargas desde URLs (YouTube, Instagram, TikTok, etc.)
DOWNLOAD_URL_VIDEO = os.environ.get("DOWNLOAD_URL_VIDEO", "/url_video")
DOWNLOAD_URL_AUDIO = os.environ.get("DOWNLOAD_URL_AUDIO", "/url_audio")
FILTER_URL_VIDEO = bool(int(os.environ.get("FILTER_URL_VIDEO", 0)))
FILTER_URL_AUDIO = bool(int(os.environ.get("FILTER_URL_AUDIO", 0)))

# Descarga automática de URLs sin preguntar
# Valores posibles: "ASK" (preguntar), "VIDEO" (descargar video automáticamente), "AUDIO" (descargar audio automáticamente)
AUTO_DOWNLOAD_FORMAT = os.environ.get("AUTO_DOWNLOAD_FORMAT", "ASK").upper()

# Configuración interna de la cola de mensajes para evitar FloodWaitError
# Valores conservadores para evitar problemas con la API de Telegram
MESSAGE_QUEUE_DELAY = 0.5  # Delay entre mensajes en segundos
MESSAGE_QUEUE_MAX_RETRIES = 5  # Número máximo de reintentos