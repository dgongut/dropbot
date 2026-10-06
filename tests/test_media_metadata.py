"""Metadatos y miniaturas de ficheros multimedia, con ffmpeg/ffprobe de verdad.

Cubre `services/video_service.py` (get_video_metadata, probe_media,
format_duration, generate_video_thumbnail), `get_file_info` de dropbot.py (la
ficha que se le enseña al usuario al terminar una descarga) y lo que llega a
Telegram al enviar un vídeo o un audio reales. Los ficheros se generan en el
propio test con las fuentes `lavfi` de ffmpeg: cortos y diminutos.
"""

import json
import os
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

from services import video_service

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="requiere ffmpeg y ffprobe",
)


def _video(duration, size, rate=10):
    return ["-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={rate}"]


def _audio(duration):
    return ["-f", "lavfi", "-i", f"sine=duration={duration}"]


H264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
VP9 = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p"]

SPECS = {
    "h264_aac.mp4": _video(1, "160x120") + _audio(1) + H264 + ["-c:a", "aac"],
    "h264_4s.mp4": _video(4, "160x120", 5) + H264,
    "vertical_4s.mp4": _video(4, "120x160", 5) + H264,
    "vp9_4s.webm": _video(4, "160x120", 5) + _audio(4) + VP9 + ["-c:a", "libopus"],
    "audio.mp3": _audio(1) + ["-c:a", "libmp3lame", "-b:a", "64k"],
    "audio.m4a": _audio(1) + ["-c:a", "aac"],
}


class MediaLibrary:
    """Genera cada fichero la primera vez que un test lo pide."""

    def __init__(self, root):
        self.root = root

    def __call__(self, name):
        path = self.root / name
        if not path.exists():
            if name == "not_media.mp4":
                path.write_text("esto no es un vídeo\n" * 50)
            else:
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-nostdin", *SPECS[name], "-shortest", "-y", str(path)],
                    check=True, capture_output=True,
                )
        return str(path)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return MediaLibrary(tmp_path_factory.mktemp("media"))


def ffprobe_video(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, check=True,
    ).stdout
    return next(s for s in json.loads(out)["streams"] if s["codec_type"] == "video")


@pytest.fixture
def no_ffmpeg(monkeypatch, tmp_path):
    """Un PATH sin ffmpeg ni ffprobe."""
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))


# --- format_duration ------------------------------------------------------------

@pytest.mark.parametrize(("seconds", "expected"), [
    (0, "00:00"),
    (7, "00:07"),
    (59, "00:59"),
    (60, "01:00"),
    (61, "01:01"),
    (3599, "59:59"),
    (3600, "01:00:00"),
    (3661, "01:01:01"),
    (100 * 3600, "100:00:00"),
])
def test_format_duration(seconds, expected):
    assert video_service.format_duration(seconds) == expected


# --- get_video_metadata ---------------------------------------------------------

@needs_ffmpeg
def test_metadatos_de_un_video(media, run_async):
    assert run_async(video_service.get_video_metadata(media("h264_aac.mp4"))) == (1, 160, 120)


@needs_ffmpeg
def test_metadatos_de_un_video_vertical(media, run_async):
    assert run_async(video_service.get_video_metadata(media("vertical_4s.mp4"))) == (4, 120, 160)


@needs_ffmpeg
def test_metadatos_de_un_webm(media, run_async):
    assert run_async(video_service.get_video_metadata(media("vp9_4s.webm"))) == (4, 160, 120)


@needs_ffmpeg
@pytest.mark.parametrize("name", ["not_media.mp4", "no_existe.mp4"])
def test_sin_video_analizable_no_hay_metadatos(media, run_async, tmp_path, name):
    path = media(name) if name != "no_existe.mp4" else str(tmp_path / name)
    assert run_async(video_service.get_video_metadata(path)) == (None, None, None)


def test_sin_ffprobe_no_hay_metadatos_ni_excepcion(no_ffmpeg, media_file, run_async):
    assert run_async(video_service.get_video_metadata(media_file("v.mp4"))) == (None, None, None)


@needs_ffmpeg
@pytest.mark.parametrize("name", ["audio.mp3", "audio.m4a"])
def test_la_duracion_de_un_audio_se_obtiene(media, run_async, name):
    duration, _, _ = run_async(video_service.get_video_metadata(media(name)))
    assert duration == 1


# --- probe_media ----------------------------------------------------------------

@needs_ffmpeg
def test_probe_media_devuelve_formato_y_pistas(media, run_async):
    probe = run_async(video_service.probe_media(media("h264_aac.mp4")))
    assert "mp4" in probe["format"]["format_name"].split(",")
    assert sorted(s["codec_name"] for s in probe["streams"]) == ["aac", "h264"]


