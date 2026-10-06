"""Almacenamiento (`store`), validación de ajustes (`settings`) y migración.

Lo que no cubre test_config: escrituras que fallan a mitad, lotes anidados,
un /config que no se puede crear, y los límites de cada ajuste.
"""

import json
import os

import pytest

import migration
import settings
import store


def _stored(config_dir):
    return json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))


@pytest.fixture
def writes(monkeypatch):
    """Cuenta las escrituras reales de settings.json sin impedirlas."""
    done = []
    real = store.write_document

    def counting(path, document):
        done.append(json.loads(json.dumps(document)))
        real(path, document)

    monkeypatch.setattr(store, "write_document", counting)
    return done


class TestEscrituraAtomica:
    def test_si_falla_el_rename_el_original_queda_intacto(self, config_dir, monkeypatch):
        store.set("language", "EN")
        original = (config_dir / "settings.json").read_text(encoding="utf-8")

        def broken_replace(src, dst):
            raise OSError("disco lleno")

        monkeypatch.setattr(store.os, "replace", broken_replace)
        store.set("language", "ES")

        assert (config_dir / "settings.json").read_text(encoding="utf-8") == original
        assert not (config_dir / "settings.json.tmp").exists()

    def test_si_falla_a_mitad_del_volcado_no_deja_nada_a_medias(self, tmp_path):
        path = tmp_path / "doc.json"
        store.write_document(str(path), {"bien": 1})

        store.write_document(str(path), {"mal": object()})

        assert json.loads(path.read_text(encoding="utf-8")) == {"bien": 1}
        assert not (tmp_path / "doc.json.tmp").exists()

    def test_si_falla_el_fsync_el_original_queda_intacto(self, tmp_path, monkeypatch):
        path = tmp_path / "doc.json"
        store.write_document(str(path), {"bien": 1})

        def broken_fsync(fd):
            raise OSError("E/S")

        monkeypatch.setattr(store.os, "fsync", broken_fsync)
        store.write_document(str(path), {"nuevo": 2})

        assert json.loads(path.read_text(encoding="utf-8")) == {"bien": 1}
        assert not (tmp_path / "doc.json.tmp").exists()

    def test_crea_la_carpeta_si_no_existe_y_conserva_unicode(self, tmp_path):
        path = tmp_path / "a" / "b" / "doc.json"
        store.write_document(str(path), {"título": "Canción ñ"})
        assert "Canción ñ" in path.read_text(encoding="utf-8")


class TestLotes:
    def test_un_lote_escribe_una_sola_vez(self, config_dir, writes):
        with store.batch():
            store.set("language", "EN")
            store.set("urls.auto_send", "SEND")
            assert writes == []
        assert len(writes) == 1
        assert writes[0]["language"] == "EN" and writes[0]["urls"]["auto_send"] == "SEND"

    def test_lotes_anidados_escriben_al_cerrar_el_de_fuera(self, config_dir, writes):
        with store.batch():
            store.set("language", "EN")
            with store.batch():
                store.set("urls.auto_send", "STORE")
            assert writes == []
            store.set("video.hw", "QSV")
        assert len(writes) == 1
        assert _stored(config_dir)["video"]["hw"] == "QSV"

    def test_un_lote_sin_cambios_no_escribe(self, config_dir, writes):
        with store.batch():
            store.get("language")
        assert writes == []
        assert not (config_dir / "settings.json").exists()

    def test_un_lote_que_lanza_guarda_lo_hecho_y_propaga(self, config_dir, writes):
        with pytest.raises(RuntimeError):
            with store.batch():
                store.set("language", "EN")
                raise RuntimeError("a mitad")
        assert len(writes) == 1
        assert _stored(config_dir)["language"] == "EN"
        # El siguiente cambio vuelve a escribir solo
        store.set("language", "ES")
        assert len(writes) == 2


