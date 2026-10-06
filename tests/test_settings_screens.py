"""/settings: las pantallas, los botones y lo que guardan."""

import asyncio

import pytest

import settings
import store
from button_data import button_data


@pytest.fixture
def screens(quiet_bot, sent_messages, monkeypatch, config_dir):
    """`handlers.settings` con sus envíos capturados y un /config vacío."""
    from conftest import _patch_messaging
    from handlers import settings as module

    changed = []

    async def on_change(key):
        changed.append(key)

    monkeypatch.setattr(module, "_on_change", on_change)
    _patch_messaging(module, sent_messages, monkeypatch)
    module.changed = changed
    return module


def _callbacks(buttons):
    return [button_data(button) for row in buttons for button in row]


def _press(screens, make_event, run_async, data):
    event = make_event(data=data, groups=(data[len(b"cfg:"):],))
    run_async(screens.handle_settings_callback(event))


class TestPantallas:
    def test_la_principal_abre_cada_ajuste(self, screens):
        text, buttons = screens.build_main()

        callbacks = _callbacks(buttons)
        for screen in (b"cfg:lang", b"cfg:links", b"cfg:par", b"cfg:fast",
                       b"cfg:video", b"cfg:ext", b"cfg:folders", b"cfg:tel", b"cfg:close"):
            assert screen in callbacks

    def test_enlaces_abre_sus_ajustes(self, screens):
        _, buttons = screens.build_links()

        callbacks = _callbacks(buttons)
        for screen in (b"cfg:fmt", b"cfg:send", b"cfg:urlq", b"cfg:pl", b"cfg:sb", b"cfg:max"):
            assert screen in callbacks

    def test_la_calidad_del_mp3_solo_sale_con_mp3(self, screens):
        assert b"cfg:aq" in _callbacks(screens.build_url_quality()[1])
        settings.put("urls.audio_format", "M4A")
        assert b"cfg:aq" not in _callbacks(screens.build_url_quality()[1])

    def test_todos_los_callbacks_caben_en_telegram(self, screens):
        """Telegram rechaza un callback_data de más de 64 bytes."""
        for screen in ("main", *screens.FIELDS, "links", "urlq", "pl", "video", "folders", "tel"):
            _, buttons = screens.build_screen(screen)
            for data in _callbacks(buttons):
                assert len(data) <= 64, data

    def test_el_selector_marca_el_valor_actual(self, screens):
        settings.put("urls.auto_send", "STORE")

        _, buttons = screens.build_picker("send")

        marked = [button.text for row in buttons for button in row if button.text.startswith("✅")]
        assert len(marked) == 1
        assert button_data(next(b for row in buttons for b in row if b.text.startswith("✅"))) == b"cfg:set:send:STORE"

    def test_sin_volumen_lo_avisa(self, screens):
        text, _ = screens.build_main()
        # El /config de los tests es un directorio temporal, no un volumen
        assert not store.is_persistent()
        assert "/config" in text


class TestGuardar:
    def test_elegir_un_valor_lo_guarda_y_lo_aplica(self, screens, make_event, run_async):
        _press(screens, make_event, run_async, b"cfg:set:par:4")

        assert settings.parallel_downloads() == 4
        assert screens.changed == ["downloads.parallel"]

    def test_tras_elegir_se_queda_en_el_selector_con_el_nuevo_marcado(
        self, screens, sent_messages, make_event, run_async
    ):
        _press(screens, make_event, run_async, b"cfg:set:send:STORE")

        edits = [kwargs["buttons"] for kind, _, kwargs in sent_messages if kind == "edit"]
        assert len(edits) == 1
        marked = [button_data(b) for row in edits[0] for b in row if b.text.startswith("✅")]
        assert marked == [b"cfg:set:send:STORE"]

    def test_pulsar_el_valor_ya_marcado_no_edita_nada(
        self, screens, sent_messages, make_event, run_async
    ):
        """Telegram rechaza editar un mensaje para dejarlo igual."""
        _press(screens, make_event, run_async, b"cfg:set:send:ASK")

        assert not [kind for kind, _, _ in sent_messages if kind == "edit"]
        assert screens.changed == []

    def test_el_interruptor_cambia_y_se_repinta_en_su_pantalla(
        self, screens, sent_messages, make_event, run_async
    ):
        _press(screens, make_event, run_async, b"cfg:set:compat:0")

        assert settings.prefer_compatible() is False
        edits = [kwargs["buttons"] for kind, _, kwargs in sent_messages if kind == "edit"]
        labels = [b.text for row in edits[0] for b in row]
        # Sigue en Calidad de los enlaces, con el interruptor ya apagado
        assert any(label.startswith("❌") for label in labels)
        assert b"cfg:set:compat:1" in _callbacks(edits[0])

    def test_la_calidad_por_defecto_es_none(self, screens, make_event, run_async):
        settings.put("video.quality", 23)

        _press(screens, make_event, run_async, b"cfg:set:q:def")

        assert settings.ffmpeg_quality() is None

    @pytest.mark.parametrize("data", [b"cfg:set:par:999", b"cfg:set:nope:1", b"cfg:set:send:rm -rf",
                                      b"cfg:set:compat:yes"])
    def test_un_callback_fabricado_no_escribe_nada(self, screens, make_event, run_async, config_dir, data):
        _press(screens, make_event, run_async, data)

        assert not (config_dir / "settings.json").exists()
        assert screens.changed == []

    def test_el_interruptor_de_las_estadisticas(self, screens, make_event, run_async, monkeypatch):
        forgotten = []
        monkeypatch.setattr(screens.stats.client(), "forget", lambda: forgotten.append(True))

        _press(screens, make_event, run_async, b"cfg:set:tel:0")
        assert settings.get("telemetry") is False
        # Apagarlas olvida también el id de la instalación
        assert forgotten

        _press(screens, make_event, run_async, b"cfg:set:tel:1")
        assert settings.get("telemetry") is True

    def test_los_enlaces_de_las_estadisticas_van_embebidos(self, screens):
        text, _ = screens.build_telemetry()
        assert "](https://stats.dgongut.com/dropbot)" in text


class TestLimiter:
    def test_subir_el_limite_deja_pasar_a_quien_espera(self, run_async):
        from utils.limiter import ResizableLimiter

        async def scenario():
            limiter = ResizableLimiter(1)
            entered = []

            async def job(name):
                async with limiter:
                    entered.append(name)
                    await asyncio.sleep(0.2)

            first = asyncio.create_task(job("a"))
            second = asyncio.create_task(job("b"))
            await asyncio.sleep(0.05)
            assert entered == ["a"]
            await limiter.set_limit(2)
            await asyncio.sleep(0.05)
            assert entered == ["a", "b"]
            await asyncio.gather(first, second)
            assert limiter.active == 0

        run_async(scenario())

    def test_bajar_el_limite_no_corta_lo_que_esta_en_curso(self, run_async):
        from utils.limiter import ResizableLimiter

        async def scenario():
            limiter = ResizableLimiter(2)
            entered = []

            async def job(name):
                async with limiter:
                    entered.append(name)
                    await asyncio.sleep(0.1)

            tasks = [asyncio.create_task(job(n)) for n in "abc"]
            await asyncio.sleep(0.02)
            await limiter.set_limit(1)
            assert entered == ["a", "b"]
            await asyncio.gather(*tasks)
            assert entered == ["a", "b", "c"]

        run_async(scenario())
