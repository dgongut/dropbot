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
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import settings
from button_data import button_data


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
            sys.exit({code})
        '''))
        script.chmod(0o755)

    install.calls = lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return install


@pytest.fixture
def launched(quiet_bot, monkeypatch, config_dir):
    """`run_url_download` interceptado: anota cada descarga que se lanzaría."""
    calls = []

    def fake_run(event, cmd, status_message, final_output_dir, **kwargs):
        calls.append(SimpleNamespace(cmd=cmd, status=status_message, final=final_output_dir, **kwargs))
        return asyncio.sleep(0)

    monkeypatch.setattr(quiet_bot, "run_url_download", fake_run)
    quiet_bot.pending_urls.clear()
    quiet_bot.active_tasks.clear()
    yield quiet_bot, calls
    quiet_bot.pending_urls.clear()
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
        monkeypatch.setattr(dropbot.requests, "head", forbidden)

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

    def test_una_pagina_html_no_es_una_descarga_directa(self, dropbot, run_async):
        url = "https://www.lasexta.com/noticias/video-del-dia_2026100660000000.html"

        is_direct, *_ = run_async(dropbot.is_direct_download_url(url))

        assert not is_direct


class TestDescargaDirectaPorCabeceras:
    def _head(self, dropbot, monkeypatch, headers=None, raises=None):
        calls = []

        def fake_head(url, **kwargs):
            calls.append((url, kwargs))
            if raises:
                raise raises
            return SimpleNamespace(headers=headers or {})

        monkeypatch.setattr(dropbot.requests, "head", fake_head)
        return calls

    def test_una_pagina_normal_no_es_directa(self, dropbot, run_async, monkeypatch):
        calls = self._head(dropbot, monkeypatch, {"Content-Type": "text/html; charset=utf-8"})

        result = run_async(dropbot.is_direct_download_url("https://www.youtube.com/watch?v=abc"))

        assert result == (False, None, None, None, None)
        (url, kwargs), = calls
        assert kwargs["allow_redirects"] is True and kwargs["timeout"] == 5

    def test_si_el_servidor_no_responde_no_es_directa(self, dropbot, run_async, monkeypatch):
        import requests

        self._head(dropbot, monkeypatch, raises=requests.ConnectionError("sin red"))

        assert run_async(dropbot.is_direct_download_url("https://x.example/watch"))[0] is False

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
        assert args[:2] == ["--cookies", str(cookies)]


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

    def test_con_fichero_va_justo_tras_el_ejecutable(self, dropbot, tmp_path, monkeypatch):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))
        cmd = ["yt-dlp", "-f", "best", "URL"]

        assert dropbot.add_ytdlp_cookies(cmd) == ["yt-dlp", "--cookies", str(cookies), "-f", "best", "URL"]
        assert cmd == ["yt-dlp", "-f", "best", "URL"], "no modifica la lista original"

    def test_una_carpeta_con_ese_nombre_no_cuenta(self, dropbot, tmp_path, monkeypatch):
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(tmp_path))

        assert dropbot.add_ytdlp_cookies(["yt-dlp", "URL"]) == ["yt-dlp", "URL"]

    def test_la_descarga_tambien_las_usa(self, launched, make_event, run_async, tmp_path, monkeypatch):
        dropbot, calls = launched
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("")
        monkeypatch.setattr(dropbot, "YTDLP_COOKIES_FILE", str(cookies))

        _run_and_drain(run_async, lambda: dropbot.start_ytdlp_download(make_event(), None, "https://x/v", False))

        assert calls[0].cmd[:3] == ["yt-dlp", "--cookies", str(cookies)]


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

    def _send(self, dropbot, make_event, run_async, text="https://www.youtube.com/watch?v=abc", event_id=301):
        event = make_event(event_id=event_id)
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
        assert "302" not in dropbot.pending_urls
        assert not calls, "no pasa por yt-dlp"
        kinds = [kind for kind, _, _ in sent_messages]
        assert kinds == ["reply", "edit"]
        assert "Analizando" in sent_messages[0][1] and "Descargando" in sent_messages[1][1]
        assert _all_buttons(sent_messages) == [b"cancel:302"]

    def test_un_video_pregunta_audio_o_video(self, link, make_event, run_async, sent_messages):
        dropbot, calls, seen = link

        self._send(dropbot, make_event, run_async)

        assert not calls
        assert dropbot.pending_urls["301"] == {"url": "https://www.youtube.com/watch?v=abc", "playlist_count": 1}
        assert sent_messages[-1][0] == "edit" and "¿Qué deseas descargar?" in sent_messages[-1][1]
        assert _all_buttons(sent_messages) == [b"url_audio:301", b"url_video:301", b"simplecancel:301"]

    @pytest.mark.parametrize("content,question,buttons", [
        ("image", "imagen", [b"url_video:301", b"simplecancel:301"]),
        ("audio", "audio", [b"url_audio:301", b"simplecancel:301"]),
        ("unknown", "contenido", [b"url_video:301", b"simplecancel:301"]),
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
        assert "301" not in dropbot.pending_urls

    def test_una_playlist_pregunta_entera_o_el_primero(self, link, make_event, run_async, sent_messages):
        dropbot, calls, seen = link
        seen.playlist = (True, 12, "Mi lista")

        self._send(dropbot, make_event, run_async)

        assert not calls
        assert dropbot.pending_urls["301"]["playlist_count"] == 12
        assert "Mi lista" in sent_messages[-1][1] and "12" in sent_messages[-1][1]
        assert _all_buttons(sent_messages) == [b"playlist_full:301", b"playlist_first:301", b"simplecancel:301"]

    def test_playlist_con_modo_fijo_pero_formato_a_preguntar(self, link, make_event, run_async, sent_messages):
        dropbot, calls, seen = link
        seen.playlist = (True, 12, "Mi lista")
        settings.put("urls.playlist", "FULL")

        self._send(dropbot, make_event, run_async)

        assert not calls
        assert _all_buttons(sent_messages) == [
            b"playlistfmt_full_audio:301", b"playlistfmt_full_video:301", b"simplecancel:301"
        ]
        assert "301" in dropbot.pending_urls

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
        assert "301" not in dropbot.pending_urls

    def test_sin_mensaje_de_analisis_pregunta_con_uno_nuevo(
        self, link, make_event, run_async, sent_messages, monkeypatch
    ):
        dropbot, _, _ = link

        async def reply_timeout(event, text=None, **kwargs):
            sent_messages.append(("reply", str(text), kwargs))
            return None

        monkeypatch.setattr(dropbot, "safe_reply", reply_timeout)

        self._send(dropbot, make_event, run_async)

        assert [kind for kind, _, _ in sent_messages] == ["reply", "reply", "reply"]
        assert sent_messages[1][2]["wait_for_result"] is False
        assert _all_buttons(sent_messages) == [b"url_audio:301", b"url_video:301", b"simplecancel:301"]

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
        assert "303" not in dropbot.pending_urls

    def test_el_texto_tras_el_enlace_no_forma_parte_de_la_url(self, link, make_event, run_async):
        dropbot, _, seen = link

        self._send(dropbot, make_event, run_async, "https://youtu.be/abc123\nMira este vídeo")

        assert seen.direct_urls == ["https://youtu.be/abc123"]


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
        assert _all_buttons(sent_messages) == [b"cancel:401"]

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

    def test_si_no_se_puede_editar_el_mensaje_va_uno_nuevo(
        self, launched, make_event, run_async, sent_messages, monkeypatch
    ):
        dropbot, calls = launched
        self._pending(dropbot)

        async def edit_timeout(message, text=None, **kwargs):
            sent_messages.append(("edit", str(text), kwargs))
            return None

        monkeypatch.setattr(dropbot, "safe_edit", edit_timeout)

        _run_and_drain(run_async, lambda: dropbot.handle_format_selection(_click(make_event, None, (b"video", b"301"))))

        assert [kind for kind, _, _ in sent_messages] == ["edit", "reply"]
        assert calls[0].status is not None


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
            return dropbot.active_tasks.get(602)

        task = run_async(scenario())

        assert isinstance(task, asyncio.Task)

    def test_una_playlist_entera_sin_cuenta_usa_el_tope(self, launched, make_event, run_async):
        dropbot, calls = launched
        settings.put("urls.playlist_limit", 10)

        self._start(dropbot, make_event, run_async, playlist_count=0, playlist_mode="full")

        assert calls[0].total_videos == 10


@pytest.mark.parametrize("handler,data,groups", [
    ("cancel_download", b"cancel:301", ()),
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
