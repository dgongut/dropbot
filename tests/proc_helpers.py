"""Lo común de los tests que esperan a algo de fuera del bucle de asyncio:
procesos que lanzan (yt-dlp falsos y lo que estos lanzan) y trabajo en hilos."""

import asyncio
import os
import shutil
import time

# El de verdad, aunque un test sustituya asyncio.sleep para que el bot no espere
_sleep = asyncio.sleep


async def wait_until(condition, timeout=15, message="no se ha cumplido a tiempo"):
    """Espera a que `condition()` dé algo cierto y lo devuelve.

    Para lo que tiene que pasar sí o sí: un proceso que arranca, un fichero
    que se abre en un hilo. Un número fijo de vueltas del bucle, o un plazo de
    un par de segundos, dependía de la velocidad de la máquina, y en la CI
    fallaba de vez en cuando. Sale en cuanto se cumple, así que el plazo solo
    se nota cuando el test va a fallar igualmente.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not (result := condition()):
        if loop.time() >= deadline:
            raise AssertionError(message)
        await _sleep(0.01)
    return result


def install_fake(bindir, name, source):
    """Escribe en `bindir` un ejecutable `name` y comprueba que es el que se usará.

    `bindir` tiene que ir ya delante en el PATH. En un tmp montado con noexec
    no se puede ejecutar, y la búsqueda en el PATH seguía hasta el yt-dlp o el
    wget de verdad, que salían a la red: es mejor fallar aquí y decir por qué.
    """
    script = bindir / name
    script.write_text(source)
    script.chmod(0o755)
    assert shutil.which(name) == str(script), (
        f"el {name} falso no se puede ejecutar (¿tmp con noexec? prueba con --basetemp): "
        f"se usaría {shutil.which(name)}"
    )
    return script


def alive(pid, grace=15):
    """Si el proceso sigue vivo (un zombi ya ha muerto, solo falta recogerlo).

    Un SIGKILL no mata en el acto: el proceso muere cuando vuelve a tener CPU,
    y con la máquina cargada aún se le veía vivo justo después. Se le dan
    `grace` segundos antes de darlo por vivo; sale en cuanto muere, así que
    solo se esperan enteros cuando el test va a fallar.
    """
    deadline = time.monotonic() + grace
    while True:
        try:
            os.kill(pid, 0)
            with open(f"/proc/{pid}/stat") as handle:
                if handle.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return False
        except ProcessLookupError:
            # Ya no está, o lo han recogido mientras se leía (a un huérfano lo
            # recoge el PID 1 cuando quiere)
            return False
        except FileNotFoundError:
            # Con /proc, lo han recogido entre las dos comprobaciones. Sin
            # /proc, como en macOS, no se sabe más: kill dice que sigue ahí
            if os.path.isdir("/proc/self"):
                return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.01)
