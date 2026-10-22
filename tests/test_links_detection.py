"""Enlaces: qué hace el bot al recibir uno y con cada botón que ofrece.

Cubre la detección (descarga directa, playlist, tipo de contenido), lo que se
le pregunta al usuario, y que cada botón descargue una sola vez aunque se
pulse dos. yt-dlp no se lanza: se sustituye por un script falso en el PATH o se
intercepta `run_url_download`.
"""

import asyncio
import json
import os
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telethon import types
from telethon.extensions import markdown

import settings
from button_data import button_data
from conftest import FORUM_TOPIC
from proc_helpers import alive
from translations import load_locale


# --- utilidades ---------------------------------------------------------------

def _flat(buttons):
    rows = buttons or []
    for row in rows if isinstance(rows, (list, tuple)) else [rows]:
        for button in row if isinstance(row, (list, tuple)) else [row]:
            yield button_data(button)


def _all_buttons(sent_messages):
    return [d for _, _, kwargs in sent_messages for d in _flat(kwargs.get("buttons"))]


@pytest.fixture
def fake_ytdlp(tmp_path, monkeypatch):
    """Un `yt-dlp` falso que imprime `stdout` y sale con `code`.

    Como el de verdad, al salir reescribe el fichero de `--cookies`; si es el
    de FAKE_YTDLP_READ_ONLY, falla como en un volumen montado `:ro`.
    `.calls()` devuelve los argumentos de cada ejecución.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_YTDLP_LOG", str(log))

    def install(stdout="", code=0, stderr=""):
        script = bindir / "yt-dlp"
        script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f'''
            import json, os, sys
            with open(os.environ["FAKE_YTDLP_LOG"], "a") as log:
                log.write(json.dumps(sys.argv[1:]) + "\\n")
            sys.stdout.write({stdout!r})
            sys.stderr.write({stderr!r})
            if "--cookies" in sys.argv:
                jar = sys.argv[sys.argv.index("--cookies") + 1]
                if jar == os.environ.get("FAKE_YTDLP_READ_ONLY"):
                    sys.stderr.write("OSError: [Errno 30] Read-only file system\\n")
                    sys.exit(1)
                with open(jar, "a") as handle:
                    handle.write("# renovada\\n")
            sys.exit({code})
        '''))
        script.chmod(0o755)

    install.calls = lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return install


# Como contra un servidor que suelta un byte de vez en cuando: no acaba nunca,
# y deja un hijo con sus tuberías, igual que el ffmpeg de un directo
HANGING_YTDLP = '''
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pids = os.environ["FAKE_YTDLP_PIDS"]
with open(pids + ".tmp", "w") as handle:
    handle.write(f"{os.getpid()} {child.pid}")
os.replace(pids + ".tmp", pids)
time.sleep(60)
'''


@pytest.fixture
def hanging_ytdlp(tmp_path, monkeypatch):
    """Instala un `yt-dlp` que no termina; `await started()` da su pid y el del hijo."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pids = tmp_path / "ytdlp-pids"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_YTDLP_PIDS", str(pids))
    script = bindir / "yt-dlp"
    script.write_text(f"#!{sys.executable}\n{HANGING_YTDLP}")
    script.chmod(0o755)

    async def started():
        for _ in range(500):
            if pids.exists():
                return [int(pid) for pid in pids.read_text().split()]
            await asyncio.sleep(0.01)
        raise AssertionError("el yt-dlp falso no llegó a arrancar")

    return started


@pytest.fixture
def launched(quiet_bot, monkeypatch, config_dir):
    """`run_url_download` interceptado: anota cada descarga que se lanzaría."""
    calls = []

    def fake_run(event, cmd, status_message, final_output_dir, **kwargs):
        calls.append(SimpleNamespace(cmd=cmd, status=status_message, final=final_output_dir, **kwargs))
        return asyncio.sleep(0)

    monkeypatch.setattr(quiet_bot, "run_url_download", fake_run)
    quiet_bot.pending_urls.clear()
    quiet_bot.claimed_urls.clear()
    quiet_bot.active_tasks.clear()
    yield quiet_bot, calls
    quiet_bot.pending_urls.clear()
    quiet_bot.claimed_urls.clear()
    quiet_bot.active_tasks.clear()


def _run_and_drain(run_async, coro_factory):
    """Ejecuta handlers y deja terminar las tareas que hayan lanzado."""
    async def scenario():
        await coro_factory()
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    run_async(scenario())


# --- is_direct_download_url ---------------------------------------------------

