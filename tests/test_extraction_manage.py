"""Descomprimir desde /manage: `handle_extract_file`, el ajuste `extract.after`
y qué pasa después con el comprimido (`delete_compressed`, `kept_message`,
`handle_compressed_file_action`).

Los comprimidos son reales (ver `test_extraction_service`), y lo que se
comprueba es lo que ve el usuario: el mensaje que le llega, los botones que le
quedan y lo que hay en disco al acabar.
"""

import asyncio
import errno
import io
import os
import stat
import tarfile
import threading
import zipfile

import pytest
import rarfile
from telethon.extensions import markdown

from basic import sanitize_filename
from button_data import button_data
from test_extraction_service import (
    MHD_PASSWORD, MOVIE, needs_unrar, rar4, rar_second_volume, write_rar_set,
)
from translations import get_text
from utils.telegram_helpers import utf16_len

# Lo más que admite Telegram en un mensaje, sin las marcas de Markdown
TELEGRAM_TEXT_LIMIT = 4096

# Un juego por partes de los que deja otro programa en la carpeta de descargas
LONG_RAR_BASE = "Pelicula.Larga.2024.1080p.BluRay.x264-GRUPO"


def _telegram_len(text):
    """La longitud de `text` tal como la mide Telegram, ya sin el Markdown."""
    return utf16_len(markdown.parse(text)[0])


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


