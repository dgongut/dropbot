"""Descargas directas con wget, de punta a punta y sin red.

Un `wget` falso en el PATH escribe en la ruta de `-O`, dibuja la barra de
progreso en stderr con retornos de carro como el de verdad y sale con el código
que pida el test.
"""

import asyncio
import errno
import http.server
import ipaddress
import json
import os
import shutil
import socket
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urljoin

import pytest

import settings
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


def _run(direct, run_async, event, status=None, filename="peli.mkv", url=URL):
    run_async(direct.bot.run_direct_download(
        event, url, filename, status, str(direct.final), direct.bot.VID_ICO, "video"))


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
        assert "42_701" not in direct.bot.active_tasks

    def test_el_comando_de_wget(self, direct, fake_wget, make_event, run_async):
        fake_wget("write()")

        _run(direct, run_async, make_event(event_id=702))

        (args,) = fake_wget.calls()
        assert "--progress=bar:force" in args
        assert args[-1] == URL
        output = args[args.index("-O") + 1]
        assert os.path.dirname(output) == str(direct.temp)
        # El temporal no depende del nombre: uno largo no cabría con el sufijo
        assert os.path.basename(output).startswith("wget_42_702_")
        assert "peli" not in os.path.basename(output)

    def test_no_machaca_un_fichero_con_el_mismo_nombre(self, direct, fake_wget, make_event, run_async):
        (direct.final / "peli.mkv").write_bytes(b"el de antes")
        fake_wget("write()")

        _run(direct, run_async, make_event(event_id=703))

        assert _ls(direct.final) == ["peli (1).mkv", "peli.mkv"]
        assert (direct.final / "peli.mkv").read_bytes() == b"el de antes"

    def test_entre_dispositivos_el_nombre_final_no_existe_hasta_acabar_la_copia(
        self, direct, fake_wget, make_event, run_async, monkeypatch
    ):
        """Con el nombre reservado antes de copiar de TEMP_DIR al volumen, un
        SIGKILL a mitad dejaba un peli.mkv vacío que nadie borraba."""
        fake_wget("write(b'x' * 1000)")
        real_replace, real_copy = os.replace, shutil.copyfileobj
        while_copying = []

        def replace(old, new):
            # Como de la capa del contenedor a un volumen
            if os.path.dirname(old) == str(direct.temp):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(old, new)

        def copy(source, target, *args):
            real_copy(source, target, *args)
            while_copying.append(_ls(direct.final))

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(shutil, "copyfileobj", copy)

        _run(direct, run_async, make_event(event_id=708))

        (listing,) = while_copying
        assert listing and all(name.startswith(".") for name in listing), "mientras se copia solo hay ocultos"
        assert _ls(direct.final) == ["peli.mkv"]
        assert (direct.final / "peli.mkv").read_bytes() == b"x" * 1000
        assert _ls(direct.temp) == []

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
        assert data == [b"cancel:42_704"]

    def test_un_nombre_de_255_bytes_se_descarga(self, direct, fake_wget, make_event, run_async, texts):
        # El máximo que deja sanitize_filename: con el nombre en el temporal
        # más el sufijo pasaba del límite del sistema y wget no podía abrirlo
        name = "a" * 251 + ".mkv"
        fake_wget("write()")

        _run(direct, run_async, make_event(event_id=705), filename=name)

        assert _ls(direct.final) == [name]
        assert "ha fallado" not in texts()
        assert _ls(direct.temp) == []

    def test_un_nombre_larguisimo_del_servidor_sin_extension_se_descarga_tambien_repetido(
        self, direct, fake_wget, make_event, run_async, texts, monkeypatch
    ):
        """El nombre del Content-Disposition lo pone el servidor. Con «1. » y más
        de 255 bytes sin otro punto, splitext toma casi todo por extensión:
        sanitize_filename lo dejaba en más de 255 bytes y no se podía guardar,
        y uno de 255 justos fallaba la segunda vez, al añadirle el sufijo."""
        def fake_head(session, url, **kwargs):
            return SimpleNamespace(is_redirect=False, headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f'attachment; filename="1. {"a" * 300}"',
            })

        monkeypatch.setattr(direct.bot.requests.Session, "head", fake_head)
        _, name, *_ = run_async(direct.bot.is_direct_download_url("https://descargas.example/doc?id=7"))
        fake_wget("write()")

        _run(direct, run_async, make_event(event_id=714), filename=name)
        _run(direct, run_async, make_event(event_id=715), filename=name)

        saved = _ls(direct.final)
        assert len(saved) == 2 and all(len(saved_name.encode()) <= 255 for saved_name in saved)
        assert "ha fallado" not in texts()
        assert _ls(direct.temp) == []

    def test_el_porcentaje_no_sale_del_nombre_del_fichero(self, direct, fake_wget, make_event, run_async):
        # wget hace desfilar el nombre delante de la barra, y este lleva su
        # propio "100%" (de un %25 en la URL)
        fake_wget('''
            for percent in (6, 7, 8):
                sys.stderr.write(f"oferta_100%_real.mp4 {percent:3d}%[=>        ]  1.00M  1.00MB/s    eta 5s\\r")
            write()
        ''')
        status = _status()

        _run(direct, run_async, make_event(event_id=706), status=status)

        edits = [call.args[0] for call in status.edit.await_args_list]
        # Sin el 100% falso, las líneas siguientes caen dentro del intervalo
        assert len(edits) == 1
        assert "6%" in edits[0] and "100%" not in edits[0]

    def test_sin_tamaño_total_se_ve_lo_descargado_y_la_barra_no_va_al_log(
        self, direct, fake_wget, make_event, run_async, monkeypatch
    ):
        # Sin Content-Length, wget dibuja "[ <=> ]" sin porcentaje
        fake_wget('''
            sys.stderr.write("Length: unspecified [application/zip]\\n")
            for i in range(300):
                sys.stderr.write(f"peli.mkv               [ <=>        ] {i}.50K  1.24MB/s\\r")
            write()
            sys.stderr.write("\\n'peli.mkv' saved [9]\\n")
        ''')
        logged = []
        monkeypatch.setattr(direct.bot, "debug", logged.append)
        status = _status()

        _run(direct, run_async, make_event(event_id=707), status=status)

        edits = [call.args[0] for call in status.edit.await_args_list]
        assert len(edits) == 1
        assert "0.50K" in edits[0] and "1.24MB/s" in edits[0] and "peli.mkv" in edits[0]
        assert not [line for line in logged if "<=>" in line]
        assert any("Length: unspecified" in line for line in logged)
        assert _ls(direct.final) == ["peli.mkv"]

    def test_la_contrasena_y_la_firma_de_la_url_no_llegan_al_log(
        self, direct, fake_wget, make_event, run_async, monkeypatch
    ):
        """En Docker el log va entero a `docker logs`: el comando de wget y lo
        que imprime dejaban ahí el usuario:contraseña de un enlace al NAS y la
        firma de uno prefirmado."""
        url = "https://admin:MiClave@nas.local:5001/peli.mkv?X-Amz-Signature=firma123"
        fake_wget('''
            sys.stderr.write(f"--2026-10-22 10:00:00--  {args[-1]}\\n")
            write()
            bar(100, eta="in 1s")
        ''')
        logged = []
        for level in ("debug", "warning", "error"):
            monkeypatch.setattr(direct.bot, level, logged.append)

        _run(direct, run_async, make_event(event_id=709), url=url)

        assert fake_wget.calls()[0][-1] == url, "wget recibe la URL entera"
        log = "\n".join(logged)
        assert "nas.local:5001/peli.mkv" in log
        assert "MiClave" not in log and "firma123" not in log
        assert _ls(direct.final) == ["peli.mkv"]


