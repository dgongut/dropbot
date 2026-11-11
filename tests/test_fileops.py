"""Mover, sugerir nombres, renombrar en lote, buscar y la carpeta de la serie
(ver fileops.py). Todo sobre el disco, sin Telegram."""

import errno
import os

import pytest

import fileops
import folders
import settings


def _touch(path, size=4):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    return path


def _tree(root):
    found = []
    for directory, _, files in os.walk(root):
        found += [os.path.relpath(os.path.join(directory, name), root) for name in files]
    return sorted(found)


class TestMover:
    def test_un_fichero_a_otra_carpeta(self, library):
        source = _touch(os.path.join(library.base, "peli.mkv"))

        moved = fileops.move_item(source, library.video)

        assert moved == os.path.join(library.video, "peli.mkv")
        assert os.path.isfile(moved) and not os.path.exists(source)

    def test_una_carpeta_con_todo_lo_que_lleva(self, library):
        _touch(os.path.join(library.base, "Serie", "T1", "01.mkv"))

        moved = fileops.move_item(os.path.join(library.base, "Serie"), library.media)

        assert _tree(moved) == [os.path.join("T1", "01.mkv")]
        assert not os.path.exists(os.path.join(library.base, "Serie"))

    def test_nunca_pisa_lo_que_ya_hay(self, library):
        source = _touch(os.path.join(library.base, "peli.mkv"), size=1)
        existing = _touch(os.path.join(library.video, "peli.mkv"), size=9)

        with pytest.raises(fileops.MoveError) as failure:
            fileops.move_item(source, library.video)

        assert failure.value.code == "exists"
        assert os.path.getsize(existing) == 9 and os.path.exists(source)

    @pytest.mark.parametrize("target, code", [("base", "same"), ("base/Serie/dentro", "inside"), ("no_existe", "no_target")])
    def test_movimientos_que_no_tienen_sentido(self, library, tmp_path, target, code):
        _touch(os.path.join(library.base, "Serie", "dentro", "01.mkv"))

        with pytest.raises(fileops.MoveError) as failure:
            fileops.move_item(os.path.join(library.base, "Serie"), str(tmp_path / target))
        assert failure.value.code == code

    def test_lo_que_ya_no_existe(self, library):
        with pytest.raises(fileops.MoveError) as failure:
            fileops.move_item(os.path.join(library.base, "nada.mkv"), library.video)
        assert failure.value.code == "gone"


class TestMoverEntreMontajesDelMismoDisco:
    """Dos volúmenes de Docker sobre el mismo disco dicen ser el mismo
    dispositivo, pero el sistema no deja renombrar de uno a otro (EXDEV): el
    movimiento fallaba con «Invalid cross-device link» en vez de copiar."""

    @pytest.fixture(autouse=True)
    def two_mounts(self, monkeypatch):
        real_rename = os.rename

        def rename(source, target):
            if os.path.dirname(source) != os.path.dirname(target):
                raise OSError(errno.EXDEV, "Invalid cross-device link", source, None, target)
            real_rename(source, target)

        monkeypatch.setattr(fileops.os, "rename", rename)

    def test_un_fichero_se_copia(self, library):
        source = _touch(os.path.join(library.base, "peli.mkv"), size=3000)

        moved = fileops.move_item(source, library.media)

        assert os.path.getsize(moved) == 3000 and not os.path.exists(source)

    def test_una_carpeta_se_copia_entera(self, library):
        folder = os.path.join(library.base, "Serie")
        _touch(os.path.join(folder, "T1", "01.mkv"), size=10)

        moved = fileops.move_item(folder, library.media)

        assert _tree(moved) == [os.path.join("T1", "01.mkv")] and not os.path.exists(folder)