class TestRaiz:
    def test_un_config_que_no_se_puede_crear_no_tumba_nada(self, tmp_path, config_dir):
        blocker = tmp_path / "soy-un-fichero"
        blocker.write_text("x")
        try:
            store.init(str(blocker / "config"))
            assert store.get("language") == "ES"
            assert store.set("language", "EN") == "EN"
            # Sigue funcionando en memoria aunque no haya disco
            assert store.get("language") == "EN"
            assert store.settings_exists() is False
        finally:
            store.init(str(config_dir))

    def test_init_sin_argumentos_conserva_la_raiz(self, config_dir):
        assert store.init() == str(config_dir)
        assert store.root() == str(config_dir)
        assert store.state_dir() == os.path.join(str(config_dir), "state")
        assert os.path.isdir(store.state_dir())

    def test_un_directorio_temporal_no_es_persistente(self, config_dir):
        assert store.is_persistent() is False


class TestLectura:
    def test_default_de_claves_que_no_existen(self):
        assert store.default("no_existe") is None
        assert store.default("urls.no_existe") is None
        assert store.default("language.sub") is None
        assert store.default("a.b.c.d") is None

    def test_default_devuelve_copia(self):
        urls = store.default("urls")
        urls["auto_send"] = "ROTO"
        assert store.default("urls.auto_send") == "ASK"

    def test_get_de_un_dict_devuelve_copia(self, config_dir):
        urls = store.get("urls")
        urls["auto_send"] = "ROTO"
        assert store.get("urls.auto_send") == "ASK"

    def test_get_de_una_lista_devuelve_copia(self, config_dir):
        store.set("lista", [1, 2])
        store.get("lista").append(3)
        assert store.get("lista") == [1, 2]

    def test_un_nivel_intermedio_que_no_es_dict_cae_en_el_defecto(self, config_dir):
        (config_dir / "settings.json").write_text(json.dumps({"urls": "texto"}), encoding="utf-8")
        store.reload()
        assert store.get("urls.auto_send") == "ASK"

    def test_set_crea_los_niveles_que_falten(self, config_dir):
        store.set("nuevo.anidado.valor", 3)
        assert _stored(config_dir)["nuevo"]["anidado"]["valor"] == 3

    def test_set_sustituye_un_nivel_que_no_era_dict(self, config_dir):
        (config_dir / "settings.json").write_text(json.dumps({"urls": "texto"}), encoding="utf-8")
        store.reload()
        store.set("urls.auto_send", "SEND")
        assert store.get("urls.auto_send") == "SEND"

    def test_reload_relee_el_disco(self, config_dir):
        store.set("language", "EN")
        (config_dir / "settings.json").write_text(json.dumps({"language": "ES"}), encoding="utf-8")
        assert store.get("language") == "EN"
        store.reload()
        assert store.get("language") == "ES"

    def test_un_documento_que_no_es_objeto_es_ilegible(self, config_dir):
        (config_dir / "settings.json").write_text("[1, 2]", encoding="utf-8")
        store.reload()
        assert store.settings_unreadable() == "not a JSON object"
        store.set("language", "EN")
        assert (config_dir / "settings.json").read_text(encoding="utf-8") == "[1, 2]"


