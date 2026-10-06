"""Conversión a un vídeo que Telegram reproduce, con ffmpeg de verdad.

Los vídeos se generan en el propio test con las fuentes `lavfi` de ffmpeg
(1 s, resoluciones mínimas) y el resultado se comprueba con ffprobe: códecs,
contenedor, duración, `faststart`, que no se quedan temporales en TEMP_DIR y
que ffmpeg no sigue vivo tras cancelar.

Lo que ya cubren test_conversion.py, test_video_hw.py y test_telegram_compat.py
(drenado de stderr, cancelación con un ffmpeg simulado, encoder elegido por
modo, planes sobre diccionarios de ffprobe a mano) no se repite aquí.
"""

import asyncio
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

import settings
from services.video_service import plan_is_noop, telegram_conversion_plan

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="requiere ffmpeg y ffprobe",
)


# --- Biblioteca de vídeos de prueba ----------------------------------------

def _lavfi_video(duration, size, rate):
    return ["-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={rate}"]


def _lavfi_audio(duration):
    return ["-f", "lavfi", "-i", f"sine=duration={duration}"]


H264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
X265 = ["-c:v", "libx265", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error"]
VP9 = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac"]
OPUS = ["-c:a", "libopus"]

# nombre -> (encoders necesarios, argumentos de ffmpeg sin la salida)
SPECS = {
    "h264_aac.mp4": ({"libx264"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1) + H264 + AAC),
    "h264_aac.mkv": ({"libx264"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1) + H264 + AAC),
    "h264_opus.mp4": ({"libx264", "libopus"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1) + H264 + OPUS),
    "h264_noaudio.mp4": ({"libx264"}, _lavfi_video(1, "160x120", 10) + H264),
    "h264_10bit.mp4": ({"libx264"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1)
                       + ["-c:v", "libx264", "-pix_fmt", "yuv420p10le"] + AAC),
    "hevc_hev1.mp4": ({"libx265"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1) + X265 + ["-tag:v", "hev1"] + AAC),
    "hevc_hvc1.mp4": ({"libx265"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1) + X265 + ["-tag:v", "hvc1"] + AAC),
    "hevc.mkv": ({"libx265"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1) + X265 + AAC),
    "vp9_opus.webm": ({"libvpx-vp9", "libopus"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1) + VP9 + OPUS),
    "vp9_noaudio.webm": ({"libvpx-vp9"}, _lavfi_video(1, "160x120", 10) + VP9),
    "vp9_vertical.webm": ({"libvpx-vp9", "libopus"}, _lavfi_video(1, "120x160", 10) + _lavfi_audio(1) + VP9 + OPUS),
    "vp9_444.webm": ({"libvpx-vp9", "libopus"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1)
                     + VP9[:-2] + ["-pix_fmt", "yuv444p"] + OPUS),
    "vp9_odd.webm": ({"libvpx-vp9", "libopus"}, _lavfi_video(1, "161x121", 10) + _lavfi_audio(1)
                     + VP9 + OPUS),
    "av1.mkv": ({"libsvtav1", "libopus"}, _lavfi_video(1, "160x120", 10) + _lavfi_audio(1)
                + ["-c:v", "libsvtav1", "-preset", "12", "-pix_fmt", "yuv420p"] + OPUS),
    "audio_only.mp4": (set(), _lavfi_audio(1) + AAC),
    # 301 s a 32x32: "largo" para el bot (> 5 min) y casi gratis de generar
    "long.avi": ({"mpeg4"}, _lavfi_video(301, "32x32", 5) + ["-c:v", "mpeg4"]),
    "long_h264.mkv": ({"libx264"}, _lavfi_video(301, "32x32", 1) + H264),
}


def _encoders():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return {line.split()[1] for line in out.splitlines() if len(line.split()) > 1 and line.startswith(" ")}


class MediaLibrary:
    """Genera cada vídeo la primera vez que un test lo pide."""

    def __init__(self, root):
        self.root = root
        self._encoders = None

    def __call__(self, name):
        path = self.root / name
        if path.exists():
            return str(path)
        if name == "not_media.mp4":
            path.write_text("esto no es un vídeo\n" * 50)
        elif name == "truncated.mp4":
            # Sin faststart el índice (moov) va al final: cortado, no hay índice
            whole = self.root / "whole_for_truncate.mp4"
            self._ffmpeg(_lavfi_video(1, "160x120", 10) + _lavfi_audio(1) + H264 + AAC, whole)
            path.write_bytes(whole.read_bytes()[:3000])
        else:
            needed, args = SPECS[name]
            if self._encoders is None:
                self._encoders = _encoders()
            missing = needed - self._encoders
            if missing:
                pytest.skip(f"el ffmpeg instalado no tiene {', '.join(sorted(missing))}")
            self._ffmpeg(args, path)
        return str(path)

    @staticmethod
    def _ffmpeg(args, path):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", *args, "-shortest", "-y", str(path)],
            check=True, capture_output=True,
        )


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return MediaLibrary(tmp_path_factory.mktemp("media"))


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, check=True,
    ).stdout
    return json.loads(out)