class TestMoverEntreDiscos:
    @pytest.fixture(autouse=True)
    def another_disk(self, monkeypatch):
        monkeypatch.setattr(fileops, "_same_device", lambda source, target: False)

    def test_un_fichero_se_copia_y_se_borra_el_original(self, library):
        source = _touch(os.path.join(library.base, "peli.mkv"), size=3000)

        moved = fileops.move_item(source, library.video)

        assert os.path.getsize(moved) == 3000 and not os.path.exists(source)
        assert os.listdir(library.video) == ["peli.mkv"]

    def test_una_carpeta_se_copia_entera_con_sus_enlaces(self, library):
        folder = os.path.join(library.base, "Serie")
        _touch(os.path.join(folder, "T1", "01.mkv"), size=10)
        os.symlink("T1/01.mkv", os.path.join(folder, "ultimo.mkv"))

        moved = fileops.move_item(folder, library.media)

        assert _tree(moved) == [os.path.join("T1", "01.mkv"), "ultimo.mkv"]
        assert os.readlink(os.path.join(moved, "ultimo.mkv")) == "T1/01.mkv"
        assert not os.path.exists(folder)
        assert os.listdir(library.media) == ["Serie"], "no queda ningún oculto de la copia"

    def test_si_no_cabe_no_se_empieza(self, library, monkeypatch):
        from types import SimpleNamespace
        monkeypatch.setattr(fileops.shutil, "disk_usage", lambda path: SimpleNamespace(free=5))
        source = _touch(os.path.join(library.base, "peli.mkv"), size=100)

        with pytest.raises(fileops.MoveError) as failure:
            fileops.move_item(source, library.video)

        assert failure.value.code == "space"
        assert os.path.exists(source) and os.listdir(library.video) == []

    def test_si_la_copia_falla_el_original_queda_intacto(self, library, monkeypatch):
        folder = os.path.join(library.base, "Serie")
        _touch(os.path.join(folder, "01.mkv"))
        _touch(os.path.join(folder, "02.mkv"))
        real = fileops.shutil.copyfileobj
        calls = []

        def failing(reader, writer, *args):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(5, "Input/output error")
            return real(reader, writer, *args)

        monkeypatch.setattr(fileops.shutil, "copyfileobj", failing)

        with pytest.raises(OSError):
            fileops.move_item(folder, library.media)

        assert _tree(folder) == ["01.mkv", "02.mkv"]
        assert os.listdir(library.media) == []

    def test_una_tuberia_no_se_copia_ni_se_pierde(self, library):
        folder = os.path.join(library.base, "Rara")
        _touch(os.path.join(folder, "a.txt"))
        os.mkfifo(os.path.join(folder, "tuberia"))

        with pytest.raises(fileops.MoveError) as failure:
            fileops.move_item(folder, library.media)

        assert failure.value.code == "special"
        assert os.path.exists(os.path.join(folder, "tuberia")) and os.listdir(library.media) == []


