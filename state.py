"""Estado en memoria compartido entre los handlers del bot.

Antes vivía como una docena de variables globales sueltas en dropbot.py. Al
tenerlo aquí, los módulos de handlers pueden compartirlo importándolo, sin
depender de dropbot.py (que sí depende de telethon y del cliente conectado).

Todo lo de aquí se pierde al reiniciar: son referencias a lo que hay a medias
en cada conversación. Los botones de Telegram no pueden llevar más de 64 bytes
de datos, así que las rutas nunca viajan en el callback data: se guardan en
estos diccionarios y el botón lleva solo un id corto.
"""

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

# Mensajes de /list y /manage que hay que borrar juntos al cerrar:
# {id de usuario: [mensaje, ...]}
list_messages = {}

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
