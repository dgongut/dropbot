"""Configuración: variables de entorno, ajustes de /settings y carpetas.

Desde la 4.0.0 casi todo es un ajuste que vive en /config/settings.json y se
cambia desde Telegram. En el primer arranque se siembra con las variables que
hubiera en el docker-compose, para que actualizar no cambie nada; después
manda settings.json. Un valor mal escrito no tumba el contenedor: se avisa y
se usa el de por defecto, que se puede arreglar desde el propio bot.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import config
import migration
import settings
import store

REPO_ROOT = Path(__file__).resolve().parent.parent

OLD_VARIABLES = ("LANGUAGE", "PARALLEL_DOWNLOADS", "FAST_CONNECTIONS", "AUTO_DOWNLOAD_FORMAT",
                 "AUTO_SEND", "FFMPEG_HW", "FFMPEG_QUALITY")


@pytest.fixture
def env(monkeypatch):
    """Pone variables de entorno solo para el test."""
    def setter(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)
    for name in OLD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return setter


def _stored(config_dir):
    return json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))


class TestValoresPorDefecto:
    def test_sin_configurar(self, config_dir):
        assert settings.auto_send() == "ASK"
        assert settings.auto_format() == "ASK"
        assert settings.ffmpeg_hw() == "NONE"
        assert settings.ffmpeg_quality() is None
        assert settings.parallel_downloads() == 2
        assert settings.fast_connections() == 8
        assert settings.language() == "ES"


class TestMigracion:
    def test_siembra_con_las_variables_del_compose(self, config_dir, env):
        env(LANGUAGE="en", AUTO_SEND="send_delete", AUTO_DOWNLOAD_FORMAT="Audio",
            FFMPEG_HW="vaapi", FFMPEG_QUALITY="23", PARALLEL_DOWNLOADS="4", FAST_CONNECTIONS="6")

        assert migration.run() is True

        assert settings.language() == "EN"
        assert settings.auto_send() == "SEND_DELETE"
        assert settings.auto_format() == "AUDIO"
        assert settings.ffmpeg_hw() == "VAAPI"
        assert settings.ffmpeg_quality() == 23
        assert settings.parallel_downloads() == 4
        assert settings.fast_connections() == 6
        assert _stored(config_dir)["urls"]["auto_send"] == "SEND_DELETE"

    def test_sin_variables_tambien_crea_el_fichero(self, config_dir, env):
        """Para que el siguiente arranque sepa que la siembra ya se hizo."""
        assert migration.run() is True
        assert (config_dir / "settings.json").exists()

    @pytest.mark.parametrize("variable", ["AUTO_SEND", "AUTO_DOWNLOAD_FORMAT", "FFMPEG_QUALITY"])
    def test_un_valor_vacio_equivale_a_no_configurarlo(self, config_dir, env, variable):
        """`AUTO_SEND=` en el .env es un error muy fácil de cometer."""
        env(**{variable: ""})
        migration.run()
        assert settings.get(settings.SETTINGS_FROM_ENV[variable]) == store.default(settings.SETTINGS_FROM_ENV[variable])

    @pytest.mark.parametrize("variable", ["AUTO_SEND", "AUTO_DOWNLOAD_FORMAT", "FFMPEG_HW",
                                          "FFMPEG_QUALITY", "PARALLEL_DOWNLOADS", "LANGUAGE"])
    def test_un_valor_invalido_se_ignora_sin_tumbar_el_arranque(self, config_dir, env, variable):
        """Hasta la 3.x abortaba el contenedor. Ahora se usa el de por
        defecto, que se puede corregir desde /settings."""
        env(**{variable: "NO_EXISTE"})

        migration.run()

        key = settings.SETTINGS_FROM_ENV[variable]
        assert settings.get(key) == store.default(key)

    def test_despues_de_sembrar_manda_settings_json(self, config_dir, env):
        """Si la variable siguiera ganando, un cambio hecho desde /settings
        se desharía en silencio en el siguiente reinicio."""
        env(AUTO_SEND="SEND")
        migration.run()
        settings.put("urls.auto_send", "STORE")

        store.reload()
        assert migration.run() is False
        assert settings.auto_send() == "STORE"


class TestAjustes:
    def test_put_rechaza_lo_que_no_vale(self, config_dir):
        with pytest.raises(ValueError):
            settings.put("urls.auto_send", "NO_EXISTE")
        with pytest.raises(ValueError):
            settings.put("video.quality", 52)
        with pytest.raises(ValueError):
            settings.put("downloads.parallel", 0)

    def test_un_valor_editado_a_mano_mal_cae_en_el_de_por_defecto(self, config_dir):
        (config_dir / "settings.json").write_text(json.dumps({
            "urls": {"auto_send": "LOQUESEA"},
            "downloads": {"parallel": "muchas"},
            "telemetry": "false",
        }), encoding="utf-8")
        store.reload()

        assert settings.auto_send() == "ASK"
        assert settings.parallel_downloads() == 2
        # "false" a mano es falso, no una cadena no vacía
        assert settings.get("telemetry") is False

    def test_un_settings_json_ilegible_no_se_sobrescribe(self, config_dir):
        """Una errata editándolo a mano no puede acabar con todos los ajustes
        sustituidos por los de por defecto."""
        broken = '{"urls": {"auto_send": "SEND",}'
        (config_dir / "settings.json").write_text(broken, encoding="utf-8")
        store.reload()

        settings.put("urls.auto_format", "VIDEO")

        assert store.settings_unreadable()
        assert (config_dir / "settings.json").read_text(encoding="utf-8") == broken

    def test_se_conservan_las_claves_desconocidas(self, config_dir):
        """Volver a una versión anterior no debe perder lo que escribió una nueva."""
        (config_dir / "settings.json").write_text(json.dumps({"futuro": 1}), encoding="utf-8")
        store.reload()

        settings.put("urls.auto_send", "SEND")

        assert _stored(config_dir)["futuro"] == 1


class TestCalidadDeLosEnlaces:
    @pytest.fixture
    def args(self, dropbot, config_dir):
        return dropbot.ytdlp_settings_args

    def test_por_defecto_pide_lo_compatible_sin_limite(self, args):
        """H.264 y AAC por delante, que Telegram reproduce sin convertir."""
        assert args(False) == ["-f", "bv*+ba/best", "-S", "vcodec:h264,res,acodec:aac"]

    def test_sin_preferir_compatible_es_lo_de_la_3x(self, args):
        settings.put("urls.prefer_compatible", False)
        settings.put("urls.audio_tags", False)
        assert args(False) == ["-f", "bv*+ba/best"]
        assert args(True) == ["-f", "bestaudio", "--extract-audio", "--audio-format", "mp3"]

    def test_el_limite_de_video_va_por_el_lado_corto(self, args):
        """res es el lado corto: un vertical de 1080x1920 cuenta como 1080p, y
        si no hay nada por debajo yt-dlp baja lo más pequeño en vez de fallar."""
        settings.put("urls.prefer_compatible", False)
        settings.put("urls.video_quality", "1080")
        assert args(False) == ["-f", "bv*+ba/best", "-S", "res:1080"]

    def test_con_limite_y_compatible_la_resolucion_va_antes_que_el_audio(self, args):
        """Con acodec:aac delante de res gana el único formato con AAC dentro:
        el combinado de 360p de YouTube, sea cual sea el límite."""
        settings.put("urls.video_quality", "720")
        assert args(False)[-1] == "vcodec:h264,res:720,acodec:aac"

    @pytest.mark.parametrize("quality,expected", [("HIGH", "192K"), ("BEST", "0")])
    def test_la_calidad_del_audio(self, args, quality, expected):
        settings.put("urls.audio_quality", quality)
        produced = args(True)
        assert produced[produced.index("--audio-quality") + 1] == expected

    def test_m4a_no_recodifica_ni_usa_la_calidad_del_mp3(self, args):
        settings.put("urls.audio_format", "M4A")
        settings.put("urls.audio_quality", "BEST")
        produced = args(True)
        assert produced[:5] == ["-f", "bestaudio[ext=m4a]/bestaudio", "--extract-audio", "--audio-format", "m4a"]
        assert "--audio-quality" not in produced

    def test_etiquetas_y_caratula_solo_en_el_audio(self, args):
        assert "--embed-thumbnail" in args(True)
        assert "--embed-thumbnail" not in args(False)
        settings.put("urls.audio_tags", False)
        assert "--embed-thumbnail" not in args(True)

    @pytest.mark.parametrize("mode,categories", [
        ("SPONSOR", "sponsor"),
        ("ALL", "sponsor,selfpromo,interaction,intro,outro,preview,music_offtopic,filler"),
    ])
    def test_sponsorblock(self, args, mode, categories):
        assert "--sponsorblock-remove" not in args(False)
        settings.put("urls.sponsorblock", mode)
        for is_audio in (False, True):
            produced = args(is_audio)
            assert produced[produced.index("--sponsorblock-remove") + 1] == categories

    def test_tamano_maximo(self, args):
        assert "--max-filesize" not in args(False)
        settings.put("urls.max_size_mb", 2048)
        produced = args(False)
        assert produced[produced.index("--max-filesize") + 1] == "2048M"

    def test_pasarse_del_tamano_maximo_se_dice(self, dropbot, config_dir):
        settings.put("urls.max_size_mb", 2048)
        lines = ["[download] File is larger than max-filesize (3000000000 bytes > 2147483648 bytes). Aborting."]
        assert "2 GB" in dropbot.url_failure_message(lines)
        assert dropbot.url_failure_message(["ERROR: otra cosa"]) == dropbot.get_text("error_url_failed_user")

    def test_el_limite_de_video_no_toca_el_audio(self, args):
        settings.put("urls.video_quality", "720")
        assert "-S" not in args(True)


class TestCarpetas:
    def test_sin_nada_montado_todo_va_a_downloads(self):
        paths = config.resolve_download_paths(mounted=lambda path: False)
        assert set(paths.values()) == {config.DOWNLOAD_PATH}

    def test_una_carpeta_montada_es_carpeta_propia(self):
        paths = config.resolve_download_paths(mounted=lambda path: path == config.DOWNLOAD_VIDEO)

        assert paths["video"] == config.DOWNLOAD_VIDEO
        # Lo de URLs sigue a los vídeos si /url_video no está montada
        assert paths["url_video"] == config.DOWNLOAD_VIDEO
        assert paths["audio"] == config.DOWNLOAD_PATH

    def test_url_video_montada_va_aparte(self):
        mounted = {config.DOWNLOAD_VIDEO, config.DOWNLOAD_URL_VIDEO}
        paths = config.resolve_download_paths(mounted=lambda path: path in mounted)

        assert paths["video"] == config.DOWNLOAD_VIDEO
        assert paths["url_video"] == config.DOWNLOAD_URL_VIDEO


class TestArranque:
    def test_sin_token_aborta_con_mensaje(self):
        env = {"TELEGRAM_ADMIN": "999", "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "testhash",
               "TELEMETRY": "false", "PATH": "/usr/bin:/bin"}
        result = subprocess.run(
            [sys.executable, "dropbot.py"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )

        assert result.returncode != 0
        assert "TELEGRAM_TOKEN" in result.stdout + result.stderr


class TestTraducciones:
    @staticmethod
    def _locales():
        english = json.loads((REPO_ROOT / "locale" / "en.json").read_text(encoding="utf-8"))
        spanish = json.loads((REPO_ROOT / "locale" / "es.json").read_text(encoding="utf-8"))
        return english, spanish

    def test_las_claves_usadas_existen_en_los_dos_idiomas(self):
        """Si falta una clave, el usuario ve "[MISSING: clave]" en Telegram."""
        import re

        sources = [REPO_ROOT / "dropbot.py", *sorted((REPO_ROOT / "handlers").glob("*.py"))]
        used = set()
        for path in sources:
            used |= set(re.findall(r'get_text\(\s*["\']([a-z0-9_]+)["\']', path.read_text(encoding="utf-8")))
        assert used, "no se han encontrado llamadas a get_text"

        english, spanish = self._locales()
        missing = sorted(k for k in used if k not in english or k not in spanish)
        assert not missing, f"claves sin traducción en algún idioma: {missing}"

    def test_las_claves_que_arma_settings_existen(self):
        """Las pantallas de /settings componen las claves con f-strings, que
        el test anterior no puede ver."""
        from handlers import settings as screens

        keys = []
        for field in screens.FIELDS:
            keys += [f"settings_{field}_title", f"settings_{field}_help"]
        for field in ("fmt", "send", "hw", "aq", "af", "sb", "plm", "ext"):
            keys += [f"settings_{field}_{value.lower()}" for value in screens._choices(field)]
        keys += ["settings_vq_max", "settings_pll_none", "settings_max_none"]
        keys += [f"settings_folder_{kind}" for kind in screens.FOLDER_ORDER]

        english, spanish = self._locales()
        missing = sorted(k for k in keys if k not in english or k not in spanish)
        assert not missing, f"claves sin traducción en algún idioma: {missing}"

    def test_los_dos_idiomas_tienen_las_mismas_claves(self):
        english, spanish = self._locales()
        assert set(english) ^ set(spanish) == set()
