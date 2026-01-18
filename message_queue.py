import asyncio
from debug import debug, error, warning

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
        self.delay_between_messages = delay_between_messages
        self.max_retries = max_retries
        self.running = True
        self.worker_task = None
        debug(f"[STARTUP] Message queue initialized (delay: {delay_between_messages}s, max_retries: {max_retries})")

    async def start(self):
        """Inicia el worker que procesa la cola"""
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._process_queue())
            debug("[STARTUP] Message queue worker started")

    async def _process_queue(self):
        """Procesa la cola de mensajes de forma continua"""
        while self.running:
            try:
                # Obtener el siguiente mensaje de la cola
                message_data = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                
                if message_data is None:  # Señal de parada
                    break

                await self._execute_message(message_data)
                await asyncio.sleep(self.delay_between_messages)
                
            except asyncio.TimeoutError:
                # Timeout normal, continuar esperando
                continue
            except Exception as e:
                error(f"Error in message queue: {str(e)}")

    async def _execute_message(self, message_data):
        """Ejecuta un mensaje con reintentos y backoff exponencial"""
        func = message_data['func']
        args = message_data['args']
        kwargs = message_data['kwargs']
        result_future = message_data.get('result_future')

        # Log para debug
        func_name = getattr(func, '__name__', str(func))
        debug(f"[QUEUE] Executing: {func_name}")

        for attempt in range(self.max_retries):
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

                # Detectar FloodWaitError de Telethon
                if "FloodWaitError" in error_type or "flood" in error_msg.lower():
                    if attempt < self.max_retries - 1:
                        # Extraer tiempo de espera si está disponible
                        try:
                            wait_time = e.seconds if hasattr(e, 'seconds') else (2 ** attempt) * 2
                        except:
                            wait_time = (2 ** attempt) * 2

                        # Esperar el tiempo que Telegram solicita
                        warning(f"[QUEUE] FloodWaitError detected for {func_name}. Waiting {wait_time}s ({wait_time // 60} minutes) before retrying...")
                        await asyncio.sleep(wait_time)
                        continue
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

        return None

    async def shutdown(self):
        """Detiene la cola de mensajes de forma ordenada"""
        self.running = False
        await self.queue.put(None)  # Señal de parada
        if self.worker_task:
            await self.worker_task
        debug("Message queue stopped")

