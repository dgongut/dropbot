"""Descompresión de ficheros: `services.extraction_service` y los ayudantes de `basic`.

Todos los comprimidos se crean de verdad en el test: los ZIP con `zipfile`, los
TAR con `tarfile` y los RAR con `rar4()` y `rar4_volumes()`, que montan a mano
un RAR 4 "store" (sin compresión), y con `rar5()`, que monta uno de RAR 5. Los
RAR sólidos y comprimidos, que no se pueden montar así, van como bytes más
abajo. `rarfile` lee el índice de un RAR
por su cuenta, pero quien extrae es `unrar`: los tests que extraen un RAR de
verdad se saltan donde no está el binario.
"""

import base64
import errno
import gzip
import io
import os
import shutil
import stat
import struct
import subprocess
import tarfile
import zipfile
import zlib
from types import SimpleNamespace

import pytest
import rarfile

from basic import (
    clean_rar_base_name, clean_youtube_link, get_filename_from_path,
    is_compressed_file, is_split_zip, sanitize_filename,
)
from services.extraction_service import FREE_SPACE_MARGIN, extract_file

needs_unrar = pytest.mark.skipif(shutil.which("unrar") is None, reason="necesita el binario unrar")

MIB = 1024 * 1024


# --- Fábricas de comprimidos ---------------------------------------------------

# Flags de la cabecera de archivo de un RAR 4 (MHD_PASSWORD: "rar -hp", que
# cifra también los nombres)
MHD_VOLUME, MHD_NEWNUMBERING, MHD_FIRSTVOLUME = 0x0001, 0x0010, 0x0100
MHD_PASSWORD = 0x0080
# Flags de la cabecera de cada fichero (LHD_PASSWORD: "rar -p", solo los datos)
LHD_SPLIT_BEFORE, LHD_SPLIT_AFTER, LHD_PASSWORD = 0x0001, 0x0002, 0x0004
# Flag del bloque final de un volumen que no es el último
EARC_NEXT_VOLUME = 0x0001


def _rar_block(head_type, flags, body):
    size = 7 + len(body)
    rest = struct.pack("<BHH", head_type, flags, size) + body
    return struct.pack("<H", zlib.crc32(rest) & 0xFFFF) + rest


def _rar_file(name, data, flags=0, unpacked_size=None, crc=None, attributes=0o100644):
    """La cabecera de un fichero guardado sin comprimir, con sus datos detrás."""
    encoded = name.encode("ascii")
    body = struct.pack(
        "<IIBIIBBHI", len(data), len(data) if unpacked_size is None else unpacked_size, 3,
        zlib.crc32(data) if crc is None else crc, 0x00210000, 20, 0x30, len(encoded), attributes,
    ) + encoded
    return _rar_block(0x74, 0x8000 | flags, body) + data


def rar4(files, archive_flags=0, file_flags=0, end_flags=0x4000):
    """Un RAR 4 válido con `files` [(nombre, bytes)] guardados sin comprimir."""
    out = b"Rar!\x1a\x07\x00"
    out += _rar_block(0x73, archive_flags, b"\0" * 6)
    for name, data in files:
        out += _rar_file(name, data, file_flags)
    out += _rar_block(0x7B, end_flags, b"")
    return out


