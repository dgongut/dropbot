"""La operativa de archivos desde Telegram: el selector de carpetas, /manage
con carpetas, mover, renombrar todo, las acciones sobre un listado, /find, lo
que se hace con una descarga recién llegada y sus ajustes."""

import os

import pytest

import config
import folders
import settings
from button_data import button_data
from conftest import ButtonPress, TextMessage, buttons_of

CALLBACK_DATA_LIMIT = 64


def _touch(path, size=4):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    return path


@pytest.fixture
def press(library, run_async):
    """Pulsa un botón por su callback data, en el handler que le toca."""
    handlers = {b"pk": library.picker.handle_picker, b"fm": library.files.handle_files_callback,
                b"arr": library.arrivals.handle_arrival_callback, b"cfg": library.settings.handle_settings_callback,
                b"fileact": library.manage.handle_file_action, b"rename": library.manage.handle_rename_file,
                b"home": library.home.handle_home}

    def do(data, **kwargs):
        event = ButtonPress(data, **kwargs)
        run_async(handlers[data.split(b":")[0]](event))
        return event
    return do


@pytest.fixture
def tap(press, sent_messages):
    """Pulsa, del último mensaje con botones, el que lleva ese texto."""
    def do(label):
        matches = [data for text, data in buttons_of(sent_messages) if label in text]
        assert matches, f"no hay ningún botón con {label!r}: {[t for t, _ in buttons_of(sent_messages)]}"
        return press(matches[0])
    return do


@pytest.fixture
def write(library, run_async):
    """El admin escribe un texto: lo atiende quien lo esté esperando."""
    def do(text, **kwargs):
        message = TextMessage(text, **kwargs)
        if not library.inputs.is_input_answer(message):
            return False
        run_async(library.inputs.handle_input(message))
        return True
    return do


def _last(sent_messages):
    return sent_messages[-1][1]


def _register(library, path):
    return library.state.register_manage_action(path).encode()


class TestSelector:
    def _open(self, library, run_async, chosen, **kwargs):
        async def on_choose(event, picker, path):
            chosen.append(path)

        async def on_cancel(event, picker):
            chosen.append(None)

        library.picker.register("test", on_choose, on_cancel)
        picker_id = library.picker.open_picker("test", "¿Dónde?", **kwargs)
        run_async(library.picker.show(ButtonPress(b"x:1"), picker_id))
        return picker_id

    def test_ofrece_las_favoritas_y_las_de_descarga(self, library, run_async, sent_messages):
        folders.add_favorite(os.path.join(library.media, "Series"))

        self._open(library, run_async, [], suggested=[os.path.join(library.video, "Mad Men")])

        labels = [text for text, _ in buttons_of(sent_messages)]
        assert [label[0] for label in labels[:4]] == ["✨", "⭐", "📂", "📂"]
        assert all(len(data) <= CALLBACK_DATA_LIMIT for _, data in buttons_of(sent_messages))

    def test_se_entra_se_baja_se_sube_y_se_elige(self, library, run_async, sent_messages, tap):
        os.makedirs(os.path.join(library.base, "Series", "Mad Men"))
        chosen = []
        self._open(library, run_async, chosen)

        tap(os.path.basename(library.base)[-10:])
        tap("Series")
        tap("Mad Men")
        tap("⬆️")
        assert f"`{os.path.join(library.base, 'Series')}`" in _last(sent_messages)
        tap("✅")

        assert chosen == [os.path.join(library.base, "Series")]
        assert library.state.pickers == {}

    def test_de_una_carpeta_conocida_se_sube_a_la_primera_pantalla(self, library, run_async, sent_messages, tap):
        self._open(library, run_async, [])
        tap(os.path.basename(library.video)[-10:])

        tap("⬆️")

        assert library.picker.get_text("picker_choose") in _last(sent_messages)

    def test_no_deja_elegir_la_carpeta_donde_ya_esta(self, library, run_async, sent_messages, tap, press):
        chosen = []
        picker_id = self._open(library, run_async, chosen, exclude=library.base)

        tap(os.path.basename(library.base)[-10:])

        assert not any("✅" in text for text, _ in buttons_of(sent_messages))
        press(f"pk:{picker_id}:ok".encode())
        assert chosen == []

    def test_crear_una_carpeta_escribiendo_su_nombre(self, library, run_async, sent_messages, tap, write):
        folders.add_favorite(library.media)
        chosen = []
        self._open(library, run_async, chosen)
        tap(os.path.basename(library.media)[-10:])
        tap("➕")

        assert write("Series nuevas")
        tap("✅")

        assert chosen == [os.path.join(library.media, "Series nuevas")]
        assert os.path.isdir(chosen[0])

    def test_una_ruta_escrita_fuera_de_un_volumen_se_rechaza(self, library, run_async, sent_messages, tap, write):
        chosen = []
        self._open(library, run_async, chosen)
        tap("📝")

        assert write(os.path.join(library.outside, "x"))

        assert library.picker.get_text("folder_error_outside") in _last(sent_messages)
        assert not os.path.exists(os.path.join(library.outside, "x"))
        assert chosen == []

    def test_cancelar_avisa_a_quien_lo_abrio(self, library, run_async, tap):
        chosen = []
        self._open(library, run_async, chosen)
        tap("❌")
        assert chosen == [None]

    def test_un_selector_de_antes_de_reiniciar_no_hace_nada(self, library, press, sent_messages):
        press(b"pk:p999:ok")
        assert sent_messages == []

    def test_muchas_subcarpetas_van_por_paginas(self, library, run_async, sent_messages, tap):
        for index in range(20):
            os.makedirs(os.path.join(library.base, f"c{index:02d}"))
        self._open(library, run_async, [])
        tap(os.path.basename(library.base)[-10:])

        labels = [text for text, _ in buttons_of(sent_messages)]
        assert "1/3" in labels and sum(1 for label in labels if label.startswith("📁")) == library.picker.PAGE_SIZE
        tap("»")
        assert "📁 c08" in [text for text, _ in buttons_of(sent_messages)]


