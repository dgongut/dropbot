"""/settings a fondo: cada pantalla, cada selector, cada interruptor y cada botón.

Complementa a test_settings_screens.py recorriendo todas las combinaciones:
qué se guarda en settings.json al pulsar cada valor, a qué pantalla lleva cada
botón de atrás, qué pasa con callbacks fabricados, con un settings.json
ilegible o con un cambio que no se puede aplicar, y que todos los textos de
todas las pantallas existen en los dos idiomas.
"""

import json

import pytest
from telethon.extensions import markdown

import config
import settings
import stats
import store
from button_data import button_data
from conftest import REPO_ROOT

ADMIN = 999
CALLBACK_DATA_LIMIT = 64
TELEGRAM_TEXT_LIMIT = 4096
LOCALES = {code: json.loads((REPO_ROOT / "locale" / f"{code}.json").read_text(encoding="utf-8"))
           for code in ("es", "en")}

SCREENS = ("main", "lang", "fmt", "send", "vq", "aq", "af", "sb", "plm", "pll", "max",
           "ext", "par", "fast", "hw", "q", "video", "links", "urlq", "pl", "folders", "tel")

# A dónde lleva el botón de atrás de cada pantalla, escrito a mano: es la
# especificación, no una copia de RETURN_SCREEN
EXPECTED_BACK = {
    "lang": "main", "ext": "main", "par": "main", "fast": "main",
    "fmt": "links", "send": "links", "sb": "links", "max": "links",
    "vq": "urlq", "af": "urlq", "aq": "urlq",
    "plm": "pl", "pll": "pl",
    "hw": "video", "q": "video",
    "video": "main", "links": "main", "folders": "main", "tel": "main",
    "urlq": "links", "pl": "links",
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

class _Event:
    def __init__(self, data, sender_id=ADMIN):
        self.id = 1
        self.message_id = 1
        self.chat_id = 42
        self.sender_id = sender_id
        self.data = data
        self._match = (data, data[len(b"cfg:"):]) if data else (b"", b"")
        self.pattern_match = self
        self.deleted = False

    def group(self, index):
        return self._match[index]

    async def get_sender(self):
        return None

    async def delete(self):
        self.deleted = True


def _all_buttons(buttons):
    return [button for row in buttons for button in row]


def _callbacks(buttons):
    return [button_data(button) for button in _all_buttons(buttons)]


def _plain(text):
    return markdown.parse(text)[0]


def _on_disk(config_dir):
    path = config_dir / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _lookup(document, dotted):
    for part in dotted.split("."):
        document = document[part]
    return document


@pytest.fixture
def screens(quiet_bot, sent_messages, monkeypatch, config_dir):
    from conftest import _patch_messaging
    from handlers import settings as module

    changed = []

    async def on_change(key):
        changed.append(key)

    monkeypatch.setattr(module, "_on_change", on_change)
    _patch_messaging(module, sent_messages, monkeypatch)
    module.changed = changed
    return module


def _press(screens, run_async, data, sender_id=ADMIN):
    event = _Event(data, sender_id=sender_id)
    run_async(screens.handle_settings_callback(event))
    return event


def _edits(sent_messages):
    return [(text, kwargs) for kind, text, kwargs in sent_messages if kind == "edit"]


def _marked(buttons):
    return [button_data(b) for b in _all_buttons(buttons) if b.text.startswith("✅ ")]


def _picker_cases():
    import handlers.settings as module

    cases = []
    for field in module.FIELDS:
        for value in module._choices(field):
            cases.append(pytest.param(field, value, id=f"{field}={module._token(value)}"))
    return cases


# ---------------------------------------------------------------------------
# Pantallas
# ---------------------------------------------------------------------------

class TestPantallas:
    @pytest.mark.parametrize("screen", SCREENS)
    def test_cada_pantalla_tiene_texto_y_botones_validos(self, screens, screen):
        text, buttons = screens.build_screen(screen)

        assert text.strip() and "MISSING" not in text
        assert len(_plain(text)) <= TELEGRAM_TEXT_LIMIT
        assert buttons, "toda pantalla tiene al menos cerrar"
        for button in _all_buttons(buttons):
            assert button.text.strip() and "MISSING" not in button.text
            data = button_data(button)
            assert data.startswith(b"cfg:")
            assert len(data) <= CALLBACK_DATA_LIMIT
        # Siempre se puede salir
        assert b"cfg:close" in _callbacks(buttons)

    @pytest.mark.parametrize("screen,back", sorted(EXPECTED_BACK.items()))
    def test_el_boton_de_atras_lleva_a_su_pantalla(self, screens, screen, back):
        _, buttons = screens.build_screen(screen)

        last_row = [button_data(b) for b in buttons[-1]]
        assert last_row == [f"cfg:{back}".encode(), b"cfg:close"]

    @pytest.mark.parametrize("screen,back", sorted(EXPECTED_BACK.items()))
    def test_la_pantalla_de_atras_vuelve_a_ofrecer_la_de_partida(self, screens, screen, back):
        """Atrás y adelante son simétricos: se vuelve a donde se vino."""
        _, buttons = screens.build_screen(back)

        assert f"cfg:{screen}".encode() in _callbacks(buttons)

    def test_la_principal_no_tiene_atras(self, screens):
        _, buttons = screens.build_main()

        assert [button_data(b) for b in buttons[-1]] == [b"cfg:close"]

    def test_una_pantalla_desconocida_es_la_principal(self, screens):
        assert screens.build_screen("inventada") == screens.build_main()

    @pytest.mark.parametrize("field", ["lang", "fmt", "send", "vq", "aq", "af", "sb", "plm",
                                       "pll", "max", "ext", "par", "fast", "hw", "q"])
    def test_cada_selector_marca_un_solo_valor_el_actual(self, screens, field):
        _, buttons = screens.build_picker(field)

        current = screens._token(screens._current(field)).encode()
        assert _marked(buttons) == [f"cfg:set:{field}:".encode() + current]

    @pytest.mark.parametrize("field,per_row", [("par", 4), ("fast", 4), ("q", 4), ("pll", 3),
                                               ("max", 3), ("send", 1), ("lang", 1)])
    def test_los_valores_se_reparten_por_filas(self, screens, field, per_row):
        _, buttons = screens.build_picker(field)

        value_rows = buttons[:-1]
        assert all(len(row) <= per_row for row in value_rows)
        assert len(value_rows[0]) == min(per_row, len(screens._choices(field)))

    def test_las_etiquetas_de_los_valores_especiales(self, screens):
        labels = lambda field: [b.text for b in _all_buttons(screens.build_picker(field)[1])]  # noqa: E731

        assert screens.get_text("settings_quality_default") in " ".join(labels("q"))
        assert screens.get_text("settings_pll_none") in " ".join(labels("pll"))
        assert screens.get_text("settings_max_none") in " ".join(labels("max"))
        assert "2 GB" in " ".join(labels("max")) and "512 MB" in " ".join(labels("max"))
        assert "1080p" in " ".join(labels("vq"))
        assert any(label.endswith("Español") for label in labels("lang"))
        assert any(label.endswith("English") for label in labels("lang"))

    def test_la_principal_cuenta_las_carpetas_propias(self, screens, monkeypatch):
        base = screens.DOWNLOAD_PATH
        for kind in screens.FOLDER_ORDER:
            monkeypatch.setitem(config.DOWNLOAD_PATHS, kind, base)
        monkeypatch.setitem(config.DOWNLOAD_PATHS, "video", "/video")
        monkeypatch.setitem(config.DOWNLOAD_PATHS, "ebook", "/ebook")

        _, buttons = screens.build_main()

        folders = next(b for b in _all_buttons(buttons) if button_data(b) == b"cfg:folders")
        assert folders.text == screens.get_text("settings_row_folders", 2)

    def test_las_carpetas_distinguen_propias_y_generales(self, screens, monkeypatch):
        base = screens.DOWNLOAD_PATH
        for kind in screens.FOLDER_ORDER:
            monkeypatch.setitem(config.DOWNLOAD_PATHS, kind, base)
        monkeypatch.setitem(config.DOWNLOAD_PATHS, "url_audio", "/url_audio")

        text, _ = screens.build_folders()

        lines = [line for line in _plain(text).splitlines() if line.startswith(("📌", "📂"))]
        assert len(lines) == len(screens.FOLDER_ORDER)
        assert sum(line.startswith("📌") for line in lines) == 1
        assert any(line.startswith("📌") and "/url_audio" in line for line in lines)

    def test_la_calidad_del_mp3_aparece_y_desaparece_al_cambiar_el_formato(
        self, screens, run_async, sent_messages
    ):
        _press(screens, run_async, b"cfg:set:af:M4A")
        _press(screens, run_async, b"cfg:urlq")
        _, kwargs = _edits(sent_messages)[-1]
        assert b"cfg:aq" not in _callbacks(kwargs["buttons"])

        _press(screens, run_async, b"cfg:set:af:MP3")
        _press(screens, run_async, b"cfg:urlq")
        _, kwargs = _edits(sent_messages)[-1]
        assert b"cfg:aq" in _callbacks(kwargs["buttons"])


# ---------------------------------------------------------------------------
# Estadísticas
# ---------------------------------------------------------------------------

class TestEstadisticas:
    def test_sin_volumen_no_hay_interruptor_y_se_explica(self, screens, monkeypatch):
        monkeypatch.setattr(stats, "forced_off", lambda: "volume")

        text, buttons = screens.build_telemetry()

        assert screens.get_text("settings_telemetry_forced_volume") in text
        assert not any(d.startswith(b"cfg:set:tel:") for d in _callbacks(buttons))
        assert b"cfg:telshow" in _callbacks(buttons)

    def test_apagadas_por_variable_lo_dice(self, screens, monkeypatch):
        monkeypatch.setattr(stats, "forced_off", lambda: "TELEMETRY")

        text, buttons = screens.build_telemetry()

        assert screens.get_text("settings_telemetry_forced_env", "TELEMETRY") in text
        assert not any(d.startswith(b"cfg:set:tel:") for d in _callbacks(buttons))

    @pytest.mark.parametrize("on,label,data", [(True, "✅", b"cfg:set:tel:0"),
                                               (False, "❌", b"cfg:set:tel:1")])
    def test_sin_bloqueo_el_interruptor_refleja_el_estado(
        self, screens, monkeypatch, on, label, data
    ):
        monkeypatch.setattr(stats, "forced_off", lambda: None)
        settings.put("telemetry", on)

        _, buttons = screens.build_telemetry()

        toggle = _all_buttons(buttons)[0]
        assert toggle.text.startswith(label) and button_data(toggle) == data

    def test_la_principal_muestra_si_estan_activas(self, screens, monkeypatch):
        monkeypatch.setattr(stats, "forced_off", lambda: None)
        row = lambda: next(b for b in _all_buttons(screens.build_main()[1])  # noqa: E731
                           if button_data(b) == b"cfg:tel").text

        assert row() == screens.get_text("settings_row_telemetry", "✅")
        settings.put("telemetry", False)
        assert row() == screens.get_text("settings_row_telemetry", "❌")

    @pytest.mark.parametrize("data", [b"cfg:set:tel:2", b"cfg:set:tel:", b"cfg:set:tel:1"])
    def test_un_valor_raro_o_el_mismo_no_cambia_nada(
        self, screens, run_async, sent_messages, config_dir, data
    ):
        # telemetry ya está a True por defecto: pulsar 1 no cambia nada
        _press(screens, run_async, data)

        assert _edits(sent_messages) == []
        assert _on_disk(config_dir) is None

    def test_apagar_y_encender_guarda_y_repinta(
        self, screens, run_async, sent_messages, config_dir, monkeypatch
    ):
        monkeypatch.setattr(stats, "forced_off", lambda: None)
        forgotten = []
        monkeypatch.setattr(stats.client(), "forget", lambda: forgotten.append(True))

        _press(screens, run_async, b"cfg:set:tel:0")
        assert _on_disk(config_dir)["telemetry"] is False and forgotten
        _, kwargs = _edits(sent_messages)[-1]
        assert b"cfg:set:tel:1" in _callbacks(kwargs["buttons"])

        _press(screens, run_async, b"cfg:set:tel:1")
        assert _on_disk(config_dir)["telemetry"] is True
        _, kwargs = _edits(sent_messages)[-1]
        assert b"cfg:set:tel:0" in _callbacks(kwargs["buttons"])

    def test_ver_que_se_envia_manda_un_mensaje_aparte(self, screens, run_async, sent_messages):
        _press(screens, run_async, b"cfg:telshow")

        [(kind, text, kwargs)] = sent_messages
        assert kind == "send_message"
        assert text.startswith(screens.get_text("telemetry_preview"))
        body = text.split("```\n", 1)[1].rsplit("\n```", 1)[0]
        payload = json.loads(body)
        assert payload["project"] == "dropbot" and "metrics" in payload
        assert _callbacks(kwargs["buttons"]) == [b"cfg:close"]
        assert kwargs.get("link_preview") is False

    def test_la_vista_previa_recorta_los_contadores_y_cabe(
        self, screens, run_async, sent_messages, monkeypatch
    ):
        client = stats.client()
        real_preview = client.preview
        usage = {f"btn_un_contador_con_nombre_bastante_largo_{i:03d}": i for i in range(200)}
        monkeypatch.setattr(client, "preview", lambda: {**real_preview(), "usage": dict(usage)})

        _press(screens, run_async, b"cfg:telshow")

        [(_, text, _)] = sent_messages
        assert screens.get_text("telemetry_preview_truncated", 175) in text
        assert len(_plain(text).encode("utf-16-le")) // 2 <= TELEGRAM_TEXT_LIMIT
        # Se quedan los más usados
        assert "btn_un_contador_con_nombre_bastante_largo_199" in text
        assert "btn_un_contador_con_nombre_bastante_largo_000" not in text


# ---------------------------------------------------------------------------
# Guardar: cada valor de cada selector y cada interruptor
# ---------------------------------------------------------------------------

class TestGuardar:
    @pytest.mark.parametrize("field,value", _picker_cases())
    def test_cada_valor_se_guarda_en_disco_y_se_repinta_marcado(
        self, screens, run_async, sent_messages, config_dir, field, value
    ):
        key = screens.FIELDS[field]
        token = screens._token(value)
        was = settings.get(key)

        _press(screens, run_async, f"cfg:set:{field}:{token}".encode())

        if value == was:
            # Telegram rechaza editar un mensaje para dejarlo igual
            assert _edits(sent_messages) == [] and _on_disk(config_dir) is None
            return
        assert settings.get(key) == value
        assert _lookup(_on_disk(config_dir), key) == value
        assert screens.changed == [key]
        [(text, kwargs)] = _edits(sent_messages)
        assert _marked(kwargs["buttons"]) == [f"cfg:set:{field}:{token}".encode()]
        assert text == screens.build_picker(field)[0]
        assert kwargs.get("link_preview") is False

    @pytest.mark.parametrize("field", ["compat", "tags"])
    def test_cada_interruptor_se_apaga_y_se_enciende(
        self, screens, run_async, sent_messages, config_dir, field
    ):
        key, screen = screens.TOGGLES[field]
        assert settings.get(key) is True

        _press(screens, run_async, f"cfg:set:{field}:0".encode())
        assert settings.get(key) is False
        assert _lookup(_on_disk(config_dir), key) is False
        text, kwargs = _edits(sent_messages)[-1]
        assert text == screens.build_screen(screen)[0]
        assert f"cfg:set:{field}:1".encode() in _callbacks(kwargs["buttons"])

        _press(screens, run_async, f"cfg:set:{field}:1".encode())
        assert settings.get(key) is True
        assert _lookup(_on_disk(config_dir), key) is True
        _, kwargs = _edits(sent_messages)[-1]
        assert f"cfg:set:{field}:0".encode() in _callbacks(kwargs["buttons"])
        assert len(_edits(sent_messages)) == 2

    @pytest.mark.parametrize("field", ["compat", "tags"])
    def test_pulsar_el_interruptor_en_su_estado_no_edita(
        self, screens, run_async, sent_messages, config_dir, field
    ):
        _press(screens, run_async, f"cfg:set:{field}:1".encode())

        assert _edits(sent_messages) == [] and _on_disk(config_dir) is None

    def test_cambiar_de_idioma_repinta_en_el_idioma_nuevo(
        self, screens, run_async, sent_messages
    ):
        _press(screens, run_async, b"cfg:set:lang:EN")

        [(text, kwargs)] = _edits(sent_messages)
        assert text.startswith(LOCALES["en"]["settings_lang_title"])
        labels = [b.text for b in _all_buttons(kwargs["buttons"])]
        assert LOCALES["en"]["button_back"] in labels
        assert LOCALES["en"]["button_close"] in labels
        assert screens.changed == ["language"]

        _press(screens, run_async, b"cfg:set:lang:ES")
        text, _ = _edits(sent_messages)[-1]
        assert text.startswith(LOCALES["es"]["settings_lang_title"])

    def test_si_aplicar_el_cambio_falla_se_guarda_y_se_repinta_igual(
        self, screens, run_async, sent_messages, config_dir, monkeypatch
    ):
        async def broken(key):
            raise RuntimeError("no se pudo")

        monkeypatch.setattr(screens, "_on_change", broken)

        _press(screens, run_async, b"cfg:set:par:6")

        assert settings.parallel_downloads() == 6
        assert _lookup(_on_disk(config_dir), "downloads.parallel") == 6
        [(_, kwargs)] = _edits(sent_messages)
        assert _marked(kwargs["buttons"]) == [b"cfg:set:par:6"]

    def test_sin_on_change_tambien_guarda(self, screens, run_async, monkeypatch):
        monkeypatch.setattr(screens, "_on_change", None)

        _press(screens, run_async, b"cfg:set:hw:NVENC")

        assert settings.ffmpeg_hw() == "NVENC"


# ---------------------------------------------------------------------------
# Botones de navegación y callbacks raros
# ---------------------------------------------------------------------------

class TestNavegacion:
    @pytest.mark.parametrize("screen", SCREENS)
    def test_abrir_cada_pantalla_la_pinta_en_el_mismo_mensaje(
        self, screens, run_async, sent_messages, screen
    ):
        _press(screens, run_async, f"cfg:{screen}".encode())

        [(kind, text, kwargs)] = sent_messages
        assert kind == "edit"
        assert (text, _callbacks(kwargs["buttons"])) == \
            (screens.build_screen(screen)[0], _callbacks(screens.build_screen(screen)[1]))

    def test_cerrar_borra_el_mensaje(self, screens, run_async, sent_messages, config_dir):
        _press(screens, run_async, b"cfg:close")

        assert [kind for kind, _, _ in sent_messages] == ["delete"]
        assert _on_disk(config_dir) is None

    @pytest.mark.parametrize("data", [
        b"cfg:\xff\xfe\xfd",
        b"cfg:set",
        b"cfg:set:lang",
        b"cfg:set:lang:EN:extra",
        b"cfg:set:lang:\xff",
        b"cfg:set:\xc3:ES",
        b"cfg:set::",
        b"cfg::::",
        b"cfg:" + "ñ".encode() * 30,
        b"cfg:set:language:EN",
        b"cfg:set:urls.auto_send:SEND",
        b"cfg:set:par:4.0",
        b"cfg:set:par:04",
        b"cfg:set:q:None",
        b"cfg:set:send:send",
        b"cfg:set:tel:true",
    ])
    def test_un_callback_basura_no_rompe_ni_guarda(
        self, screens, run_async, sent_messages, config_dir, data
    ):
        _press(screens, run_async, data)

        assert _on_disk(config_dir) is None
        assert screens.changed == []
        assert settings.language() == "ES"
        # Si algo se pinta, es una pantalla válida (la principal)
        for text, kwargs in _edits(sent_messages):
            assert (text, _callbacks(kwargs["buttons"])) == \
                (screens.build_main()[0], _callbacks(screens.build_main()[1]))

    def test_un_no_admin_no_puede_tocar_nada(self, screens, run_async, sent_messages, config_dir):
        for data in (b"cfg:main", b"cfg:set:lang:EN", b"cfg:set:compat:0", b"cfg:set:tel:0",
                     b"cfg:close", b"cfg:telshow"):
            event = _press(screens, run_async, data, sender_id=12345)
            assert not event.deleted

        assert sent_messages == []
        assert _on_disk(config_dir) is None


class TestComando:
    def test_el_admin_recibe_la_principal(self, screens, run_async, sent_messages):
        event = _Event(None)

        run_async(screens.handle_settings_command(event))

        assert event.deleted, "el /settings se borra para no ensuciar el chat"
        [(kind, text, kwargs)] = sent_messages
        assert kind == "send_message"
        assert text == screens.build_main()[0]
        assert kwargs.get("link_preview") is False

    def test_un_no_admin_recibe_el_aviso_y_nada_mas(self, screens, run_async, sent_messages):
        run_async(screens.handle_settings_command(_Event(None, sender_id=12345)))

        assert [(kind, text) for kind, text, _ in sent_messages] == \
            [("send_message", screens.get_text("user_not_admin"))]
        assert "buttons" not in sent_messages[0][2]

    def test_si_no_se_puede_borrar_el_comando_sigue(self, screens, run_async, sent_messages):
        class Undeletable(_Event):
            async def delete(self):
                raise RuntimeError("sin permisos")

        run_async(screens.handle_settings_command(Undeletable(None)))

        assert [kind for kind, _, _ in sent_messages] == ["send_message"]


# ---------------------------------------------------------------------------
# settings.json ilegible
# ---------------------------------------------------------------------------

class TestAjustesIlegibles:
    @pytest.fixture
    def broken(self, screens, config_dir):
        path = config_dir / "settings.json"
        path.write_text('{"language": "EN", roto', encoding="utf-8")
        store.reload()
        yield path
        store.reload()

    def test_la_principal_avisa_con_la_ruta(self, screens, broken):
        assert store.settings_unreadable()

        text, _ = screens.build_main()

        assert screens.get_text("settings_unreadable", store.settings_path()) in text
        assert str(broken) in _plain(text)

    def test_se_usan_los_valores_por_defecto(self, screens, broken):
        text, _ = screens.build_main()

        assert text.startswith(LOCALES["es"]["settings_title"])

    def test_cambiar_un_valor_no_pisa_el_fichero(self, screens, broken, run_async, sent_messages):
        original = broken.read_text(encoding="utf-8")

        _press(screens, run_async, b"cfg:set:par:5")
        _press(screens, run_async, b"cfg:set:compat:0")

        assert broken.read_text(encoding="utf-8") == original
        assert len(_edits(sent_messages)) == 2, "se sigue pudiendo usar el menú"

    def test_todas_las_pantallas_se_pueden_abrir(self, screens, broken):
        for screen in SCREENS:
            text, buttons = screens.build_screen(screen)
            assert text and buttons


# ---------------------------------------------------------------------------
# Traducciones
# ---------------------------------------------------------------------------

def _render_everything(screens, monkeypatch):
    """Pinta todas las pantallas en todos sus estados y devuelve los textos."""
    rendered = []

    def collect():
        for screen in SCREENS:
            text, buttons = screens.build_screen(screen)
            rendered.append(text)
            rendered.extend(b.text for b in _all_buttons(buttons))
        rendered.append(screens.build_telemetry_preview()[0])

    collect()
    settings.put("urls.audio_format", "M4A")
    collect()
    settings.put("urls.audio_format", "MP3")
    for forced in (None, "TELEMETRY", "volume"):
        monkeypatch.setattr(stats, "forced_off", lambda forced=forced: forced)
        collect()
    return rendered


@pytest.mark.parametrize("language", ["ES", "EN"])
def test_todo_texto_de_los_ajustes_existe_en_los_dos_idiomas(screens, monkeypatch, language):
    """Ningún texto puede depender del respaldo en inglés ni salir como [MISSING]."""
    used = set()
    real_get_text = screens.get_text

    def recording(key, *args):
        used.add(key)
        return real_get_text(key, *args)

    monkeypatch.setattr(screens, "get_text", recording)
    settings.put("language", language)
    unreadable = store.settings_path()

    rendered = _render_everything(screens, monkeypatch)
    rendered.append(recording("settings_unreadable", unreadable))
    rendered.append(recording("user_not_admin"))

    missing = {code: sorted(key for key in used if key not in LOCALES[code]) for code in LOCALES}
    assert missing == {"es": [], "en": []}
    for text in rendered:
        assert "MISSING" not in text, text


def test_cada_pantalla_cambia_de_idioma(screens):
    """Con el bot en inglés no queda ninguna pantalla en español."""
    spanish = {screen: screens.build_screen(screen) for screen in SCREENS}
    settings.put("language", "EN")

    for screen in SCREENS:
        text, buttons = screens.build_screen(screen)
        assert text != spanish[screen][0], screen
        labels = [b.text for b in _all_buttons(buttons)]
        assert LOCALES["en"]["button_close"] in labels, screen
        assert LOCALES["es"]["button_close"] not in labels, screen
