"""Temporales de descarga: nombres finales, cancelación y rescate de playlists.

Antes, cancelar una descarga borraba `*.part*` de la carpeta de destino (donde
el usuario guarda cosas como "Pelicula.part1.rar") y no tocaba TEMP_DIR, que es
donde se descarga. Los ficheros se movían con nombres calculados contra
TEMP_DIR, así que dos vídeos con el mismo título se machacaban en el destino.
"""

import asyncio
import errno
import os
import shutil

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


# --- reserve_unique_path / copy_and_remove / move_to_unique_path ------------

def test_reservar_nombre_no_devuelve_dos_veces_la_misma_ruta(tmp_path):
    from utils.file_helpers import reserve_unique_path

    first = reserve_unique_path(str(tmp_path), "video.mp4")
    second = reserve_unique_path(str(tmp_path), "video.mp4")
    third = reserve_unique_path(str(tmp_path), "video.mp4")

    assert [os.path.basename(p) for p in (first, second, third)] == [
        "video.mp4", "video (1).mp4", "video (2).mp4"
    ]


def test_reservar_un_nombre_de_255_bytes_repetido_cabe_con_el_sufijo(tmp_path):
    from utils.file_helpers import reserve_unique_path

    name = "ñ" * 125 + ".pdf"  # 254 bytes
    first = reserve_unique_path(str(tmp_path), name)
    second = reserve_unique_path(str(tmp_path), name)

    assert os.path.basename(first) == name
    assert os.path.basename(second) == "ñ" * 123 + " (1).pdf"


def test_reservar_repetido_un_nombre_de_255_bytes_sin_extension_no_falla(tmp_path):
    """En "1. Introducción…" splitext toma por extensión todo lo que sigue al
    punto: con el sufijo no quedaba sitio para la base, el nombre pasaba de 255
    bytes y daba ENAMETOOLONG. La descarga, ya entera, se perdía."""
    from utils.file_helpers import reserve_unique_path

    name = "1. " + "a" * 252  # 255 bytes
    first = reserve_unique_path(str(tmp_path), name)
    second = reserve_unique_path(str(tmp_path), name)

    assert os.path.basename(first) == name
    assert os.path.basename(second) == "1. " + "a" * 248 + " (1)"


def test_una_copia_fallida_no_deja_el_destino_a_medias(tmp_path):
    from utils.file_helpers import copy_and_remove

    dst = tmp_path / "dst.mp4"
    dst.write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        copy_and_remove(str(tmp_path / "no-existe.mp4"), str(dst))
    assert not dst.exists()


def _between_devices(monkeypatch, src):
    """Como de la capa del contenedor a un volumen: renombrar `src` da EXDEV."""
    real_replace = os.replace

    def replace(old, new):
        if old == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(old, new)

    monkeypatch.setattr(os, "replace", replace)


def test_entre_dispositivos_el_nombre_final_no_se_llena_a_medias(tmp_path, monkeypatch):
    """Una parada a mitad de copia no puede dejar un fichero truncado con el
    nombre final. Con un oculto basta: antes la copia iba a un segundo oculto
    dentro del primero, con una reserva de más que nadie podía borrar."""
    from utils.file_helpers import PARTIAL_SUFFIX, move_to_unique_path, partial_name

    final = tmp_path / "final"
    final.mkdir()
    src = _touch(tmp_path / "descarga", b"x" * 1000)
    _between_devices(monkeypatch, src)
    while_copying = []
    real_copy = shutil.copyfileobj

    def copy(source, target, *args):
        real_copy(source, target, *args)
        while_copying.append(sorted(os.listdir(final)))

    monkeypatch.setattr(shutil, "copyfileobj", copy)

    moved = move_to_unique_path(src, str(final), "Pelicula.mkv")

    (listing,) = while_copying
    assert len(listing) == 1, f"mientras se copia solo hay un oculto: {listing}"
    assert listing[0].startswith(partial_name("")) and listing[0].endswith(PARTIAL_SUFFIX)
    assert os.listdir(final) == ["Pelicula.mkv"] and moved == str(final / "Pelicula.mkv")
    assert (final / "Pelicula.mkv").read_bytes() == b"x" * 1000
    assert not os.path.exists(src)


def test_si_borran_el_origen_a_mitad_de_copia_no_queda_ningun_oculto(tmp_path, monkeypatch):
    """Al cancelar, remove_temp_files borra el temporal aunque el hilo ya lo
    esté copiando. Al acabar, borrar el origen fallaba y el oculto, con el
    contenido entero, se quedaba en la carpeta hasta el siguiente arranque."""
    from utils.file_helpers import move_to_unique_path

    final = tmp_path / "final"
    final.mkdir()
    src = _touch(tmp_path / "descarga", b"x" * 1000)
    _between_devices(monkeypatch, src)
    real_copy = shutil.copyfileobj

    def copy(source, target, *args):
        real_copy(source, target, *args)
        os.remove(src)  # lo que hace remove_temp_files al cancelar

    monkeypatch.setattr(shutil, "copyfileobj", copy)

    moved = move_to_unique_path(src, str(final), "Video.mp4")

    assert os.listdir(final) == ["Video.mp4"] and moved == str(final / "Video.mp4")
    assert (final / "Video.mp4").read_bytes() == b"x" * 1000