class TestSugerencia:
    def test_una_pelicula_y_un_episodio(self, library):
        assert fileops.suggest_name("The.Matrix.1999.1080p.BluRay.x264.mkv") == "The Matrix (1999) - 1080p.mkv"
        assert fileops.suggest_name("Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL.mkv") == "14x04 - Chicago Fire - 1080p.mkv"

    def test_lo_que_ya_tiene_su_nombre_no_sugiere_nada(self, library):
        assert fileops.suggest_name("The Matrix (1999) - 1080p.mkv") is None
        assert fileops.suggest_name("notas.txt") is None

    def test_el_prefijo_de_temporada_sigue_al_idioma(self, library):
        assert fileops.suggest_name("Mad.Men.S02.1080p.WEB-DL") == "T2 - Mad Men - 1080p"
        settings.put("language", "EN")
        assert fileops.suggest_name("Mad.Men.S02.1080p.WEB-DL") == "S2 - Mad Men - 1080p"

    def test_usa_la_plantilla_del_usuario(self, library):
        settings.put("rename.template_movie", "{titulo} - {año}")
        assert fileops.suggest_name("The.Matrix.1999.1080p.mkv") == "The Matrix - 1999"

    def test_un_fichero_toma_de_su_carpeta_lo_que_le_falta(self, library):
        episode = _touch(os.path.join(library.base, "Show.Name.S02.1080p", "01.mkv"))
        _touch(os.path.join(library.base, "Show.Name.S02.1080p", "02.mkv"))

        assert fileops.suggest_for(episode) == "2x01 - Show Name - 1080p.mkv"

    def test_solo_los_videos_y_nunca_la_muestra(self, library):
        folder = os.path.join(library.base, "The.Matrix.1999.1080p")
        assert fileops.suggest_for(_touch(os.path.join(folder, "info.nfo"))) is None
        assert fileops.suggest_for(_touch(os.path.join(folder, "Sample", "sample.mkv"))) is None
        assert fileops.suggest_for(_touch(os.path.join(library.base, "The.Matrix.1999.1080p.pdf"))) is None

    def test_una_carpeta_que_cuelga_de_descargas(self, library):
        folder = os.path.join(library.base, "Mad.Men.2007.COMPLETE.1080p.NF.WEB-DL")
        os.makedirs(folder)
        assert fileops.suggest_for(folder) == "Mad Men (2007) - 1080p"

    @pytest.mark.parametrize("name", ["", ".oculto", "a/b", "a\\b", "x‮y.mkv", "a" * 300])
    def test_un_nombre_que_no_vale_no_se_sugiere(self, name):
        assert not fileops.usable_name(name)


class TestSubtitulos:
    def test_van_con_su_video(self, library):
        folder = library.base
        video = _touch(os.path.join(folder, "The.Matrix.1999.1080p.mkv"))
        for name in ("The.Matrix.1999.1080p.srt", "The.Matrix.1999.1080p.es.srt", "Otra.srt", "The.Matrix.1999.1080p.nfo"):
            _touch(os.path.join(folder, name))

        new_path, subtitles = fileops.rename_item(video, "The Matrix (1999) - 1080p.mkv")

        assert subtitles == 2
        assert sorted(os.listdir(folder)) == [
            "Otra.srt", "The Matrix (1999) - 1080p.es.srt", "The Matrix (1999) - 1080p.mkv",
            "The Matrix (1999) - 1080p.srt", "The.Matrix.1999.1080p.nfo"]
        assert new_path == os.path.join(folder, "The Matrix (1999) - 1080p.mkv")

    def test_el_subtitulo_es_del_video_de_nombre_mas_largo(self, library):
        _touch(os.path.join(library.base, "Alien.mkv"))
        _touch(os.path.join(library.base, "Alien Resurrection.mkv"))
        _touch(os.path.join(library.base, "Alien Resurrection.es.srt"))

        assert fileops.subtitle_renames(os.path.join(library.base, "Alien.mkv"), "Alien (1979).mkv") == []
        assert fileops.subtitle_renames(os.path.join(library.base, "Alien Resurrection.mkv"), "AR.mkv") == [
            (os.path.join(library.base, "Alien Resurrection.es.srt"), "AR.es.srt")]

    def test_renombrar_no_pisa_nada(self, library):
        video = _touch(os.path.join(library.base, "a.mkv"), size=1)
        _touch(os.path.join(library.base, "b.mkv"), size=7)

        with pytest.raises(FileExistsError):
            fileops.rename_item(video, "b.mkv")
        assert os.path.getsize(os.path.join(library.base, "b.mkv")) == 7

    def test_una_carpeta_no_arrastra_nada(self, library):
        folder = os.path.join(library.base, "Carpeta.mkv")
        os.makedirs(folder)
        _touch(os.path.join(library.base, "Carpeta.srt"))

        _, subtitles = fileops.rename_item(folder, "Otra")

        assert subtitles == 0 and os.path.exists(os.path.join(library.base, "Carpeta.srt"))


