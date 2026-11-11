"""Las carpetas en las que el bot puede guardar (ver folders.py).

La regla que no se puede romper: solo dentro de un volumen. Una ruta fuera de
uno vive en la capa del contenedor y se pierde al actualizarlo.
"""

import os

import pytest

import config
import folders
import settings


class TestNormalizar:
    @pytest.mark.parametrize("raw, expected", [
        ("/downloads/Series", "/downloads/Series"),
        ("  /downloads/Series/  ", "/downloads/Series"),
        ("/downloads//Series/./Mad Men", "/downloads/Series/Mad Men"),
        ("/downloads/a/../b", "/downloads/b"),
        ("//downloads", "/downloads"),
    ])
    def test_deja_la_ruta_limpia(self, raw, expected):
        assert folders.normalize(raw) == expected

    @pytest.mark.parametrize("raw", [
        "", "   ", "Series", "downloads/Series", "~/Series", "/downloads/.oculta",
        "/downloads/a\nb", "/downloads/x‮y", "/downloads/" + "a" * 300, "/" + "a/" * 600,
    ])
    def test_rechaza_lo_que_no_es_una_ruta_que_valga(self, raw):
        assert folders.normalize(raw) is None


class TestDentroDeUnVolumen:
    def test_la_general_y_lo_que_cuelga_de_ella_valen_siempre(self, library):
        assert folders.is_allowed(library.base)
        assert folders.is_allowed(os.path.join(library.base, "Series", "Mad Men"))

    def test_un_volumen_montado_vale_aunque_no_sea_de_ningun_tipo(self, library):
        assert folders.is_allowed(library.media)
        assert folders.is_allowed(os.path.join(library.media, "Series"))

    def test_fuera_de_un_volumen_no_vale(self, library):
        assert not folders.is_allowed(library.outside)
        assert not folders.is_allowed(os.path.join(library.outside, "Series"))
        assert not folders.is_allowed("/")

    def test_config_no_vale_aunque_sea_un_volumen(self, library, config_dir, volumes):
        volumes.add(str(config_dir))
        assert not folders.is_allowed(str(config_dir))
        assert not folders.is_allowed(os.path.join(str(config_dir), "state"))

    @pytest.mark.parametrize("path", ["/proc/1", "/etc/cron.d", "/app/cookies", "/dev/shm", "/usr/local"])
    def test_las_del_sistema_no_valen_aunque_esten_montadas(self, library, volumes, path):
        volumes.add(path)
        assert not folders.is_allowed(path)

    def test_un_enlace_que_sale_del_volumen_no_vale(self, library):
        """El bot va como root: un enlace dentro de /downloads hacia fuera le
        dejaría escribir donde no debe."""
        link = os.path.join(library.base, "enlace")
        os.symlink(library.outside, link)

        assert not folders.is_allowed(link)
        assert not folders.is_allowed(os.path.join(link, "dentro"))
        with pytest.raises(folders.FolderError) as failure:
            folders.check(os.path.join(link, "nueva"), create=True)
        assert failure.value.code == "outside"
        assert not os.path.exists(os.path.join(library.outside, "nueva"))

    def test_un_enlace_entre_volumenes_vale(self, library):
        link = os.path.join(library.base, "a_media")
        os.symlink(library.media, link)
        assert folders.is_allowed(link)


class TestComprobar:
    def test_crea_la_carpeta_si_se_pide(self, library):
        path = os.path.join(library.media, "Series", "Mad Men")

        assert folders.check(path, create=True) == path
        assert os.path.isdir(path)

    def test_sin_crear_tiene_que_existir(self, library):
        with pytest.raises(folders.FolderError) as failure:
            folders.check(os.path.join(library.media, "no_existe"))
        assert failure.value.code == "missing"

    def test_un_fichero_no_es_una_carpeta(self, library):
        target = os.path.join(library.base, "fichero.txt")
        open(target, "w").close()
        with pytest.raises(folders.FolderError) as failure:
            folders.check(target, create=True)
        assert failure.value.code == "not_folder"

    def test_fuera_de_un_volumen_ni_se_crea(self, library):
        path = os.path.join(library.outside, "nueva")
        with pytest.raises(folders.FolderError) as failure:
            folders.check(path, create=True)
        assert failure.value.code == "outside"
        assert not os.path.exists(path)

    def test_una_ruta_relativa_no_vale(self, library):
        with pytest.raises(folders.FolderError) as failure:
            folders.check("Series", create=True)
        assert failure.value.code == "invalid"