def test_entre_dispositivos_la_copia_no_sigue_un_enlace_puesto_en_su_lugar(tmp_path, monkeypatch):
    """La copia reservaba su oculto y luego lo abría otra vez por el nombre: en
    una carpeta compartida, otro podía cambiarlo antes por un enlace, y el bot
    (que va como root) escribía la copia en su destino."""
    from utils import file_helpers

    final = tmp_path / "final"
    final.mkdir()
    victim = _touch(tmp_path / "settings.json", b"{}")
    src = _touch(tmp_path / "descarga", b"x" * 1000)
    _between_devices(monkeypatch, src)
    real_reserve = file_helpers.reserve_unique_path

    def reserve_and_swap(directory, filename, *args, **kwargs):
        reserved = real_reserve(directory, filename, *args, **kwargs)
        path = reserved[0] if isinstance(reserved, tuple) else reserved
        # Lo que haría el otro contenedor en cuanto aparece el oculto
        os.remove(path)
        os.symlink(victim, path)
        return reserved

    monkeypatch.setattr(file_helpers, "reserve_unique_path", reserve_and_swap)

    with pytest.raises(FileNotFoundError):
        file_helpers.move_to_unique_path(src, str(final), "Pelicula.mkv")

    assert (tmp_path / "settings.json").read_bytes() == b"{}"
    assert os.listdir(final) == [], "ni el enlace ni el oculto se quedan"
    assert os.path.exists(src), "la descarga sigue donde estaba"


def test_no_mueve_lo_que_no_es_un_fichero(tmp_path):
    """Un enlace a un fichero del bot no se lleva a la carpeta de descargas."""
    from utils.file_helpers import copy_and_remove, move_to_unique_path, reserve_unique_path

    target = _touch(tmp_path / "settings.json", b"ajustes")
    link = tmp_path / "descarga.mp4"
    os.symlink(target, link)
    final = tmp_path / "final"
    final.mkdir()
    dst = reserve_unique_path(str(final), "descarga.mp4")

    with pytest.raises(OSError):
        copy_and_remove(str(link), dst)
    with pytest.raises(OSError):
        move_to_unique_path(str(link), str(final), "descarga.mp4")

    assert os.listdir(final) == []
    assert (tmp_path / "settings.json").read_bytes() == b"ajustes"


def test_al_arrancar_solo_se_borran_los_ocultos_a_medias(tmp_path):
    from utils.file_helpers import remove_partial_files

    # El bot de los tests es el 123456 (ver conftest)
    for name in (".123456.dl_1_1.dropbot-part", ".123456.copy (2).dropbot-part", ".123456.move.dropbot-part"):
        _touch(tmp_path / name)
    kept = [".oculto", "Pelicula.part1.rar", "visible.dropbot-part", "peli.mkv"]
    for name in kept:
        _touch(tmp_path / name)
    (tmp_path / ".123456.carpeta.dropbot-part").mkdir()

    remove_partial_files(str(tmp_path))

    assert sorted(os.listdir(tmp_path)) == sorted(kept + [".123456.carpeta.dropbot-part"])


def test_al_arrancar_no_se_borra_lo_que_escribe_otro_bot_en_la_misma_carpeta(tmp_path):
    """Dos bots, uno por persona de la casa, con el mismo /downloads: al
    reiniciar uno, borraba la descarga, la copia o la extracción en curso del
    otro, que fallaba o daba por buena una carpeta a medias."""
    from utils.file_helpers import partial_name, remove_partial_extractions, remove_partial_files

    others = [".654321.dl_1_1.dropbot-part", ".654321.copy.dropbot-part", ".1234567.move.dropbot-part"]
    for name in others:
        _touch(tmp_path / name)
    for name in (".654321.Serie.extracting", ".1234567.Fotos.extracting"):
        (tmp_path / name).mkdir()
        others.append(name)
    _touch(tmp_path / partial_name("dl_1_1.dropbot-part"))
    (tmp_path / partial_name("Pelis.extracting")).mkdir()

    remove_partial_files(str(tmp_path))
    remove_partial_extractions(str(tmp_path))

    assert sorted(os.listdir(tmp_path)) == sorted(others)