class TestRenombrarTodo:
    @pytest.fixture
    def series(self, library):
        top = os.path.join(library.base, "Mad.Men.2007.COMPLETE.1080p.NF.WEB-DL")
        season = os.path.join(top, "Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2")
        _touch(os.path.join(season, "Mad.Men.S01E01.1080p.mkv"))
        _touch(os.path.join(season, "Mad.Men.S01E01.1080p.es.srt"))
        _touch(os.path.join(season, "Mad.Men.S01E02.1080p.mkv"))
        _touch(os.path.join(top, "Extras", "making.of.txt"))
        return top

    def test_la_carpeta_las_temporadas_y_los_episodios(self, library, series):
        plan = fileops.build_plan(series)

        assert [(os.path.basename(path), name) for path, name in plan.files] == [
            ("Mad.Men.S01E01.1080p.mkv", "1x01 - Mad Men - 1080p.mkv"),
            ("Mad.Men.S01E01.1080p.es.srt", "1x01 - Mad Men - 1080p.es.srt"),
            ("Mad.Men.S01E02.1080p.mkv", "1x02 - Mad Men - 1080p.mkv")]
        assert [(os.path.basename(path), name) for path, name in plan.folders] == [
            ("Mad.Men.2007.S01.NF.WEB-DL.1080P.AV1-Txv2", "T1 - Mad Men - 1080p"),
            ("Mad.Men.2007.COMPLETE.1080p.NF.WEB-DL", "Mad Men (2007) - 1080p")]
        assert plan.skipped == 0

    def test_aplicado_deja_el_arbol_como_se_enseño(self, library, series):
        renamed = []
        files_done, folders_done, errors = fileops.apply_plan(fileops.build_plan(series), lambda old, new: renamed.append(new))

        assert (files_done, folders_done, errors) == (3, 2, [])
        assert _tree(os.path.join(library.base, "Mad Men (2007) - 1080p")) == [
            os.path.join("Extras", "making.of.txt"),
            os.path.join("T1 - Mad Men - 1080p", "1x01 - Mad Men - 1080p.es.srt"),
            os.path.join("T1 - Mad Men - 1080p", "1x01 - Mad Men - 1080p.mkv"),
            os.path.join("T1 - Mad Men - 1080p", "1x02 - Mad Men - 1080p.mkv")]
        assert len(renamed) == 5
        # Hecho, no queda nada que sugerir
        again = fileops.build_plan(os.path.join(library.base, "Mad Men (2007) - 1080p"))
        assert not again.files and not again.folders

    def test_sin_la_propia_carpeta(self, library, series):
        plan = fileops.build_plan(series, include_top=False)
        assert [name for _, name in plan.folders] == ["T1 - Mad Men - 1080p"]

    def test_dos_que_darian_el_mismo_nombre_no_chocan(self, library):
        top = os.path.join(library.base, "Show.Name.S01.1080p")
        _touch(os.path.join(top, "Show.Name.S01E01.1080p.WEB.mkv"))
        _touch(os.path.join(top, "Show.Name.S01E01.1080p.BluRay.mkv"))

        plan = fileops.build_plan(top, include_top=False)

        assert len(plan.files) == 1 and plan.skipped == 1

    def test_un_nombre_ocupado_al_aplicar_se_cuenta_como_error(self, library, series):
        plan = fileops.build_plan(series, include_top=False)
        season = os.path.dirname(plan.files[0][0])
        _touch(os.path.join(season, "1x02 - Mad Men - 1080p.mkv"), size=99)

        files_done, _, errors = fileops.apply_plan(plan)

        assert files_done == 2 and len(errors) == 1
        assert os.path.getsize(os.path.join(os.path.dirname(season), "T1 - Mad Men - 1080p", "1x02 - Mad Men - 1080p.mkv")) == 99

    def test_un_fichero_suelto(self, library):
        video = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.mkv"))
        _touch(os.path.join(library.base, "The.Matrix.1999.1080p.srt"))

        plan = fileops.build_plan(video)

        assert [name for _, name in plan.files] == ["The Matrix (1999) - 1080p.mkv", "The Matrix (1999) - 1080p.srt"]

    def test_varios_elementos_en_un_plan(self, library, series):
        video = _touch(os.path.join(library.base, "The.Matrix.1999.1080p.mkv"))

        plan = fileops.merge_plans([fileops.build_plan(series), fileops.build_plan(video)])

        assert len(plan.files) == 4 and len(plan.folders) == 2
        assert fileops.apply_plan(plan)[2] == []