class TestServidorLento:
    def test_wget_no_espera_horas_a_un_servidor_parado(self, direct, fake_wget, make_event, run_async):
        fake_wget("write()")

        _run(direct, run_async, make_event(event_id=741))

        (args,) = fake_wget.calls()
        assert {"--connect-timeout=30", "--read-timeout=60", "--tries=5", "--waitretry=5"} <= set(args)
        # --timeout fija también el de lectura y, según el orden, lo pisaría
        assert not [arg for arg in args if arg.startswith("--timeout")]

    def test_un_reintento_se_avisa(self, direct, fake_wget, make_event, run_async):
        fake_wget('''
            bar(40)
            sys.stderr.write("\\n2026-10-21 21:09:29 (146 KB/s) - Read error at byte 300000/1000000 "
                             "(Connection timed out). Retrying.\\n\\n")
            sys.stderr.write("--2026-10-21 21:09:30--  (try: 2)  https://cdn.example.com/files/peli.mkv\\n")
            write()
            bar(100, eta="in 3s")
        ''')
        status = _status()

        _run(direct, run_async, make_event(event_id=742), status=status)

        edits = [call.args[0] for call in status.edit.await_args_list]
        assert "40%" in edits[0]
        assert "Reintentando" in edits[1] and "intento 2 de 5" in edits[1]
        assert "100%" in edits[-1]
        assert _ls(direct.final) == ["peli.mkv"]

    def test_con_telegram_sin_responder_la_descarga_sigue(
        self, direct, fake_wget, make_event, run_async, monkeypatch
    ):
        """Telethon retiene la edición hasta reconectar (minutos) y, mientras,
        nadie leía la salida de wget: la barra sin tamaño total y el aviso de
        reintento esperaban la edición sin tope, la tubería se llenaba y wget
        se quedaba parado."""
        monkeypatch.setattr(direct.bot, "PROGRESS_EDIT_TIMEOUT", 0.1)
        fake_wget('''
            sys.stderr.write("Length: unspecified [application/zip]\\n")
            sys.stderr.write("peli.mkv               [ <=>        ]  1.20M  1.24MB/s\\r")
            sys.stderr.write("\\n--2026-10-21 21:09:30--  (try: 2)  https://cdn.example.com/files/peli.mkv\\n")
            write()
        ''')

        async def no_answer(*args, **kwargs):
            await asyncio.sleep(3600)

        status = MagicMock(name="progreso")
        status.edit = AsyncMock(side_effect=no_answer)

        run_async(asyncio.wait_for(direct.bot.run_direct_download(
            make_event(event_id=743), URL, "peli.mkv", status, str(direct.final),
            direct.bot.VID_ICO, "video"), 10))

        assert _ls(direct.final) == ["peli.mkv"]
        edits = [call.args[0] for call in status.edit.await_args_list]
        assert len(edits) == 2, "se sigue intentando, sin esperar a Telegram"
        assert "1.20M" in edits[0] and "Reintentando" in edits[1]


