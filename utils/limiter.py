"""Un semáforo cuyo límite se puede cambiar en marcha.

asyncio.Semaphore fija su valor al crearse, y el número de descargas
simultáneas ahora se cambia desde /settings sin reiniciar. Subir el límite
deja pasar al momento a quien esperaba; bajarlo no interrumpe lo que ya está
en curso, solo hace esperar a los siguientes hasta que haya hueco.
"""

import asyncio


class ResizableLimiter:
    def __init__(self, limit):
        self._limit = max(1, int(limit))
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self):
        return self._limit

    @property
    def active(self):
        return self._active

    async def set_limit(self, limit):
        async with self._condition:
            self._limit = max(1, int(limit))
            self._condition.notify_all()

    async def __aenter__(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1
        return self

    async def __aexit__(self, *exc):
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()
        return False
