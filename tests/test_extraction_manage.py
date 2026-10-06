"""Descomprimir desde /manage: `handle_extract_file`, el ajuste `extract.after`
y qué pasa después con el comprimido (`delete_compressed`, `kept_message`,
`handle_compressed_file_action`).

Los comprimidos son reales (ver `test_extraction_service`), y lo que se
comprueba es lo que ve el usuario: el mensaje que le llega, los botones que le
quedan y lo que hay en disco al acabar.
"""

import asyncio
import io
import os
import tarfile
import zipfile

import pytest
import rarfile

from button_data import button_data
from test_extraction_service import rar4, rar_second_volume
from translations import get_text


def _payloads(sent_messages):
    payloads = []
    for _, _, kwargs in sent_messages:
        for row in kwargs.get("buttons") or []:
            for button in row if isinstance(row, (list, tuple)) else [row]:
                data = button_data(button)
                if data:
                    payloads.append(data)
    return payloads


@pytest.fixture
def extractor(quiet_manage, config_dir, tmp_path, sent_messages, make_event, run_async):
    """Una carpeta de descargas y una forma de pulsar "Descomprimir" en un fichero."""
    import settings

    manage = quiet_manage
    manage.pending_file_actions.clear()
    settings.put("extract.after", "ASK")
    downloads = tmp_path / "descargas"
    downloads.mkdir()

    class Extractor:
        root = downloads
        module = manage

        def register(self, path, file_id="x1"):
            manage.pending_file_actions[file_id] = str(path)
            return file_id

        def extract(self, path, file_id="x1"):
            self.register(path, file_id)
            run_async(manage.handle_extract_file(make_event(groups=(file_id.encode(),))))

        def act(self, action, path, file_id="x1"):
            self.register(path, file_id)
            run_async(manage.handle_compressed_file_action(
                make_event(groups=(action.encode(), file_id.encode()))
            ))

        @staticmethod
        def last():
            return sent_messages[-1][1]

        @staticmethod
        def payloads():
            return _payloads(sent_messages)

    return Extractor()


def make_zip(path, members=None):
    with zipfile.ZipFile(path, "w") as bundle:
        if members is None:
            members = {"dentro.txt": b"hola"}
        for name, data in members.items():
            bundle.writestr(name, data)
    return path


def make_tar(path, mode):
    with tarfile.open(path, mode) as bundle:
        info = tarfile.TarInfo("dentro.txt")
        info.size = 4
        bundle.addfile(info, io.BytesIO(b"hola"))
    return path


def _non_admin_event(make_event, groups):
    event = make_event(groups=groups)
    event.sender_id = 1

    async def get_sender():
        return None

    event.get_sender = get_sender
    return event


# --- Extraer ---------------------------------------------------------------------

