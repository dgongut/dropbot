"""Estadísticas de DropBot (`stats.py`): cuándo se envían y qué llevan.

Lo que se manda tiene que estar declarado en el manifiesto del repo de
telemetría, que descarta cualquier otra clave; y nunca puede llevar una
ruta, un nombre ni un id de Telegram.
"""

import os
from pathlib import Path

import pytest

import config
import settings
import stats
import store

MANIFEST_CANDIDATES = (
    Path("/telemetry-projects/dropbot.yaml"),
    Path("/Users/dgongut/Documents/git/telemetry/projects/dropbot.yaml"),
)


@pytest.fixture
def manifest():
    yaml = pytest.importorskip("yaml")
    for path in MANIFEST_CANDIDATES:
        if path.is_file():
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    pytest.skip("no está el manifiesto de dgongut/telemetry")


@pytest.fixture
def fresh(config_dir, monkeypatch):
    """Un /config persistente, sin TELEMETRY y con un cliente nuevo."""
    monkeypatch.delenv("TELEMETRY", raising=False)
    monkeypatch.setattr(store, "_persistent", True)
    monkeypatch.setattr(stats, "_client", None)
    return config_dir


class TestCuandoSeEnvian:
    def test_la_variable_telemetry_las_apaga(self, fresh, monkeypatch):
        monkeypatch.setenv("TELEMETRY", "false")
        assert stats.forced_off() == "TELEMETRY"
        assert stats.is_on() is False

    def test_sin_volumen_no_se_envian(self, fresh, monkeypatch):
        monkeypatch.setattr(store, "_persistent", False)
        assert stats.forced_off() == "volume"
        assert stats.is_on() is False

    def test_la_variable_tiene_prioridad_sobre_el_volumen(self, fresh, monkeypatch):
        monkeypatch.setenv("TELEMETRY", "no")
        monkeypatch.setattr(store, "_persistent", False)
        assert stats.forced_off() == "TELEMETRY"

    def test_activadas_por_defecto_con_volumen(self, fresh):
        assert stats.forced_off() is None
        assert stats.is_on() is True

    def test_el_ajuste_las_apaga(self, fresh):
        settings.put("telemetry", False)
        assert stats.is_on() is False

    def test_el_estado_va_en_config_state(self, fresh):
        assert stats.client().state_path == os.path.join(str(fresh), "state", "telemetry.json")


class TestMetricas:
    def test_todas_las_claves(self, fresh):
        metrics = stats.collect_metrics()
        expected = {
            "language", "admins", "parallel_downloads", "fast_connections", "auto_format",
            "auto_send", "video_quality", "audio_quality", "prefer_compatible", "audio_format",
            "audio_tags", "sponsorblock", "playlist_mode", "playlist_limit", "max_size_mb",
            "extract_after", "ffmpeg_hw", "ffmpeg_quality_custom", "cookies", "gpu_device",
        } | {f"folder_{kind}" for kind in config.DOWNLOAD_PATHS}
        assert set(metrics) == expected

    def test_solo_numeros_booleanos_y_valores_cerrados(self, fresh, monkeypatch):
        monkeypatch.setattr(stats, "TELEGRAM_ADMIN", "123456789,987654321")
        settings.put("video.quality", "23")
        metrics = stats.collect_metrics()

        for key, value in metrics.items():
            assert isinstance(value, (bool, int, str)), key
            if isinstance(value, str):
                assert "/" not in value and "\\" not in value, key
                assert len(value) <= 12, key
            assert "123456789" not in str(value) and "987654321" not in str(value), key
        # La calidad elegida no se manda, solo que hay una a medida
        assert metrics["ffmpeg_quality_custom"] is True
        assert 23 not in metrics.values()

    @pytest.mark.parametrize("admins, expected", [
        ("1", 1), ("1,2", 2), ("1, 2 ,3", 3), ("1,,2,", 2), ("", 0),
    ])
    def test_cuenta_los_admins_sin_mandarlos(self, fresh, monkeypatch, admins, expected):
        monkeypatch.setattr(stats, "TELEGRAM_ADMIN", admins)
        assert stats.collect_metrics()["admins"] == expected

    def test_las_carpetas_dicen_si_tienen_volumen_no_cual(self, fresh, monkeypatch):
        paths = config.resolve_download_paths(mounted=lambda path: path == config.DOWNLOAD_VIDEO)
        monkeypatch.setattr(stats, "DOWNLOAD_PATHS", paths)
        metrics = stats.collect_metrics()
        assert metrics["folder_video"] is True
        assert metrics["folder_url_video"] is True
        assert metrics["folder_audio"] is False

    def test_valores_de_los_ajustes_en_minusculas(self, fresh):
        settings.put("language", "EN")
        settings.put("urls.auto_send", "SEND_DELETE")
        settings.put("video.hw", "VAAPI")
        metrics = stats.collect_metrics()
        assert metrics["language"] == "en"
        assert metrics["auto_send"] == "send_delete"
        assert metrics["ffmpeg_hw"] == "vaapi"

    def test_cada_clave_esta_en_el_manifiesto(self, fresh, manifest):
        declared = manifest["metrics"]
        metrics = stats.collect_metrics()
        missing = sorted(set(metrics) - set(declared))
        assert not missing, f"métricas que el servidor descartaría: {missing}"

    def test_cada_valor_posible_cabe_en_el_manifiesto(self, fresh, manifest):
        """Recorre todos los valores de cada ajuste de lista cerrada."""
        declared = manifest["metrics"]
        closed = {
            "language": ("language", settings.LANGUAGES),
            "auto_format": ("urls.auto_format", settings.AUTO_FORMATS),
            "auto_send": ("urls.auto_send", settings.AUTO_SEND_MODES),
            "video_quality": ("urls.video_quality", settings.VIDEO_QUALITIES),
            "audio_quality": ("urls.audio_quality", settings.AUDIO_QUALITIES),
            "audio_format": ("urls.audio_format", settings.AUDIO_FORMATS),
            "sponsorblock": ("urls.sponsorblock", settings.SPONSORBLOCK_MODES),
            "playlist_mode": ("urls.playlist", settings.PLAYLIST_MODES),
            "extract_after": ("extract.after", settings.EXTRACT_AFTER_MODES),
            "ffmpeg_hw": ("video.hw", settings.FFMPEG_HW_MODES),
        }
        problems = []
        for metric, (key, values) in closed.items():
            allowed = [str(v) for v in declared[metric]["values"]]
            for value in values:
                settings.put(key, value)
                sent = stats.collect_metrics()[metric]
                if sent not in allowed:
                    problems.append(f"{metric}={sent!r}")
        numeric = {
            "parallel_downloads": ("downloads.parallel", settings.PARALLEL_CHOICES),
            "fast_connections": ("downloads.fast_connections", settings.FAST_CONNECTIONS_CHOICES),
            "playlist_limit": ("urls.playlist_limit", settings.PLAYLIST_LIMIT_CHOICES),
            "max_size_mb": ("urls.max_size_mb", settings.MAX_SIZE_CHOICES),
        }
        for metric, (key, values) in numeric.items():
            spec = declared[metric]
            for value in values:
                settings.put(key, value)
                sent = stats.collect_metrics()[metric]
                if not spec["min"] <= sent <= spec["max"]:
                    problems.append(f"{metric}={sent!r}")
        for metric, value in stats.collect_metrics().items():
            if declared.get(metric, {}).get("type") == "bool" and not isinstance(value, bool):
                problems.append(f"{metric} no es bool")
        assert not problems, problems