class TestTextoPedido:
    def test_solo_vale_en_su_chat_y_no_los_comandos_ni_los_enlaces(self, library):
        library.inputs.ask(ButtonPress(b"x:1", message_id=7), "template", kind="movie")

        assert library.inputs.is_input_answer(TextMessage("{titulo}"))
        assert library.inputs.is_input_answer(TextMessage("/downloads/Series"))
        assert not library.inputs.is_input_answer(TextMessage("/list"))
        assert not library.inputs.is_input_answer(TextMessage("/find@bot casa"))
        assert not library.inputs.is_input_answer(TextMessage("https://ejemplo.com/x"))
        assert not library.inputs.is_input_answer(TextMessage("hola", chat_id=77))
        assert not library.inputs.is_input_answer(TextMessage("hola", sender_id=5))

    def test_en_un_grupo_solo_vale_citando_el_mensaje(self, library):
        library.inputs.ask(ButtonPress(b"x:1", message_id=7), "template", kind="movie")

        assert not library.inputs.is_input_answer(TextMessage("hola", is_private=False))
        assert library.inputs.is_input_answer(TextMessage("hola", is_private=False, reply_to_msg_id=7))

    def test_caduca_salvo_que_se_cite(self, library, monkeypatch):
        library.inputs.ask(ButtonPress(b"x:1", message_id=7), "template", kind="movie")
        clock = library.inputs.time.monotonic()
        monkeypatch.setattr(library.inputs.time, "monotonic", lambda: clock + library.inputs.INPUT_TIMEOUT + 1)

        assert not library.inputs.is_input_answer(TextMessage("hola"))
        assert library.inputs.is_input_answer(TextMessage("hola", reply_to_msg_id=7))

    def test_pedir_un_texto_olvida_el_renombrado_a_medias_y_al_reves(self, library, press):
        target = _touch(os.path.join(library.base, "a.txt"))
        file_id = _register(library, target)
        press(b"rename:" + file_id)
        assert library.state.pending_renames

        library.inputs.ask(ButtonPress(b"x:1"), "template", kind="movie")
        assert not library.state.pending_renames

        press(b"rename:" + file_id)
        assert not library.state.pending_inputs


class TestCarpetasEnManage:
    def test_una_carpeta_se_abre_y_ensena_lo_que_lleva(self, library, press, sent_messages):
        folder = os.path.join(library.base, "Serie")
        _touch(os.path.join(folder, "b.mkv"))
        os.makedirs(os.path.join(folder, "Extras"))
        _touch(os.path.join(folder, ".oculto"))

        press(b"fm:o:" + _register(library, folder))

        assert f"`{folder}`" in _last(sent_messages)
        buttons = buttons_of(sent_messages)
        assert [text for text, _ in buttons[:2]] == ["1. 📽️ b.mkv · 4 B", "2. 📁 Extras · 0 B"]
        assert buttons[0][1].startswith(b"fileact:") and buttons[1][1].startswith(b"fm:o:")
        # Subir desde una carpeta que cuelga de descargas lleva a la de descargas
        assert (library.manage.get_text("button_picker_up"), b"fm:o:" + _register(library, library.base)) in buttons
        assert all(len(data) <= CALLBACK_DATA_LIMIT for _, data in buttons)

    def test_desde_dentro_se_vuelve_a_la_carpeta_de_encima(self, library, press, sent_messages):
        inner = os.path.join(library.base, "Serie", "T1")
        target = _touch(os.path.join(inner, "01.mkv"))

        press(b"fileact:" + _register(library, target))
        back = [data for text, data in buttons_of(sent_messages) if text == library.manage.get_text("button_back_to_manage")]
        assert back == [b"fm:o:" + _register(library, inner)]

        press(b"fm:o:" + _register(library, inner))
        up = [data for text, data in buttons_of(sent_messages) if text == library.manage.get_text("button_picker_up")]
        assert up == [b"fm:o:" + _register(library, os.path.join(library.base, "Serie"))]

    def test_una_carpeta_vacia_lo_dice(self, library, press, sent_messages):
        folder = os.path.join(library.base, "Vacía")
        os.makedirs(folder)

        press(b"fm:o:" + _register(library, folder))

        assert library.files.get_text("folder_empty") in _last(sent_messages)
        assert not any(data.startswith(b"fm:ma:") for _, data in buttons_of(sent_messages))

    def test_una_carpeta_larga_va_por_paginas(self, library, press, sent_messages):
        folder = os.path.join(library.base, "Muchos")
        for index in range(45):
            _touch(os.path.join(folder, f"f{index:02d}.txt"))
        file_id = _register(library, folder)

        press(b"fm:o:" + file_id + b":2")

        labels = [text for text, _ in buttons_of(sent_messages)]
        assert labels[0] == "41. 📚 f40.txt · 4 B" and "3/3" in labels
        assert b"fm:o:" + file_id + b":1" in [data for _, data in buttons_of(sent_messages)]

    def test_nueva_carpeta_desde_la_que_esta_abierta(self, library, press, tap, write, sent_messages):
        """Para crear una carpeta había que pasar por «Mover» o por los ajustes."""
        _touch(os.path.join(library.base, "peli.mkv"))
        press(b"fm:o:" + _register(library, library.base))

        tap("➕")
        assert library.files.get_text("manage_new_folder_ask", f"`{library.base}`") == _last(sent_messages)
        # Un nombre que no vale se dice, y se sigue esperando otro
        assert write("a/b")
        assert library.files.get_text("rename_invalid_chars") in _last(sent_messages)
        assert write("Pantera Rosa")

        assert os.path.isdir(os.path.join(library.base, "Pantera Rosa"))
        # De vuelta en la carpeta, con la nueva ya dentro
        assert any("📁 Pantera Rosa" in text for text, _ in buttons_of(sent_messages))
        assert not write("otra"), "ya no se espera ningún nombre"

    def test_nueva_carpeta_no_pisa_una_que_ya_existe(self, library, press, tap, write, sent_messages):
        os.makedirs(os.path.join(library.base, "Series"))
        press(b"fm:o:" + _register(library, library.base))

        tap("➕")
        assert write("Series")

        assert library.files.get_text("rename_already_exists_title") in _last(sent_messages)

    def test_lo_que_ya_no_existe(self, library, press, sent_messages):
        press(b"fm:o:m999_1")
        assert library.files.get_text("error_item_not_found") in _last(sent_messages)