class TestDescargaDirectaPorExtension:
    @pytest.fixture(autouse=True)
    def no_network(self, dropbot, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("con extensión conocida no hace falta preguntar al servidor")
        monkeypatch.setattr(dropbot.requests.Session, "head", forbidden)

    @pytest.mark.parametrize("url,kind,folder", [
        ("https://cdn.example.com/peli.mkv", "video", "video"),
        ("https://cdn.example.com/tema.flac", "audio", "audio"),
        ("https://cdn.example.com/foto.webp", "image", "photo"),
        ("https://cdn.example.com/ubuntu.torrent", "torrent", "torrent"),
        ("https://cdn.example.com/libro.epub", "ebook", "ebook"),
    ])
    def test_cada_tipo_a_su_carpeta(self, dropbot, run_async, url, kind, folder):
        is_direct, filename, content_type, path, icon = run_async(dropbot.is_direct_download_url(url))

        assert is_direct
        assert filename == os.path.basename(url)
        assert content_type == kind
        assert path == dropbot.DOWNLOAD_PATHS[folder]
        assert icon

    def test_ignora_la_query_y_decodifica_el_nombre(self, dropbot, run_async):
        url = "https://cdn.example.com/m%C3%BAsica/Mi%20Canci%C3%B3n.MP3?token=abc&exp=1"

        is_direct, filename, content_type, *_ = run_async(dropbot.is_direct_download_url(url))

        assert is_direct and content_type == "audio"
        assert filename == "Mi Canción.MP3"

    def test_un_nombre_con_caracteres_prohibidos_se_sanea(self, dropbot, run_async):
        url = 'https://cdn.example.com/a%3Ab%22c%7C.mp4'

        _, filename, *_ = run_async(dropbot.is_direct_download_url(url))

        assert filename.endswith(".mp4")
        assert not set(':"|') & set(filename)

    def test_una_pagina_html_no_es_una_descarga_directa(self, dropbot, run_async, monkeypatch):
        url = "https://www.lasexta.com/noticias/video-del-dia_2026100660000000.html"

        def fake_head(session, url, **kwargs):
            # Solo se miran sus redirecciones: aunque diga que es una descarga
            return SimpleNamespace(headers={"Content-Disposition": "attachment"}, is_redirect=False)

        monkeypatch.setattr(dropbot.requests.Session, "head", fake_head)

        is_direct, *_ = run_async(dropbot.is_direct_download_url(url))

        assert not is_direct


class TestDescargaDirectaPorCabeceras:
    def _head(self, dropbot, monkeypatch, headers=None, raises=None):
        calls = []

        def fake_head(session, url, **kwargs):
            calls.append((url, kwargs))
            if raises:
                raise raises
            return SimpleNamespace(headers=headers or {}, is_redirect=False)

        monkeypatch.setattr(dropbot.requests.Session, "head", fake_head)
        return calls

    def test_una_pagina_normal_no_es_directa(self, dropbot, run_async, monkeypatch):
        calls = self._head(dropbot, monkeypatch, {"Content-Type": "text/html; charset=utf-8"})

        result = run_async(dropbot.is_direct_download_url("https://www.youtube.com/watch?v=abc"))

        assert result == (False, None, None, None, None)
        (url, kwargs), = calls
        # Las redirecciones se siguen a mano, comprobando cada salto
        assert kwargs["allow_redirects"] is False and kwargs["timeout"] == 5

    def test_si_el_servidor_no_responde_no_es_directa(self, dropbot, run_async, monkeypatch):
        import requests

        self._head(dropbot, monkeypatch, raises=requests.ConnectionError("sin red"))

        assert run_async(dropbot.is_direct_download_url("https://x.example/watch"))[0] is False

    def test_un_servidor_que_gotea_no_cuelga_el_analisis_ni_ocupa_el_executor(self, dropbot, run_async, monkeypatch):
        """Las cabeceras a goteo: el timeout de requests es por lectura, no total."""
        import requests

        release = threading.Event()

        def dripping_head(session, url, **kwargs):
            release.wait(5)
            raise requests.ConnectionError("goteo")

        monkeypatch.setattr(dropbot.requests.Session, "head", dripping_head)
        monkeypatch.setattr(dropbot, "HEAD_TIMEOUT", 0.2)

        async def scenario():
            # Un executor de un solo hilo: el HEAD no puede quedarse con él
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
            started = time.monotonic()
            result = await dropbot.is_direct_download_url("https://evil.example/descarga")
            copied = await asyncio.wait_for(asyncio.to_thread(lambda: "copiado"), 2)
            return result, copied, time.monotonic() - started

        try:
            result, copied, elapsed = run_async(scenario())
        finally:
            release.set()

        assert result == (False, None, None, None, None)
        assert copied == "copiado"
        assert elapsed < 2

    @pytest.mark.parametrize("ctype,kind,folder", [
        ("video/mp4", "video", "video"),
        ("audio/mpeg", "audio", "audio"),
        ("image/png", "image", "photo"),
        ("application/x-bittorrent", "torrent", "torrent"),
    ])
    def test_el_tipo_sale_del_content_type(self, dropbot, run_async, monkeypatch, ctype, kind, folder):
        self._head(dropbot, monkeypatch, {
            "Content-Type": ctype, "Content-Disposition": 'attachment; filename="fichero.bin"',
        })

        is_direct, filename, content_type, path, _ = run_async(
            dropbot.is_direct_download_url("https://x.example/get?id=1"))

        assert (is_direct, filename, content_type, path) == (True, "fichero.bin", kind, dropbot.DOWNLOAD_PATHS[folder])

    def test_un_tipo_desconocido_va_a_la_carpeta_general(self, dropbot, run_async, monkeypatch):
        self._head(dropbot, monkeypatch, {
            "Content-Type": "application/zip", "Content-Disposition": "attachment; filename=datos.zip",
        })

        _, filename, content_type, path, _ = run_async(dropbot.is_direct_download_url("https://x.example/dl"))

        assert (filename, content_type, path) == ("datos.zip", "file", dropbot.DOWNLOAD_PATH)

    def test_attachment_sin_nombre_usa_el_de_la_url(self, dropbot, run_async, monkeypatch):
        self._head(dropbot, monkeypatch, {"Content-Type": "application/octet-stream",
                                          "Content-Disposition": "attachment"})

        _, filename, *_ = run_async(dropbot.is_direct_download_url("https://x.example/files/informe"))

        assert filename == "informe"

    def test_attachment_sin_nombre_ni_ruta(self, dropbot, run_async, monkeypatch):
        self._head(dropbot, monkeypatch, {"Content-Disposition": "attachment"})

        _, filename, *_ = run_async(dropbot.is_direct_download_url("https://x.example/"))

        assert filename == "download"

    def test_filename_estrella_codificado(self, dropbot, run_async, monkeypatch):
        self._head(dropbot, monkeypatch, {
            "Content-Type": "application/pdf",
            "Content-Disposition": "attachment; filename*=UTF-8''Informe%20final.pdf",
        })

        _, filename, *_ = run_async(dropbot.is_direct_download_url("https://x.example/dl?id=7"))

        assert filename == "Informe final.pdf"


# --- detect_playlist / detect_content_type ------------------------------------

def _jsonl(*items):
    return "".join(json.dumps(item) + "\n" for item in items)


class TestDetectPlaylist:
    def test_varias_lineas_son_una_playlist(self, dropbot, fake_ytdlp, run_async):
        fake_ytdlp(_jsonl(*({"id": str(i), "playlist_title": "Mi lista"} for i in range(4))))

        assert run_async(dropbot.detect_playlist("https://x/list")) == (True, 4, "Mi lista")
        (args,) = fake_ytdlp.calls()
        assert args[:3] == ["--flat-playlist", "--dump-json", "--no-warnings"]
        assert args[-1] == "https://x/list"

    def test_sin_playlist_title_usa_playlist(self, dropbot, fake_ytdlp, run_async):
        fake_ytdlp(_jsonl({"id": "1", "playlist": "Canal"}, {"id": "2"}))

        assert run_async(dropbot.detect_playlist("https://x/list")) == (True, 2, "Canal")

    def test_un_json_roto_no_impide_detectarla(self, dropbot, fake_ytdlp, run_async):
        fake_ytdlp("no es json\n{}\n")

        assert run_async(dropbot.detect_playlist("https://x/list")) == (True, 2, "Playlist")

    @pytest.mark.parametrize("item,title", [
        ({"playlist_title": None, "playlist": "Canal"}, "Canal"),
        ({"playlist_title": None, "playlist": None}, "Playlist"),
        ({"playlist_title": ""}, "Playlist"),
    ])
    def test_un_titulo_vacio_no_sale_como_none(self, dropbot, fake_ytdlp, run_async, item, title):
        fake_ytdlp(_jsonl(item, {"id": "2"}))

        assert run_async(dropbot.detect_playlist("https://x/list")) == (True, 2, title)

    def test_un_titulo_enorme_se_recorta(self, dropbot, fake_ytdlp, run_async):
        """Lo pone quien crea la playlist: uno de miles de caracteres hacía que
        Telegram rechazara la pregunta y el mensaje se quedaba en "Analizando"."""
        fake_ytdlp(_jsonl({"playlist_title": "x" * 5000}, {"id": "2"}))

        _, _, title = run_async(dropbot.detect_playlist("https://x/list"))

        assert title == "x" * 200

    @pytest.mark.parametrize("mode,limit,end", [
        ("FIRST", 0, "2"), ("FULL", 10, "11"), ("FULL", 0, None), ("ASK", 10, None),
    ])
    def test_con_el_modo_fijo_no_recorre_la_playlist_entera(
        self, dropbot, fake_ytdlp, run_async, config_dir, mode, limit, end
    ):
        """Recorrer un canal de miles de vídeos lleva minutos, y con el modo fijo
        el total no se enseña: basta con saber si pasa del tope. Al preguntar sí
        se dice cuántos hay."""
        settings.put("urls.playlist", mode)
        settings.put("urls.playlist_limit", limit)
        fake_ytdlp(_jsonl({"id": "1"}, {"id": "2"}))

        assert run_async(dropbot.detect_playlist("https://x/canal"))[0] is True

        (args,) = fake_ytdlp.calls()
        if end is None:
            assert "--playlist-end" not in args
        else:
            assert args[args.index("--playlist-end") + 1] == end
        assert args[-1] == "https://x/canal"

    def test_si_ytdlp_no_acaba_se_corta_sin_dejarlo_vivo(
        self, dropbot, hanging_ytdlp, run_async, monkeypatch
    ):
        """Su timeout es por lectura: un servidor que soltaba un byte de vez en
        cuando lo tenía esperando para siempre, y cada reintento sumaba otro."""
        monkeypatch.setattr(dropbot, "YTDLP_DETECT_TIMEOUT", 0.5)

        async def scenario():
            task = asyncio.create_task(dropbot.detect_playlist("https://x/goteo"))
            pids = await hanging_ytdlp()
            done, _ = await asyncio.wait([task], timeout=10)
            if not done:
                task.cancel()
            return done, task, pids

        done, task, pids = run_async(scenario())

        assert done, "detect_playlist no ha vuelto"
        # Ni vídeo suelto ni playlist: como vídeo suelto se bajaría entera, sin tope
        with pytest.raises(TimeoutError):
            task.result()
        assert not any(alive(pid) for pid in pids), "ni yt-dlp ni lo que lanzó siguen vivos"

    def test_una_linea_es_un_video_suelto(self, dropbot, fake_ytdlp, run_async):
        fake_ytdlp(_jsonl({"id": "1"}))

        assert run_async(dropbot.detect_playlist("https://x/v")) == (False, 1, None)

    def test_si_ytdlp_falla_se_trata_como_video_suelto(self, dropbot, fake_ytdlp, run_async):
        fake_ytdlp(_jsonl({"id": "1"}, {"id": "2"}), code=1, stderr="ERROR: Unsupported URL\n")

        assert run_async(dropbot.detect_playlist("https://x/v")) == (False, 1, None)

    def test_sin_salida(self, dropbot, fake_ytdlp, run_async):
        fake_ytdlp("")

        assert run_async(dropbot.detect_playlist("https://x/v")) == (False, 1, None)

    def test_sin_ytdlp_instalado(self, dropbot, run_async, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))

        assert run_async(dropbot.detect_playlist("https://x/v")) == (False, 1, None)

    def test_usa_las_cookies_si_existen(self, dropbot, fake_ytdlp, run_async, tmp_path, monkeypatch):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))
        fake_ytdlp(_jsonl({"id": "1"}))

        run_async(dropbot.detect_playlist("https://x/v"))

        (args,) = fake_ytdlp.calls()
        assert args[0] == "--cookies"
        # Una copia propia, que se borra al terminar: el original no se toca
        assert args[1] != str(cookies) and not os.path.exists(args[1])
        assert cookies.read_text() == "# Netscape HTTP Cookie File\n"

    def test_con_las_cookies_de_solo_lectura_la_playlist_se_detecta(
        self, dropbot, fake_ytdlp, run_async, tmp_path, monkeypatch
    ):
        """Con el volumen montado `:ro`, yt-dlp fallaba al guardarlas al salir y la
        playlist pasaba por un vídeo suelto: se bajaba entera, sin preguntar ni tope."""
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))
        monkeypatch.setenv("FAKE_YTDLP_READ_ONLY", str(cookies))
        fake_ytdlp(_jsonl({"id": "1", "playlist_title": "Mi lista"}, {"id": "2"}))

        assert run_async(dropbot.detect_playlist("https://x/list")) == (True, 2, "Mi lista")


