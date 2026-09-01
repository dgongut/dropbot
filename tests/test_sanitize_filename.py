"""`sanitize_filename` tiene que ser segura sin destruir el nombre.

Antes normalizaba a ASCII, así que "Canción.mp3" llegaba al disco como
"Cancion.mp3" y un título en japonés se quedaba en "archivo.mp3". Los sistemas
de ficheros modernos y Telegram manejan UTF-8 sin problema; lo que hay que
quitar son los separadores de ruta y los caracteres de control.
"""

import pytest

from basic import sanitize_filename


class TestSeguridad:
    """Lo que no debe pasar nunca."""

    @pytest.mark.parametrize("hostile", [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "/etc/shadow",
        "subcarpeta/fichero.mp4",
        "fichero\x00oculto.mp4",
    ])
    def test_no_escapa_del_directorio(self, hostile):
        clean = sanitize_filename(hostile)
        assert "/" not in clean
        assert "\\" not in clean
        assert "\x00" not in clean

    def test_sin_caracteres_de_control(self):
        clean = sanitize_filename("mal\x01\x02\x1fnombre.mp4")
        assert not any(ord(c) < 32 for c in clean)

    @pytest.mark.parametrize("reserved", ['<', '>', ':', '"', '|', '?', '*'])
    def test_sin_caracteres_reservados(self, reserved):
        assert reserved not in sanitize_filename(f"na{reserved}me.mp4")

    def test_nunca_devuelve_vacio(self):
        assert sanitize_filename("///.mp4")
        assert sanitize_filename("___")

    def test_respeta_el_limite_de_longitud(self):
        clean = sanitize_filename("a" * 400 + ".mkv")
        assert len(clean) <= 255
        assert clean.endswith(".mkv")


class TestNoDestruye:
    """Lo que sí debe conservarse."""

    def test_conserva_acentos(self):
        assert sanitize_filename("Canción de cuna.mp3") == "Canción de cuna.mp3"

    def test_conserva_enie_y_dieresis(self):
        assert sanitize_filename("El Niño pingüino.mp4") == "El Niño pingüino.mp4"

    @pytest.mark.parametrize("name", [
        "夏の思い出.mp4",
        "Москва.mp3",
        "Ελλάδα.mkv",
        "김치.webm",
    ])
    def test_conserva_alfabetos_no_latinos(self, name):
        assert sanitize_filename(name) == name

    def test_conserva_la_extension(self):
        assert sanitize_filename("Vídeo raro/nombre.mkv").endswith(".mkv")

    def test_conserva_espacios_y_guiones(self):
        assert sanitize_filename("Mi Video - Parte 2.mp4") == "Mi Video - Parte 2.mp4"

    def test_es_idempotente(self):
        once = sanitize_filename("Canción/rara: 2026.mp3")
        assert sanitize_filename(once) == once


class TestLongitudEnBytes:
    """El límite del sistema de ficheros es en bytes, no en caracteres."""

    def test_nombre_no_latino_largo_cabe_en_bytes(self):
        # 200 caracteres japoneses son 600 bytes en UTF-8
        clean = sanitize_filename("夏" * 200 + ".mp4")
        assert len(clean.encode("utf-8")) <= 255
        assert clean.endswith(".mp4")

    def test_no_deja_caracteres_partidos(self):
        clean = sanitize_filename("ñ" * 300 + ".mp3")
        clean.encode("utf-8").decode("utf-8")  # no debe lanzar
        assert len(clean.encode("utf-8")) <= 255

    def test_extension_larga_no_desborda(self):
        clean = sanitize_filename("a" * 300 + "." + "e" * 40)
        assert len(clean.encode("utf-8")) <= 255
