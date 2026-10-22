import asyncio
import time
from collections import deque
from telethon.errors import BadRequestError, ForbiddenError, UnauthorizedError
from logger import debug, error, warning

# Errores de Telegram que no se arreglan reintentando: el mensaje ya no existe,
# no ha cambiado, el callback caducó, no hay permisos... Reintentarlos solo
# bloqueaba la cola (el worker es único) unos 10 s por cada uno. Lo mismo con
# un texto que no se puede codificar (un nombre de fichero que no es UTF-8)
PERMANENT_ERRORS = (BadRequestError, ForbiddenError, UnauthorizedError, UnicodeError)


def is_permanent(e):
    """Si reintentar no puede arreglar el error.

    Además de los de Telegram, el ValueError de Telethon cuando no sabe a quién
    escribir: un usuario que nunca ha abierto el chat con el bot no aparece por
    mucho que se reintente, y con un id que contuviera "429" se tomaba por un
    rate limit y paraba la cola unos 30 s
    """
    return isinstance(e, PERMANENT_ERRORS) or (isinstance(e, ValueError) and "input entity" in str(e))


# La petición de Telegram que hace cada llamada que pasa por la cola. Telethon
# guarda el FloodWait por tipo de petición para todo el cliente: mientras dura,
# una edición de cualquier mensaje lo lanza al momento si falta más de un
# minuto, y si falta menos se duerme dentro de la llamada y sale al vencer
REQUEST_KINDS = {
    "edit": "edit",
    "reply": "send", "respond": "send", "send_message": "send",
    "send_file": "media",
    "delete": "delete",
}


def request_kind(func):
    """El tipo de petición de `func` (ver REQUEST_KINDS); si no está, su nombre"""
    name = getattr(func, '__name__', str(func))
    return REQUEST_KINDS.get(name, name)


