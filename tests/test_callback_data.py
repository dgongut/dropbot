"""Telegram limita el payload de un botón inline a 64 bytes.

Pasarse del límite no degrada nada: Telegram rechaza el mensaje entero
(ButtonDataInvalidError), así que el usuario se queda sin los botones. Estos
tests recorren los botones que construye el bot y comprueban el límite.
"""

from button_data import button_data

CALLBACK_DATA_LIMIT = 64


def _walk(buttons):
    """Aplana la matriz de botones de Telethon y devuelve los `data`."""
    for row in buttons or []:
        for button in row if isinstance(row, (list, tuple)) else [row]:
            data = button_data(button)
            if data is not None:
                yield data


def test_manage_buttons_fit_with_a_long_path(dropbot):
    """El flujo de /manage usa ids cortos; se comprueba que sigue así."""
    _, buttons = dropbot.get_extraction_message_and_buttons(
        True,
        "Comprimido.rar",
        "/downloads/una/ruta/francamente/larga/Comprimido",
        file_id="abcd1234",
    )

    for data in _walk(buttons):
        assert len(data) <= CALLBACK_DATA_LIMIT, f"{data!r} ocupa {len(data)} bytes"


def test_every_extraction_outcome_fits(dropbot):
    """Todos los resultados posibles de extract_file()."""
    for outcome in (True, False, "missing_parts", "corrupted", "password", "no_space"):
        _, buttons = dropbot.get_extraction_message_and_buttons(
            outcome, "Un.Nombre.De.Fichero.Deliberadamente.Larguisimo.2026.rar",
            "/downloads/Un.Nombre.De.Fichero.Deliberadamente.Larguisimo.2026",
            file_id="abcd1234",
        )
        for data in _walk(buttons):
            assert len(data) <= CALLBACK_DATA_LIMIT, (
                f"outcome={outcome!r}: {data!r} ocupa {len(data)} bytes"
            )
