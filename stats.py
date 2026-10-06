"""Estadísticas anónimas de uso.

Una vez al día, una foto de los ajustes y cuántas veces se ha usado cada cosa,
enviada a telemetry.dgongut.com. Solo números, sí/no y valores de listas
cerradas: nunca un nombre de fichero, una URL, una ruta ni un id de Telegram.
Lo que se puede enviar está declarado en el manifiesto del repo de
telemetría, que descarta todo lo que no esté en él, y se publica en la página
de privacidad del proyecto.

Activadas por defecto. No se cuenta ni se envía nada salvo que se cumplan las
dos cosas:

    permitido   /config es un volumen, y TELEMETRY=false no está puesto
    activado    el ajuste, que se cambia desde /settings

Sin volumen el id de instalación sería nuevo en cada recreación, así que una
instalación contaría como muchas, y desactivarlas no sobreviviría a la
siguiente actualización.
"""

import os

import settings
import store
import telemetry
from config import (
    DOWNLOAD_PATH, DOWNLOAD_PATHS, TELEGRAM_ADMIN, TELEMETRY_DEBUG,
    TELEMETRY_ENDPOINT, YTDLP_COOKIES_FILE,
)
from logger import debug, warning

PROJECT = "dropbot"
# Lo que se enseña en vez de la lista entera cuando no cabe en un mensaje
PREVIEW_USAGE_KEYS = 25

_client = None
# La pone start(): este módulo no puede importar dropbot, que lo importa a él
_version = "unknown"


def forced_off():
    """Por qué están desactivadas digan lo que digan los ajustes, o None.

    Devuelve "TELEMETRY" si las apaga la variable, o "volume" cuando /config
    no sobreviviría a recrear el contenedor.
    """
    if telemetry.disabled_by_environment():
        return "TELEMETRY"
    if not store.is_persistent():
        return "volume"
    return None


def is_on():
    """Si el ajuste está activado y nada lo anula."""
    return forced_off() is None and bool(settings.get("telemetry"))


def collect_metrics():
    """La foto que va en cada envío. Cada clave tiene que estar en el manifiesto."""
    metrics = {
        "language": settings.language().lower(),
        "admins": len([admin for admin in str(TELEGRAM_ADMIN).split(",") if admin.strip()]),
        "parallel_downloads": settings.parallel_downloads(),
        "fast_connections": settings.fast_connections(),
        "auto_format": settings.auto_format().lower(),
        "auto_send": settings.auto_send().lower(),
        "video_quality": settings.video_quality().lower(),
        "audio_quality": settings.audio_quality().lower(),
        "prefer_compatible": settings.prefer_compatible(),
        "audio_format": settings.audio_format().lower(),
        "audio_tags": settings.audio_tags(),
        "sponsorblock": settings.sponsorblock().lower(),
        "playlist_mode": settings.playlist_mode().lower(),
        "playlist_limit": settings.playlist_limit(),
        "max_size_mb": settings.max_size_mb(),
        "extract_after": settings.extract_after().lower(),
        "ffmpeg_hw": settings.ffmpeg_hw().lower(),
        "ffmpeg_quality_custom": settings.ffmpeg_quality() is not None,
        "cookies": os.path.isfile(YTDLP_COOKIES_FILE),
        "gpu_device": os.path.exists("/dev/dri"),
    }
    # Qué tipos tienen carpeta propia: si sí o si no, nunca la ruta
    for kind, path in DOWNLOAD_PATHS.items():
        metrics[f"folder_{kind}"] = path != DOWNLOAD_PATH
    return metrics


def client():
    global _client

    if _client is None:
        _client = telemetry.Telemetry(
            project=PROJECT,
            version=_version,
            state_path=os.path.join(store.state_dir(), "telemetry.json"),
            metrics=collect_metrics,
            enabled=is_on,
            endpoint=TELEMETRY_ENDPOINT,
            log=debug,
            debug=TELEMETRY_DEBUG,
        )
    return _client


def start(version):
    global _version

    _version = version
    if _client is not None:
        _client.version = version
    if TELEMETRY_DEBUG:
        warning(f"[TELEMETRY] TELEMETRY_DEBUG is on: statistics go to {TELEMETRY_ENDPOINT} a minute after every start")
    client().start()


def count(key):
    """Cuenta un uso de `key` para el envío del día. Nunca lanza."""
    try:
        client().count(key)
    except Exception as e:
        debug(f"[TELEMETRY] Could not count {key}: {e}")


def disable():
    """Las desactiva y olvida esta instalación.

    El id se va con ellas, así que volver a activarlas empieza como una
    instalación nueva en lugar de coser los dos periodos.
    """
    settings.put("telemetry", False)
    client().forget()


def preview():
    """Exactamente lo que llevaría el próximo envío.

    Los contadores se recortan a los más usados si la lista entera no cabría
    en un mensaje de Telegram; el segundo valor dice cuántos se ocultaron.
    """
    payload = client().preview()
    usage = payload.get("usage") or {}
    hidden = 0
    if len(usage) > PREVIEW_USAGE_KEYS:
        ranked = sorted(usage.items(), key=lambda item: (-item[1], item[0]))
        payload["usage"] = dict(ranked[:PREVIEW_USAGE_KEYS])
        hidden = len(usage) - PREVIEW_USAGE_KEYS
    return payload, hidden
