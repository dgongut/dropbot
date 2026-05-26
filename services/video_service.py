"""
Servicio para obtención de metadatos y miniaturas de vídeo usando ffmpeg/ffprobe.
"""
import os
import time
import json
import asyncio

from config import TEMP_DIR
from debug import debug, warning, error


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

        debug(f"[METADATA] Running ffprobe...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            data = json.loads(stdout.decode())

            # Buscar el stream de video
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    duration = int(float(data.get("format", {}).get("duration", 0)))
                    width = stream.get("width", 0)
                    height = stream.get("height", 0)
                    debug(f"[METADATA] ✅ Metadata obtained: {duration}s, {width}x{height}")
                    return duration, width, height

            warning(f"[METADATA] ⚠️ Video stream not found")
        else:
            error_msg = stderr.decode() if stderr else "Unknown error"
            error(f"[METADATA] ❌ Error running ffprobe (code {proc.returncode}): {error_msg[:200]}")

        return None, None, None
    except Exception as e:
        warning(f"[METADATA] ❌ Exception getting video metadata: {e}")
        return None, None, None


def format_duration(seconds):
    """Formatea la duración en formato HH:MM:SS o MM:SS"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes:02d}:{secs:02d}"


async def generate_video_thumbnail(video_path, output_path=None, timestamp="00:00:03"):
    """
    Genera una miniatura de un video en el segundo especificado.
    Retorna la ruta del thumbnail generado o None si falla.
    """
    try:
        debug(f"[THUMBNAIL] Generating thumbnail for: {video_path}")

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
            "-vf", "scale=320:-1",  # Escalar a 320px de ancho manteniendo aspecto
            "-y",  # Sobrescribir sin preguntar
            output_path
        ]

        debug(f"[THUMBNAIL] Running ffmpeg command...")

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
