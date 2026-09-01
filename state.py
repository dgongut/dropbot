"""Estado en memoria compartido entre los handlers del bot.

Antes vivía como una docena de variables globales sueltas en dropbot.py. Al
tenerlo aquí, los módulos de handlers pueden compartirlo importándolo, sin
depender de dropbot.py (que sí depende de telethon y del cliente conectado).

Todo lo de aquí se pierde al reiniciar: son referencias a lo que hay a medias
en cada conversación. Los botones de Telegram no pueden llevar más de 64 bytes
de datos, así que las rutas nunca viajan en el callback data: se guardan en
estos diccionarios y el botón lleva solo un id corto.
"""

# Tareas de descarga en curso, por id de evento, para poder cancelarlas
active_tasks = {}

# Conversiones que el usuario ha cancelado, por id de conversión
cancelled_conversions = set()

# Peticiones de "enviar el original sin convertir", por id de conversión
send_original_requests = set()

# Ficheros con acciones pendientes en /manage: {id corto: ruta}
pending_file_actions = {}

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

# Descargas de playlist en curso: {id de evento: {...}}
playlist_downloads = {}


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


def register_pending_file_action(file_path):
    """Guarda una ruta y devuelve un id corto para usarlo en un botón."""
    file_id = f"a{abs(hash(file_path)) % 100000000}"
    pending_file_actions[file_id] = file_path
    return file_id
