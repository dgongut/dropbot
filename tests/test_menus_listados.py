"""El listado de una carpeta en el explorador de /manage.

Cada test monta su propio árbol de carpetas en `tmp_path` y abre la general
como lo haría el botón de una carpeta (`fm:o:<id>`). Se comprueba lo que vería el
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
from conftest import _patch_messaging

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
def manage(quiet_manage, sent_messages, monkeypatch):
    """`handlers.manage` con el estado compartido limpio antes y después, y
    los envíos del explorador (handlers/files.py) capturados con los suyos."""
    from handlers import files
    _patch_messaging(files, sent_messages, monkeypatch)
    for state in (quiet_manage.pending_file_actions, quiet_manage.pending_renames,
                  quiet_manage.manage_trails):
        state.clear()
    yield quiet_manage
    for state in (quiet_manage.pending_file_actions, quiet_manage.pending_renames,
                  quiet_manage.manage_trails):
        state.clear()


@pytest.fixture
def folders(manage, tmp_path, monkeypatch):
    """Monta las carpetas de descarga en tmp_path.

    `folders("video", "photo")` les da carpeta propia a esos tipos; los demás
    van a la general. Devuelve un dict tipo -> ruta, con "base" para la general.
    """
    def setup(*own, create=True):
        base = str(tmp_path / "downloads")
        monkeypatch.setattr(config, "DOWNLOAD_PATH", base)
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


def _open(manage, make_event, run_async, path, page=b""):
    """Abre `path` en el explorador, como el botón de esa carpeta."""
    from handlers import files
    file_id = manage.register_manage_action(path).encode()
    data = b"o:" + file_id + (b":" + page if page else b"")
    run_async(files.handle_files_callback(make_event(groups=(data,))))


def _manage(manage, make_event, run_async, category=b"all"):
    """Abre la carpeta general; `b"all:2"` es su página 2."""
    _open(manage, make_event, run_async, config.DOWNLOAD_PATH, category.partition(b":")[2])


def _page_buttons(manage, payloads):
    """Las páginas a las que llevan los botones de página de la carpeta general."""
    prefix = b"fm:o:" + manage.register_manage_action(config.DOWNLOAD_PATH).encode() + b":"
    return [int(p[len(prefix):]) for p in payloads if p.startswith(prefix)]


def _sent(sent_messages, kinds=("send_message", "reply", "edit")):
    return [(text, kwargs) for kind, text, kwargs in sent_messages if kind in kinds]


class TestCarpeta:
    def test_una_carpeta_vacia_lo_dice(self, manage, folders, sent_messages, make_event, run_async):
        folders()

        _manage(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        assert manage.get_text("folder_empty") in text
        assert not any(p.startswith(b"fileact:") for p in _payloads_of(kwargs))

    def test_una_carpeta_que_no_existe_no_revienta(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        folders(create=False)

        _manage(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert text == manage.get_text("error_item_not_found")

    def test_un_boton_por_elemento_en_orden_y_registrado(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        for name in ("zeta.mkv", "Alfa.mkv", "medio.mkv"):
            _write(os.path.join(paths["base"], name))
        os.makedirs(os.path.join(paths["base"], "Carpeta"))
        _write(os.path.join(paths["base"], ".oculto"))

        _manage(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        rows = _rows(kwargs)
        # Un fichero abre sus acciones; una carpeta se abre (fm:o)
        file_rows = [row for row in rows if row[0].text[0].isdigit()]
        labels = [row[0].text for row in file_rows]
        # Número, icono, nombre y lo que ocupa
        assert [label.split(" ", 2)[2].split(" · ")[0] for label in labels] == ["Alfa.mkv", "Carpeta", "medio.mkv", "zeta.mkv"]
        assert labels[0].startswith("1. ") and labels[1].startswith("2. 📁 Carpeta · ")
        assert all(" · " in label and label.endswith(" B") for label in labels)
        assert [button_data(row[0]).split(b":")[0] for row in file_rows] == [b"fileact", b"fm", b"fileact", b"fileact"]
        resolved = [manage.pending_file_actions[button_data(row[0]).rsplit(b":", 1)[1].decode()]
                    for row in file_rows]
        assert resolved == [os.path.join(paths["base"], name) for name in ("Alfa.mkv", "Carpeta", "medio.mkv", "zeta.mkv")]
        assert manage.get_text("total_files_folders_space", 3, 1, "30.00 B") in text

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

        _open(manage, make_event, run_async, paths["video"])
        [shown_id] = [p.split(b":", 1)[1] for p in _payloads_of(_sent(sent_messages)[0][1])
                      if p.startswith(b"fileact:")]
        _open(manage, make_event, run_async, paths["audio"])
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
        # Los dos ficheros y la propia carpeta
        assert len(manage.pending_file_actions) == 3

    def test_sin_carpetas_el_total_solo_cuenta_ficheros(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "a.txt"), 2048)

        _manage(manage, make_event, run_async)

        [(text, _)] = _sent(sent_messages)
        assert manage.get_text("total_files_space", 1, "2.00 KB") in text

    def test_un_listado_largo_va_por_paginas(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        """Antes, con más de 80 elementos no se podía gestionar ninguno."""
        paths = folders()
        for i in range(85):
            _write(os.path.join(paths["base"], f"f{i:02d}.txt"))
        os.makedirs(os.path.join(paths["base"], "una_carpeta"))

        _manage(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        payloads = _payloads_of(kwargs)
        assert sum(1 for p in payloads if p.startswith(b"fileact:")) == manage.MANAGE_PAGE_SIZE
        assert manage.get_text("total_files_folders_space", 85, 1, "850.00 B") in text
        # 86 elementos de 20 en 20: cinco páginas, y de la primera solo se avanza
        assert "1/5" in _labels_of(kwargs)
        assert _page_buttons(manage, payloads) == [1]
        # Telegram no admite más de 100 botones por mensaje
        assert len(payloads) <= 100

    def test_cada_pagina_enseña_los_suyos_y_sigue_la_numeracion(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        for i in range(45):
            _write(os.path.join(paths["base"], f"f{i:02d}.txt"))

        _manage(manage, make_event, run_async, category=b"all:2")

        [(_, kwargs)] = _sent(sent_messages)
        labels = [label for label in _labels_of(kwargs) if label[0].isdigit() and ". " in label]
        assert [label.split(" ", 2)[2].split(" · ")[0] for label in labels] == [f"f{i:02d}.txt" for i in range(40, 45)]
        assert labels[0].startswith("41. ")
        assert "3/3" in _labels_of(kwargs)
        assert _page_buttons(manage, _payloads_of(kwargs)) == [1]

    def test_una_pagina_que_ya_no_existe_enseña_la_ultima(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        for i in range(25):
            _write(os.path.join(paths["base"], f"f{i:02d}.txt"))

        _manage(manage, make_event, run_async, category=b"all:9")

        [(_, kwargs)] = _sent(sent_messages)
        assert sum(1 for p in _payloads_of(kwargs) if p.startswith(b"fileact:")) == 5

    def test_una_pagina_justa_no_lleva_botones_de_pagina(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        for i in range(manage.MANAGE_PAGE_SIZE):
            _write(os.path.join(paths["base"], f"f{i:02d}.txt"))

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        payloads = _payloads_of(kwargs)
        assert sum(1 for p in payloads if p.startswith(b"fileact:")) == manage.MANAGE_PAGE_SIZE
        assert _page_buttons(manage, payloads) == []
        assert b"fm:ma:" + manage.register_manage_action(paths["base"]).encode() in payloads

    def test_la_etiqueta_recorta_los_nombres_largos(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "a" * 60 + ".mkv"))

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        label = _labels_of(kwargs)[0]
        # Más corto que antes, para que quepa el tamaño detrás
        assert ("a" * 22 + "... · ") in label and "a" * 23 not in label

    def test_los_callbacks_caben_con_nombres_largos_y_unicode(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        names = ["Ñandú_" + "á" * 120 + ".mkv", "日本語のファイル名" * 8 + ".mp4",
                 "🎬" * 60 + ".mkv", "x" * 240]
        for name in names:
            _write(os.path.join(paths["base"], name))
        os.makedirs(os.path.join(paths["base"], "📁" * 50))

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        payloads = _payloads_of(kwargs)
        assert sum(1 for p in payloads if p.startswith(b"fileact:")) == 4
        assert sum(1 for p in payloads if p.startswith(b"fm:o:")) == 1
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
        assert any("caf\\xe9.mp3 · " in label for label in _labels_of(kwargs))
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
        from handlers import files
        paths = folders()
        _write(os.path.join(paths["base"], "secreto.txt"))

        run_async(files.handle_files_callback(_NotAdmin()))

        assert sent_messages == []
        assert manage.pending_file_actions == {}

    def test_en_ingles_los_botones_salen_en_ingles(
        self, manage, folders, sent_messages, make_event, run_async, config_dir
    ):
        folders()
        settings.put("language", "EN")

        _manage(manage, make_event, run_async)

        [(_, kwargs)] = _sent(sent_messages)
        labels = _labels_of(kwargs)
        assert not any("Cerrar" in label for label in labels), labels


# ---------------------------------------------------------------------------
# En un foro con temas
# ---------------------------------------------------------------------------

class TestEnUnForo:
    @pytest.mark.parametrize("count", [0, 1, 81])
    def test_el_listado_se_queda_en_el_tema_del_boton(
        self, manage, folders, sent_messages, forum_callback, run_async, count
    ):
        """En un supergrupo con temas, lo que se manda sin reply_to cae en
        General. El explorador pinta la carpeta en el propio mensaje del
        botón: se queda en su tema sin tener que decirlo, y no se borra nada."""
        from handlers import files
        paths = folders()
        for i in range(count):
            _write(os.path.join(paths["base"], f"f{i:03d}.txt"))
        file_id = manage.register_manage_action(paths["base"]).encode()

        run_async(files.handle_files_callback(forum_callback((b"o:" + file_id,))))

        assert [kind for kind, _, _ in sent_messages] == ["edit"]


# ---------------------------------------------------------------------------
# Robustez
# ---------------------------------------------------------------------------

class TestRobustez:
    def test_un_enlace_roto_no_impide_listar_el_resto(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        paths = folders()
        _write(os.path.join(paths["base"], "bueno.txt"))
        os.symlink("/no/existe", os.path.join(paths["base"], "roto.txt"))

        _manage(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        labels = [label for label in _labels_of(kwargs) if label[0].isdigit()]
        assert len(labels) == 1 and "bueno.txt · " in labels[0]

    def test_un_fichero_con_thumb_jpg_en_el_nombre_se_lista(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        """Se saltaba todo lo que llevara "_thumb.jpg", pero las miniaturas del
        bot van a TEMP_DIR: solo se escondían ficheros del usuario, que desde
        el bot ya no se podían enviar, renombrar ni borrar."""
        paths = folders()
        names = ["presupuesto_thumb.jpg.pdf", "IMG_2034_thumb.jpg"]
        for name in names:
            _write(os.path.join(paths["base"], name))

        _manage(manage, make_event, run_async)

        [(text, kwargs)] = _sent(sent_messages)
        shown = " ".join(label for label in _labels_of(kwargs) if label[0].isdigit())
        assert all(name[:22] in shown for name in names), shown
        assert manage.get_text("total_files_space", 2, "20.00 B") in text

    def test_el_tamano_de_las_carpetas_se_calcula_fuera_del_event_loop(
        self, manage, folders, sent_messages, make_event, run_async, monkeypatch
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
        _manage(manage, make_event, run_async)

        assert threads and threading.get_ident() not in threads
        [(text, _)] = _sent(sent_messages)
        assert manage.get_text("total_files_folders_space", 0, 1, "10.00 B") in text

    def test_el_log_cuenta_lo_que_hay_sin_volcar_los_nombres(
        self, manage, folders, make_event, run_async, monkeypatch
    ):
        """Cada listado, y cada vuelta a /manage, escribía la lista entera de
        cada carpeta: con miles de ficheros, una línea de cientos de KB."""
        paths = folders()
        names = ["Contabilidad_2025.xlsx", "Vacaciones en Asturias.mkv"]
        for name in names:
            _write(os.path.join(paths["base"], name))
        logged = []
        monkeypatch.setattr(manage, "debug", logged.append)

        _manage(manage, make_event, run_async)

        assert f"[FILE_MANAGE] Directory {paths['base']} contains 2 items" in logged
        assert not [line for line in logged if any(name in line for name in names)]

    def test_cambiar_de_pagina_edita_el_mensaje_en_vez_de_mandar_otro(
        self, manage, folders, sent_messages, make_event, run_async
    ):
        """Al cambiar de página el chat saltaba: el listado se borraba y
        llegaba uno nuevo."""
        paths = folders()
        for i in range(30):
            _write(os.path.join(paths["base"], f"f{i:02d}.txt"))

        _manage(manage, make_event, run_async, category=b"all:1")

        assert [kind for kind, _, _ in sent_messages] == ["edit"]

    def test_init_sin_el_pipeline_de_envio_falla_al_arrancar(self, manage, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(manage, "_send_file_fast", None)
        bot = MagicMock()

        with pytest.raises(RuntimeError, match="_send_file_fast"):
            manage.init(bot)
        bot.on.assert_not_called()
