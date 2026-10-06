"""Descargas de enlaces con yt-dlp, de punta a punta y sin red.

Se pone delante en el PATH un `yt-dlp` falso que imprime las líneas que
imprime el de verdad, crea los ficheros en la ruta de `-o` y sale con el código
que pida el test. Así se prueba el comando que construye el bot, la lectura de
la salida, qué acaba en la carpeta de destino, qué queda en TEMP_DIR y qué se le
dice al usuario.
"""

import asyncio
import json
import os
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import settings
from button_data import button_data

URL = "https://www.youtube.com/watch?v=abc"

# Lo común de todos los yt-dlp falsos: registra los argumentos y da utilidades
# para escribir la ruta de -o igual que la resolvería yt-dlp
_FAKE_HEADER = '''
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_YTDLP_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
template = args[args.index("-o") + 1] if "-o" in args else ""

def path(title, ext, index=None):
    resolved = template.replace("%(playlist_index&{}-|)s", f"{index}-" if index else "")
    return resolved.replace("%(title).200s", title).replace("%(ext)s", ext)

def touch(p, data=b"x"):
    with open(p, "wb") as handle:
        handle.write(data)

def out(line):
    print(line, flush=True)

def err(line):
    print(line, file=sys.stderr, flush=True)

def ready():
    touch(os.environ["FAKE_YTDLP_READY"])
'''


