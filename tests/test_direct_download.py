"""Descargas directas (wget): nombre del servidor y lectura del progreso."""

import asyncio
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("disposition, expected", [
    ('attachment; filename="../../etc/cron.d/x"', "x"),
    ('attachment; filename="/root/.ssh/authorized_keys"', "authorized_keys"),
    ('attachment; filename="..\\\\..\\\\win.ini"', "win.ini"),
    ('attachment; filename=".."', "archivo"),
    ('attachment; filename="Informe 2026.pdf"', "Informe 2026.pdf"),
])
def test_el_nombre_del_servidor_no_sale_de_la_carpeta(dropbot, run_async, monkeypatch, disposition, expected):
    def fake_head(url, **kwargs):
        return SimpleNamespace(headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": disposition,
        })

    monkeypatch.setattr(dropbot.requests, "head", fake_head)

    is_direct, filename, *_ = run_async(dropbot.is_direct_download_url("https://example.com/get?id=1"))

    assert is_direct
    assert filename == expected


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