class TestDescomprimir:
    def test_un_zip_se_extrae_en_una_carpeta_con_su_nombre(self, extractor, sent_messages):
        archive = make_zip(extractor.root / "Fotos del viaje.zip",
                           {"día 1/playa.jpg": b"p", "notas.txt": b"n"})

        extractor.extract(archive)

        folder = extractor.root / "Fotos del viaje"
        assert (folder / "día 1" / "playa.jpg").read_bytes() == b"p"
        assert (folder / "notas.txt").read_bytes() == b"n"
        assert archive.exists(), "con ASK el comprimido sigue ahí"
        assert sent_messages[0][1] == get_text("decompressing_file", "Fotos del viaje.zip")
        assert get_text("extraction_success_title") in extractor.last()
        assert "`Fotos del viaje`" in extractor.last()
        assert get_text("extraction_ask_delete") in extractor.last()
        assert {b"delcompressed:x1", b"keepcompressed:x1"} <= set(extractor.payloads())

    @pytest.mark.parametrize("name,mode,folder", [
        ("copia.tar", "w", "copia"),
        ("copia.tgz", "w:gz", "copia"),
        ("copia.tbz", "w:bz2", "copia"),
    ])
    def test_los_tar_se_extraen(self, extractor, name, mode, folder):
        archive = make_tar(extractor.root / name, mode)

        extractor.extract(archive)

        assert (extractor.root / folder / "dentro.txt").read_bytes() == b"hola"
        assert get_text("extraction_success_title") in extractor.last()

    @pytest.mark.parametrize("name,mode", [("copia.tar.gz", "w:gz"), ("copia.tar.bz2", "w:bz2")])
    def test_un_tar_con_doble_extension_se_extrae_en_una_carpeta_sin_tar(
        self, extractor, name, mode
    ):
        archive = make_tar(extractor.root / name, mode)

        extractor.extract(archive)

        assert (extractor.root / "copia" / "dentro.txt").exists()

    def test_un_rar_real_se_extrae(self, extractor):
        archive = extractor.root / "serie.rar"
        archive.write_bytes(rar4([("cap1.txt", b"uno")]))

        extractor.extract(archive)

        assert (extractor.root / "serie" / "cap1.txt").read_bytes() == b"uno"
        assert get_text("extraction_success_title") in extractor.last()

    def test_la_primera_parte_de_un_rar_se_extrae_sin_el_sufijo_de_parte(self, extractor):
        archive = extractor.root / "pelicula.part1.rar"
        archive.write_bytes(rar4([("pelicula.mkv", b"video")]))

        extractor.extract(archive)

        assert (extractor.root / "pelicula" / "pelicula.mkv").read_bytes() == b"video"

    def test_un_rar_conserva_las_mayusculas_en_la_carpeta(self, extractor):
        archive = extractor.root / "Mi Serie.rar"
        archive.write_bytes(rar4([("cap1.txt", b"uno")]))

        extractor.extract(archive)

        assert "Mi Serie" in os.listdir(extractor.root)

    def test_una_parte_suelta_de_rar_avisa_de_que_faltan_partes(self, extractor):
        archive = extractor.root / "pelicula.part2.rar"
        archive.write_bytes(rar_second_volume())

        extractor.extract(archive)

        assert get_text("extraction_missing_parts_title") in extractor.last()
        assert sorted(os.listdir(extractor.root)) == ["pelicula.part2.rar"]
        assert b"fileact:x1" in extractor.payloads()
        assert not any(p.startswith(b"delcompressed:") for p in extractor.payloads())

    def test_un_rar_con_el_crc_mal_se_da_por_corrupto(self, extractor):
        data = bytearray(rar4([("hola.txt", b"hola" * 100)]))
        data[-20] ^= 0xFF
        archive = extractor.root / "crc.rar"
        archive.write_bytes(bytes(data))

        extractor.extract(archive)

        assert get_text("extraction_corrupted_title") in extractor.last()
        assert os.listdir(extractor.root) == ["crc.rar"]

    def test_un_rar_truncado_avisa_de_extraccion_incompleta(self, extractor):
        archive = extractor.root / "corto.rar"
        archive.write_bytes(rar4([("hola.txt", b"hola" * 100)])[:60])

        extractor.extract(archive)

        assert get_text("extraction_partial_title") in extractor.last()
        assert os.listdir(extractor.root) == ["corto.rar"]

    def test_un_zip_corrupto_avisa_del_error_y_no_deja_carpeta(self, extractor):
        archive = extractor.root / "roto.zip"
        archive.write_bytes(b"PK\x03\x04" + b"\0" * 50)

        extractor.extract(archive)

        assert get_text("extraction_error_title") in extractor.last()
        assert os.listdir(extractor.root) == ["roto.zip"]
        assert not any(p.startswith(b"delcompressed:") for p in extractor.payloads())

    def test_un_zip_vacio_crea_una_carpeta_vacia(self, extractor):
        archive = make_zip(extractor.root / "vacio.zip", {})

        extractor.extract(archive)

        assert os.listdir(extractor.root / "vacio") == []
        assert get_text("extraction_success_title") in extractor.last()

    def test_zip_slip_desde_manage_no_escribe_fuera(self, extractor, tmp_path):
        archive = make_zip(extractor.root / "slip.zip",
                           {"../../fuera.txt": b"x", "../hermano.txt": b"y"})

        extractor.extract(archive)

        assert not (tmp_path / "fuera.txt").exists()
        assert not (extractor.root / "hermano.txt").exists()
        assert sorted(os.listdir(extractor.root)) == ["slip", "slip.zip"]

    def test_si_la_carpeta_de_destino_ya_existe_no_se_toca(self, extractor):
        archive = make_zip(extractor.root / "paquete.zip", {"dentro.txt": b"nuevo"})
        existing = extractor.root / "paquete"
        existing.mkdir()
        (existing / "dentro.txt").write_bytes(b"mio")

        extractor.extract(archive)

        assert (existing / "dentro.txt").read_bytes() == b"mio"
        assert extractor.last() == get_text("error_extraction_folder_exists", "paquete")

    def test_un_fichero_con_el_nombre_de_la_carpeta_tambien_lo_impide(self, extractor):
        archive = make_zip(extractor.root / "paquete.zip")
        (extractor.root / "paquete").write_bytes(b"soy un fichero")

        extractor.extract(archive)

        assert (extractor.root / "paquete").read_bytes() == b"soy un fichero"
        assert extractor.last() == get_text("error_extraction_folder_exists", "paquete")

    def test_un_fichero_que_ya_no_existe(self, extractor):
        extractor.extract(extractor.root / "borrado.zip")

        assert extractor.last() == get_text("error_file_not_found")

    def test_un_id_desconocido(self, extractor, make_event, run_async):
        run_async(extractor.module.handle_extract_file(make_event(groups=(b"nada",))))

        assert extractor.last() == get_text("error_file_not_found")

    def test_algo_que_no_es_un_comprimido_se_rechaza(self, extractor):
        target = extractor.root / "video.mkv"
        target.write_bytes(b"\0" * 64)

        extractor.extract(target)

        assert extractor.last() == get_text("error_not_compressed")
        assert os.listdir(extractor.root) == ["video.mkv"]

    def test_quien_no_es_admin_no_puede_descomprimir(
        self, extractor, sent_messages, make_event, run_async
    ):
        archive = make_zip(extractor.root / "paquete.zip")
        extractor.register(archive)

        run_async(extractor.module.handle_extract_file(_non_admin_event(make_event, (b"x1",))))

        assert sent_messages == []
        assert os.listdir(extractor.root) == ["paquete.zip"]

    def test_un_zip_partido_se_rechaza(self, extractor):
        archive = make_zip(extractor.root / "copia.zip")
        (extractor.root / "copia.z01").write_bytes(b"segunda parte")

        extractor.extract(archive)

        assert extractor.last() == get_text("error_split_zip_not_supported")
        assert sorted(os.listdir(extractor.root)) == ["copia.z01", "copia.zip"]

    def test_un_fallo_no_deja_una_carpeta_vacia_que_bloquee_el_reintento(self, extractor):
        part = extractor.root / "copia.z01"
        part.write_bytes(b"segunda parte de un zip")

        extractor.extract(part)

        assert sorted(os.listdir(extractor.root)) == ["copia.z01"]

    def test_el_mensaje_de_progreso_se_actualiza_mientras_tarda(
        self, extractor, sent_messages, monkeypatch
    ):
        """Cada 10 s se reedita el mensaje; aquí los 10 s duran unos milisegundos."""
        import time

        archive = make_zip(extractor.root / "lento.zip")
        real_wait_for = asyncio.wait_for

        async def fast_wait_for(awaitable, timeout):
            return await real_wait_for(awaitable, timeout=0.01)

        def slow_extract(file_path, extract_to):
            time.sleep(0.15)
            return True

        monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)
        monkeypatch.setattr(extractor.module, "extract_file", slow_extract)

        extractor.extract(archive)

        sent = [text for _, text, _ in sent_messages]
        assert get_text("decompressing_file_progress", "lento.zip", 10) in sent
        assert get_text("decompressing_file_progress", "lento.zip", 20) in sent
        assert get_text("extraction_success_title") in extractor.last()

    def test_si_el_mensaje_de_progreso_falla_la_extraccion_sigue(
        self, extractor, monkeypatch
    ):
        import time

        archive = make_zip(extractor.root / "lento.zip")
        real_wait_for = asyncio.wait_for
        real_edit = extractor.module.safe_edit

        async def fast_wait_for(awaitable, timeout):
            return await real_wait_for(awaitable, timeout=0.01)

        async def flaky_edit(message, text=None, **kwargs):
            if "10s" in str(text) or "20s" in str(text):
                raise RuntimeError("Telegram no responde")
            return await real_edit(message, text, **kwargs)

        def slow_extract(file_path, extract_to):
            time.sleep(0.1)
            return extract_file_real(file_path, extract_to)

        from services.extraction_service import extract_file as extract_file_real

        monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)
        monkeypatch.setattr(extractor.module, "extract_file", slow_extract)
        monkeypatch.setattr(extractor.module, "safe_edit", flaky_edit)

        extractor.extract(archive)

        assert (extractor.root / "lento" / "dentro.txt").exists()
        assert get_text("extraction_success_title") in extractor.last()


