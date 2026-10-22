"""Menús de /list y /manage: qué categorías se ofrecen y qué se lista en cada una.

Cada test monta su propio árbol de carpetas en `tmp_path` y decide qué tipos
tienen carpeta propia tocando `config.DOWNLOAD_PATHS` (el mismo dict que usan
`handlers.manage` y `config.has_own_folder`). Se comprueba lo que vería el
usuario: los mensajes enviados, su texto ya interpretado como Markdown por
Telethon, y los botones con su `callback_data`.
"""

import os
import threading

import pytest
from telethon.extensions import markdown

import config
import settings
from button_data import button_data
from conftest import FORUM_TOPIC

TELEGRAM_TEXT_LIMIT = 4096
CALLBACK_DATA_LIMIT = 64
KINDS = ("audio", "video", "photo", "torrent", "ebook", "url_video", "url_audio")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _rows(kwargs):
    return kwargs.get("buttons") or []


def _payloads_of(kwargs):
    return [button_data(button) for row in _rows(kwargs) for button in row]


def _labels_of(kwargs):
    return [button.text for row in _rows(kwargs) for button in row]


def _plain(text):
    """El texto tal como lo verá el usuario, después de interpretar el Markdown."""
    return markdown.parse(text)[0]


def _utf16_len(text):
    """Telegram cuenta la longitud de un mensaje en unidades UTF-16."""
    return len(text.encode("utf-16-le")) // 2


