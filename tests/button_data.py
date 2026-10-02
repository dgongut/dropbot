"""Payload de un botón inline, sea cual sea la versión de Telethon.

Hasta Telethon 1.44 `Button.inline()` devolvía un KeyboardButtonCallback con
`.data`; desde 1.45 devuelve un KeyboardInlineButton con el payload en
`.type.data`.
"""


def button_data(button):
    data = getattr(button, "data", None)
    if data is None:
        data = getattr(getattr(button, "type", None), "data", None)
    return data
