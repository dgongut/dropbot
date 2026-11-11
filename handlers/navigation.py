"""Lo que comparten las pantallas para volver al menú principal (ver handlers/home.py).

Aparte de home.py para que los demás módulos de handlers puedan poner el botón
sin importarlo: home importa a todos, y sería un ciclo.
"""

from telethon import Button

from translations import get_text

HOME = "home:main"
# El explorador de /manage: las carpetas del servidor (ver handlers/files.py)
MANAGE_ROOT = "fm:r"


def home_button():
    return Button.inline(get_text("button_home"), data=HOME)


def exit_row():
    """La última fila de una pantalla de primer nivel: al menú principal, o cerrar."""
    return [home_button(), Button.inline(get_text("button_close"), data="close")]
