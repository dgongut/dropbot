"""Invariantes del registro de handlers.

Estos tests no comprueban lógica, sino que el cableado con Telegram sigue
completo: es lo que se rompe al mover handlers de sitio, y un handler que se
queda sin registrar o sin control de admin no da ningún error visible, solo
deja de responder (o responde a quien no debe).
"""

import inspect

import pytest

# Todo lo que el bot atiende hoy. Si añades un handler, añádelo aquí a mano:
# el objetivo es que quitar o dejar de registrar uno sin darse cuenta falle.
EXPECTED_CALLBACKS = {
    b"cancel:",
    b"simplecancel:(.+)",
    b"cancelconv:(.+)",
    b"sendoriginal:(.+)",
    b"keep:(.+)",
    b"del:(.+)",
    b"playlist_(full|first):(.+)",
    b"playlistfmt_(full|first)_(audio|video):(.+)",
    b"url_(audio|video):(.+)",
    b"listcat:(.+)",
    b"managecat:(.+)",
    b"fileact:(.+)",
    b"download:(.+)",
    b"close",
    b"delete:(.+)",
    b"confirmdelete:(.+)",
    b"rename:(.+)",
    b"extract:(.+)",
    b"(delcompressed|keepcompressed):(.+)",
    b"(send|senddelete|nosend):(.+)",
}

EXPECTED_HANDLER_COUNT = 24


def _pattern_of(event):
    """Extrae el patrón original de un evento de Telethon.

    Telethon compila el patrón y guarda el `match` del regex: en CallbackQuery
    va en `.match` y en NewMessage en `.pattern`. Los handlers que filtran con
    una lambda (`data=`/`func=`) no tienen patrón que recuperar.
    """
    matcher = getattr(event, "match", None) or getattr(event, "pattern", None)
    pattern = getattr(getattr(matcher, "__self__", None), "pattern", None)
    if pattern is None:
        return None
    return pattern if isinstance(pattern, bytes) else pattern.encode()


def test_todos_los_handlers_estan_registrados(registered_handlers):
    assert len(registered_handlers) == EXPECTED_HANDLER_COUNT, (
        "cambió el número de handlers registrados; si es intencionado, "
        "actualiza EXPECTED_HANDLER_COUNT"
    )


def test_no_hay_handlers_duplicados(registered_handlers):
    names = [func.__name__ for _, func in registered_handlers]
    duplicated = {n for n in names if names.count(n) > 1}
    assert not duplicated, f"dos handlers con el mismo nombre: {duplicated}"


def test_todos_los_handlers_controlan_que_sea_admin(registered_handlers):
    """Un handler sin control de admin deja el servidor abierto a cualquiera.

    Es lo primero que se olvida al añadir uno nuevo y no produce ningún
    síntoma visible hasta que alguien lo encuentra.
    """
    ungated = []
    for _, func in registered_handlers:
        source = inspect.getsource(func)
        if "check_admin_and_warn" not in source and "is_admin" not in source:
            ungated.append(func.__name__)

    assert not ungated, f"handlers sin control de admin: {ungated}"


@pytest.mark.parametrize("pattern", sorted(EXPECTED_CALLBACKS))
def test_cada_callback_esperado_tiene_handler(registered_handlers, pattern):
    patterns = {_pattern_of(event) for event, _ in registered_handlers}
    patterns.discard(None)  # los que filtran con lambda no exponen patrón

    if pattern in patterns:
        return

    # `cancel:` se registra con una lambda sobre los datos, no con patrón
    lambda_filtered = [
        event for event, _ in registered_handlers if _pattern_of(event) is None
    ]
    assert lambda_filtered, f"nadie atiende {pattern!r}"


def test_los_handlers_son_corrutinas(registered_handlers):
    not_async = [f.__name__ for _, f in registered_handlers
                 if not inspect.iscoroutinefunction(f)]
    assert not not_async, f"handlers que no son async: {not_async}"