def streams(probe, kind):
    return [s for s in probe["streams"] if s["codec_type"] == kind]


def duration(probe):
    return float(probe["format"]["duration"])


def top_level_boxes(path):
    """Los átomos de primer nivel de un MP4, en orden."""
    boxes = []
    with open(path, "rb") as handle:
        while True:
            header = handle.read(8)
            if len(header) < 8:
                break
            size, kind = struct.unpack(">I4s", header)
            consumed = 8
            if size == 1:
                size = struct.unpack(">Q", handle.read(8))[0]
                consumed = 16
            boxes.append(kind.decode("latin-1"))
            if size == 0:
                break
            handle.seek(size - consumed, os.SEEK_CUR)
    return boxes


def assert_faststart(path):
    boxes = top_level_boxes(path)
    assert "moov" in boxes and "mdat" in boxes
    assert boxes.index("moov") < boxes.index("mdat"), f"sin faststart: {boxes}"


# --- Utilidades para lanzar la conversión ------------------------------------

@pytest.fixture
def conversion(quiet_bot, config_dir, run_async, monkeypatch):
    """Lanza la conversión real y registra lo que hizo.

    Expone los comandos de ffmpeg construidos, los contadores de estadísticas
    y borra al acabar el fichero convertido (que es temporal).
    """
    dropbot = quiet_bot
    os.makedirs(dropbot.TEMP_DIR, exist_ok=True)

    class Harness:
        commands = []
        calls = []
        counted = []
        results = []

        def run(self, path, status_message=None):
            before = set(os.listdir(dropbot.TEMP_DIR))
            result = run_async(dropbot.convert_video_to_telegram_compatible(path, status_message))
            if result is not None and result != path:
                self.results.append(result)
            self.new_temp_files = set(os.listdir(dropbot.TEMP_DIR)) - before - {
                os.path.basename(r) for r in self.results
            }
            return result

    harness = Harness()
    harness.dropbot = dropbot
    original_build = dropbot.build_ffmpeg_conversion_command

    def build(input_path, output_path, hardware=None, quality=None, plan=None):
        harness.calls.append({"hardware": hardware, "quality": quality, "plan": plan})
        command = original_build(input_path, output_path, hardware, quality, plan)
        harness.commands.append(command)
        return command

    harness.original_build = original_build
    monkeypatch.setattr(dropbot, "build_ffmpeg_conversion_command", build)
    monkeypatch.setattr(dropbot.stats, "count", harness.counted.append)
    yield harness
    for result in harness.results:
        if os.path.exists(result):
            os.remove(result)


def _video_codec_arg(command):
    return command[command.index("-c:v") + 1]


# --- Qué conversión se hace según el fichero ---------------------------------

@needs_ffmpeg
@pytest.mark.parametrize(("name", "expected"), [
    ("h264_aac.mp4", {"video": "copy", "audio": "copy", "remux": False, "hvc1": False}),
    ("h264_noaudio.mp4", {"video": "copy", "audio": None, "remux": False, "hvc1": False}),
    ("h264_aac.mkv", {"video": "copy", "audio": "copy", "remux": True, "hvc1": False}),
    ("h264_opus.mp4", {"video": "copy", "audio": "encode", "remux": False, "hvc1": False}),
    ("hevc_hvc1.mp4", {"video": "copy", "audio": "copy", "remux": False, "hvc1": True}),
    ("hevc_hev1.mp4", {"video": "copy", "audio": "copy", "remux": True, "hvc1": True}),
    ("hevc.mkv", {"video": "copy", "audio": "copy", "remux": True, "hvc1": True}),
    ("vp9_opus.webm", {"video": "encode", "audio": "encode", "remux": True, "hvc1": False}),
    ("vp9_noaudio.webm", {"video": "encode", "audio": None, "remux": True, "hvc1": False}),
    ("h264_10bit.mp4", {"video": "encode", "audio": "copy", "remux": False, "hvc1": False}),
    ("av1.mkv", {"video": "encode", "audio": "encode", "remux": True, "hvc1": False}),
])
def test_el_plan_sobre_ficheros_reales(dropbot, media, run_async, name, expected):
    path = media(name)
    assert telegram_conversion_plan(path, run_async(dropbot.probe_media(path))) == expected


