"""Telegram limita el payload de un botón inline a 64 bytes.

Pasarse del límite no degrada nada: Telegram rechaza el mensaje entero
(ButtonDataInvalidError), así que el usuario se queda sin los botones. Estos
tests recorren los botones que construye el bot y comprueban el límite.
"""

CALLBACK_DATA_LIMIT = 64


def _walk(buttons):
    """Aplana la matriz de botones de Telethon y devuelve los `data`."""
    for row in buttons or []:
        for button in row if isinstance(row, (list, tuple)) else [row]:
            data = getattr(button, "data", None)
            if data is not None:
                yield data


def test_extraction_buttons_fit_with_a_long_path(dropbot):
    """El flujo automático de extracción ofrece borrar o mantener el comprimido.

    Antes metía la ruta completa en el payload, así que un nombre de fichero
    normal (los de yt-dlp o los de una descarga cualquiera lo son) rompía el
    límite y hacía desaparecer los botones.
    """
    long_path = "/downloads/Coleccion.De.Documentales.2026.COMPLETA.Temporada.01.rar"
    assert len(long_path) > CALLBACK_DATA_LIMIT, "la ruta de prueba debe pasarse del límite"

    _, buttons = dropbot.get_extraction_message_and_buttons(
        True, "Coleccion.rar", "/downloads/Coleccion", long_path
    )

    payloads = list(_walk(buttons))
    assert payloads, "el resultado correcto debe ofrecer botones"
    for data in payloads:
        assert len(data) <= CALLBACK_DATA_LIMIT, f"{data!r} ocupa {len(data)} bytes"


def test_extraction_buttons_resolve_back_to_the_file(dropbot):
    """El id corto del payload tiene que poder resolverse a la ruta real."""
    path = "/downloads/Otra.Coleccion.Muy.Larga.De.Verdad.Que.Si.2026.rar"

    _, buttons = dropbot.get_extraction_message_and_buttons(
        True, "Otra.rar", "/downloads/Otra", path
    )

    ids = [data.split(b":", 1)[1].decode() for data in _walk(buttons)]
    assert ids, "debe haber al menos un botón con id"
    for file_id in ids:
        assert dropbot.pending_file_actions.get(file_id) == path


def test_manage_buttons_fit_with_a_long_path(dropbot):
    """El flujo de /manage ya usaba ids cortos; se comprueba que sigue así."""
    _, buttons = dropbot.get_extraction_message_and_buttons(
        True,
        "Comprimido.rar",
        "/downloads/una/ruta/francamente/larga/Comprimido",
        "/downloads/una/ruta/francamente/larga/Comprimido.rar",
        file_id="abcd1234",
        from_manage=True,
    )

    for data in _walk(buttons):
        assert len(data) <= CALLBACK_DATA_LIMIT, f"{data!r} ocupa {len(data)} bytes"


def test_every_extraction_outcome_fits(dropbot):
    """Los cuatro resultados posibles de extract_file(), en ambos flujos."""
    path = "/downloads/Un.Nombre.De.Fichero.Deliberadamente.Larguisimo.2026.rar"

    for outcome in (True, False, "missing_parts", "corrupted"):
        for from_manage in (False, True):
            _, buttons = dropbot.get_extraction_message_and_buttons(
                outcome, "f.rar", "/downloads/f", path,
                file_id="abcd1234", from_manage=from_manage,
            )
            for data in _walk(buttons):
                assert len(data) <= CALLBACK_DATA_LIMIT, (
                    f"outcome={outcome!r} from_manage={from_manage}: "
                    f"{data!r} ocupa {len(data)} bytes"
                )
