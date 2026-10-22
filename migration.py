"""Migración de la 3.x a la 4.0.0, al arrancar y antes de leer ningún ajuste.

Actualizar no requiere tocar nada: settings.json se siembra con las variables
que ya hay en el docker-compose, así que cada valor que se tenía es el que se
conserva. Lo que cambia es dónde vive a partir de entonces.

Ejecutarla otra vez sobre una instalación ya migrada no hace nada: la siembra
solo ocurre si settings.json no existe. Un settings.json de una versión
anterior se pone al día una sola vez (ver _upgrade_settings).
"""

import os

import settings
import store
from config import DEPRECATED_FOLDER_FILTERS
from logger import debug, warning

# El aviso por Telegram de que las variables del compose se han importado está
# por dar. Se guarda con la siembra, en settings.json, y no en memoria: si el
# arranque que sembró se caía antes de avisar, el aviso no llegaba nunca
IMPORT_NOTICE = "notices.settings_imported"

# Los ajustes numéricos que la 4.0 acota, y entre qué límites se dejan los
# valores de la 3.x. Allí no tenían tope, y un FAST_CONNECTIONS de 1 o menos
# (un 0, p. ej.) era el método estándar, que ahora es el 1. Descartarlos los
# dejaba en 2 descargas y 8 conexiones, justo lo contrario de lo que pedía
# quien había puesto un 0. Un PARALLEL_DOWNLOADS menor que 1 ya abortaba la
# 3.x: ese sigue sin valer, y por eso no tiene mínimo
LIMITS_FROM_3X = {
    "downloads.parallel": (None, settings.PARALLEL_RANGE[1]),
    "downloads.fast_connections": settings.FAST_CONNECTIONS_RANGE,
}

# La versión de settings.json que deja este arranque: la 4.0.0 escribía la 1.
# Cada una de después cambia algo de lo que ya estaba guardado
SETTINGS_VERSION = 2


def run():
    """Pone el almacenamiento al día. Devuelve True si acaba de sembrarlo."""
    store.init()
    seeded = _seed_settings_from_env()
    _upgrade_settings()
    _warn_deprecated_env(seeded)
    return seeded


def _seed_settings_from_env():
    """Escribe settings.json a partir del entorno, solo la primera vez.

    Después el entorno se ignora: quien cambia un ajuste desde Telegram no
    puede verlo volver atrás en el siguiente reinicio.
    """
    if store.settings_exists() or store.settings_unreadable():
        return False

    with store.batch():
        for variable, key in settings.SETTINGS_FROM_ENV.items():
            raw = os.environ.get(variable)
            if raw is None or raw == "":
                continue
            try:
                settings.put(key, _within_limits(variable, key, raw))
            except (TypeError, ValueError):
                warning(f"[CONFIG] Ignoring {variable}={raw!r}: not a valid value. Using {store.default(key)!r}")

        # Se escribe aunque el compose no tenga nada, para que el siguiente
        # arranque sepa que la siembra ya se hizo. Ya en la última versión:
        # lo recién sembrado no tiene nada que poner al día
        store.set("settings_version", SETTINGS_VERSION)
        # En la misma escritura que la siembra: no puede quedar una sin la otra
        if settings.env_settings_present():
            store.set(IMPORT_NOTICE, True)
    if store.write_error() is None:
        debug(f"[CONFIG] Settings file created at {store.settings_path()}")
    return True


def _upgrade_settings():
    """Pone al día un settings.json escrito por una versión anterior.

    Una sola vez: lo que se elija después en /settings, o se escriba a mano,
    se respeta.
    """
    version = store.get("settings_version")
    if store.settings_unreadable() or (isinstance(version, int) and version >= SETTINGS_VERSION):
        return
    with store.batch():
        # 2 (4.0.1): la opción «2 GB» eran 2048 MiB, y Telegram no admite más
        # de 2000. Se descargaba lo que luego no se podía enviar, y en
        # /settings no salía marcada ninguna opción
        if settings.max_size_mb() == 2048:
            settings.put("urls.max_size_mb", 2000)
            debug("[CONFIG] urls.max_size_mb 2048 -> 2000: Telegram does not accept more than 2000 MiB")
        store.set("settings_version", SETTINGS_VERSION)


def _within_limits(variable, key, raw):
    """`raw` llevado al valor permitido más cercano, si la 3.x lo aceptaba.

    Lo que no es un número se devuelve tal cual: lo rechaza settings.put,
    como cualquier otro valor que no vale.
    """
    if key not in LIMITS_FROM_3X:
        return raw
    try:
        value = int(str(raw).strip())
    except ValueError:
        return raw
    low, high = LIMITS_FROM_3X[key]
    adjusted = min(value, high) if low is None else min(max(value, low), high)
    if adjusted != value:
        warning(f"[CONFIG] {variable}={raw!r} is out of the range allowed now: using {adjusted}")
    return adjusted


def import_notice_pending():
    """Si a ningún admin le ha llegado todavía el aviso de la importación."""
    return store.get(IMPORT_NOTICE) is True


def import_notice_sent():
    """Le ha llegado a alguno: no se repite en los siguientes arranques."""
    store.set(IMPORT_NOTICE, False)


def _warn_deprecated_env(seeded):
    """Avisa de las variables que ya no hacen nada.

    Callarse sería peor: el usuario cambia el compose, reinicia, no ve ningún
    cambio y no tiene forma de saber que el valor se lee de otro sitio.
    """
    present = settings.env_settings_present()
    if present:
        if seeded:
            debug(f"[CONFIG] Imported into {store.settings_path()}: {', '.join(present)}. They can be removed from the compose.")
        else:
            warning(f"[CONFIG] These variables are no longer read and can be removed from the compose: "
                    f"{', '.join(present)}. Their values are now managed with /settings.")

    filters = [name for name in DEPRECATED_FOLDER_FILTERS if os.environ.get(name) not in (None, "")]
    if filters:
        warning(f"[CONFIG] These variables are no longer read and can be removed from the compose: "
                f"{', '.join(filters)}. A type of file now goes to its own folder whenever that folder "
                f"is mounted as a volume (for example /video), and to /downloads otherwise.")
