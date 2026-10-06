"""Los ajustes del bot, leídos y validados.

Los valores viven en settings.json (ver `store`) y se cambian desde /settings,
sin reiniciar. Este módulo sabe qué admite cada uno: el resto del código pide
`settings.auto_send()` y recibe siempre un valor válido.

Un valor mal escrito a mano en settings.json no tumba el bot: se avisa en el
log una vez y se usa el valor por defecto, que es lo que permite arreglarlo
desde el propio /settings.
"""

import os

import store
from logger import warning

LANGUAGES = ("ES", "EN")
# En el selector cada idioma se nombra en sí mismo, que es lo que busca quien
# busca el suyo
LANGUAGE_NAMES = {"ES": "Español", "EN": "English"}
AUTO_FORMATS = ("ASK", "VIDEO", "AUDIO")
AUTO_SEND_MODES = ("ASK", "SEND", "SEND_DELETE", "STORE")
FFMPEG_HW_MODES = ("NONE", "VAAPI", "NVENC", "QSV")
# Resolución máxima de lo que se baja de un enlace (el lado corto del vídeo)
VIDEO_QUALITIES = ("MAX", "2160", "1440", "1080", "720", "480", "360")
# Calidad del MP3 que se saca de un enlace, y el --audio-quality de yt-dlp que
# le corresponde. STANDARD no pasa nada y deja el de yt-dlp (VBR ~128 kbps),
# que es lo que hacían las versiones anteriores
AUDIO_BITRATES = {"STANDARD": None, "HIGH": "192K", "BEST": "0"}
AUDIO_QUALITIES = tuple(AUDIO_BITRATES)
AUDIO_FORMATS = ("MP3", "M4A")
# Categorías de SponsorBlock que se quitan con cada opción
SPONSORBLOCK_CATEGORIES = {
    "OFF": None,
    "SPONSOR": "sponsor",
    "PROMO": "sponsor,selfpromo,interaction",
    "ALL": "sponsor,selfpromo,interaction,intro,outro,preview,music_offtopic,filler",
}
SPONSORBLOCK_MODES = tuple(SPONSORBLOCK_CATEGORIES)
PLAYLIST_MODES = ("ASK", "FULL", "FIRST")
EXTRACT_AFTER_MODES = ("ASK", "DELETE", "KEEP")
PLAYLIST_LIMIT_CHOICES = (0, 10, 25, 50, 100, 200)
# En MiB, que es como lo cuenta --max-filesize de yt-dlp
MAX_SIZE_CHOICES = (0, 512, 1024, 2048, 5120, 10240)


def size_label(mb):
    """512 -> "512 MB", 2048 -> "2 GB"."""
    return f"{mb // 1024} GB" if mb and mb % 1024 == 0 else f"{mb} MB"
QUALITY_RANGE = (1, 51)
PARALLEL_RANGE = (1, 20)
FAST_CONNECTIONS_RANGE = (1, 20)

# Lo que ofrece /settings para los ajustes numéricos. Botones y no texto
# libre: no hay nada que validar ni que explicar si se escribe mal
PARALLEL_CHOICES = (1, 2, 3, 4, 5, 6, 8, 10)
FAST_CONNECTIONS_CHOICES = (1, 2, 4, 6, 8, 12, 16, 20)
QUALITY_CHOICES = (None, 18, 20, 23, 26, 28, 30, 32, 35)


def _choice(choices):
    def parse(raw):
        value = str(raw).strip().upper()
        if value not in choices:
            raise ValueError(f"not one of {'/'.join(choices)}: {raw!r}")
        return value
    return parse


def _int_range(low, high):
    def parse(raw):
        if isinstance(raw, bool):
            raise ValueError(f"not a number: {raw!r}")
        value = int(str(raw).strip())
        if not low <= value <= high:
            raise ValueError(f"not between {low} and {high}: {raw!r}")
        return value
    return parse


def _optional(parse):
    """Vacío o null equivale a no configurarlo."""
    def wrapper(raw):
        if raw is None or str(raw).strip() == "":
            return None
        return parse(raw)
    return wrapper


def _bool(raw):
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


