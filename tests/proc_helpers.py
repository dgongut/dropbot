"""Procesos que lanzan los tests (yt-dlp falsos y lo que estos lanzan)."""

import os
import time


def alive(pid, grace=5):
    """Si el proceso sigue vivo (un zombi ya ha muerto, solo falta recogerlo).

    Un SIGKILL no mata en el acto: el proceso muere cuando vuelve a tener CPU,
    y con la máquina cargada aún se le veía vivo justo después. Se le dan
    `grace` segundos antes de darlo por vivo.
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