class TelegramMessageQueue:
    """
    Sistema de cola de mensajes asíncrono con rate limiting para evitar saturar Telegram.
    Implementa:
    - Cola de mensajes con delays configurables
    - Reintentos con backoff exponencial
    - Manejo de errores de rate limiting (FloodWaitError, 429)
    """
    def __init__(self, delay_between_messages=0.5, max_retries=5):
        """
        Inicializa la cola de mensajes.

        Args:
            delay_between_messages: Tiempo en segundos entre mensajes (default: 0.5)
            max_retries: Número máximo de reintentos por mensaje (default: 5)
        """
        self.queue = asyncio.Queue()
        # Lo que un FloodWait obliga a esperar, por tipo de petición (ver
        # request_kind): hasta cuándo está bloqueado cada tipo y, en el orden
        # en que llegó, lo que espera a que venza
        self.blocked = {}
        self.deferred = {}
        self.delay_between_messages = delay_between_messages
        self.max_retries = max_retries
        self.running = True
        self.worker_task = None
        debug(f"[STARTUP] Message queue initialized (delay: {delay_between_messages}s, max_retries: {max_retries})")

    async def start(self):
        """Inicia el worker que procesa la cola"""
        if self.worker_task is None or self.worker_task.done():
            # shutdown() lo deja a False: sin esto, el worker nuevo salía al
            # instante y lo que se encolaba después no se enviaba nunca
            self.running = True
            self.worker_task = asyncio.create_task(self._process_queue())
            debug("[STARTUP] Message queue worker started")

    async def _process_queue(self):
        """Procesa la cola de mensajes de forma continua"""
        while self.running:
            try:
                # Lo aparcado por un FloodWait sale en cuanto vence su espera;
                # mientras, se sigue con la cola
                message_data = self._due_deferred()
                if message_data is None:
                    # Obtener el siguiente mensaje de la cola
                    message_data = await asyncio.wait_for(self.queue.get(), timeout=1.0)

                    if message_data is None:  # Señal de parada
                        break

                    # Mientras su tipo tenga algo aparcado, va detrás. Si no,
                    # Telethon lo dormía dentro de la llamada (con el worker
                    # parado para todos) y salía antes que lo aparcado, que al
                    # vencer lo pisaba: una edición antigua quedaba como la última
                    kind = request_kind(message_data['func'])
                    if kind in self.deferred:
                        self.deferred[kind].append(message_data)
                        continue

                await self._execute_message(message_data)
                await asyncio.sleep(self.delay_between_messages)
                
            except asyncio.TimeoutError:
                # Timeout normal, continuar esperando
                continue
            except Exception as e:
                error(f"Error in message queue: {str(e)}")

    def _due_deferred(self):
        """El primer mensaje aparcado de un tipo cuya espera ya ha vencido; si no hay, None"""
        now = time.monotonic()
        for kind, parked in self.deferred.items():
            if self.blocked[kind] <= now:
                message_data = parked.popleft()
                if not parked:
                    del self.deferred[kind]
                    del self.blocked[kind]
                return message_data
        return None

    async def _execute_message(self, message_data):
        """Ejecuta un mensaje con reintentos y backoff exponencial"""
        func = message_data['func']
        args = message_data['args']
        kwargs = message_data['kwargs']
        result_future = message_data.get('result_future')

        # Log para debug
        func_name = getattr(func, '__name__', str(func))

        # Un mensaje que vuelve de estar aparcado sigue por su siguiente intento
        for attempt in range(message_data.get('attempt', 0), self.max_retries):
            # Quien esperaba el resultado se cansó (add_message cancela el future
            # al agotar su timeout) y ya ha seguido por su cuenta: ejecutarlo
            # ahora duplicaría el mensaje
            if result_future is not None and result_future.cancelled():
                warning(f"[QUEUE] Skipping {func_name}: the caller stopped waiting for it")
                return None
            if attempt == 0:
                debug(f"[QUEUE] Executing: {func_name}")
            try:
                debug(f"[QUEUE] Attempt {attempt + 1}/{self.max_retries} for {func_name}")
                result = await func(*args, **kwargs)
                debug(f"[QUEUE] ✅ {func_name} completed successfully")
                if result_future and not result_future.done():
                    result_future.set_result(result)
                return result
                
            except Exception as e:
                error_msg = str(e)
                error_type = type(e).__name__

                error(f"[QUEUE] ❌ Error in {func_name}: {error_type} - {error_msg}")

                if is_permanent(e):
                    if result_future and not result_future.done():
                        result_future.set_exception(e)
                    break

                # Detectar FloodWaitError de Telethon
                if "FloodWaitError" in error_type or "flood" in error_msg.lower():
                    if attempt < self.max_retries - 1:
                        # Extraer tiempo de espera si está disponible
                        try:
                            wait_time = e.seconds if hasattr(e, 'seconds') else (2 ** attempt) * 2
                        except:
                            wait_time = (2 ** attempt) * 2

                        # Se reintenta pasado el tiempo que Telegram solicita, pero sin
                        # dormir aquí: el worker es único y, mientras, no salía nada,
                        # ni a otros chats ni las ediciones, borrados o respuestas que
                        # Telegram no estaba limitando
                        warning(f"[QUEUE] FloodWaitError detected for {func_name}. Retrying in {wait_time}s ({wait_time // 60} minutes), the rest of the queue goes on")
                        message_data['attempt'] = attempt + 1
                        kind = request_kind(func)
                        self.blocked[kind] = time.monotonic() + wait_time
                        # El primero de su tipo: o no había nada aparcado, o
                        # vuelve de estarlo y era el que iba delante
                        self.deferred.setdefault(kind, deque()).appendleft(message_data)
                        return None
                    else:
                        error(f"[QUEUE] FloodWaitError persists after {self.max_retries} attempts for {func_name}")
                
                # Detectar rate limiting genérico (429, Too Many Requests)
                elif "429" in error_msg or "Too Many Requests" in error_msg:
                    if attempt < self.max_retries - 1:
                        wait_time = (2 ** attempt) * 2  # Backoff exponencial: 2, 4, 8, 16 segundos
                        warning(f"[QUEUE] Rate limit detected (429) for {func_name}. Waiting {wait_time}s before retrying...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        error(f"[QUEUE] Rate limit persists after {self.max_retries} attempts for {func_name}")

                # Otros errores: reintento con delay lineal
                elif attempt < self.max_retries - 1:
                    wait_time = 1 * (attempt + 1)
                    warning(f"[QUEUE] Retry {attempt + 1}/{self.max_retries} for {func_name} in {wait_time}s due to: {error_msg}")
                    await asyncio.sleep(wait_time)
                    continue

                # Último intento fallido
                error(f"[QUEUE] ❌ Final error for {func_name} after {self.max_retries} attempts: {error_msg}")
                if result_future and not result_future.done():
                    result_future.set_exception(e)
                break

    async def add_message(self, func, *args, wait_for_result=False, **kwargs):
        """
        Añade un mensaje a la cola.

        Args:
            func: Función asíncrona a ejecutar (debe ser async)
            *args: Argumentos posicionales para la función
            wait_for_result: Si True, espera y retorna el resultado (default: False)
            **kwargs: Argumentos nombrados para la función

        Returns:
            El resultado de la función si wait_for_result=True, None en caso contrario
        """
        func_name = getattr(func, '__name__', str(func))
        result_future = asyncio.Future() if wait_for_result else None

        debug(f"[QUEUE] Adding to queue: {func_name} (wait_for_result={wait_for_result})")

        await self.queue.put({
            'func': func,
            'args': args,
            'kwargs': kwargs,
            'result_future': result_future
        })

        debug(f"[QUEUE] Added to queue: {func_name} (queue size: {self.queue.qsize()})")

        if wait_for_result:
            try:
                debug(f"[QUEUE] Waiting for result of {func_name} (timeout: 300s)...")
                result = await asyncio.wait_for(result_future, timeout=300)  # Esperar máximo 5 minutos
                debug(f"[QUEUE] ✅ Got result for {func_name}")
                return result
            except asyncio.TimeoutError:
                error(f"[QUEUE] ❌ Timeout waiting for {func_name} result in queue (waited 300s)")
                return None
            except asyncio.CancelledError:
                # shutdown() cancela el future de lo que quedó sin enviar: para
                # quien esperaba es como un timeout. Si a quien cancelan es a
                # la propia tarea que espera, se propaga como siempre
                if asyncio.current_task().cancelling():
                    raise
                warning(f"[QUEUE] {func_name} was not sent: the queue stopped")
                return None

        return None

    async def shutdown(self):
        """Detiene la cola de mensajes de forma ordenada"""
        self.running = False
        await self.queue.put(None)  # Señal de parada
        if self.worker_task:
            await self.worker_task
        # Lo que quedó en la cola, o aparcado por un FloodWait, ya no se va a
        # enviar: se descarta (también la señal de parada, si el worker salió
        # sin leerla, para que un start() posterior no se pare con ella) y se
        # cancela el future de quien esperaba el resultado, que si no se quedaba
        # esperando 300 s
        pending = [message_data for parked in self.deferred.values() for message_data in parked]
        self.deferred.clear()
        self.blocked.clear()
        while not self.queue.empty():
            pending.append(self.queue.get_nowait())
        for message_data in pending:
            result_future = message_data and message_data.get('result_future')
            if result_future is not None and not result_future.done():
                result_future.cancel()
        debug("Message queue stopped")