class TestDetectContentType:
    @pytest.mark.parametrize("item,expected", [
        ({"vcodec": "avc1.64001F", "acodec": "mp4a.40.2", "ext": "mp4"}, "video"),
        ({"vcodec": "none", "acodec": "opus", "ext": "webm"}, "audio"),
        ({"acodec": "mp3", "ext": "mp3"}, "audio"),
        ({"vcodec": "none", "acodec": "none", "ext": "JPG"}, "image"),
        ({"vcodec": None, "acodec": None, "ext": "webp"}, "image"),
        ({"vcodec": "none", "acodec": "none", "ext": "html"}, "unknown"),
        ({}, "unknown"),
    ])
    def test_el_tipo_sale_de_los_codecs(self, dropbot, fake_ytdlp, run_async, item, expected):
        fake_ytdlp(_jsonl(item, {"vcodec": "h264"}))

        assert run_async(dropbot.detect_content_type("https://x/v")) == expected

    def test_pide_solo_el_primero_sin_descargar(self, dropbot, fake_ytdlp, run_async):
        fake_ytdlp(_jsonl({"vcodec": "vp9"}))

        run_async(dropbot.detect_content_type("https://x/v"))

        (args,) = fake_ytdlp.calls()
        assert "--skip-download" in args
        assert args[args.index("--playlist-items") + 1] == "1"

    @pytest.mark.parametrize("stdout,code", [("", 0), ("{roto\n", 0), (_jsonl({"vcodec": "h264"}), 2)])
    def test_lo_que_no_se_puede_leer_es_desconocido(self, dropbot, fake_ytdlp, run_async, stdout, code):
        fake_ytdlp(stdout, code=code, stderr="WARNING: algo\n")

        assert run_async(dropbot.detect_content_type("https://x/v")) == "unknown"

    def test_si_ytdlp_no_acaba_es_desconocido_sin_dejarlo_vivo(
        self, dropbot, hanging_ytdlp, run_async, monkeypatch
    ):
        monkeypatch.setattr(dropbot, "YTDLP_DETECT_TIMEOUT", 0.5)

        async def scenario():
            task = asyncio.create_task(dropbot.detect_content_type("https://x/goteo"))
            pids = await hanging_ytdlp()
            done, _ = await asyncio.wait([task], timeout=10)
            if not done:
                task.cancel()
            return done, task, pids

        done, task, pids = run_async(scenario())

        assert done, "detect_content_type no ha vuelto"
        assert task.result() == "unknown"
        assert not any(alive(pid) for pid in pids)

    def test_con_las_cookies_de_solo_lectura_se_detecta_el_tipo(
        self, dropbot, fake_ytdlp, run_async, tmp_path, monkeypatch
    ):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))
        monkeypatch.setenv("FAKE_YTDLP_READ_ONLY", str(cookies))
        fake_ytdlp(_jsonl({"vcodec": "avc1", "acodec": "mp4a.40.2", "ext": "mp4"}))

        assert run_async(dropbot.detect_content_type("https://x/v")) == "video"
        (args,) = fake_ytdlp.calls()
        assert not os.path.exists(args[args.index("--cookies") + 1]), "la copia se borra"


def test_el_analisis_no_deja_en_el_log_la_contrasena_ni_los_tokens_de_la_url(
    dropbot, fake_ytdlp, run_async, monkeypatch
):
    """En Docker el log va entero a `docker logs`: lo que yt-dlp dice al
    analizar repite la URL, con el usuario:contraseña de un enlace al NAS."""
    url = "https://admin:MiClave@nas.local/video?token=secreto"
    page = "https://admin:MiClave@nas.local/noticia.html?token=secreto"
    logged = []
    monkeypatch.setattr(dropbot, "debug", logged.append)
    fake_ytdlp("", code=1, stderr=f"ERROR: [generic] Unable to download webpage: {url}\n")

    def unreachable(session, url, **kwargs):
        # Como requests: el error repite la ruta y la query
        raise dropbot.requests.ConnectionError("Max retries exceeded with url: /noticia.html?token=secreto")

    monkeypatch.setattr(dropbot.requests.Session, "head", unreachable)

    run_async(dropbot.detect_playlist(url))
    run_async(dropbot.detect_content_type(url))
    run_async(dropbot.is_direct_download_url(page))

    assert all(args[-1] == url for args in fake_ytdlp.calls()), "yt-dlp recibe la URL entera"
    log = "\n".join(logged)
    assert "nas.local/video" in log and "nas.local/noticia.html" in log
    assert "MiClave" not in log and "secreto" not in log


# --- calculate_ytdlp_sleep_interval / add_ytdlp_cookies -----------------------

@pytest.mark.parametrize("count,seconds", [
    (0, 0), (1, 0), (2, 1), (10, 1), (11, 2), (50, 2), (51, 5), (100, 5),
    (101, 8), (200, 8), (201, 12), (300, 12), (301, 18), (500, 18), (501, 25), (5000, 25),
])
def test_la_pausa_entre_videos_crece_con_la_playlist(dropbot, count, seconds):
    assert dropbot.calculate_ytdlp_sleep_interval(count) == seconds