class TestTamañoMaximo:
    def test_si_el_servidor_anuncia_mas_del_maximo_no_se_descarga(
        self, direct, fake_wget, make_event, run_async, texts
    ):
        settings.put("urls.max_size_mb", 1024)
        fake_wget('''
            sys.stderr.write("Length: 42949672960 (40G) [video/x-matroska]\\n")
            sys.stderr.flush()
            write(b"a medias")
            time.sleep(10)
        ''')

        started = time.monotonic()
        _run(direct, run_async, make_event(event_id=751), status=_status())

        assert time.monotonic() - started < 5, "wget se para nada más ver el tamaño"
        assert "pasa del tamaño máximo de 1 GB" in texts()
        assert "ha fallado" not in texts()
        assert _ls(direct.final) == [] and _ls(direct.temp) == []
        assert "42_751" not in direct.bot.active_tasks

    def test_sin_tamaño_anunciado_se_para_al_pasar_del_maximo(
        self, direct, fake_wget, make_event, run_async, texts
    ):
        settings.put("urls.max_size_mb", 1)
        fake_wget('''
            write(b"x" * (2 * 1024 * 1024))
            sys.stderr.write("peli.mkv               [ <=>        ]   2.00M  1.00MB/s\\r")
            sys.stderr.flush()
            time.sleep(10)
        ''')

        started = time.monotonic()
        _run(direct, run_async, make_event(event_id=752), status=_status())

        assert time.monotonic() - started < 5
        assert "pasa del tamaño máximo de 1 MB" in texts()
        assert _ls(direct.final) == [] and _ls(direct.temp) == []

    def test_por_debajo_del_maximo_se_descarga(self, direct, fake_wget, make_event, run_async, texts):
        settings.put("urls.max_size_mb", 1)
        fake_wget('''
            sys.stderr.write("Length: 9 [video/x-matroska]\\n")
            write()
            bar(100, eta="in 1s")
        ''')

        _run(direct, run_async, make_event(event_id=753), status=_status())

        assert _ls(direct.final) == ["peli.mkv"]
        assert "tamaño máximo" not in texts()


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
        assert "42_711" not in direct.bot.active_tasks

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
        assert "42_713" not in direct.bot.active_tasks
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
            if fake_wget.ready.exists() and isinstance(bot.active_tasks.get(f"42_{event_id}"), asyncio.subprocess.Process):
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
            direct.bot.active_tasks["42_721"] = task
            await self._wait_ready(fake_wget, direct.bot, 721)
            await direct.bot.cancel_download(make_event(event_id=9721, data=b"cancel:42_721"))
            await asyncio.wait_for(task, timeout=10)

        run_async(scenario())

        assert "Descarga cancelada" in texts()
        assert "ha fallado" not in texts()
        assert _ls(direct.final) == [] and _ls(direct.temp) == []
        assert "42_721" not in direct.bot.active_tasks

    def test_el_cancelar_no_para_la_del_mismo_id_en_otro_chat(self, direct, fake_wget, make_event, run_async):
        """Cada supergrupo numera sus mensajes por su cuenta: con solo el id, la
        descarga de un grupo pisaba en active_tasks a la del mismo id en
        privado, y el Cancelar de esta paraba la del grupo."""
        fake_wget(self.BODY)
        keys = ("42_723", "-1001234567890_723")

        async def scenario():
            tasks = [asyncio.create_task(direct.bot.run_direct_download(
                make_event(event_id=723, chat_id=chat_id), URL, "peli.mkv", _status(),
                str(direct.final), direct.bot.VID_ICO, "video")) for chat_id in (42, -1001234567890)]
            for _ in range(500):
                if fake_wget.ready.exists() and all(
                        isinstance(direct.bot.active_tasks.get(key), asyncio.subprocess.Process) for key in keys):
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("los wget falsos no llegaron a arrancar")
            await direct.bot.cancel_download(make_event(event_id=9723, data=b"cancel:42_723"))
            await asyncio.wait_for(tasks[0], timeout=10)
            group_running = not tasks[1].done()
            tasks[1].cancel()
            await asyncio.wait([tasks[1]], timeout=10)
            return group_running

        assert run_async(scenario()), "la descarga del grupo sigue"
        assert not set(keys) & direct.bot.active_tasks.keys()
        assert _ls(direct.temp) == []

    def test_al_acabar_no_quita_la_tarea_que_la_ha_pisado(self, direct, fake_wget, make_event, run_async):
        """Otra descarga con la misma clave pisa la entrada: al acabar, la
        primera se llevaba la de la otra, que ya no se podía cancelar."""
        fake_wget('''
            ready()
            time.sleep(0.3)
            write()
        ''')
        other = object()

        async def scenario():
            task = asyncio.create_task(direct.bot.run_direct_download(
                make_event(event_id=724), URL, "peli.mkv", None, str(direct.final), direct.bot.VID_ICO, "video"))
            await self._wait_ready(fake_wget, direct.bot, 724)
            direct.bot.active_tasks["42_724"] = other
            await task

        run_async(scenario())

        assert direct.bot.active_tasks.pop("42_724") is other

    def test_cancelar_la_tarea_mata_wget(self, direct, fake_wget, make_event, run_async, texts):
        fake_wget(self.BODY)

        async def scenario():
            event = make_event(event_id=722)
            task = asyncio.create_task(direct.bot.run_direct_download(
                event, URL, "peli.mkv", None, str(direct.final), direct.bot.VID_ICO, "video"))
            await self._wait_ready(fake_wget, direct.bot, 722)
            proc = direct.bot.active_tasks["42_722"]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return proc

        proc = run_async(scenario())

        assert proc.returncode is not None
        assert _ls(direct.temp) == []
        assert "42_722" not in direct.bot.active_tasks


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
            await direct.bot.active_tasks["42_731"]

        run_async(scenario())

        assert _ls(direct.final) == ["peli.mkv"]
        assert "Descarga completada" in texts()
        assert "42_731" not in direct.bot.active_tasks


