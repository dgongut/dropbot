"""Menús de /list y /manage: qué categorías se ofrecen y qué se lista en cada una.

Cada test monta su propio árbol de carpetas en `tmp_path` y decide qué tipos
tienen carpeta propia tocando `config.DOWNLOAD_PATHS` (el mismo dict que usan
`handlers.manage` y `config.has_own_folder`). Se comprueba lo que vería el
usuario: los mensajes enviados, su texto ya interpretado como Markdown por
Telethon, y los botones con su `callback_data`.
"""

import os

import pytest
from telethon.extensions import markdown

import config
import settings
from button_data import button_data

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

    def test_los_ocultos_y_las_miniaturas_no_se_listan(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], ".oculto"))
        _write(os.path.join(paths["base"], "video_123_thumb.jpg"))
        _write(os.path.join(paths["base"], "visible.mkv"))

        _list(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        plain = _plain(text)
        assert "visible.mkv" in plain
        assert ".oculto" not in plain
        assert "_thumb.jpg" not in plain
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
        _write(os.path.join(paths["video"], "x_thumb.jpg"))

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