class TestCookies:
    def test_sin_fichero_el_comando_no_cambia(self, dropbot, tmp_path, monkeypatch):
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(tmp_path / "no-hay.txt"))
        cmd = ["yt-dlp", "-f", "best", "URL"]

        assert dropbot.add_ytdlp_cookies(cmd) == ["yt-dlp", "-f", "best", "URL"]

    def test_con_fichero_va_una_copia_justo_tras_el_ejecutable(self, dropbot, tmp_path, monkeypatch):
        """yt-dlp reescribe al salir el fichero que recibe: con el original, los
        que iban a la vez se pisaban entre sí y pisaban unas cookies recién
        exportadas."""
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))
        cmd = ["yt-dlp", "-f", "best", "URL"]

        produced = dropbot.add_ytdlp_cookies(cmd)
        copy = produced[2]
        try:
            assert produced[:2] == ["yt-dlp", "--cookies"] and produced[3:] == ["-f", "best", "URL"]
            assert copy != str(cookies) and os.path.dirname(copy) == dropbot.TEMP_DIR
            with open(copy) as handle:
                assert handle.read() == "# Netscape HTTP Cookie File\n"
            assert cmd == ["yt-dlp", "-f", "best", "URL"], "no modifica la lista original"
        finally:
            dropbot.release_ytdlp_cookies(produced)

        assert not os.path.exists(copy)

    def test_cada_ytdlp_tiene_su_copia(self, dropbot, tmp_path, monkeypatch):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))

        first = dropbot.add_ytdlp_cookies(["yt-dlp", "URL"])
        second = dropbot.add_ytdlp_cookies(["yt-dlp", "URL"])
        dropbot.release_ytdlp_cookies(first)
        dropbot.release_ytdlp_cookies(second)

        assert first[2] != second[2]

    def test_una_carpeta_con_ese_nombre_no_cuenta(self, dropbot, tmp_path, monkeypatch):
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(tmp_path))

        assert dropbot.add_ytdlp_cookies(["yt-dlp", "URL"]) == ["yt-dlp", "URL"]

    def test_soltar_un_comando_sin_copia_no_borra_nada(self, dropbot, tmp_path, monkeypatch):
        """Solo se borran las copias: nunca el fichero del admin."""
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))

        dropbot.release_ytdlp_cookies(["yt-dlp", "--cookies", str(cookies), "URL"], save_renewed=True)

        assert cookies.exists()

    def test_lo_renovado_solo_se_guarda_si_se_pide(self, dropbot, tmp_path, monkeypatch):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# sesion\n")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))

        for save_renewed, expected in ((False, "# sesion\n"), (True, "# sesion\n# renovada\n")):
            cmd = dropbot.add_ytdlp_cookies(["yt-dlp", "URL"])
            with open(cmd[2], "a") as jar:
                jar.write("# renovada\n")

            dropbot.release_ytdlp_cookies(cmd, save_renewed=save_renewed)

            assert cookies.read_text() == expected
            assert not os.path.exists(cmd[2])

    def test_de_solo_lectura_no_se_intenta_guardar_lo_renovado(self, dropbot, tmp_path, monkeypatch):
        """`os.access` da False en un volumen `:ro` aunque el bot corra como root."""
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# sesion\n")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))
        cmd = dropbot.add_ytdlp_cookies(["yt-dlp", "URL"])
        with open(cmd[2], "a") as jar:
            jar.write("# renovada\n")
        real_access = os.access
        monkeypatch.setattr(os, "access", lambda path, mode, **kwargs: (
            path != str(cookies) and real_access(path, mode, **kwargs)))

        dropbot.release_ytdlp_cookies(cmd, save_renewed=True)

        assert cookies.read_text() == "# sesion\n"
        assert not os.path.exists(cmd[2])

    def test_la_descarga_tambien_las_usa(self, launched, make_event, run_async, tmp_path, monkeypatch):
        dropbot, calls = launched
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))

        _run_and_drain(run_async, lambda: dropbot.start_ytdlp_download(make_event(), None, "https://x/v", False))

        cmd = calls[0].cmd
        try:
            assert cmd[:2] == ["yt-dlp", "--cookies"] and cmd[2] != str(cookies)
            with open(cmd[2]) as handle:
                assert handle.read() == "# Netscape HTTP Cookie File\n"
        finally:
            # Aquí no corre run_url_download, que es quien la suelta
            dropbot.release_ytdlp_cookies(cmd)


# --- handle_url_link ----------------------------------------------------------