def _write(path, size=10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    return path


def _write_not_utf8(directory, raw_name=b"caf\xe9.mp3"):
    """Un fichero con el nombre en Latin-1, que no es UTF-8, como los de un ZIP
    de Windows descomprimido en el host. Devuelve su ruta tal como la ve
    Python, con sustitutos ("caf\\udce9.mp3")."""
    path = os.path.join(os.fsencode(directory), raw_name)
    try:
        with open(path, "wb") as handle:
            handle.write(b"\0")
    except OSError:
        pytest.skip("este sistema de ficheros no admite nombres que no son UTF-8")
    return os.fsdecode(path)


class _NotAdmin:
    """Un callback de alguien que no es el administrador."""

    def __init__(self, groups=(b"all",)):
        self.id = 1
        self.chat_id = 42
        self.sender_id = 12345
        self.data = None
        self._groups = (b"",) + tuple(groups)
        self.pattern_match = self

    def group(self, index):
        return self._groups[index]

    async def get_sender(self):
        return None


@pytest.fixture
def manage(quiet_manage):
    """`handlers.manage` con el estado compartido limpio antes y después."""
    for state in (quiet_manage.pending_file_actions, quiet_manage.pending_renames,
                  quiet_manage.list_messages):
        state.clear()
    yield quiet_manage
    for state in (quiet_manage.pending_file_actions, quiet_manage.pending_renames,
                  quiet_manage.list_messages):
        state.clear()


@pytest.fixture
def folders(manage, tmp_path, monkeypatch):
    """Monta las carpetas de descarga en tmp_path.

    `folders("video", "photo")` les da carpeta propia a esos tipos; los demás
    van a la general. Devuelve un dict tipo -> ruta, con "base" para la general.
    """
    assert manage.DOWNLOAD_PATHS is config.DOWNLOAD_PATHS, \
        "handlers.manage debe compartir el dict de rutas con config"

    def setup(*own, create=True):
        base = str(tmp_path / "downloads")
        monkeypatch.setattr(config, "DOWNLOAD_PATH", base)
        monkeypatch.setattr(manage, "DOWNLOAD_PATH", base)
        paths = {"base": base}
        for kind in KINDS:
            path = str(tmp_path / kind) if kind in own else base
            # Lo descargado de URLs cae donde caiga el resto del vídeo/audio
            if kind == "url_video" and kind not in own:
                path = paths["video"]
            if kind == "url_audio" and kind not in own:
                path = paths["audio"]
            monkeypatch.setitem(config.DOWNLOAD_PATHS, kind, path)
            paths[kind] = path
        if create:
            for path in set(paths.values()):
                os.makedirs(path, exist_ok=True)
        return paths

    return setup


def _list(manage, make_event, run_async, category=b"all"):
    run_async(manage.handle_list_category(make_event(groups=(category,))))


def _manage(manage, make_event, run_async, category=b"all"):
    run_async(manage.handle_manage_category(make_event(groups=(category,))))


def _sent(sent_messages, kinds=("send_message", "reply")):
    return [(text, kwargs) for kind, text, kwargs in sent_messages if kind in kinds]


# ---------------------------------------------------------------------------
# Categorías
# ---------------------------------------------------------------------------

class TestCategorias:
    def test_sin_carpetas_propias_solo_se_ofrece_todos(self, manage, folders):
        folders()

        categories = manage.get_available_categories()

        assert [cat_id for cat_id, _ in categories] == ["all"]

    @pytest.mark.parametrize("own,expected", [
        (("video",), ["all", "video"]),
        (("url_video",), ["all", "video"]),
        (("audio",), ["all", "audio"]),
        (("url_audio",), ["all", "audio"]),
        (("photo",), ["all", "photo"]),
        (("torrent",), ["all", "torrent"]),
        (("ebook",), ["all", "ebook"]),
        (KINDS, ["all", "video", "audio", "photo", "torrent", "ebook"]),
    ])
    def test_cada_carpeta_propia_anade_su_categoria(self, manage, folders, own, expected):
        folders(*own)

        assert [cat_id for cat_id, _ in manage.get_available_categories()] == expected

    def test_los_botones_van_de_tres_en_tres_y_cerrar_aparte(self, manage, folders):
        folders(*KINDS)

        buttons = manage.get_category_buttons()

        assert [len(row) for row in buttons] == [3, 3, 1]
        assert button_data(buttons[-1][0]) == b"close"
        payloads = [button_data(b) for row in buttons[:-1] for b in row]
        assert payloads == [b"listcat:all", b"listcat:video", b"listcat:audio",
                            b"listcat:photo", b"listcat:torrent", b"listcat:ebook"]

    def test_la_categoria_actual_no_se_ofrece(self, manage, folders):
        folders("video", "photo")

        buttons = manage.get_category_buttons(exclude_category="video")

        payloads = [button_data(b) for row in buttons for b in row]
        assert payloads == [b"listcat:all", b"listcat:photo", b"close"]

    def test_sin_categorias_queda_solo_cerrar(self, manage, folders):
        folders()

        buttons = manage.get_category_buttons(exclude_category="all")

        assert [[button_data(b) for b in row] for row in buttons] == [[b"close"]]

    def test_en_ingles_las_categorias_salen_en_ingles(self, manage, folders, config_dir):
        folders(*KINDS)
        settings.put("language", "EN")

        labels = [b.text for row in manage.get_category_buttons() for b in row]

        for spanish in ("Todos", "Fotos", "Cerrar"):
            assert not any(spanish in label for label in labels), labels


# ---------------------------------------------------------------------------
# /list
# ---------------------------------------------------------------------------

class TestListar:
    def test_carpeta_vacia_avisa_y_ofrece_categorias(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        folders("video")

        _list(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        assert text == manage.get_text("list_empty")
        assert b"listcat:video" in _payloads_of(kwargs)
        assert b"listcat:all" not in _payloads_of(kwargs), "la categoría actual no se ofrece"
        assert manage.list_messages[999], "debe recordar el mensaje para borrarlo"

    def test_carpetas_que_no_existen_no_revientan(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        folders(*KINDS, create=False)

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert text == manage.get_text("list_empty")

    def test_los_ocultos_no_se_listan(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], ".oculto"))
        _write(os.path.join(paths["base"], "visible.mkv"))

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        plain = _plain(text)
        assert "visible.mkv" in plain
        assert ".oculto" not in plain
        assert manage.get_text("total_files_space", 1, "10.00 B") in text

    def test_las_carpetas_salen_con_su_tamano_y_se_cuentan_aparte(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "Serie", "cap1.mkv"), 1000)
        _write(os.path.join(paths["base"], "Serie", "sub", "cap2.mkv"), 24)
        _write(os.path.join(paths["base"], "suelto.txt"), 1024)

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        plain = _plain(text)
        serie = plain.split("Serie", 1)[1].split("\n\n")[0]
        assert "📁" in plain.split("Serie")[0].splitlines()[-1]
        assert "1.00 KB" in serie, "el tamaño de la carpeta es el de todo su contenido"
        assert manage.get_text("total_files_folders_space", 1, 1, "2.00 KB") in text

    def test_se_ordena_alfabeticamente_sin_distinguir_mayusculas(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders("video")
        for name in ("zeta.mkv", "Beta.mkv", "alfa.mkv"):
            _write(os.path.join(paths["base"], name))
        _write(os.path.join(paths["video"], "Gamma.mkv"))

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        plain = _plain(text)
        positions = [plain.index(name) for name in ("alfa.mkv", "Beta.mkv", "Gamma.mkv", "zeta.mkv")]
        assert positions == sorted(positions)
        assert "1. " in plain and "4. " in plain

    def test_varias_categorias_en_la_misma_carpeta_no_duplican(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        # Sin carpetas propias, las siete apuntan a la general
        paths = folders()
        _write(os.path.join(paths["base"], "unico.mkv"))

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert _plain(text).count("unico.mkv") == 1

    def test_video_y_url_video_en_la_misma_carpeta_no_duplican(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders("video")
        _write(os.path.join(paths["video"], "peli.mkv"))
        _write(os.path.join(paths["base"], "fuera.txt"))

        _list(manage, make_event, run_async, b"video")

        [(text, _)] = _sent(sent_messages)
        plain = _plain(text)
        assert plain.count("peli.mkv") == 1
        assert "fuera.txt" not in plain, "la categoría vídeo solo mira su carpeta"

    def test_una_categoria_desconocida_lista_todo(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders("photo")
        _write(os.path.join(paths["photo"], "foto.jpg"))
        _write(os.path.join(paths["base"], "otro.txt"))

        _list(manage, make_event, run_async, b"inventada")

        [(text, _)] = _sent(sent_messages)
        assert "foto.jpg" in _plain(text) and "otro.txt" in _plain(text)

    def test_borra_el_listado_anterior_y_recuerda_el_nuevo(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "a.txt"))
        manage.list_messages[999] = ["viejo1", "viejo2"]

        _list(manage, make_event, run_async)

        # Los dos viejos y el propio mensaje con los botones
        assert [kind for kind, _, _ in sent_messages].count("delete") == 3
        assert manage.list_messages[999] and "viejo1" not in manage.list_messages[999]

    def test_muchos_ficheros_se_parten_en_varios_mensajes(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        names = [f"fichero_numero_{i:04d}_con_un_nombre_largo.mkv" for i in range(300)]
        for name in names:
            _write(os.path.join(paths["base"], name))

        _list(manage, make_event, run_async)

        messages = _sent(sent_messages)
        assert len(messages) > 1
        for index, (text, kwargs) in enumerate(messages, 1):
            assert len(text) <= TELEGRAM_TEXT_LIMIT
            assert f"(Parte {index}/{len(messages)})" in text
            # Los botones de categorías solo en el último
            assert bool(kwargs.get("buttons")) is (index == len(messages))
        joined = "\n".join(_plain(text) for text, _ in messages)
        for number in range(1, 301):
            assert f"\n{number}. " in "\n" + joined
        assert len(manage.list_messages[999]) == len(messages)

    def test_un_nombre_largo_se_recorta(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        long_name = "x" * 80 + ".mkv"
        _write(os.path.join(paths["base"], long_name))

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert "x" * 37 + "..." in _plain(text)
        assert long_name not in text

    @pytest.mark.parametrize("name", [
        "mi_video_de_prueba.mkv",
        "doble__guion__bajo.mkv",
        "con*asterisco*.mkv",
        "doble**asterisco**.mkv",
        "[corchete](enlace).mkv",
        "tilde~~doble~~.mkv",
    ])
    def test_los_caracteres_de_markdown_no_rompen_el_listado(
        self, manage, folders, sent_messages, make_event, run_async, name
    ):
        paths = folders()
        _write(os.path.join(paths["base"], name))

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert name in _plain(text)

    def test_una_comilla_invertida_no_rompe_el_listado(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        name = "canción `remix`.mp3"
        _write(os.path.join(paths["base"], name))

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert name in _plain(text)

    def test_nombres_con_emoji_no_pasan_del_limite_de_telegram(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        for i in range(200):
            _write(os.path.join(paths["base"], f"{i:03d}" + "🎬" * 36 + ".x"))

        _list(manage, make_event, run_async)

        for text, _ in _sent(sent_messages):
            assert _utf16_len(_plain(text)) <= TELEGRAM_TEXT_LIMIT

    def test_un_nombre_que_no_es_utf8_no_rompe_el_listado(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        """Sus sustitutos no se pueden codificar: el listado entero acababa en
        "Error al listar archivos" cada vez."""
        paths = folders()
        _write_not_utf8(paths["base"])
        _write(os.path.join(paths["base"], "bueno.txt"))

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        text.encode("utf-8")  # lo que hace Telethon al enviarlo
        assert "caf\\xe9.mp3" in _plain(text) and "bueno.txt" in _plain(text)

    def test_un_error_al_leer_la_carpeta_avisa(
        self, manage, folders, sent_messages, make_event, run_async, monkeypatch
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "a.txt"))

        def denied(path):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(manage.os, "listdir", denied)
        _list(manage, make_event, run_async)

        assert [text for text, _ in _sent(sent_messages)] == [manage.get_text("error_list_files")]

    @pytest.mark.parametrize("handler", ["handle_list_category", "handle_manage_category"])
    def test_una_carpeta_sin_permisos_no_tira_el_listado_entero(
        self, manage, folders, sent_messages, make_event, run_async, monkeypatch, handler
    ):
        paths = folders("video")
        _write(os.path.join(paths["base"], "visible.txt"))
        real_listdir = os.listdir

        def denied_in_video(path):
            if path == paths["video"]:
                raise PermissionError(13, "Permission denied", path)
            return real_listdir(path)

        monkeypatch.setattr(manage.os, "listdir", denied_in_video)
        run_async(getattr(manage, handler)(make_event(groups=(b"all",))))

        [(text, kwargs)] = _sent(sent_messages)
        if handler == "handle_list_category":
            assert "visible.txt" in _plain(text)
        else:
            assert any(label.endswith("visible.txt") for label in _labels_of(kwargs))

    def test_un_no_admin_no_ve_nada(self, manage, folders, sent_messages, run_async):
        paths = folders()
        _write(os.path.join(paths["base"], "secreto.txt"))

        run_async(manage.handle_list_category(_NotAdmin()))

        assert sent_messages == []

    def test_en_ingles_la_cabecera_sale_en_ingles(
        self, manage, folders, sent_messages, make_event, run_async, config_dir
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "a.txt"))
        settings.put("language", "EN")

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert "Archivos" not in text


# ---------------------------------------------------------------------------
# /manage
# ---------------------------------------------------------------------------

class TestGestionar:
    def test_carpeta_vacia_avisa_y_ofrece_categorias(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        folders("torrent")

        _manage(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        assert text == manage.get_text("manage_no_files")
        assert _payloads_of(kwargs) == [b"managecat:torrent", b"close"]
        assert manage.pending_file_actions == {}

    def test_carpetas_que_no_existen_no_revientan(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        folders(*KINDS, create=False)

        _manage(manage, make_event, run_async, b"video")

        [(text, _)] = _sent(sent_messages)
        assert text == manage.get_text("manage_no_files")

    def test_un_boton_por_elemento_en_orden_y_registrado(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders("video")
        for name in ("zeta.mkv", "Alfa.mkv"):
            _write(os.path.join(paths["base"], name))
        _write(os.path.join(paths["video"], "medio.mkv"))
        os.makedirs(os.path.join(paths["base"], "Carpeta"))
        _write(os.path.join(paths["base"], ".oculto"))

        _manage(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        rows = _rows(kwargs)
        file_rows = [row for row in rows if button_data(row[0]).startswith(b"fileact:")]
        labels = [row[0].text for row in file_rows]
        assert [label.split(" ", 2)[2] for label in labels] == ["Alfa.mkv", "Carpeta", "medio.mkv", "zeta.mkv"]
        assert labels[0].startswith("1. ") and labels[1] == "2. 📁 Carpeta"
        resolved = [manage.pending_file_actions[button_data(row[0]).split(b":", 1)[1].decode()]
                    for row in file_rows]
        assert resolved == [os.path.join(paths["base"], "Alfa.mkv"),
                            os.path.join(paths["base"], "Carpeta"),
                            os.path.join(paths["video"], "medio.mkv"),
                            os.path.join(paths["base"], "zeta.mkv")]
        assert b"managecat:video" in _payloads_of(kwargs)
        assert b"managecat:all" not in _payloads_of(kwargs)
        assert manage.get_text("total_files_folders_space", 3, 1, "30.00 B") in text
        assert manage.list_messages[999]

    def test_otro_listado_no_cambia_a_que_fichero_apunta_un_boton(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        """El id era `{fila}_{hash % 100000}`: dos rutas en la misma fila podían
        compartirlo, y el listado de otro admin cambiaba a qué fichero apuntaba
        el «Sí, eliminar» que había pendiente. Se borraba otro fichero."""
        paths = folders("video", "audio")
        shown = _write(os.path.join(paths["video"], "clip_1.mp4"))
        # Una ruta que, en la misma fila (la única de su listado), daba el mismo id
        collision = abs(hash(shown)) % 100000
        candidates = (os.path.join(paths["audio"], f"clip_{n}.mp3") for n in range(10_000_000))
        other = next(path for path in candidates if abs(hash(path)) % 100000 == collision)
        _write(other)

        _manage(manage, make_event, run_async, b"video")
        [shown_id] = [p.split(b":", 1)[1] for p in _payloads_of(_sent(sent_messages)[0][1])
                      if p.startswith(b"fileact:")]
        _manage(manage, make_event, run_async, b"audio")
        run_async(manage.handle_confirm_delete(make_event(groups=(shown_id,))))

        assert not os.path.exists(shown)
        assert os.path.exists(other), "el botón borraba el fichero del otro listado"

    def test_listar_otra_vez_conserva_los_ids_sin_acumularlos(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        for name in ("a.txt", "b.txt"):
            _write(os.path.join(paths["base"], name))

        _manage(manage, make_event, run_async)
        _manage(manage, make_event, run_async)

        first, second = [[p for p in _payloads_of(kwargs) if p.startswith(b"fileact:")]
                         for _, kwargs in _sent(sent_messages)]
        assert first == second
        assert len(manage.pending_file_actions) == 2

    def test_sin_carpetas_el_total_solo_cuenta_ficheros(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "a.txt"), 2048)

        _manage(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert manage.get_text("total_files_space", 1, "2.00 KB") in text

    def test_la_misma_carpeta_en_varias_categorias_no_duplica(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "unico.mkv"))

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        assert sum(1 for p in _payloads_of(kwargs) if p.startswith(b"fileact:")) == 1

    def test_mas_de_ochenta_elementos_pide_filtrar(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders("video")
        for i in range(85):
            _write(os.path.join(paths["base"], f"f{i:02d}.txt"))
        os.makedirs(os.path.join(paths["base"], "una_carpeta"))

        _manage(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        assert manage.get_text("manage_too_many_items") in text
        assert manage.get_text("manage_too_many_files_folders", 85, 1) in text
        assert not any(p.startswith(b"fileact:") for p in _payloads_of(kwargs))
        assert b"managecat:video" in _payloads_of(kwargs)

    def test_mas_de_ochenta_ficheros_sin_carpetas(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        for i in range(81):
            _write(os.path.join(paths["base"], f"f{i:02d}.txt"))

        _manage(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert manage.get_text("manage_too_many_files", 81) in text

    def test_ochenta_justos_aun_se_listan(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        for i in range(80):
            _write(os.path.join(paths["base"], f"f{i:02d}.txt"))

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        assert sum(1 for p in _payloads_of(kwargs) if p.startswith(b"fileact:")) == 80
        # Telegram no admite más de 100 botones por mensaje
        assert len(_payloads_of(kwargs)) <= 100

    def test_la_etiqueta_recorta_los_nombres_largos(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "a" * 60 + ".mkv"))

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        label = _labels_of(kwargs)[0]
        assert label.endswith("a" * 25 + "...")

    def test_los_callbacks_caben_con_nombres_largos_y_unicode(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders(*KINDS)
        names = ["Ñandú_" + "á" * 120 + ".mkv", "日本語のファイル名" * 8 + ".mp4",
                 "🎬" * 60 + ".mkv", "x" * 240]
        for name in names:
            _write(os.path.join(paths["video"], name))
        os.makedirs(os.path.join(paths["base"], "📁" * 50))

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        payloads = _payloads_of(kwargs)
        assert sum(1 for p in payloads if p.startswith(b"fileact:")) == 5
        for payload in payloads:
            assert len(payload) <= CALLBACK_DATA_LIMIT, payload

    def test_un_nombre_que_no_es_utf8_no_rompe_los_botones(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        """La etiqueta del botón llevaba los sustitutos: Telethon no podía
        serializarla, la cola reintentaba unos 10 s y acababa en error."""
        paths = folders()
        real_path = _write_not_utf8(paths["base"])

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        for row in _rows(kwargs):
            for button in row:
                bytes(button)  # Telethon serializa así los botones al enviarlos
        assert any(label.endswith("caf\\xe9.mp3") for label in _labels_of(kwargs))
        assert real_path in manage.pending_file_actions.values(), \
            "las acciones siguen usando la ruta de verdad"

    def test_un_error_al_leer_la_carpeta_avisa(
        self, manage, folders, sent_messages, make_event, run_async, monkeypatch
    ):
        folders()

        def denied(path):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(manage.os, "listdir", denied)
        _manage(manage, make_event, run_async)

        assert [text for text, _ in _sent(sent_messages)] == [manage.get_text("error_manage_files")]

    def test_un_no_admin_no_ve_nada(self, manage, folders, sent_messages, run_async):
        paths = folders()
        _write(os.path.join(paths["base"], "secreto.txt"))

        run_async(manage.handle_manage_category(_NotAdmin()))

        assert sent_messages == []
        assert manage.pending_file_actions == {}

    def test_en_ingles_los_botones_salen_en_ingles(
        self, manage, folders, sent_messages, make_event, run_async, config_dir
    ):
        folders("video")
        settings.put("language", "EN")

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        labels = _labels_of(kwargs)
        assert not any("Cerrar" in label for label in labels), labels


# ---------------------------------------------------------------------------
# En un foro con temas
# ---------------------------------------------------------------------------

class TestEnUnForo:
    @pytest.mark.parametrize("handler", ["handle_list_category", "handle_manage_category"])
    @pytest.mark.parametrize("files", [0, 1, 81])
    def test_el_listado_va_al_tema_del_menu(
        self, manage, folders, sent_messages, forum_callback, run_async, monkeypatch, handler, files
    ):
        """En un supergrupo con temas, lo que se manda sin reply_to cae en
        General: el menú se pulsaba en un tema y el listado salía en otro. El
        tema sale del mensaje del menú, que se borra: hay que pedirlo antes."""
        paths = folders()
        for i in range(files):
            _write(os.path.join(paths["base"], f"f{i:03d}.txt"))
        click = forum_callback((b"all",))

        async def delete(message, *args, **kwargs):
            if message is click:
                click._client.message = None  # borrado, Telegram ya no lo da
            sent_messages.append(("delete", "", {}))

        monkeypatch.setattr(manage, "safe_delete", delete)

        run_async(getattr(manage, handler)(click))

        listing = [kwargs for kind, _, kwargs in sent_messages if kind != "delete"]
        assert listing and all(kwargs.get("reply_to") == FORUM_TOPIC for kwargs in listing)


# ---------------------------------------------------------------------------
# Robustez
# ---------------------------------------------------------------------------

class TestRobustez:
    @pytest.mark.parametrize("handler", ["handle_list_category", "handle_manage_category"])
    def test_un_enlace_roto_no_impide_listar_el_resto(
        self, manage, folders, sent_messages, make_event, run_async, handler
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "bueno.txt"))
        os.symlink("/no/existe", os.path.join(paths["base"], "roto.txt"))

        run_async(getattr(manage, handler)(make_event(groups=(b"all",))))

        [(text, kwargs)] = _sent(sent_messages)
        if handler == "handle_list_category":
            assert "bueno.txt" in _plain(text) and "roto.txt" not in _plain(text)
        else:
            labels = [label for label in _labels_of(kwargs) if label[0].isdigit()]
            assert len(labels) == 1 and labels[0].endswith("bueno.txt")

    @pytest.mark.parametrize("handler", ["handle_list_category", "handle_manage_category"])
    def test_un_fichero_con_thumb_jpg_en_el_nombre_se_lista(
        self, manage, folders, sent_messages, make_event, run_async, handler
    ):
        """Se saltaba todo lo que llevara "_thumb.jpg", pero las miniaturas del
        bot van a TEMP_DIR: solo se escondían ficheros del usuario, que desde
        el bot ya no se podían enviar, renombrar ni borrar."""
        paths = folders()
        names = ["presupuesto_thumb.jpg.pdf", "IMG_2034_thumb.jpg"]
        for name in names:
            _write(os.path.join(paths["base"], name))

        run_async(getattr(manage, handler)(make_event(groups=(b"all",))))

        [(text, kwargs)] = _sent(sent_messages)
        if handler == "handle_list_category":
            shown = _plain(text)
        else:
            shown = " ".join(label for label in _labels_of(kwargs) if label[0].isdigit())
        assert all(name in shown for name in names), shown
        assert manage.get_text("total_files_space", 2, "20.00 B") in text

    @pytest.mark.parametrize("handler", ["handle_list_category", "handle_manage_category"])
    def test_el_tamano_de_las_carpetas_se_calcula_fuera_del_event_loop(
        self, manage, folders, sent_messages, make_event, run_async, monkeypatch, handler
    ):
        """Recorrer una biblioteca grande (o en un volumen de red) tarda segundos,
        y en el loop paraba los botones, la cola y el progreso de las descargas."""
        paths = folders()
        _write(os.path.join(paths["base"], "Serie", "cap1.mkv"))
        threads = []
        real_size = manage.get_directory_size

        def get_directory_size(path):
            threads.append(threading.get_ident())
            return real_size(path)

        monkeypatch.setattr(manage, "get_directory_size", get_directory_size)
        run_async(getattr(manage, handler)(make_event(groups=(b"all",))))

        assert threads and threading.get_ident() not in threads
        [(text, _)] = _sent(sent_messages)
        assert manage.get_text("total_files_folders_space", 0, 1, "10.00 B") in text

    @pytest.mark.parametrize("handler,tag", [("handle_list_category", "FILE_LIST"),
                                             ("handle_manage_category", "FILE_MANAGE")])
    def test_el_log_cuenta_lo_que_hay_sin_volcar_los_nombres(
        self, manage, folders, make_event, run_async, monkeypatch, handler, tag
    ):
        """Cada listado, y cada vuelta a /manage, escribía la lista entera de
        cada carpeta: con miles de ficheros, una línea de cientos de KB. Desde
        /manage, además, con la etiqueta [LIST] de /list."""
        paths = folders()
        names = ["Contabilidad_2025.xlsx", "Vacaciones en Asturias.mkv"]
        for name in names:
            _write(os.path.join(paths["base"], name))
        logged = []
        monkeypatch.setattr(manage, "debug", logged.append)

        run_async(getattr(manage, handler)(make_event(groups=(b"all",))))

        assert f"[{tag}] Directory {paths['base']} contains 2 items" in logged
        assert not [line for line in logged if any(name in line for name in names)]
        if handler == "handle_manage_category":
            assert not [line for line in logged if line.startswith("[LIST]")]

    @pytest.mark.parametrize("handler", ["handle_list_category", "handle_manage_category"])
    def test_si_no_se_puede_borrar_el_listado_anterior_sigue(
        self, manage, folders, sent_messages, make_event, run_async, monkeypatch, handler
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "a.txt"))
        manage.list_messages[999] = ["ya_borrado", "otro"]

        async def flaky_delete(message, *args, **kwargs):
            if message == "ya_borrado":
                raise RuntimeError("message to delete not found")
            sent_messages.append(("delete", "", {}))

        monkeypatch.setattr(manage, "safe_delete", flaky_delete)
        run_async(getattr(manage, handler)(make_event(groups=(b"all",))))

        assert [kind for kind, _, _ in sent_messages].count("delete") == 2
        assert len(_sent(sent_messages)) == 1
        assert "ya_borrado" not in manage.list_messages[999]

    def test_init_sin_el_pipeline_de_envio_falla_al_arrancar(self, manage, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(manage, "_send_file_fast", None)
        bot = MagicMock()

        with pytest.raises(RuntimeError, match="_send_file_fast"):
            manage.init(bot)
        bot.on.assert_not_called()
