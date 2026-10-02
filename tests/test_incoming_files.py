"""Qué mensajes cuentan como "me han mandado un fichero"."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from telethon.tl import types


def _event(media, **attrs):
    fields = {"media": media, "document": None, "video": None, "audio": None, "photo": None}
    fields.update(attrs)
    return SimpleNamespace(**fields)


def test_la_vista_previa_de_un_enlace_no_es_un_fichero(dropbot):
    """Telethon expone la imagen de la preview en .photo: no hay que descargarla."""
    preview = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
    assert not dropbot.is_file_message(_event(preview, photo=MagicMock()))


def test_una_foto_enviada_si_es_un_fichero(dropbot):
    photo = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))
    assert dropbot.is_file_message(_event(photo, photo=MagicMock()))


def test_un_texto_no_es_un_fichero(dropbot):
    assert not dropbot.is_file_message(_event(None))