@needs_ffmpeg
@pytest.mark.parametrize("name", ["audio_only.mp4", "not_media.mp4", "truncated.mp4"])
def test_sin_pista_de_video_analizable_no_hay_plan(dropbot, media, run_async, name):
    path = media(name)
    plan = telegram_conversion_plan(path, run_async(dropbot.probe_media(path)))
    assert plan is None
    assert not plan_is_noop(plan)


@needs_ffmpeg
def test_un_mp4_compatible_con_extension_en_mayusculas_no_se_toca(dropbot, media, run_async, tmp_path):
    path = tmp_path / "VIDEO.MP4"
    shutil.copy(media("h264_aac.mp4"), path)
    assert plan_is_noop(telegram_conversion_plan(str(path), run_async(dropbot.probe_media(str(path)))))


@needs_ffmpeg
def test_un_mp4_con_otra_extension_se_reempaqueta(dropbot, media, run_async, tmp_path):
    """El contenedor es MP4 pero la extensión no: Telegram decide por el nombre."""
    path = tmp_path / "video.mov"
    shutil.copy(media("h264_aac.mp4"), path)
    plan = telegram_conversion_plan(str(path), run_async(dropbot.probe_media(str(path))))
    assert plan["remux"] is True and plan["video"] == "copy"


# --- Resultado de la conversión (comprobado con ffprobe) ---------------------

@needs_ffmpeg
@pytest.mark.parametrize("name", ["h264_aac.mp4", "h264_noaudio.mp4", "hevc_hvc1.mp4"])
def test_lo_que_telegram_ya_reproduce_se_devuelve_tal_cual(conversion, media, name):
    path = media(name)
    assert conversion.run(path) == path
    assert conversion.commands == [], "no debería lanzar ffmpeg"
    assert conversion.new_temp_files == set()


@needs_ffmpeg
def test_mkv_h264_se_reempaqueta_sin_recodificar_y_con_faststart(conversion, media):
    source = media("h264_aac.mkv")
    result = conversion.run(source)

    assert result != source and result.endswith("_telegram.mp4")
    assert result.startswith(conversion.dropbot.TEMP_DIR)
    assert _video_codec_arg(conversion.commands[0]) == "copy"
    assert conversion.counted == ["convert_remux"]
    probe = ffprobe(result)
    assert "mp4" in probe["format"]["format_name"].split(",")
    assert [s["codec_name"] for s in streams(probe, "video")] == ["h264"]
    assert [s["codec_name"] for s in streams(probe, "audio")] == ["aac"]
    # Mismo vídeo: mismos fotogramas y misma duración
    assert streams(probe, "video")[0]["nb_frames"] == "10"
    assert duration(probe) == pytest.approx(duration(ffprobe(source)), abs=0.1)
    assert_faststart(result)
    assert plan_is_noop(telegram_conversion_plan(result, probe))
    assert conversion.new_temp_files == set()


@needs_ffmpeg
def test_mp4_con_audio_opus_solo_convierte_el_audio(conversion, media):
    source = media("h264_opus.mp4")
    result = conversion.run(source)

    command = conversion.commands[0]
    assert _video_codec_arg(command) == "copy"
    assert command[command.index("-c:a") + 1] == "aac"
    probe = ffprobe(result)
    assert [s["codec_name"] for s in streams(probe, "video")] == ["h264"]
    assert [s["codec_name"] for s in streams(probe, "audio")] == ["aac"]
    assert duration(probe) == pytest.approx(1.0, abs=0.15)
    assert_faststart(result)


@needs_ffmpeg
@pytest.mark.parametrize("name", ["hevc_hev1.mp4", "hevc.mkv"])
def test_hevc_se_reetiqueta_como_hvc1_sin_recodificar(conversion, media, name):
    """Los reproductores de Apple solo aceptan HEVC en MP4 con la etiqueta hvc1."""
    result = conversion.run(media(name))

    assert _video_codec_arg(conversion.commands[0]) == "copy"
    probe = ffprobe(result)
    video = streams(probe, "video")[0]
    assert video["codec_name"] == "hevc"
    assert video["codec_tag_string"] == "hvc1"
    assert_faststart(result)
    assert plan_is_noop(telegram_conversion_plan(result, probe))