def rar4_volumes(files, parts, new_numbering=True):
    """Los volúmenes de un RAR 4 multiparte de verdad, que acepta unrar.

    Los ficheros de `files` van enteros en el primer volumen, salvo el último,
    que se reparte entre los `parts` volúmenes.
    """
    *whole, (name, data) = files
    size = -(-len(data) // parts)
    chunks = [data[start:start + size] for start in range(0, len(data), size)]
    volumes = []
    for number, chunk in enumerate(chunks):
        first, last = number == 0, number == len(chunks) - 1
        flags = MHD_VOLUME | (MHD_NEWNUMBERING if new_numbering else 0) | (MHD_FIRSTVOLUME if first else 0)
        out = b"Rar!\x1a\x07\x00" + _rar_block(0x73, flags, b"\0" * 6)
        if first:
            out += b"".join(_rar_file(whole_name, whole_data) for whole_name, whole_data in whole)
        # Cada trozo lleva el CRC de lo suyo, y el último el del fichero entero
        split = (0 if first else LHD_SPLIT_BEFORE) | (0 if last else LHD_SPLIT_AFTER)
        out += _rar_file(name, chunk, split, unpacked_size=len(data),
                         crc=zlib.crc32(data if last else chunk))
        out += _rar_block(0x7B, 0x4000 | (0 if last else EARC_NEXT_VOLUME), b"")
        volumes.append(out)
    return volumes


def write_rar_set(folder, base, files, parts, new_numbering=True):
    """Escribe en `folder` un RAR multiparte y devuelve las rutas de sus partes.

    Con `new_numbering` se llaman base.partN.rar; sin él, base.rar, base.r00...
    """
    if new_numbering:
        names = [f"{base}.part{number}.rar" for number in range(1, parts + 1)]
    else:
        names = [f"{base}.rar"] + [f"{base}.r{number:02d}" for number in range(parts - 1)]
    paths = []
    for name, volume in zip(names, rar4_volumes(files, parts, new_numbering)):
        path = folder / name
        path.write_bytes(volume)
        paths.append(path)
    return paths


def rar_second_volume(name="pelicula.mkv", data=b"B" * 32):
    """La segunda parte de un RAR multiparte, sin la primera."""
    return rar4([(name, data)], archive_flags=MHD_VOLUME | MHD_NEWNUMBERING,
                file_flags=LHD_SPLIT_BEFORE)


# Flags de la cabecera de cada fichero de un RAR 5 (UNKNOWN_SIZE: "rar -si",
# que comprime lo que le llega por una tubería sin saber cuánto será)
RAR5_FILE_HAS_CRC, RAR5_FILE_UNKNOWN_SIZE = 0x0004, 0x0008


def _vint(number):
    """Un entero como los guarda RAR 5: de 7 en 7 bits, con el octavo si sigue."""
    out = bytearray()
    while number > 0x7F:
        out.append(number & 0x7F | 0x80)
        number >>= 7
    return bytes(out + bytes([number]))


def _rar5_block(fields):
    size = _vint(len(fields))
    return struct.pack("<I", zlib.crc32(size + fields)) + size + fields


def rar5(name, data, file_flags=0, unpacked_size=None):
    """Un RAR 5 válido con un fichero guardado sin comprimir."""
    encoded = name.encode("ascii")
    header = (
        b"".join(_vint(field) for field in (
            2, 0x0002, len(data), RAR5_FILE_HAS_CRC | file_flags,
            len(data) if unpacked_size is None else unpacked_size, 0o644,
        ))
        + struct.pack("<I", zlib.crc32(data)) + _vint(0) + _vint(1) + _vint(len(encoded)) + encoded
    )
    return (b"Rar!\x1a\x07\x01\x00" + _rar5_block(_vint(1) + _vint(0) + _vint(0))
            + _rar5_block(header) + data + _rar5_block(_vint(5) + _vint(0) + _vint(0)))


# Lo bastante grande como para repartirlo en varios volúmenes
MOVIE = bytes(range(256)) * 1200

# RAR 5 sólidos y comprimidos, hechos con RAR 7.11 porque no se pueden montar a
# mano. capitulos.rar: `rar a -s -m3 capitulos.rar capitulo1.txt ... capitulo4.txt`,
# con "capitulo N " 100 veces en cada uno
SOLID_CHAPTERS_RAR = base64.b64decode(
    "UmFyIRoHAQAJ78hvCwEFBwQGAQGAgIAAaA0BYCsCAwuoAATMCKSDAkL65xiAGwENY2FwaXR1"
    "bG8xLnR4dAoDE4At2Wo8cz45xLslM1Qz+FAyCvgLfgQ1XKpVK3CQ/pdvwRmjyY+YYem9f5E9"
    "8WKP8LBRpacrAgMLhwAEzAikgwJfifMDwBsBDWNhcGl0dWxvMi50eHQKAxOALdlq2xVAOUAe"
    "BHrin4CfcySwKwIDC4cABMwIpIMCa1ovvMAbAQ1jYXBpdHVsbzMudHh0CgMTgC3ZansmQTlA"
    "HgR7Ap+AXssDgisCAwuHAATMCKSDAmVv2zXAGwENY2FwaXR1bG80LnR4dAoDE4At2WqCJkI5"
    "QB4EeyKfgB13VlEDBQQA"
)
SOLID_CHAPTERS = {f"capitulo{n}.txt": f"capitulo {n} ".encode() * 100 for n in range(1, 5)}

# nombres_raros.rar: `rar a -s -m3 -@ nombres_raros.rar Bonus "Hola?.txt"
# "@notas.txt" capitulo1.txt capitulo2.txt`, con "Bonus/Hola?.txt" por delante
# de "Hola?.txt" dentro del archivo
SOLID_ODD_NAMES_RAR = base64.b64decode(
    "UmFyIRoHAQAJ78hvCwEFBwQGAQGAgIAAxoAWBygCAwudAASMBqSDAkB6pw6AQwEKQG5vdGFz"
    "LnR4dAoDE8Iq2WppqOYVxIQaJAMz+kJKxawNg3jFga3BX+mbED3OyFRkMJjML9vmLQIDC4gA"
    "BNAFpIMCUNFzD8BDAQ9Cb251cy9Ib2xhPy50eHQKAxPCKtlqjRXkFUQbBd3XiMkY7dyF5ycC"
    "AwuHAATuBaSDAnSOau7AQwEJSG9sYT8udHh0CgMTwirZasVn5RVHGQTi6RmelD84/ysCAwuH"
    "AASgBqSDAsGOL5bAQwENY2FwaXR1bG8xLnR4dAoDE8Iq2WontOcVQR8E/CDNACGRmqErAgML"
    "hwAEyAakgwKJfYgXwEMBDWNhcGl0dWxvMi50eHQKAxPCKtlqIqHoFUEfBNKQ4QBjyAFVHQID"
    "CwABAO2DAcAAAQVCb251cwoDE8Iq2WpEuOIVHXdWUQMFBAA="
)
SOLID_ODD_NAMES = {
    "@notas.txt": b"notas " * 130,
    os.path.join("Bonus", "Hola?.txt"): b"extra " * 120,
    "Hola?.txt": b"hola " * 150,
    "capitulo1.txt": b"uno " * 200,
    "capitulo2.txt": b"dos " * 210,
}

# copias.rar: `rar a -oi:1 -m3 copias.rar uno.txt copia.txt`, los dos con
# "identico " 300 veces: "copia.txt" se guarda como una referencia a "uno.txt"
COPY_REFERENCE_RAR = base64.b64decode(
    "UmFyIRoHAQDz4YLrCwEFBwAGAQGAgIAAt2i7NiUCAwubAASMFaSDAtYJYdOAAwEHdW5vLnR4"
    "dAoDE4ov2WpxEdYxxIYYAwI/siivm/BAkEl/kIvK1qQLq+djuZEgrd1fwzMCAxeAAASMFaSD"
    "AgAAAACAAAEJY29waWEudHh0CgMTii/ZatRA1zELBQUAB3Vuby50eHQdd1ZRAwUEAA=="
)


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return str(path)


def make_tar(path, members, mode="w"):
    with tarfile.open(path, mode) as bundle:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return str(path)


def tree(root):
    """Los ficheros bajo `root`, relativos y con su contenido."""
    found = {}
    for current, _, files in os.walk(root):
        for name in files:
            full = os.path.join(current, name)
            with open(full, "rb") as handle:
                found[os.path.relpath(full, root)] = handle.read()
    return found


@pytest.fixture
def workdir(tmp_path):
    """Un directorio con el comprimido y su carpeta de destino ya creada,
    como la deja `handle_extract_file` antes de llamar a `extract_file`."""
    source = tmp_path / "descargas"
    source.mkdir()
    dest = source / "destino"
    dest.mkdir()
    return source, dest


# --- ZIP -----------------------------------------------------------------------

class TestZip:
    def test_extrae_los_ficheros_y_las_subcarpetas(self, workdir):
        source, dest = workdir
        archive = make_zip(source / "fotos.zip", {
            "a.txt": b"uno",
            "viaje/b.txt": b"dos",
            "viaje/2024/c.txt": b"tres",
        })

        assert extract_file(archive, str(dest)) is True
        assert tree(dest) == {
            "a.txt": b"uno",
            os.path.join("viaje", "b.txt"): b"dos",
            os.path.join("viaje", "2024", "c.txt"): b"tres",
        }

    def test_conserva_acentos_y_espacios_en_los_nombres(self, workdir):
        source, dest = workdir
        archive = make_zip(source / "Canciones del año.zip", {
            "Canción número 1.mp3": b"x",
            "Ñandú/Piña colada.txt": b"y",
        })

        assert extract_file(archive, str(dest)) is True
        assert (dest / "Canción número 1.mp3").read_bytes() == b"x"
        assert (dest / "Ñandú" / "Piña colada.txt").read_bytes() == b"y"

    def test_la_extension_en_mayusculas_tambien_vale(self, workdir):
        source, dest = workdir
        archive = make_zip(source / "FOTOS.ZIP", {"a.txt": b"uno"})

        assert extract_file(archive, str(dest)) is True
        assert (dest / "a.txt").exists()

    def test_un_zip_vacio_se_da_por_bueno_y_deja_la_carpeta_vacia(self, workdir):
        source, dest = workdir
        archive = make_zip(source / "vacio.zip", {})

        assert extract_file(archive, str(dest)) is True
        assert dest.is_dir() and os.listdir(dest) == []

    def test_las_carpetas_vacias_del_zip_se_crean(self, workdir):
        source, dest = workdir
        archive = source / "carpetas.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("vacia/", b"")
            bundle.writestr("llena/a.txt", b"a")

        assert extract_file(str(archive), str(dest)) is True
        assert (dest / "vacia").is_dir()
        assert (dest / "llena" / "a.txt").exists()

    def test_un_zip_truncado_falla_y_borra_la_carpeta(self, workdir):
        source, dest = workdir
        archive = make_zip(source / "roto.zip", {"a.txt": b"hola" * 1000})
        with open(archive, "r+b") as handle:
            handle.truncate(os.path.getsize(archive) // 2)

        assert extract_file(archive, str(dest)) is False
        assert not dest.exists()

    def test_un_fichero_que_no_es_zip_falla_y_borra_la_carpeta(self, workdir):
        source, dest = workdir
        archive = source / "falso.zip"
        archive.write_bytes(b"esto no es un zip")

        assert extract_file(str(archive), str(dest)) is False
        assert not dest.exists()

    def test_un_miembro_con_el_crc_mal_falla_y_no_deja_nada_a_medias(self, workdir):
        source, dest = workdir
        archive = source / "crc.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:
            bundle.writestr("bueno.txt", b"bien")
            bundle.writestr("malo.txt", b"A" * 200)
        data = bytearray(archive.read_bytes())
        offset = data.find(b"A" * 200)
        data[offset + 10] = ord("B")
        archive.write_bytes(bytes(data))

        assert extract_file(str(archive), str(dest)) is False
        assert not dest.exists(), "la extracción a medias tiene que desaparecer"

    @pytest.mark.parametrize("evil_name", [
        "../evil.txt",
        "../../evil.txt",
        "sub/../../evil.txt",
        "/tmp/dropbot-zip-slip-absoluto.txt",
    ])
    def test_zip_slip_no_escribe_fuera_del_destino(self, workdir, evil_name):
        source, dest = workdir
        archive = make_zip(source / "slip.zip", {evil_name: b"maligno", "ok.txt": b"ok"})
        before_outside = set(os.listdir(source.parent)) | set(os.listdir(source))

        extract_file(archive, str(dest))

        after_outside = set(os.listdir(source.parent)) | set(os.listdir(source))
        assert after_outside == before_outside
        assert not os.path.exists("/tmp/dropbot-zip-slip-absoluto.txt")
        for path in tree(dest) if dest.exists() else {}:
            assert not os.path.isabs(path) and not path.startswith("..")

    def test_los_ficheros_que_ya_existen_en_el_destino_se_sobrescriben(self, workdir):
        """`extract_file` no se niega a pisar: la protección contra una carpeta
        que ya existe está en el handler, que no llega a llamarla."""
        source, dest = workdir
        (dest / "a.txt").write_bytes(b"viejo")
        (dest / "otro.txt").write_bytes(b"intacto")
        archive = make_zip(source / "nuevo.zip", {"a.txt": b"nuevo"})

        assert extract_file(archive, str(dest)) is True
        assert (dest / "a.txt").read_bytes() == b"nuevo"
        assert (dest / "otro.txt").read_bytes() == b"intacto"

    def test_crea_el_destino_si_no_existe(self, tmp_path):
        archive = make_zip(tmp_path / "a.zip", {"a.txt": b"a"})
        dest = tmp_path / "no" / "existe"

        assert extract_file(archive, str(dest)) is True
        assert (dest / "a.txt").exists()


# --- TAR -----------------------------------------------------------------------

class TestTar:
    @pytest.mark.parametrize("name,mode", [
        ("copia.tar", "w"),
        ("copia.tar.gz", "w:gz"),
        ("copia.tgz", "w:gz"),
        ("copia.tar.bz2", "w:bz2"),
        ("copia.tbz", "w:bz2"),
        ("COPIA.TAR.GZ", "w:gz"),
    ])
    def test_extrae_todas_las_variantes(self, workdir, name, mode):
        source, dest = workdir
        archive = make_tar(source / name, {
            "raiz.txt": b"r",
            "carpeta/año 2024.txt": b"acento",
        }, mode)

        assert extract_file(archive, str(dest)) is True
        assert tree(dest) == {
            "raiz.txt": b"r",
            os.path.join("carpeta", "año 2024.txt"): b"acento",
        }

    def test_un_tar_vacio_se_da_por_bueno(self, workdir):
        source, dest = workdir
        archive = make_tar(source / "vacio.tar", {})

        assert extract_file(archive, str(dest)) is True
        assert os.listdir(dest) == []

    @pytest.mark.parametrize("evil_name", [
        "../evil.txt",
        "sub/../../evil.txt",
        "/tmp/dropbot-tar-slip-absoluto.txt",
    ])
    def test_tar_slip_se_rechaza_entero_y_no_escribe_nada(self, workdir, evil_name):
        source, dest = workdir
        archive = make_tar(source / "slip.tar", {"ok.txt": b"ok", evil_name: b"maligno"})

        assert extract_file(archive, str(dest)) is False
        assert not dest.exists()
        assert not (source / "evil.txt").exists()
        assert not (source.parent / "evil.txt").exists()
        assert not os.path.exists("/tmp/dropbot-tar-slip-absoluto.txt")

    @pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
    def test_los_enlaces_se_rechazan(self, workdir, link_type):
        source, dest = workdir
        archive = source / "enlace.tar"
        with tarfile.open(archive, "w") as bundle:
            info = tarfile.TarInfo("a.txt")
            info.size = 1
            bundle.addfile(info, io.BytesIO(b"a"))
            link = tarfile.TarInfo("enlace")
            link.type = link_type
            link.linkname = "/etc/passwd" if link_type == tarfile.SYMTYPE else "a.txt"
            bundle.addfile(link)

        assert extract_file(str(archive), str(dest)) is False
        assert not dest.exists()

    def test_los_dispositivos_los_frena_el_filtro_data(self, workdir):
        source, dest = workdir
        archive = source / "dispositivo.tar"
        with tarfile.open(archive, "w") as bundle:
            info = tarfile.TarInfo("null")
            info.type = tarfile.CHRTYPE
            info.devmajor, info.devminor = 1, 3
            bundle.addfile(info)

        assert extract_file(str(archive), str(dest)) is False
        assert not dest.exists()

    def test_un_tar_gz_truncado_falla_y_borra_la_carpeta(self, workdir):
        source, dest = workdir
        archive = make_tar(source / "roto.tar.gz", {"a.bin": os.urandom(20000)}, "w:gz")
        with open(archive, "r+b") as handle:
            handle.truncate(os.path.getsize(archive) // 2)

        assert extract_file(archive, str(dest)) is False
        assert not dest.exists()

    @pytest.mark.parametrize("damage", ["corte_al_empezar_una_cabecera",
                                        "corte_a_mitad_de_una_cabecera",
                                        "cabecera_con_un_byte_mal"])
    def test_un_tar_cortado_o_danado_en_una_cabecera_no_se_da_por_bueno(self, workdir, damage):
        """tarfile da por acabado el archivo, sin error, en la primera cabecera
        vacía, cortada o dañada: se extraían los miembros de antes y ya."""
        source, dest = workdir
        archive = make_tar(source / "codigo.tar", {f"img{n}.jpg": bytes([n]) * 1000 for n in range(4)})
        # Cada miembro ocupa su cabecera y sus datos redondeados a bloques de 512
        third_header = 2 * (512 + 1024)
        with open(archive, "rb") as handle:
            data = bytearray(handle.read())
        if damage == "corte_al_empezar_una_cabecera":
            data = data[:third_header]
        elif damage == "corte_a_mitad_de_una_cabecera":
            data = data[:third_header + 100]
        else:
            data[third_header + 5] ^= 0xFF
        with open(archive, "wb") as handle:
            handle.write(bytes(data))

        assert extract_file(archive, str(dest)) == "corrupted"
        assert not dest.exists(), "no se extrae nada de un TAR incompleto"

    def test_un_tar_comprimido_con_una_cabecera_danada_tampoco(self, workdir):
        source, dest = workdir
        plain = make_tar(source / "codigo.tar", {f"img{n}.jpg": bytes([n]) * 1000 for n in range(4)})
        with open(plain, "rb") as handle:
            data = bytearray(handle.read())
        data[2 * (512 + 1024) + 5] ^= 0xFF
        archive = source / "codigo.tar.gz"
        with gzip.open(archive, "wb") as handle:
            handle.write(bytes(data))

        assert extract_file(str(archive), str(dest)) == "corrupted"
        assert not dest.exists()

    def test_sin_soporte_de_filter_usa_el_extractall_de_siempre(self, workdir, monkeypatch):
        """Pythons antiguos no conocen `filter=`: se reintenta sin él."""
        source, dest = workdir
        archive = make_tar(source / "viejo.tar", {"a.txt": b"a"})
        original = tarfile.TarFile.extractall
        calls = []

        def old_extractall(self, path=".", members=None, **kwargs):
            calls.append(kwargs)
            if "filter" in kwargs:
                raise TypeError("unexpected keyword argument 'filter'")
            return original(self, path, members, filter="data")

        monkeypatch.setattr(tarfile.TarFile, "extractall", old_extractall)

        assert extract_file(archive, str(dest)) is True
        assert (dest / "a.txt").read_bytes() == b"a"
        assert calls == [{"filter": "data"}, {}]


# --- RAR -----------------------------------------------------------------------

class TestRar:
    @needs_unrar
    def test_extrae_un_rar_real(self, workdir):
        source, dest = workdir
        archive = source / "serie.rar"
        archive.write_bytes(rar4([("cap1.txt", b"uno"), ("extras/cap2.txt", b"dos")]))

        assert extract_file(str(archive), str(dest)) is True
        assert tree(dest) == {
            "cap1.txt": b"uno",
            os.path.join("extras", "cap2.txt"): b"dos",
        }

    @needs_unrar
    def test_un_rar_se_reconoce_por_su_contenido_y_no_por_la_extension(self, workdir):
        source, dest = workdir
        archive = source / "serie.part1.rar"
        archive.write_bytes(rar4([("cap1.txt", b"uno")]))

        assert extract_file(str(archive), str(dest)) is True
        assert (dest / "cap1.txt").exists()

    @pytest.mark.parametrize("evil_name", ["../evil.txt", "/tmp/dropbot-rar-slip.txt"])
    def test_rar_slip_no_escribe_fuera_del_destino(self, workdir, evil_name):
        source, dest = workdir
        archive = source / "slip.rar"
        archive.write_bytes(rar4([(evil_name, b"maligno")]))

        extract_file(str(archive), str(dest))

        assert not (source / "evil.txt").exists()
        assert not (source.parent / "evil.txt").exists()
        assert not os.path.exists("/tmp/dropbot-rar-slip.txt")

    def test_un_enlace_simbolico_se_rechaza_entero(self, workdir):
        """Igual que en los TAR: un enlace puede apuntar fuera del destino."""
        source, dest = workdir
        archive = source / "enlace.rar"
        archive.write_bytes(
            b"Rar!\x1a\x07\x00" + _rar_block(0x73, 0, b"\0" * 6)
            + _rar_file("a.txt", b"a")
            + _rar_file("enlace", b"../../fuera", attributes=0o120777)
            + _rar_block(0x7B, 0x4000, b"")
        )

        assert extract_file(str(archive), str(dest)) is False
        assert not dest.exists()

    @needs_unrar
    def test_una_copia_de_un_fichero_identico_no_cuenta_como_enlace(self, workdir):
        source, dest = workdir
        archive = source / "copias.rar"
        archive.write_bytes(COPY_REFERENCE_RAR)

        assert extract_file(str(archive), str(dest)) is True
        assert tree(dest) == {"uno.txt": b"identico " * 300, "copia.txt": b"identico " * 300}

    @needs_unrar
    @pytest.mark.parametrize("attributes", [0o104755, 0o102755, 0o101777, 0o106777, 0o100755], ids=oct)
    def test_los_ficheros_salen_sin_setuid_setgid_ni_sticky(self, workdir, attributes):
        """unrar deja a cada fichero los permisos que trae el RAR: con el bot
        como root, un RAR ajeno dejaba en la carpeta del host un ejecutable
        setuid de root. El resto de permisos, como el de ejecución, se queda."""
        source, dest = workdir
        archive = source / "utilidades.rar"
        archive.write_bytes(
            b"Rar!\x1a\x07\x00" + _rar_block(0x73, 0, b"\0" * 6)
            + _rar_file("bin/tool", b"#!/bin/sh\n", attributes=attributes)
            + _rar_block(0x7B, 0x4000, b"")
        )

        assert extract_file(str(archive), str(dest)) is True
        assert stat.S_IMODE((dest / "bin" / "tool").stat().st_mode) == attributes & 0o777

    @needs_unrar
    def test_un_rar5_se_extrae(self, workdir):
        source, dest = workdir
        archive = source / "nuevo.rar"
        archive.write_bytes(rar5("hola.txt", b"hola" * 100))

        assert extract_file(str(archive), str(dest)) is True
        assert tree(dest) == {"hola.txt": b"hola" * 100}

    def test_una_parte_suelta_que_no_es_la_primera_avisa_de_que_faltan_partes(self, workdir):
        source, dest = workdir
        archive = source / "pelicula.part2.rar"
        archive.write_bytes(rar_second_volume())

        assert extract_file(str(archive), str(dest)) == "missing_parts"
        assert not dest.exists(), "la carpeta vacía se tiene que borrar"

    @needs_unrar
    @pytest.mark.parametrize("new_numbering", [True, False])
    def test_un_rar_por_partes_se_extrae_entero(self, workdir, new_numbering):
        source, dest = workdir
        parts = write_rar_set(source, "pelicula", [("extras.txt", b"entero"), ("pelicula.mkv", MOVIE)],
                              3, new_numbering)

        assert extract_file(str(parts[0]), str(dest)) is True
        assert tree(dest) == {"extras.txt": b"entero", "pelicula.mkv": MOVIE}

    @pytest.mark.parametrize("missing", [1, 2])
    def test_un_rar_por_partes_al_que_le_falta_una_no_se_extrae(self, workdir, missing):
        """rarfile no lanza nada si no encuentra el volumen siguiente: lo apunta
        y sigue con lo que tiene."""
        source, dest = workdir
        parts = write_rar_set(source, "pelicula", [("extras.txt", b"entero"), ("pelicula.mkv", MOVIE)], 3)
        parts[missing].unlink()

        assert extract_file(str(parts[0]), str(dest)) == "missing_parts"
        assert not dest.exists()
        assert all(part.exists() for number, part in enumerate(parts) if number != missing)

    @needs_unrar
    @pytest.mark.parametrize("cut", [1, 2])
    def test_un_rar_por_partes_con_una_cortada_esta_corrupto(self, workdir, monkeypatch, cut):
        """rarfile solo lee las cabeceras y no lo nota: lo dice unrar al
        comprobar el CRC. Con FORCE_TOOL rarfile lee también estos miembros sin
        comprimir con unrar, como hace con los comprimidos: así se daba por
        buena la extracción con el último fichero a medias."""
        monkeypatch.setattr(rarfile, "FORCE_TOOL", True)
        source, dest = workdir
        parts = write_rar_set(source, "pelicula", [("extras.txt", b"entero"), ("pelicula.mkv", MOVIE)], 3)
        with open(parts[cut], "r+b") as handle:
            handle.truncate(os.path.getsize(parts[cut]) - 300)

        assert extract_file(str(parts[0]), str(dest)) == "corrupted"
        assert not dest.exists(), "tampoco se queda el fichero que estaba entero"

    @pytest.mark.parametrize("archive_flags,file_flags", [
        (MHD_PASSWORD, 0),   # rar -hp: el índice sale vacío
        (0, LHD_PASSWORD),   # rar -p: solo los datos
    ])
    def test_un_rar_con_contrasena_no_se_da_por_descomprimido(self, workdir, archive_flags, file_flags):
        """Con las cabeceras cifradas no había nada que extraer, y "nada" se
        daba por bueno: se anunciaba el éxito y se ofrecía borrar el RAR."""
        source, dest = workdir
        archive = source / "fotos.rar"
        archive.write_bytes(rar4([("foto.jpg", b"jpg")], archive_flags=archive_flags, file_flags=file_flags))

        assert extract_file(str(archive), str(dest)) == "password"
        assert not dest.exists()

    @needs_unrar
    def test_un_rar_solido_se_extrae_con_una_sola_llamada_a_unrar(self, workdir, monkeypatch):
        """rarfile sacaba cada miembro con su propio "unrar p", que en un RAR
        sólido vuelve a descomprimir todo lo anterior: con miles de ficheros,
        horas."""
        source, dest = workdir
        archive = source / "capitulos.rar"
        archive.write_bytes(SOLID_CHAPTERS_RAR)
        launched = []

        class CountingPopen(subprocess.Popen):
            def __init__(self, args, *more, **kwargs):
                launched.append(list(args))
                super().__init__(args, *more, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", CountingPopen)
        monkeypatch.setattr(rarfile, "Popen", CountingPopen)

        assert extract_file(str(archive), str(dest)) is True
        assert tree(dest) == SOLID_CHAPTERS
        assert [command[1] for command in launched] == ["x"]

    @needs_unrar
    def test_un_rar_solido_con_arrobas_e_interrogaciones_en_los_nombres_sale_entero(self, workdir):
        """Pasados a unrar de uno en uno, "@notas.txt" se tomaba por una lista
        de ficheros y "Hola?.txt" por una máscara que casaba antes con
        "Bonus/Hola?.txt": salía "extracción incompleta" o "archivo dañado"."""
        source, dest = workdir
        archive = source / "nombres_raros.rar"
        archive.write_bytes(SOLID_ODD_NAMES_RAR)

        assert extract_file(str(archive), str(dest)) is True
        assert tree(dest) == SOLID_ODD_NAMES

    @needs_unrar
    def test_un_rar_con_interrogacion_en_su_nombre_no_abre_otros_que_encajen(self, workdir):
        """unrar también toma por una máscara el nombre del archivo: "Hola?.rar"
        abriría además "HolaX.rar"."""
        source, dest = workdir
        archive = source / "Hola?.rar"
        archive.write_bytes(rar4([("mio.txt", b"mio")]))
        (source / "HolaX.rar").write_bytes(rar4([("ajeno.txt", b"ajeno")]))

        assert extract_file(str(archive), str(dest)) is True
        assert tree(dest) == {"mio.txt": b"mio"}

    @needs_unrar
    def test_un_rar_por_partes_con_asterisco_en_su_nombre_encuentra_sus_partes(self, workdir):
        source, dest = workdir
        parts = write_rar_set(source, "Serie*", [("extras.txt", b"entero"), ("serie.mkv", MOVIE)], 3)
        (source / "SerieX.part1.rar").write_bytes(rar4([("ajeno.txt", b"ajeno")]))

        assert extract_file(str(parts[0]), str(dest)) is True
        assert tree(dest) == {"extras.txt": b"entero", "serie.mkv": MOVIE}

    @needs_unrar
    def test_un_rar_con_el_crc_mal_esta_corrupto(self, workdir):
        source, dest = workdir
        data = bytearray(rar4([("hola.txt", b"hola" * 100)]))
        data[-20] ^= 0xFF
        archive = source / "crc.rar"
        archive.write_bytes(bytes(data))

        assert extract_file(str(archive), str(dest)) == "corrupted"
        assert not dest.exists()

    @needs_unrar
    def test_un_rar_truncado_esta_corrupto_y_no_deja_nada(self, workdir):
        source, dest = workdir
        archive = source / "corto.rar"
        archive.write_bytes(rar4([("hola.txt", b"hola" * 100)])[:60])

        assert extract_file(str(archive), str(dest)) == "corrupted"
        assert not dest.exists()

    # Cómo se interpreta cada salida de unrar, sin tener que provocarla
    @pytest.fixture
    def unrar_exits(self, workdir, monkeypatch):
        source, dest = workdir
        archive = source / "falla.rar"
        archive.write_bytes(rar4([("a.txt", b"a")]))

        def install(returncode, output=""):
            def fake_run(command, **kwargs):
                # Lo que unrar haya llegado a escribir antes de fallar
                open(os.path.join(command[-1], "a medias.txt"), "wb").close()
                return subprocess.CompletedProcess(command, returncode, "", output)

            monkeypatch.setattr(subprocess, "run", fake_run)
            return str(archive), dest
        return install

    @pytest.mark.parametrize("returncode,output,expected", [
        (3, "a.txt - checksum error", "corrupted"),
        (3, "Unexpected end of archive", "corrupted"),
        (6, "Cannot find volume /descargas/falla.part2.rar", "missing_parts"),
        (11, "Incorrect password for a.txt", "password"),
        # Con el disco lleno, unrar puede contar "Inappropriate ioctl for device"
        (5, "Write error in the file a.txt", "no_space"),
        (2, "Algo raro", False),
    ])
    def test_la_salida_de_unrar_decide_el_resultado(self, unrar_exits, returncode, output, expected):
        archive, dest = unrar_exits(returncode, output)

        assert extract_file(archive, str(dest)) == expected
        assert not dest.exists(), "lo extraído a medias se borra"

    def test_sin_unrar_falla_sin_reventar(self, workdir, monkeypatch):
        source, dest = workdir
        archive = source / "a.rar"
        archive.write_bytes(rar4([("a.txt", b"a")]))

        def missing_tool(command, **kwargs):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", command[0])

        monkeypatch.setattr(subprocess, "run", missing_tool)

        assert extract_file(str(archive), str(dest)) is False
        assert not dest.exists()

    @pytest.mark.parametrize("returncode,output,expected", [
        (6, "Cannot find volume", "missing_parts"),
        (3, "checksum error", "corrupted"),
        (2, "Algo raro", False),
    ])
    def test_si_no_se_puede_limpiar_la_carpeta_el_resultado_llega_igual(
        self, unrar_exits, monkeypatch, returncode, output, expected
    ):
        archive, dest = unrar_exits(returncode, output)

        def broken_rmtree(path, *args, **kwargs):
            raise PermissionError("sin permiso")

        monkeypatch.setattr(shutil, "rmtree", broken_rmtree)

        assert extract_file(archive, str(dest)) == expected

    @needs_unrar
    def test_unrar_de_la_imagen_entiende_los_rar_de_los_tests(self, workdir):
        """Comprueba que los RAR hechos a mano son RAR de verdad y no solo algo
        que acepta rarfile: se los pasamos a la herramienta real."""
        source, dest = workdir
        archive = source / "real.rar"
        archive.write_bytes(rar4([("hola.txt", b"hola")]))
        parts = write_rar_set(source, "partes", [("a.txt", b"a"), ("b.bin", MOVIE)], 3)

        for path in (archive, parts[0]):
            result = subprocess.run(["unrar", "t", str(path)], capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr

    def test_un_falso_rar_no_se_extrae(self, workdir):
        source, dest = workdir
        archive = source / "falso.rar"
        archive.write_bytes(b"no soy un rar")

        assert extract_file(str(archive), str(dest)) is False

    @pytest.mark.parametrize("name", ["falso.rar", "pelicula.z01", "pelicula.r00", "pelicula.7z"])
    def test_un_formato_no_reconocido_tambien_borra_la_carpeta(self, workdir, name):
        source, dest = workdir
        archive = source / name
        archive.write_bytes(b"no soy un comprimido conocido")

        assert extract_file(str(archive), str(dest)) is False
        assert not dest.exists()

    def test_un_formato_no_reconocido_no_borra_una_carpeta_con_cosas(self, workdir):
        """Solo se quita la carpeta si está vacía: si ya tenía algo, no es una
        carpeta que haya creado la extracción."""
        source, dest = workdir
        (dest / "mio.txt").write_bytes(b"del usuario")
        archive = source / "pelicula.z01"
        archive.write_bytes(b"no soy un comprimido conocido")

        assert extract_file(str(archive), str(dest)) is False
        assert (dest / "mio.txt").read_bytes() == b"del usuario"


# --- Espacio en disco -------------------------------------------------------------

class TestEspacioEnDisco:
    """Un comprimido de pocos KB puede declarar cientos de GB: se escribía hasta
    llenar el disco, y después se borraba todo con un "archivo corrupto"."""

    @pytest.fixture
    def free_space(self, monkeypatch):
        def set_free(free):
            monkeypatch.setattr(shutil, "disk_usage",
                                lambda path: SimpleNamespace(total=2 * free, used=free, free=free))
        return set_free

    @pytest.mark.parametrize("name", ["ceros.zip", "ceros.tar.bz2", "ceros.rar"])
    def test_si_no_cabe_no_se_extrae_nada(self, workdir, free_space, name):
        source, dest = workdir
        members = {"ceros.bin": b"\0" * (2 * MIB)}
        if name.endswith(".zip"):
            archive = make_zip(source / name, members)
        elif name.endswith(".tar.bz2"):
            archive = make_tar(source / name, members, "w:bz2")
        else:
            archive = str(source / name)
            with open(archive, "wb") as handle:
                handle.write(rar4(list(members.items())))
        free_space(FREE_SPACE_MARGIN + MIB)

        assert extract_file(archive, str(dest)) == "no_space"
        assert not dest.exists()

    def test_si_cabe_dejando_el_margen_se_extrae(self, workdir, free_space):
        source, dest = workdir
        archive = make_zip(source / "ceros.zip", {"ceros.bin": b"\0" * (MIB // 2)})
        free_space(FREE_SPACE_MARGIN + MIB)

        assert extract_file(archive, str(dest)) is True
        assert (dest / "ceros.bin").stat().st_size == MIB // 2

    @pytest.mark.parametrize("archive", [
        rar5("ceros.bin", b"\0" * MIB, file_flags=RAR5_FILE_UNKNOWN_SIZE, unpacked_size=0),
        # En RAR 4, un tamaño de 0xFFFFFFFF sin la parte alta (LHD_LARGE)
        b"Rar!\x1a\x07\x00" + _rar_block(0x73, 0, b"\0" * 6)
        + _rar_file("ceros.bin", b"\0" * MIB, unpacked_size=0xFFFFFFFF)
        + _rar_block(0x7B, 0x4000, b""),
    ], ids=["rar5", "rar4"])
    def test_un_rar_que_no_dice_cuanto_ocupa_no_se_extrae(self, workdir, free_space, monkeypatch, archive):
        """unrar no hace caso entonces del tamaño de la cabecera y descomprime
        hasta el final de los datos: uno que declaraba 0 bytes pasaba la
        comprobación de espacio y llenaba el disco."""
        source, dest = workdir
        path = source / "bomba.rar"
        path.write_bytes(archive)
        launched = []

        def fake_run(command, **kwargs):
            launched.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        free_space(1024 * 1024 * MIB)

        assert extract_file(str(path), str(dest)) is False
        assert launched == [], "no llega a unrar"
        assert not dest.exists()

    def test_quedarse_sin_sitio_a_mitad_no_se_toma_por_un_archivo_corrupto(self, workdir, monkeypatch):
        source, dest = workdir
        archive = make_zip(source / "fotos.zip", {"a.jpg": b"a"})

        def disk_full(self, path=None, *args, **kwargs):
            open(os.path.join(path, "a medias.jpg"), "wb").close()
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(zipfile.ZipFile, "extractall", disk_full)

        assert extract_file(archive, str(dest)) == "no_space"
        assert not dest.exists()


# --- basic.py ------------------------------------------------------------------

class TestEsComprimido:
    @pytest.mark.parametrize("name", [
        "a.zip", "a.ZIP", "a.tar", "a.tar.gz", "a.tgz", "a.tar.bz2", "a.tbz", "a.rar",
        "a.z01", "a.z99", "a.z100", "a.r00", "a.R12", "a.part1.rar", "a.Part03.RAR",
        "/descargas/Una película.zip",
    ])
    def test_reconoce_los_comprimidos(self, name):
        assert is_compressed_file(name) is True

    @pytest.mark.parametrize("name", [
        "a.mkv", "a.txt", "a.7z", "a.gz", "a.z1", "a.r1", "zip", "a.zip.txt",
        "a.rar.part", "a.part1.mkv", "",
    ])
    def test_no_confunde_otros_ficheros(self, name):
        assert is_compressed_file(name) is False


class TestZipPartido:
    def test_un_zip_con_su_z01_al_lado_esta_partido(self, tmp_path):
        (tmp_path / "copia.zip").write_bytes(b"x")
        (tmp_path / "copia.z01").write_bytes(b"x")

        assert is_split_zip(str(tmp_path / "copia.zip")) is True

    def test_desde_una_parte_z0n_tambien_se_detecta(self, tmp_path):
        (tmp_path / "copia.z01").write_bytes(b"x")
        (tmp_path / "copia.z02").write_bytes(b"x")

        assert is_split_zip(str(tmp_path / "copia.z02")) is True

    def test_basta_cualquier_parte_aunque_falte_la_primera(self, tmp_path):
        (tmp_path / "copia.z07").write_bytes(b"x")

        assert is_split_zip(str(tmp_path / "copia.zip")) is True

    def test_un_zip_solo_no_esta_partido(self, tmp_path):
        (tmp_path / "copia.zip").write_bytes(b"x")
        (tmp_path / "otra.z01").write_bytes(b"x")

        assert is_split_zip(str(tmp_path / "copia.zip")) is False

    @pytest.mark.parametrize("name", ["copia.rar", "copia.tar.gz", "copia.mkv"])
    def test_lo_que_no_es_zip_no_esta_partido(self, tmp_path, name):
        (tmp_path / "copia.z01").write_bytes(b"x")

        assert is_split_zip(str(tmp_path / name)) is False

    def test_la_extension_en_mayusculas(self, tmp_path):
        (tmp_path / "COPIA.ZIP").write_bytes(b"x")
        (tmp_path / "COPIA.z01").write_bytes(b"x")

        assert is_split_zip(str(tmp_path / "COPIA.ZIP")) is True


class TestNombreBaseRar:
    @pytest.mark.parametrize("name,expected", [
        ("pelicula.rar", "pelicula"),
        ("pelicula.part1.rar", "pelicula"),
        ("pelicula.part01.rar", "pelicula"),
        ("pelicula.part12.rar", "pelicula"),
        ("pelicula.r00", "pelicula"),
        ("pelicula.r15", "pelicula"),
        ("mi.serie.s01.part2.rar", "mi.serie.s01"),
        ("Una película.rar", "Una película"),
        ("Mi Serie.PART2.RAR", "Mi Serie"),
        ("Mi Serie.R01", "Mi Serie"),
    ])
    def test_quita_extension_y_numero_de_parte_y_conserva_mayusculas(self, name, expected):
        assert clean_rar_base_name(name) == expected


class TestNombreDeFichero:
    @pytest.mark.parametrize("path,expected", [
        ("/descargas/videos/película.mkv", "película.mkv"),
        ("relativo/fichero.zip", "fichero.zip"),
        ("solo.txt", "solo.txt"),
        ("/descargas/carpeta/", "carpeta"),
    ])
    def test_devuelve_el_ultimo_componente(self, path, expected):
        assert get_filename_from_path(path) == expected


class TestSanearNombre:
    @pytest.mark.parametrize("name,expected", [
        ("copia.tar.gz", "copia.tar.gz"),
        ("Fotos del año.zip", "Fotos del año.zip"),
        ("a/b\\c:d.rar", "a_b_c_d.rar"),
        ("..", "archivo"),
        ("___.zip", "archivo.zip"),
        ("a__b.zip", "a_b.zip"),
    ])
    def test_nombres_de_comprimidos(self, name, expected):
        assert sanitize_filename(name) == expected

    def test_los_acentos_en_nfd_quedan_en_nfc(self):
        import unicodedata

        nfd = unicodedata.normalize("NFD", "canción.zip")
        assert sanitize_filename(nfd) == unicodedata.normalize("NFC", "canción.zip")

    def test_un_nombre_larguisimo_cabe_en_255_bytes_y_conserva_la_extension(self):
        result = sanitize_filename("ñ" * 300 + ".tar.gz")

        assert len(result.encode("utf-8")) <= 255
        assert result.endswith(".gz")


class TestEnlaceDeYoutube:
    @pytest.mark.parametrize("url,expected", [
        ("https://youtu.be/abc123", "https://www.youtube.com/watch?v=abc123"),
        ("https://youtu.be/abc123?si=xyz", "https://www.youtube.com/watch?v=abc123"),
        ("https://www.youtube.com/watch?v=abc123&list=PL1", "https://www.youtube.com/watch?v=abc123"),
        ("https://www.youtube.com/watch?feature=share&v=abc123", "https://www.youtube.com/watch?v=abc123"),
        ("https://www.youtube.com/shorts/abc123?feature=share", "https://www.youtube.com/watch?v=abc123"),
        ("https://vimeo.com/123", "https://vimeo.com/123"),
    ])
    def test_normaliza_los_enlaces(self, url, expected):
        assert clean_youtube_link(url) == expected

    def test_un_watch_sin_v_se_queda_como_esta(self):
        url = "https://www.youtube.com/watch?list=PL1"
        assert clean_youtube_link(url) == url

    def test_un_watch_sin_parametros_se_queda_como_esta(self):
        url = "https://www.youtube.com/watch"
        assert clean_youtube_link(url) == url