class TestAlRecibirUnEnlace:
    @pytest.fixture
    def link(self, launched, monkeypatch):
        dropbot, calls = launched
        seen = SimpleNamespace(direct_urls=[], direct_runs=[], playlist=(False, 1, None), content="video")

        async def not_direct(url):
            seen.direct_urls.append(url)
            return False, None, None, None, None

        async def playlist(url):
            return seen.playlist

        async def content(url):
            return seen.content

        async def direct_run(*args):
            seen.direct_runs.append(args)

        monkeypatch.setattr(dropbot, "is_direct_download_url", not_direct)
        monkeypatch.setattr(dropbot, "detect_playlist", playlist)
        monkeypatch.setattr(dropbot, "detect_content_type", content)
        monkeypatch.setattr(dropbot, "run_direct_download", direct_run)
        return dropbot, calls, seen

    def _send(self, dropbot, make_event, run_async, text="https://www.youtube.com/watch?v=abc", event_id=301, chat_id=42):
        event = make_event(event_id=event_id, chat_id=chat_id)
        event.raw_text = text
        _run_and_drain(run_async, lambda: dropbot.handle_url_link(event))
        return event

    def test_un_enlace_directo_se_descarga_sin_preguntar(
        self, launched, make_event, run_async, sent_messages, monkeypatch
    ):
        dropbot, calls = launched
        runs = []

        async def direct_run(event, url, filename, status, folder, icon, kind):
            runs.append((url, filename, folder, kind))

        monkeypatch.setattr(dropbot, "run_direct_download", direct_run)

        self._send(dropbot, make_event, run_async, "https://cdn.example.com/peli.mkv", event_id=302)

        assert runs == [("https://cdn.example.com/peli.mkv", "peli.mkv", dropbot.DOWNLOAD_PATHS["video"], "video")]
        assert "42_302" not in dropbot.pending_urls
        assert not calls, "no pasa por yt-dlp"
        kinds = [kind for kind, _, _ in sent_messages]
        assert kinds == ["reply", "edit"]
        assert "Analizando" in sent_messages[0][1] and "Descargando" in sent_messages[1][1]
        # El Cancelar del análisis y el de la descarga paran lo mismo
        assert _all_buttons(sent_messages) == [b"cancel:42_302", b"cancel:42_302"]

    def test_un_video_pregunta_audio_o_video(self, link, make_event, run_async, sent_messages):
        dropbot, calls, seen = link

        self._send(dropbot, make_event, run_async)

        assert not calls
        assert dropbot.pending_urls["42_301"] == {"url": "https://www.youtube.com/watch?v=abc", "playlist_count": 1}
        assert sent_messages[-1][0] == "edit" and "¿Qué deseas descargar?" in sent_messages[-1][1]
        assert _all_buttons(sent_messages) == [
            b"cancel:42_301", b"url_audio:42_301", b"url_video:42_301", b"simplecancel:42_301"
        ]
        assert "42_301" not in dropbot.active_tasks, "con la pregunta, el análisis ya no se cancela"

    @pytest.mark.parametrize("content,question,buttons", [
        ("image", "imagen", [b"cancel:42_301", b"url_video:42_301", b"simplecancel:42_301"]),
        ("audio", "audio", [b"cancel:42_301", b"url_audio:42_301", b"simplecancel:42_301"]),
        ("unknown", "contenido", [b"cancel:42_301", b"url_video:42_301", b"simplecancel:42_301"]),
    ])
    def test_cada_tipo_tiene_su_pregunta(self, link, make_event, run_async, sent_messages, content, question, buttons):
        dropbot, _, seen = link
        seen.content = content

        self._send(dropbot, make_event, run_async)

        assert question in sent_messages[-1][1]
        assert _all_buttons(sent_messages) == buttons

    @pytest.mark.parametrize("fmt,is_audio", [("VIDEO", False), ("AUDIO", True)])
    def test_con_formato_automatico_descarga_directamente(self, link, make_event, run_async, fmt, is_audio):
        dropbot, calls, _ = link
        settings.put("urls.auto_format", fmt)

        self._send(dropbot, make_event, run_async)

        assert len(calls) == 1
        assert ("--extract-audio" in calls[0].cmd) is is_audio
        assert calls[0].final == dropbot.DOWNLOAD_PATHS["url_audio" if is_audio else "url_video"]
        assert "42_301" not in dropbot.pending_urls

    def test_una_playlist_pregunta_entera_o_el_primero(self, link, make_event, run_async, sent_messages):
        dropbot, calls, seen = link
        seen.playlist = (True, 12, "Mi lista")

        self._send(dropbot, make_event, run_async)

        assert not calls
        assert dropbot.pending_urls["42_301"]["playlist_count"] == 12
        assert "Mi lista" in sent_messages[-1][1] and "12" in sent_messages[-1][1]
        assert _all_buttons(sent_messages) == [
            b"cancel:42_301", b"playlist_full:42_301", b"playlist_first:42_301", b"simplecancel:42_301"
        ]

    def test_playlist_con_modo_fijo_pero_formato_a_preguntar(self, link, make_event, run_async, sent_messages):
        dropbot, calls, seen = link
        seen.playlist = (True, 12, "Mi lista")
        settings.put("urls.playlist", "FULL")

        self._send(dropbot, make_event, run_async)

        assert not calls
        assert _all_buttons(sent_messages) == [
            b"cancel:42_301", b"playlistfmt_full_audio:42_301", b"playlistfmt_full_video:42_301", b"simplecancel:42_301"
        ]
        assert "42_301" in dropbot.pending_urls

    def test_playlist_con_modo_y_formato_fijos_descarga_con_el_tope(self, link, make_event, run_async):
        dropbot, calls, seen = link
        seen.playlist = (True, 120, "Mi lista")
        settings.put("urls.playlist", "FULL")
        settings.put("urls.auto_format", "AUDIO")
        settings.put("urls.playlist_limit", 25)

        self._send(dropbot, make_event, run_async)

        (call,) = calls
        assert call.is_full_playlist and call.total_videos == 25
        assert call.cmd[call.cmd.index("--sleep-interval") + 1] == "2"
        assert "42_301" not in dropbot.pending_urls

    def test_sin_mensaje_de_analisis_no_manda_otro_y_pregunta_con_uno_nuevo(
        self, link, make_event, run_async, sent_messages, monkeypatch
    ):
        """Un segundo «Analizando…» mandado sin esperar a la cola atascada
        salía después, sin referencia, y se quedaba para siempre en el chat."""
        dropbot, _, _ = link

        async def reply_timeout(event, text=None, **kwargs):
            sent_messages.append(("reply", str(text), kwargs))
            return None

        monkeypatch.setattr(dropbot, "safe_reply", reply_timeout)

        self._send(dropbot, make_event, run_async)

        assert [kind for kind, _, _ in sent_messages] == ["reply", "reply"]
        assert "Analizando" in sent_messages[0][1] and "Analizando" not in sent_messages[1][1]
        assert _all_buttons(sent_messages) == [
            b"cancel:42_301", b"url_audio:42_301", b"url_video:42_301", b"simplecancel:42_301"
        ]

    def test_sin_mensaje_de_analisis_un_enlace_directo_baja_sin_progreso(
        self, launched, make_event, run_async, sent_messages, monkeypatch
    ):
        """Ni un «Descargando…» mandado sin esperar, que nadie borraría."""
        dropbot, _ = launched
        runs = []

        async def reply_timeout(event, text=None, **kwargs):
            sent_messages.append(("reply", str(text), kwargs))
            return None

        async def direct_run(event, url, filename, status, folder, icon, kind):
            runs.append((filename, status))

        monkeypatch.setattr(dropbot, "safe_reply", reply_timeout)
        monkeypatch.setattr(dropbot, "run_direct_download", direct_run)

        self._send(dropbot, make_event, run_async, "https://cdn.example.com/peli.mkv", event_id=302)

        assert runs == [("peli.mkv", None)]
        assert [kind for kind, _, _ in sent_messages] == ["reply"]
        assert "Analizando" in sent_messages[0][1]

    def test_quien_no_es_admin_no_consigue_nada(self, link, make_event, run_async, sent_messages):
        dropbot, calls, seen = link
        event = make_event(event_id=303)
        event.raw_text = "https://x/v"
        event.sender_id = 1234

        async def get_sender():
            return SimpleNamespace(username="intruso")

        event.get_sender = get_sender

        run_async(dropbot.handle_url_link(event))

        assert sent_messages == [] and not seen.direct_urls
        assert "42_303" not in dropbot.pending_urls

    def test_el_texto_tras_el_enlace_no_forma_parte_de_la_url(self, link, make_event, run_async):
        dropbot, _, seen = link

        self._send(dropbot, make_event, run_async, "https://youtu.be/abc123\nMira este vídeo")

        assert seen.direct_urls == ["https://youtu.be/abc123"]

    def test_el_mismo_id_en_otro_chat_no_pisa_el_enlace(self, link, make_event, run_async):
        """Cada supergrupo numera sus mensajes por su cuenta: el enlace de un
        grupo pisaba al del mismo id en privado, y su botón bajaba el del grupo."""
        dropbot, calls, _ = link

        self._send(dropbot, make_event, run_async, "https://youtu.be/privado", event_id=1200, chat_id=42)
        self._send(dropbot, make_event, run_async, "https://youtu.be/grupo", event_id=1200, chat_id=-1001234567890)
        _run_and_drain(run_async, lambda: dropbot.handle_format_selection(
            _click(make_event, None, (b"video", b"42_1200"))))

        (call,) = calls
        assert call.cmd[-1] == "https://youtu.be/privado"
        assert dropbot.pending_urls["-1001234567890_1200"]["url"] == "https://youtu.be/grupo"

    def test_el_cancelar_del_analisis_no_para_el_del_mismo_id_en_otro_chat(
        self, link, make_event, run_async, sent_messages, monkeypatch
    ):
        """En active_tasks pasaba lo mismo: el análisis del grupo pisaba al del
        mismo id en privado, y el Cancelar de este paraba el del grupo."""
        dropbot, _, _ = link

        async def analyzing(event, url, url_id, analyzing_msg):
            await asyncio.Event().wait()

        monkeypatch.setattr(dropbot, "analyze_url", analyzing)
        private = make_event(event_id=1201, chat_id=42)
        group = make_event(event_id=1201, chat_id=-1001234567890)
        private.raw_text = group.raw_text = "https://youtu.be/abc"

        async def scenario():
            handlers = [asyncio.create_task(dropbot.handle_url_link(event)) for event in (private, group)]
            while len(sent_messages) < 2:
                await asyncio.sleep(0)
            cancel = _all_buttons(sent_messages[:1])[0]
            await dropbot.cancel_download(make_event(event_id=9201, data=cancel))
            await asyncio.wait(handlers, timeout=0.5)
            states = [handler.cancelled() for handler in handlers]
            for handler in handlers:
                handler.cancel()
            await asyncio.wait(handlers)
            return cancel, states

        cancel, states = run_async(scenario())

        assert cancel == b"cancel:42_1201"
        assert states == [True, False], "solo se para el análisis del privado"

    def test_el_cancelar_de_una_descarga_lleva_el_chat_y_cabe_en_64_bytes(
        self, launched, make_event, run_async, sent_messages
    ):
        """Lanzada con un botón, la clave lleva el id de la consulta, que ocupa
        más que el de un mensaje: con el chat más largo sigue cabiendo."""
        dropbot, _ = launched
        chat_id, query_id = -(10**12 + 2**52), -(2**63)

        _run_and_drain(run_async, lambda: dropbot.start_ytdlp_download(
            make_event(event_id=query_id, chat_id=chat_id), MagicMock(), "https://x/v", False))

        (cancel,) = _all_buttons(sent_messages)
        assert cancel == f"cancel:{chat_id}_{query_id}".encode()
        assert len(cancel) <= 64

    def test_los_botones_del_enlace_llevan_la_clave_de_su_descarga(
        self, link, make_event, run_async, sent_messages, monkeypatch
    ):
        """La de task_key: con una copia aparte de la fórmula, un cambio en
        task_key dejaba los botones del enlace con otra clave que la del Cancelar."""
        dropbot, _, _ = link
        monkeypatch.setattr(dropbot, "task_key", lambda event: f"k{event.chat_id}_{event.id}")

        self._send(dropbot, make_event, run_async)

        assert _all_buttons(sent_messages) == [
            b"cancel:k42_301", b"url_audio:k42_301", b"url_video:k42_301", b"simplecancel:k42_301"
        ]
        assert "k42_301" in dropbot.pending_urls

    def test_los_botones_caben_en_64_bytes_con_cualquier_chat(self, link, make_event, run_async, sent_messages):
        dropbot, _, seen = link
        seen.playlist = (True, 12, "Mi lista")
        # Con el primero de la playlist fijo sale el botón más largo
        settings.put("urls.playlist", "FIRST")

        # Los ids más largos: un canal de 52 bits, con su prefijo, y un mensaje de 31
        self._send(dropbot, make_event, run_async, event_id=2**31 - 1, chat_id=-(10**12 + 2**52))

        data = _all_buttons(sent_messages)
        assert any(d.startswith(b"playlistfmt_first_video:") for d in data)
        assert all(len(d) <= 64 for d in data), data

    def test_el_titulo_de_una_playlist_no_se_interpreta(self, link, make_event, run_async, sent_messages):
        """Lo pone quien crea la playlist: un [texto](url) salía como un enlace
        oculto, y unos ** o __ sueltos descuadraban o tumbaban el mensaje."""
        dropbot, _, seen = link
        title = "Mis vídeos [Verifica tu cuenta aquí](https://evil.example/login) a__**b__"
        seen.playlist = (True, 12, title)

        self._send(dropbot, make_event, run_async)

        text, entities = markdown.parse(sent_messages[-1][1])
        assert title in text
        assert {type(entity) for entity in entities} == {types.MessageEntityBold, types.MessageEntityCode}

    def test_si_el_analisis_tarda_demasiado_se_dice_y_no_descarga(
        self, link, make_event, run_async, sent_messages, texts, monkeypatch
    ):
        dropbot, calls, _ = link

        async def too_slow(url):
            raise TimeoutError

        monkeypatch.setattr(dropbot, "detect_playlist", too_slow)

        self._send(dropbot, make_event, run_async)

        assert not calls
        assert "ha tardado demasiado" in texts()
        assert sent_messages[-1][2]["buttons"] is None, "sin el Cancelar del análisis"
        assert "42_301" not in dropbot.pending_urls
        assert "42_301" not in dropbot.active_tasks

    def test_el_cancelar_del_analisis_lo_para_sin_dejar_ytdlp_vivo(
        self, launched, hanging_ytdlp, make_event, run_async, sent_messages, texts, monkeypatch
    ):
        """«Analizando enlace...» no tenía botones: si el análisis no acababa no
        había nada que pulsar, y su yt-dlp seguía vivo hasta reiniciar."""
        dropbot, calls = launched

        async def not_direct(url):
            return False, None, None, None, None

        monkeypatch.setattr(dropbot, "is_direct_download_url", not_direct)
        event = make_event(event_id=310)
        event.raw_text = "https://evil.example/v"

        async def scenario():
            handler = asyncio.create_task(dropbot.handle_url_link(event))
            pids = await hanging_ytdlp()
            await dropbot.cancel_download(make_event(event_id=9310, data=b"cancel:42_310"))
            done, _ = await asyncio.wait([handler], timeout=10)
            if not done:
                handler.cancel()
            return done, handler, pids

        done, handler, pids = run_async(scenario())

        assert done, "el análisis no se ha parado"
        assert handler.cancelled()
        assert _all_buttons(sent_messages[:1]) == [b"cancel:42_310"]
        assert "Descarga cancelada" in texts()
        assert "42_310" not in dropbot.pending_urls
        assert "42_310" not in dropbot.active_tasks
        assert not calls
        assert not any(alive(pid) for pid in pids), "ni yt-dlp ni lo que lanzó siguen vivos"

    @pytest.mark.parametrize("fmt", ["ASK", "VIDEO"])
    def test_el_cancelar_durante_el_head_para_el_analisis(
        self, launched, make_event, run_async, texts, monkeypatch, fmt
    ):
        """Un enlace sin extensión espera al HEAD hasta HEAD_TIMEOUT, y su except
        desnudo se tragaba la cancelación: el análisis seguía con yt-dlp,
        preguntaba qué descargar o, con un formato fijo, descargaba lo que el
        admin acababa de cancelar."""
        dropbot, calls = launched
        settings.put("urls.auto_format", fmt)
        started, release = threading.Event(), threading.Event()
        detected = []

        def slow_head(session, url, **kwargs):
            started.set()
            release.wait(5)
            raise dropbot.requests.ConnectionError("sin respuesta")

        async def playlist(url):
            detected.append("playlist")
            return False, 1, None

        async def content(url):
            detected.append("content")
            return "video"

        monkeypatch.setattr(dropbot.requests.Session, "head", slow_head)
        monkeypatch.setattr(dropbot, "detect_playlist", playlist)
        monkeypatch.setattr(dropbot, "detect_content_type", content)
        event = make_event(event_id=311)
        event.raw_text = "https://lento.example/descarga"

        async def scenario():
            handler = asyncio.create_task(dropbot.handle_url_link(event))
            while not started.is_set():
                await asyncio.sleep(0.01)
            await dropbot.cancel_download(make_event(event_id=9311, data=b"cancel:42_311"))
            done, _ = await asyncio.wait([handler], timeout=5)
            release.set()
            if not done:
                handler.cancel()
            return handler

        handler = run_async(scenario())

        assert handler.cancelled()
        assert detected == [], "no sigue con yt-dlp"
        assert not calls, "no descarga lo cancelado"
        assert "Descarga cancelada" in texts()
        assert "42_311" not in dropbot.pending_urls
        assert "42_311" not in dropbot.active_tasks