@needs_ffmpeg
@pytest.mark.parametrize("name", ["vp9_opus.webm", "av1.mkv"])
def test_un_codec_que_telegram_no_reproduce_se_recodifica_a_h264_aac(conversion, media, name):
    source = media(name)
    result = conversion.run(source)

    assert _video_codec_arg(conversion.commands[0]) == "libx264"
    assert conversion.counted == ["convert_none"]
    probe = ffprobe(result)
    video = streams(probe, "video")[0]
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 160, 120)
    assert video["pix_fmt"] == "yuv420p"
    assert [s["codec_name"] for s in streams(probe, "audio")] == ["aac"]
    assert duration(probe) == pytest.approx(duration(ffprobe(source)), abs=0.15)
    assert_faststart(result)
    assert plan_is_noop(telegram_conversion_plan(result, probe))
    assert conversion.new_temp_files == set()


@needs_ffmpeg
def test_un_video_sin_audio_sigue_sin_audio_tras_convertir(conversion, media):
    result = conversion.run(media("vp9_noaudio.webm"))

    probe = ffprobe(result)
    assert [s["codec_name"] for s in streams(probe, "video")] == ["h264"]
    assert streams(probe, "audio") == []


@needs_ffmpeg
def test_un_video_vertical_conserva_la_orientacion(conversion, media):
    result = conversion.run(media("vp9_vertical.webm"))

    video = streams(ffprobe(result), "video")[0]
    assert (video["width"], video["height"]) == (120, 160)


@needs_ffmpeg
@pytest.mark.parametrize("name", ["vp9_444.webm", "h264_10bit.mp4"])
def test_la_recodificacion_produce_siempre_h264_420_de_8_bits(conversion, media, name):
    result = conversion.run(media(name))

    probe = ffprobe(result)
    video = streams(probe, "video")[0]
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert plan_is_noop(telegram_conversion_plan(result, probe))


@needs_ffmpeg
def test_un_video_de_dimensiones_impares_tambien_se_convierte(conversion, media):
    source = media("vp9_odd.webm")
    result = conversion.run(source)

    assert result != source
    video = streams(ffprobe(result), "video")[0]
    assert (video["codec_name"], video["pix_fmt"]) == ("h264", "yuv420p")


@needs_ffmpeg
@pytest.mark.parametrize("name", ["not_media.mp4", "truncated.mp4"])
def test_un_fichero_que_no_se_puede_convertir_se_devuelve_tal_cual(conversion, media, name):
    source = media(name)
    assert conversion.run(source) == source
    assert len(conversion.commands) == 1, "en software no hay reintento"
    assert conversion.new_temp_files == set(), "no puede quedar un convertido a medias"


@needs_ffmpeg
def test_un_mp4_solo_de_audio_no_rompe_la_conversion(conversion, media):
    source = media("audio_only.mp4")
    result = conversion.run(source)

    assert result is not None and os.path.exists(result)
    probe = ffprobe(result)
    assert streams(probe, "video") == []
    assert [s["codec_name"] for s in streams(probe, "audio")] == ["aac"]


@needs_ffmpeg
def test_la_calidad_configurada_llega_a_ffmpeg(conversion, media):
    settings.put("video.quality", 35)

    result = conversion.run(media("vp9_noaudio.webm"))

    command = conversion.commands[0]
    assert command[command.index("-crf") + 1] == "35"
    assert streams(ffprobe(result), "video")[0]["codec_name"] == "h264"


# --- Reintento en software ----------------------------------------------------

@needs_ffmpeg
@pytest.mark.parametrize("hardware", ["VAAPI", "NVENC", "QSV"])
def test_si_falla_el_encoder_hardware_se_reintenta_en_software(conversion, media, hardware):
    """En la imagen no hay GPU: el encoder hardware falla de verdad."""
    settings.put("video.hw", hardware)
    settings.put("video.quality", 30)
    source = media("vp9_opus.webm")

    result = conversion.run(source)

    assert [call["hardware"] for call in conversion.calls] == [hardware, "NONE"]
    assert _video_codec_arg(conversion.commands[0]) != "libx264"
    software = conversion.commands[1]
    assert _video_codec_arg(software) == "libx264"
    assert "-hwaccel" not in software
    assert software[software.index("-crf") + 1] == "30"
    assert conversion.counted == [f"convert_{hardware.lower()}", "convert_fallback", "convert_none"]
    probe = ffprobe(result)
    assert streams(probe, "video")[0]["codec_name"] == "h264"
    assert [s["codec_name"] for s in streams(probe, "audio")] == ["aac"]
    assert duration(probe) == pytest.approx(1.0, abs=0.15)
    assert_faststart(result)
    assert conversion.new_temp_files == set(), "el intento fallido no puede dejar restos"