# --- redirecciones ------------------------------------------------------------

# Cada URL de FAKE_WGET_REDIRECTS responde con una redirección a su valor, que
# wget no sigue (--max-redirect=0); las demás, con el fichero
REDIRECTING_WGET = '''
    hops = json.loads(os.environ.get("FAKE_WGET_REDIRECTS", "{}"))
    url = args[-1]
    if url in hops:
        open(target, "wb").close()
        sys.stderr.write(f"Location: {hops[url]} [following]\\n0 redirections exceeded.\\n")
        sys.exit(8)
    write()
'''

PRIVATE_REDIRECT = "redirige a una dirección de la red local"


class TestRedirecciones:
    @pytest.fixture(autouse=True)
    def fake_dns(self, monkeypatch):
        """Resuelve los nombres de los tests sin red; las IP literales, tal cual."""
        table = {"cdn.example.com": "93.184.215.14", "acortador.example": "104.18.2.3",
                 "objects.example.net": "151.101.1.1", "nas.lan": "192.168.1.10",
                 "descargas.xn--bcher-kva.example": "151.101.1.2"}

        def getaddrinfo(host, port=None, *args, **kwargs):
            address = table.get(host, host)
            try:
                ipaddress.ip_address(address)
            except ValueError:
                raise socket.gaierror(f"{host} no existe")
            # Con el puerto, para que requests pueda conectar a un servidor local
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 0))]

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    def _head(self, dropbot, monkeypatch, hops, headers=None):
        """Cada URL de `hops` redirige a su valor; las demás responden `headers`."""
        requested = []
        headers = headers or {"Content-Disposition": 'attachment; filename="peli.bin"'}

        def fake_head(session, url, allow_redirects=False, **kwargs):
            # Como requests: con allow_redirects sigue los saltos por su cuenta
            while True:
                requested.append(url)
                if url not in hops:
                    return SimpleNamespace(is_redirect=False, headers=headers)
                if not allow_redirects:
                    return SimpleNamespace(is_redirect=True, headers={"Location": hops[url]})
                url = urljoin(url, hops[url])

        monkeypatch.setattr(dropbot.requests.Session, "head", fake_head)
        return requested

    @pytest.mark.parametrize("chain", [
        ["https://acortador.example/x", "http://192.168.1.1/cgi-bin/reboot.cgi"],
        ["https://acortador.example/x", "http://169.254.169.254/latest/meta-data/"],
        ["https://acortador.example/x", "http://127.0.0.1:4416/"],
        ["http://nas.lan/a", "https://cdn.example.com/b", "http://192.168.1.1/c"],
    ])
    def test_el_head_no_sigue_un_salto_de_lo_publico_a_la_red_local(self, dropbot, run_async, monkeypatch, chain):
        requested = self._head(dropbot, monkeypatch, dict(zip(chain, chain[1:])))

        with pytest.raises(dropbot.PrivateRedirectError):
            run_async(dropbot.is_direct_download_url(chain[0]))

        assert requested == chain[:-1]

    @pytest.mark.parametrize("location", [
        "http://192%2E168%2E1%2E1/cgi-bin/reboot.cgi",
        "http://%31%39%32.168.1.1/cgi-bin/reboot.cgi",
        "http://192%252E168%252E1%252E1/cgi-bin/reboot.cgi",
        "http://no-existe.example/peli.bin",
    ])
    def test_el_head_no_sigue_un_salto_publico_a_un_host_que_no_se_puede_comprobar(
        self, dropbot, run_async, monkeypatch, location
    ):
        """getaddrinfo no decodifica los %XX del host y requests sí: el salto no
        resolvía, pasaba por público y el HEAD llegaba al router. Lo que no
        resuelve tampoco se sigue, que no se sabe adónde iría."""
        requested = self._head(dropbot, monkeypatch, {"https://acortador.example/x": location})

        with pytest.raises(dropbot.PrivateRedirectError):
            run_async(dropbot.is_direct_download_url("https://acortador.example/x"))

        assert requested == ["https://acortador.example/x"]

    @pytest.mark.parametrize("location", [
        "https://descargas.bücher.example/peli.bin",
        "https://descargas.b%C3%BCcher.example/peli.bin",
    ])
    def test_el_head_sigue_una_redireccion_a_un_dominio_con_tildes(self, dropbot, run_async, monkeypatch, location):
        """Se pide en ASCII, el mismo nombre que se ha comprobado."""
        requested = self._head(dropbot, monkeypatch, {"https://acortador.example/x": location})

        is_direct, filename, *_ = run_async(dropbot.is_direct_download_url("https://acortador.example/x"))

        assert is_direct and filename == "peli.bin"
        assert requested == ["https://acortador.example/x", "https://descargas.xn--bcher-kva.example/peli.bin"]

    @pytest.mark.parametrize("target, normalized", [
        ("http://192%2E168%2E1%2E10/a", "http://192.168.1.10/a"),
        ("http://[fd00::5]:8080/a?b=1", "http://[fd00::5]:8080/a?b=1"),
        ("http://yo:clave@NAS.lan./a", "http://yo:clave@nas.lan/a"),
    ])
    def test_el_salto_se_pide_con_el_host_que_se_ha_comprobado(self, dropbot, target, normalized):
        assert dropbot.checked_redirect(True, target) == (normalized, True)

    def test_el_head_sigue_las_redirecciones_dentro_de_la_lan_del_admin(self, dropbot, run_async, monkeypatch):
        requested = self._head(dropbot, monkeypatch, {"http://nas.lan/descarga": "/files/peli.bin"})

        is_direct, filename, *_ = run_async(dropbot.is_direct_download_url("http://nas.lan/descarga"))

        assert is_direct and filename == "peli.bin"
        assert requested == ["http://nas.lan/descarga", "http://nas.lan/files/peli.bin"]

    @pytest.mark.parametrize("location, followed", [
        # El UTF-8 en bruto de "Canción", que http.client lee como latin-1
        ("/files/CanciÃ³n.pdf", "http://nas.lan/files/Canción.pdf"),
        # Un latin-1 de verdad no es UTF-8 válido y se queda igual
        ("/files/Canci\xf3n.pdf", "http://nas.lan/files/Canci\xf3n.pdf"),
    ])
    def test_el_head_sigue_un_location_con_tildes_sin_codificar(
        self, dropbot, run_async, monkeypatch, location, followed
    ):
        """Siguiendo los saltos a mano se pedía "Canci%C3%83%C2%B3n.pdf", el
        servidor daba 404 y el enlace acababa en yt-dlp."""
        requested = self._head(dropbot, monkeypatch, {"http://nas.lan/get?id=3": location},
                               headers={"Content-Disposition": 'attachment; filename="Cancion.pdf"'})

        is_direct, filename, *_ = run_async(dropbot.is_direct_download_url("http://nas.lan/get?id=3"))

        assert is_direct and filename == "Cancion.pdf"
        assert requested == ["http://nas.lan/get?id=3", followed]

    def test_el_head_sigue_las_redirecciones_publicas(self, dropbot, run_async, monkeypatch):
        requested = self._head(dropbot, monkeypatch, {"https://acortador.example/x": "https://cdn.example.com/get?id=1"})

        is_direct, filename, *_ = run_async(dropbot.is_direct_download_url("https://acortador.example/x"))

        assert is_direct and filename == "peli.bin"
        assert requested == ["https://acortador.example/x", "https://cdn.example.com/get?id=1"]

    def test_una_pagina_html_que_redirige_a_la_red_local_no_llega_a_ytdlp(self, dropbot, run_async, monkeypatch):
        """A un enlace acabado en .html no se le hacía el HEAD: le bastaba la
        extensión para que yt-dlp siguiera su redirección al router."""
        page = "https://acortador.example/v.html"
        requested = self._head(dropbot, monkeypatch, {page: "http://192.168.1.1/cgi-bin/reboot.cgi"})

        with pytest.raises(dropbot.PrivateRedirectError):
            run_async(dropbot.is_direct_download_url(page))

        assert requested == [page]

    def test_una_pagina_html_la_sigue_analizando_ytdlp(self, dropbot, run_async, monkeypatch):
        """El HEAD solo comprueba las redirecciones: aunque conteste como una
        descarga, una página la tiene que analizar yt-dlp."""
        page = "https://acortador.example/noticia.html"
        requested = self._head(dropbot, monkeypatch, {page: "https://cdn.example.com/noticia.html"})

        assert run_async(dropbot.is_direct_download_url(page)) == (False, None, None, None, None)
        assert requested == [page, "https://cdn.example.com/noticia.html"]

    @pytest.mark.parametrize("slow", [True, False])
    def test_si_el_head_no_contesta_sigue_hacia_ytdlp_y_queda_en_el_log(
        self, dropbot, run_async, monkeypatch, slow
    ):
        """Un servidor lento, o que no acepta HEAD, no quiere decir que el
        enlace no valga. Un except desnudo se lo tragaba sin dejar rastro; al
        log va sin el texto del error, que repite la URL con su firma."""
        monkeypatch.setattr(dropbot, "HEAD_TIMEOUT", 0.1)
        logged = []
        monkeypatch.setattr(dropbot, "debug", logged.append)

        def fake_head(session, url, **kwargs):
            if slow:
                time.sleep(0.3)
            raise dropbot.requests.ConnectionError("Max retries exceeded with url: /ver?firma=secreta")

        monkeypatch.setattr(dropbot.requests.Session, "head", fake_head)

        result = run_async(dropbot.is_direct_download_url("https://cdn.example.com/ver?firma=secreta"))

        assert result == (False, None, None, None, None)
        log = "\n".join(logged)
        assert "No answer to HEAD" in log and "cdn.example.com/ver" in log
        assert "secreta" not in log

    def test_un_enlace_que_redirige_a_la_red_local_no_llega_a_ytdlp(
        self, direct, make_event, run_async, texts, monkeypatch
    ):
        # Sin Content-Disposition: lo siguiente sería preguntarle a yt-dlp
        requested = self._head(direct.bot, monkeypatch,
                               {"https://acortador.example/video": "http://192.168.1.1/cgi-bin/reboot.cgi"},
                               headers={"Content-Type": "text/html"})

        async def forbidden(url):
            raise AssertionError("yt-dlp seguiría la misma redirección")

        monkeypatch.setattr(direct.bot, "detect_playlist", forbidden)
        monkeypatch.setattr(direct.bot, "detect_content_type", forbidden)
        event = make_event(event_id=771)
        event.raw_text = "https://acortador.example/video"

        run_async(direct.bot.handle_url_link(event))

        assert requested == ["https://acortador.example/video"]
        assert PRIVATE_REDIRECT in texts()
        assert f"{event.chat_id}_{event.id}" not in direct.bot.pending_urls

    def test_wget_no_sigue_una_redireccion_a_la_red_local(
        self, direct, fake_wget, make_event, run_async, texts, monkeypatch
    ):
        monkeypatch.setenv("FAKE_WGET_REDIRECTS", json.dumps({URL: "http://192.168.1.1/cgi-bin/reboot.cgi"}))
        fake_wget(REDIRECTING_WGET)

        _run(direct, run_async, make_event(event_id=772), status=_status())

        calls = fake_wget.calls()
        assert [args[-1] for args in calls] == [URL]
        assert "--max-redirect=0" in calls[0]
        assert PRIVATE_REDIRECT in texts()
        assert _ls(direct.final) == [] and _ls(direct.temp) == []
        assert "42_772" not in direct.bot.active_tasks

    @pytest.mark.parametrize("location", [
        "http://192%2E168%2E1%2E1/cgi-bin/reboot.cgi",
        "http://no-existe.example/peli.mkv",
    ])
    def test_wget_no_sigue_un_salto_a_un_host_que_no_se_puede_comprobar(
        self, direct, fake_wget, make_event, run_async, texts, monkeypatch, location
    ):
        monkeypatch.setenv("FAKE_WGET_REDIRECTS", json.dumps({URL: location}))
        fake_wget(REDIRECTING_WGET)

        _run(direct, run_async, make_event(event_id=776), status=_status())

        assert [args[-1] for args in fake_wget.calls()] == [URL]
        assert PRIVATE_REDIRECT in texts()
        assert _ls(direct.final) == [] and _ls(direct.temp) == []

    @pytest.mark.skipif(shutil.which("wget") is None, reason="necesita el binario wget")
    def test_wget_de_verdad_no_llega_a_la_lan_con_el_host_en_porcentajes(
        self, direct, make_event, run_async, texts, monkeypatch
    ):
        """wget decodifica los %XX del host antes de resolverlo: el salto a
        127%2E0%2E0%2E2 llegaba a ese equipo y el bot decía «Descarga
        completada». Aquí 127.0.0.1 hace de servidor público del atacante."""
        lan_hits = []

        class Lan(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                lan_hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        try:
            lan = http.server.ThreadingHTTPServer(("127.0.0.2", 0), Lan)
        except OSError:
            pytest.skip("127.0.0.2 no es local en este sistema")
        location = f"http://127%2E0%2E0%2E2:{lan.server_address[1]}/cgi-bin/reboot.cgi"

        class Attacker(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        attacker = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Attacker)
        for server in (lan, attacker):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        resolve = socket.getaddrinfo

        def getaddrinfo(host, *args, **kwargs):
            if host == "127.0.0.1":
                return resolve("93.184.215.14", *args, **kwargs)
            return resolve(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        for proxy in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            monkeypatch.delenv(proxy, raising=False)
        try:
            _run(direct, run_async, make_event(event_id=777), status=_status(),
                 url=f"http://127.0.0.1:{attacker.server_address[1]}/peli.mkv")
        finally:
            for server in (lan, attacker):
                server.shutdown()
                server.server_close()

        assert lan_hits == [], "la petición ha llegado a la red local"
        assert PRIVATE_REDIRECT in texts()
        assert _ls(direct.final) == []

    @pytest.fixture
    def cookie_server(self, monkeypatch):
        """Un gestor de descargas que marca la sesión con una cookie al
        redirigir al fichero, y que sin ella no lo da. Devuelve su URL y las
        peticiones que recibe."""
        received = []

        class Server(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, body):
                cookie = self.headers.get("Cookie", "")
                received.append((self.command, self.path, cookie))
                if self.path.startswith("/get/"):
                    self.send_response(302)
                    self.send_header("Set-Cookie", "dl=ok; Path=/")
                    self.send_header("Location", "/dl/" + self.path.removeprefix("/get/"))
                    self.send_header("Content-Length", "0")
                elif "dl=ok" not in cookie:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                else:
                    self.send_response(200)
                    self.send_header("Content-Disposition", 'attachment; filename="libro.epub"')
                    self.send_header("Content-Length", "9")
                self.end_headers()
                if body and self.path.startswith("/dl/") and "dl=ok" in cookie:
                    self.wfile.write(b"contenido")

            def do_GET(self):
                self._answer(True)

            def do_HEAD(self):
                self._answer(False)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for proxy in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            monkeypatch.delenv(proxy, raising=False)
        yield f"http://127.0.0.1:{server.server_address[1]}", received
        server.shutdown()
        server.server_close()

    @pytest.mark.skipif(shutil.which("wget") is None, reason="necesita el binario wget")
    def test_wget_de_verdad_lleva_la_cookie_de_la_redireccion_al_salto_siguiente(
        self, direct, make_event, run_async, texts, cookie_server
    ):
        """Con un wget por salto, la cookie que ponía la redirección se perdía:
        el fichero contestaba 403 y lo que bajaba la 4.0.0 fallaba."""
        base, received = cookie_server

        _run(direct, run_async, make_event(event_id=778), status=_status(),
             filename="libro.epub", url=f"{base}/get/libro.epub")

        assert ("GET", "/dl/libro.epub", "dl=ok") in received
        assert _ls(direct.final) == ["libro.epub"]
        assert (direct.final / "libro.epub").read_bytes() == b"contenido"
        assert _ls(direct.temp) == [], "ni el temporal ni las cookies se quedan"
        assert "Descarga completada" in texts()

    def test_el_head_lleva_la_cookie_de_la_redireccion_al_salto_siguiente(
        self, dropbot, run_async, cookie_server
    ):
        """Sin la cookie, el HEAD acababa en 403 y el enlace no se reconocía
        como descarga directa."""
        base, received = cookie_server

        is_direct, filename, *_ = run_async(dropbot.is_direct_download_url(f"{base}/get/libro"))

        assert is_direct and filename == "libro.epub"
        assert received[-1] == ("HEAD", "/dl/libro", "dl=ok")

    def test_wget_sigue_las_redirecciones_publicas(self, direct, fake_wget, make_event, run_async, monkeypatch):
        signed = "https://objects.example.net/peli.mkv?sig=1"
        monkeypatch.setenv("FAKE_WGET_REDIRECTS", json.dumps({URL: signed}))
        fake_wget(REDIRECTING_WGET)

        _run(direct, run_async, make_event(event_id=773))

        assert [args[-1] for args in fake_wget.calls()] == [URL, signed]
        assert _ls(direct.final) == ["peli.mkv"]
        assert _ls(direct.temp) == []

    def test_wget_sigue_una_redireccion_con_espacios_sin_codificar(
        self, direct, fake_wget, make_event, run_async, monkeypatch
    ):
        """wget escapa lo que no es ASCII pero deja los espacios: el Location
        no se reconocía y la descarga fallaba con «0 redirections exceeded»."""
        monkeypatch.setenv("FAKE_WGET_REDIRECTS", json.dumps({URL: "/files/Mi peli.mkv"}))
        fake_wget(REDIRECTING_WGET)

        _run(direct, run_async, make_event(event_id=780))

        assert [args[-1] for args in fake_wget.calls()] == [URL, "https://cdn.example.com/files/Mi%20peli.mkv"]
        assert _ls(direct.final) == ["peli.mkv"]

    @pytest.mark.parametrize("location", [
        "/cdn/peli.mkv?token=SECRETO&expires=1",
        "cdn/peli.mkv?token=SECRETO",
        "//cdn.example.com/cdn/peli.mkv?token=SECRETO",
        "https://objects.example.net/peli.mkv?token=SECRETO",
    ])
    def test_la_firma_de_una_redireccion_no_llega_al_log(
        self, direct, fake_wget, make_event, run_async, monkeypatch, location
    ):
        """wget imprime la cabecera Location tal cual: si era relativa, el
        token de descarga iba entero a `docker logs`."""
        monkeypatch.setenv("FAKE_WGET_REDIRECTS", json.dumps({URL: location}))
        fake_wget(REDIRECTING_WGET)
        logged = []
        for level in ("debug", "warning", "error"):
            monkeypatch.setattr(direct.bot, level, logged.append)

        _run(direct, run_async, make_event(event_id=779))

        assert fake_wget.calls()[-1][-1] == urljoin(URL, location), "wget recibe la URL entera"
        assert any("Location:" in line and "token=***" in line for line in logged)
        assert not [line for line in logged if "SECRETO" in line]
        assert _ls(direct.final) == ["peli.mkv"]

    def test_wget_sigue_una_redireccion_relativa_dentro_de_la_lan_del_admin(
        self, direct, fake_wget, make_event, run_async, monkeypatch
    ):
        lan = "http://nas.lan/compartido/peli.mkv"
        monkeypatch.setenv("FAKE_WGET_REDIRECTS", json.dumps({lan: "/descargas/peli.mkv"}))
        fake_wget(REDIRECTING_WGET)

        _run(direct, run_async, make_event(event_id=774), url=lan)

        assert [args[-1] for args in fake_wget.calls()] == [lan, "http://nas.lan/descargas/peli.mkv"]
        assert _ls(direct.final) == ["peli.mkv"]

    def test_un_bucle_de_redirecciones_acaba_en_error(
        self, direct, fake_wget, make_event, run_async, texts, monkeypatch
    ):
        monkeypatch.setenv("FAKE_WGET_REDIRECTS", json.dumps({URL: URL}))
        fake_wget(REDIRECTING_WGET)

        _run(direct, run_async, make_event(event_id=775), status=_status())

        assert len(fake_wget.calls()) == direct.bot.MAX_REDIRECTS + 1
        assert "ha fallado" in texts()
        assert _ls(direct.final) == [] and _ls(direct.temp) == []