class TestCarpetaDeCadaTipo:
    def test_sin_elegir_nada_mandan_los_volumenes(self, library):
        assert config.DOWNLOAD_PATHS["video"] == library.video
        assert config.DOWNLOAD_PATHS["url_video"] == library.video
        assert config.DOWNLOAD_PATHS["audio"] == library.base
        assert folders.chosen_paths() == {}

    def test_la_elegida_manda_y_se_aplica_sin_reiniciar(self, library):
        chosen = os.path.join(library.media, "Musica")
        paths = config.DOWNLOAD_PATHS

        folders.set_kind_folder("audio", chosen)

        # El mismo diccionario: los demás módulos lo tienen importado por su nombre
        assert config.DOWNLOAD_PATHS is paths
        assert paths["audio"] == chosen and os.path.isdir(chosen)
        # Lo de los enlaces sigue al resto del audio, como siempre
        assert paths["url_audio"] == chosen
        assert paths["video"] == library.video
        assert settings.folder_paths() == {"audio": chosen}
        assert folders.is_chosen("audio") and not folders.is_chosen("video")

    def test_los_enlaces_pueden_ir_aparte(self, library):
        links = os.path.join(library.base, "YouTube")

        folders.set_kind_folder("url_video", links)

        assert config.DOWNLOAD_PATHS["url_video"] == links
        assert config.DOWNLOAD_PATHS["video"] == library.video

    def test_volver_a_la_de_por_defecto(self, library):
        folders.set_kind_folder("video", os.path.join(library.media, "Pelis"))

        folders.set_kind_folder("video", None)

        assert config.DOWNLOAD_PATHS["video"] == library.video
        assert settings.folder_paths() == {}

    def test_no_se_puede_elegir_una_fuera_de_un_volumen(self, library):
        with pytest.raises(folders.FolderError):
            folders.set_kind_folder("video", library.outside)
        assert config.DOWNLOAD_PATHS["video"] == library.video
        assert settings.folder_paths() == {}

    def test_un_tipo_que_no_existe_no_se_guarda(self, library):
        with pytest.raises(folders.FolderError):
            folders.set_kind_folder("other", library.media)

    def test_una_elegida_que_deja_de_estar_montada_se_ignora(self, library, volumes):
        """Se quitó el volumen del compose: guardar ahí sería guardar dentro
        del contenedor. Se usa la de por defecto, y el ajuste no se pierde por
        si el volumen vuelve."""
        chosen = os.path.join(library.media, "Pelis")
        folders.set_kind_folder("video", chosen)

        volumes.discard(library.media)
        folders.apply()

        assert config.DOWNLOAD_PATHS["video"] == library.video
        assert settings.folder_paths() == {"video": chosen}
        assert not folders.is_chosen("video")

    def test_un_settings_json_con_basura_no_rompe_nada(self, library):
        import store
        store.set("folders.paths", {"video": 5, "inventado": "/x", "audio": "relativa"})
        store.set("folders.favorites", "no es una lista")

        folders.apply()

        assert config.DOWNLOAD_PATHS["video"] == library.video
        assert folders.favorites() == []


class TestFavoritas:
    def test_se_anaden_sin_repetir_y_se_quitan(self, library):
        series = os.path.join(library.media, "Series")

        assert folders.add_favorite(series) is True
        assert folders.add_favorite(series + "/") is False
        assert folders.favorites() == [series]

        folders.remove_favorite(series)
        assert folders.favorites() == []

    def test_una_fuera_de_un_volumen_no_se_anade(self, library):
        with pytest.raises(folders.FolderError):
            folders.add_favorite(library.outside)
        assert folders.favorites() == []

    def test_las_que_dejan_de_valer_no_se_ofrecen(self, library, volumes):
        folders.add_favorite(library.media)
        volumes.discard(library.media)
        assert folders.favorites() == []

    def test_lo_conocido_son_las_favoritas_y_las_de_descarga_sin_repetir(self, library):
        series = os.path.join(library.media, "Series")
        folders.add_favorite(series)
        folders.add_favorite(library.video)

        assert folders.known() == [series, library.video, library.base]
        assert folders.roots() == [library.base, library.video]

    def test_la_carpeta_conocida_que_contiene_a_otra(self, library):
        folders.add_favorite(os.path.join(library.base, "Series"))

        assert folders.root_of(os.path.join(library.base, "Series", "Mad Men")) == os.path.join(library.base, "Series")
        assert folders.root_of(os.path.join(library.base, "otra")) == library.base
        assert folders.root_of(library.outside) is None
        # "/tmp/base2" no cuelga de "/tmp/base"
        assert folders.root_of(library.base + "2") is None

    def test_las_subcarpetas_sin_las_ocultas_y_por_nombre(self, library):
        for name in ("zeta", "Alfa", ".oculta"):
            os.makedirs(os.path.join(library.base, name))
        open(os.path.join(library.base, "fichero.txt"), "w").close()

        assert [os.path.basename(path) for path in folders.subfolders(library.base)] == ["Alfa", "zeta"]

    def test_de_un_volumen_no_se_sube_fuera(self, library):
        assert folders.parent_of(os.path.join(library.media, "Series")) == library.media
        assert folders.parent_of(library.media) is None