@needs_ffmpeg
def test_con_hardware_configurado_la_copia_de_pistas_no_usa_la_gpu(conversion, media):
    settings.put("video.hw", "VAAPI")

    result = conversion.run(media("h264_aac.mkv"))

    assert len(conversion.commands) == 1
    assert "-hwaccel" not in conversion.commands[0]
    assert conversion.counted == ["convert_remux"]
    assert streams(ffprobe(result), "video")[0]["codec_name"] == "h264"


@needs_ffmpeg
def test_si_falla_la_copia_de_pistas_se_recodifica_entero(conversion, media, monkeypatch):
    dropbot = conversion.dropbot
    traced = dropbot.build_ffmpeg_conversion_command

    def broken_copy(input_path, output_path, hardware=None, quality=None, plan=None):
        command = traced(input_path, output_path, hardware, quality, plan)
        if plan is not None and plan["video"] == "copy":
            # Un filtro inexistente: ffmpeg falla de verdad al copiar
            command[-1:-1] = ["-bsf:v", "filtro_que_no_existe"]
        return command

    monkeypatch.setattr(dropbot, "build_ffmpeg_conversion_command", broken_copy)

    result = conversion.run(media("h264_aac.mkv"))

    assert conversion.calls[0]["plan"]["video"] == "copy"
    assert conversion.calls[1]["plan"] is None
    assert _video_codec_arg(conversion.commands[1]) == "libx264"
    assert conversion.counted == ["convert_remux", "convert_fallback", "convert_none"]
    probe = ffprobe(result)
    assert streams(probe, "video")[0]["codec_name"] == "h264"
    assert_faststart(result)
    assert conversion.new_temp_files == set()


@needs_ffmpeg
def test_si_tambien_falla_en_software_se_devuelve_el_original_sin_bucles(conversion, media):
    settings.put("video.hw", "VAAPI")
    source = media("not_media.mp4")

    assert conversion.run(source) == source
    assert [call["hardware"] for call in conversion.calls] == ["VAAPI", "NONE"]
    assert conversion.new_temp_files == set()


def test_sin_ffmpeg_instalado_se_envia_el_original(conversion, media_file, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))
    source = media_file("v.webm")

    assert conversion.run(source) == source
    assert conversion.new_temp_files == set()


# --- Mensajes de estado, cancelación y "enviar original" ---------------------

def _button_data(kwargs):
    rows = kwargs.get("buttons") or []
    flat = []
    for row in rows:
        flat.extend(row if isinstance(row, list) else [row])
    return [button.type.data.decode() for button in flat]


def _is_progress_bar(text):
    return "█" in text or "░" in text


@needs_ffmpeg
def test_los_mensajes_de_estado_de_una_conversion_corta(conversion, media, sent_messages):
    dropbot = conversion.dropbot
    conversion.run(media("vp9_noaudio.webm"), MagicMock(name="status"))

    edits = [(text, kwargs) for kind, text, kwargs in sent_messages if kind == "edit"]
    first_text, first_kwargs = edits[0]
    assert first_text == dropbot.get_text("converting_video_progress")
    data = _button_data(first_kwargs)
    assert len(data) == 1 and data[0].startswith("cancelconv:conv_")
    assert edits[-1][0] == dropbot.get_text("preparing_send")


@needs_ffmpeg
def test_un_video_largo_ofrece_enviar_el_original(conversion, media, sent_messages):
    dropbot = conversion.dropbot
    conversion.run(media("long.avi"), MagicMock(name="status"))

    first_text, first_kwargs = next((t, k) for kind, t, k in sent_messages if kind == "edit")
    assert first_text == dropbot.get_text("converting_video_long_progress", 5)
    data = _button_data(first_kwargs)
    conversion_id = data[0].split(":", 1)[1]
    assert data == [f"cancelconv:{conversion_id}", f"sendoriginal:{conversion_id}"]
    # Las barras de progreso siguen ofreciendo los dos botones
    bars = [k for kind, t, k in sent_messages if kind == "edit" and _is_progress_bar(t)]
    assert bars and all(len(_button_data(k)) == 2 for k in bars)


@needs_ffmpeg
def test_un_video_largo_que_solo_se_reempaqueta_no_ofrece_el_original(conversion, media, sent_messages):
    """Copiar pistas tarda segundos: no tiene sentido ofrecer saltárselo."""
    conversion.run(media("long_h264.mkv"), MagicMock(name="status"))

    first_kwargs = next(k for kind, t, k in sent_messages if kind == "edit")
    assert [d.split(":")[0] for d in _button_data(first_kwargs)] == ["cancelconv"]