# --- extract.after ---------------------------------------------------------------

class TestTrasDescomprimirSegunElAjuste:
    def test_preguntar_deja_el_comprimido_y_pregunta(self, extractor):
        archive = make_zip(extractor.root / "paquete.zip")

        extractor.extract(archive)

        assert archive.exists()
        assert get_text("extraction_ask_delete") in extractor.last()

    def test_borrar_lo_borra_y_lo_cuenta_en_el_mismo_mensaje(self, extractor):
        import settings

        settings.put("extract.after", "DELETE")
        archive = make_zip(extractor.root / "paquete.zip")

        extractor.extract(archive)

        assert not archive.exists()
        assert (extractor.root / "paquete" / "dentro.txt").exists()
        assert get_text("extraction_success_title") in extractor.last()
        assert get_text("file_deleted_title") in extractor.last()
        assert get_text("extraction_ask_delete") not in extractor.last()
        assert "x1" not in extractor.module.pending_file_actions
        assert set(extractor.payloads()) == {b"managecat:all", b"close"}

    def test_borrar_un_rar_multiparte_borra_todas_sus_partes(self, extractor):
        import settings

        settings.put("extract.after", "DELETE")
        first = extractor.root / "serie.rar"
        first.write_bytes(rar4([("cap1.txt", b"uno")]))
        for extra in ("serie.r00", "serie.r01"):
            (extractor.root / extra).write_bytes(b"otra parte")
        (extractor.root / "otra cosa.mkv").write_bytes(b"no se toca")

        extractor.extract(first)

        assert sorted(os.listdir(extractor.root)) == ["otra cosa.mkv", "serie"]
        assert get_text("files_deleted_count", 3) in extractor.last()

    def test_conservar_lo_deja_y_lo_dice(self, extractor):
        import settings

        settings.put("extract.after", "KEEP")
        archive = make_zip(extractor.root / "paquete.zip")

        extractor.extract(archive)

        assert archive.exists()
        assert get_text("file_kept_desc") in extractor.last()
        assert get_text("extraction_ask_delete") not in extractor.last()
        assert set(extractor.payloads()) == {b"managecat:all", b"close"}
        assert extractor.module.pending_file_actions.get("x1") == str(archive)

    @pytest.mark.parametrize("after", ["DELETE", "KEEP"])
    def test_si_la_extraccion_falla_el_comprimido_no_se_toca(self, extractor, after):
        import settings

        settings.put("extract.after", after)
        archive = extractor.root / "roto.zip"
        archive.write_bytes(b"PK\x03\x04" + b"\0" * 50)

        extractor.extract(archive)

        assert archive.exists()
        assert get_text("extraction_error_title") in extractor.last()
        assert get_text("file_deleted_title") not in extractor.last()
        assert get_text("file_kept_desc") not in extractor.last()

    def test_borrar_con_partes_que_faltan_no_borra_la_parte(self, extractor):
        import settings

        settings.put("extract.after", "DELETE")
        archive = extractor.root / "pelicula.part2.rar"
        archive.write_bytes(rar_second_volume())

        extractor.extract(archive)

        assert archive.exists()
        assert get_text("extraction_missing_parts_title") in extractor.last()