class TestMoverDesdeManage:
    def test_elegir_carpeta_y_mover(self, library, press, tap, sent_messages):
        target = _touch(os.path.join(library.base, "peli.mkv"))
        file_id = _register(library, target)

        press(b"fm:mv:" + file_id)
        tap(os.path.basename(library.video)[-10:])
        tap("✅")

        moved = os.path.join(library.video, "peli.mkv")
        assert os.path.isfile(moved) and not os.path.exists(target)
        assert library.files.get_text("move_done", "peli.mkv", library.video) in _last(sent_messages)
        # El botón de siempre sigue valiendo, ahora para la ruta nueva
        assert library.state.pending_file_actions[file_id.decode()] == moved

    def test_cancelar_vuelve_a_las_acciones_sin_mover(self, library, press, tap, sent_messages):
        target = _touch(os.path.join(library.base, "peli.mkv"))
        press(b"fm:mv:" + _register(library, target))

        tap("❌")

        assert os.path.exists(target)
        assert any(data.startswith(b"rename:") for _, data in buttons_of(sent_messages))

    def test_si_ya_hay_uno_igual_no_lo_pisa_y_lo_dice(self, library, press, tap, sent_messages):
        target = _touch(os.path.join(library.base, "peli.mkv"), size=1)
        _touch(os.path.join(library.video, "peli.mkv"), size=9)
        press(b"fm:mv:" + _register(library, target))
        tap(os.path.basename(library.video)[-10:])

        tap("✅")

        assert library.files.get_text("move_error_exists", "peli.mkv") in _last(sent_messages)
        assert os.path.getsize(os.path.join(library.video, "peli.mkv")) == 9 and os.path.exists(target)

    def test_un_episodio_lleva_delante_la_carpeta_de_su_serie(self, library, press, sent_messages):
        show = os.path.join(library.video, "Chicago Fire")
        _touch(os.path.join(show, "14x03 - Chicago Fire.mkv"))
        target = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.mkv"))

        press(b"fm:mv:" + _register(library, target))

        assert buttons_of(sent_messages)[0][0] == f"✨ {library.picker.short_path(show)}"

    def test_con_las_series_desactivadas_no_la_propone(self, library, press, sent_messages):
        settings.put("series.suggest", False)
        _touch(os.path.join(library.video, "Chicago Fire", "14x03 - Chicago Fire.mkv"))
        target = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.mkv"))

        press(b"fm:mv:" + _register(library, target))

        assert not any(text.startswith("✨") for text, _ in buttons_of(sent_messages))


class TestRenombrarConLaSugerencia:
    def test_renombrar_la_ofrece_y_un_toque_la_aplica(self, library, press, tap, sent_messages):
        target = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.mkv"))
        _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.srt"))
        file_id = _register(library, target)

        press(b"rename:" + file_id)
        assert "`The Matrix (1999) - 1080p.mkv`" in _last(sent_messages)
        tap("✅")

        assert sorted(os.listdir(library.base)) == ["The Matrix (1999) - 1080p.mkv", "The Matrix (1999) - 1080p.srt"]
        assert library.manage.get_text("rename_subtitles_too", 1) in _last(sent_messages)
        assert library.state.pending_file_actions[file_id.decode()].endswith("The Matrix (1999) - 1080p.mkv")
        # Ya no se espera el nombre escrito a mano
        assert not library.state.pending_renames

    def test_sin_sugerencia_no_hay_boton(self, library, press, sent_messages):
        press(b"rename:" + _register(library, _touch(os.path.join(library.base, "notas.txt"))))
        assert [data.split(b":")[0] for _, data in buttons_of(sent_messages)] == [b"fileact"]

    def test_si_el_nombre_ya_existe_no_pisa(self, library, press, sent_messages):
        target = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.mkv"), size=1)
        _touch(os.path.join(library.base, "The Matrix (1999) - 1080p.mkv"), size=9)

        press(b"fm:rs:" + _register(library, target))

        assert library.files.get_text("rename_already_exists_title") in _last(sent_messages)
        assert os.path.exists(target)

    def test_renombrar_a_mano_tambien_se_lleva_los_subtitulos(self, library, press, run_async, sent_messages):
        target = _touch(os.path.join(library.base, "peli.mkv"))
        _touch(os.path.join(library.base, "peli.es.srt"))
        press(b"rename:" + _register(library, target))

        message = TextMessage("Otra.mkv")
        assert library.manage.is_rename_answer(message)
        run_async(library.manage.handle_rename_input(message))

        assert sorted(os.listdir(library.base)) == ["Otra.es.srt", "Otra.mkv"]


class TestRenombrarTodo:
    @pytest.fixture
    def series(self, library):
        top = os.path.join(library.base, "Mad.Men.2007.COMPLETE.1080p.NF.WEB-DL")
        _touch(os.path.join(top, "Mad.Men.2007.S01.1080p", "Mad.Men.S01E01.1080p.mkv"))
        _touch(os.path.join(top, "Mad.Men.2007.S01.1080p", "Mad.Men.S01E02.1080p.mkv"))
        return top

    def test_vista_previa_y_confirmar(self, library, series, press, tap, sent_messages):
        file_id = _register(library, series)

        press(b"fm:ra:" + file_id)
        preview = _last(sent_messages)
        assert library.files.get_text("rename_all_count", 2, 2) in preview
        assert "`Mad Men (2007) - 1080p`" in preview and "`1x01 - Mad Men - 1080p.mkv`" in preview
        assert os.path.isdir(series), "la vista previa no toca nada"
        tap("✅")

        renamed = os.path.join(library.base, "Mad Men (2007) - 1080p")
        assert sorted(os.listdir(os.path.join(renamed, "T1 - Mad Men - 1080p"))) == [
            "1x01 - Mad Men - 1080p.mkv", "1x02 - Mad Men - 1080p.mkv"]
        assert library.files.get_text("rename_all_done", 2, 2) in _last(sent_messages)
        assert library.state.pending_file_actions[file_id.decode()] == renamed

    def test_se_renombra_lo_que_se_enseño_y_no_lo_que_llegue_despues(self, library, series, press, tap):
        press(b"fm:ra:" + _register(library, series))
        late = _touch(os.path.join(series, "Mad.Men.2007.S01.1080p", "Mad.Men.S01E03.1080p.mkv"))

        tap("✅")

        assert os.path.basename(late) in os.listdir(os.path.join(library.base, "Mad Men (2007) - 1080p", "T1 - Mad Men - 1080p"))

    def test_un_doble_toque_no_renombra_dos_veces(self, library, series, press, sent_messages):
        file_id = _register(library, series)
        press(b"fm:ra:" + file_id)
        press(b"fm:rc:i." + file_id)
        count = len(sent_messages)

        press(b"fm:rc:i." + file_id)

        assert len(sent_messages) == count

    def test_sin_nada_que_renombrar_lo_dice(self, library, press, sent_messages):
        folder = os.path.join(library.base, "Documentos")
        _touch(os.path.join(folder, "notas.txt"))

        press(b"fm:ra:" + _register(library, folder))

        assert library.files.get_text("rename_all_nothing") in _last(sent_messages)
        assert not any(data.startswith(b"fm:rc:") for _, data in buttons_of(sent_messages))


