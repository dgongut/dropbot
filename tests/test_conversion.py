"""El stderr de ffmpeg se drena en paralelo para no bloquearlo.

Sin drenado, un ffmpeg que vuelca más de ~64 KiB a stderr y nadie lo lee se
queda bloqueado escribiendo para siempre: no falla, no avanza y el reintento
en software nunca salta (visto con un WebM que VAAPI no pudo decodificar).
"""

import asyncio
import sys


def test_un_ffmpeg_verboso_que_falla_no_bloquea(dropbot, run_async):
    async def scenario():
        code = (
            "import sys; "
            "sys.stderr.buffer.write(b'x' * (4 * 1024 * 1024)); "
            "sys.stderr.flush(); "
            "sys.exit(1)"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        chunks = []
        drain = asyncio.ensure_future(
            dropbot.accumulate_process_stderr(proc, chunks)
        )
        # Leer stdout hasta EOF, como hace el bucle de progreso
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
        await asyncio.wait_for(proc.wait(), timeout=30)
        await asyncio.wait_for(drain, timeout=30)
        assert proc.returncode == 1
        assert sum(len(chunk) for chunk in chunks) == 4 * 1024 * 1024

    run_async(scenario())
