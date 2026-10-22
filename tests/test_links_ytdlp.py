"""Descargas de enlaces con yt-dlp, de punta a punta y sin red.

Se pone delante en el PATH un `yt-dlp` falso que imprime las líneas que
imprime el de verdad, crea los ficheros en la ruta de `-o` y sale con el código
que pida el test. Así se prueba el comando que construye el bot, la lectura de
la salida, qué acaba en la carpeta de destino, qué queda en TEMP_DIR y qué se le
dice al usuario.
"""

import asyncio
import contextlib
import json
import os
import signal
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import settings
from button_data import button_data
from conftest import FORUM_TOPIC
from proc_helpers import alive, install_fake, wait_until

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

def done(p):
    """Lo que imprime --print after_move:… al acabar del todo un vídeo."""
    if "--print" in args:
        out(args[args.index("--print") + 1].removeprefix("after_move:").replace("%(filepath)s", p))

def gone(p, timeout=15):
    """Si `p` deja de estar en su sitio (el bot se lo lleva) antes de `timeout` s.

    Sale en cuanto se lo lleva, y el plazo es solo para una máquina lenta: un
    bot que los moviera todos al final no se lo llevaría mientras yt-dlp sigue.
    """
    end = time.time() + timeout
    while os.path.exists(p):
        if time.time() > end:
            return False
        time.sleep(0.01)
    return True
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
        return str(install_fake(bindir, "yt-dlp", f"#!{sys.executable}\n{_FAKE_HEADER}\n{textwrap.dedent(body)}"))

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
    """Lanza la descarga como lo hace el bot y espera a que termine.

    `target` es el mensaje que pasa a ser el de progreso (el de análisis o el
    de los botones); si no se da, uno cualquiera.
    """
    if target is None:
        target = MagicMock(name="botones")

    async def scenario():
        await links.bot.start_ytdlp_download(event, target, URL, is_audio, playlist_count, mode)
        await links.bot.active_tasks[links.bot.task_key(event)]
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
        assert "42_501" not in links.bot.active_tasks

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
        for flag in ("--sleep-interval", "--no-playlist", "--ignore-errors", "--playlist-end", "--print"):
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
        assert "convertir" not in texts()

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

    def test_si_no_se_puede_editar_el_mensaje_baja_sin_uno_nuevo(
        self, links, fake_ytdlp, make_event, run_async, sent_messages, monkeypatch
    ):
        """Si la cola no edita a tiempo, un «Descargando…» mandado sin esperar
        salía después, sin referencia: ni progreso ni nadie que lo borrara."""
        fake_ytdlp(VIDEO_MERGE)

        async def edit_timeout(message, text=None, **kwargs):
            sent_messages.append(("edit", str(text), kwargs))
            return None

        monkeypatch.setattr(links.bot, "safe_edit", edit_timeout)

        _download(links, run_async, make_event(event_id=509), target=MagicMock())

        downloading = [kind for kind, text, _ in sent_messages if "Descargando" in text]
        assert downloading == ["edit"]
        assert _ls(links.video) == ["Mi_video.mp4"]
        assert "Descarga completada" in " ".join(text for _, text, _ in sent_messages)