def _not_utf8_zip(directory, raw_name=b"caf\xe9.zip"):
    """Un ZIP con el nombre en Latin-1, que no es UTF-8, como los que quedan al
    descomprimir en el host uno hecho en Windows. Devuelve su ruta tal como la
    ve Python, con sustitutos ("caf\\udce9.zip")."""
    path = os.fsdecode(os.path.join(os.fsencode(directory), raw_name))
    try:
        make_zip(path)
    except OSError:
        pytest.skip("este sistema de ficheros no admite nombres que no son UTF-8")
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

    @needs_unrar
    def test_un_rar_real_se_extrae(self, extractor):
        archive = extractor.root / "serie.rar"
        archive.write_bytes(rar4([("cap1.txt", b"uno")]))

        extractor.extract(archive)

        assert (extractor.root / "serie" / "cap1.txt").read_bytes() == b"uno"
        assert get_text("extraction_success_title") in extractor.last()

    @needs_unrar
    def test_la_primera_parte_de_un_rar_se_extrae_sin_el_sufijo_de_parte(self, extractor):
        archive = extractor.root / "pelicula.part1.rar"
        archive.write_bytes(rar4([("pelicula.mkv", b"video")]))

        extractor.extract(archive)

        assert (extractor.root / "pelicula" / "pelicula.mkv").read_bytes() == b"video"

    @needs_unrar
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

    @needs_unrar
    def test_un_rar_con_el_crc_mal_se_da_por_corrupto(self, extractor):
        data = bytearray(rar4([("hola.txt", b"hola" * 100)]))
        data[-20] ^= 0xFF
        archive = extractor.root / "crc.rar"
        archive.write_bytes(bytes(data))

        extractor.extract(archive)

        assert get_text("extraction_corrupted_title") in extractor.last()
        assert os.listdir(extractor.root) == ["crc.rar"]

    @needs_unrar
    def test_un_rar_truncado_se_da_por_corrupto(self, extractor):
        archive = extractor.root / "corto.rar"
        archive.write_bytes(rar4([("hola.txt", b"hola" * 100)])[:60])

        extractor.extract(archive)

        assert get_text("extraction_corrupted_title") in extractor.last()
        assert os.listdir(extractor.root) == ["corto.rar"]

    def test_un_rar_con_contrasena_lo_dice_y_no_ofrece_borrarlo(self, extractor):
        archive = extractor.root / "fotos.rar"
        archive.write_bytes(rar4([("foto.jpg", b"jpg")], archive_flags=MHD_PASSWORD))

        extractor.extract(archive)

        assert get_text("extraction_password_title") in extractor.last()
        assert get_text("extraction_success_title") not in extractor.last()
        assert os.listdir(extractor.root) == ["fotos.rar"]
        assert not any(p.startswith(b"delcompressed:") for p in extractor.payloads())

    def test_si_no_cabe_en_el_disco_lo_dice(self, extractor, monkeypatch):
        import shutil
        from types import SimpleNamespace

        archive = make_zip(extractor.root / "grande.zip", {"ceros.bin": b"\0" * 1024})
        monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(total=1, used=1, free=0))

        extractor.extract(archive)

        assert get_text("extraction_no_space_title") in extractor.last()
        assert os.listdir(extractor.root) == ["grande.zip"]

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

    def test_si_la_ficha_ya_no_esta_no_queda_una_carpeta_que_bloquee_el_reintento(
        self, extractor, monkeypatch
    ):
        """La ficha se cerró justo antes de pulsar Descomprimir y la edición de
        "Descomprimiendo" falla. La carpeta ya estaba creada, y cada reintento
        respondía que la carpeta de destino ya existe."""
        from telethon.errors import MessageIdInvalidError

        archive = make_zip(extractor.root / "serie.zip")
        real_edit = extractor.module.safe_edit

        async def gone_edit(message, text=None, **kwargs):
            if text == get_text("decompressing_file", "serie.zip"):
                raise MessageIdInvalidError(request=None)
            return await real_edit(message, text, **kwargs)

        monkeypatch.setattr(extractor.module, "safe_edit", gone_edit)
        extractor.extract(archive)

        assert os.listdir(extractor.root) == ["serie.zip"]

        monkeypatch.setattr(extractor.module, "safe_edit", real_edit)
        extractor.extract(archive)

        assert (extractor.root / "serie" / "dentro.txt").exists()
        assert get_text("extraction_success_title") in extractor.last()

    def test_mientras_se_descomprime_la_carpeta_no_aparece_en_manage(self, extractor, monkeypatch):
        """Se extraía directamente en la carpeta final: /manage la mostraba a
        medias y dejaba borrarla o renombrarla, y al acabar se anunciaba el
        éxito igual."""
        from services.extraction_service import extract_file as real_extract

        archive = make_zip(extractor.root / "Fotos2024.zip", {"a.jpg": b"a", "b.jpg": b"b"})
        visible = []

        def watched_extract(file_path, extract_to):
            result = real_extract(file_path, extract_to)
            # /list y /manage se saltan lo que empieza por punto
            visible.append(sorted(name for name in os.listdir(extractor.root) if not name.startswith(".")))
            return result

        monkeypatch.setattr(extractor.module, "extract_file", watched_extract)
        extractor.extract(archive)

        assert visible == [["Fotos2024.zip"]]
        assert sorted(os.listdir(extractor.root / "Fotos2024")) == ["a.jpg", "b.jpg"]
        assert sorted(os.listdir(extractor.root)) == ["Fotos2024", "Fotos2024.zip"]

    def test_mientras_se_descomprime_solo_el_bot_entra_en_la_carpeta(self, extractor, monkeypatch):
        """unrar deja a cada fichero los permisos que trae el RAR, también un
        setuid, y no se le quitan hasta que termina: mientras, cualquiera podía
        entrar en la carpeta oculta y ejecutarlo. Al acabar, la carpeta tiene
        los permisos de cualquier otra."""
        from services.extraction_service import extract_file as real_extract

        archive = make_zip(extractor.root / "Fotos2024.zip", {"a.jpg": b"a"})
        modes = []

        def watched_extract(file_path, extract_to):
            modes.append(stat.S_IMODE(os.stat(extract_to).st_mode))
            return real_extract(file_path, extract_to)

        monkeypatch.setattr(extractor.module, "extract_file", watched_extract)
        extractor.extract(archive)
        (extractor.root / "otra").mkdir()

        assert len(modes) == 1 and modes[0] & 0o077 == 0, oct(modes[0])
        assert (extractor.root / "Fotos2024").stat().st_mode == (extractor.root / "otra").stat().st_mode

    def test_en_un_disco_que_no_deja_cambiar_permisos_se_extrae_igual(self, extractor, monkeypatch):
        """FAT y exFAT, según cómo se monten, no dejan hacer chmod."""
        def fchmod(fd, mode):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(os, "fchmod", fchmod)
        archive = make_zip(extractor.root / "Fotos2024.zip", {"a.jpg": b"a"})

        extractor.extract(archive)

        assert (extractor.root / "Fotos2024" / "a.jpg").read_bytes() == b"a"
        assert get_text("extraction_success_title") in extractor.last()

    def test_si_telethon_cancela_el_handler_la_extraccion_acaba_y_se_cuenta(
        self, extractor, make_event, run_async, monkeypatch
    ):
        """Con un corte de Telegram, disconnect() cancela los handlers en
        marcha: la extracción seguía en su hilo, pero se quedaba en la carpeta
        oculta y nadie contaba cómo había ido."""
        from services.extraction_service import extract_file as real_extract

        archive = make_zip(extractor.root / "Fotos2024.zip", {"a.jpg": b"a"})
        manage = extractor.module
        extracting, finish = threading.Event(), threading.Event()

        def slow_extract(file_path, extract_to):
            extracting.set()
            finish.wait(5)
            return real_extract(file_path, extract_to)

        monkeypatch.setattr(manage, "extract_file", slow_extract)
        extractor.register(archive)

        async def scenario():
            handler = asyncio.create_task(manage.handle_extract_file(make_event(groups=(b"x1",))))
            while not extracting.is_set():
                await asyncio.sleep(0.01)
            # Como Telethon al desconectarse
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
            finish.set()
            while manage.extractions_in_progress:
                await asyncio.sleep(0.01)

        run_async(asyncio.wait_for(scenario(), 5))

        assert sorted(os.listdir(extractor.root)) == ["Fotos2024", "Fotos2024.zip"]
        assert get_text("extraction_success_title") in extractor.last()

    def test_lo_que_deja_una_parada_a_mitad_se_borra_al_arrancar(self, extractor, monkeypatch):
        """La carpeta oculta de una extracción cortada solo se borraba al volver
        a descomprimir ese mismo fichero."""
        from services.extraction_service import extract_file as real_extract
        from utils.file_helpers import remove_partial_extractions

        archive = make_zip(extractor.root / "Fotos2024.zip", {"a.jpg": b"a"})

        def stopped_extract(file_path, extract_to):
            real_extract(file_path, extract_to)
            raise SystemError("parada a mitad")

        monkeypatch.setattr(extractor.module, "extract_file", stopped_extract)
        with pytest.raises(SystemError):
            extractor.extract(archive)
        assert sorted(os.listdir(extractor.root)) == [".123456.Fotos2024.extracting", "Fotos2024.zip"]

        remove_partial_extractions(str(extractor.root))

        assert os.listdir(extractor.root) == ["Fotos2024.zip"]

    def test_si_la_carpeta_aparece_mientras_tanto_no_se_mezcla_ni_se_borra_el_comprimido(
        self, extractor, monkeypatch
    ):
        """Si durante la extracción el admin crea o renombra algo con ese
        nombre, lo extraído no se mete ahí ni se da por bueno: con "Borrar" se
        perdía el comprimido y quedaba una carpeta incompleta."""
        import settings
        from services.extraction_service import extract_file as real_extract

        settings.put("extract.after", "DELETE")
        archive = make_zip(extractor.root / "Fotos2024.zip", {"a.jpg": b"a"})

        def extract_while_renaming(file_path, extract_to):
            result = real_extract(file_path, extract_to)
            os.makedirs(extractor.root / "Fotos2024", exist_ok=True)
            (extractor.root / "Fotos2024" / "mio.txt").write_bytes(b"del usuario")
            return result

        monkeypatch.setattr(extractor.module, "extract_file", extract_while_renaming)
        extractor.extract(archive)

        assert archive.exists()
        assert os.listdir(extractor.root / "Fotos2024") == ["mio.txt"]
        assert sorted(os.listdir(extractor.root)) == ["Fotos2024", "Fotos2024.zip"]
        assert extractor.last() == get_text("error_extraction_folder_exists", "Fotos2024")

    def test_si_la_carpeta_oculta_desaparece_a_mitad_no_se_da_por_buena(self, extractor, monkeypatch):
        """Otro bot que compartía la carpeta la borraba al arrancar. zipfile
        vuelve a crearla con lo que queda por extraer: se anunciaba el éxito
        con la mitad de los ficheros y, con "Borrar", se perdía el comprimido."""
        import settings
        import shutil
        import zipfile

        settings.put("extract.after", "DELETE")
        members = {f"ep{n}.txt": b"x" for n in range(5)}
        archive = make_zip(extractor.root / "Serie.zip", members)

        def extract_while_swept(file_path, extract_to):
            with zipfile.ZipFile(file_path) as bundle:
                for number, name in enumerate(sorted(members)):
                    if number == 3:
                        shutil.rmtree(extract_to)
                    bundle.extract(name, extract_to)
            return True

        monkeypatch.setattr(extractor.module, "extract_file", extract_while_swept)
        extractor.extract(archive)

        assert archive.exists()
        assert os.listdir(extractor.root) == ["Serie.zip"]
        assert get_text("extraction_success_title") not in extractor.last()

    def test_dos_pulsaciones_seguidas_solo_descomprimen_una_vez(
        self, extractor, monkeypatch, sent_messages, make_event, run_async
    ):
        import time
        from services.extraction_service import extract_file as real_extract

        archive = make_zip(extractor.root / "paquete.zip")
        extractor.register(archive)
        calls = []

        def slow_extract(file_path, extract_to):
            calls.append(extract_to)
            time.sleep(0.1)
            return real_extract(file_path, extract_to)

        monkeypatch.setattr(extractor.module, "extract_file", slow_extract)

        async def double_click():
            await asyncio.gather(
                extractor.module.handle_extract_file(make_event(groups=(b"x1",))),
                extractor.module.handle_extract_file(make_event(groups=(b"x1",))),
            )

        run_async(double_click())

        assert len(calls) == 1
        assert get_text("error_extraction_in_progress", "paquete") in [text for _, text, _ in sent_messages]
        assert (extractor.root / "paquete" / "dentro.txt").exists()
        assert extractor.module.extractions_in_progress == set()

    @pytest.mark.parametrize("name", [
        "a" * 246 + ".zip",
        # Un título en japonés que el bot recortó al guardarlo (253 bytes)
        sanitize_filename("長い題名の動画" * 14 + ".zip"),
    ])
    def test_un_comprimido_con_un_nombre_largo_se_extrae(self, extractor, name):
        """La carpeta oculta lleva además el id del bot y ".extracting": con un
        nombre largo pasaba de los 255 bytes, os.makedirs fallaba y la ficha
        se quedaba en «Descomprimiendo…» sin extraer nada."""
        archive = make_zip(extractor.root / name)
        folder = name[:-len(".zip")]

        extractor.extract(archive)

        assert (extractor.root / folder / "dentro.txt").read_bytes() == b"hola"
        assert get_text("extraction_success_title") in extractor.last()
        assert sorted(os.listdir(extractor.root)) == sorted([folder, name])

    def test_dos_nombres_largos_que_empiezan_igual_no_comparten_la_carpeta_oculta(
        self, extractor, monkeypatch
    ):
        """Recortado, el nombre de la carpeta oculta podía salir igual para
        los dos, y al empezar una extracción se borraba lo que llevaba la otra."""
        from services.extraction_service import extract_file as real_extract

        names = ["b" * 240 + "_parte1.zip", "b" * 240 + "_parte2.zip"]
        work_paths = []

        def watched_extract(file_path, extract_to):
            work_paths.append(extract_to)
            return real_extract(file_path, extract_to)

        monkeypatch.setattr(extractor.module, "extract_file", watched_extract)
        for number, name in enumerate(names):
            extractor.extract(make_zip(extractor.root / name), file_id=f"x{number}")

        assert len(set(work_paths)) == 2
        assert all(len(os.fsencode(os.path.basename(path))) <= 255 for path in work_paths)
        assert sorted(os.listdir(extractor.root)) == sorted(names + [name[:-len(".zip")] for name in names])

    def test_si_no_se_puede_crear_la_carpeta_oculta_lo_dice(self, extractor, monkeypatch):
        """Un error de disco al crearla (sin permisos, o una de antes que no se
        pudo borrar) se escapaba del handler y la ficha se quedaba en
        «Descomprimiendo…» para siempre."""
        from utils.file_helpers import EXTRACTING_SUFFIX

        archive = make_zip(extractor.root / "serie.zip")
        real_makedirs = os.makedirs

        def makedirs(path, *args, **kwargs):
            if str(path).endswith(EXTRACTING_SUFFIX):
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real_makedirs(path, *args, **kwargs)

        monkeypatch.setattr(os, "makedirs", makedirs)
        extractor.extract(archive)

        assert get_text("extraction_error_title") in extractor.last()
        assert b"fileact:x1" in extractor.payloads()
        assert os.listdir(extractor.root) == ["serie.zip"]


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

    def test_borrar_no_se_lleva_otro_fichero_que_ocupo_su_nombre(self, extractor, monkeypatch):
        """Durante la extracción se renombra y llega otro «Pack.zip»: al acabar
        se borraba ese, que ni se había extraído."""
        import settings

        settings.put("extract.after", "DELETE")
        archive = make_zip(extractor.root / "Pack.zip")
        renamed = extractor.root / "Pack-viejo.zip"
        real_extract = extractor.module.extract_file

        def extract_while_renaming(file_path, extracted_path):
            result = real_extract(file_path, extracted_path)
            os.rename(archive, renamed)
            make_zip(archive, {"nuevo.txt": b"sin extraer"})
            return result

        monkeypatch.setattr(extractor.module, "extract_file", extract_while_renaming)

        extractor.extract(archive)

        assert archive.exists() and renamed.exists()
        with zipfile.ZipFile(archive) as bundle:
            assert bundle.namelist() == ["nuevo.txt"]
        assert get_text("delete_error_replaced", "Pack.zip") in extractor.last()
        assert get_text("file_deleted_title") not in extractor.last()

    @needs_unrar
    def test_borrar_un_rar_multiparte_borra_todas_sus_partes(self, extractor):
        import settings

        settings.put("extract.after", "DELETE")
        parts = write_rar_set(extractor.root, "serie", [("cap1.txt", b"uno"), ("cap2.mkv", MOVIE)], 3,
                              new_numbering=False)
        (extractor.root / "otra cosa.mkv").write_bytes(b"no se toca")

        extractor.extract(parts[0])

        assert sorted(os.listdir(extractor.root)) == ["otra cosa.mkv", "serie"]
        assert get_text("files_deleted_count", 3) in extractor.last()

    @needs_unrar
    def test_borrar_un_rar_de_cien_partes_lo_cuenta_en_un_mensaje_que_cabe(self, extractor):
        """Con el nombre de cada parte, pasaba de los 4096 caracteres: Telegram
        rechazaba la edición y el mensaje se quedaba en «Descomprimiendo…»."""
        import settings

        settings.put("extract.after", "DELETE")
        parts = write_rar_set(extractor.root, LONG_RAR_BASE, [("a.txt", b"a"), ("b.mkv", MOVIE * 4)], 100)

        extractor.extract(parts[0])

        assert os.listdir(extractor.root) == [LONG_RAR_BASE]
        msg = extractor.last()
        assert _telegram_len(msg) <= TELEGRAM_TEXT_LIMIT
        assert get_text("extraction_success_title") in msg
        assert get_text("files_deleted_count", 100) in msg
        assert f"`{parts[0].name}`" in msg and get_text("files_deleted_more", 90) in msg

    @needs_unrar
    def test_borrar_con_una_parte_cortada_no_borra_nada(self, extractor, monkeypatch):
        """Con "Borrar", un juego de partes con una cortada se daba por
        descomprimido y se borraban todas: lo extraído quedaba a medias y sin
        nada de donde repetirlo."""
        import settings

        # Así lee rarfile los miembros comprimidos: con unrar. Con estos sin
        # comprimir lo hace solo si se le obliga
        monkeypatch.setattr(rarfile, "FORCE_TOOL", True)
        settings.put("extract.after", "DELETE")
        parts = write_rar_set(extractor.root, "pelicula", [("extras.txt", b"entero"), ("pelicula.mkv", MOVIE)], 3)
        with open(parts[2], "r+b") as handle:
            handle.truncate(os.path.getsize(parts[2]) - 300)

        extractor.extract(parts[0])

        assert get_text("extraction_corrupted_title") in extractor.last()
        assert sorted(os.listdir(extractor.root)) == [part.name for part in parts]

    def test_borrar_con_una_parte_que_falta_no_borra_las_demas(self, extractor):
        import settings

        settings.put("extract.after", "DELETE")
        parts = write_rar_set(extractor.root, "pelicula", [("extras.txt", b"entero"), ("pelicula.mkv", MOVIE)], 3)
        parts[1].unlink()

        extractor.extract(parts[0])

        assert get_text("extraction_missing_parts_title") in extractor.last()
        assert sorted(os.listdir(extractor.root)) == [parts[0].name, parts[2].name]

    def test_borrar_un_rar_con_contrasena_no_lo_borra(self, extractor):
        import settings

        settings.put("extract.after", "DELETE")
        archive = extractor.root / "fotos.rar"
        archive.write_bytes(rar4([("foto.jpg", b"jpg")], archive_flags=MHD_PASSWORD))

        extractor.extract(archive)

        assert archive.exists()
        assert get_text("extraction_password_title") in extractor.last()

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
        parts = write_rar_set(extractor.root, "Pelicula", [("a.txt", b"a"), ("b.mkv", MOVIE)], 3)
        (extractor.root / "Pelicula").mkdir()

        extractor.act("delcompressed", parts[0])

        assert os.listdir(extractor.root) == ["Pelicula"], "la carpeta extraída se queda"
        assert get_text("files_deleted_count", 3) in extractor.last()

    def test_borrar_un_rar_de_cien_partes_lo_cuenta_en_un_mensaje_que_cabe(self, extractor):
        """Con el nombre de cada parte, Telegram rechazaba la edición: la ficha
        seguía con Eliminar y Conservar de algo que ya estaba borrado."""
        parts = write_rar_set(extractor.root, LONG_RAR_BASE, [("a.txt", b"a"), ("b.mkv", MOVIE * 4)], 100)
        (extractor.root / LONG_RAR_BASE).mkdir()

        extractor.act("delcompressed", parts[0])

        assert os.listdir(extractor.root) == [LONG_RAR_BASE]
        msg = extractor.last()
        assert _telegram_len(msg) <= TELEGRAM_TEXT_LIMIT
        assert get_text("files_deleted_count", 100) in msg
        assert get_text("files_deleted_more", 90) in msg

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
        parts = write_rar_set(extractor.root, "serie", [("a.txt", b"a"), ("b.mkv", MOVIE)], 4,
                              new_numbering=False)

        msg = extractor.module.delete_compressed(str(parts[0]))

        assert os.listdir(extractor.root) == []
        assert get_text("files_deleted_count", 4) in msg

    def test_el_mensaje_dice_que_partes_se_han_borrado(self, extractor):
        parts = write_rar_set(extractor.root, "pelicula", [("a.txt", b"a"), ("b.mkv", MOVIE)], 2)

        msg = extractor.module.delete_compressed(str(parts[0]))

        assert "`pelicula.part1.rar`" in msg and "`pelicula.part2.rar`" in msg

    @pytest.mark.parametrize("chosen,kept", [
        ("serie.rar", ["serie.part1.rar", "serie.part2.rar"]),
        ("serie.part1.rar", ["serie.rar"]),
    ])
    def test_borrar_un_rar_no_se_lleva_otro_con_la_misma_base_y_otro_esquema(self, extractor, chosen, kept):
        """Se borraba todo lo que encajara con base.rar, base.rNN o
        base.partN.rar, mezclando los dos esquemas: un RAR suelto y otro por
        partes con el mismo nombre caían juntos."""
        (extractor.root / "serie.rar").write_bytes(rar4([("suelto.txt", b"suelto")]))
        write_rar_set(extractor.root, "serie", [("a.txt", b"a"), ("b.mkv", MOVIE)], 2)

        extractor.module.delete_compressed(str(extractor.root / chosen))

        assert sorted(os.listdir(extractor.root)) == kept

    def test_desde_una_parte_que_no_se_puede_abrir_solo_caen_las_de_su_esquema(self, extractor, monkeypatch):
        """Sin el índice del juego se buscan por nombre: la misma base, el mismo
        esquema y mayúsculas libres solo en la extensión."""
        monkeypatch.setattr(rarfile, "is_rarfile", lambda path: True)
        for name in ("serie.rar", "serie.r00", "serie.r01", "serie.R02", "serie.part1.rar", "Serie.r03"):
            (extractor.root / name).write_bytes(b"x")

        msg = extractor.module.delete_compressed(str(extractor.root / "serie.r00"))

        assert sorted(os.listdir(extractor.root)) == ["Serie.r03", "serie.part1.rar"]
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
        parts = write_rar_set(extractor.root, "pelicula", [("a.txt", b"a"), ("b.mkv", MOVIE)], 2)
        others = ["pelicula 2.part1.rar", "pelicula 2.part2.rar"]
        for name in others:
            (extractor.root / name).write_bytes(b"ajeno")

        extractor.module.delete_compressed(str(parts[0]))

        assert sorted(os.listdir(extractor.root)) == sorted(others)