class TestAccionesConTodos:
    @pytest.fixture
    def folder(self, library):
        path = os.path.join(library.base, "Pendientes")
        for name in ("a.mkv", "b.mkv", "Sub/c.mkv"):
            _touch(os.path.join(path, name), size=10)
        return path

    def test_moverlos_todos(self, library, folder, press, tap, sent_messages):
        ref = _register(library, folder)
        _touch(os.path.join(library.video, "b.mkv"), size=99)

        press(b"fm:ma:" + ref)
        assert library.files.get_text("mass_title", 3) in _last(sent_messages)
        tap("📦")
        tap(os.path.basename(library.video)[-10:])
        tap("✅")

        assert sorted(os.listdir(library.video)) == ["Sub", "a.mkv", "b.mkv"]
        assert os.listdir(folder) == ["b.mkv"], "el que chocaba se queda donde estaba"
        assert os.path.getsize(os.path.join(library.video, "b.mkv")) == 99
        assert library.files.get_text("mass_move_done", 2, 3, library.video) in _last(sent_messages)
        assert library.files.get_text("move_error_exists", "b.mkv") in _last(sent_messages)

    def test_borrarlos_pide_confirmar_y_borra_lo_que_se_enseño(self, library, folder, press, tap, sent_messages):
        ref = _register(library, folder)
        press(b"fm:md:" + ref)
        assert library.files.get_text("mass_delete_question", 3, "30.00 B") in _last(sent_messages)
        assert len(os.listdir(folder)) == 3
        late = _touch(os.path.join(folder, "llega_despues.mkv"))

        tap("✅")

        assert os.listdir(folder) == ["llega_despues.mkv"] and os.path.exists(late)
        assert library.files.get_text("mass_delete_done", 3, 3) in _last(sent_messages)

    def test_sin_haber_visto_la_confirmacion_no_se_borra_nada(self, library, folder, press, sent_messages):
        press(b"fm:mdc:" + _register(library, folder))
        assert len(os.listdir(folder)) == 3 and sent_messages == []

    def test_una_confirmacion_caducada_avisa_y_un_doble_toque_no(
        self, library, folder, press, tap, monkeypatch
    ):
        """«Sí, eliminar» de antes de reiniciar el bot no hacía nada ni decía
        por qué. El segundo toque de un doble toque sigue sin decir nada."""
        answers = []

        async def answer(event, *args, **kwargs):
            answers.append((args, kwargs))

        monkeypatch.setattr(library.files, "safe_answer", answer)
        ref = _register(library, folder)

        press(b"fm:mdc:" + ref)
        assert answers == [((library.files.get_text("picker_expired"),), {"alert": True})]
        assert len(os.listdir(folder)) == 3

        press(b"fm:md:" + ref)
        press(b"fm:mdc:" + ref)
        press(b"fm:mdc:" + ref)
        assert answers[-2:] == [((), {}), ((), {})]

    def test_un_aviso_de_llegada_caducado_lo_dice(self, library, press, monkeypatch):
        answers = []

        async def answer(event, *args, **kwargs):
            answers.append((args, kwargs))

        monkeypatch.setattr(library.arrivals, "safe_answer", answer)

        press(b"arr:undo:a1_0000")

        assert answers == [((library.arrivals.get_text("picker_expired"),), {"alert": True})]

    def test_los_ids_cortos_cambian_de_un_arranque_a_otro(self, library, monkeypatch):
        """El contador vuelve a empezar al reiniciar: sin la marca del arranque,
        el botón de un aviso viejo resolvía al aviso nuevo con ese número."""
        first = library.state.short_id("a")
        monkeypatch.setattr(library.state, "_short_seq", 0)
        monkeypatch.setattr(library.state, "_run_token", "ffff")

        assert library.state.short_id("a") != first

    def test_la_confirmacion_de_otro_admin_no_vale(self, library, folder, press, run_async):
        ref = _register(library, folder)
        press(b"fm:md:" + ref)

        other = ButtonPress(b"fm:mdc:" + ref, sender_id=4242)
        run_async(library.files.mass_delete_confirmed(other, ref.decode()))

        assert len(os.listdir(folder)) == 3

    def test_cancelar_el_borrado_vuelve_al_menu(self, library, folder, press, tap, sent_messages):
        press(b"fm:md:" + _register(library, folder))
        tap("❌")
        assert library.files.get_text("mass_title", 3) in _last(sent_messages)
        assert len(os.listdir(folder)) == 3

    def test_un_listado_vacio(self, library, press, sent_messages):
        empty = os.path.join(library.base, "Vacía")
        os.makedirs(empty)
        press(b"fm:ma:" + _register(library, empty))
        assert library.files.get_text("mass_nothing") in _last(sent_messages)


class TestBuscar:
    def _find(self, library, run_async, query):
        run_async(library.files.send_find(ButtonPress(b"x:1"), query, None))

    def test_los_resultados_abren_sus_acciones(self, library, run_async, sent_messages, press):
        _touch(os.path.join(library.base, "Series", "La Casa de Papel", "1x02.mkv"))
        _touch(os.path.join(library.video, "Casa.Tomada.mkv"))

        self._find(library, run_async, "casa")

        buttons = buttons_of(sent_messages)
        assert [text for text, _ in buttons[:2]] == ["1. 📽️ Casa.Tomada.mkv · 4 B", "2. 📁 La Casa de Papel · 4 B"]
        assert buttons[0][1].startswith(b"fileact:") and buttons[1][1].startswith(b"fm:o:")
        mass = [data for _, data in buttons if data.startswith(b"fm:ma:q.")]
        press(mass[0])
        assert library.files.get_text("mass_title", 2) in _last(sent_messages)

    def test_sin_texto_explica_como_se_usa(self, library, run_async, sent_messages):
        self._find(library, run_async, "")
        assert _last(sent_messages) == library.files.get_text("find_usage")

    def test_sin_resultados(self, library, run_async, sent_messages):
        self._find(library, run_async, "nada de nada")
        assert library.files.get_text("find_none", "nada de nada") in _last(sent_messages)

    def test_los_resultados_van_por_paginas(self, library, run_async, sent_messages, tap):
        for index in range(25):
            _touch(os.path.join(library.base, f"casa {index:02d}.txt"))
        self._find(library, run_async, "casa")

        tap("»")

        assert buttons_of(sent_messages)[0][0] == "21. 📚 casa 20.txt · 4 B"