def _throttle(dropbot, monkeypatch):
    """ffmpeg procesa a 50x tiempo real y avisa del progreso cada 0,1 s.

    Así la conversión de un vídeo de 5 min dura unos segundos (y no
    milisegundos) y da tiempo a pulsar "cancelar" mientras ffmpeg trabaja.
    """
    traced = dropbot.build_ffmpeg_conversion_command

    def slow(*args, **kwargs):
        command = traced(*args, **kwargs)
        command[-1:-1] = ["-vf", "realtime=speed=50"]
        return [command[0], "-stats_period", "0.1", *command[1:]]

    monkeypatch.setattr(dropbot, "build_ffmpeg_conversion_command", slow)


async def _wait_for_progress(dropbot, sent_messages):
    """Espera a la primera barra de progreso y devuelve (id, proceso)."""
    for _ in range(1000):
        ids = [k for k in dropbot.active_tasks
               if str(k).startswith("conv_") and not str(k).endswith("_original_path")]
        if ids and any(kind == "edit" and _is_progress_bar(t) for kind, t, _ in sent_messages):
            return ids[0], dropbot.active_tasks[ids[0]]
        await asyncio.sleep(0.01)
    raise AssertionError("la conversión no ha llegado a informar de progreso")


def _outputs_for(dropbot, source):
    base = os.path.splitext(os.path.basename(source))[0]
    return glob.glob(os.path.join(dropbot.TEMP_DIR, f"{base}_*_telegram.mp4"))


@needs_ffmpeg
def test_el_boton_cancelar_para_ffmpeg_y_borra_el_parcial(
    conversion, media, sent_messages, make_event, run_async, monkeypatch
):
    dropbot = conversion.dropbot
    _throttle(dropbot, monkeypatch)
    source = media("long.avi")

    async def scenario():
        task = asyncio.ensure_future(
            dropbot.convert_video_to_telegram_compatible(source, MagicMock(name="status"))
        )
        conversion_id, proc = await _wait_for_progress(dropbot, sent_messages)
        await dropbot.handle_cancel_conversion(make_event(groups=(conversion_id.encode(),)))
        return await asyncio.wait_for(task, timeout=20), conversion_id, proc

    result, conversion_id, proc = run_async(scenario())

    assert result is None
    assert proc.returncode is not None, "ffmpeg no puede quedarse vivo"
    assert _outputs_for(dropbot, source) == []
    assert dropbot.get_text("conversion_cancelled") in [t for _, t, _ in sent_messages]
    assert conversion_id not in dropbot.active_tasks
    assert conversion_id not in dropbot.cancelled_conversions


@needs_ffmpeg
def test_enviar_el_original_corta_la_conversion_y_devuelve_el_original(
    conversion, media, sent_messages, make_event, run_async, monkeypatch
):
    dropbot = conversion.dropbot
    _throttle(dropbot, monkeypatch)
    source = media("long.avi")

    async def scenario():
        task = asyncio.ensure_future(
            dropbot.convert_video_to_telegram_compatible(source, MagicMock(name="status"))
        )
        conversion_id, proc = await _wait_for_progress(dropbot, sent_messages)
        await dropbot.handle_send_original(make_event(groups=(conversion_id.encode(),)))
        return await asyncio.wait_for(task, timeout=20), conversion_id, proc

    result, conversion_id, proc = run_async(scenario())

    assert result == source
    assert proc.returncode is not None
    assert _outputs_for(dropbot, source) == []
    assert sent_messages[-1][:2] == ("edit", dropbot.get_text("preparing_send"))
    assert conversion_id not in dropbot.send_original_requests
    assert conversion_id not in dropbot.cancelled_conversions


@needs_ffmpeg
def test_cancelar_la_tarea_mata_ffmpeg_y_borra_el_parcial(
    conversion, media, sent_messages, run_async, monkeypatch
):
    """Cancelar la tarea que envía (no el botón) tampoco deja ffmpeg vivo."""
    dropbot = conversion.dropbot
    _throttle(dropbot, monkeypatch)
    source = media("long.avi")

    async def scenario():
        task = asyncio.ensure_future(
            dropbot.convert_video_to_telegram_compatible(source, MagicMock(name="status"))
        )
        _, proc = await _wait_for_progress(dropbot, sent_messages)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return proc

    proc = run_async(scenario())

    assert proc.returncode is not None
    assert _outputs_for(dropbot, source) == []


