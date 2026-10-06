"""Descargas directas con wget, de punta a punta y sin red.

Un `wget` falso en el PATH escribe en la ruta de `-O`, dibuja la barra de
progreso en stderr con retornos de carro como el de verdad y sale con el código
que pida el test.
"""

import asyncio
import json
import os
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from button_data import button_data

URL = "https://cdn.example.com/files/peli.mkv"

_FAKE_HEADER = '''
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_WGET_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
target = args[args.index("-O") + 1]

def bar(percent, size="1.00M", speed="1.00MB/s", eta="eta 5s"):
    sys.stderr.write(f"peli.mkv  {percent}%[=====>     ]  {size}  {speed}    {eta}\\r")
    sys.stderr.flush()

def write(data=b"contenido"):
    with open(target, "wb") as handle:
        handle.write(data)

def ready():
    open(os.environ["FAKE_WGET_READY"], "w").close()
'''


@pytest.fixture
def fake_wget(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "wget-calls.jsonl"
    ready = tmp_path / "wget-ready"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_WGET_LOG", str(log))
    monkeypatch.setenv("FAKE_WGET_READY", str(ready))

    def install(body):
        script = bindir / "wget"
        script.write_text(f"#!{sys.executable}\n{_FAKE_HEADER}\n{textwrap.dedent(body)}")
        script.chmod(0o755)

    install.calls = lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    install.ready = ready
    return install


@pytest.fixture
def direct(quiet_bot, tmp_path, monkeypatch, config_dir):
    bot = quiet_bot
    temp = tmp_path / "temp"
    final = tmp_path / "video"
    temp.mkdir()
    final.mkdir()
    monkeypatch.setattr(bot, "TEMP_DIR", str(temp))

    async def file_info(path):
        return {"type": "video", "size_formatted": "9 B", "duration_formatted": None,
                "resolution": None, "codec_video": None, "codec_audio": None, "bitrate": None}

    monkeypatch.setattr(bot, "get_file_info", file_info)
    bot.active_tasks.clear()
    yield SimpleNamespace(bot=bot, temp=temp, final=final)
    bot.active_tasks.clear()


def _status():
    status = MagicMock(name="progreso")
    status.edit = AsyncMock()
    return status


def _run(direct, run_async, event, status=None, filename="peli.mkv"):
    run_async(direct.bot.run_direct_download(
        event, URL, filename, status, str(direct.final), direct.bot.VID_ICO, "video"))


def _ls(folder):
    return sorted(os.listdir(folder))


class TestExito:
    def test_el_fichero_acaba_en_su_carpeta_con_su_nombre(
        self, direct, fake_wget, make_event, run_async, sent_messages, texts
    ):
        fake_wget('''
            bar(50)
            write()
            bar(100, eta="in 1s")
            sys.stderr.write("\\n'peli.mkv' saved [9/9]\\n")
        ''')

        _run(direct, run_async, make_event(event_id=701), status=_status())

        assert _ls(direct.final) == ["peli.mkv"]
        assert (direct.final / "peli.mkv").read_bytes() == b"contenido"
        assert _ls(direct.temp) == []
        assert "Descarga completada" in texts() and "peli.mkv" in texts()
        assert sent_messages[0][0] == "delete", "primero se quita el progreso"
        assert 701 not in direct.bot.active_tasks

    def test_el_comando_de_wget(self, direct, fake_wget, make_event, run_async):
        fake_wget("write()")

        _run(direct, run_async, make_event(event_id=702))

        (args,) = fake_wget.calls()
        assert "--progress=bar:force" in args
        assert args[-1] == URL
        output = args[args.index("-O") + 1]
        assert os.path.dirname(output) == str(direct.temp)
        assert os.path.basename(output).startswith("peli.mkv_temp")

    def test_no_machaca_un_fichero_con_el_mismo_nombre(self, direct, fake_wget, make_event, run_async):
        (direct.final / "peli.mkv").write_bytes(b"el de antes")
        fake_wget("write()")

        _run(direct, run_async, make_event(event_id=703))

        assert _ls(direct.final) == ["peli (1).mkv", "peli.mkv"]
        assert (direct.final / "peli.mkv").read_bytes() == b"el de antes"

    def test_el_progreso_se_muestra_y_el_100_siempre(self, direct, fake_wget, make_event, run_async):
        fake_wget('''
            bar(10, size="100K", speed="2.5MB/s", eta="eta 1m 2s")
            bar(20)
            bar(30)
            write()
            bar(100, eta="in 3s")
        ''')
        status = _status()

        _run(direct, run_async, make_event(event_id=704), status=status)

        edits = [call.args[0] for call in status.edit.await_args_list]
        # La primera línea y el 100%; las intermedias caen dentro del intervalo
        assert len(edits) == 2
        assert "10%" in edits[0] and "2.5MB/s" in edits[0] and "peli.mkv" in edits[0]
        assert "100%" in edits[1]
        data = [button_data(b) for b in status.edit.await_args_list[0].kwargs["buttons"]]
        assert data == [b"cancel:704"]


class TestFallos:
    def test_un_error_de_wget_se_dice_y_no_deja_temporales(
        self, direct, fake_wget, make_event, run_async, texts
    ):
        fake_wget('''
            write(b"")
            sys.stderr.write("ERROR 404: Not Found.\\n")
            sys.exit(8)
        ''')

        _run(direct, run_async, make_event(event_id=711), status=_status())

        assert "La descarga desde la URL ha fallado" in texts()
        assert _ls(direct.final) == [] and _ls(direct.temp) == []
        assert 711 not in direct.bot.active_tasks

    def test_codigo_cero_sin_fichero_es_un_fallo(self, direct, fake_wget, make_event, run_async, texts):
        fake_wget("pass")

        _run(direct, run_async, make_event(event_id=712))

        assert "ha fallado" in texts()
        assert _ls(direct.final) == []

    def test_sin_wget_se_avisa_y_se_libera_la_tarea(
        self, direct, make_event, run_async, texts, sent_messages, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PATH", str(tmp_path / "vacio"))

        _run(direct, run_async, make_event(event_id=713), status=_status())

        assert "ha fallado" in texts()
        assert sent_messages[0][0] == "delete"
        assert 713 not in direct.bot.active_tasks
        assert _ls(direct.temp) == []


class TestCancelar:
    BODY = '''
        write(b"a medias")
        bar(5)
        ready()
        time.sleep(30)
    '''

    async def _wait_ready(self, fake_wget, bot, event_id):
        for _ in range(500):
            if fake_wget.ready.exists() and isinstance(bot.active_tasks.get(event_id), asyncio.subprocess.Process):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("el wget falso no llegó a arrancar")

    def test_el_boton_cancelar_para_wget_y_borra_el_temporal(
        self, direct, fake_wget, make_event, run_async, texts
    ):
        fake_wget(self.BODY)

        async def scenario():
            event = make_event(event_id=721)
            task = asyncio.create_task(direct.bot.run_direct_download(
                event, URL, "peli.mkv", _status(), str(direct.final), direct.bot.VID_ICO, "video"))
            direct.bot.active_tasks[721] = task
            await self._wait_ready(fake_wget, direct.bot, 721)
            await direct.bot.cancel_download(make_event(event_id=9721, data=b"cancel:721"))
            await asyncio.wait_for(task, timeout=10)

        run_async(scenario())

        assert "Descarga cancelada" in texts()
        assert "ha fallado" not in texts()
        assert _ls(direct.final) == [] and _ls(direct.temp) == []
        assert 721 not in direct.bot.active_tasks

    def test_cancelar_la_tarea_mata_wget(self, direct, fake_wget, make_event, run_async, texts):
        fake_wget(self.BODY)

        async def scenario():
            event = make_event(event_id=722)
            task = asyncio.create_task(direct.bot.run_direct_download(
                event, URL, "peli.mkv", None, str(direct.final), direct.bot.VID_ICO, "video"))
            await self._wait_ready(fake_wget, direct.bot, 722)
            proc = direct.bot.active_tasks[722]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return proc

        proc = run_async(scenario())

        assert proc.returncode is not None
        assert _ls(direct.temp) == []
        assert 722 not in direct.bot.active_tasks


class TestDesdeElMensaje:
    def test_un_enlace_directo_baja_de_punta_a_punta(
        self, direct, fake_wget, make_event, run_async, texts, monkeypatch
    ):
        monkeypatch.setitem(direct.bot.DOWNLOAD_PATHS, "video", str(direct.final))
        fake_wget("write()")
        event = make_event(event_id=731)
        event.raw_text = URL

        async def scenario():
            await direct.bot.handle_url_link(event)
            await direct.bot.active_tasks[731]

        run_async(scenario())

        assert _ls(direct.final) == ["peli.mkv"]
        assert "Descarga completada" in texts()
        assert 731 not in direct.bot.active_tasks
