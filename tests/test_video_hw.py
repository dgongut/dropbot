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


def test_vaapi_uses_full_hardware_pipeline(dropbot):
    command = dropbot.build_ffmpeg_conversion_command("input.mkv", "output.mp4", "VAAPI")

    assert command[command.index("-hwaccel") + 1] == "vaapi"
    assert command[command.index("-hwaccel_output_format") + 1] == "vaapi"
    assert command[command.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    # Los frames ya están en la GPU: sin filtro de subida a memoria de vídeo
    assert "-vf" not in command


@pytest.mark.parametrize(("hardware", "hwaccel"), [
    ("NONE", None),
    ("VAAPI", "vaapi"),
    ("NVENC", "cuda"),
    ("QSV", "qsv"),
])
def test_hardware_decode_flags_go_before_input(dropbot, hardware, hwaccel):
    command = dropbot.build_ffmpeg_conversion_command("input.mkv", "output.mp4", hardware)

    if hwaccel is None:
        assert "-hwaccel" not in command
    else:
        assert command[command.index("-hwaccel") + 1] == hwaccel
        assert command.index("-hwaccel") < command.index("-i")


@pytest.mark.parametrize(("hardware", "quality_arg"), [
    ("NONE", "-crf"),
    ("VAAPI", "-qp"),
    ("NVENC", "-cq"),
    ("QSV", "-global_quality"),
])
def test_quality_is_optional_and_encoder_specific(dropbot, hardware, quality_arg):
    without_quality = dropbot.build_ffmpeg_conversion_command(
        "input.mkv", "output.mp4", hardware
    )
    with_quality = dropbot.build_ffmpeg_conversion_command(
        "input.mkv", "output.mp4", hardware, 23
    )

    assert quality_arg not in without_quality
    assert with_quality[with_quality.index(quality_arg) + 1] == "23"