class TestDescargaRecienLlegada:
    def _arrive(self, library, run_async, path):
        arrangement = run_async(library.arrivals.arrange(path))
        run_async(library.arrivals.offer(TextMessage("x"), arrangement))
        return arrangement

    def test_sin_nada_activado_no_toca_ni_pregunta(self, library, run_async, sent_messages):
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.mkv"))

        arrangement = self._arrive(library, run_async, path)

        assert arrangement.path == path and arrangement.notes == [] and sent_messages == []

    def test_renombrado_automatico(self, library, run_async, sent_messages):
        settings.put("rename.auto", True)
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.mkv"))
        send_id = library.state.register_pending_send(path)

        arrangement = self._arrive(library, run_async, path)

        assert arrangement.path == os.path.join(library.base, "The Matrix (1999) - 1080p.mkv")
        assert os.path.isfile(arrangement.path) and not os.path.exists(path)
        assert arrangement.notes == [library.arrivals.get_text("arrival_renamed", "The.Matrix.1999.1080p.mkv")]
        # El botón de enviar a Telegram que ya se hubiera enseñado sigue valiendo
        assert library.state.pending_files[send_id] == arrangement.path
        assert sent_messages == []

    @pytest.mark.parametrize("name", ["foto.2024.1080p.jpg", "Ubuntu.2024.torrent", "notas.txt"])
    def test_solo_renombra_videos(self, library, run_async, name):
        settings.put("rename.auto", True)
        path = _touch(os.path.join(library.base, name))
        assert self._arrive(library, run_async, path).path == path

    def test_si_el_nombre_ya_lo_tiene_otro_pregunta_si_lo_sustituye(self, library, run_async, sent_messages, tap):
        settings.put("rename.auto", True)
        previous = _touch(os.path.join(library.base, "The Matrix (1999) - 1080p.mkv"), size=1)
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.x264.mkv"), size=9)

        arrangement = self._arrive(library, run_async, path)

        assert arrangement.path == path and os.path.getsize(previous) == 1
        assert library.arrivals.get_text("arrival_conflict", "The Matrix (1999) - 1080p.mkv") in _last(sent_messages)
        tap("🔁")
        assert os.listdir(library.base) == ["The Matrix (1999) - 1080p.mkv"]
        assert os.path.getsize(previous) == 9

    def test_no_sustituir_deja_los_dos(self, library, run_async, sent_messages, tap):
        settings.put("rename.auto", True)
        _touch(os.path.join(library.base, "The Matrix (1999) - 1080p.mkv"))
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.x264.mkv"))
        self._arrive(library, run_async, path)

        tap("❌")

        assert len(os.listdir(library.base)) == 2 and sent_messages[-1][0] == "delete"

    def test_sustituir_sin_preguntar(self, library, run_async, sent_messages):
        settings.put("rename.auto", True)
        settings.put("rename.replace", True)
        _touch(os.path.join(library.base, "The Matrix (1999) - 1080p.mkv"), size=1)
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.x264.mkv"), size=9)

        arrangement = self._arrive(library, run_async, path)

        assert os.listdir(library.base) == ["The Matrix (1999) - 1080p.mkv"]
        assert os.path.getsize(arrangement.path) == 9
        assert library.arrivals.get_text("arrival_replaced", "The Matrix (1999) - 1080p.mkv") in arrangement.notes
        assert sent_messages == []

    def test_nunca_sustituye_una_carpeta(self, library, run_async):
        settings.put("rename.auto", True)
        settings.put("rename.replace", True)
        _touch(os.path.join(library.base, "The Matrix (1999) - 1080p.mkv", "dentro.mkv"))
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.x264.mkv"))

        arrangement = self._arrive(library, run_async, path)

        assert arrangement.path == path
        assert os.path.exists(os.path.join(library.base, "The Matrix (1999) - 1080p.mkv", "dentro.mkv"))

    def test_propone_la_carpeta_de_la_serie_y_la_mueve(self, library, run_async, sent_messages, tap):
        show = os.path.join(library.video, "Chicago Fire")
        _touch(os.path.join(show, "14x03 - Chicago Fire.mkv"))
        path = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.1080p.mkv"))

        arrangement = self._arrive(library, run_async, path)

        assert arrangement.path == path, "solo se propone: no se mueve nada sin pulsar"
        assert library.arrivals.get_text("arrival_series_found", "Chicago Fire") in _last(sent_messages)
        assert all(len(data) <= CALLBACK_DATA_LIMIT for _, data in buttons_of(sent_messages))
        tap("📦")
        assert os.path.isfile(os.path.join(show, "Chicago.Fire.S14E04.1080p.mkv")) and not os.path.exists(path)

    def test_un_doble_toque_no_mueve_dos_veces(self, library, run_async, sent_messages, press):
        _touch(os.path.join(library.video, "Chicago Fire", "14x03 - Chicago Fire.mkv"))
        self._arrive(library, run_async, _touch(os.path.join(library.base, "Chicago.Fire.S14E04.1080p.mkv")))
        data = buttons_of(sent_messages)[0][1]
        press(data)
        count = len(sent_messages)

        press(data)

        assert len(sent_messages) == count

    def test_directo_a_la_carpeta_de_la_serie_con_deshacer(self, library, run_async, sent_messages, tap):
        settings.put("series.direct", True)
        show = os.path.join(library.video, "Chicago Fire")
        _touch(os.path.join(show, "14x03 - Chicago Fire.mkv"))
        path = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.1080p.mkv"))

        arrangement = self._arrive(library, run_async, path)

        assert arrangement.path == os.path.join(show, "Chicago.Fire.S14E04.1080p.mkv")
        assert os.path.isfile(arrangement.path)
        # Lo cuenta el aviso con el botón de deshacer, y solo él: antes salía
        # también en «Descarga completada», dos veces seguidas
        assert arrangement.notes == []
        assert library.arrivals.get_text("arrival_series_placed", show) in _last(sent_messages)
        tap("↩️")
        assert os.path.isfile(path) and not os.path.exists(arrangement.path)

    def test_directo_solo_sin_dudas(self, library, run_async, sent_messages):
        """Dos carpetas con el nombre de la serie: se propone, no se decide."""
        settings.put("series.direct", True)
        for root in (library.video, library.media):
            _touch(os.path.join(root, "Chicago Fire", "14x03 - Chicago Fire.mkv"))
        folders.add_favorite(library.media)
        path = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.1080p.mkv"))

        arrangement = self._arrive(library, run_async, path)

        assert arrangement.path == path
        assert sum(1 for text, _ in buttons_of(sent_messages) if "📦" in text) == 2

    def test_con_las_series_desactivadas_no_propone_nada(self, library, run_async, sent_messages):
        settings.put("series.suggest", False)
        _touch(os.path.join(library.video, "Chicago Fire", "14x03 - Chicago Fire.mkv"))

        self._arrive(library, run_async, _touch(os.path.join(library.base, "Chicago.Fire.S14E04.1080p.mkv")))

        assert sent_messages == []

    def test_preguntar_la_carpeta_de_cada_descarga(self, library, run_async, sent_messages, tap):
        folders.add_favorite(library.media)
        settings.put("folders.ask", True)
        path = _touch(os.path.join(library.base, "documento.pdf"))

        self._arrive(library, run_async, path)
        assert library.arrivals.get_text("arrival_ask_title", "documento.pdf") in _last(sent_messages)
        tap(os.path.basename(library.media)[-10:])
        tap("✅")

        assert os.path.isfile(os.path.join(library.media, "documento.pdf")) and not os.path.exists(path)

    def test_un_torrent_no_se_toca_ni_se_pregunta(self, library, run_async, sent_messages):
        settings.put("folders.ask", True)
        settings.put("rename.auto", True)
        path = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.torrent"))

        assert self._arrive(library, run_async, path).path == path
        assert sent_messages == []

    def test_un_fallo_no_tumba_la_descarga(self, library, run_async, monkeypatch):
        settings.put("rename.auto", True)
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.mkv"))

        def broken(*args):
            raise RuntimeError("boom")

        monkeypatch.setattr(library.arrivals, "_arrange", broken)

        assert run_async(library.arrivals.arrange(path)).path == path

    def test_aviso_de_poco_espacio(self, library, monkeypatch):
        from types import SimpleNamespace
        monkeypatch.setattr(library.arrivals.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024))

        assert library.arrivals.low_space_note(library.base, 500) is None
        assert library.arrivals.low_space_note(library.base, None) is None
        note = library.arrivals.low_space_note(library.base, 4096)
        assert "4.00 KB" in note and "1.00 KB" in note
        settings.put("folders.low_space", False)
        assert library.arrivals.low_space_note(library.base, 4096) is None