def test_al_arrancar_se_borran_las_extracciones_a_medias(tmp_path):
    """Una parada a mitad de descomprimir dejaba la carpeta oculta, que solo se
    borraba si se volvía a descomprimir ese mismo fichero."""
    from utils.file_helpers import remove_partial_extractions

    leftover = tmp_path / ".123456.Fotos 2024.extracting"
    (leftover / "sub").mkdir(parents=True)
    _touch(leftover / "sub" / "a.jpg")
    # Solo carpetas ocultas: ni las visibles, ni un fichero, ni un enlace
    (tmp_path / "Serie.extracting").mkdir()
    _touch(tmp_path / ".123456.notas.extracting")
    elsewhere = tmp_path / "fuera"
    elsewhere.mkdir()
    _touch(elsewhere / "mio.txt")
    os.symlink(elsewhere, tmp_path / ".123456.enlace.extracting")

    remove_partial_extractions(str(tmp_path))

    assert sorted(os.listdir(tmp_path)) == [
        ".123456.enlace.extracting", ".123456.notas.extracting", "Serie.extracting", "fuera"]
    assert (elsewhere / "mio.txt").exists()


def test_mover_una_descarga_no_machaca_un_fichero_existente(dropbot, dirs, run_async):
    temp, final = dirs
    _touch(final / "Video.mp4", b"el de antes")
    src = _touch(temp / "Video_temp123.mp4", b"el nuevo")

    moved = run_async(dropbot.move_download_to(src, str(final)))

    assert os.path.basename(moved) == "Video (1).mp4"
    assert (final / "Video.mp4").read_bytes() == b"el de antes"
    assert (final / "Video (1).mp4").read_bytes() == b"el nuevo"
    assert not os.path.exists(src)


def test_mover_una_descarga_entre_dispositivos_no_ocupa_el_nombre_hasta_acabar(
    dropbot, dirs, run_async, monkeypatch
):
    """Con el nombre reservado antes de copiar, un SIGKILL a mitad dejaba un
    fichero vacío con el nombre final que nadie borraba."""
    temp, final = dirs
    src = _touch(temp / "Video_temp123.mp4", b"x" * 1000)
    _between_devices(monkeypatch, src)
    while_copying = []
    real_copy = shutil.copyfileobj

    def copy(source, target, *args):
        real_copy(source, target, *args)
        while_copying.append(sorted(os.listdir(final)))

    monkeypatch.setattr(shutil, "copyfileobj", copy)

    moved = run_async(dropbot.move_download_to(src, str(final)))

    (listing,) = while_copying
    assert listing and all(name.startswith(dropbot.partial_name("")) for name in listing), \
        "mientras se copia solo hay ocultos de este bot"
    assert os.listdir(final) == ["Video.mp4"] and moved == str(final / "Video.mp4")
    assert (final / "Video.mp4").read_bytes() == b"x" * 1000
    assert not os.path.exists(src)


def test_si_falla_al_dar_el_nombre_final_no_deja_nada_en_la_carpeta(tmp_path, monkeypatch):
    from utils.file_helpers import move_to_unique_path

    final = tmp_path / "final"
    final.mkdir()
    src = _touch(tmp_path / "descarga", b"contenido")
    real_replace = os.replace

    def replace(old, new):
        if new == str(final / "Pelicula.mkv"):
            raise OSError(errno.EIO, "Input/output error")
        return real_replace(old, new)

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(OSError):
        move_to_unique_path(src, str(final), "Pelicula.mkv")

    assert os.listdir(final) == []


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


def test_un_fichero_terminado_de_la_descarga_es_suyo(dropbot, dirs):
    temp, _ = dirs

    assert dropbot.is_ytdlp_output(_touch(temp / "001-Video_temp777.mp4"), "_temp777")


@pytest.mark.parametrize("kind", [
    "fuera de TEMP_DIR", "saliendo con ..", "enlace hacia fuera", "carpeta",
    "de otra descarga", "intermedio", "no existe", "ruta relativa",
    "con un NUL", "con un NUL en TEMP_DIR",
])
def test_lo_que_no_ha_dejado_la_descarga_no_es_suyo(dropbot, dirs, tmp_path, kind):
    """Las rutas salen de la salida de yt-dlp, que repite texto de un tercero.
    Con un NUL, comprobarla lanzaba ValueError en vez de decir que no."""
    temp, _ = dirs
    outside = _touch(tmp_path / "settings_temp777.json")
    paths = {
        "fuera de TEMP_DIR": outside,
        "saliendo con ..": os.path.join(str(temp), "..", "settings_temp777.json"),
        "enlace hacia fuera": str(temp / "Enlace_temp777.mp4"),
        "carpeta": str(temp / "Carpeta_temp777.mp4"),
        "de otra descarga": _touch(temp / "Otro_temp888.mp4"),
        "intermedio": _touch(temp / "Video_temp777.f137.mp4"),
        "no existe": str(temp / "Fantasma_temp777.mp4"),
        "ruta relativa": "Downloading playlist: Top_temp777",
        "con un NUL": "/tmp/a\x00b_temp777.mp4",
        "con un NUL en TEMP_DIR": str(temp / "Video\x00_temp777.mp4"),
    }
    os.symlink(outside, temp / "Enlace_temp777.mp4")
    os.mkdir(temp / "Carpeta_temp777.mp4")

    assert not dropbot.is_ytdlp_output(paths[kind], "_temp777")


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
        dropbot.active_tasks["42_4242"] = task
        await task
        return "42_4242" in dropbot.active_tasks

    assert run_async(scenario()) is False