class TestTemaDelForo:
    """El mensaje del botón pasa a ser el de progreso y se borra al terminar:
    lo que se le contestaba después, sin el tema, salía en General."""

    def _answers(self, sent_messages):
        """Lo que se dice al usuario aparte de editar y borrar el progreso."""
        return [(kind, text, kwargs) for kind, text, kwargs in sent_messages
                if kind not in ("edit", "delete")]

    @pytest.mark.parametrize("auto_send, then", [
        ("ASK", ("upload_asking",)),
        ("SEND", ("sending", "Mi_video.mp4")),
    ])
    def test_lo_que_se_dice_al_acabar_va_al_tema_del_boton(
        self, links, fake_ytdlp, forum_callback, run_async, sent_messages, auto_send, then
    ):
        """«Descarga completada» y, detrás, sus botones o el «Enviando…»: con
        los botones en General, al pulsar Enviar el vídeo también iba allí."""
        settings.put("urls.auto_send", auto_send)
        fake_ytdlp(VIDEO_MERGE)
        click = forum_callback()

        _download(links, run_async, click, target=click)

        answers = self._answers(sent_messages)
        assert "Descarga completada" in answers[0][1]
        assert answers[1][1] == links.bot.get_text(*then)
        assert [(kind, kwargs["reply_to"]) for kind, _, kwargs in answers] == [("send_message", FORUM_TOPIC)] * 2

    def test_el_aviso_de_la_playlist_va_al_tema_del_boton(
        self, links, fake_ytdlp, forum_callback, run_async, sent_messages
    ):
        fake_ytdlp(PLAYLIST_3)
        click = forum_callback()

        _download(links, run_async, click, playlist_count=3, mode="full", target=click)

        [(kind, text, kwargs)] = self._answers(sent_messages)
        assert "Playlist almacenada" in text
        assert kind == "send_message" and kwargs["reply_to"] == FORUM_TOPIC


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
        assert "42_511" not in links.bot.active_tasks

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
        assert "42_515" not in links.bot.active_tasks

    def test_un_fichero_anunciado_que_no_existe_no_rompe_nada(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp('''
            out(f"[download] Destination: {path('Fantasma', 'mp4')}")
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
        ''')

        _download(links, run_async, make_event(event_id=516))

        assert _ls(links.video) == []
        assert "42_516" not in links.bot.active_tasks

    def test_un_merge_que_falla_se_dice(self, links, fake_ytdlp, make_event, run_async, texts):
        """yt-dlp anuncia el fichero mezclado antes de que ffmpeg lo escriba: si
        ffmpeg falla (sin espacio, por ejemplo), ese fichero no llega a existir
        y el progreso desaparecía sin ningún aviso."""
        fake_ytdlp('''
            v = path("Mi_video", "f137.mp4")
            a = path("Mi_video", "f251.webm")
            final = path("Mi_video", "mkv")
            out(f"[download] Destination: {v}")
            touch(v)
            out("[download] 100% of   10.00MiB in 00:00:01 at 10.00MiB/s")
            out(f"[download] Destination: {a}")
            touch(a)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
            out(f'[Merger] Merging formats into "{final}"')
            touch(path("Mi_video", "temp.mkv"), b"a medias")
            err("ERROR: Postprocessing: Conversion failed!")
            sys.exit(1)
        ''')

        _download(links, run_async, make_event(event_id=517))

        assert "La descarga desde la URL ha fallado" in texts()
        assert _ls(links.video) == [] and _ls(links.temp) == []

    def test_un_audio_a_medio_convertir_no_se_entrega(self, links, fake_ytdlp, make_event, run_async, texts):
        """ffmpeg escribe el .mp3 directamente: si falla a medias, queda cortado y
        yt-dlp no llega a borrar el original, que es lo que está entero."""
        fake_ytdlp('''
            src = path("Cancion", "webm")
            mp3 = path("Cancion", "mp3")
            out(f"[download] Destination: {src}")
            touch(src, b"original")
            out("[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s")
            out(f"[ExtractAudio] Destination: {mp3}")
            touch(mp3, b"cortado")
            err("ERROR: Postprocessing: audio conversion failed: No space left on device")
            sys.exit(1)
        ''')

        _download(links, run_async, make_event(event_id=518), is_audio=True)

        assert _ls(links.audio) == ["Cancion.webm"]
        assert (links.audio / "Cancion.webm").read_bytes() == b"original"
        assert texts().count("Descarga completada") == 1
        assert _ls(links.temp) == []

    @pytest.mark.parametrize("audio_format, source", [("MP3", "webm"), ("M4A", "opus")])
    def test_si_no_se_puede_convertir_el_audio_se_avisa(
        self, links, fake_ytdlp, make_event, run_async, texts, audio_format, source
    ):
        """Se entrega el original, que está entero, pero no es lo que se pidió:
        se daba por una descarga completada sin más, y con el envío automático
        llegaba un .webm en vez de un MP3."""
        settings.put("urls.audio_format", audio_format)
        fake_ytdlp(f'''
            src = path("Cancion", "{source}")
            dst = path("Cancion", "{audio_format.lower()}")
            out(f"[download] Destination: {{src}}")
            touch(src, b"original")
            out("[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s")
            out(f"[ExtractAudio] Destination: {{dst}}")
            touch(dst, b"cortado")
            err("ERROR: Postprocessing: audio conversion failed: Conversion failed!")
            sys.exit(1)
        ''')

        _download(links, run_async, make_event(event_id=585), is_audio=True)

        assert _ls(links.audio) == [f"Cancion.{source}"]
        assert texts().count("Descarga completada") == 1
        assert f"No se ha podido convertir a {audio_format}" in texts()
        assert f"`Cancion.{source}`" in texts()

    def test_una_linea_de_mas_de_64_kib_no_corta_la_descarga(self, links, fake_ytdlp, make_event, run_async, texts):
        """yt-dlp repite el título de la playlist, que pone un tercero: con
        readline() una línea tan larga cortaba la lectura, se avisaba de un
        error y yt-dlp seguía descargando sin que nadie lo leyera."""
        fake_ytdlp('''
            out("[download] Downloading playlist: " + "A" * 100000)
            final = path("Clip", "mp4")
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
        ''')

        _download(links, run_async, make_event(event_id=571))

        assert _ls(links.video) == ["Clip.mp4"]
        assert "Descarga completada" in texts() and "ha fallado" not in texts()

    def test_una_salida_que_no_es_utf8_no_tira_la_descarga(self, links, fake_ytdlp, make_event, run_async, texts):
        """ffmpeg imprime tal cual los metadatos, que no tienen por qué ser UTF-8."""
        fake_ytdlp('''
            sys.stderr.buffer.write(b"[ffmpeg] title: \\xff\\xfe sin decodificar\\n")
            sys.stderr.flush()
            final = path("Clip", "mp4")
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
        ''')

        _download(links, run_async, make_event(event_id=572))

        assert _ls(links.video) == ["Clip.mp4"]
        assert "ha fallado" not in texts()

    def test_un_error_a_mitad_no_deja_ytdlp_vivo(
        self, links, fake_ytdlp, make_event, run_async, texts, monkeypatch
    ):
        """Se avisaba del error y se borraban los temporales con yt-dlp vivo: sin
        nadie leyendo su salida, acababa bloqueado escribiendo hasta reiniciar."""
        fake_ytdlp('''
            part = path("Largo", "f137.mp4")
            out(f"[download] Destination: {part}")
            touch(part + ".part")
            out("[download]  12.0% of   10.00MiB at    1.00MiB/s ETA 00:09")
            time.sleep(30)
        ''')
        procs = []

        def broken(line):
            procs.append(links.bot.active_tasks["42_573"])
            raise RuntimeError("fallo leyendo la salida")

        monkeypatch.setattr(links.bot, "parse_progress", broken)

        try:
            _download(links, run_async, make_event(event_id=573))

            (proc,) = procs
            assert proc.returncode is not None, "yt-dlp no debe quedar vivo"
        finally:
            if procs and procs[0].returncode is None:
                os.kill(procs[0].pid, signal.SIGKILL)
        assert "ha fallado" in texts()
        assert _ls(links.temp) == []
        assert "42_573" not in links.bot.active_tasks


PLAYLIST_3 = '''
for index, title in ((1, "Uno"), (2, "Dos"), (3, "Tres")):
    out(f"[download] Downloading item {index} of 3")
    final = path(title, "mp4", index)
    out(f"[download] Destination: {final}")
    touch(final, title.encode())
    out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
out("[download] Finished downloading playlist: Lista")
'''

# Como el de verdad con --print after_move: avisa de cada vídeo terminado y,
# antes de bajar el siguiente, apunta si el bot ya lo ha sacado de TEMP_DIR.
# FAKE_UNAVAILABLE es un vídeo que no está disponible
PLAYLIST_DONE = '''
skip = os.environ.get("FAKE_UNAVAILABLE")
moved = []
for index, title in ((1, "Uno"), (2, "Dos"), (3, "Tres")):
    if title == skip:
        err(f"ERROR: [youtube] {title}: Video unavailable")
        continue
    final = path(title, "mp4", index)
    out(f"[download] Destination: {final}")
    touch(final, title.encode())
    out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
    done(final)
    moved.append(gone(final))
with open(os.environ["FAKE_YTDLP_READY"] + ".moved", "w") as handle:
    json.dump(moved, handle)
sys.exit(1 if skip else 0)
'''


def _moved_early(fake_ytdlp):
    return json.loads((fake_ytdlp.ready.parent / "ytdlp-ready.moved").read_text())


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
        assert data == [b"cancel:42_521"], "solo el botón de cancelar del progreso"
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
        # Avisa de cada vídeo terminado sin callar el progreso
        assert args[args.index("--print") + 1] == "after_move:DROPBOT_DONE:%(filepath)s"
        assert "--no-quiet" in args

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

    def test_solo_el_primero_de_una_lista_pura_baja_uno(self, links, fake_ytdlp, make_event, run_async, texts):
        """En playlist?list=… (sin vídeo en el enlace) --no-playlist no hace nada:
        yt-dlp bajaba la lista entera, sin tope ni pausa entre vídeos."""
        fake_ytdlp('''
            items = [(1, "Uno"), (2, "Dos"), (3, "Tres")]
            if "--playlist-items" in args:
                items = items[:int(args[args.index("--playlist-items") + 1])]
            for index, title in items:
                final = path(title, "mp4", index)
                out(f"[download] Destination: {final}")
                touch(final)
                out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
        ''')

        _download(links, run_async, make_event(event_id=520), playlist_count=3, mode="first")

        assert _ls(links.video) == ["1-Uno.mp4"]
        assert texts().count("Descarga completada") == 1

    def test_un_titulo_que_imita_una_ruta_no_mueve_nada_de_fuera(
        self, links, fake_ytdlp, make_event, run_async, tmp_path, monkeypatch
    ):
        """yt-dlp imprime tal cual el título de la playlist, que pone un tercero.
        Con uno que imitaba la línea del Merger, el bot movía ese fichero a la
        carpeta de descargas y lo borraba de su sitio (el contenedor es root)."""
        victim = tmp_path / "settings.json"
        victim.write_text("{}")
        monkeypatch.setenv("FAKE_VICTIM", str(victim))
        ajeno = links.temp / "Otro_temp1.mp4"
        ajeno.write_bytes(b"de otra descarga")
        fake_ytdlp('''
            victim = os.environ["FAKE_VICTIM"]
            out(f"[download] Downloading playlist: Top 2026 [Merger] Merging formats into {victim}")
            # Un salto de línea en el título imita la línea entera
            out(f'[Merger] Merging formats into "{victim}"')
            out(f"[ExtractAudio] Destination: {os.path.dirname(template)}/Otro_temp1.mp4")
            enlace = path("Enlace", "mp4", 2)
            os.symlink(victim, enlace)
            out(f'[Merger] Merging formats into "{enlace}"')
            # También el aviso de vídeo terminado, que se mueve en el acto
            for ruta in (victim, f"{os.path.dirname(template)}/Otro_temp1.mp4", enlace):
                done(ruta)
            final = path("Uno", "mp4", 1)
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
            done(final)
        ''')

        _download(links, run_async, make_event(event_id=519), playlist_count=2, mode="full")

        assert victim.read_text() == "{}"
        assert ajeno.read_bytes() == b"de otra descarga"
        assert _ls(links.video) == ["1-Uno.mp4"]

    @pytest.mark.parametrize("mode, count", [(None, 1), ("first", 3)])
    def test_un_titulo_con_un_nul_no_tumba_la_descarga(
        self, links, fake_ytdlp, make_event, run_async, texts, mode, count
    ):
        """Comprobar una ruta con un NUL lanzaba ValueError: la descarga se daba
        por fallida y se borraba de TEMP_DIR el vídeo, que estaba bien."""
        fake_ytdlp(r'''
            # Un título con un salto de línea y un NUL imita la línea del Merger
            out("[download] Downloading playlist: Top")
            out("[Merger] Merging formats into /tmp/a\x00b")
            final = path("Uno", "mp4", 1)
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
        ''')

        _download(links, run_async, make_event(event_id=531), playlist_count=count, mode=mode)

        assert _ls(links.video) == ["1-Uno.mp4"]
        assert "Descarga completada" in texts() and "ha fallado" not in texts()

    def test_un_aviso_de_terminado_con_un_nul_no_se_lleva_la_playlist(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        """El error rompía la cadena de traslados: ya no se movía ningún vídeo,
        la descarga se daba por fallida y se vaciaba TEMP_DIR."""
        fake_ytdlp(r'''
            out("[download] Downloading playlist: Top")
            out("DROPBOT_DONE:/tmp/a\x00b")
            for index, title in ((1, "Uno"), (2, "Dos")):
                final = path(title, "mp4", index)
                out(f"[download] Destination: {final}")
                touch(final)
                out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
                done(final)
        ''')

        _download(links, run_async, make_event(event_id=532), playlist_count=2, mode="full")

        assert _ls(links.video) == ["1-Uno.mp4", "2-Dos.mp4"]
        assert "Playlist almacenada" in texts() and "ha fallado" not in texts()

    def test_si_falla_la_comprobacion_de_un_video_terminado_se_mueven_los_demas(
        self, links, fake_ytdlp, make_event, run_async, texts, monkeypatch
    ):
        """Cada traslado espera al anterior: el error de uno lo relanzaban todos
        los de detrás y la playlist acababa en el error genérico."""
        real = links.bot.is_ytdlp_output
        checked = []

        def fails_once(path, marker):
            checked.append(path)
            if len(checked) == 1:
                raise OSError("Input/output error")
            return real(path, marker)

        monkeypatch.setattr(links.bot, "is_ytdlp_output", fails_once)
        fake_ytdlp('''
            for index, title in ((1, "Uno"), (2, "Dos")):
                final = path(title, "mp4", index)
                out(f"[download] Destination: {final}")
                touch(final)
                out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
                done(final)
        ''')

        _download(links, run_async, make_event(event_id=533), playlist_count=2, mode="full")

        assert _ls(links.video) == ["1-Uno.mp4", "2-Dos.mp4"]
        assert "Playlist almacenada" in texts() and "ha fallado" not in texts()

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
        assert "convertir" not in texts()

    def test_en_una_playlist_se_avisa_de_los_audios_sin_convertir(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        """Con --ignore-errors, yt-dlp sigue con el resto y da por terminado el
        original del que falla: se guardaba y contaba como «correctamente»."""
        fake_ytdlp('''
            for index, title in ((1, "Uno"), (2, "Dos")):
                src = path(title, "webm", index)
                mp3 = path(title, "mp3", index)
                out(f"[download] Destination: {src}")
                touch(src)
                out("[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s")
                out(f"[ExtractAudio] Destination: {mp3}")
                touch(mp3)
                if index == 1:
                    err("ERROR: Postprocessing: audio conversion failed: Conversion failed!")
                    done(src)
                else:
                    os.remove(src)
                    out(f"Deleting original file {src} (pass -k to keep)")
                    done(mp3)
            sys.exit(1)
        ''')

        _download(links, run_async, make_event(event_id=586), is_audio=True, playlist_count=2, mode="full")

        assert _ls(links.audio) == ["1-Uno.webm", "2-Dos.mp3"]
        assert "**2 archivos**" in texts()
        assert "Sin convertir a MP3: 1." in texts()

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

    def test_cada_video_se_lleva_a_su_carpeta_en_cuanto_acaba(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        """Se movían todos al final: una playlist de horas se quedaba entera en
        TEMP_DIR, en el disco del contenedor, y un reinicio a mitad la perdía."""
        fake_ytdlp(PLAYLIST_DONE)

        _download(links, run_async, make_event(event_id=581), playlist_count=3, mode="full")

        assert _moved_early(fake_ytdlp) == [True, True, True]
        # Ni se mueven dos veces ni se cuentan dos veces
        assert _ls(links.video) == ["1-Uno.mp4", "2-Dos.mp4", "3-Tres.mp4"]
        assert (links.video / "2-Dos.mp4").read_bytes() == b"Dos"
        assert _ls(links.temp) == []
        assert "Playlist almacenada" in texts() and "**3 archivos**" in texts()
        assert "parcial" not in texts()

    def test_los_videos_movidos_al_acabar_cuentan_bien_los_que_faltan(
        self, links, fake_ytdlp, make_event, run_async, texts, monkeypatch
    ):
        monkeypatch.setenv("FAKE_UNAVAILABLE", "Dos")
        fake_ytdlp(PLAYLIST_DONE)

        _download(links, run_async, make_event(event_id=582), playlist_count=3, mode="full")

        assert _moved_early(fake_ytdlp) == [True, True]
        assert _ls(links.video) == ["1-Uno.mp4", "3-Tres.mp4"]
        assert "parcial" in texts() and "**2/3 archivos**" in texts()
        assert "ha fallado" not in texts()

    def test_el_envio_automatico_manda_los_videos_movidos_al_acabar(
        self, links, fake_ytdlp, make_event, run_async
    ):
        settings.put("urls.auto_send", "SEND")
        fake_ytdlp(PLAYLIST_DONE)

        _download(links, run_async, make_event(event_id=583), playlist_count=3, mode="full")

        assert _moved_early(fake_ytdlp) == [True, True, True]
        assert links.uploaded == [("1-Uno.mp4", False), ("2-Dos.mp4", False), ("3-Tres.mp4", False)]

    def test_un_traslado_lento_no_para_la_lectura_de_ytdlp(
        self, links, fake_ytdlp, make_event, run_async, monkeypatch
    ):
        """Esperando la copia en la lectura de stdout, una larga entre
        dispositivos llenaba la tubería y dejaba a yt-dlp parado."""
        fake_ytdlp('''
            first = path("Uno", "mp4", 1)
            touch(first)
            done(first)
            # Más de lo que cabe en la tubería y el búfer de asyncio (unos
            # 192 KiB) mientras el bot copia el primero
            for _ in range(5000):
                out("[download]  50.0% of   10.00MiB at    1.00MiB/s ETA 00:05" + " " * 40)
            ready()
        ''')
        real_move = links.bot.move_download_to
        while_moving = []

        async def slow_move(path, folder):
            # La copia no acaba hasta que yt-dlp ha podido escribirlo todo; si
            # la lectura está parada, no llegará a poder
            with contextlib.suppress(AssertionError):
                await wait_until(fake_ytdlp.ready.exists)
            while_moving.append(fake_ytdlp.ready.exists())
            return await real_move(path, folder)

        monkeypatch.setattr(links.bot, "move_download_to", slow_move)

        _download(links, run_async, make_event(event_id=584), playlist_count=2, mode="full")

        assert while_moving == [True], "yt-dlp ha seguido escribiendo mientras se copiaba"
        assert _ls(links.video) == ["1-Uno.mp4"]


# Como el de verdad: carga las cookies al empezar y, al salir, reescribe el
# fichero que recibe con las que cargó más las que le ha renovado el sitio
COOKIES_DOWNLOAD = '''
jar = args[args.index("--cookies") + 1]
with open(jar) as handle:
    loaded = handle.read()
if os.environ.get("FAKE_NEW_EXPORT"):
    # Mientras dura la descarga, el admin exporta unas cookies nuevas
    with open(os.environ["FAKE_NEW_EXPORT"], "w") as handle:
        handle.write("# exportacion nueva\\n")
final = path("Clip", "mp4")
out(f"[download] Destination: {final}")
touch(final)
out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
with open(jar, "w") as handle:
    handle.write(loaded + "# renovada\\n")
'''


class TestCookies:
    @pytest.fixture
    def cookies(self, links, tmp_path, monkeypatch):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# sesion\n")
        monkeypatch.setattr(links.bot, "YTDLP_COOKIES_FILE", str(cookies))
        return cookies

    def test_unas_cookies_exportadas_durante_la_descarga_no_se_pisan(
        self, links, cookies, fake_ytdlp, make_event, run_async, monkeypatch
    ):
        """Con el fichero compartido, la descarga lo reescribía al salir con lo
        que cargó al empezar y la exportación nueva desaparecía sin aviso."""
        monkeypatch.setenv("FAKE_NEW_EXPORT", str(cookies))
        fake_ytdlp(COOKIES_DOWNLOAD)

        _download(links, run_async, make_event(event_id=561))

        assert cookies.read_text() == "# exportacion nueva\n"
        assert _ls(links.video) == ["Clip.mp4"]
        assert _ls(links.temp) == [], "la copia también se borra"

    def test_lo_que_renueva_una_descarga_se_guarda(self, links, cookies, fake_ytdlp, make_event, run_async):
        fake_ytdlp(COOKIES_DOWNLOAD)

        _download(links, run_async, make_event(event_id=562))

        assert cookies.read_text() == "# sesion\n# renovada\n"
        assert _ls(links.temp) == []

    def test_una_descarga_fallida_no_guarda_nada(self, links, cookies, fake_ytdlp, make_event, run_async):
        fake_ytdlp(COOKIES_DOWNLOAD + 'sys.exit(1)\n')

        _download(links, run_async, make_event(event_id=563))

        assert cookies.read_text() == "# sesion\n"
        assert _ls(links.temp) == []


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
time.sleep(60)
'''

# El primero ya se lo ha llevado el bot al terminarlo (--print after_move)
CANCELABLE_PLAYLIST_DONE = '''
first = path("Uno", "mp4", 1)
out(f"[download] Destination: {first}")
touch(first, b"terminado")
out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
done(first)
gone(first)
part = path("Dos", "f137.mp4", 2)
out(f"[download] Destination: {part}")
touch(part + ".part")
out("[download]  12.0% of   10.00MiB at    1.00MiB/s ETA 00:09")
ready()
time.sleep(60)
'''

CANCELABLE_SINGLE = '''
part = path("Largo", "f137.mp4")
out(f"[download] Destination: {part}")
touch(part + ".part")
ready()
time.sleep(60)
'''

# Un directo: yt-dlp le pasa la grabación a ffmpeg, que hereda sus tuberías
CANCELABLE_LIVE = '''
import subprocess
part = path("Directo", "mp4")
out(f"[download] Destination: {part}")
ffmpeg = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(os.environ["FAKE_YTDLP_READY"] + ".ffmpeg", "w") as handle:
    handle.write(str(ffmpeg.pid))
touch(part + ".part")
ready()
ffmpeg.wait()
'''


async def _wait_ready(fake_ytdlp, bot, event_id):
    await wait_until(
        lambda: fake_ytdlp.ready.exists()
        and isinstance(bot.active_tasks.get(f"42_{event_id}"), asyncio.subprocess.Process),
        message="el yt-dlp falso no llegó a arrancar",
    )


class TestCancelar:
    def _run_and_cancel(self, links, fake_ytdlp, make_event, run_async, event_id, playlist_count, mode):
        async def scenario():
            event = make_event(event_id=event_id)
            await links.bot.start_ytdlp_download(event, MagicMock(), URL, False, playlist_count, mode)
            task = links.bot.active_tasks[f"42_{event_id}"]
            await _wait_ready(fake_ytdlp, links.bot, event_id)
            click = make_event(event_id=9000 + event_id, data=f"cancel:42_{event_id}".encode())
            await links.bot.cancel_download(click)
            await asyncio.wait_for(task, timeout=30)
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
        assert "42_531" not in links.bot.active_tasks

    def test_cancelar_una_playlist_cuenta_los_videos_ya_movidos(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp(CANCELABLE_PLAYLIST_DONE)

        self._run_and_cancel(links, fake_ytdlp, make_event, run_async, 538, 3, "full")

        assert _ls(links.video) == ["1-Uno.mp4"]
        assert (links.video / "1-Uno.mp4").read_bytes() == b"terminado"
        assert _ls(links.temp) == []
        assert "Se conservaron **1 de 3 vídeos**" in texts()

    def test_cancelar_un_video_suelto_borra_lo_descargado(
        self, links, fake_ytdlp, make_event, run_async, texts
    ):
        fake_ytdlp(CANCELABLE_SINGLE)

        self._run_and_cancel(links, fake_ytdlp, make_event, run_async, 532, 1, None)

        assert _ls(links.video) == []
        assert _ls(links.temp) == []
        assert "Descarga cancelada" in texts()
        assert "ha fallado" not in texts()
        assert "42_532" not in links.bot.active_tasks

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
            await links.bot.start_ytdlp_download(event, MagicMock(), URL, False)
            task = links.bot.active_tasks["42_534"]
            await _wait_ready(fake_ytdlp, links.bot, 534)
            proc = links.bot.active_tasks["42_534"]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return proc

        proc = run_async(scenario())

        assert proc.returncode is not None, "yt-dlp no debe quedar vivo"
        assert _ls(links.temp) == []
        assert "Descarga cancelada" in texts()
        assert "42_534" not in links.bot.active_tasks

    def test_cancelar_un_directo_para_tambien_el_ffmpeg(self, links, fake_ytdlp, make_event, run_async, texts):
        """Solo se paraba yt-dlp: ffmpeg seguía grabando con sus tuberías abiertas,
        así que la descarga no llegaba a acabar y el disco se iba llenando."""
        fake_ytdlp(CANCELABLE_LIVE)

        self._run_and_cancel(links, fake_ytdlp, make_event, run_async, 535, 1, None)

        assert not alive(int((fake_ytdlp.ready.parent / "ytdlp-ready.ffmpeg").read_text()))
        assert "Descarga cancelada" in texts()
        assert _ls(links.video) == [] and _ls(links.temp) == []
        assert "42_535" not in links.bot.active_tasks

    def test_cancelar_la_tarea_para_tambien_el_ffmpeg(self, links, fake_ytdlp, make_event, run_async, texts):
        fake_ytdlp(CANCELABLE_LIVE)

        async def scenario():
            event = make_event(event_id=536)
            await links.bot.start_ytdlp_download(event, MagicMock(), URL, False)
            task = links.bot.active_tasks["42_536"]
            await _wait_ready(fake_ytdlp, links.bot, 536)
            task.cancel()
            done, _ = await asyncio.wait([task], timeout=30)
            return done

        assert run_async(scenario()), "la cancelación no ha terminado"
        assert not alive(int((fake_ytdlp.ready.parent / "ytdlp-ready.ffmpeg").read_text()))
        assert "Descarga cancelada" in texts()
        assert _ls(links.temp) == []

    def test_al_acabar_no_quita_la_tarea_que_la_ha_pisado(self, links, fake_ytdlp, make_event, run_async):
        """Dos descargas del mismo mensaje compartían la clave: la primera en
        acabar se llevaba la de la otra, que ya no se podía cancelar."""
        # No acaba hasta que la otra ha pisado la entrada, por lento que vaya
        fake_ytdlp('''
            ready()
            while not os.path.exists(os.environ["FAKE_YTDLP_READY"] + ".go"):
                time.sleep(0.01)
            final = path("Clip", "mp4")
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s")
        ''')
        other = object()

        async def scenario():
            event = make_event(event_id=537)
            await links.bot.start_ytdlp_download(event, None, URL, False)
            task = links.bot.active_tasks["42_537"]
            await _wait_ready(fake_ytdlp, links.bot, 537)
            links.bot.active_tasks["42_537"] = other
            open(f"{fake_ytdlp.ready}.go", "w").close()
            await asyncio.wait_for(task, timeout=30)

        run_async(scenario())

        assert links.bot.active_tasks["42_537"] is other


class TestBotonCancelar:
    def test_una_tarea_que_aun_no_ha_lanzado_ytdlp_se_cancela(self, quiet_bot, make_event, run_async):
        async def scenario():
            task = asyncio.create_task(asyncio.sleep(30))
            quiet_bot.active_tasks["42_541"] = task
            await quiet_bot.cancel_download(make_event(data=b"cancel:42_541"))
            await asyncio.sleep(0)
            return task.cancelled()

        try:
            assert run_async(scenario()) is True
        finally:
            quiet_bot.active_tasks.pop("42_541", None)

    def test_una_descarga_ya_terminada_solo_borra_el_mensaje(self, quiet_bot, make_event, run_async, sent_messages):
        quiet_bot.active_tasks.pop("42_542", None)

        run_async(quiet_bot.cancel_download(make_event(data=b"cancel:42_542")))

        assert [kind for kind, _, _ in sent_messages] == ["delete"]

    def test_un_proceso_que_acaba_justo_al_cancelar(self, quiet_bot, make_event, run_async, sent_messages):
        async def scenario():
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")

            def gone():
                raise ProcessLookupError

            proc.terminate = gone
            quiet_bot.active_tasks["42_543"] = proc
            try:
                await quiet_bot.cancel_download(make_event(data=b"cancel:42_543"))
            finally:
                quiet_bot.active_tasks.pop("42_543", None)
                proc.kill()
                await proc.wait()

        run_async(scenario())

        assert [kind for kind, _, _ in sent_messages] == ["delete"]

    @pytest.mark.parametrize("data", [b"cancel:abc", b"cancel:", b"cancel:\xff"])
    def test_un_data_mal_formado_solo_se_contesta(
        self, quiet_bot, make_event, run_async, sent_messages, monkeypatch, data
    ):
        """Los botones del bot siempre llevan un id: uno fabricado a mano no
        cancela nada ni deja el botón girando con una excepción en el log."""
        answered = []

        async def answer(event, *args, **kwargs):
            answered.append(event)

        monkeypatch.setattr(quiet_bot, "safe_answer", answer)

        run_async(quiet_bot.cancel_download(make_event(data=data)))

        assert len(answered) == 1
        assert sent_messages == []


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
        assert _buttons(status.edit.await_args_list[0].kwargs) == [b"cancel:42_551"]

    def test_con_telegram_sin_responder_la_descarga_sigue(
        self, links, fake_ytdlp, make_event, run_async, monkeypatch
    ):
        """Telethon retiene la edición hasta reconectar (minutos) y, mientras,
        nadie leía la salida de yt-dlp: la tubería se llenaba y se paraba."""
        monkeypatch.setattr(links.bot, "PROGRESS_EDIT_TIMEOUT", 0.1)
        fake_ytdlp('''
            final = path("Clip", "mp4")
            out(f"[download] Destination: {final}")
            touch(final)
            out("[download]  45.2% of   10.00MiB at    1.23MiB/s ETA 00:30")
            out("[download] 100% of   10.00MiB in 00:00:08 at 1.20MiB/s")
        ''')

        async def no_answer(*args, **kwargs):
            await asyncio.sleep(3600)

        status = MagicMock(name="progreso")
        status.edit = AsyncMock(side_effect=no_answer)
        script = os.path.join(os.environ["PATH"].split(os.pathsep)[0], "yt-dlp")
        cmd = [script, "-o", str(links.temp / "%(title).200s_temp1.%(ext)s"), URL]

        run_async(asyncio.wait_for(links.bot.run_url_download(
            make_event(event_id=552), cmd, status, str(links.video), temp_marker="_temp1",
        ), 10))

        assert _ls(links.video) == ["Clip.mp4"]
        assert status.edit.call_count == 2, "se sigue intentando, sin esperar a Telegram"


class TestLog:
    @pytest.fixture
    def logged(self, links, monkeypatch):
        lines = []
        for level in ("debug", "warning", "error"):
            monkeypatch.setattr(links.bot, level, lines.append)
        return lines

    @pytest.mark.parametrize("finished, code", [(True, 0), (False, 0), (False, 1)])
    def test_el_progreso_no_llena_el_log_y_lo_demas_sigue(
        self, links, fake_ytdlp, logged, make_event, run_async, finished, code
    ):
        """Con --newline yt-dlp da varias líneas de progreso por segundo, y cada
        una iba al log tres veces; el ffmpeg de un directo, otra por cada
        estadística. Sin rotación, el log de Docker crecía sin tope. Tampoco
        van en lo que se vuelca al log cuando la descarga no deja nada."""
        fake_ytdlp('''
            final = path("Clip", "mp4")
            out(f"[download] Destination: {final}")
            for i in range(300):
                out(f"[download] {i / 3:5.1f}% of   10.00MiB at    1.23MiB/s ETA 00:30")
                out(f"[download]  {i}.00KiB at  512.00KiB/s (00:00:0{i % 10})")
                sys.stderr.write(f"frame= {i} fps= 25 q=-1.0 size= {i}kB time=00:00:01.00 bitrate=1.0kbits/s speed=1.0x\\r")
            if FINISHED:
                touch(final)
            out("[download] 100% of   10.00MiB in 00:00:08 at 1.20MiB/s")
            err("WARNING: [generic] Falling back on generic information extractor")
            sys.exit(CODE)
        '''.replace("FINISHED", str(finished)).replace("CODE", str(code)))
        status = MagicMock(name="progreso")
        status.edit = AsyncMock()
        script = os.path.join(os.environ["PATH"].split(os.pathsep)[0], "yt-dlp")
        cmd = [script, "-o", str(links.temp / "%(title).200s_temp1.%(ext)s"), URL]

        run_async(links.bot.run_url_download(
            make_event(event_id=562), cmd, status, str(links.video), temp_marker="_temp1"))

        assert _ls(links.video) == (["Clip.mp4"] if finished else [])
        assert status.edit.await_count >= 2, "el progreso se sigue leyendo"
        assert len(logged) < 60, f"{len(logged)} líneas de log"
        assert not [line for line in logged if "ETA 00:30" in line or "KiB/s" in line or "frame=" in line]
        assert any("Destination:" in line for line in logged)
        assert any("100% of   10.00MiB in 00:00:08" in line for line in logged)
        assert any("Falling back on generic" in line for line in logged)

    @pytest.mark.parametrize("code", [0, 1])
    def test_la_contrasena_y_los_tokens_de_la_url_no_llegan_al_log(
        self, links, fake_ytdlp, logged, make_event, run_async, code
    ):
        """El comando de yt-dlp y lo que imprime dejaban en `docker logs` el
        usuario:contraseña de la URL y sus tokens; si fallaba, a nivel ERROR."""
        url = "https://admin:MiClave@nas.local/video?token=secreto"
        fake_ytdlp(f'''
            out(f"[generic] Extracting URL: {{args[-1]}}")
            err(f"ERROR: [generic] Unable to download webpage: {{args[-1]}}")
            sys.exit({code})
        ''')

        async def scenario():
            await links.bot.start_ytdlp_download(make_event(event_id=563), MagicMock(), url, False)
            await links.bot.active_tasks["42_563"]

        run_async(scenario())

        assert fake_ytdlp.calls()[0][-1] == url, "yt-dlp recibe la URL entera"
        log = "\n".join(logged)
        assert "nas.local/video" in log
        assert "MiClave" not in log and "secreto" not in log


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

    def test_una_edicion_sin_respuesta_no_se_espera(self, quiet_bot, make_event, run_async, config_dir, monkeypatch):
        monkeypatch.setattr(quiet_bot, "PROGRESS_EDIT_TIMEOUT", 0.1)

        async def no_answer(*args, **kwargs):
            await asyncio.sleep(3600)

        status = MagicMock(name="progreso")
        status.edit = AsyncMock(side_effect=no_answer)

        run_async(asyncio.wait_for(quiet_bot.update_progress_message(status, self.INFO, make_event(), "x"), 5))

        status.edit.assert_called_once()

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

    def test_el_audio_convertido_ocupa_el_sitio_del_original(self, dropbot):
        lines = []
        for index in (1, 2):
            lines += [
                f"[download] Destination: /tmp/{index}-C_temp1.webm",
                "[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s",
                f"[ExtractAudio] Destination: /tmp/{index}-C_temp1.mp3",
                f"Deleting original file /tmp/{index}-C_temp1.webm (pass -k to keep)",
            ]
        assert dropbot.extract_file_paths(lines) == ["/tmp/1-C_temp1.mp3", "/tmp/2-C_temp1.mp3"]

    def test_un_audio_que_no_se_llega_a_convertir_se_queda_en_el_original(self, dropbot):
        """Sin "Deleting original file", ffmpeg no terminó: el .mp3 está cortado."""
        lines = [
            "[download] Destination: /tmp/Cancion_temp1.webm",
            "[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s",
            "[ExtractAudio] Destination: /tmp/Cancion_temp1.mp3",
        ]
        assert dropbot.extract_file_paths(lines) == ["/tmp/Cancion_temp1.webm"]

    def test_dice_que_audios_no_se_llegaron_a_convertir(self, dropbot):
        lines = []
        for index in (1, 2):
            lines += [
                f"[download] Destination: /tmp/{index}-C_temp1.webm",
                "[download] 100% of    3.00MiB in 00:00:01 at 3.00MiB/s",
                f"[ExtractAudio] Destination: /tmp/{index}-C_temp1.mp3",
            ]
        lines.append("Deleting original file /tmp/2-C_temp1.webm (pass -k to keep)")
        unconverted = set()

        paths = dropbot.extract_file_paths(lines, unconverted)

        assert paths == ["/tmp/1-C_temp1.webm", "/tmp/2-C_temp1.mp3"]
        assert unconverted == {"/tmp/1-C_temp1.webm"}

    def test_un_titulo_de_playlist_no_cuela_rutas(self, dropbot):
        """yt-dlp imprime tal cual el título de la playlist, que pone un tercero."""
        lines = [
            "[download] Downloading playlist: Top 2026 [Merger] Merging formats into /app/dropbot.py",
            "[download] Downloading playlist: X [ExtractAudio] Destination: /config/settings.json",
            "[download] Downloading playlist: X [download] Destination: /etc/hostname",
            "[download] 100% of    1.00MiB in 00:00:00 at 5.00MiB/s",
        ]
        assert dropbot.extract_file_paths(lines) == []


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

    def test_un_audio_a_medio_convertir_no_se_rescata(self, temp, run_async):
        """Al cancelar mientras ffmpeg convierte, conviven el original y el .mp3 a
        medias (yt-dlp borra el original al terminar): ninguno es una canción
        terminada, y antes se rescataban los dos y contaban como dos vídeos."""
        for name in ("001-A_temp9.mp3", "002-B_temp9.webm", "002-B_temp9.mp3"):
            (temp.temp / name).write_bytes(b"x")

        moved = run_async(temp.bot.rescue_completed_playlist_files("_temp9", str(temp.audio)))

        assert moved == 1
        assert _ls(temp.audio) == ["001-A.mp3"]

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
