"""El stderr de ffmpeg se drena en paralelo para no bloquearlo.

Sin drenado, un ffmpeg que vuelca más de ~64 KiB a stderr y nadie lo lee se
queda bloqueado escribiendo para siempre: no falla, no avanza y el reintento
en software nunca salta (visto con un WebM que VAAPI no pudo decodificar).
"""

import ast
import asyncio
import contextlib
import inspect
import itertools
import sys

import pytest

from proc_helpers import wait_until


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
        # Se ha leído todo, pero solo se guardan el principio y el final
        kept = dropbot.FFMPEG_STDERR_HEAD_BYTES + dropbot.FFMPEG_STDERR_TAIL_BYTES
        skipped = 4 * 1024 * 1024 - kept
        assert chunks[1] == f"\n[... {skipped} bytes skipped ...]\n".encode()
        assert len(chunks[0]) + len(chunks[2]) == kept

    run_async(scenario())


def _drain(dropbot, run_async, data):
    """Lo que queda guardado del stderr de un proceso que escribe `data`."""
    async def scenario():
        # Lo que entra por stdin sale por stderr
        code = "import sys; sys.stderr.buffer.write(sys.stdin.buffer.read())"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        chunks = []
        drain = asyncio.ensure_future(dropbot.accumulate_process_stderr(proc, chunks))
        proc.stdin.write(data)
        await proc.stdin.drain()
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), timeout=30)
        await asyncio.wait_for(drain, timeout=30)
        return b"".join(chunks)

    return run_async(scenario())


def test_un_stderr_enorme_no_se_guarda_entero(dropbot, run_async):
    """Un vídeo dañado escribe varias líneas de error por fotograma: guardado
    entero eran cientos de MB en memoria y luego en el log."""
    data = b"inicio\n" + b"error while decoding MB\n" * 200_000 + b"fin\n"

    kept = _drain(dropbot, run_async, data)

    assert kept.startswith(b"inicio\n") and kept.endswith(b"fin\n")
    assert len(kept) < dropbot.FFMPEG_STDERR_HEAD_BYTES + dropbot.FFMPEG_STDERR_TAIL_BYTES + 100
    skipped = len(data) - dropbot.FFMPEG_STDERR_HEAD_BYTES - dropbot.FFMPEG_STDERR_TAIL_BYTES
    assert f"[... {skipped} bytes skipped ...]".encode() in kept


def test_un_stderr_corto_se_guarda_entero(dropbot, run_async):
    data = b"Error opening output file\n" * 100
    assert _drain(dropbot, run_async, data) == data


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

    # Sin plan, como con el fichero de ceros, pero sin lanzar el ffprobe de la
    # máquina: en la CI tardaba en arrancar más que lo que se esperaba aquí
    async def probe(path):
        return None

    monkeypatch.setattr(dropbot, "get_video_metadata", metadata)
    monkeypatch.setattr(dropbot, "probe_media", probe)
    monkeypatch.setattr(
        dropbot, "build_ffmpeg_conversion_command",
        lambda *args, **kwargs: [sys.executable, "-c", FAKE_FFMPEG, "-c:v", "fake"],
    )


async def _started_conversion(dropbot):
    """El id de la conversión en marcha, en cuanto ha lanzado ffmpeg."""
    ids = await wait_until(
        lambda: [k for k in dropbot.active_tasks if str(k).startswith("conv_")],
        message="la conversión no ha llegado a lanzar ffmpeg",
    )
    return ids[0]


async def _until_done(task, proc, timeout=30):
    """El resultado de la conversión, o falla (sin colgarse) si no acaba.

    Sin asyncio.wait_for(task): al vencer cancela la tarea y espera a que
    acabe, y una conversión que no ha parado ffmpeg está en proc.wait(), que
    no vuelve. El test se quedaba colgado en vez de fallar.
    """
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        # Ni cancelándola acabaría: espera a ffmpeg, y asyncio no ve que ha
        # muerto hasta que alguien lee lo que dejó en la tubería. Si el bot
        # sigue leyéndola (RuntimeError), ya le llega el final él solo
        proc.kill()
        with contextlib.suppress(RuntimeError, TimeoutError):
            await asyncio.wait_for(proc.stdout.read(), 5)
        await asyncio.wait({task}, timeout=5)
        pytest.fail("la conversión no ha terminado: ffmpeg sigue vivo o nadie lo para")
    return task.result()


