"""Estado en memoria compartido entre los handlers del bot.

Antes vivía como una docena de variables globales sueltas en dropbot.py. Al
tenerlo aquí, los módulos de handlers pueden compartirlo importándolo, sin
depender de dropbot.py (que sí depende de telethon y del cliente conectado).

Todo lo de aquí se pierde al reiniciar: son referencias a lo que hay a medias
en cada conversación. Los botones de Telegram no pueden llevar más de 64 bytes
de datos, así que las rutas nunca viajan en el callback data: se guardan en
estos diccionarios y el botón lleva solo un id corto.
"""

import os

# Tareas de descarga en curso, por chat e id de evento (ver task_key en
# dropbot.py), para poder cancelarlas
active_tasks = {}

# Conversiones que el usuario ha cancelado, por id de conversión
cancelled_conversions = set()

# Peticiones de "enviar el original sin convertir", por id de conversión
send_original_requests = set()

# Ficheros con acciones pendientes en /manage: {id corto: ruta}
pending_file_actions = {}

# Carpetas de destino de las extracciones en curso. Mientras dura, otra
# pulsación de Descomprimir que vaya a la misma carpeta se rechaza
extractions_in_progress = set()

# Ficheros esperando un nombre nuevo: {id de usuario: [petición, ...]}
pending_renames = {}

# Ficheros descargados esperando a que el usuario decida qué hacer con ellos:
# {id corto: ruta}
pending_files = {}

# URLs pendientes de que el usuario elija formato: {id corto: url}
pending_urls = {}


_pending_send_seq = 0


def register_pending_send(file_path):
    """Guarda una ruta pendiente de enviar y devuelve un id corto para el botón.

    El id lleva un contador para que dos ficheros del mismo evento no compartan
    clave, y un hash de la ruta para que los botones de una sesión anterior no
    resuelvan por accidente a un fichero distinto tras reiniciar el bot.
    """
    global _pending_send_seq
    _pending_send_seq += 1
    file_id = f"{_pending_send_seq}_{abs(hash(file_path)) % 100000}"
    pending_files[file_id] = file_path
    return file_id


# El id de cada ruta de los listados de /manage: {ruta: id corto}
_manage_ids = {}
_manage_seq = 0


def register_manage_action(file_path):
    """Guarda una ruta de un listado de /manage y devuelve el id de su botón.

    Antes el id era la fila más un hash de la ruta, y dos rutas podían
    coincidir: el listado de otro admin cambiaba a qué fichero apuntaba un
    botón ya enseñado, y un «Sí, eliminar» pendiente borraba otro. Ahora cada
    ruta lleva un contador propio, más el hash para que los botones de antes de
    reiniciar no resuelvan a otra, y conserva su id mientras siga apuntando a
    ella: listar una y otra vez no hace crecer el diccionario.
    """
    global _manage_seq
    file_id = _manage_ids.get(file_path)
    if file_id is None or pending_file_actions.get(file_id) != file_path:
        _manage_seq += 1
        file_id = f"m{_manage_seq}_{abs(hash(file_path)) % 100000}"
        _manage_ids[file_path] = file_id
        pending_file_actions[file_id] = file_path
    return file_id


# Selectores de carpeta abiertos: {id corto: datos} (ver handlers/picker.py)
pickers = {}

# Texto que se espera de cada usuario (una ruta, una plantilla...):
# {id de usuario: petición} (ver handlers/inputs.py)
pending_inputs = {}

# Lo que se enseñó antes de confirmar una acción sobre varios elementos, para
# hacer exactamente eso y no lo que haya al pulsar: {(usuario, clave): datos}
confirmations = {}

# Por dónde se ha ido en cada mensaje de /manage o /find, para que «Volver»
# regrese al listado del que se venía (su página, su categoría, la búsqueda) y
# no siempre al general: {(chat, mensaje): [callback del listado, ...]}
manage_trails = {}

# Búsquedas de /find, para sus botones de página: {id corto: texto buscado}
find_queries = {}

# Descargas recién llegadas con algo que ofrecer (moverlas a la carpeta de su
# serie, sustituir una versión anterior...): {id corto: datos}
arrivals = {}

_short_seq = 0
# Distinto en cada arranque: el contador vuelve a empezar al reiniciar, y sin
# esto el «Deshacer» de un aviso de antes resolvía a la descarga que hubiera
# recibido ese mismo número después
_run_token = os.urandom(2).hex()


def short_id(prefix):
    """Un id corto para un botón, que no se repite ni de un arranque a otro."""
    global _short_seq
    _short_seq += 1
    return f"{prefix}{_short_seq}_{_run_token}"


# Lo que ya se ha atendido desde que arrancó el bot: {(id del registro, clave)}
_claimed = set()


def claim(registry, key):
    """Saca de `registry` lo que un botón tenía pendiente, y lo apunta.

    Así, cuando un botón no encuentra lo suyo, se distingue el segundo toque de
    un doble toque (ya atendido: no se dice nada) del botón de antes de
    reiniciar (caducado: se avisa, en vez de quedarse mudo).
    """
    value = registry.pop(key, None)
    if value is not None:
        _claimed.add((id(registry), key))
    return value


def was_claimed(registry, key):
    return (id(registry), key) in _claimed


def unclaim(registry, key):
    """La clave vuelve a estar pendiente (una confirmación que se pide otra vez)."""
    _claimed.discard((id(registry), key))


def repoint(old_path, new_path):
    """Un fichero ha cambiado de sitio o de nombre: lo que apuntaba a la ruta
    vieja (los botones de /manage, los de enviar) pasa a apuntar a la nueva.
    También lo que había dentro, si era una carpeta."""
    prefix = old_path.rstrip("/") + "/"

    def moved(path):
        if path == old_path:
            return new_path
        if path.startswith(prefix):
            return os.path.join(new_path, path[len(prefix):])
        return None

    for registry in (pending_file_actions, pending_files):
        for key, path in list(registry.items()):
            target = moved(path)
            if target is not None:
                registry[key] = target
    for path, file_id in list(_manage_ids.items()):
        target = moved(path)
        if target is not None:
            del _manage_ids[path]
            _manage_ids[target] = file_id
