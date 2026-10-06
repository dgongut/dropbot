"""
Servicio para obtención de metadatos y miniaturas de vídeo usando ffmpeg/ffprobe.
"""
import os
import time
import json
import asyncio

from config import TEMP_DIR
from logger import debug, warning, error


async def get_video_metadata(file_path):
    """Obtiene metadatos del video usando ffprobe"""
    try:
        debug(f"[METADATA] Getting metadata from: {file_path}")

        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path
        ]

        debug("[METADATA] Running ffprobe...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            data = json.loads(stdout.decode())

            streams = data.get("streams", [])
            duration = int(float(data.get("format", {}).get("duration", 0)))

            # Buscar el stream de video
            for stream in streams:
                if stream.get("codec_type") == "video":
                    width = stream.get("width", 0)
                    height = stream.get("height", 0)
                    debug(f"[METADATA] ✅ Metadata obtained: {duration}s, {width}x{height}")
                    return duration, width, height

            # Un audio sin carátula no tiene pista de vídeo, pero los envíos
            # piden aquí también su duración: sin ella Telegram lo muestra sin
            # barra de reproducción. Se devuelve sin dimensiones
            if any(stream.get("codec_type") == "audio" for stream in streams):
                debug(f"[METADATA] ✅ Audio metadata obtained: {duration}s")
                return duration or None, None, None

            warning("[METADATA] ⚠️ Video stream not found")
        else:
            error_msg = stderr.decode() if stderr else "Unknown error"
            error(f"[METADATA] ❌ Error running ffprobe (code {proc.returncode}): {error_msg[:200]}")

        return None, None, None
    except Exception as e:
        warning(f"[METADATA] ❌ Exception getting video metadata: {e}")
        return None, None, None


async def probe_media(file_path):
    """Devuelve la salida JSON de ffprobe (formato y streams) o None si falla."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            warning(f"[PROBE] ⚠️ ffprobe failed (code {proc.returncode}) for {file_path}")
            return None
        return json.loads(stdout.decode())
    except Exception as e:
        warning(f"[PROBE] ❌ Exception probing {file_path}: {e}")
        return None


# Lo que Telegram reproduce sin convertir dentro de un MP4: H.264 de 8 bits o
# HEVC de 8/10 bits (lo que graban los móviles), en 4:2:0, con audio AAC o MP3
# (o sin audio). El H.264 de 10 bits no lo decodifican muchos dispositivos.
TELEGRAM_VIDEO_PIX_FMTS = {
    "h264": {"yuv420p", "yuvj420p"},
    "hevc": {"yuv420p", "yuvj420p", "yuv420p10le"},
}
TELEGRAM_AUDIO_CODECS = {"aac", "mp3"}


def telegram_conversion_plan(file_path, probe):
    """Decide cuánto trabajo hace falta para que Telegram reproduzca el vídeo.

    Devuelve un dict con:
      - "video": "copy" o "encode"
      - "audio": "copy", "encode" o None (sin pista de audio)
      - "remux": True si hay que reescribir el contenedor como MP4
    o None si no se pudo analizar (en ese caso se convierte entero).
    Si video y audio son "copy" y remux es False, el fichero vale tal cual.
    """
    if not probe:
        return None
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    vcodec = video.get("codec_name")
    video_ok = video.get("pix_fmt") in TELEGRAM_VIDEO_PIX_FMTS.get(vcodec, ())
    if audio is None:
        audio_action = None
    elif audio.get("codec_name") in TELEGRAM_AUDIO_CODECS:
        audio_action = "copy"
    else:
        audio_action = "encode"

    format_names = set((probe.get("format", {}).get("format_name") or "").split(","))
    is_mp4 = "mp4" in format_names and file_path.lower().endswith(".mp4")
    # Los reproductores de Apple solo aceptan HEVC en MP4 con la etiqueta
    # "hvc1"; con "hev1" (o al venir de MKV) hay que reetiquetarlo, sin recodificar
    needs_hvc1 = vcodec == "hevc" and video.get("codec_tag_string") != "hvc1"

    return {
        "video": "copy" if video_ok else "encode",
        "audio": audio_action,
        "remux": not is_mp4 or (video_ok and needs_hvc1),
        "hvc1": video_ok and vcodec == "hevc",
    }


def plan_is_noop(plan):
    """True si el plan indica que el fichero ya es compatible tal cual."""
    return (
        plan is not None
        and plan["video"] == "copy"
        and plan["audio"] in ("copy", None)
        and not plan["remux"]
    )


def format_duration(seconds):
    """Formatea la duración en formato HH:MM:SS o MM:SS"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes:02d}:{secs:02d}"


# Telegram rechaza miniaturas con algún lado de más de 320 px
THUMBNAIL_MAX_SIDE = 320


async def _thumbnail_timestamp(video_path):
    """Instante para la miniatura: el segundo 3 (pasado el negro inicial), o la
    mitad del vídeo si dura menos, porque más allá del final ffmpeg no
    encuentra fotograma y no escribe nada."""
    probe = await probe_media(video_path)
    try:
        duration = float((probe or {}).get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0
    return f"{min(3.0, duration / 2):.3f}" if duration > 0 else "0"


async def generate_video_thumbnail(video_path, output_path=None, timestamp=None):
    """
    Genera una miniatura de un video en el segundo especificado (por defecto,
    el que elige _thumbnail_timestamp según la duración).
    Retorna la ruta del thumbnail generado o None si falla.
    """
    try:
        debug(f"[THUMBNAIL] Generating thumbnail for: {video_path}")

        if timestamp is None:
            timestamp = await _thumbnail_timestamp(video_path)

        if output_path is None:
            # Generar thumbnail en /tmp
            video_filename = os.path.basename(video_path)
            base_name = os.path.splitext(video_filename)[0]
            timestamp_ms = int(time.time() * 1000)
            output_path = os.path.join(TEMP_DIR, f"{base_name}_{timestamp_ms}_thumb.jpg")

        debug(f"[THUMBNAIL] Output path: {output_path}")
        debug(f"[THUMBNAIL] Timestamp: {timestamp}")

        cmd = [
            "ffmpeg",
            "-i", video_path,
            "-ss", timestamp,  # Segundo del video para capturar
            "-vframes", "1",   # Solo 1 frame
            # Encajar en 320x320 manteniendo el aspecto: con scale=320:-1 un
            # vídeo vertical salía de 320x427
            "-vf", (f"scale={THUMBNAIL_MAX_SIDE}:{THUMBNAIL_MAX_SIDE}"
                    ":force_original_aspect_ratio=decrease"),
            "-y",  # Sobrescribir sin preguntar
            output_path
        ]

        debug("[THUMBNAIL] Running ffmpeg command...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        _, stderr = await proc.communicate()

        if proc.returncode == 0 and os.path.exists(output_path):
            thumb_size = os.path.getsize(output_path)
            debug(f"[THUMBNAIL] ✅ Thumbnail generated successfully: {output_path} ({thumb_size} bytes)")
            return output_path
        else:
            error_msg = stderr.decode() if stderr else "Unknown error"
            warning(f"[THUMBNAIL] ❌ Error generating thumbnail (code {proc.returncode}): {error_msg[:200]}")
            return None
    except Exception as e:
        warning(f"[THUMBNAIL] ❌ Exception generating thumbnail: {e}")
        return None
