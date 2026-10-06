"""¿Está esta ruta montada como volumen?

Lo usan dos cosas que deciden según lo que haya en el docker-compose, en lugar
de preguntarlo con una variable:

- `config`, para saber a qué carpeta va cada tipo de fichero: si `/video` está
  montada, los vídeos van ahí; si no, a `/downloads`.
- `store`, para saber si los ajustes sobreviven a recrear el contenedor.
"""

import os


def is_mounted(path):
    """True si `path` es en sí un punto de montaje.

    os.path.ismount compara ids de dispositivo, que basta para los volúmenes que
    crea Docker, pero también se mira /proc/mounts: equivocarse aquí significa
    escribir en el sistema de ficheros del contenedor y perderlo todo en la
    siguiente actualización.
    """
    try:
        if os.path.ismount(path):
            return True
    except OSError:
        pass
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as mounts:
            for line in mounts:
                fields = line.split()
                if len(fields) > 1 and fields[1] == path:
                    return True
    except OSError:
        pass
    return False


def is_persisted(path, mounted=is_mounted):
    """True si lo que se escriba bajo `path` sobrevive a recrear el contenedor.

    No hace falta que el volumen esté montado justo en `path`: montar un padre
    vale igual. La raíz del contenedor no cuenta, porque en un contenedor
    siempre es un punto de montaje.
    """
    current = os.path.abspath(path)
    while current and current != os.path.dirname(current):
        if mounted(current):
            return True
        current = os.path.dirname(current)
    return False