@pytest.fixture
def fake_ytdlp(tmp_path, monkeypatch):
    """Instala un `yt-dlp` falso con el cuerpo dado; `.calls()` da sus argumentos."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "ytdlp-calls.jsonl"
    ready = tmp_path / "ytdlp-ready"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_YTDLP_LOG", str(log))
    monkeypatch.setenv("FAKE_YTDLP_READY", str(ready))

    def install(body):
        script = bindir / "yt-dlp"
        script.write_text(f"#!{sys.executable}\n{_FAKE_HEADER}\n{textwrap.dedent(body)}")
        script.chmod(0o755)
        return str(script)

    install.calls = lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    install.ready = ready
    return install


@pytest.fixture
def links(quiet_bot, tmp_path, monkeypatch, config_dir):
    """Carpetas propias, análisis del fichero y envío a Telegram simulados."""
    bot = quiet_bot
    temp = tmp_path / "temp"
    video = tmp_path / "url_video"
    audio = tmp_path / "url_audio"
    for folder in (temp, video, audio):
        folder.mkdir()
    monkeypatch.setattr(bot, "TEMP_DIR", str(temp))
    monkeypatch.setitem(bot.DOWNLOAD_PATHS, "url_video", str(video))
    monkeypatch.setitem(bot.DOWNLOAD_PATHS, "url_audio", str(audio))

    async def file_info(path):
        kind = "audio" if path.endswith((".mp3", ".m4a")) else "video"
        return {
            "type": kind, "size_formatted": "1 KB", "duration_formatted": None,
            "resolution": None, "codec_video": None, "codec_audio": None, "bitrate": None,
        }

    uploaded = []

    async def send(event, file_path, sending_msg=None, delete_after=False):
        uploaded.append((os.path.basename(file_path), delete_after))
        return MagicMock(id=1)

    monkeypatch.setattr(bot, "get_file_info", file_info)
    monkeypatch.setattr(bot, "send_file_to_telegram", send)
    bot.active_tasks.clear()
    bot.pending_urls.clear()
    bot.pending_files.clear()
    yield SimpleNamespace(bot=bot, temp=temp, video=video, audio=audio, uploaded=uploaded)
    bot.active_tasks.clear()


def _download(links, run_async, event, is_audio=False, playlist_count=1, mode=None, target=None):
    """Lanza la descarga como lo hace el bot y espera a que termine."""
    async def scenario():
        await links.bot.start_ytdlp_download(event, target, URL, is_audio, playlist_count, mode)
        await links.bot.active_tasks[event.id]
    run_async(scenario())


def _ls(folder):
    return sorted(os.listdir(folder))


def _buttons(kwargs):
    rows = kwargs.get("buttons") or []
    flat = []
    for row in rows if isinstance(rows, (list, tuple)) else [rows]:
        flat.extend(row if isinstance(row, (list, tuple)) else [row])
    return [button_data(button) for button in flat]


VIDEO_MERGE = '''
v = path("Mi_video", "f137.mp4")
a = path("Mi_video", "f140.m4a")
final = path("Mi_video", "mp4")
out("[youtube] Extracting URL: https://www.youtube.com/watch?v=abc")
out("[info] abc: Downloading 1 format(s): 137+140")
out(f"[download] Destination: {v}")
touch(v)
out("[download]  50.0% of   10.00MiB at    1.00MiB/s ETA 00:05")
out("[download] 100% of   10.00MiB in 00:00:01 at 10.00MiB/s")
out(f"[download] Destination: {a}")
touch(a)
out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
out(f'[Merger] Merging formats into "{final}"')
touch(final, b"video final")
os.remove(v)
os.remove(a)
out(f"Deleting original file {v} (pass -k to keep)")
out(f"Deleting original file {a} (pass -k to keep)")
'''


class TestUnVideo:
    def test_el_video_mezclado_acaba_en_su_carpeta_con_nombre_limpio(
        self, links, fake_ytdlp, make_event, run_async, sent_messages, texts
    ):
        fake_ytdlp(VIDEO_MERGE)

        _download(links, run_async, make_event(event_id=501))

        assert _ls(links.video) == ["Mi_video.mp4"]
        assert (links.video / "Mi_video.mp4").read_bytes() == b"video final"
        assert _ls(links.temp) == [], "no debe quedar nada en TEMP_DIR"
        assert "Descarga completada" in texts() and "Mi_video.mp4" in texts()
        assert any(kind == "delete" for kind, _, _ in sent_messages), "el progreso se borra"
        assert 501 not in links.bot.active_tasks

    def test_ofrece_los_botones_de_envio(self, links, fake_ytdlp, make_event, run_async, sent_messages):
        fake_ytdlp(VIDEO_MERGE)

        _download(links, run_async, make_event(event_id=502))

        data = [d for _, _, kwargs in sent_messages for d in _buttons(kwargs)]
        assert any(d.startswith(b"send:") for d in data)
        assert list(links.bot.pending_files.values()) == [str(links.video / "Mi_video.mp4")]

    def test_el_comando_lleva_los_ajustes_y_descarga_en_temp(self, links, fake_ytdlp, make_event, run_async):
        fake_ytdlp(VIDEO_MERGE)
        settings.put("urls.video_quality", "720")
        settings.put("urls.max_size_mb", 512)

        _download(links, run_async, make_event(event_id=503))

        (args,) = fake_ytdlp.calls()
        assert args[args.index("-f") + 1] == "bv*+ba/best"
        assert args[args.index("-S") + 1] == "vcodec:h264,res:720,acodec:aac"
        assert args[args.index("--max-filesize") + 1] == "512M"
        assert "--restrict-filenames" in args and "--newline" in args
        output = args[args.index("-o") + 1]
        assert os.path.dirname(output) == str(links.temp)
        assert URL in args
        # Un vídeo suelto ni duerme entre descargas ni toca opciones de playlist
        for flag in ("--sleep-interval", "--no-playlist", "--ignore-errors", "--playlist-end"):
            assert flag not in args

    def test_un_audio_mp3_se_queda_solo_con_el_mp3(self, links, fake_ytdlp, make_event, run_async, texts):
        """yt-dlp baja el .webm, lo pasa a .mp3 y borra el .webm."""
        fake_ytdlp('''
            src = path("Cancion", "webm")
            mp3 = path("Cancion", "mp3")
            out(f"[download] Destination: {src}")
            touch(src)
            out("[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s")
            out(f"[ExtractAudio] Destination: {mp3}")
            touch(mp3, b"mp3")
            os.remove(src)
            out(f"Deleting original file {src} (pass -k to keep)")
        ''')

        _download(links, run_async, make_event(event_id=504), is_audio=True)

        (args,) = fake_ytdlp.calls()
        assert "--extract-audio" in args and args[args.index("--audio-format") + 1] == "mp3"
        assert _ls(links.audio) == ["Cancion.mp3"]
        assert _ls(links.video) == []
        assert _ls(links.temp) == []
        assert texts().count("Descarga completada") == 1

    def test_un_m4a_que_no_hace_falta_convertir(self, links, fake_ytdlp, make_event, run_async):
        settings.put("urls.audio_format", "M4A")
        fake_ytdlp('''
            m4a = path("Podcast", "m4a")
            out(f"[download] Destination: {m4a}")
            touch(m4a)
            out("[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s")
            out(f"[ExtractAudio] Not converting audio {m4a}; file is already in target format m4a")
        ''')

        _download(links, run_async, make_event(event_id=505), is_audio=True)

        assert _ls(links.audio) == ["Podcast.m4a"]

    def test_no_machaca_un_video_con_el_mismo_nombre(self, links, fake_ytdlp, make_event, run_async):
        (links.video / "Mi_video.mp4").write_bytes(b"el de antes")
        fake_ytdlp(VIDEO_MERGE)

        _download(links, run_async, make_event(event_id=506))

        assert _ls(links.video) == ["Mi_video (1).mp4", "Mi_video.mp4"]
        assert (links.video / "Mi_video.mp4").read_bytes() == b"el de antes"

    def test_los_restos_que_deja_ytdlp_se_borran(self, links, fake_ytdlp, make_event, run_async):
        fake_ytdlp('''
            final = path("Clip", "mp4")
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
            touch(path("Clip", "f251.webm.part"))
            touch(path("Clip", "mp4.ytdl"))
        ''')

        _download(links, run_async, make_event(event_id=507))

        assert _ls(links.video) == ["Clip.mp4"]
        assert _ls(links.temp) == []

    def test_no_toca_los_temporales_de_otra_descarga(self, links, fake_ytdlp, make_event, run_async):
        ajeno = links.temp / "Otro_temp1.mp4.part"
        ajeno.write_bytes(b"de otra descarga")
        fake_ytdlp(VIDEO_MERGE)

        _download(links, run_async, make_event(event_id=508))

        assert ajeno.exists()

    def test_si_no_se_puede_editar_el_mensaje_se_envia_uno_nuevo(
        self, links, fake_ytdlp, make_event, run_async, sent_messages, monkeypatch
    ):
        fake_ytdlp(VIDEO_MERGE)

        async def edit_timeout(message, text=None, **kwargs):
            sent_messages.append(("edit", str(text), kwargs))
            return None

        monkeypatch.setattr(links.bot, "safe_edit", edit_timeout)

        _download(links, run_async, make_event(event_id=509), target=MagicMock())

        kinds = [(kind, "Descargando" in text) for kind, text, _ in sent_messages[:2]]
        assert kinds == [("edit", True), ("reply", True)]
        assert _buttons(sent_messages[1][2]) == [b"cancel:509"]


class TestFallos:
    def test_sin_ficheros_y_codigo_de_error_se_dice(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp('''
            out("[generic] Extracting URL: https://www.youtube.com/watch?v=abc")
            err("ERROR: Unsupported URL: https://www.youtube.com/watch?v=abc")
            sys.exit(1)
        ''')

        _download(links, run_async, make_event(event_id=511))

        assert "La descarga desde la URL ha fallado" in texts()
        assert _ls(links.video) == [] and _ls(links.temp) == []
        assert 511 not in links.bot.active_tasks

    def test_codigo_cero_pero_sin_ficheros_tambien_es_un_fallo(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp('out("[youtube] abc: Downloading webpage")')

        _download(links, run_async, make_event(event_id=512))

        assert "La descarga desde la URL ha fallado" in texts()

    def test_pasarse_del_tamano_maximo_se_explica(self, links, fake_ytdlp, make_event, run_async, texts):
        """yt-dlp sale con 0 y no deja nada: el usuario debe saber por qué."""
        settings.put("urls.max_size_mb", 512)
        fake_ytdlp('''
            out("[info] abc: Downloading 1 format(s): 137+140")
            out("[download] File is larger than max-filesize (912345678 bytes > 536870912 bytes). Aborting.")
        ''')

        _download(links, run_async, make_event(event_id=513))

        assert "512 MB" in texts()
        assert "ha fallado" not in texts()

    def test_un_error_con_fichero_descargado_es_un_exito(self, links, fake_ytdlp, make_event, run_async, texts):
        """Un aviso de postproceso hace salir con 1 aunque el vídeo esté bajado."""
        fake_ytdlp('''
            final = path("Clip", "mp4")
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
            err("ERROR: Postprocessing: Conversion failed!")
            sys.exit(1)
        ''')

        _download(links, run_async, make_event(event_id=514))

        assert _ls(links.video) == ["Clip.mp4"]
        assert "Descarga completada" in texts()
        assert "ha fallado" not in texts()

    def test_si_ytdlp_no_existe_se_avisa_y_se_libera_la_tarea(
        self, links, make_event, run_async, sent_messages, texts, tmp_path
    ):
        event = make_event(event_id=515)
        status = MagicMock(name="progreso")

        run_async(links.bot.run_url_download(
            event, [str(tmp_path / "no-existe")], status, str(links.video), temp_marker="_temp515"
        ))

        assert "La descarga desde la URL ha fallado" in texts()
        assert sent_messages[0][0] == "delete", "el mensaje de progreso se quita"
        assert 515 not in links.bot.active_tasks

    def test_un_fichero_anunciado_que_no_existe_no_rompe_nada(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp('''
            out(f"[download] Destination: {path('Fantasma', 'mp4')}")
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
        ''')

        _download(links, run_async, make_event(event_id=516))

        assert _ls(links.video) == []
        assert 516 not in links.bot.active_tasks


PLAYLIST_3 = '''
for index, title in ((1, "Uno"), (2, "Dos"), (3, "Tres")):
    out(f"[download] Downloading item {index} of 3")
    final = path(title, "mp4", index)
    out(f"[download] Destination: {final}")
    touch(final, title.encode())
    out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
out("[download] Finished downloading playlist: Lista")
'''


class TestPlaylists:
    def test_la_playlist_entera_se_guarda_sin_preguntar(
        self, links, fake_ytdlp, make_event, run_async, sent_messages, texts
    ):
        fake_ytdlp(PLAYLIST_3)

        _download(links, run_async, make_event(event_id=521), playlist_count=3, mode="full")

        assert _ls(links.video) == ["1-Uno.mp4", "2-Dos.mp4", "3-Tres.mp4"]
        assert _ls(links.temp) == []
        assert "Playlist almacenada" in texts() and "**3 archivos**" in texts()
        data = [d for _, _, kwargs in sent_messages for d in _buttons(kwargs)]
        assert data == [b"cancel:521"], "solo el botón de cancelar del progreso"
        assert not links.uploaded

    def test_el_comando_de_playlist_entera(self, links, fake_ytdlp, make_event, run_async):
        fake_ytdlp(PLAYLIST_3)
        settings.put("urls.playlist_limit", 2)

        _download(links, run_async, make_event(event_id=522), playlist_count=3, mode="full")

        (args,) = fake_ytdlp.calls()
        assert "--ignore-errors" in args
        assert args[args.index("--playlist-end") + 1] == "2"
        # 2 vídeos: 1 segundo entre uno y otro
        assert args[args.index("--sleep-interval") + 1] == "1"

    def test_solo_el_primero_ofrece_botones(self, links, fake_ytdlp, make_event, run_async, sent_messages):
        fake_ytdlp('''
            final = path("Uno", "mp4")
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
        ''')

        _download(links, run_async, make_event(event_id=523), playlist_count=30, mode="first")

        (args,) = fake_ytdlp.calls()
        assert "--no-playlist" in args and "--sleep-interval" not in args
        assert _ls(links.video) == ["Uno.mp4"]
        assert any(d.startswith(b"send:") for _, _, kw in sent_messages for d in _buttons(kw))

    def test_una_playlist_con_videos_no_disponibles_cuenta_lo_que_hay(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp('''
            for index, title in ((1, "Uno"), (3, "Tres")):
                final = path(title, "mp4", index)
                out(f"[download] Destination: {final}")
                touch(final)
                out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
            err("ERROR: [youtube] xyz: Video unavailable. This video is private")
            sys.exit(1)
        ''')

        _download(links, run_async, make_event(event_id=524), playlist_count=3, mode="full")

        assert _ls(links.video) == ["1-Uno.mp4", "3-Tres.mp4"]
        assert "parcial" in texts() and "**2/3 archivos**" in texts()
        assert "ha fallado" not in texts()

    def test_una_playlist_de_la_que_solo_queda_un_video_avisa_de_los_que_faltan(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp('''
            final = path("Uno", "mp4", 1)
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
            err("ERROR: [youtube] b: Video unavailable")
            err("ERROR: [youtube] c: Private video")
            sys.exit(1)
        ''')

        _download(links, run_async, make_event(event_id=529), playlist_count=3, mode="full")

        assert _ls(links.video) == ["1-Uno.mp4"]
        assert "1/3" in texts()

    def test_una_playlist_de_audio_se_queda_con_los_mp3(self, links, fake_ytdlp, make_event, run_async, texts):
        fake_ytdlp('''
            for index, title in ((1, "Uno"), (2, "Dos")):
                src = path(title, "webm", index)
                mp3 = path(title, "mp3", index)
                out(f"[download] Destination: {src}")
                touch(src)
                out("[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s")
                out(f"[ExtractAudio] Destination: {mp3}")
                touch(mp3)
                os.remove(src)
                out(f"Deleting original file {src} (pass -k to keep)")
        ''')

        _download(links, run_async, make_event(event_id=525), is_audio=True, playlist_count=2, mode="full")

        assert _ls(links.audio) == ["1-Uno.mp3", "2-Dos.mp3"]
        assert "**2 archivos**" in texts()
        assert "parcial" not in texts()

    @pytest.mark.parametrize("mode,delete_after", [("SEND", False), ("SEND_DELETE", True)])
    def test_el_envio_automatico_manda_cada_video(
        self, links, fake_ytdlp, make_event, run_async, mode, delete_after
    ):
        settings.put("urls.auto_send", mode)
        fake_ytdlp(PLAYLIST_3)

        _download(links, run_async, make_event(event_id=526), playlist_count=3, mode="full")

        assert links.uploaded == [
            ("1-Uno.mp4", delete_after), ("2-Dos.mp4", delete_after), ("3-Tres.mp4", delete_after)
        ]

    def test_el_envio_automatico_no_manda_las_imagenes_de_un_carrusel(
        self, links, fake_ytdlp, make_event, run_async
    ):
        settings.put("urls.auto_send", "SEND")
        fake_ytdlp('''
            for index, ext in ((1, "jpg"), (2, "mp4"), (3, "jpg")):
                final = path("Post", ext, index)
                out(f"[download] Destination: {final}")
                touch(final)
                out("[download] 100% of  200.00KiB in 00:00:00 at 5.00MiB/s")
        ''')

        _download(links, run_async, make_event(event_id=527), playlist_count=3, mode="full")

        assert _ls(links.video) == ["1-Post.jpg", "2-Post.mp4", "3-Post.jpg"]
        assert links.uploaded == [("2-Post.mp4", False)]

    def test_store_no_envia_la_playlist(self, links, fake_ytdlp, make_event, run_async):
        settings.put("urls.auto_send", "STORE")
        fake_ytdlp(PLAYLIST_3)

        _download(links, run_async, make_event(event_id=528), playlist_count=3, mode="full")

        assert not links.uploaded


CANCELABLE_PLAYLIST = '''
done = path("Uno", "mp4", 1)
out(f"[download] Destination: {done}")
touch(done, b"terminado")
out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
part = path("Dos", "f137.mp4", 2)
out(f"[download] Destination: {part}")
touch(part + ".part")
out("[download]  12.0% of   10.00MiB at    1.00MiB/s ETA 00:09")
ready()
time.sleep(30)
'''

CANCELABLE_SINGLE = '''
part = path("Largo", "f137.mp4")
out(f"[download] Destination: {part}")
touch(part + ".part")
ready()
time.sleep(30)
'''


async def _wait_ready(fake_ytdlp, bot, event_id):
    for _ in range(500):
        if fake_ytdlp.ready.exists() and isinstance(bot.active_tasks.get(event_id), asyncio.subprocess.Process):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("el yt-dlp falso no llegó a arrancar")


class TestCancelar:
    def _run_and_cancel(self, links, fake_ytdlp, make_event, run_async, event_id, playlist_count, mode):
        async def scenario():
            event = make_event(event_id=event_id)
            await links.bot.start_ytdlp_download(event, None, URL, False, playlist_count, mode)
            task = links.bot.active_tasks[event_id]
            await _wait_ready(fake_ytdlp, links.bot, event_id)
            click = make_event(event_id=9000 + event_id, data=f"cancel:{event_id}".encode())
            await links.bot.cancel_download(click)
            await asyncio.wait_for(task, timeout=10)
        run_async(scenario())

    def test_cancelar_una_playlist_conserva_los_videos_terminados(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp(CANCELABLE_PLAYLIST)

        self._run_and_cancel(links, fake_ytdlp, make_event, run_async, 531, 3, "full")

        assert _ls(links.video) == ["1-Uno.mp4"]
        assert (links.video / "1-Uno.mp4").read_bytes() == b"terminado"
        assert _ls(links.temp) == [], "el vídeo a medias se borra"
        assert "Se conservaron **1 de 3 vídeos**" in texts()
        assert 531 not in links.bot.active_tasks

    def test_cancelar_un_video_suelto_borra_lo_descargado(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp(CANCELABLE_SINGLE)

        self._run_and_cancel(links, fake_ytdlp, make_event, run_async, 532, 1, None)

        assert _ls(links.video) == []
        assert _ls(links.temp) == []
        assert "Descarga cancelada" in texts()
        assert "ha fallado" not in texts()
        assert 532 not in links.bot.active_tasks

    def test_cancelar_una_playlist_sin_nada_terminado_es_una_cancelacion_normal(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp(CANCELABLE_SINGLE)

        self._run_and_cancel(links, fake_ytdlp, make_event, run_async, 533, 5, "full")

        assert _ls(links.video) == [] and _ls(links.temp) == []
        assert "Descarga cancelada" in texts()
        assert "conservaron" not in texts()

    def test_cancelar_la_tarea_mata_ytdlp_y_limpia(self, links, fake_ytdlp, make_event, run_async, texts):
        """La otra vía de cancelación: la tarea de asyncio (p. ej. al apagar)."""
        fake_ytdlp(CANCELABLE_SINGLE)

        async def scenario():
            event = make_event(event_id=534)
            await links.bot.start_ytdlp_download(event, None, URL, False)
            task = links.bot.active_tasks[534]
            await _wait_ready(fake_ytdlp, links.bot, 534)
            proc = links.bot.active_tasks[534]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return proc

        proc = run_async(scenario())

        assert proc.returncode is not None, "yt-dlp no debe quedar vivo"
        assert _ls(links.temp) == []
        assert "Descarga cancelada" in texts()
        assert 534 not in links.bot.active_tasks


class TestBotonCancelar:
    def test_una_tarea_que_aun_no_ha_lanzado_ytdlp_se_cancela(self, quiet_bot, make_event, run_async):
        async def scenario():
            task = asyncio.create_task(asyncio.sleep(30))
            quiet_bot.active_tasks[541] = task
            await quiet_bot.cancel_download(make_event(data=b"cancel:541"))
            await asyncio.sleep(0)
            return task.cancelled()

        try:
            assert run_async(scenario()) is True
        finally:
            quiet_bot.active_tasks.pop(541, None)

    def test_una_descarga_ya_terminada_solo_borra_el_mensaje(self, quiet_bot, make_event, run_async, sent_messages):
        quiet_bot.active_tasks.pop(542, None)

        run_async(quiet_bot.cancel_download(make_event(data=b"cancel:542")))

        assert [kind for kind, _, _ in sent_messages] == ["delete"]

    def test_un_proceso_que_acaba_justo_al_cancelar(self, quiet_bot, make_event, run_async, sent_messages):
        async def scenario():
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")

            def gone():
                raise ProcessLookupError

            proc.terminate = gone
            quiet_bot.active_tasks[543] = proc
            try:
                await quiet_bot.cancel_download(make_event(data=b"cancel:543"))
            finally:
                quiet_bot.active_tasks.pop(543, None)
                proc.kill()
                await proc.wait()

        run_async(scenario())

        assert [kind for kind, _, _ in sent_messages] == ["delete"]


class TestProgreso:
    def test_el_progreso_de_una_playlist_dice_por_que_video_va(
        self, links, fake_ytdlp, make_event, run_async
    ):
        fake_ytdlp('''
            final = path("Dos", "mp4", 2)
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download]  45.2% of ~  10.00MiB at    1.23MiB/s ETA 00:30 (frag 3/10)")
            out("[download] 100% of   10.00MiB in 00:00:08 at 1.20MiB/s")
        ''')
        status = MagicMock(name="progreso")
        status.edit = AsyncMock()
        script = os.path.join(os.environ["PATH"].split(os.pathsep)[0], "yt-dlp")
        cmd = [script, "-o", str(links.temp / "%(playlist_index&{}-|)s%(title).200s_temp1.%(ext)s"), URL]

        run_async(links.bot.run_url_download(
            make_event(event_id=551), cmd, status, str(links.video),
            is_full_playlist=True, total_videos=4, temp_marker="_temp1",
        ))

        texts = [call.args[0] for call in status.edit.await_args_list]
        assert len(texts) == 2, "la primera línea y el 100% se muestran siempre"
        assert "Vídeo 2/4" in texts[0] and "45.2%" in texts[0] and "2-Dos_temp1.mp4" in texts[0]
        # (2-1)/4 de la playlist + 45.2%/4 del vídeo actual
        assert "36.3% total" in texts[0]
        assert "50.0% total" in texts[1]
        assert _buttons(status.edit.await_args_list[0].kwargs) == [b"cancel:551"]


class TestParseProgress:
    @pytest.mark.parametrize("line,expected", [
        ("[download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:30",
         {"percent": "45.2", "size": "123.45MiB", "speed": "1.23MiB/s", "eta": "00:30"}),
        ("[download]  13.7% of ~   4.89GiB at   62.33MiB/s ETA 01:10 (frag 42/306)",
         {"percent": "13.7", "size": "4.89GiB", "speed": "62.33MiB/s", "eta": "01:10"}),
        ("[download] 100% of   10.00MiB in 00:00:01 at 10.00MiB/s",
         {"percent": "100", "size": "10.00MiB", "speed": "N/A", "eta": "N/A"}),
        ("[download]   0.0% of   10.00MiB at  Unknown B/s ETA Unknown",
         {"percent": "0.0", "size": "10.00MiB", "speed": "N/A", "eta": "N/A"}),
    ])
    def test_lineas_reales(self, dropbot, line, expected):
        assert dropbot.parse_progress(line) == expected

    @pytest.mark.parametrize("line", [
        "[download] Destination: /tmp/x.mp4",
        "[youtube] abc: Downloading webpage",
        "[download] 100% done",
        "",
    ])
    def test_lo_que_no_es_progreso(self, dropbot, line):
        assert dropbot.parse_progress(line) is None


class TestUpdateProgressMessage:
    INFO = {"percent": "50", "size": "10MiB", "speed": "1MiB/s", "eta": "00:05"}

    def _status(self):
        status = MagicMock(name="progreso")
        status.edit = AsyncMock()
        return status

    def test_sin_mensaje_no_hace_nada(self, quiet_bot, make_event, run_async):
        run_async(quiet_bot.update_progress_message(None, self.INFO, make_event()))

    def test_dibuja_la_barra_y_el_nombre(self, quiet_bot, make_event, run_async, config_dir):
        status = self._status()

        run_async(quiet_bot.update_progress_message(status, self.INFO, make_event(event_id=8), "clip.mp4"))

        text = status.edit.await_args.args[0]
        assert "█" * 10 + "░" * 10 in text
        assert "clip.mp4" in text and "1MiB/s" in text and "00:05" in text

    def test_sin_nombre_usa_el_del_progreso(self, quiet_bot, make_event, run_async, config_dir):
        status = self._status()

        run_async(quiet_bot.update_progress_message(status, {**self.INFO, "filename": "wget.iso"}, make_event()))

        assert "wget.iso" in status.edit.await_args.args[0]

    def test_un_error_de_telegram_no_corta_la_descarga(self, quiet_bot, make_event, run_async, config_dir):
        status = MagicMock(name="progreso")
        status.edit = AsyncMock(side_effect=RuntimeError("FloodWait"))

        run_async(quiet_bot.update_progress_message(status, self.INFO, make_event(), "x"))

    def test_un_progreso_ilegible_no_corta_la_descarga(self, quiet_bot, make_event, run_async, config_dir):
        status = self._status()

        run_async(quiet_bot.update_progress_message(status, {"percent": "NaN?"}, make_event(), "x"))

        status.edit.assert_not_awaited()


class TestExtractFilePaths:
    def test_un_fichero_sin_mezcla(self, dropbot):
        lines = [
            "[download] Destination: /tmp/Clip_temp1.mp4",
            "[download]  50.0% of 1.00MiB at 1.00MiB/s ETA 00:01",
            "[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s",
        ]
        assert dropbot.extract_file_paths(lines) == ["/tmp/Clip_temp1.mp4"]

    def test_un_fichero_a_medias_no_cuenta(self, dropbot):
        lines = ["[download] Destination: /tmp/Clip_temp1.mp4", "[download]  50.0% of 1.00MiB"]
        assert dropbot.extract_file_paths(lines) == []

    def test_audio_extraido(self, dropbot):
        lines = [
            "[download] Destination: /tmp/Cancion_temp1.webm",
            "[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s",
            "[ExtractAudio] Destination: /tmp/Cancion_temp1.mp3",
            "Deleting original file /tmp/Cancion_temp1.webm (pass -k to keep)",
        ]
        assert "/tmp/Cancion_temp1.mp3" in dropbot.extract_file_paths(lines)

    def test_formatos_sueltos_sin_mezclar_no_se_entregan(self, dropbot):
        """Si la mezcla no llega a ocurrir, los .fNNN no son el vídeo."""
        lines = [
            "[download] Destination: /tmp/V_temp1.f137.mp4",
            "[download] 100% of   10.00MiB in 00:00:01 at 10.00MiB/s",
            "[download] Destination: /tmp/V_temp1.f140.m4a",
            "[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s",
        ]
        assert dropbot.extract_file_paths(lines) == []

    def test_una_playlist_mezclada_da_cada_video_una_vez(self, dropbot):
        lines = []
        for index in (1, 2):
            lines += [
                f"[download] Destination: /tmp/{index}-V_temp1.f137.mp4",
                "[download] 100% of   10.00MiB in 00:00:01 at 10.00MiB/s",
                f"[download] Destination: /tmp/{index}-V_temp1.f140.m4a",
                "[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s",
                f'[Merger] Merging formats into "/tmp/{index}-V_temp1.mp4"',
                # El 100% repetido tras la mezcla no debe duplicar
                "[download] 100% of   11.00MiB",
            ]
        assert dropbot.extract_file_paths(lines) == ["/tmp/1-V_temp1.mp4", "/tmp/2-V_temp1.mp4"]

    def test_las_rutas_con_espacios_se_conservan(self, dropbot):
        lines = ['[Merger] Merging formats into "/tmp/Un vídeo_temp1.mp4"']
        assert dropbot.extract_file_paths(lines) == ["/tmp/Un vídeo_temp1.mp4"]

    def test_un_fichero_ya_descargado(self, dropbot):
        lines = [
            "[youtube] abc: Downloading webpage",
            "[download] /tmp/Clip_temp1.mp4 has already been downloaded",
        ]
        assert dropbot.extract_file_paths(lines) == ["/tmp/Clip_temp1.mp4"]

    def test_un_video_ya_descargado_dentro_de_una_playlist(self, dropbot):
        lines = [
            "[download] Destination: /tmp/1-A_temp1.f137.mp4",
            "[download] 100% of   10.00MiB in 00:00:01 at 10.00MiB/s",
            '[Merger] Merging formats into "/tmp/1-A_temp1.mp4"',
            "[download] /tmp/2-B_temp1.mp4 has already been downloaded",
        ]
        assert dropbot.extract_file_paths(lines) == ["/tmp/1-A_temp1.mp4", "/tmp/2-B_temp1.mp4"]


class TestUrlFailureMessage:
    def test_el_tamano_maximo_en_stderr_tambien_se_reconoce(self, dropbot, config_dir):
        settings.put("urls.max_size_mb", 1024)
        lines = ["ERROR: something", "[download] File is larger than max-filesize (2 bytes > 1 bytes). Aborting."]
        assert "1 GB" in dropbot.url_failure_message(lines)

    def test_sin_lineas_es_el_error_generico(self, dropbot, config_dir):
        assert dropbot.url_failure_message([]) == dropbot.get_text("error_url_failed_user")


class TestRescateYLimpieza:
    @pytest.fixture
    def temp(self, links):
        return links

    def test_el_rescate_acepta_audio_y_descarta_lo_que_no_es_media(self, temp, run_async):
        for name in ("1-A_temp9.mp3", "2-B_temp9.m4a", "3-C_temp9.opus", "1-A_temp9.jpg",
                     "4-D_temp9.mp4.part", "4-D_temp9.temp.mp4"):
            (temp.temp / name).write_bytes(b"x")

        moved = run_async(temp.bot.rescue_completed_playlist_files("_temp9", str(temp.audio)))

        assert moved == 3
        assert _ls(temp.audio) == ["1-A.mp3", "2-B.m4a", "3-C.opus"]

    def test_un_fallo_al_mover_no_para_el_rescate(self, temp, run_async, monkeypatch):
        for name in ("1-A_temp9.mp4", "2-B_temp9.mp4"):
            (temp.temp / name).write_bytes(b"x")
        real = temp.bot.move_download_to

        async def flaky(path, final_dir):
            if "1-A" in path:
                raise OSError("disco lleno")
            return await real(path, final_dir)

        monkeypatch.setattr(temp.bot, "move_download_to", flaky)

        assert run_async(temp.bot.rescue_completed_playlist_files("_temp9", str(temp.video))) == 1
        assert _ls(temp.video) == ["2-B.mp4"]

    def test_remove_temp_files_no_toca_carpetas_ni_otras_marcas(self, temp):
        (temp.temp / "x_temp9.mp4").write_bytes(b"x")
        (temp.temp / "dir_temp9").mkdir()
        (temp.temp / "x_temp8.mp4").write_bytes(b"x")

        temp.bot.remove_temp_files("_temp9")

        assert _ls(temp.temp) == ["dir_temp9", "x_temp8.mp4"]

    def test_mover_conserva_acentos_y_quita_solo_la_marca(self, temp, run_async):
        src = temp.temp / "Canción_v2_temp55.mp3"
        src.write_bytes(b"x")

        final = run_async(temp.bot.move_download_to(str(src), str(temp.audio)))

        assert os.path.basename(final) == "Canción_v2.mp3"
        assert not src.exists()

    def test_mover_no_toca_un_titulo_que_parece_una_marca(self, temp, run_async):
        src = temp.temp / "Prueba_temp2.final_temp1759780000000.mp4"
        src.write_bytes(b"x")

        final = run_async(temp.bot.move_download_to(str(src), str(temp.video)))

        assert os.path.basename(final) == "Prueba_temp2.final.mp4"

    def test_handle_cancel_sin_mensaje_ni_marca(self, quiet_bot, run_async, sent_messages):
        run_async(quiet_bot.handle_cancel(None))
        assert sent_messages == []

    def test_handle_cancel_quita_los_botones(self, quiet_bot, run_async, sent_messages, config_dir):
        run_async(quiet_bot.handle_cancel(MagicMock()))
        ((kind, text, kwargs),) = sent_messages
        assert kind == "edit" and "Descarga cancelada" in text and kwargs["buttons"] is None
