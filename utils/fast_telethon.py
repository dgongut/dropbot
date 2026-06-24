"""
Transferencia paralela de ficheros para Telethon (estilo FastTelethon).

Acelera descargas y subidas abriendo varias conexiones (MTProtoSender)
simultáneas al mismo datacenter y transfiriendo varios trozos a la vez,
alcanzando velocidades equivalentes a Pyrogram.

Basado en la implementación de Tulir Asokan / painor (licencia MIT),
adaptada a Telethon 1.43.x con limpieza de conexiones garantizada.
"""
import asyncio
import hashlib
import inspect
import math

from telethon import utils, helpers
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest, ImportAuthorizationRequest,
)
from telethon.tl.functions.upload import (
    GetFileRequest, SaveFilePartRequest, SaveBigFilePartRequest,
)
from telethon.tl.types import InputFileBig, InputFile


# Nº máximo de chunks pre-descargados que cada conexión puede mantener en cola
# esperando a ser escritos a disco. Limita la memoria del pipeline de descarga
# (memoria ≈ conexiones × _PIPELINE_BUFFER × part_size) y aplica backpressure
# para que una conexión rápida no se desboque si el disco va lento.
_PIPELINE_BUFFER = 4


class _DownloadSender:
    def __init__(self, client, sender, file, offset, limit, stride, count):
        self.client = client
        self.sender = sender
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count

    async def next(self):
        if not self.remaining:
            return None
        result = await self.client._call(self.sender, self.request)
        self.remaining -= 1
        self.request.offset += self.stride
        return result.bytes

    def disconnect(self):
        return self.sender.disconnect()


class _UploadSender:
    def __init__(self, client, sender, file_id, part_count, big, index, stride, loop):
        self.client = client
        self.sender = sender
        self.part_count = part_count
        if big:
            self.request = SaveBigFilePartRequest(file_id, index, part_count, b"")
        else:
            self.request = SaveFilePartRequest(file_id, index, b"")
        self.stride = stride
        self.previous = None
        self.loop = loop

    async def next(self, data):
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(data))

    async def _next(self, data):
        self.request.bytes = data
        await self.client._call(self.sender, self.request)
        self.request.file_part += self.stride

    async def disconnect(self):
        if self.previous:
            await self.previous
        return await self.sender.disconnect()


class _ParallelTransferrer:
    def __init__(self, client, dc_id=None):
        self.client = client
        self.loop = asyncio.get_running_loop()
        self.dc_id = dc_id or client.session.dc_id
        self.auth_key = (None if dc_id and client.session.dc_id != dc_id
                         else client.session.auth_key)
        self.senders = None
        self.upload_ticker = 0

    async def _cleanup(self):
        if self.senders:
            await asyncio.gather(
                *(s.disconnect() for s in self.senders),
                return_exceptions=True,
            )
        self.senders = None

    @staticmethod
    def _connection_count(file_size, max_count, full_size=100 * 1024 * 1024):
        if file_size > full_size:
            return max_count
        return max(1, math.ceil((file_size / full_size) * max_count))

    async def _create_sender(self):
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(self.client._connection(
            dc.ip_address, dc.port, dc.id,
            loggers=self.client._log,
            proxy=self.client._proxy,
            local_addr=self.client._local_addr,
        ))
        if not self.auth_key:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(
                id=auth.id, bytes=auth.bytes)
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        return sender

    async def _create_download_sender(self, file, index, part_size, stride, count):
        return _DownloadSender(
            self.client, await self._create_sender(), file,
            index * part_size, part_size, stride, count)

    async def _create_upload_sender(self, file_id, part_count, big, index, stride):
        return _UploadSender(
            self.client, await self._create_sender(), file_id,
            part_count, big, index, stride, self.loop)

    async def _init_download(self, connections, file, part_count, part_size):
        minimum, remainder = divmod(part_count, connections)

        def get_part_count():
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        first = _DownloadSender(
            self.client, await self._create_sender(), file,
            0, part_size, connections * part_size, get_part_count())
        rest = await asyncio.gather(*(
            self._create_download_sender(
                file, i, part_size, connections * part_size, get_part_count())
            for i in range(1, connections)))
        self.senders = [first, *rest]

    async def download(self, file, file_size, max_connections, part_size_kb=None):
        connections = self._connection_count(file_size, max_connections)
        part_size = (part_size_kb or utils.get_appropriated_part_size(file_size)) * 1024
        part_count = math.ceil(file_size / part_size)
        await self._init_download(connections, file, part_count, part_size)

        # Pipeline continuo: cada conexión descarga sus partes sin esperar a las
        # demás y las va depositando en su propia cola acotada (backpressure). El
        # consumidor las recoge en orden global estricto (round-robin por índice
        # de parte), por lo que el fichero se reconstruye byte a byte igual que
        # en el modo secuencial. La conexión i atiende las partes i, i+N, i+2N…,
        # exactamente las que pide queues[part % connections].
        queues = [asyncio.Queue(maxsize=_PIPELINE_BUFFER) for _ in self.senders]

        async def _producer(sender, queue):
            try:
                while True:
                    data = await sender.next()
                    if not data:
                        break
                    await queue.put(data)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # Propagar el fallo al consumidor (activará el fallback estándar).
                await queue.put(exc)

        producers = [
            self.loop.create_task(_producer(s, q))
            for s, q in zip(self.senders, queues)
        ]
        try:
            for part in range(part_count):
                item = await queues[part % connections].get()
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            for producer in producers:
                producer.cancel()
            await asyncio.gather(*producers, return_exceptions=True)
            await self._cleanup()

    async def init_upload(self, file_id, file_size, max_connections, part_size_kb=None):
        connections = self._connection_count(file_size, max_connections)
        part_size = (part_size_kb or utils.get_appropriated_part_size(file_size)) * 1024
        part_count = math.ceil(file_size / part_size)
        is_large = file_size > 10 * 1024 * 1024
        first = _UploadSender(
            self.client, await self._create_sender(), file_id,
            part_count, is_large, 0, connections, self.loop)
        rest = await asyncio.gather(*(
            self._create_upload_sender(file_id, part_count, is_large, i, connections)
            for i in range(1, connections)))
        self.senders = [first, *rest]
        return part_size, part_count, is_large

    async def upload(self, part):
        await self.senders[self.upload_ticker].next(part)
        self.upload_ticker = (self.upload_ticker + 1) % len(self.senders)

    async def finish_upload(self):
        await self._cleanup()


