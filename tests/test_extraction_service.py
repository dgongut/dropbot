"""Descompresión de ficheros: `services.extraction_service` y los ayudantes de `basic`.

Todos los comprimidos se crean de verdad en el test: los ZIP con `zipfile`, los
TAR con `tarfile` y los RAR con `rar4()`, que monta a mano un RAR 4 "store"
(sin compresión). Un RAR así lo lee `rarfile` por su cuenta, sin `unrar`, así
que estos tests corren también en la CI. Solo los que necesitan de verdad el
binario se saltan donde no está.
"""

import io
import os
import shutil
import struct
import tarfile
import zipfile
import zlib

import pytest
import rarfile

from basic import (
    clean_rar_base_name, clean_youtube_link, get_filename_from_path,
    is_compressed_file, is_split_zip, sanitize_filename,
)
from services.extraction_service import extract_file


# --- Fábricas de comprimidos ---------------------------------------------------

# Flags de la cabecera de archivo de un RAR 4
MHD_VOLUME, MHD_NEWNUMBERING, MHD_FIRSTVOLUME = 0x0001, 0x0010, 0x0100
# Flags de la cabecera de cada fichero
LHD_SPLIT_BEFORE, LHD_SPLIT_AFTER = 0x0001, 0x0002


def _rar_block(head_type, flags, body):
    size = 7 + len(body)
    rest = struct.pack("<BHH", head_type, flags, size) + body
    return struct.pack("<H", zlib.crc32(rest) & 0xFFFF) + rest


def rar4(files, archive_flags=0, file_flags=0, end_flags=0x4000):
    """Un RAR 4 válido con `files` [(nombre, bytes)] guardados sin comprimir."""
    out = b"Rar!\x1a\x07\x00"
    out += _rar_block(0x73, archive_flags, b"\0" * 6)
    for name, data in files:
        encoded = name.encode("ascii")
        body = struct.pack(
            "<IIBIIBBHI", len(data), len(data), 3, zlib.crc32(data),
            0x00210000, 20, 0x30, len(encoded), 0x81A4,
        ) + encoded
        out += _rar_block(0x74, 0x8000 | file_flags, body) + data
    out += _rar_block(0x7B, end_flags, b"")
    return out