class TestMensajeDeConservado:
    def test_lleva_el_nombre_y_los_textos(self, extractor):
        msg = extractor.module.kept_message("Canción del año.zip")

        assert "`Canción del año.zip`" in msg
        assert get_text("file_kept_title") in msg
        assert get_text("file_kept_desc") in msg


# --- Nombres que no son UTF-8 ----------------------------------------------------

@pytest.fixture
def telethon_edit(extractor, monkeypatch):
    """safe_edit codificando el texto como Telethon antes de enviarlo: con un
    nombre con sustitutos la edición falla, como en Telegram."""
    real_edit = extractor.module.safe_edit

    async def edit(message, text=None, **kwargs):
        markdown.parse(text)
        text.encode("utf-8")
        return await real_edit(message, text, **kwargs)

    monkeypatch.setattr(extractor.module, "safe_edit", edit)


class TestNombreQueNoEsUtf8:
    """La ficha de un comprimido con el nombre en Latin-1 ya se enseñaba, con
    su Descomprimir, pero «Descomprimiendo» llevaba el nombre tal cual:
    Telethon no podía enviarlo, y el botón no hacía nada ni decía nada."""

    @pytest.mark.parametrize("after,action", [
        ("ASK", None), ("ASK", "delcompressed"), ("ASK", "keepcompressed"),
        ("DELETE", None), ("KEEP", None),
    ])
    def test_se_descomprime_y_se_cuenta_con_el_nombre_escapado(
        self, extractor, telethon_edit, sent_messages, after, action
    ):
        import settings

        settings.put("extract.after", after)
        archive = _not_utf8_zip(extractor.root)

        extractor.extract(archive)
        if action:
            extractor.act(action, archive)

        root = os.fsencode(extractor.root)
        assert os.listdir(os.path.join(root, b"caf\xe9")) == [b"dentro.txt"]
        deleted = after == "DELETE" or action == "delcompressed"
        assert (b"caf\xe9.zip" in os.listdir(root)) is not deleted
        assert sent_messages[0][1] == get_text("decompressing_file", "caf\\xe9.zip")
        assert "caf\\xe9.zip" in markdown.parse(extractor.last())[0]

    def test_si_la_carpeta_ya_existe_lo_dice_con_el_nombre_escapado(self, extractor, telethon_edit):
        archive = _not_utf8_zip(extractor.root)
        os.mkdir(os.path.join(os.fsencode(extractor.root), b"caf\xe9"))

        extractor.extract(archive)

        assert extractor.last() == get_text("error_extraction_folder_exists", "caf\\xe9")

    def test_borrar_un_rar_por_partes_las_nombra_escapadas(self, extractor):
        parts = write_rar_set(extractor.root, os.fsdecode(b"caf\xe9"), [("a.txt", b"a"), ("b.mkv", MOVIE)], 2)

        msg = extractor.module.delete_compressed(str(parts[0]))

        msg.encode("utf-8")  # lo que hace Telethon al enviarlo
        assert os.listdir(extractor.root) == []
        assert "`caf\\xe9.part1.rar`" in msg and "`caf\\xe9.part2.rar`" in msg
