import os
import re

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_ADMIN = os.environ.get("TELEGRAM_ADMIN")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH")
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID")
LANGUAGE = os.environ.get("LANGUAGE")
DEFAULT_DOWNLOAD_PATH = os.environ.get("DEFAULT_DOWNLOAD_PATH")
DEFAULT_DOWNLOAD_AUDIO = os.environ.get("DEFAULT_DOWNLOAD_AUDIO")
DEFAULT_DOWNLOAD_VIDEO = os.environ.get("DEFAULT_DOWNLOAD_VIDEO")
DEFAULT_DOWNLOAD_PHOTO = os.environ.get("DEFAULT_DOWNLOAD_PHOTO")
DEFAULT_DOWNLOAD_DOCUMENT = os.environ.get("DEFAULT_DOWNLOAD_DOCUMENT")
DEFAULT_DOWNLOAD_TORRENT = os.environ.get("DEFAULT_DOWNLOAD_TORRENT")

# Constants
ANONYMOUS_USER_ID = "1087968824"
DEFAULT_EMPTY_STR = "abc"
URL_PATTERN = re.compile(r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+")
EXTENSIONS_VIDEO = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpeg", ".3gp"}
EXTENSIONS_AUDIO = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"}
EXTENSIONS_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".svg", ".ico"}
EXTENSIONS_DOCUMENT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt", ".csv"}
EXTENSIONS_TORRENT = {".torrent"}
IMG_ICO = "🌅"
VID_ICO = "📽️"
AUD_ICO = "🎶"
DOC_ICO = "📄"
TOR_ICO = "🧲"
DEF_ICO = "📥"