class TestContadores:
    def test_count_suma_cuando_estan_activadas(self, fresh):
        stats.count("cmd_start")
        stats.count("cmd_start")
        assert stats.client().preview()["usage"] == {"cmd_start": 2}

    def test_count_no_hace_nada_si_estan_apagadas(self, fresh, monkeypatch):
        monkeypatch.setenv("TELEMETRY", "false")
        stats.count("cmd_start")
        assert stats.client().preview()["usage"] == {}

    def test_count_nunca_lanza(self, fresh, monkeypatch):
        def broken():
            raise RuntimeError("sin cliente")

        monkeypatch.setattr(stats, "client", broken)
        stats.count("cmd_start")

    def test_disable_apaga_y_olvida_la_instalacion(self, fresh):
        stats.count("cmd_start")
        stats.client()._state["install_id"] = "id-anterior"

        stats.disable()

        assert settings.get("telemetry") is False
        preview = stats.client().preview()
        assert preview["install_id"] is None
        assert preview["usage"] == {}

    def test_preview_recorta_a_los_mas_usados(self, fresh):
        extra = 5
        for index in range(stats.PREVIEW_USAGE_KEYS + extra):
            stats.client().count(f"cmd_{index:02d}", index + 1)
        # Un empate con el último que entra: decide el nombre
        stats.client().count("cmd_aa", extra + 1)

        payload, hidden = stats.preview()

        assert hidden == extra + 1
        assert len(payload["usage"]) == stats.PREVIEW_USAGE_KEYS
        assert "cmd_29" in payload["usage"] and "cmd_00" not in payload["usage"]
        assert "cmd_05" in payload["usage"] and "cmd_aa" not in payload["usage"]

    def test_preview_sin_recorte(self, fresh):
        stats.count("cmd_start")
        payload, hidden = stats.preview()
        assert hidden == 0
        assert payload["usage"] == {"cmd_start": 1}
        assert payload["project"] == "dropbot"
        assert payload["metrics"] == stats.collect_metrics()

    def test_start_pone_la_version_y_arranca_una_vez(self, fresh, monkeypatch):
        client = stats.client()
        runs = []
        client._run = lambda: runs.append(1)
        monkeypatch.setattr(stats, "_version", "unknown")

        stats.start("4.1.0")
        stats.start("4.1.0")
        client._thread.join(timeout=1)

        assert client.version == "4.1.0"
        assert stats.preview()[0]["version"] == "4.1.0"
        assert runs == [1]

    def test_start_en_debug_avisa(self, fresh, monkeypatch):
        logged = []
        monkeypatch.setattr(stats, "TELEMETRY_DEBUG", True)
        monkeypatch.setattr(stats, "warning", logged.append)
        monkeypatch.setattr(stats, "_version", "unknown")
        client = stats.client()
        client._run = lambda: None

        stats.start("dev")
        client._thread.join(timeout=1)
        assert logged and "TELEMETRY_DEBUG" in logged[0]
