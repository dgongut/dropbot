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


# --- Cancelar / enviar original ----------------------------------------------
# El id de conversión salía de un hash de la ruta y las marcas no se borraban:
# un toque tardío en "cancelar" dejaba la marca puesta y la siguiente
# conversión de ese fichero se cortaba sin parar ffmpeg, que se quedaba
# bloqueado escribiendo progreso en una tubería que ya nadie leía.

FAKE_FFMPEG = (
    "import sys, time\n"
    "while True:\n"
    "    sys.stdout.write('out_time_ms=1000000\\n' * 50)\n"
    "    sys.stdout.flush()\n"
    "    time.sleep(0.01)\n"
)


def _fake_conversion(dropbot, monkeypatch):
    async def metadata(path):
        return 10, 640, 480

    monkeypatch.setattr(dropbot, "get_video_metadata", metadata)
    monkeypatch.setattr(
        dropbot, "build_ffmpeg_conversion_command",
        lambda *args, **kwargs: [sys.executable, "-c", FAKE_FFMPEG, "-c:v", "fake"],
    )


def test_cancelar_corta_la_conversion_y_mata_ffmpeg(quiet_bot, media_file, run_async, monkeypatch):
    dropbot = quiet_bot
    _fake_conversion(dropbot, monkeypatch)

    async def scenario():
        task = asyncio.ensure_future(
            dropbot.convert_video_to_telegram_compatible(media_file("v.mkv"))
        )
        for _ in range(200):
            ids = [k for k in dropbot.active_tasks if str(k).startswith("conv_")]
            if ids:
                break
            await asyncio.sleep(0.01)
        conversion_id = ids[0]
        proc = dropbot.active_tasks[conversion_id]
        # Solo la marca, sin terminar el proceso: el bucle tiene que pararlo
        dropbot.cancelled_conversions.add(conversion_id)
        result = await asyncio.wait_for(task, timeout=10)
        return result, proc, conversion_id

    result, proc, conversion_id = run_async(scenario())
    assert result is None
    assert proc.returncode is not None, "ffmpeg no puede quedarse vivo"
    assert conversion_id not in dropbot.cancelled_conversions
    assert conversion_id not in dropbot.active_tasks


def test_cada_conversion_tiene_su_propio_id(dropbot):
    first = next(dropbot._conversion_ids)
    second = next(dropbot._conversion_ids)
    assert first != second


def test_cancelar_una_conversion_terminada_no_deja_marca(quiet_bot, make_event, run_async):
    dropbot = quiet_bot
    event = make_event(groups=(b"conv_inexistente",))

    run_async(dropbot.handle_cancel_conversion(event))
    run_async(dropbot.handle_send_original(event))

    assert "conv_inexistente" not in dropbot.cancelled_conversions
    assert "conv_inexistente" not in dropbot.send_original_requests