@needs_ffmpeg
def test_probe_media_de_algo_que_no_es_multimedia(media, run_async):
    assert run_async(video_service.probe_media(media("not_media.mp4"))) is None


def test_probe_media_sin_ffprobe(no_ffmpeg, media_file, run_async):
    assert run_async(video_service.probe_media(media_file("v.mp4"))) is None


# --- generate_video_thumbnail ----------------------------------------------------

@needs_ffmpeg
def test_la_miniatura_es_un_jpeg_de_320_de_ancho(media, run_async, tmp_path):
    output = str(tmp_path / "thumb.jpg")

    result = run_async(video_service.generate_video_thumbnail(media("h264_4s.mp4"), output))

    assert result == output
    with open(output, "rb") as handle:
        assert handle.read(2) == b"\xff\xd8"
    stream = ffprobe_video(output)
    assert (stream["width"], stream["height"]) == (320, 240)


@needs_ffmpeg
def test_la_miniatura_por_defecto_va_a_temp_dir(media, run_async):
    os.makedirs(video_service.TEMP_DIR, exist_ok=True)

    result = run_async(video_service.generate_video_thumbnail(media("h264_4s.mp4")))
    try:
        assert result is not None
        assert os.path.dirname(result) == video_service.TEMP_DIR
        assert os.path.basename(result).startswith("h264_4s_")
        assert result.endswith("_thumb.jpg")
        assert os.path.getsize(result) > 0
    finally:
        if result and os.path.exists(result):
            os.remove(result)


@needs_ffmpeg
def test_un_video_de_menos_de_3_segundos_tambien_tiene_miniatura(media, run_async, tmp_path):
    output = str(tmp_path / "thumb.jpg")
    assert run_async(video_service.generate_video_thumbnail(media("h264_aac.mp4"), output)) == output


@needs_ffmpeg
def test_la_miniatura_de_un_video_vertical_no_pasa_de_320(media, run_async, tmp_path):
    output = str(tmp_path / "thumb.jpg")

    assert run_async(video_service.generate_video_thumbnail(media("vertical_4s.mp4"), output)) == output
    stream = ffprobe_video(output)
    assert max(stream["width"], stream["height"]) <= 320


@needs_ffmpeg
def test_la_miniatura_en_otro_instante(media, run_async, tmp_path):
    output = str(tmp_path / "thumb.jpg")
    result = run_async(video_service.generate_video_thumbnail(media("h264_aac.mp4"), output, "00:00:00.5"))
    assert result == output and os.path.getsize(output) > 0


@needs_ffmpeg
@pytest.mark.parametrize("name", ["audio.mp3", "not_media.mp4"])
def test_sin_video_no_hay_miniatura(media, run_async, tmp_path, name):
    output = tmp_path / "thumb.jpg"
    assert run_async(video_service.generate_video_thumbnail(media(name), str(output))) is None
    assert not output.exists()


def test_sin_ffmpeg_no_hay_miniatura(no_ffmpeg, media_file, run_async, tmp_path):
    assert run_async(video_service.generate_video_thumbnail(media_file("v.mp4"), str(tmp_path / "t.jpg"))) is None


# --- get_file_info ---------------------------------------------------------------

@needs_ffmpeg
def test_ficha_de_un_video(dropbot, media, run_async):
    path = media("h264_aac.mp4")

    info = run_async(dropbot.get_file_info(path))

    assert info["type"] == "video"
    assert info["extension"] == ".mp4"
    assert info["size"] == os.path.getsize(path)
    assert info["size_formatted"].endswith("KB")
    assert (info["duration"], info["duration_formatted"]) == (1, "00:01")
    assert info["resolution"] == "160x120"
    assert (info["codec_video"], info["codec_audio"]) == ("H264", "AAC")
    assert info["bitrate"].endswith(" kbps")


@needs_ffmpeg
def test_ficha_de_un_webm(dropbot, media, run_async):
    info = run_async(dropbot.get_file_info(media("vp9_4s.webm")))

    assert info["type"] == "video"
    assert info["duration_formatted"] == "00:04"
    assert (info["codec_video"], info["codec_audio"]) == ("VP9", "OPUS")


@needs_ffmpeg
def test_ficha_de_un_audio(dropbot, media, run_async):
    info = run_async(dropbot.get_file_info(media("audio.mp3")))

    assert info["type"] == "audio"
    assert info["duration"] == 1
    assert info["codec_audio"] == "MP3"
    assert info["codec_video"] is None and info["resolution"] is None
    # Media del contenedor: ronda los 64 kbps del MP3 más las cabeceras
    assert 60 <= int(info["bitrate"].removesuffix(" kbps")) <= 80


