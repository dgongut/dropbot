"""`sanitize_filename` tiene que ser segura sin destruir el nombre.

Antes normalizaba a ASCII, así que "Canción.mp3" llegaba al disco como
"Cancion.mp3" y un título en japonés se quedaba en "archivo.mp3". Los sistemas
de ficheros modernos y Telegram manejan UTF-8 sin problema; lo que hay que
quitar son los separadores de ruta y los caracteres de control.
"""

import pytest

from basic import _truncate_to_bytes, sanitize_filename


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

    @pytest.mark.parametrize("name", ["1. " + "a" * 308, "x." + "b" * 300])
    def test_un_punto_al_principio_sin_extension_no_desborda(self, name):
        """splitext toma por extensión todo lo que sigue al último punto: sin
        sitio para la base, con 'archivo' delante pasaba de 255 bytes y el
        fichero no se podía crear."""
        clean = sanitize_filename(name)
        assert len(clean.encode("utf-8")) <= 255
        assert name.startswith(clean)

    def test_recortar_a_un_maximo_negativo_no_deja_nada(self):
        # Con el slice negativo solo se quitaban bytes del final: "abcd"
        assert _truncate_to_bytes("abcdef", -2) == ""


RLO = "\N{RIGHT-TO-LEFT OVERRIDE}"
ZWJ = "\N{ZERO WIDTH JOINER}"
# Bandera de Inglaterra: la bandera negra y caracteres de etiqueta (Cf)
INGLATERRA = "\N{WAVING BLACK FLAG}" + "".join(chr(0xE0000 + ord(c)) for c in "gbeng") + "\N{CANCEL TAG}"


class TestExtensionDisfrazada:
    """Las marcas bidi y los invisibles no pueden esconder la extensión real.

    Con un U+202E, "Factura_2026<RLO>fdp.exe" se ve como "Factura_2026exe.pdf"
    en el mensaje de descarga, en /manage y en el Explorador de Windows.
    """

    def test_la_marca_rlo_no_da_la_vuelta_a_la_extension(self):
        assert sanitize_filename(f"Factura_2026{RLO}fdp.exe") == "Factura_2026_fdp.exe"

    @pytest.mark.parametrize("mark", [
        "\x7f", "\x85", "\x9b",
        "\N{ARABIC LETTER MARK}",
        "\N{LEFT-TO-RIGHT MARK}", "\N{RIGHT-TO-LEFT MARK}",
        "\N{LEFT-TO-RIGHT EMBEDDING}", RLO,
        "\N{LEFT-TO-RIGHT ISOLATE}", "\N{POP DIRECTIONAL ISOLATE}",
        "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}",
    ])
    def test_quita_del_c1_y_marcas_bidi(self, mark):
        assert sanitize_filename(f"a{mark}b.pdf") == "a_b.pdf"

    def test_quita_los_invisibles_de_los_extremos(self):
        name = "\N{ZERO WIDTH NO-BREAK SPACE}informe\N{ZERO WIDTH SPACE}.pdf\N{WORD JOINER}"
        assert sanitize_filename(name) == "informe.pdf"

    @pytest.mark.parametrize("name", [
        f"👨{ZWJ}👩{ZWJ}👧 familia.jpg",
        "می\N{ZERO WIDTH NON-JOINER}خواهم.mp3",
        f"Viaje a {INGLATERRA}.mp4",
        "ข่าว\N{ZERO WIDTH SPACE}วันนี้.mp4",
    ])
    def test_conserva_emoji_compuestos_y_alfabetos(self, name):
        assert sanitize_filename(name) == name


class TestSinPuntoDelante:
    """Un nombre que empieza por punto queda oculto: /list y /manage no lo
    muestran y desde el bot ya no se puede enviar, renombrar ni borrar."""

    @pytest.mark.parametrize("name, expected", [
        (".mp4", "archivo.mp4"),
        (".env", "archivo.env"),
        ("..mp4", "archivo.mp4"),
        (".pelicula.mp4", "pelicula.mp4"),
        # El strip de los "_" dejaba el punto delante
        ("_.notas.pdf", "notas.pdf"),
        ("?.x.mp4", "x.mp4"),
        (":.x.mp4", "x.mp4"),
        ("._x.mp4", "x.mp4"),
        ("\N{ZERO WIDTH SPACE}.x.mp4", "x.mp4"),
    ])
    def test_no_deja_un_nombre_oculto(self, name, expected):
        assert sanitize_filename(name) == expected

    def test_un_nombre_larguisimo_con_punto_delante_cabe_en_255_bytes(self):
        clean = sanitize_filename("." + "e" * 300)
        assert not clean.startswith(".")
        assert len(clean.encode("utf-8")) <= 255


@pytest.mark.parametrize("name", [
    ".mp4", ".pelicula.mp4", "_.notas.pdf", f"Factura{RLO}fdp.exe",
    "\N{ZERO WIDTH SPACE}informe\N{ZERO WIDTH SPACE}.pdf", "." + "e" * 300,
    "1. " + "a" * 308,
])
def test_es_idempotente_con_nombres_raros(name):
    once = sanitize_filename(name)
    assert sanitize_filename(once) == once