@needs_ffmpeg
def test_si_falla_editar_el_progreso_tras_cancelar_se_para(
    conversion, media, sent_messages, run_async, monkeypatch
):
    """El mensaje de estado ya no se puede editar porque se canceló."""
    dropbot = conversion.dropbot
    _throttle(dropbot, monkeypatch)
    source = media("long.avi")
    edit = dropbot.safe_edit

    async def failing_edit(message, text=None, **kwargs):
        if _is_progress_bar(str(text)):
            for key in list(dropbot.active_tasks):
                if str(key).startswith("conv_"):
                    dropbot.cancelled_conversions.add(key)
            raise RuntimeError("MessageNotModified")
        return await edit(message, text, **kwargs)

    monkeypatch.setattr(dropbot, "safe_edit", failing_edit)

    result = run_async(dropbot.convert_video_to_telegram_compatible(source, MagicMock(name="status")))

    assert result is None
    assert _outputs_for(dropbot, source) == []


def _explode_at(dropbot, monkeypatch, marker):
    """Hace que la conversión falle de forma inesperada al llegar a `marker`."""
    original_debug = dropbot.debug

    def debug(message, *args, **kwargs):
        if marker in str(message):
            raise RuntimeError(f"fallo inesperado en {marker!r}")
        return original_debug(message, *args, **kwargs)

    monkeypatch.setattr(dropbot, "debug", debug)