class TestParsers:
    @pytest.mark.parametrize("key, raw, expected", [
        ("language", " en ", "EN"),
        ("downloads.parallel", 1, 1),
        ("downloads.parallel", "20", 20),
        ("downloads.parallel", " 5 ", 5),
        ("downloads.fast_connections", 1, 1),
        ("downloads.fast_connections", 20, 20),
        ("urls.auto_format", "audio", "AUDIO"),
        ("urls.auto_send", "send_delete", "SEND_DELETE"),
        ("urls.video_quality", 720, "720"),
        ("urls.video_quality", "max", "MAX"),
        ("urls.audio_quality", "best", "BEST"),
        ("urls.audio_format", "m4a", "M4A"),
        ("urls.sponsorblock", "all", "ALL"),
        ("urls.playlist", "first", "FIRST"),
        ("urls.playlist_limit", 0, 0),
        ("urls.playlist_limit", 10000, 10000),
        ("urls.max_size_mb", 0, 0),
        ("urls.max_size_mb", 1000000, 1000000),
        ("extract.after", "keep", "KEEP"),
        ("video.hw", "nvenc", "NVENC"),
        ("video.quality", None, None),
        ("video.quality", "", None),
        ("video.quality", "  ", None),
        ("video.quality", 1, 1),
        ("video.quality", "51", 51),
        ("urls.prefer_compatible", True, True),
        ("urls.prefer_compatible", "yes", True),
        ("urls.audio_tags", "ON", True),
        ("urls.audio_tags", 1, True),
        ("telemetry", "false", False),
        ("telemetry", "0", False),
        ("telemetry", "cualquier-cosa", False),
        ("telemetry", None, False),
    ])
    def test_valores_validos_y_limite(self, key, raw, expected):
        assert settings.PARSERS[key](raw) == expected

    @pytest.mark.parametrize("key, raw", [
        ("language", "FR"),
        ("language", ""),
        ("downloads.parallel", 0),
        ("downloads.parallel", 21),
        ("downloads.parallel", "dos"),
        ("downloads.parallel", True),
        ("downloads.parallel", 2.5),
        ("downloads.parallel", None),
        ("downloads.fast_connections", 0),
        ("downloads.fast_connections", 21),
        ("urls.auto_format", "ambos"),
        ("urls.auto_send", "SEND-DELETE"),
        ("urls.video_quality", "4320"),
        ("urls.audio_quality", "LOW"),
        ("urls.audio_format", "OGG"),
        ("urls.sponsorblock", "SI"),
        ("urls.playlist", "LAST"),
        ("urls.playlist_limit", -1),
        ("urls.playlist_limit", 10001),
        ("urls.max_size_mb", -1),
        ("urls.max_size_mb", 1000001),
        ("extract.after", "MAYBE"),
        ("video.hw", "AMF"),
        ("video.quality", 0),
        ("video.quality", 52),
        ("video.quality", "alta"),
        ("video.quality", False),
    ])
    def test_valores_invalidos(self, key, raw):
        with pytest.raises((ValueError, TypeError)):
            settings.PARSERS[key](raw)

    def test_cada_parser_acepta_su_valor_por_defecto(self):
        for key, parse in settings.PARSERS.items():
            default = store.default(key)
            assert parse(default) == default, key

    def test_cada_ajuste_tiene_lector(self, config_dir):
        readers = [settings.language, settings.parallel_downloads, settings.fast_connections,
                   settings.auto_format, settings.auto_send, settings.video_quality,
                   settings.audio_quality, settings.prefer_compatible, settings.audio_format,
                   settings.audio_tags, settings.sponsorblock, settings.playlist_mode,
                   settings.playlist_limit, settings.max_size_mb, settings.extract_after,
                   settings.ffmpeg_hw, settings.ffmpeg_quality]
        defaults = [settings.get(key) for key in settings.PARSERS if key != "telemetry"]
        assert [reader() for reader in readers] == defaults

    @pytest.mark.parametrize("mb, label", [
        (0, "0 MB"), (512, "512 MB"), (1024, "1 GB"), (1536, "1536 MB"),
        (2048, "2 GB"), (5120, "5 GB"), (10240, "10 GB"), (1, "1 MB"),
    ])
    def test_size_label(self, mb, label):
        assert settings.size_label(mb) == label

    def test_las_opciones_de_los_selectores_son_validas(self):
        for value in settings.PARALLEL_CHOICES:
            settings.parse_parallel(value)
        for value in settings.FAST_CONNECTIONS_CHOICES:
            settings.parse_fast_connections(value)
        for value in settings.QUALITY_CHOICES:
            settings.parse_quality(value)
        for value in settings.PLAYLIST_LIMIT_CHOICES:
            settings.parse_playlist_limit(value)
        for value in settings.MAX_SIZE_CHOICES:
            settings.parse_max_size(value)