parse_language = _choice(LANGUAGES)
parse_auto_format = _choice(AUTO_FORMATS)
parse_auto_send = _choice(AUTO_SEND_MODES)
parse_ffmpeg_hw = _choice(FFMPEG_HW_MODES)
parse_video_quality = _choice(VIDEO_QUALITIES)
parse_audio_quality = _choice(AUDIO_QUALITIES)
parse_audio_format = _choice(AUDIO_FORMATS)
parse_sponsorblock = _choice(SPONSORBLOCK_MODES)
parse_playlist_mode = _choice(PLAYLIST_MODES)
parse_extract_after = _choice(EXTRACT_AFTER_MODES)
parse_playlist_limit = _int_range(0, 10000)
parse_max_size = _int_range(0, 1000000)
parse_quality = _optional(_int_range(*QUALITY_RANGE))
parse_parallel = _int_range(*PARALLEL_RANGE)
parse_fast_connections = _int_range(*FAST_CONNECTIONS_RANGE)

# Cada ajuste: su clave en settings.json y cómo se lee
PARSERS = {
    "language": parse_language,
    "downloads.parallel": parse_parallel,
    "downloads.fast_connections": parse_fast_connections,
    "urls.auto_format": parse_auto_format,
    "urls.auto_send": parse_auto_send,
    "urls.video_quality": parse_video_quality,
    "urls.audio_quality": parse_audio_quality,
    "urls.prefer_compatible": _bool,
    "urls.audio_format": parse_audio_format,
    "urls.audio_tags": _bool,
    "urls.sponsorblock": parse_sponsorblock,
    "urls.playlist": parse_playlist_mode,
    "urls.playlist_limit": parse_playlist_limit,
    "urls.max_size_mb": parse_max_size,
    "extract.after": parse_extract_after,
    "video.hw": parse_ffmpeg_hw,
    "video.quality": parse_quality,
    "telemetry": _bool,
}

# Variables que en la 4.0.0 pasaron a ser ajustes, con su clave. En el primer
# arranque siembran settings.json, así que quien actualiza conserva todo lo que
# tenía. A partir de ahí manda settings.json y la variable solo genera un
# aviso: si siguiera ganando, un cambio hecho desde /settings se desharía en
# silencio en el siguiente reinicio.
SETTINGS_FROM_ENV = {
    "LANGUAGE": "language",
    "PARALLEL_DOWNLOADS": "downloads.parallel",
    "FAST_CONNECTIONS": "downloads.fast_connections",
    "AUTO_DOWNLOAD_FORMAT": "urls.auto_format",
    "AUTO_SEND": "urls.auto_send",
    "FFMPEG_HW": "video.hw",
    "FFMPEG_QUALITY": "video.quality",
}

_warned = set()


def get(key):
    """El valor de un ajuste, ya validado. Si no vale, el de por defecto."""
    raw = store.get(key)
    try:
        return PARSERS[key](raw)
    except (TypeError, ValueError):
        if key not in _warned:
            _warned.add(key)
            warning(f"[CONFIG] Invalid value for {key} in {store.settings_path()}: {raw!r}. Using {store.default(key)!r}")
        return store.default(key)


def put(key, raw):
    """Valida y guarda un ajuste. Lanza ValueError si el valor no vale."""
    value = PARSERS[key](raw)
    _warned.discard(key)
    return store.set(key, value)


def language():
    return get("language")


def parallel_downloads():
    return get("downloads.parallel")


def fast_connections():
    return get("downloads.fast_connections")


def auto_format():
    return get("urls.auto_format")


def auto_send():
    return get("urls.auto_send")


def video_quality():
    return get("urls.video_quality")


def audio_quality():
    return get("urls.audio_quality")


def prefer_compatible():
    return get("urls.prefer_compatible")


def audio_format():
    return get("urls.audio_format")


def audio_tags():
    return get("urls.audio_tags")


def sponsorblock():
    return get("urls.sponsorblock")


def playlist_mode():
    return get("urls.playlist")


def playlist_limit():
    return get("urls.playlist_limit")


def max_size_mb():
    return get("urls.max_size_mb")


def extract_after():
    return get("extract.after")


def ffmpeg_hw():
    return get("video.hw")


def ffmpeg_quality():
    return get("video.quality")


def env_settings_present():
    """Las variables antiguas que siguen puestas en el compose."""
    return [name for name in SETTINGS_FROM_ENV if os.environ.get(name) not in (None, "")]