class TestBuscar:
    @pytest.fixture
    def files(self, library):
        for path in ("La.Casa.de.Papel.S01E01.mkv", "Series/La Casa de Papel/1x02.mkv", "Canción.mp3", ".oculto/casa.mkv"):
            _touch(os.path.join(library.base, path))
        _touch(os.path.join(library.video, "Casa.Tomada.1080p.mkv"))
        return library

    def _names(self, found, library):
        return sorted(os.path.relpath(path, os.path.dirname(library.base)) for path in found)

    def test_las_palabras_en_cualquier_orden_sin_tildes_ni_mayusculas(self, files):
        assert self._names(fileops.search("papel CASA"), files) == [
            "base/La.Casa.de.Papel.S01E01.mkv", "base/Series/La Casa de Papel"]
        assert self._names(fileops.search("cancion"), files) == ["base/Canción.mp3"]

    def test_entre_comillas_la_frase_exacta(self, files):
        assert self._names(fileops.search('"casa de"'), files) == [
            "base/La.Casa.de.Papel.S01E01.mkv", "base/Series/La Casa de Papel"]
        assert fileops.search('"papel casa"') == []

    def test_una_ruta_tiene_que_aparecer_tal_cual(self, files):
        assert self._names(fileops.search(f"{files.video} casa"), files) == ["video/Casa.Tomada.1080p.mkv"]

    def test_sin_nada_que_buscar_no_devuelve_todo(self, files):
        assert fileops.search("") == [] and fileops.search('""') == [] and fileops.search("...") == []

    def test_lo_oculto_no_sale_y_hay_un_tope(self, files):
        assert not any(".oculto" in path for path in fileops.search("casa"))
        assert len(fileops.search("casa", limit=2)) == 2