# --- Botones de después ------------------------------------------------------------

class TestBorrarOConservarDespues:
    def test_conservar(self, extractor):
        archive = make_zip(extractor.root / "paquete.zip")

        extractor.act("keepcompressed", archive)

        assert archive.exists()
        assert extractor.last() == extractor.module.kept_message("paquete.zip")
        assert set(extractor.payloads()) == {b"managecat:all", b"close"}

    def test_borrar(self, extractor):
        archive = make_zip(extractor.root / "paquete.zip")

        extractor.act("delcompressed", archive)

        assert not archive.exists()
        assert get_text("file_deleted_title") in extractor.last()
        assert "paquete.zip" in extractor.last()
        assert "x1" not in extractor.module.pending_file_actions

    def test_borrar_un_rar_por_partes_las_borra_todas(self, extractor):
        first = extractor.root / "Pelicula.part1.rar"
        first.write_bytes(rar4([("a.txt", b"a")]))
        for extra in ("Pelicula.part2.rar", "PELICULA.PART3.RAR"):
            (extractor.root / extra).write_bytes(b"otra parte")
        (extractor.root / "Pelicula").mkdir()

        extractor.act("delcompressed", first)

        assert os.listdir(extractor.root) == ["Pelicula"], "la carpeta extraída se queda"
        assert get_text("files_deleted_count", 3) in extractor.last()

    def test_si_el_fichero_ya_no_esta(self, extractor):
        extractor.act("delcompressed", extractor.root / "ya_borrado.zip")

        assert extractor.last() == get_text("error_file_not_found")

    def test_quien_no_es_admin_no_puede_borrar(self, extractor, sent_messages, make_event, run_async):
        archive = make_zip(extractor.root / "paquete.zip")
        extractor.register(archive)

        run_async(extractor.module.handle_compressed_file_action(
            _non_admin_event(make_event, (b"delcompressed", b"x1"))
        ))

        assert archive.exists()
        assert sent_messages == []