class TestAjustes:
    def test_elegir_la_carpeta_de_un_tipo(self, library, press, tap, sent_messages):
        folders.add_favorite(library.media)
        os.makedirs(os.path.join(library.media, "Musica"))

        press(b"cfg:folders")
        tap(library.settings.get_text("settings_folder_audio"))
        tap("📂")
        tap(os.path.basename(library.media)[-10:])
        tap("Musica")
        tap("✅")

        chosen = os.path.join(library.media, "Musica")
        assert config.DOWNLOAD_PATHS["audio"] == chosen
        assert f"`{chosen}`" in _last(sent_messages)
        tap("↩️")
        assert config.DOWNLOAD_PATHS["audio"] == library.base and settings.folder_paths() == {}

    def test_la_pantalla_dice_de_donde_sale_cada_carpeta(self, library):
        folders.set_kind_folder("audio", os.path.join(library.media, "Musica"))

        text, buttons = library.settings.build_folders()

        assert f"✏️ {library.settings.get_text('settings_folder_audio')}" in text
        assert f"📌 {library.settings.get_text('settings_folder_video')}" in text
        assert f"📂 {library.settings.get_text('settings_folder_photo')}" in text
        assert all(len(data) <= CALLBACK_DATA_LIMIT for _, data in buttons_of([("", "", {"buttons": buttons})]))

    def test_favoritas_anadir_y_quitar(self, library, press, tap, write, sent_messages):
        press(b"cfg:fav")
        tap("➕")
        # Otro volumen no sale entre las conocidas: se escribe su ruta
        tap("📝")
        assert write(library.media)
        tap("✅")
        assert folders.favorites() == [library.media]

        tap("🗑")
        assert folders.favorites() == []
        assert library.settings.get_text("settings_fav_empty") in _last(sent_messages)

    @pytest.mark.parametrize("field, key, screen", [
        (b"ask", "folders.ask", b"folders"), (b"space", "folders.low_space", b"folders"),
        (b"ar", "rename.auto", b"ren"), (b"ss", "series.suggest", b"ser"), (b"sd", "series.direct", b"ser"),
    ])
    def test_los_interruptores(self, library, press, field, key, screen):
        before = settings.get(key)
        press(b"cfg:" + screen)

        press(b"cfg:set:" + field + (b":0" if before else b":1"))

        assert settings.get(key) is (not before)

    def test_sustituir_solo_sale_con_el_renombrado_automatico(self, library):
        def toggles():
            return [data for _, data in buttons_of([("", "", {"buttons": library.settings.build_rename()[1]})])]

        assert b"cfg:set:arr:1" not in toggles()
        settings.put("rename.auto", True)
        assert b"cfg:set:arr:1" in toggles()

    def test_editar_una_plantilla(self, library, press, write, sent_messages):
        press(b"cfg:tpl")
        press(b"cfg:tple:movie")

        assert write("{titulo} - {año}[ - {resolucion}]")

        assert settings.rename_template("movie") == "{titulo} - {año}[ - {resolucion}]"
        assert "`Minions and Monsters - 2026 - 1080p`" in _last(sent_messages)

    def test_una_plantilla_mal_escrita_no_se_guarda_y_se_sigue_esperando(self, library, press, write, sent_messages):
        press(b"cfg:tple:series")

        assert write("{temporada}x{inventado}")
        assert library.settings.get_text("settings_tpl_error_unknown_field", "inventado") in _last(sent_messages)
        assert settings.rename_template("series") is None

        assert write("{temporada}x{episodio.2} {titulo}")
        assert settings.rename_template("series") == "{temporada}x{episodio.2} {titulo}"

    def test_una_plantilla_no_puede_llevar_rutas(self, library, press, write):
        press(b"cfg:tple:movie")
        assert write("../{titulo}")
        assert settings.rename_template("movie") is None

    def test_restaurar_las_plantillas(self, library, press):
        settings.put("rename.template_movie", "{titulo}")
        settings.put("rename.template_season", "{titulo} T{temporada}")

        # Primero pregunta: un toque borraba las plantillas escritas a mano
        press(b"cfg:tplr")
        assert settings.rename_template("movie") == "{titulo}"

        press(b"cfg:tplrc")

        assert all(settings.rename_template(kind) is None for kind in settings.TEMPLATE_KINDS)

    def test_manage_es_un_explorador_de_carpetas(self, library, press, sent_messages):
        """«Todos» lo mezclaba todo al mismo nivel: con la carpeta de vídeos
        dentro de la general salían juntos la carpeta y lo que lleva dentro."""
        inside = os.path.join(library.base, "Vídeos")
        folders.set_kind_folder("video", inside)
        _touch(os.path.join(inside, "peli.mkv"))
        _touch(os.path.join(library.base, "suelto.mkv"))

        # La de vídeos está dentro de la general: no es otra entrada
        assert library.manage.top_folders() == [library.base]
        press(b"fm:r")

        labels = [text for text, _ in buttons_of(sent_messages)]
        assert [label for label in labels if "📁" in label or "📽️" in label] == ["1. 📽️ suelto.mkv · 4 B", "2. 📁 Vídeos · 4 B"]
        assert not any("peli.mkv" in label for label in labels), "lo de dentro está en su carpeta"
        # De la única carpeta por la que se entra no se sube: se vuelve al menú,
        # y no se ofrece renombrarla ni eliminarla
        assert library.manage.get_text("button_home") in labels
        assert library.manage.get_text("button_picker_up") not in labels
        assert library.manage.get_text("manage_button_this_folder") not in labels

    def test_con_varias_carpetas_manage_deja_elegir_y_se_sube_a_ellas(self, library, press, sent_messages):
        series = os.path.join(library.media, "Series")
        folders.add_favorite(series)
        tops = library.manage.top_folders()
        assert library.base in tops and series in tops

        press(b"fm:r")
        assert _last(sent_messages) == library.manage.get_text("manage_select_folder")
        opened = [data for _, data in buttons_of(sent_messages) if data.startswith(b"fm:o:")]
        assert len(opened) == len(tops)

        press(opened[0])
        assert (library.manage.get_text("button_picker_up"), b"fm:r") in buttons_of(sent_messages)

    def test_los_que_se_llaman_igual_llevan_su_carpeta(self, library):
        """El mismo nombre en dos carpetas (en los resultados de /find) daba
        dos botones idénticos."""
        _touch(os.path.join(library.base, "peli.mkv"))
        _touch(os.path.join(library.video, "peli.mkv"))
        _touch(os.path.join(library.base, "otra.mkv"))

        _, buttons = library.manage.build_manage_listing(
            [library.manage.item_info(os.path.join(folder, name))
             for folder, name in ((library.base, "otra.mkv"), (library.base, "peli.mkv"), (library.video, "peli.mkv"))],
            0, 0, page_data=lambda number: "x", mass_ref="q.x", title="")

        labels = [button.text for row in buttons for button in row if button_data(button).startswith(b"fileact:")]
        same = [label for label in labels if "peli.mkv" in label]
        assert len(same) == 2 and len(set(same)) == 2
        assert all(" · 📁 " in label for label in same)
        assert " · 📁 " not in [label for label in labels if "otra.mkv" in label][0]

    def test_eliminar_una_carpeta_dice_cuanto_ocupa(self, library, run_async, sent_messages):
        folder = os.path.join(library.base, "Vieja")
        _touch(os.path.join(folder, "a.mkv"), size=10)
        _touch(os.path.join(folder, "Sub", "b.mkv"), size=20)
        file_id = _register(library, folder)

        run_async(library.manage.handle_delete_file(ButtonPress(b"delete:" + file_id)))

        assert library.manage.get_text("file_actions_size", "30.00 B") in _last(sent_messages)

    def test_el_menu_principal_lleva_a_cada_cosa_y_se_vuelve_a_el(self, library, press, write, sent_messages):
        """/start es un menú con botones, y las pantallas de primer nivel
        (ajustes, listados, búsqueda) llevan uno para volver a él."""
        _touch(os.path.join(library.base, "peli.mkv"))
        _, menu = library.home.build_menu()
        datas = [button_data(button) for row in menu for button in row]
        assert datas == [b"home:manage", b"home:find", b"cfg:main", b"home:about", b"close"]

        def has_home():
            return (library.home.get_text("button_home"), b"home:main") in buttons_of(sent_messages)

        for data in (b"home:manage", b"cfg:main", b"home:about"):
            press(data)
            assert sent_messages[-1][0] == "edit" and has_home(), data

        # Buscar pide el texto y enseña los resultados en el mismo mensaje
        press(b"home:find")
        assert _last(sent_messages) == library.home.get_text("find_prompt") and has_home()
        assert write("peli")
        assert library.files.get_text("find_title", "peli") in _last(sent_messages)
        assert sent_messages[-1][0] == "edit" and has_home()

        press(b"home:main")
        assert _last(sent_messages) == library.home.get_text("welcome_message")

    def test_volver_al_menu_deja_de_esperar_la_busqueda(self, library, press, write):
        press(b"home:find")
        press(b"home:main")

        assert not write("peli")

    def test_el_rastro_vuelve_a_la_pagina_y_a_la_busqueda(self, library, press, sent_messages, run_async):
        """«Volver» llevaba siempre a la primera página, y desde un resultado
        de /find no se podía regresar a la búsqueda."""
        for index in range(25):
            _touch(os.path.join(library.base, f"peli{index:02d}.mkv"))
        back_label = library.manage.get_text("button_back_to_manage")

        def back():
            return [data for text, data in buttons_of(sent_messages) if text == back_label]

        base = _register(library, library.base)
        press(b"fm:o:" + base + b":1")
        press(b"fileact:" + _register(library, os.path.join(library.base, "peli24.mkv")))
        assert back() == [b"fm:o:" + base + b":1"]

        # Otro mensaje: una búsqueda, y una carpeta abierta desde ella
        folder = os.path.join(library.base, "Serie")
        _touch(os.path.join(folder, "01.mkv"))
        query_id = library.state.short_id("q")
        library.state.find_queries[query_id] = "peli"
        press(f"fm:f:{query_id}:1".encode(), message_id=2)
        press(b"fileact:" + _register(library, os.path.join(library.base, "peli03.mkv")), message_id=2)
        assert back() == [f"fm:f:{query_id}:1".encode()]

        press(b"fm:o:" + _register(library, folder), message_id=2)
        up = [data for text, data in buttons_of(sent_messages)
              if text == library.manage.get_text("button_picker_up")]
        assert up == [f"fm:f:{query_id}:1".encode()]

    def test_salir_de_la_plantilla_deja_de_esperar_el_texto(self, library, press, write):
        press(b"cfg:tple:movie")
        press(b"cfg:tpl")
        assert not write("{titulo}")

    def test_todos_los_callbacks_nuevos_caben_en_telegram(self, library):
        folders.add_favorite(library.media)
        for screen in ("main", "folders", "fav", "ren", "ser", "tpl"):
            _, buttons = library.settings.build_screen(screen)
            for _, data in buttons_of([("", "", {"buttons": buttons})]):
                assert len(data) <= CALLBACK_DATA_LIMIT, data


