"""Descargas directas (wget): nombre del servidor y lectura del progreso."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("disposition, expected", [
    ('attachment; filename="../../etc/cron.d/x"', "x"),
    ('attachment; filename="/root/.ssh/authorized_keys"', "authorized_keys"),
    ('attachment; filename="..\\\\..\\\\win.ini"', "win.ini"),
    ('attachment; filename=".."', "archivo"),
    ('attachment; filename="Informe 2026.pdf"', "Informe 2026.pdf"),
])
def test_el_nombre_del_servidor_no_sale_de_la_carpeta(dropbot, run_async, monkeypatch, disposition, expected):
    def fake_head(session, url, **kwargs):
        return SimpleNamespace(is_redirect=False, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": disposition,
        })

    monkeypatch.setattr(dropbot.requests.Session, "head", fake_head)

    is_direct, filename, *_ = run_async(dropbot.is_direct_download_url("https://example.com/get?id=1"))

    assert is_direct
    assert filename == expected


@pytest.mark.parametrize("disposition, expected", [
    # U+202E: se veía como "Factura_2026exe.pdf" y era un .exe
    ("attachment; filename*=UTF-8''Factura_2026%E2%80%AEfdp.exe", "Factura_2026_fdp.exe"),
    # Con punto delante quedaba oculto y fuera de /list y /manage
    ('attachment; filename=".pelicula.mp4"', "pelicula.mp4"),
    ("attachment; filename*=UTF-8''%2Emp4", "archivo.mp4"),
])
def test_el_nombre_del_servidor_no_se_disfraza_ni_se_oculta(dropbot, run_async, monkeypatch, disposition, expected):
    def fake_head(session, url, **kwargs):
        return SimpleNamespace(is_redirect=False, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": disposition,
        })

    monkeypatch.setattr(dropbot.requests.Session, "head", fake_head)

    is_direct, filename, *_ = run_async(dropbot.is_direct_download_url("https://descargas.example/doc?id=7"))

    assert is_direct
    assert filename == expected


@pytest.mark.parametrize("raw, expected", [
    # El UTF-8 en bruto llega a requests leído como latin-1 ("CanciÃ³n")
    ("Canción – final.pdf".encode("utf-8").decode("latin-1"), "Canción – final.pdf"),
    # Un latin-1 de verdad no es UTF-8 válido: se queda como está
    ("Canción.pdf", "Canción.pdf"),
    ("informe.pdf", "informe.pdf"),
])
def test_un_nombre_con_utf8_en_bruto_no_se_guarda_con_mojibake(dropbot, raw, expected):
    assert dropbot.content_disposition_filename(f'attachment; filename="{raw}"') == expected


def test_la_barra_de_progreso_no_pasa_del_100(dropbot, run_async, make_event):
    status = SimpleNamespace(edit=AsyncMock())
    progress = {"percent": "150", "size": "1.00M", "speed": "1.00MB/s", "eta": "5s"}

    run_async(dropbot.update_progress_message(status, progress, make_event(event_id=1), "peli.mkv"))

    (call,) = status.edit.await_args_list
    bar = next(line for line in call.args[0].splitlines() if "█" in line)
    assert bar.strip("`") == "█" * 20


def test_la_barra_de_wget_con_retornos_de_carro_no_rompe_la_lectura(dropbot, run_async):
    """wget reescribe la barra con \\r: 200 KB sin un solo \\n."""
    async def scenario():
        code = (
            "import sys\n"
            "for i in range(2500):\n"
            "    sys.stderr.write(f'x  {i % 100}%[===>   ]  1.0M  1MB/s  eta 1s\\r')\n"
            "sys.stderr.write('saved\\n')\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        lines = [line async for line in dropbot.iter_progress_lines(proc.stderr)]
        await asyncio.wait_for(proc.wait(), timeout=30)
        return lines

    lines = run_async(scenario())
    assert len(lines) == 2501
    assert lines[1].startswith("x  1%")
    assert lines[-1] == "saved"