def rar_second_volume(name="pelicula.mkv", data=b"B" * 32):
    """La segunda parte de un RAR multiparte, sin la primera."""
    return rar4([(name, data)], archive_flags=MHD_VOLUME | MHD_NEWNUMBERING,
                file_flags=LHD_SPLIT_BEFORE)


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
    def test_extrae_un_rar_real(self, workdir):
        source, dest = workdir
        archive = source / "serie.rar"
        archive.write_bytes(rar4([("cap1.txt", b"uno"), ("extras/cap2.txt", b"dos")]))

        assert extract_file(str(archive), str(dest)) is True
        assert tree(dest) == {
            "cap1.txt": b"uno",
            os.path.join("extras", "cap2.txt"): b"dos",
        }

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

    def test_una_parte_suelta_que_no_es_la_primera_avisa_de_que_faltan_partes(self, workdir):
        source, dest = workdir
        archive = source / "pelicula.part2.rar"
        archive.write_bytes(rar_second_volume())

        assert extract_file(str(archive), str(dest)) == "missing_parts"
        assert not dest.exists(), "la carpeta vacía se tiene que borrar"

    def test_un_rar_con_el_crc_mal_esta_corrupto(self, workdir):
        source, dest = workdir
        data = bytearray(rar4([("hola.txt", b"hola" * 100)]))
        data[-20] ^= 0xFF
        archive = source / "crc.rar"
        archive.write_bytes(bytes(data))

        assert extract_file(str(archive), str(dest)) == "corrupted"
        assert not dest.exists()

    def test_un_rar_truncado_es_una_extraccion_parcial_y_se_borra(self, workdir):
        """rarfile deja el fichero creado a 0 bytes antes de quedarse sin datos."""
        source, dest = workdir
        archive = source / "corto.rar"
        archive.write_bytes(rar4([("hola.txt", b"hola" * 100)])[:60])

        assert extract_file(str(archive), str(dest)) == "partial"
        assert not dest.exists()

    # Los mensajes que da rarfile según lo que encuentre, sin depender de la
    # herramienta externa que haya instalada
    @pytest.fixture
    def failing_rar(self, workdir, monkeypatch):
        source, dest = workdir
        archive = source / "falla.rar"
        archive.write_bytes(rar4([("a.txt", b"a")]))

        def install(exc, before=None):
            class FailingRar:
                def __init__(self, *args, **kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

                def namelist(self):
                    return ["a.txt"]

                def extractall(self, path):
                    if before:
                        before(path)
                    raise exc

            monkeypatch.setattr(rarfile, "RarFile", FailingRar)
            return str(archive), dest
        return install

    @pytest.mark.parametrize("message", [
        "Need to start from first volume",
        "Need first volume",
        "Missing volume: pelicula.part3.rar",
        "Unexpected end of archive",
    ])
    def test_los_mensajes_de_partes_que_faltan(self, failing_rar, message):
        archive, dest = failing_rar(rarfile.Error(message))

        assert extract_file(archive, str(dest)) == "missing_parts"
        assert not dest.exists()

    @pytest.mark.parametrize("message", [
        "Corrupt file", "Archive is damaged", "Bad RAR file",
        "CRC failed in a.txt", "Checksum error in the encrypted file",
    ])
    def test_los_mensajes_de_archivo_corrupto(self, failing_rar, message):
        archive, dest = failing_rar(rarfile.Error(message))

        assert extract_file(archive, str(dest)) == "corrupted"
        assert not dest.exists()

    def test_lectura_corta_con_ficheros_vacios_es_una_extraccion_parcial(self, failing_rar):
        def leave_half(path):
            with open(os.path.join(path, "lleno.txt"), "wb") as handle:
                handle.write(b"datos")
            open(os.path.join(path, "vacio.txt"), "wb").close()

        archive, dest = failing_rar(
            rarfile.BadRarFile("Failed the read enough data: req=10 got=0"), leave_half,
        )

        assert extract_file(archive, str(dest)) == "partial"
        assert not dest.exists(), "lo extraído a medias se borra"

    def test_lectura_corta_con_todo_extraido_se_da_por_buena(self, failing_rar):
        def leave_all(path):
            os.makedirs(os.path.join(path, "sub"))
            with open(os.path.join(path, "sub", "lleno.txt"), "wb") as handle:
                handle.write(b"datos")

        archive, dest = failing_rar(
            rarfile.BadRarFile("Failed the read enough data: req=10 got=0"), leave_all,
        )

        assert extract_file(archive, str(dest)) is True
        assert (dest / "sub" / "lleno.txt").read_bytes() == b"datos"

    def test_lectura_corta_sin_nada_extraido_esta_corrupto(self, failing_rar):
        archive, dest = failing_rar(
            rarfile.BadRarFile("Failed the read enough data: req=10 got=0"),
        )

        assert extract_file(archive, str(dest)) == "corrupted"
        assert not dest.exists()

    def test_sin_herramienta_para_descomprimir_falla_sin_reventar(self, failing_rar):
        archive, dest = failing_rar(rarfile.RarCannotExec("Cannot find working tool"))

        assert extract_file(archive, str(dest)) is False

    def test_un_error_desconocido_falla_y_borra_la_carpeta(self, failing_rar):
        def leave_something(path):
            open(os.path.join(path, "a medias.txt"), "wb").close()

        archive, dest = failing_rar(rarfile.Error("Algo raro"), leave_something)

        assert extract_file(archive, str(dest)) is False
        assert not dest.exists()

    @pytest.mark.parametrize("exc,expected", [
        (rarfile.Error("Missing volume"), "missing_parts"),
        (rarfile.Error("Corrupt file"), "corrupted"),
        (rarfile.BadRarFile("Failed the read enough data"), "corrupted"),
        (rarfile.Error("Algo raro"), False),
    ])
    def test_si_no_se_puede_limpiar_la_carpeta_el_resultado_llega_igual(
        self, failing_rar, monkeypatch, exc, expected
    ):
        archive, dest = failing_rar(exc)

        def broken_rmtree(path, *args, **kwargs):
            raise PermissionError("sin permiso")

        monkeypatch.setattr(shutil, "rmtree", broken_rmtree)

        assert extract_file(archive, str(dest)) == expected

    def test_si_no_se_puede_limpiar_tras_una_parcial_sigue_siendo_parcial(
        self, failing_rar, monkeypatch
    ):
        archive, dest = failing_rar(
            rarfile.BadRarFile("Failed the read enough data"),
            lambda path: open(os.path.join(path, "vacio.txt"), "wb").close(),
        )

        def busy_rmtree(path, *args, **kwargs):
            raise OSError("ocupado")

        monkeypatch.setattr(shutil, "rmtree", busy_rmtree)

        assert extract_file(archive, str(dest)) == "partial"

    @pytest.mark.skipif(shutil.which("unrar") is None, reason="necesita el binario unrar")
    def test_unrar_de_la_imagen_entiende_los_rar_de_los_tests(self, workdir):
        """Comprueba que el RAR hecho a mano es un RAR de verdad y no solo algo
        que acepta rarfile: se lo pasamos a la herramienta real."""
        import subprocess

        source, dest = workdir
        archive = source / "real.rar"
        archive.write_bytes(rar4([("hola.txt", b"hola")]))

        result = subprocess.run(["unrar", "t", str(archive)], capture_output=True, text=True)
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