class TestEnElBot:
    """Lo anterior, enganchado en dropbot.py: el aviso de descarga y /find."""

    @pytest.fixture
    def downloaded(self, library, quiet_bot, monkeypatch):
        sent = []

        async def file_info(path):
            return {"type": "video", "size_formatted": "1 MB", "duration_formatted": None,
                    "resolution": None, "codec_video": None, "codec_audio": None, "bitrate": None}

        async def send(event, file_path, sending_msg=None, delete_after=False):
            sent.append((file_path, delete_after))

        monkeypatch.setattr(quiet_bot, "get_file_info", file_info)
        monkeypatch.setattr(quiet_bot, "send_file_to_telegram", send)
        return quiet_bot, sent

    def test_el_aviso_y_el_envio_van_con_el_nombre_nuevo(
        self, library, downloaded, sent_messages, make_event, run_async
    ):
        dropbot, sent = downloaded
        settings.put("rename.auto", True)
        settings.put("urls.auto_send", "SEND")
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.mkv"))

        run_async(dropbot.handle_success(make_event(), path))

        renamed = os.path.join(library.base, "The Matrix (1999) - 1080p.mkv")
        assert sent == [(renamed, False)]
        notice = sent_messages[0][1]
        assert "`The Matrix (1999) - 1080p.mkv`" in notice
        assert dropbot.get_text("arrival_renamed", "The.Matrix.1999.1080p.BluRay.mkv") in notice

    def test_si_se_va_a_enviar_y_borrar_no_se_ofrece_moverlo(
        self, library, downloaded, sent_messages, make_event, run_async
    ):
        dropbot, sent = downloaded
        settings.put("urls.auto_send", "SEND_DELETE")
        _touch(os.path.join(library.video, "Chicago Fire", "14x03 - Chicago Fire.mkv"))
        path = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.1080p.mkv"))

        run_async(dropbot.handle_success(make_event(), path))

        assert sent == [(path, True)]
        assert not buttons_of(sent_messages)

    def test_el_aviso_de_descarga_dice_a_que_carpeta_ha_ido(
        self, library, downloaded, sent_messages, make_event, run_async
    ):
        """Era lo primero que se quería saber y no lo decía."""
        dropbot, _ = downloaded
        path = _touch(os.path.join(library.video, "Vacaciones.mkv"))

        run_async(dropbot.handle_success(make_event(), path, show_action_buttons=False))

        assert dropbot.get_text("file_info_folder", f"`{library.video}`") in sent_messages[0][1]

    def test_un_episodio_numerado_se_propone_como_suposicion(
        self, library, downloaded, sent_messages, make_event, run_async
    ):
        """Sin el nombre de la serie en el fichero, decir «ya tienes episodios
        de Pantera Rosa» afirmaba algo que el bot no sabe."""
        dropbot, _ = downloaded
        _touch(os.path.join(library.base, "Pantera Rosa", "001 - The Pink Phink (1964).mkv"))
        path = _touch(os.path.join(library.base, "002 - Pink Pajamas (1964).mkv"))

        run_async(dropbot.handle_success(make_event(), path, show_action_buttons=False))

        assert os.path.exists(path), "no se mueve sin preguntar"
        assert library.arrivals.get_text("arrival_series_guess") in _last(sent_messages)
        assert any(data.startswith(b"arr:mv:") for _, data in buttons_of(sent_messages))

    def test_lo_que_llega_por_telegram_tambien_propone_la_carpeta_de_la_serie(
        self, library, downloaded, sent_messages, make_event, run_async
    ):
        dropbot, _ = downloaded
        _touch(os.path.join(library.video, "Chicago Fire", "14x03 - Chicago Fire.mkv"))
        path = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.1080p.mkv"))

        run_async(dropbot.handle_success(make_event(), path, show_action_buttons=False))

        assert any(data.startswith(b"arr:mv:") for _, data in buttons_of(sent_messages))

    def test_sin_nada_activado_el_aviso_es_el_de_siempre(
        self, library, downloaded, sent_messages, make_event, run_async
    ):
        dropbot, _ = downloaded
        path = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.BluRay.mkv"))

        run_async(dropbot.handle_success(make_event(), path, show_action_buttons=False))

        assert len(sent_messages) == 1 and os.path.exists(path)

    def test_las_carpetas_elegidas_deciden_donde_se_descarga(self, library, quiet_bot):
        from types import SimpleNamespace
        chosen = os.path.join(library.media, "Pelis")
        folders.set_kind_folder("video", chosen)
        message = SimpleNamespace(file=SimpleNamespace(name="peli.mkv"), video=None, audio=None, voice=None, photo=None)

        path, _ = quiet_bot.get_download_path(SimpleNamespace(message=message))

        assert path == chosen
