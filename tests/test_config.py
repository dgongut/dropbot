"""La configuración se valida al arrancar, no a mitad de una descarga.

Un valor mal escrito en el `.env` debe abortar con un mensaje claro. Un valor
vacío (`AUTO_SEND=` sin nada detrás) debe equivaler a no ponerlo: es un error
de escritura muy fácil de cometer y no tiene por qué tirar el contenedor.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_ENV = {
    "LANGUAGE": "ES",
    "TELEGRAM_ADMIN": "999",
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "testhash",
}


def _read_config(**overrides):
    """Importa `config` en un proceso aparte y devuelve los valores pedidos.

    En un subproceso porque `config` se evalúa una sola vez por intérprete.
    """
    env = dict(BASE_ENV)
    env.update({k: v for k, v in overrides.items() if v is not None})
    code = (
        "import json, config; "
        "print(json.dumps({'AUTO_SEND': config.AUTO_SEND, "
        "'AUTO_DOWNLOAD_FORMAT': config.AUTO_DOWNLOAD_FORMAT}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


class TestValoresPorDefecto:
    def test_sin_configurar_es_ask(self):
        assert _read_config()["AUTO_SEND"] == "ASK"
        assert _read_config()["AUTO_DOWNLOAD_FORMAT"] == "ASK"

    @pytest.mark.parametrize("variable", ["AUTO_SEND", "AUTO_DOWNLOAD_FORMAT"])
    def test_un_valor_vacio_equivale_a_no_configurarlo(self, variable):
        """`AUTO_SEND=` en el .env devolvía cadena vacía, no el default, y la
        validación de arranque abortaba el contenedor."""
        assert _read_config(**{variable: ""})[variable] == "ASK"

    @pytest.mark.parametrize("written,expected", [
        ("send", "SEND"),
        ("Send_Delete", "SEND_DELETE"),
        ("store", "STORE"),
    ])
    def test_no_distingue_mayusculas(self, written, expected):
        assert _read_config(AUTO_SEND=written)["AUTO_SEND"] == expected


class TestValidacionDeArranque:
    def _start(self, **overrides):
        env = dict(BASE_ENV)
        env.update(overrides)
        # Sin TELEGRAM_TOKEN: el arranque para antes de tocar la red, así que
        # da igual que el resto de la configuración esté a medias
        return subprocess.run(
            [sys.executable, "dropbot.py"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )

    @pytest.mark.parametrize("variable,valid", [
        ("AUTO_SEND", "ASK/SEND/SEND_DELETE/STORE"),
        ("AUTO_DOWNLOAD_FORMAT", "ASK/VIDEO/AUDIO"),
    ])
    def test_un_valor_invalido_aborta_con_mensaje(self, variable, valid):
        result = self._start(**{variable: "NO_EXISTE"})

        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert variable in output and valid in output

    def test_un_idioma_invalido_aborta(self):
        result = self._start(LANGUAGE="FR")

        assert result.returncode != 0
        assert "LANGUAGE" in result.stdout + result.stderr

    @pytest.mark.parametrize("value", ["ASK", "SEND", "SEND_DELETE", "STORE", ""])
    def test_los_valores_validos_pasan_la_validacion(self, value):
        """Debe llegar hasta la comprobación del token, que va después."""
        result = self._start(AUTO_SEND=value)

        output = result.stdout + result.stderr
        assert "AUTO_SEND only can be" not in output
        assert "TELEGRAM_TOKEN" in output


class TestTraducciones:
    def test_las_claves_usadas_existen_en_los_dos_idiomas(self):
        """Si falta una clave en los dos ficheros, el usuario ve
        "[MISSING: clave]" en Telegram."""
        import re

        source = (REPO_ROOT / "dropbot.py").read_text(encoding="utf-8")
        used = set(re.findall(r'get_text\(\s*["\']([a-z0-9_]+)["\']', source))
        assert used, "no se han encontrado llamadas a get_text"

        english = json.loads((REPO_ROOT / "locale" / "en.json").read_text(encoding="utf-8"))
        spanish = json.loads((REPO_ROOT / "locale" / "es.json").read_text(encoding="utf-8"))

        # El fallback de get_text es el inglés, así que una clave que no esté en
        # ninguno de los dos no tiene forma de resolverse
        missing = sorted(k for k in used if k not in english and k not in spanish)
        assert not missing, f"claves sin traducción en ningún idioma: {missing}"
