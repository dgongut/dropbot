"""Solo se convierte lo que Telegram no puede reproducir.

Antes se recodificaba todo vídeo, aunque ya fuera un MP4 H.264 + AAC que
Telegram reproduce sin problema, y eso podía tardar minutos sin necesidad.
"""

import os
import shutil
import subprocess

import pytest

from services.video_service import plan_is_noop, telegram_conversion_plan


def probe(fmt="mov,mp4,m4a,3gp,3g2,mj2", vcodec="h264", pix_fmt="yuv420p", acodec="aac", tag=None):
    streams = [{"codec_type": "video", "codec_name": vcodec, "pix_fmt": pix_fmt,
                "codec_tag_string": tag or {"h264": "avc1", "hevc": "hvc1"}.get(vcodec, "")}]
    if acodec:
        streams.append({"codec_type": "audio", "codec_name": acodec})
    return {"format": {"format_name": fmt}, "streams": streams}


def test_mp4_h264_aac_no_se_toca():
    assert plan_is_noop(telegram_conversion_plan("v.mp4", probe()))


def test_mp4_h264_sin_audio_no_se_toca():
    assert plan_is_noop(telegram_conversion_plan("v.mp4", probe(acodec=None)))


def test_mp4_h264_mp3_no_se_toca():
    assert plan_is_noop(telegram_conversion_plan("v.mp4", probe(acodec="mp3")))


def test_mkv_h264_aac_solo_cambia_contenedor():
    plan = telegram_conversion_plan("v.mkv", probe(fmt="matroska,webm"))
    assert plan == {"video": "copy", "audio": "copy", "remux": True, "hvc1": False}


def test_mov_se_reempaqueta_como_mp4():
    plan = telegram_conversion_plan("v.mov", probe())
    assert plan["video"] == "copy" and plan["remux"]


def test_mp4_h264_opus_solo_convierte_audio():
    plan = telegram_conversion_plan("v.mp4", probe(acodec="opus"))
    assert plan == {"video": "copy", "audio": "encode", "remux": False, "hvc1": False}
    assert not plan_is_noop(plan)


@pytest.mark.parametrize("vcodec,pix_fmt", [
    ("vp9", "yuv420p"),
    ("av1", "yuv420p"),
    ("hevc", "yuv422p"),
    ("h264", "yuv420p10le"),  # H.264 de 10 bits no lo decodifican muchos móviles
])
def test_video_incompatible_se_recodifica(vcodec, pix_fmt):
    plan = telegram_conversion_plan("v.mp4", probe(vcodec=vcodec, pix_fmt=pix_fmt))
    assert plan["video"] == "encode"


@pytest.mark.parametrize("pix_fmt", ["yuv420p", "yuv420p10le"])
def test_mp4_hevc_hvc1_no_se_toca(pix_fmt):
    # Lo que graba un móvil: Telegram lo reproduce tal cual
    plan = telegram_conversion_plan("v.mp4", probe(vcodec="hevc", pix_fmt=pix_fmt, acodec="mp3"))
    assert plan_is_noop(plan)


def test_mp4_hevc_hev1_se_reetiqueta_sin_recodificar(dropbot):
    plan = telegram_conversion_plan("v.mp4", probe(vcodec="hevc", tag="hev1"))
    assert plan == {"video": "copy", "audio": "copy", "remux": True, "hvc1": True}
    command = dropbot.build_ffmpeg_conversion_command("in.mp4", "out.mp4", plan=plan)
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-tag:v") + 1] == "hvc1"


def test_mkv_hevc_se_reempaqueta_como_hvc1():
    plan = telegram_conversion_plan("v.mkv", probe(fmt="matroska,webm", vcodec="hevc", tag="[0][0][0][0]"))
    assert plan["video"] == "copy" and plan["remux"] and plan["hvc1"]


def test_sin_analisis_se_convierte_entero():
    assert telegram_conversion_plan("v.mp4", None) is None
    assert not plan_is_noop(None)


def test_la_caratula_no_cuenta_como_video():
    data = probe()
    data["streams"].insert(0, {"codec_type": "video", "codec_name": "mjpeg",
                               "disposition": {"attached_pic": 1}})
    assert plan_is_noop(telegram_conversion_plan("v.mp4", data))


def test_copia_de_pistas_no_recodifica_video(dropbot):
    plan = {"video": "copy", "audio": "encode", "remux": False}
    command = dropbot.build_ffmpeg_conversion_command("in.mp4", "out.mp4", "VAAPI", "23", plan)
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"
    assert "-hwaccel" not in command
    assert command[-1] == "out.mp4"


# --- Con ffmpeg de verdad -----------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="requiere ffmpeg"
)


def make_video(path, vcodec, acodec):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=duration=1",
         "-c:v", vcodec, "-pix_fmt", "yuv420p", "-c:a", acodec, "-shortest", "-y", str(path)],
        check=True,
    )
    return str(path)


@needs_ffmpeg
def test_un_mp4_compatible_se_envia_tal_cual(quiet_bot, tmp_path, run_async, monkeypatch):
    video = make_video(tmp_path / "v.mp4", "libx264", "aac")

    def no_ffmpeg(*args, **kwargs):
        raise AssertionError("no debería lanzar ffmpeg")

    monkeypatch.setattr(quiet_bot, "build_ffmpeg_conversion_command", no_ffmpeg)
    assert run_async(quiet_bot.convert_video_to_telegram_compatible(video)) == video


@needs_ffmpeg
def test_un_mkv_h264_se_reempaqueta_sin_recodificar(quiet_bot, tmp_path, run_async):
    video = make_video(tmp_path / "v.mkv", "libx264", "libopus")

    result = run_async(quiet_bot.convert_video_to_telegram_compatible(video))
    try:
        assert result != video and result.endswith(".mp4")
        out = run_async(quiet_bot.probe_media(result))
        plan = telegram_conversion_plan(result, out)
        assert plan_is_noop(plan)
    finally:
        if result != video and os.path.exists(result):
            os.remove(result)
