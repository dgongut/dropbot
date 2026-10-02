"""Temporales de descarga: nombres finales, cancelación y rescate de playlists.

Antes, cancelar una descarga borraba `*.part*` de la carpeta de destino (donde
el usuario guarda cosas como "Pelicula.part1.rar") y no tocaba TEMP_DIR, que es
donde se descarga. Los ficheros se movían con nombres calculados contra
TEMP_DIR, así que dos vídeos con el mismo título se machacaban en el destino.
"""

import asyncio
import os

import pytest


@pytest.fixture
def dirs(dropbot, tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    final = tmp_path / "final"
    temp.mkdir()
    final.mkdir()
    monkeypatch.setattr(dropbot, "TEMP_DIR", str(temp))
    return temp, final


def _touch(path, content=b"x"):
    path.write_bytes(content)
    return str(path)


# --- reserve_unique_path / copy_and_remove ----------------------------------

def test_reservar_nombre_no_devuelve_dos_veces_la_misma_ruta(tmp_path):
    from utils.file_helpers import reserve_unique_path

    first = reserve_unique_path(str(tmp_path), "video.mp4")
    second = reserve_unique_path(str(tmp_path), "video.mp4")
    third = reserve_unique_path(str(tmp_path), "video.mp4")

    assert [os.path.basename(p) for p in (first, second, third)] == [
        "video.mp4", "video (1).mp4", "video (2).mp4"
    ]


def test_una_copia_fallida_no_deja_el_destino_a_medias(tmp_path):
    from utils.file_helpers import copy_and_remove

    dst = tmp_path / "dst.mp4"
    dst.write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        copy_and_remove(str(tmp_path / "no-existe.mp4"), str(dst))
    assert not dst.exists()


def test_mover_una_descarga_no_machaca_un_fichero_existente(dropbot, dirs, run_async):
    temp, final = dirs
    _touch(final / "Video.mp4", b"el de antes")
    src = _touch(temp / "Video_temp123.mp4", b"el nuevo")

    moved = run_async(dropbot.move_download_to(src, str(final)))

    assert os.path.basename(moved) == "Video (1).mp4"
    assert (final / "Video.mp4").read_bytes() == b"el de antes"
    assert (final / "Video (1).mp4").read_bytes() == b"el nuevo"
    assert not os.path.exists(src)


# --- handle_cancel ------------------------------------------------------------

def test_cancelar_no_borra_ficheros_del_usuario(quiet_bot, dirs, run_async, monkeypatch):
    dropbot = quiet_bot
    temp, final = dirs
    monkeypatch.setitem(dropbot.DOWNLOAD_PATHS, "url_video", str(final))
    user_file = _touch(final / "Pelicula.part1.rar")
    mine = _touch(temp / "Video_temp555.f137.mp4.part")
    other = _touch(temp / "Otro_temp999.mp4.part")

    run_async(dropbot.handle_cancel(None, dropbot.ytdlp_temp_marker(555)))

    assert os.path.exists(user_file), "un .part1.rar del usuario no es un temporal"
    assert not os.path.exists(mine)
    assert os.path.exists(other), "los temporales de otra descarga no se tocan"


# --- yt-dlp: intermedios y rescate de playlist ------------------------------

@pytest.mark.parametrize("name", [
    "Video_temp1.f137.mp4",
    "Video_temp1.f251.webm",
    "Video_temp1.fdash-video=1.mp4",
    "Video_temp1.mp4.part",
    "Video_temp1.f137.mp4.part-Frag3",
    "Video_temp1.mp4.ytdl",
    "Video_temp1.temp.mp4",
])
def test_se_reconocen_los_intermedios_de_ytdlp(dropbot, name):
    assert dropbot.is_ytdlp_partial(name)


@pytest.mark.parametrize("name", [
    "Video_temp1.mp4",
    "001-Video_temp1.mkv",
    "Canción_temp1.mp3",
    "v1.final_temp1.mp4",
])
def test_un_fichero_terminado_no_es_intermedio(dropbot, name):
    assert not dropbot.is_ytdlp_partial(name)


def test_extract_file_paths_ignora_los_formatos_sueltos(dropbot):
    lines = [
        "[download] Destination: /tmp/Video_temp1.f137.mp4",
        "[download] 100% of 10.00MiB in 00:01",
        "[download] Destination: /tmp/Video_temp1.f140.m4a",
        "[download] 100% of 1.00MiB in 00:01",
        '[Merger] Merging formats into "/tmp/Video_temp1.mp4"',
    ]
    assert dropbot.extract_file_paths(lines) == ["/tmp/Video_temp1.mp4"]


def test_el_rescate_de_playlist_solo_mueve_lo_terminado_de_esa_descarga(dropbot, dirs, run_async):
    temp, final = dirs
    marker = dropbot.ytdlp_temp_marker(777)
    _touch(temp / "001-Uno_temp777.mp4")
    _touch(temp / "002-Dos_temp777.f137.mp4")
    _touch(temp / "002-Dos_temp777.f140.m4a.part")
    ajeno = _touch(temp / "Otro_temp888.mp4")
    conversion = _touch(temp / "algo_123_telegram.mp4")

    moved = run_async(dropbot.rescue_completed_playlist_files(marker, str(final)))

    assert moved == 1
    assert sorted(os.listdir(final)) == ["001-Uno.mp4"]
    assert os.path.exists(ajeno) and os.path.exists(conversion)


# --- descargas de Telegram ----------------------------------------------------

def test_limited_download_libera_su_entrada_en_active_tasks(quiet_bot, make_event, run_async, monkeypatch):
    dropbot = quiet_bot

    async def fake_download(event):
        return None

    monkeypatch.setattr(dropbot, "download_media", fake_download)

    async def scenario():
        event = make_event(event_id=4242)
        task = asyncio.create_task(dropbot.limited_download(event))
        dropbot.active_tasks[event.id] = task
        await task
        return event.id in dropbot.active_tasks

    assert run_async(scenario()) is False