@needs_ffmpeg
def test_un_fallo_inesperado_con_ffmpeg_en_marcha_lo_mata(
    conversion, media, run_async, monkeypatch
):
    dropbot = conversion.dropbot
    _throttle(dropbot, monkeypatch)
    _explode_at(dropbot, monkeypatch, "Starting progress reading")
    spawned = []
    create = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await create(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    source = media("long.avi")

    async def scenario():
        result = await dropbot.convert_video_to_telegram_compatible(source)
        ffmpeg = spawned[-1]
        code = await asyncio.wait_for(ffmpeg.wait(), timeout=5)
        return result, code

    result, code = run_async(scenario())

    assert result == source
    assert code != 0, "ffmpeg tenía que morir, no terminar la conversión"
    assert _outputs_for(dropbot, source) == []


@needs_ffmpeg
def test_un_fallo_inesperado_tras_convertir_borra_el_convertido(conversion, media, monkeypatch):
    dropbot = conversion.dropbot
    _explode_at(dropbot, monkeypatch, "Waiting for process completion")
    source = media("vp9_noaudio.webm")

    assert conversion.run(source) == source
    assert conversion.new_temp_files == set()


def test_una_excepcion_inesperada_devuelve_el_original(conversion, media_file, monkeypatch):
    dropbot = conversion.dropbot

    async def plan_ok(path):
        return None

    def explode(*args, **kwargs):
        raise RuntimeError("fallo inesperado")

    monkeypatch.setattr(dropbot, "probe_media", plan_ok)
    monkeypatch.setattr(dropbot, "build_ffmpeg_conversion_command", explode)
    source = media_file("v.webm")

    assert conversion.run(source) == source


# --- build_ffmpeg_conversion_command (lo que no cubre test_video_hw.py) ------

def test_sin_modo_explicito_usa_el_ajuste(dropbot, config_dir):
    settings.put("video.hw", "NVENC")
    command = dropbot.build_ffmpeg_conversion_command("in.webm", "out.mp4")
    assert _video_codec_arg(command) == "h264_nvenc"
    assert command[command.index("-hwaccel") + 1] == "cuda"


def test_el_modo_se_normaliza(dropbot):
    command = dropbot.build_ffmpeg_conversion_command("in.webm", "out.mp4", "  qsv ")
    assert _video_codec_arg(command) == "h264_qsv"
    assert command[command.index("-qsv_device") + 1] == "/dev/dri/renderD128"


def test_un_modo_desconocido_es_un_error(dropbot):
    with pytest.raises(ValueError):
        dropbot.build_ffmpeg_conversion_command("in.webm", "out.mp4", "AMF")


@pytest.mark.parametrize("quality", [None, "", "   "])
def test_sin_calidad_no_se_pasa_ningun_parametro_de_calidad(dropbot, quality):
    command = dropbot.build_ffmpeg_conversion_command("in.webm", "out.mp4", "NONE", quality)
    assert "-crf" not in command
    assert command[-1] == "out.mp4"


def test_la_calidad_nvenc_completa_va_antes_de_la_salida(dropbot):
    command = dropbot.build_ffmpeg_conversion_command("in.webm", "out.mp4", "NVENC", 28)
    start = command.index("-rc:v")
    assert command[start:start + 7] == ["-rc:v", "vbr", "-cq", "28", "-b:v", "0", "out.mp4"]


@pytest.mark.parametrize("hardware", ["NONE", "VAAPI", "NVENC", "QSV"])
def test_todos_los_modos_piden_faststart_y_progreso(dropbot, hardware):
    command = dropbot.build_ffmpeg_conversion_command("in.webm", "out.mp4", hardware, 23)
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert command[command.index("-progress") + 1] == "pipe:1"
    assert "-y" in command
    assert command[command.index("-i") + 1] == "in.webm"


def test_la_copia_de_pistas_hevc_reetiqueta_y_mapea_solo_video_y_audio(dropbot):
    plan = {"video": "copy", "audio": "copy", "remux": True, "hvc1": True}
    command = dropbot.build_ffmpeg_conversion_command("in.mkv", "out.mp4", "NONE", 23, plan)

    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert command[command.index("-c:a") + 1] == "copy"
    maps = [command[i + 1] for i, arg in enumerate(command) if arg == "-map"]
    assert maps == ["0:V:0", "0:a:0?"]
    # La calidad no aplica a una copia
    assert "-crf" not in command
    assert command[command.index("-movflags") + 1] == "+faststart"


def test_un_plan_que_recodifica_usa_el_encoder_del_modo(dropbot):
    plan = {"video": "encode", "audio": "copy", "remux": True, "hvc1": False}
    command = dropbot.build_ffmpeg_conversion_command("in.webm", "out.mp4", "VAAPI", None, plan)
    assert _video_codec_arg(command) == "h264_vaapi"


# --- Lectura de la salida de los subprocesos ---------------------------------

def _reader(data):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _lines(run_async, dropbot, data, chunk_size=4096):
    async def collect():
        return [line async for line in dropbot.iter_progress_lines(_reader(data), chunk_size)]
    return run_async(collect())


def test_las_lineas_se_parten_por_retorno_de_carro(dropbot, run_async):
    data = b" 10% [=>   ]\r 50% [==>  ]\r100% [=====]\nhecho"
    assert _lines(run_async, dropbot, data) == [" 10% [=>   ]", " 50% [==>  ]", "100% [=====]", "hecho"]


def test_una_linea_partida_entre_lecturas_llega_entera(dropbot, run_async):
    data = "año=1\rcañón=2\n".encode()
    assert _lines(run_async, dropbot, data, chunk_size=1) == ["año=1", "cañón=2"]


def test_una_linea_enorme_sin_saltos_no_rompe_la_lectura(dropbot, run_async):
    """readline() fallaba pasados 64 KiB sin salto de línea."""
    data = b"x" * (200 * 1024)
    assert _lines(run_async, dropbot, data) == ["x" * (200 * 1024)]


def test_los_bytes_invalidos_se_sustituyen(dropbot, run_async):
    assert _lines(run_async, dropbot, b"ok\xff\n") == ["ok�"]


def test_una_salida_vacia_no_da_lineas(dropbot, run_async):
    assert _lines(run_async, dropbot, b"") == []


def test_el_drenado_de_stderr_acumula_todo_y_termina(dropbot, run_async):
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.stderr.write('a' * 70000 + 'fin')",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        chunks = []
        await asyncio.wait_for(dropbot.accumulate_process_stderr(proc, chunks), timeout=10)
        await proc.wait()
        return b"".join(chunks)

    assert run_async(scenario()) == b"a" * 70000 + b"fin"


def test_un_error_al_drenar_stderr_no_se_propaga(dropbot, run_async):
    class BrokenStderr:
        async def read(self, size):
            raise OSError("tubería rota")

    chunks = [b"previo"]
    proc = MagicMock(stderr=BrokenStderr())
    run_async(dropbot.accumulate_process_stderr(proc, chunks))
    assert chunks == [b"previo"]


# --- _stop_process -------------------------------------------------------------

def test_parar_sin_proceso_no_hace_nada(dropbot):
    dropbot._stop_process(None)


def test_parar_un_proceso_terminado_no_lo_mata(dropbot):
    proc = MagicMock(returncode=0)
    dropbot._stop_process(proc)
    proc.kill.assert_not_called()


def test_parar_un_proceso_que_acaba_de_morir_no_falla(dropbot):
    proc = MagicMock(returncode=None)
    proc.kill.side_effect = ProcessLookupError
    dropbot._stop_process(proc)
    proc.kill.assert_called_once()


def test_parar_un_proceso_vivo_lo_mata(dropbot, run_async):
    async def scenario():
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
        dropbot._stop_process(proc)
        return await asyncio.wait_for(proc.wait(), timeout=10)

    assert run_async(scenario()) != 0