class TestFicheroConEnlaceEnElPie:
    """Telethon ejecuta todos los handlers cuyo filtro casa, y en un fichero el
    patrón se compara con el pie: un vídeo con el pie "https://..." se bajaba
    dos veces, con la misma clave en active_tasks, y uno con "/start" (o
    "/settings") se borraba mientras se descargaba."""

    @staticmethod
    def _accepts(registered_handlers, name):
        (builder,) = [event for event, func in registered_handlers if func.__name__ == name]
        return builder.func

    @staticmethod
    def _message(media, **kinds):
        attrs = {"document": None, "video": None, "audio": None, "photo": None, **kinds}
        return SimpleNamespace(media=media, **attrs)

    @pytest.mark.parametrize("handler", ["handle_url_link", "handle_start", "handle_settings_command"])
    def test_un_fichero_no_pasa_por_los_handlers_de_texto(self, registered_handlers, handler):
        video = self._message(MagicMock(spec=types.MessageMediaDocument), document=object(), video=object())

        assert not self._accepts(registered_handlers, handler)(video)

    @pytest.mark.parametrize("handler", ["handle_url_link", "handle_start", "handle_settings_command"])
    def test_el_texto_sigue_entrando(self, registered_handlers, handler):
        text = self._message(None)
        preview = self._message(MagicMock(spec=types.MessageMediaWebPage), photo=object())

        accepts = self._accepts(registered_handlers, handler)
        assert accepts(text) and accepts(preview), "un enlace con vista previa no es un fichero"


