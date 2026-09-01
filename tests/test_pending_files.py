"""Los botones de "¿qué hago con el fichero?" tienen que apuntar a su fichero.

`pending_files` se indexaba por id de evento, no de fichero. Una sola URL puede
generar varios ficheros (el bucle de `run_url_download`), y cada vuelta
sobrescribía la entrada anterior: solo el último botón funcionaba y los demás
no hacían nada al pulsarlos.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def offer_buttons(quiet_bot, monkeypatch):
    """handle_success con lo mínimo simulado para llegar a los botones."""
    async def file_info(path):
        return {
            "type": "video", "size_formatted": "1 MB",
            "duration_formatted": "0:10", "resolution": "640x480",
            "codec_video": "h264", "codec_audio": "aac", "bitrate": None,
        }

    monkeypatch.setattr(quiet_bot, "get_file_info", file_info)
    return quiet_bot


def _button_ids(sent_messages, prefix=b"send:"):
    """Ids extraídos de los botones enviados al usuario."""
    ids = []
    for _, _, kwargs in sent_messages:
        for row in kwargs.get("buttons") or []:
            for button in row if isinstance(row, (list, tuple)) else [row]:
                data = getattr(button, "data", b"")
                if data.startswith(prefix):
                    ids.append(data.split(b":", 1)[1].decode())
    return ids


def test_two_files_from_one_event_get_their_own_buttons(
    offer_buttons, sent_messages, make_event, media_file, run_async
):
    dropbot = offer_buttons
    dropbot.pending_files.clear()

    first = media_file("primero.mp4")
    second = media_file("segundo.mp4")
    event = make_event(event_id=7)

    run_async(dropbot.handle_success(event, first))
    run_async(dropbot.handle_success(event, second))

    registered = set(dropbot.pending_files.values())
    assert registered == {first, second}, (
        "las dos entradas deben convivir; antes la segunda sobrescribía a la primera"
    )

    ids = _button_ids(sent_messages)
    assert len(ids) == 2 and len(set(ids)) == 2, "cada fichero necesita su propio id"
    assert {dropbot.pending_files[i] for i in ids} == {first, second}


def test_each_button_sends_its_own_file(
    offer_buttons, sent_messages, make_event, media_file, run_async, monkeypatch
):
    """Pulsar el botón del primer fichero debe enviar el primero, no el último."""
    dropbot = offer_buttons
    dropbot.pending_files.clear()

    first = media_file("uno.mp4")
    second = media_file("dos.mp4")
    event = make_event(event_id=11)

    run_async(dropbot.handle_success(event, first))
    run_async(dropbot.handle_success(event, second))

    ids = _button_ids(sent_messages)
    first_id = next(i for i in ids if dropbot.pending_files[i] == first)

    sent = []

    async def spy(event, file_path, sending_msg=None, delete_after=False):
        sent.append(file_path)
        return MagicMock(id=1)

    monkeypatch.setattr(dropbot, "send_file_to_telegram", spy)

    click = make_event(event_id=11, groups=(b"send", first_id.encode()))
    run_async(dropbot.handle_send_choice(click))

    assert sent == [first], f"se envió {sent} en lugar del primer fichero"


def test_double_click_is_ignored(
    offer_buttons, sent_messages, make_event, media_file, run_async, monkeypatch
):
    """El segundo clic no debe reenviar: la entrada se extrae de forma atómica."""
    dropbot = offer_buttons
    dropbot.pending_files.clear()

    path = media_file("unico.mp4")
    run_async(dropbot.handle_success(make_event(event_id=21), path))
    file_id = _button_ids(sent_messages)[0]

    sent = []

    async def spy(event, file_path, sending_msg=None, delete_after=False):
        sent.append(file_path)
        return MagicMock(id=1)

    monkeypatch.setattr(dropbot, "send_file_to_telegram", spy)

    click = make_event(event_id=21, groups=(b"send", file_id.encode()))
    run_async(dropbot.handle_send_choice(click))
    run_async(dropbot.handle_send_choice(click))

    assert sent == [path], "el doble clic debe enviarse una sola vez"


def test_button_payloads_fit_in_the_telegram_limit(
    offer_buttons, sent_messages, make_event, media_file, run_async
):
    dropbot = offer_buttons
    dropbot.pending_files.clear()

    run_async(dropbot.handle_success(make_event(event_id=31), media_file("x.mp4")))

    for _, _, kwargs in sent_messages:
        for row in kwargs.get("buttons") or []:
            for button in row if isinstance(row, (list, tuple)) else [row]:
                assert len(button.data) <= 64