class TestCarpetaDeLaSerie:
    def test_un_episodio_sin_el_nombre_de_la_serie_va_con_sus_vecinos(self, library):
        """"002 - Pink Pajamas (1964).mkv" no dice de qué serie es: se sabe
        por la carpeta que tiene el 001 numerado igual."""
        show = os.path.join(library.base, "Pantera Rosa")
        _touch(os.path.join(show, "001 - The Pink Phink (1964).mkv"))
        second = _touch(os.path.join(library.base, "002 - Pink Pajamas (1964).mkv"))
        third = _touch(os.path.join(library.base, "003 - We Give Pink Stamps (1965).mkv"))

        # Se propone, nunca se da por segura: es una suposición por la
        # numeración, y moverlo sin preguntar sería arriesgado
        assert fileops.series_candidates(second) == [(show, False)]
        assert fileops.series_candidates(third) == [(show, False)]
        assert fileops.name_parser.series_auto_dir(fileops.series_candidates(second)) is None

    def test_con_dos_carpetas_numeradas_igual_solo_se_propone(self, library):
        show = os.path.join(library.base, "Pantera Rosa")
        other = os.path.join(library.base, "Otra serie")
        _touch(os.path.join(show, "001 - The Pink Phink (1964).mkv"))
        _touch(os.path.join(other, "001 - Piloto.mkv"))
        _touch(os.path.join(other, "004 - Cuarto.mkv"))
        new = _touch(os.path.join(library.base, "002 - Pink Pajamas (1964).mkv"))

        assert sorted(fileops.series_candidates(new)) == [(other, False), (show, False)]

    def test_un_numero_que_la_carpeta_ya_tiene_o_un_año_no_cuentan(self, library):
        show = os.path.join(library.base, "Pantera Rosa")
        _touch(os.path.join(show, "001 - The Pink Phink (1964).mkv"))

        assert fileops.series_candidates(_touch(os.path.join(library.base, "001 - Otra cosa.mkv"))) == []
        assert fileops.series_candidates(_touch(os.path.join(library.base, "1964 - Documental.mkv"))) == []
        assert fileops.series_candidates(_touch(os.path.join(library.base, "02 - Cancion.mp3"))) == []

    def test_una_carpeta_con_otro_nombre_y_solo_episodios_de_la_serie_vale_como_suya(self, library):
        """"Breakingg BAAAD" con capítulos de Breaking Bad es la carpeta de la
        serie aunque el nombre no lo diga: con «Directo», el siguiente entra."""
        show = os.path.join(library.video, "Breakingg BAAAD")
        _touch(os.path.join(show, "1x03 - Breaking Bad - 720p.mkv"))
        _touch(os.path.join(show, "1x03 - Breaking Bad - 720p.es.srt"))
        new = _touch(os.path.join(library.base, "Breaking.Bad.2008.S02E01.720p.HDTV.x264-NTb.mkv"))

        assert fileops.series_candidates(new) == [(show, True)]

    def test_una_carpeta_con_mas_cosas_solo_se_propone(self, library):
        """Un «Pendientes» con un capítulo suelto no es la carpeta de la serie."""
        mixed = os.path.join(library.video, "Pendientes")
        _touch(os.path.join(mixed, "1x03 - Breaking Bad - 720p.mkv"))
        _touch(os.path.join(mixed, "The.Matrix.1999.1080p.mkv"))
        new = _touch(os.path.join(library.base, "Breaking.Bad.2008.S02E01.720p.HDTV.x264-NTb.mkv"))

        assert fileops.series_candidates(new) == [(mixed, False)]

    def test_propone_la_carpeta_donde_estan_los_demas(self, library):
        show = os.path.join(library.media, "Series", "Chicago Fire")
        _touch(os.path.join(show, "14x03 - Chicago Fire.mkv"))
        _touch(os.path.join(library.media, "Series", "Chicago Med", "1x01 - Chicago Med.mkv"))
        folders.add_favorite(library.media)
        new = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.1080p.WEB.h264-ETHEL.mkv"))

        assert fileops.series_folders(new) == [show]
        assert fileops.series_candidates(new) == [(show, True)]

    def test_la_temporada_siguiente_va_a_su_carpeta_aunque_no_exista(self, library):
        _touch(os.path.join(library.video, "Mad Men", "Season 01", "Mad.Men.S01E01.mkv"))
        new = _touch(os.path.join(library.base, "Mad.Men.S02E01.1080p.mkv"))

        assert fileops.series_folders(new) == [os.path.join(library.video, "Mad Men", "Season 02")]

    def test_las_carpetas_de_descarga_no_se_proponen(self, library):
        _touch(os.path.join(library.video, "Chicago.Fire.S14E03.mkv"))
        new = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.mkv"))

        assert fileops.series_folders(new) == []

    def test_una_pelicula_o_lo_que_ya_esta_en_su_sitio_no_se_mueve(self, library):
        show = os.path.join(library.video, "Chicago Fire")
        _touch(os.path.join(show, "14x03 - Chicago Fire.mkv"))

        assert fileops.series_folders(_touch(os.path.join(show, "Chicago.Fire.S14E04.mkv"))) == []
        assert fileops.series_folders(_touch(os.path.join(library.base, "The.Matrix.1999.1080p.mkv"))) == []

    def test_nunca_propone_una_carpeta_fuera_de_un_volumen(self, library, volumes):
        show = os.path.join(library.media, "Chicago Fire")
        _touch(os.path.join(show, "14x03 - Chicago Fire.mkv"))
        folders.add_favorite(library.media)
        new = _touch(os.path.join(library.base, "Chicago.Fire.S14E04.mkv"))
        assert fileops.series_folders(new) == [show]

        volumes.discard(library.media)

        assert fileops.series_folders(new) == []
