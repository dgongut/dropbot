"""Configuración de encoders hardware para la conversión de vídeo."""

import pytest


@pytest.mark.parametrize(("hardware", "encoder"), [
    ("NONE", "libx264"),
    ("VAAPI", "h264_vaapi"),
    ("NVENC", "h264_nvenc"),
    ("QSV", "h264_qsv"),
])
def test_build_ffmpeg_command_selects_encoder(dropbot, hardware, encoder):
    command = dropbot.build_ffmpeg_conversion_command("input.mkv", "output.mp4", hardware)

    assert command[0] == "ffmpeg"
    assert command[command.index("-c:v") + 1] == encoder
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[-1] == "output.mp4"


def test_vaapi_configures_device_and_upload_filter(dropbot):
    command = dropbot.build_ffmpeg_conversion_command("input.mkv", "output.mp4", "VAAPI")

    assert command[command.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert command[command.index("-vf") + 1] == "format=nv12,hwupload"
