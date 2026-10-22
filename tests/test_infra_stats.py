"""Estadísticas de DropBot (`stats.py`): cuándo se envían y qué llevan.

Lo que se manda tiene que estar declarado en el manifiesto del repo de
telemetría, que descarta cualquier otra clave; y nunca puede llevar una
ruta, un nombre ni un id de Telegram.
"""

import json
import os
import threading
from pathlib import Path

import pytest

import basic
import config
import settings
import stats
import store
import telemetry

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

    def test_con_settings_ilegible_no_se_envian(self, fresh):
        """Ilegible, se usan los valores por defecto, que las activan: se
        enviaban aunque el fichero dijera que no, y desactivarlas no se guardaba."""
        (fresh / "settings.json").write_text('{"telemetry": false,\n "language": "EN",\n}', encoding="utf-8")
        store.reload()

        assert stats.forced_off() == "settings"
        assert stats.is_on() is False

    def test_si_no_se_puede_guardar_la_baja_no_se_envian(self, fresh, monkeypatch):
        """Con /config de solo lectura, un NFS con root_squash o el disco
        lleno, desactivarlas no llegaba al disco: tras reiniciar volvían a
        estar activadas y se enviaban con un id nuevo."""
        def read_only(src, dst):
            raise OSError(30, "Read-only file system")

        with monkeypatch.context() as patched:
            patched.setattr(store.os, "replace", read_only)
            stats.disable()
        # El reinicio: lo que no llegó al disco se pierde
        store.reload()

        assert settings.get("telemetry") is True
        assert stats.forced_off() == "settings"
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
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", "123456789,987654321")
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
        # El id de un grupo se ignora: no es un admin
        ("1,-1002", 1),
    ])
    def test_cuenta_los_admins_sin_mandarlos(self, fresh, monkeypatch, admins, expected):
        monkeypatch.setattr(basic, "TELEGRAM_ADMIN", admins)
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

    def test_desactivarlas_mientras_el_hilo_vuelca_no_devuelve_el_id_al_disco(self, fresh, monkeypatch):
        """El hilo copia el estado bajo el lock del cliente y lo escribe
        después, ya sin él. Si forget() escribía entre medias, el volcado del
        hilo devolvía al disco el id que se acababa de olvidar."""
        client = stats.client()
        client._state["install_id"] = "id-anterior"
        stats.count("cmd_start")
        copied, resume = threading.Event(), threading.Event()
        real_makedirs = os.makedirs

        def makedirs(path, *args, **kwargs):
            # El hilo, ya con su copia del estado y antes de escribirla
            if threading.current_thread().name == "telemetry":
                copied.set()
                resume.wait(timeout=5)
            return real_makedirs(path, *args, **kwargs)

        monkeypatch.setattr(telemetry.os, "makedirs", makedirs)
        flushing = threading.Thread(target=client._flush, kwargs={"force": True}, name="telemetry")
        flushing.start()
        assert copied.wait(timeout=5)
        disabling = threading.Thread(target=stats.disable)
        disabling.start()
        # En fila, forget() espera a que acabe el volcado en curso
        disabling.join(timeout=0.2)
        resume.set()
        flushing.join(timeout=5)
        disabling.join(timeout=5)

        state = json.loads((fresh / "state" / "telemetry.json").read_text(encoding="utf-8"))
        assert state["install_id"] is None and state["pending"] == {}

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

    def test_al_arrancar_con_la_variable_se_olvida_la_instalacion(self, fresh, monkeypatch):
        """Solo el botón de /settings olvidaba el id. Con TELEMETRY=false se
        quedaba en disco, y si un día se quitaba la variable, el primer envío
        cosía los dos periodos con el mismo id y los contadores de antes."""
        state = fresh / "state" / "telemetry.json"
        state.write_text(json.dumps({"install_id": "id-anterior", "pending": {"cmd_list": 3}}),
                         encoding="utf-8")
        monkeypatch.setenv("TELEMETRY", "false")
        monkeypatch.setattr(stats, "_version", "unknown")
        client = stats.client()
        client._run = lambda: None

        stats.start("4.0.1")
        client._thread.join(timeout=1)

        stored = json.loads(state.read_text(encoding="utf-8"))
        assert stored["install_id"] is None and stored["pending"] == {}
        assert stats.preview()[0]["install_id"] is None

    def test_con_la_variable_y_nada_que_olvidar_no_se_escribe(self, fresh, monkeypatch):
        monkeypatch.setenv("TELEMETRY", "false")
        monkeypatch.setattr(stats, "_version", "unknown")
        client = stats.client()
        client._run = lambda: None

        stats.start("4.0.1")
        client._thread.join(timeout=1)

        assert not (fresh / "state" / "telemetry.json").exists()

    def test_sin_la_variable_al_arrancar_se_conserva_el_id(self, fresh, monkeypatch):
        state = fresh / "state" / "telemetry.json"
        state.write_text(json.dumps({"install_id": "id-anterior", "pending": {}}), encoding="utf-8")
        monkeypatch.setattr(stats, "_version", "unknown")
        client = stats.client()
        client._run = lambda: None

        stats.start("4.0.1")
        client._thread.join(timeout=1)

        assert stats.preview()[0]["install_id"] == "id-anterior"

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
