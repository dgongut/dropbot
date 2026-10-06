"""Migración de la 3.x a la 4.0.0, al arrancar y antes de leer ningún ajuste.

Actualizar no requiere tocar nada: settings.json se siembra con las variables
que ya hay en el docker-compose, así que cada valor que se tenía es el que se
conserva. Lo que cambia es dónde vive a partir de entonces.

Ejecutarla otra vez sobre una instalación ya migrada no hace nada: la siembra
solo ocurre si settings.json no existe.
"""

import os

import settings
import store
from config import DEPRECATED_FOLDER_FILTERS
from logger import debug, warning


def run():
    """Pone el almacenamiento al día. Devuelve True si acaba de sembrarlo."""
    store.init()
    seeded = _seed_settings_from_env()
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
                settings.put(key, raw)
            except (TypeError, ValueError):
                warning(f"[CONFIG] Ignoring {variable}={raw!r}: not a valid value. Using {store.default(key)!r}")

        # Se escribe aunque el compose no tenga nada, para que el siguiente
        # arranque sepa que la siembra ya se hizo
        store.set("settings_version", 1)
    debug(f"[CONFIG] Settings file created at {store.settings_path()}")
    return True


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
