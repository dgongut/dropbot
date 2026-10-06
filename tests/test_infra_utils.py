"""Utilidades sueltas: volúmenes, ficheros, limitador, traducciones, log,
donantes y carpetas de destino."""

import asyncio
import json
import logging
import os
from types import SimpleNamespace

import pytest

import config
import logger
import settings
import translations
from services import donors_service
from utils import file_helpers, mounts
from utils.limiter import ResizableLimiter


class TestVolumenes:
    def test_un_punto_de_montaje_segun_el_sistema(self, monkeypatch):
        monkeypatch.setattr(mounts.os.path, "ismount", lambda path: path == "/video")
        assert mounts.is_mounted("/video") is True

    def test_si_ismount_falla_se_mira_proc_mounts(self, monkeypatch, tmp_path):
        def broken(path):
            raise OSError("sin permiso")

        fake_mounts = tmp_path / "mounts"
        fake_mounts.write_text("overlay / overlay rw 0 0\n/dev/sda1 /video ext4 rw 0 0\nbroken\n")
        real_open = open
        monkeypatch.setattr(mounts.os.path, "ismount", broken)
        monkeypatch.setattr(mounts, "open", lambda path, *a, **k: real_open(
            fake_mounts if path == "/proc/mounts" else path, *a, **k), raising=False)

        assert mounts.is_mounted("/video") is True
        assert mounts.is_mounted("/audio") is False
        assert mounts.is_mounted("/vid") is False

    def test_sin_proc_mounts_no_esta_montado(self, monkeypatch):
        def no_proc(*args, **kwargs):
            raise OSError("no hay /proc")

        monkeypatch.setattr(mounts.os.path, "ismount", lambda path: False)
        monkeypatch.setattr(mounts, "open", no_proc, raising=False)
        assert mounts.is_mounted("/video") is False

    def test_montar_un_padre_tambien_persiste(self):
        mounted = {"/data"}
        assert mounts.is_persisted("/data/config/state", mounted=mounted.__contains__) is True
        assert mounts.is_persisted("/data", mounted=mounted.__contains__) is True
        assert mounts.is_persisted("/otro/config", mounted=mounted.__contains__) is False

    def test_la_raiz_del_contenedor_no_cuenta(self):
        assert mounts.is_persisted("/config", mounted=lambda path: path == "/") is False

    def test_una_ruta_relativa_se_resuelve(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        seen = []
        mounts.is_persisted("config", mounted=lambda path: seen.append(path) or False)
        assert seen[0] == str(tmp_path / "config")


class TestCarpetas:
    def test_has_own_folder(self, monkeypatch):
        paths = config.resolve_download_paths(mounted=lambda path: path == config.DOWNLOAD_AUDIO)
        monkeypatch.setattr(config, "DOWNLOAD_PATHS", paths)
        assert config.has_own_folder("audio") is True
        assert config.has_own_folder("url_audio") is True
        assert config.has_own_folder("video", "photo") is False
        assert config.has_own_folder("video", "audio") is True
        assert config.has_own_folder() is False

    def test_todo_montado(self):
        paths = config.resolve_download_paths(mounted=lambda path: True)
        assert paths == {
            "audio": config.DOWNLOAD_AUDIO, "video": config.DOWNLOAD_VIDEO,
            "photo": config.DOWNLOAD_PHOTO, "torrent": config.DOWNLOAD_TORRENT,
            "ebook": config.DOWNLOAD_EBOOK, "url_video": config.DOWNLOAD_URL_VIDEO,
            "url_audio": config.DOWNLOAD_URL_AUDIO,
        }

    def test_url_audio_sola_no_arrastra_al_audio(self):
        paths = config.resolve_download_paths(mounted=lambda path: path == config.DOWNLOAD_URL_AUDIO)
        assert paths["url_audio"] == config.DOWNLOAD_URL_AUDIO
        assert paths["audio"] == config.DOWNLOAD_PATH
        assert paths["url_video"] == config.DOWNLOAD_PATH


class TestFicheros:
    @pytest.mark.parametrize("size, text", [
        (0, "0.00 B"), (1023, "1023.00 B"), (1024, "1.00 KB"), (1536, "1.50 KB"),
        (1024 ** 2, "1.00 MB"), (5 * 1024 ** 3, "5.00 GB"), (1024 ** 4, "1.00 TB"),
        (3 * 1024 ** 5, "3.00 PB"),
    ])
    def test_format_file_size(self, size, text):
        assert file_helpers.format_file_size(size) == text

    def test_tamano_de_una_carpeta_recursivo(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x.bin").write_bytes(b"1" * 100)
        (tmp_path / "y.bin").write_bytes(b"1" * 50)
        # Un enlace roto no suma ni rompe la cuenta
        os.symlink(tmp_path / "no-existe", tmp_path / "roto")
        assert file_helpers.get_directory_size(str(tmp_path)) == 150

    def test_tamano_de_una_carpeta_que_no_existe(self, tmp_path):
        assert file_helpers.get_directory_size(str(tmp_path / "nada")) == 0

    def test_tamano_si_falla_a_mitad_devuelve_lo_contado(self, tmp_path, monkeypatch):
        (tmp_path / "x.bin").write_bytes(b"1" * 10)
        logged = []

        def broken(path):
            raise OSError("desaparecido")

        monkeypatch.setattr(file_helpers.os.path, "getsize", broken)
        monkeypatch.setattr(file_helpers, "warning", logged.append)
        assert file_helpers.get_directory_size(str(tmp_path)) == 0
        assert logged

    def test_nombre_unico(self, tmp_path):
        assert file_helpers.get_unique_filename(str(tmp_path), "a.mp4") == "a.mp4"
        (tmp_path / "a.mp4").touch()
        assert file_helpers.get_unique_filename(str(tmp_path), "a.mp4") == "a (1).mp4"
        (tmp_path / "a (1).mp4").touch()
        assert file_helpers.get_unique_filename(str(tmp_path), "a.mp4") == "a (2).mp4"
        (tmp_path / "sin_extension").touch()
        assert file_helpers.get_unique_filename(str(tmp_path), "sin_extension") == "sin_extension (1)"

    def test_reservar_con_sufijos(self, tmp_path):
        first = file_helpers.reserve_unique_path(str(tmp_path), "b.txt")
        second = file_helpers.reserve_unique_path(str(tmp_path), "b.txt")
        assert os.path.basename(first) == "b.txt"
        assert os.path.basename(second) == "b (1).txt"
        assert os.path.exists(second)

    def test_mover_copia_y_borra_el_origen(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"datos")
        file_helpers.copy_and_remove(str(src), str(dst))
        assert dst.read_bytes() == b"datos" and not src.exists()

    @pytest.mark.parametrize("extension, icon", [
        (".torrent", config.TOR_ICO), (".epub", config.BOO_ICO), (".mkv", config.VID_ICO),
        (".flac", config.AUD_ICO), (".png", config.IMG_ICO), (".zip", config.ZIP_ICO),
        (".rar", config.ZIP_ICO), (".xyz", config.DEF_ICO), ("", config.DEF_ICO),
    ])
    def test_icono_por_extension(self, extension, icon):
        assert file_helpers.get_file_icon(extension) == icon


class TestLimitador:
    def test_el_limite_minimo_es_uno(self, run_async):
        async def scenario():
            limiter = ResizableLimiter(0)
            low = limiter.limit
            await limiter.set_limit(-3)
            return low, limiter.limit

        assert run_async(scenario()) == (1, 1)

    def test_no_deja_pasar_a_mas_de_los_que_permite(self, run_async):
        async def scenario():
            limiter = ResizableLimiter(2)
            peak = 0

            async def job():
                nonlocal peak
                async with limiter:
                    peak = max(peak, limiter.active)
                    await asyncio.sleep(0.001)

            await asyncio.gather(*(job() for _ in range(6)))
            return peak, limiter.active

        assert run_async(scenario()) == (2, 0)

    def test_una_excepcion_dentro_libera_el_hueco(self, run_async):
        async def scenario():
            limiter = ResizableLimiter(1)
            with pytest.raises(RuntimeError):
                async with limiter:
                    raise RuntimeError("falla")
            await asyncio.wait_for(limiter.__aenter__(), timeout=1)
            return limiter.active

        assert run_async(scenario()) == 1


class TestTraducciones:
    @pytest.fixture
    def locales(self, monkeypatch, config_dir):
        books = {
            "es": {"saludo": "Hola $1, tienes $2", "solo_es": "sí"},
            "en": {"saludo": "Hello $1, you have $2", "solo_en": "english only"},
        }

        def load(locale):
            if locale not in books:
                raise FileNotFoundError(locale)
            return books[locale]

        monkeypatch.setattr(translations, "load_locale", load)
        logs = []
        monkeypatch.setattr(translations, "warning", lambda m: logs.append(("warning", m)))
        monkeypatch.setattr(translations, "error", lambda m: logs.append(("error", m)))
        books["logs"] = logs
        return books

    def test_placeholders(self, locales):
        assert translations.get_text("saludo", "Ana", 3) == "Hola Ana, tienes 3"

    def test_placeholders_que_sobran_o_faltan(self, locales):
        assert translations.get_text("saludo", "Ana") == "Hola Ana, tienes $2"
        assert translations.get_text("saludo", "Ana", 3, "extra") == "Hola Ana, tienes 3"

    def test_el_placeholder_10_no_lo_pisa_el_1(self, locales):
        locales["es"]["diez"] = "$1-$10"
        args = [f"a{i}" for i in range(1, 11)]
        assert translations.get_text("diez", *args) == "a1-a10"

    def test_un_valor_con_dolar_no_se_vuelve_a_sustituir(self, locales):
        """Un nombre de fichero como 'pago $2.pdf' no es un placeholder."""
        assert translations.get_text("saludo", "pago $2.pdf", 3) == "Hola pago $2.pdf, tienes 3"
        assert translations.get_text("saludo", "Ana", "$1") == "Hola Ana, tienes $1"

    def test_fallback_a_ingles(self, locales):
        assert translations.get_text("solo_en") == "english only"
        assert locales["logs"][0][0] == "warning"

    def test_clave_inexistente(self, locales):
        assert translations.get_text("no_existe") == "[MISSING: no_existe]"
        assert locales["logs"][0][0] == "error"

    def test_cambio_de_idioma_en_caliente(self, locales):
        assert translations.get_text("saludo", "Ana", 1).startswith("Hola")
        settings.put("language", "EN")
        assert translations.get_text("saludo", "Ana", 1).startswith("Hello")
        assert translations.get_text("solo_es") == "[MISSING: solo_es]"

    def test_un_idioma_sin_fichero_cae_en_ingles(self, locales, monkeypatch):
        monkeypatch.setattr(settings, "language", lambda: "FR")
        assert translations.get_text("saludo", "Ana", 2) == "Hello Ana, you have 2"
        assert any(level == "error" and "FR" in m for level, m in locales["logs"])

    def test_sin_ingles_tampoco_revienta(self, locales, monkeypatch):
        del locales["en"]
        assert translations.get_text("solo_en") == "[MISSING: solo_en]"

    def test_ficheros_reales_y_cache(self, config_dir):
        translations.clear_translation_cache()
        english = translations.load_locale("en")
        assert translations.load_locale("en") is english
        assert translations.load_locale.cache_info().hits >= 1
        translations.clear_translation_cache()
        assert translations.load_locale.cache_info().currsize == 0
        with pytest.raises(FileNotFoundError):
            translations.load_locale("xx")

    def test_un_locale_con_json_roto_cae_en_ingles(self, config_dir, monkeypatch, tmp_path):
        (tmp_path / "es.json").write_text("{roto", encoding="utf-8")
        (tmp_path / "en.json").write_text(json.dumps({"k": "ok"}), encoding="utf-8")
        monkeypatch.setattr(translations, "LOCALE_DIR", tmp_path)
        translations.clear_translation_cache()
        try:
            assert translations.get_text("k") == "ok"
        finally:
            translations.clear_translation_cache()


class TestLogger:
    @pytest.fixture
    def named(self, request):
        name = f"dropbot-test-{request.node.name}"
        yield name
        log = logging.getLogger(name)
        for handler in list(log.handlers):
            handler.close()
            log.removeHandler(handler)

    def test_solo_consola_por_defecto(self, named):
        log = logger.setup_logger(named)
        assert [type(h) for h in log.handlers] == [logging.StreamHandler]
        assert log.propagate is False
        # Llamarlo otra vez no duplica handlers
        assert logger.setup_logger(named) is log
        assert len(log.handlers) == 1

    def test_con_fichero_rota_y_escribe(self, named, tmp_path):
        path = tmp_path / "logs" / "dropbot.log"
        log = logger.setup_logger(named, log_file=str(path), log_level="DEBUG", use_colors=False)
        log.debug("una línea de depuración")
        for handler in log.handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
        assert "Log file enabled" in text and "una línea de depuración" in text

    def test_un_fichero_imposible_no_tumba_el_log(self, named, tmp_path, capsys):
        blocker = tmp_path / "fichero"
        blocker.write_text("x")
        log = logger.setup_logger(named, log_file=str(blocker / "sub" / "x.log"))
        assert len(log.handlers) == 1

    def test_colores_solo_en_un_terminal(self):
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "mal", None, None)
        formatter = logger.ColoredFormatter(fmt="%(levelname)s %(message)s")
        formatter.use_colors = True
        assert formatter.format(record).startswith("\033[31mERROR\033[0m")

        plain = logger.ColoredFormatter(fmt="%(levelname)s", use_colors=False)
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
        assert plain.format(record) == "INFO"

    def test_funciones_de_conveniencia(self, monkeypatch):
        seen = []

        class Recorder:
            def __getattr__(self, level):
                return lambda message: seen.append((level, message))

        monkeypatch.setattr(logger, "_logger", Recorder())
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
        logger.critical("c")
        assert seen == [("debug", "d"), ("info", "i"), ("warning", "w"), ("error", "e"), ("critical", "c")]

    def test_get_logger_crea_uno_solo(self, monkeypatch):
        monkeypatch.setattr(logger, "_logger", None)
        first = logger.get_logger()
        assert logger.get_logger() is first
        assert first.name == "dropbot"


class FakeResponse:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class TestDonantes:
    @pytest.fixture
    def http(self, monkeypatch):
        state = SimpleNamespace(response=None, calls=[])
        logged = []

        def get(url, headers=None, timeout=None):
            state.calls.append((url, headers, timeout))
            if isinstance(state.response, Exception):
                raise state.response
            return state.response

        monkeypatch.setattr(donors_service.requests, "get", get)
        monkeypatch.setattr(donors_service, "error", logged.append)
        state.logged = logged
        return state

    def test_lista_ordenada(self, http, run_async):
        http.response = FakeResponse(data=["zeta", "Alfa", "beta"])
        assert run_async(donors_service.get_array_donors_online()) == ["Alfa", "beta", "zeta"]
        url, headers, timeout = http.calls[0]
        assert url == config.DONORS_URL
        assert headers["Cache-Control"] == "no-cache"
        assert timeout == 10

    @pytest.mark.parametrize("response", [
        FakeResponse(data={"no": "lista"}),
        FakeResponse(data=ValueError("no json"), text="<html>"),
        FakeResponse(status=500),
        ConnectionError("sin red"),
        FakeResponse(data=[1, "a"]),  # no se puede ordenar
    ])
    def test_cualquier_fallo_da_lista_vacia(self, http, run_async, response):
        http.response = response
        assert run_async(donors_service.get_array_donors_online()) == []
        assert http.logged

    @pytest.fixture
    def sent(self, monkeypatch, config_dir):
        messages = []

        async def send(chat_id, text, **kwargs):
            messages.append((chat_id, text, kwargs))

        monkeypatch.setattr(donors_service, "safe_send_message", send)
        return messages

    def test_print_donors_en_html(self, http, sent, run_async):
        http.response = FakeResponse(data=["Bea", "Ana"])
        run_async(donors_service.print_donors(42))
        chat_id, text, kwargs = sent[0]
        assert chat_id == 42
        assert "· Ana\n· Bea\n" in text
        assert kwargs == {"parse_mode": "HTML"}

    def test_print_donors_escapa_los_nombres(self, http, sent, run_async):
        """El mensaje va en HTML: un nombre con < o & no puede romperlo."""
        http.response = FakeResponse(data=["<b>Ana</b> & Co"])
        run_async(donors_service.print_donors(42))
        assert "· &lt;b&gt;Ana&lt;/b&gt; &amp; Co\n" in sent[0][1]

    def test_la_peticion_no_bloquea_el_event_loop(self, http, run_async, monkeypatch):
        import threading
        loop_thread = threading.get_ident()
        threads = []
        http.response = FakeResponse(data=["Ana"])
        real_get = donors_service.requests.get

        def get(*args, **kwargs):
            threads.append(threading.get_ident())
            return real_get(*args, **kwargs)

        monkeypatch.setattr(donors_service.requests, "get", get)
        assert run_async(donors_service.get_array_donors_online()) == ["Ana"]
        assert threads and threads[0] != loop_thread

    def test_print_donors_sin_lista_avisa(self, http, sent, run_async):
        http.response = FakeResponse(status=404)
        run_async(donors_service.print_donors(42))
        chat_id, text, kwargs = sent[0]
        assert text == translations.get_text("error_getting_donors")
        assert kwargs == {"parse_mode": translations.PARSE_MODE}


class TestNombresComoCodigoEnLasTraducciones:
    """Un hueco `$1` en una traducción es un nombre que va como código."""

    def test_un_nombre_normal_queda_igual_que_siempre(self, config_dir):
        from translations import get_text

        assert get_text("downloaded", "🎶", "tema.mp3").endswith("`tema.mp3`")

    def test_una_comilla_invertida_no_mutila_el_nombre(self, config_dir):
        from basic import md_code
        from translations import get_text

        name = "canción `remix`.mp3"
        assert md_code(name) in get_text("downloaded", "🎶", name)

    def test_un_hueco_sin_comillas_no_cambia(self, config_dir):
        from translations import get_text

        assert get_text("version", "4.0.0").startswith("⚙️ **Versión:** 4.0.0")