def test_cancelar_corta_la_conversion_y_mata_ffmpeg(quiet_bot, media_file, run_async, monkeypatch):
    dropbot = quiet_bot
    _fake_conversion(dropbot, monkeypatch)

    async def scenario():
        task = asyncio.ensure_future(
            dropbot.convert_video_to_telegram_compatible(media_file("v.mkv"))
        )
        conversion_id = await _started_conversion(dropbot)
        proc = dropbot.active_tasks[conversion_id]
        # Solo la marca, sin terminar el proceso: el bucle tiene que pararlo
        dropbot.cancelled_conversions.add(conversion_id)
        result = await _until_done(task, proc)
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


def _new_run_prefix(dropbot):
    """El prefijo de los ids de conversión que tendría un arranque nuevo.

    Evalúa la expresión con la que dropbot.py inicializa `_conversion_run`, y
    no una copia suya: si se fijara, daría el mismo en todos los arranques.
    """
    initializer = next(
        node.value for node in ast.parse(inspect.getsource(dropbot)).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_conversion_run" for target in node.targets)
    )
    return eval(compile(ast.Expression(initializer), "dropbot.py", "eval"), vars(dropbot))


def test_el_prefijo_de_las_conversiones_cambia_en_cada_arranque(dropbot):
    assert _new_run_prefix(dropbot) != _new_run_prefix(dropbot)


@pytest.mark.parametrize("handler", ["handle_cancel_conversion", "handle_send_original"])
def test_un_boton_de_antes_de_reiniciar_no_corta_la_conversion_de_ahora(
    quiet_bot, media_file, make_event, run_async, monkeypatch, handler
):
    """El contador vuelve a 1 en cada arranque y los botones de una conversión
    que se cortó al reiniciar siguen en el chat: los de la primera conversión
    del arranque anterior cortaban la primera del nuevo."""
    dropbot = quiet_bot
    _fake_conversion(dropbot, monkeypatch)
    # El botón que dejó en el chat la primera conversión del arranque anterior
    old_button = f"conv_{_new_run_prefix(dropbot)}_1"
    # Recién arrancado otra vez: es la primera conversión de este proceso
    monkeypatch.setattr(dropbot, "_conversion_run", _new_run_prefix(dropbot))
    monkeypatch.setattr(dropbot, "_conversion_ids", itertools.count(1))

    async def scenario():
        task = asyncio.ensure_future(
            dropbot.convert_video_to_telegram_compatible(media_file("v.mkv"))
        )
        conversion_id = await _started_conversion(dropbot)
        proc = dropbot.active_tasks[conversion_id]

        await getattr(dropbot, handler)(make_event(groups=(old_button.encode(),)))
        await asyncio.sleep(0.1)
        untouched = (
            proc.returncode is None
            and conversion_id not in dropbot.cancelled_conversions
            and conversion_id not in dropbot.send_original_requests
        )

        dropbot.cancelled_conversions.add(conversion_id)
        await _until_done(task, proc)
        return conversion_id, untouched

    conversion_id, untouched = run_async(scenario())
    assert conversion_id.endswith("_1"), "es la primera conversión del arranque"
    assert untouched, "el botón viejo ha cortado la conversión nueva"


def test_cancelar_una_conversion_terminada_no_deja_marca(quiet_bot, make_event, run_async):
    dropbot = quiet_bot
    event = make_event(groups=(b"conv_inexistente",))

    run_async(dropbot.handle_cancel_conversion(event))
    run_async(dropbot.handle_send_original(event))

    assert "conv_inexistente" not in dropbot.cancelled_conversions
    assert "conv_inexistente" not in dropbot.send_original_requests