@needs_ffmpeg
def test_ficha_de_un_video_que_no_lo_es(dropbot, media, run_async):
    """Extensión de vídeo, contenido basura: tipo por extensión y sin datos."""
    info = run_async(dropbot.get_file_info(media("not_media.mp4")))

    assert info["type"] == "video"
    assert info["size"] > 0
    assert info["duration"] is None and info["resolution"] is None and info["codec_video"] is None


@pytest.mark.parametrize(("name", "kind"), [
    ("foto.jpg", "image"),
    ("x.torrent", "torrent"),
    ("notas.pdf", "document"),
    ("SIN_EXTENSION", "document"),
])
def test_la_ficha_de_lo_que_no_es_multimedia_no_llama_a_ffprobe(
    dropbot, media_file, run_async, monkeypatch, name, kind
):
    async def no_subprocess(*args, **kwargs):
        raise AssertionError("no debería lanzar ffprobe")

    monkeypatch.setattr(dropbot.asyncio, "create_subprocess_exec", no_subprocess)

    info = run_async(dropbot.get_file_info(media_file(name, 2048)))

    assert info["type"] == kind
    assert info["size"] == 2048 and info["size_formatted"] == "2.00 KB"
    assert info["duration"] is None


def test_la_ficha_de_un_fichero_que_ya_no_existe(dropbot, run_async, tmp_path):
    info = run_async(dropbot.get_file_info(str(tmp_path / "borrado.mkv")))

    assert info == {"size": 0, "size_formatted": "0 B", "extension": ".mkv", "type": "document"}


# --- Lo que llega a Telegram al enviar ficheros reales ---------------------------

@pytest.fixture
def upload(quiet_bot, config_dir, monkeypatch):
    """Envío real (conversión, metadatos, miniatura) con la subida simulada."""
    captured = {}

    async def fake_send(entity, path, filename, attributes, thumb, is_video, callback):
        captured.update(path=path, filename=filename, attributes=attributes, thumb=thumb,
                        thumb_existed=bool(thumb) and os.path.exists(thumb),
                        thumb_size=ffprobe_video(thumb) if thumb and os.path.exists(thumb) else None,
                        file_existed=os.path.exists(path))
        return MagicMock(id=7)

    monkeypatch.setattr(quiet_bot, "_send_file_fast", fake_send)
    os.makedirs(quiet_bot.TEMP_DIR, exist_ok=True)
    return captured


def _attribute(captured, cls):
    return next((a for a in captured["attributes"] if isinstance(a, cls)), None)


@needs_ffmpeg
def test_enviar_un_webm_lo_convierte_y_lo_anuncia_como_video(quiet_bot, upload, media, make_event, run_async):
    source = media("vp9_4s.webm")
    before = set(os.listdir(quiet_bot.TEMP_DIR))

    message = run_async(quiet_bot.send_file_to_telegram(make_event(), source, MagicMock(name="status")))

    assert message is not None
    assert upload["path"] != source and upload["file_existed"]
    assert upload["filename"] == "vp9_4s.mp4"
    video = _attribute(upload, DocumentAttributeVideo)
    assert (video.duration, video.w, video.h) == (4, 160, 120)
    assert video.supports_streaming is True
    assert upload["thumb_existed"]
    assert (upload["thumb_size"]["width"], upload["thumb_size"]["height"]) == (320, 240)
    # El convertido y la miniatura son temporales; el original se queda
    assert set(os.listdir(quiet_bot.TEMP_DIR)) == before
    assert os.path.exists(source)


@needs_ffmpeg
def test_enviar_un_mp4_compatible_lo_sube_tal_cual(quiet_bot, upload, media, make_event, run_async):
    source = media("h264_4s.mp4")

    run_async(quiet_bot.send_file_to_telegram(make_event(), source))

    assert upload["path"] == source
    assert upload["filename"] == "h264_4s.mp4"
    video = _attribute(upload, DocumentAttributeVideo)
    assert (video.duration, video.w, video.h) == (4, 160, 120)


@needs_ffmpeg
def test_enviar_un_mp3_real_lleva_su_duracion(quiet_bot, upload, media, make_event, run_async):
    run_async(quiet_bot.send_file_to_telegram(make_event(), media("audio.mp3")))

    audio = _attribute(upload, DocumentAttributeAudio)
    assert audio is not None and audio.duration == 1