class TestBorrarComprimido:
    def test_un_zip_se_borra_solo(self, extractor):
        archive = make_zip(extractor.root / "paquete.zip")
        (extractor.root / "paquete.z01").write_bytes(b"x")

        msg = extractor.module.delete_compressed(str(archive))

        assert sorted(os.listdir(extractor.root)) == ["paquete.z01"]
        assert get_text("file_deleted_title") in msg and "`paquete.zip`" in msg

    def test_un_rar_viejo_con_r00_borra_todas_sus_partes(self, extractor):
        first = extractor.root / "serie.rar"
        first.write_bytes(rar4([("a.txt", b"a")]))
        for extra in ("serie.r00", "serie.r01", "serie.R02"):
            (extractor.root / extra).write_bytes(b"x")

        msg = extractor.module.delete_compressed(str(first))

        assert os.listdir(extractor.root) == []
        assert get_text("files_deleted_count", 4) in msg

    def test_borrar_desde_una_parte_r00(self, extractor, monkeypatch):
        """Pulsar borrar sobre la parte .r00 también se lleva todo el juego."""
        monkeypatch.setattr(rarfile, "is_rarfile", lambda path: True)
        for name in ("serie.rar", "serie.r00", "serie.r01"):
            (extractor.root / name).write_bytes(b"x")

        msg = extractor.module.delete_compressed(str(extractor.root / "serie.r00"))

        assert os.listdir(extractor.root) == []
        assert get_text("files_deleted_count", 3) in msg

    def test_un_rar_falso_se_borra_como_un_fichero_mas(self, extractor):
        fake = extractor.root / "falso.rar"
        fake.write_bytes(b"no soy un rar")
        (extractor.root / "falso.part2.rar").write_bytes(b"x")

        msg = extractor.module.delete_compressed(str(fake))

        assert os.listdir(extractor.root) == ["falso.part2.rar"]
        assert get_text("file_deleted_title") in msg

    def test_un_rar_con_otra_extension_se_borra_solo(self, extractor):
        """Un RAR de verdad llamado .cbr no tiene partes que buscar."""
        comic = extractor.root / "tebeo.cbr"
        comic.write_bytes(rar4([("pagina1.jpg", b"p")]))
        (extractor.root / "tebeo.rar").write_bytes(b"otro")

        msg = extractor.module.delete_compressed(str(comic))

        assert os.listdir(extractor.root) == ["tebeo.rar"]
        assert get_text("file_deleted_title") in msg and "`tebeo.cbr`" in msg

    def test_si_no_se_puede_borrar_lo_dice(self, extractor):
        msg = extractor.module.delete_compressed(str(extractor.root / "no_existe.zip"))

        assert get_text("delete_error_title") in msg

    def test_borrar_un_rar_no_se_lleva_ficheros_que_solo_empiezan_igual(self, extractor):
        first = extractor.root / "serie.rar"
        first.write_bytes(rar4([("a.txt", b"a")]))
        others = ["serie - extras.rar", "serie2.rar", "serie.party.mkv"]
        for name in others:
            (extractor.root / name).write_bytes(b"ajeno")

        extractor.module.delete_compressed(str(first))

        assert sorted(os.listdir(extractor.root)) == sorted(others)

    def test_borrar_un_rar_por_partes_no_toca_otro_juego_de_partes(self, extractor):
        first = extractor.root / "pelicula.part1.rar"
        first.write_bytes(rar4([("a.txt", b"a")]))
        (extractor.root / "pelicula.part2.rar").write_bytes(b"x")
        others = ["pelicula 2.part1.rar", "pelicula 2.part2.rar"]
        for name in others:
            (extractor.root / name).write_bytes(b"ajeno")

        extractor.module.delete_compressed(str(first))

        assert sorted(os.listdir(extractor.root)) == sorted(others)


class TestMensajeDeConservado:
    def test_lleva_el_nombre_y_los_textos(self, extractor):
        msg = extractor.module.kept_message("Canción del año.zip")

        assert "`Canción del año.zip`" in msg
        assert get_text("file_kept_title") in msg
        assert get_text("file_kept_desc") in msg