def test_las_dos_traducciones_ponen_el_titulo_de_la_playlist_como_codigo():
    """Así get_text lo rellena con md_code y Telethon no interpreta nada dentro."""
    for language in ("es", "en"):
        assert "`$1`" in load_locale(language)["playlist_detected"]


# --- botones: formato, playlist, cancelar --------------------------------------

def _click(make_event, data, groups, event_id=401):
    return make_event(event_id=event_id, data=data, groups=groups)


class TestBotonFormato:
    def _pending(self, dropbot, url_id="301", count=1):
        dropbot.pending_urls[url_id] = {"url": "https://x/v", "playlist_count": count}

    @pytest.mark.parametrize("fmt,is_audio,folder", [(b"audio", True, "url_audio"), (b"video", False, "url_video")])
    def test_lanza_la_descarga_elegida(self, launched, make_event, run_async, sent_messages, fmt, is_audio, folder):
        dropbot, calls = launched
        self._pending(dropbot)

        _run_and_drain(run_async, lambda: dropbot.handle_format_selection(_click(make_event, None, (fmt, b"301"))))

        (call,) = calls
        assert ("--extract-audio" in call.cmd) is is_audio
        assert call.final == dropbot.DOWNLOAD_PATHS[folder]
        assert call.cmd[-1] == "https://x/v"
        assert not call.is_full_playlist and call.total_videos == 1
        assert sent_messages[0][0] == "edit" and "Descargando" in sent_messages[0][1]
        assert _all_buttons(sent_messages) == [b"cancel:42_401"]

    def test_doble_clic_descarga_una_sola_vez(self, launched, make_event, run_async):
        dropbot, calls = launched
        self._pending(dropbot)
        click = _click(make_event, None, (b"video", b"301"))

        async def twice():
            await asyncio.gather(dropbot.handle_format_selection(click), dropbot.handle_format_selection(click))

        _run_and_drain(run_async, twice)

        assert len(calls) == 1

    def test_el_segundo_clic_no_tapa_la_descarga_en_marcha(self, launched, make_event, run_async, texts):
        dropbot, calls = launched
        self._pending(dropbot)
        click = _click(make_event, None, (b"video", b"301"))

        async def twice():
            await dropbot.handle_format_selection(click)
            await dropbot.handle_format_selection(click)

        _run_and_drain(run_async, twice)

        assert "expirado" not in texts()

    def test_una_solicitud_caducada_se_dice(self, launched, make_event, run_async, sent_messages):
        dropbot, calls = launched

        run_async(dropbot.handle_format_selection(_click(make_event, None, (b"audio", b"999"))))

        assert not calls
        ((kind, text, kwargs),) = sent_messages
        assert kind == "edit" and "expirado" in text and kwargs["buttons"] is None

    def test_tras_cancelar_el_boton_ya_no_descarga(self, launched, make_event, run_async, sent_messages):
        dropbot, calls = launched
        self._pending(dropbot)

        run_async(dropbot.cancel_simple(_click(make_event, None, (b"301",))))
        run_async(dropbot.handle_format_selection(_click(make_event, None, (b"video", b"301"))))

        assert not calls
        assert "301" not in dropbot.pending_urls
        assert sent_messages[0][0] == "delete"

    def test_cancelar_tras_elegir_no_borra_el_mensaje_y_avisa(
        self, launched, make_event, run_async, sent_messages, monkeypatch
    ):
        """El Cancelar de la pregunta, pulsado mientras la edición con el progreso
        espera en la cola, borraba el mensaje y la descarga seguía sin nada a la
        vista para pararla."""
        dropbot, calls = launched
        self._pending(dropbot)
        answers = []

        async def answer(event, *args, **kwargs):
            answers.append((args, kwargs))

        monkeypatch.setattr(dropbot, "safe_answer", answer)

        _run_and_drain(run_async, lambda: dropbot.handle_format_selection(_click(make_event, None, (b"video", b"301"))))
        run_async(dropbot.cancel_simple(_click(make_event, None, (b"301",))))

        assert len(calls) == 1
        assert "delete" not in [kind for kind, _, _ in sent_messages]
        (text,), kwargs = answers[-1]
        assert "ya ha empezado" in text and kwargs["alert"] is True

    def test_si_no_se_puede_editar_el_mensaje_baja_sin_progreso(
        self, launched, make_event, run_async, sent_messages, monkeypatch
    ):
        """Un «Descargando…» mandado sin esperar a la cola atascada salía
        después, sin referencia, y se quedaba sin progreso ni borrarse."""
        dropbot, calls = launched
        self._pending(dropbot)

        async def edit_timeout(message, text=None, **kwargs):
            sent_messages.append(("edit", str(text), kwargs))
            return None

        monkeypatch.setattr(dropbot, "safe_edit", edit_timeout)

        _run_and_drain(run_async, lambda: dropbot.handle_format_selection(_click(make_event, None, (b"video", b"301"))))

        assert [kind for kind, _, _ in sent_messages] == ["edit"]
        assert len(calls) == 1 and calls[0].status is None


class TestBotonesDePlaylist:
    def _pending(self, dropbot, count=12):
        dropbot.pending_urls["301"] = {"url": "https://x/list", "playlist_count": count}

    def test_elegir_entera_pregunta_el_formato(self, launched, make_event, run_async, sent_messages):
        dropbot, calls = launched
        self._pending(dropbot)

        run_async(dropbot.handle_playlist_selection(_click(make_event, None, (b"full", b"301"))))

        assert not calls
        assert "301" in dropbot.pending_urls, "el formato aún no se ha elegido"
        assert _all_buttons(sent_messages) == [
            b"playlistfmt_full_audio:301", b"playlistfmt_full_video:301", b"simplecancel:301"
        ]

    def test_elegir_el_primero_con_formato_fijo_descarga(self, launched, make_event, run_async):
        dropbot, calls = launched
        self._pending(dropbot)
        settings.put("urls.auto_format", "VIDEO")

        _run_and_drain(run_async, lambda: dropbot.handle_playlist_selection(_click(make_event, None, (b"first", b"301"))))

        (call,) = calls
        assert "--no-playlist" in call.cmd and call.total_videos == 1
        assert "301" not in dropbot.pending_urls

    def test_doble_clic_con_formato_fijo_descarga_una_vez(self, launched, make_event, run_async, sent_messages):
        dropbot, calls = launched
        self._pending(dropbot)
        settings.put("urls.auto_format", "AUDIO")
        click = _click(make_event, None, (b"full", b"301"))

        async def twice():
            await dropbot.handle_playlist_selection(click)
            await dropbot.handle_playlist_selection(click)

        _run_and_drain(run_async, twice)

        assert len(calls) == 1

    def test_el_segundo_clic_no_tapa_la_playlist_en_marcha(self, launched, make_event, run_async, texts):
        dropbot, calls = launched
        self._pending(dropbot)
        settings.put("urls.auto_format", "AUDIO")
        click = _click(make_event, None, (b"full", b"301"))

        async def twice():
            await dropbot.handle_playlist_selection(click)
            await dropbot.handle_playlist_selection(click)

        _run_and_drain(run_async, twice)

        assert "expirado" not in texts()

    def test_una_playlist_caducada_se_dice(self, launched, make_event, run_async, texts):
        dropbot, calls = launched

        run_async(dropbot.handle_playlist_selection(_click(make_event, None, (b"full", b"999"))))

        assert not calls and "expirado" in texts()

    def test_continue_playlist_sin_mensaje_caducada_calla(self, launched, make_event, run_async, sent_messages):
        dropbot, _ = launched

        run_async(dropbot.continue_playlist(make_event(), None, "999", "full"))

        assert sent_messages == []

    def test_continue_playlist_sin_mensaje_pregunta_con_uno_nuevo(self, launched, make_event, run_async, sent_messages):
        dropbot, _ = launched
        self._pending(dropbot)

        run_async(dropbot.continue_playlist(make_event(), None, "301", "first"))

        assert sent_messages[0][0] == "reply"
        assert b"playlistfmt_first_video:301" in _all_buttons(sent_messages)

    @pytest.mark.parametrize("mode,fmt", [(b"full", b"video"), (b"first", b"audio")])
    def test_el_formato_de_la_playlist_lanza_la_descarga(self, launched, make_event, run_async, mode, fmt):
        dropbot, calls = launched
        self._pending(dropbot, count=8)

        _run_and_drain(run_async, lambda: dropbot.handle_playlist_format_selection(
            _click(make_event, None, (mode, fmt, b"301"))))

        (call,) = calls
        assert ("--extract-audio" in call.cmd) is (fmt == b"audio")
        if mode == b"full":
            assert call.is_full_playlist and call.total_videos == 8
            assert "--ignore-errors" in call.cmd
            assert call.cmd[call.cmd.index("--sleep-interval") + 1] == "1"
        else:
            assert not call.is_full_playlist and call.total_videos == 1
            assert "--no-playlist" in call.cmd and "--sleep-interval" not in call.cmd

    def test_doble_clic_en_el_formato_descarga_una_vez(self, launched, make_event, run_async):
        dropbot, calls = launched
        self._pending(dropbot)
        click = _click(make_event, None, (b"full", b"video", b"301"))

        async def twice():
            await asyncio.gather(dropbot.handle_playlist_format_selection(click),
                                 dropbot.handle_playlist_format_selection(click))

        _run_and_drain(run_async, twice)

        assert len(calls) == 1

    def test_el_formato_de_una_playlist_caducada_se_dice(self, launched, make_event, run_async, texts):
        dropbot, calls = launched

        run_async(dropbot.handle_playlist_format_selection(_click(make_event, None, (b"full", b"video", b"999"))))

        assert not calls and "expirado" in texts()