class TestAvisos:
    def test_un_valor_invalido_se_avisa_una_sola_vez(self, config_dir, monkeypatch):
        logged = []
        monkeypatch.setattr(settings, "warning", logged.append)
        monkeypatch.setattr(settings, "_warned", set())
        store.set("downloads.parallel", 99)

        assert settings.parallel_downloads() == 2
        assert settings.parallel_downloads() == 2
        assert len(logged) == 1 and "downloads.parallel" in logged[0]

    def test_guardarlo_bien_vuelve_a_permitir_el_aviso(self, config_dir, monkeypatch):
        logged = []
        monkeypatch.setattr(settings, "warning", logged.append)
        monkeypatch.setattr(settings, "_warned", set())
        store.set("downloads.parallel", 99)
        settings.parallel_downloads()
        settings.put("downloads.parallel", 3)
        store.set("downloads.parallel", 99)
        settings.parallel_downloads()
        assert len(logged) == 2

    def test_put_no_guarda_lo_invalido(self, config_dir):
        with pytest.raises(ValueError):
            settings.put("downloads.parallel", 0)
        assert not (config_dir / "settings.json").exists()

    def test_variables_antiguas_presentes(self, monkeypatch):
        for name in settings.SETTINGS_FROM_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AUTO_SEND", "SEND")
        monkeypatch.setenv("LANGUAGE", "")
        assert settings.env_settings_present() == ["AUTO_SEND"]


class TestMigracion:
    @pytest.fixture
    def logged(self, monkeypatch):
        lines = []
        monkeypatch.setattr(migration, "warning", lambda m: lines.append(("warning", m)))
        monkeypatch.setattr(migration, "debug", lambda m: lines.append(("debug", m)))
        for name in list(settings.SETTINGS_FROM_ENV) + ["FILTER_VIDEO", "FILTER_AUDIO"]:
            monkeypatch.delenv(name, raising=False)
        return lines

    def test_avisa_de_los_filtros_de_carpeta_que_ya_no_se_leen(self, config_dir, logged, monkeypatch):
        monkeypatch.setenv("FILTER_VIDEO", "1")
        monkeypatch.setenv("FILTER_AUDIO", "")
        migration.run()
        filters = [m for level, m in logged if level == "warning" and "FILTER_" in m]
        assert len(filters) == 1
        assert "FILTER_VIDEO" in filters[0] and "FILTER_AUDIO" not in filters[0]

    def test_al_sembrar_las_variables_solo_se_anotan(self, config_dir, logged, monkeypatch):
        monkeypatch.setenv("AUTO_SEND", "SEND")
        assert migration.run() is True
        assert not any(level == "warning" for level, _ in logged)
        assert any("AUTO_SEND" in m for level, m in logged if level == "debug")

    def test_ya_migrado_las_variables_se_avisan(self, config_dir, logged, monkeypatch):
        migration.run()
        monkeypatch.setenv("AUTO_SEND", "SEND")
        assert migration.run() is False
        assert any("AUTO_SEND" in m for level, m in logged if level == "warning")
        assert settings.auto_send() == "ASK"

    def test_con_settings_ilegible_no_siembra(self, config_dir, logged, monkeypatch):
        (config_dir / "settings.json").write_text("{roto", encoding="utf-8")
        store.reload()
        monkeypatch.setenv("AUTO_SEND", "SEND")
        assert migration.run() is False
        assert (config_dir / "settings.json").read_text(encoding="utf-8") == "{roto"

    def test_la_siembra_escribe_una_sola_vez(self, config_dir, logged, monkeypatch, writes):
        monkeypatch.setenv("AUTO_SEND", "SEND")
        monkeypatch.setenv("LANGUAGE", "EN")
        monkeypatch.setenv("FFMPEG_HW", "nope")
        migration.run()
        assert len(writes) == 1
        assert writes[0]["settings_version"] == 1
        assert writes[0]["video"]["hw"] == "NONE"