def connection_count(file_size, max_connections, full_size=100 * 1024 * 1024):
    """Número real de conexiones paralelas que se usarán para `file_size`.
    Escala con el tamaño: solo alcanza `max_connections` por encima de `full_size`."""
    return _ParallelTransferrer._connection_count(file_size, max_connections, full_size)


async def download_file(client, location, out, max_connections, progress_callback=None):
    """Descarga `location` (Document/Photo) en el stream `out` usando varias
    conexiones paralelas. `location` debe exponer `.size`."""
    size = location.size
    dc_id, input_location = utils.get_input_location(location)
    transferrer = _ParallelTransferrer(client, dc_id)
    loop = asyncio.get_running_loop()
    downloaded = 0
    async for chunk in transferrer.download(input_location, size, max_connections):
        # Escribir en un hilo para no bloquear el event loop (importante con
        # varias descargas concurrentes y/o sistemas de ficheros lentos).
        await loop.run_in_executor(None, out.write, chunk)
        downloaded += len(chunk)
        if progress_callback:
            r = progress_callback(downloaded, size)
            if inspect.isawaitable(r):
                await r
    # Verificación de integridad: si faltan bytes, fallar para que el llamador
    # recurra al método estándar en lugar de guardar un fichero truncado.
    if downloaded != size:
        raise ValueError(
            f"Incomplete parallel download: got {downloaded} of {size} bytes"
        )
    return out


async def upload_file(client, file, file_size, max_connections, progress_callback=None):
    """Sube el stream binario `file` con varias conexiones paralelas y devuelve
    el handle (InputFile/InputFileBig) listo para `client.send_file(file=...)`."""
    file_id = helpers.generate_random_long()
    hash_md5 = hashlib.md5()
    uploader = _ParallelTransferrer(client)
    part_size, part_count, is_large = await uploader.init_upload(
        file_id, file_size, max_connections)
    loop = asyncio.get_running_loop()
    buffer = bytearray()
    uploaded = 0
    try:
        while True:
            # Leer en un hilo para no bloquear el event loop (importante con
            # sistemas de ficheros lentos como bind mounts en Docker/Mac).
            data = await loop.run_in_executor(None, file.read, part_size)
            if not data:
                break
            uploaded += len(data)
            if progress_callback:
                r = progress_callback(uploaded, file_size)
                if inspect.isawaitable(r):
                    await r
            if not is_large:
                hash_md5.update(data)
            if len(buffer) == 0 and len(data) == part_size:
                await uploader.upload(data)
                continue
            buffer.extend(data)
            while len(buffer) >= part_size:
                await uploader.upload(bytes(buffer[:part_size]))
                del buffer[:part_size]
        if len(buffer) > 0:
            await uploader.upload(bytes(buffer))
    finally:
        await uploader.finish_upload()
    if is_large:
        return InputFileBig(file_id, part_count, "upload")
    return InputFile(file_id, part_count, "upload", hash_md5.hexdigest())