class TestCancelarAntesDeElegir:
    def test_borra_la_pregunta_y_olvida_la_url(self, quiet_bot, make_event, run_async, sent_messages):
        quiet_bot.pending_urls["555"] = {"url": "https://x/v", "playlist_count": 1}

        run_async(quiet_bot.cancel_simple(make_event(groups=(b"555",))))

        assert "555" not in quiet_bot.pending_urls
        assert [kind for kind, _, _ in sent_messages] == ["delete"]

    def test_doble_clic_no_falla(self, quiet_bot, make_event, run_async, sent_messages):
        run_async(quiet_bot.cancel_simple(make_event(groups=(b"556",))))
        run_async(quiet_bot.cancel_simple(make_event(groups=(b"556",))))

        assert [kind for kind, _, _ in sent_messages] == ["delete", "delete"]


# --- start_ytdlp_download -----------------------------------------------------

class TestComandoDeDescarga:
    def _start(self, dropbot, make_event, run_async, **kwargs):
        args = dict(target=MagicMock(), url="https://x/v", is_audio=False)
        args.update(kwargs)
        event = make_event(event_id=601)
        _run_and_drain(run_async, lambda: dropbot.start_ytdlp_download(event, **args))

    def test_la_salida_va_a_temp_con_su_marca(self, launched, make_event, run_async):
        dropbot, calls = launched

        self._start(dropbot, make_event, run_async)

        (call,) = calls
        output = call.cmd[call.cmd.index("-o") + 1]
        assert output.startswith(dropbot.TEMP_DIR + os.sep)
        assert call.temp_marker and call.temp_marker in output
        assert call.cmd[0] == "yt-dlp" and call.cmd[-1] == "https://x/v"

    def test_la_tarea_queda_registrada_para_poder_cancelarla(self, launched, make_event, run_async):
        dropbot, _ = launched

        async def scenario():
            await dropbot.start_ytdlp_download(make_event(event_id=602), None, "https://x/v", False)
            return dropbot.active_tasks.get("42_602")

        task = run_async(scenario())

        assert isinstance(task, asyncio.Task)

    def test_en_un_foro_el_tema_del_boton_se_sabe_antes_de_borrar_su_mensaje(
        self, launched, forum_callback, run_async
    ):
        """El mensaje del botón pasa a ser el del progreso, que se borra al
        acabar, antes de enviar lo descargado al tema del que salió."""
        dropbot, _ = launched
        click = forum_callback()

        _run_and_drain(run_async, lambda: dropbot.start_ytdlp_download(click, click, "https://x/v", False))
        click._client.message = None  # el progreso ya se ha borrado

        assert run_async(dropbot.get_topic(click)) == FORUM_TOPIC

    def test_en_un_foro_el_cancelar_recien_salido_para_la_descarga(
        self, launched, forum_callback, run_async, sent_messages, monkeypatch
    ):
        """Pedir el tema del botón es una petición a Telegram, hasta un minuto
        con un FloodWait. Hecha con el Cancelar ya a la vista, ese Cancelar no
        encontraba la tarea: borraba el mensaje y yt-dlp arrancaba igual."""
        dropbot, _ = launched
        click = forum_callback()
        get_messages = click._client.get_messages

        async def slow_get_messages(chat, ids):
            await asyncio.sleep(0.2)
            return await get_messages(chat, ids)

        click._client.get_messages = slow_get_messages
        monkeypatch.setattr(dropbot, "run_url_download", lambda *args, **kwargs: asyncio.sleep(1))
        cancel = forum_callback(query_id=20_000)
        cancel.query.data = f"cancel:{dropbot.task_key(click)}".encode()

        async def scenario():
            start = asyncio.create_task(dropbot.start_ytdlp_download(click, click, "https://x/v", False))
            while not sent_messages:  # el «Descargando…» con su Cancelar
                await asyncio.sleep(0.01)
            await dropbot.cancel_download(cancel)
            await start
            download = dropbot.active_tasks[dropbot.task_key(click)]
            await asyncio.gather(download, return_exceptions=True)
            return download.cancelled()

        assert run_async(scenario()), "la descarga tenía que pararse"
        assert [kind for kind, _, _ in sent_messages] == ["edit"]

    def test_una_playlist_entera_sin_cuenta_usa_el_tope(self, launched, make_event, run_async):
        dropbot, calls = launched
        settings.put("urls.playlist_limit", 10)

        self._start(dropbot, make_event, run_async, playlist_count=0, playlist_mode="full")

        assert calls[0].total_videos == 10


@pytest.mark.parametrize("handler,data,groups", [
    ("cancel_download", b"cancel:42_301", ()),
    ("cancel_simple", None, (b"301",)),
    ("handle_playlist_selection", None, (b"full", b"301")),
    ("handle_playlist_format_selection", None, (b"full", b"video", b"301")),
    ("handle_format_selection", None, (b"video", b"301")),
])
def test_los_botones_de_enlaces_no_hacen_caso_a_quien_no_es_admin(
    launched, make_event, run_async, sent_messages, handler, data, groups
):
    dropbot, calls = launched
    dropbot.pending_urls["301"] = {"url": "https://x/v", "playlist_count": 3}
    settings.put("urls.auto_format", "VIDEO")
    event = make_event(data=data, groups=groups)
    event.sender_id = 1234

    async def get_sender():
        return SimpleNamespace(username="intruso")

    event.get_sender = get_sender

    _run_and_drain(run_async, lambda: getattr(dropbot, handler)(event))

    assert not calls and sent_messages == []
    assert "301" in dropbot.pending_urls


def test_doble_clic_simultaneo_en_playlist_con_formato_fijo(launched, make_event, run_async):
    dropbot, calls = launched
    dropbot.pending_urls["301"] = {"url": "https://x/list", "playlist_count": 3}
    settings.put("urls.auto_format", "VIDEO")
    click = make_event(groups=(b"full", b"301"))

    async def twice():
        await asyncio.gather(dropbot.handle_playlist_selection(click), dropbot.handle_playlist_selection(click))

    _run_and_drain(run_async, twice)

    assert len(calls) == 1
