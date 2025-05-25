import os
import re

# Constants
ANONYMOUS_USER_ID = "1087968824"
DEFAULT_EMPTY_STR = "abc"
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
    ".epub", ".mobi", ".azw", ".azw3", ".azw4", ".pdf", ".docx", ".txt", ".fb2", ".lit",
    ".ibooks", ".pdb", ".prc", ".opf", ".rtf", ".html", ".htm", ".djvu", ".cbz", ".cbr",
    ".cb7", ".xps", ".ps", ".nfo"
}
IMG_ICO = "🌅"
VID_ICO = "📽️"
AUD_ICO = "🎶"
TOR_ICO = "🧲"
BOO_ICO = "📚"
DEF_ICO = "📥"
DOWNLOAD_PATH = "/downloads"
DOWNLOAD_AUDIO = "/audio"
DOWNLOAD_VIDEO = "/video"
DOWNLOAD_YOUTUBE_VIDEO = "/youtube_video"
DOWNLOAD_YOUTUBE_AUDIO = "/youtube_audio"
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
FILTER_YOUTUBE_VIDEO = bool(int(os.environ.get("FILTER_YOUTUBE_VIDEO", 0)))
FILTER_YOUTUBE_AUDIO = bool(int(os.environ.get("FILTER_YOUTUBE_AUDIO", 0)))
PARALLEL_DOWNLOADS = int(os.environ.get("PARALLEL_DOWNLOADS", 2))