"""Cliente de telemetría (`telemetry.py`), contra un servidor HTTP local real.

`telemetry.py` es la copia del cliente de referencia de dgongut/telemetry:
un fallo aquí está también en el repo de origen. Se prueba de punta a punta
con un `http.server` en 127.0.0.1 y un puerto libre, y el tiempo se controla
pasando `now` a `_tick()` y retrasando `_started_at`, sin dormir nunca.
"""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import telemetry
from telemetry import (
    DEFAULT_INTERVAL_HOURS, FLUSH_SECONDS, MAX_INTERVAL_HOURS, RETRY_SECONDS,
    START_DELAY_SECONDS, Telemetry,
)

NOW = 1_800_000_000.0
HOUR = 3600


class Server:
    """Un endpoint de telemetría de juguete: anota cada ping y responde lo pedido."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.body = b'{"ok": true}'
        self.on_request = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                server.requests.append({
                    "payload": json.loads(self.rfile.read(length)),
                    "headers": dict(self.headers),
                })
                if server.on_request:
                    server.on_request()
                self.send_response(server.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(server.body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/ping"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def answer(self, document, status=200):
        self.status = status
        self.body = document if isinstance(document, bytes) else json.dumps(document).encode()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    instance = Server()
    yield instance
    instance.close()


@pytest.fixture
def allowed(monkeypatch):
    """conftest pone TELEMETRY=false para todos los tests; aquí no."""
    monkeypatch.delenv("TELEMETRY", raising=False)


@pytest.fixture
def make_client(tmp_path, allowed):
    """Un cliente listo para enviar: ya pasó el retraso de arranque."""
    logs = []

    def factory(endpoint="http://127.0.0.1:9/v1/ping", ready=True, **kwargs):
        kwargs.setdefault("metrics", lambda: {"hosts": 3})
        kwargs.setdefault("log", logs.append)
        client = Telemetry(project="dropbot", version="9.9.9",
                           state_path=str(tmp_path / "state" / "telemetry.json"),
                           endpoint=endpoint, **kwargs)
        if ready:
            client._started_at -= START_DELAY_SECONDS + 1
        client.logs = logs
        return client
    return factory


def _stored(client):
    with open(client.state_path, encoding="utf-8") as handle:
        return json.load(handle)


class TestEnvio:
    def test_un_envio_correcto(self, make_client, server):
        client = make_client(server.url)
        client.count("cmd_start")
        client.count("cmd_start")
        client.count("file_video", 3)

        assert client._tick(now=NOW) is True

        request = server.requests[0]
        payload = request["payload"]
        assert payload["schema"] == 1
        assert payload["project"] == "dropbot"
        assert payload["version"] == "9.9.9"
        assert payload["arch"] == telemetry.architecture()
        assert payload["metrics"] == {"hosts": 3}
        assert payload["usage"] == {"cmd_start": 2, "file_video": 3}
        uuid.UUID(payload["install_id"])
        assert request["headers"]["User-Agent"] == "dropbot/9.9.9 telemetry/1"
        assert request["headers"]["Content-Type"] == "application/json"

        # Lo enviado sale de pendientes, y el estado queda en disco
        assert client.preview()["usage"] == {}
        stored = _stored(client)
        assert stored["install_id"] == payload["install_id"]
        assert stored["last_sent"] == NOW
        assert stored["pending"] == {}

    def test_el_id_se_mantiene_entre_envios(self, make_client, server):
        client = make_client(server.url)
        client._tick(now=NOW)
        client._tick(now=NOW + DEFAULT_INTERVAL_HOURS * HOUR)

        ids = {r["payload"]["install_id"] for r in server.requests}
        assert len(server.requests) == 2 and len(ids) == 1

    def test_lo_contado_durante_el_envio_se_conserva(self, make_client, server):
        client = make_client(server.url)
        client.count("cmd_start", 2)
        client.count("btn_ok")
        # Mientras el servidor atiende el POST, el bot sigue contando
        server.on_request = lambda: (client.count("cmd_start"), client.count("cmd_new"))

        assert client._tick(now=NOW) is True

        assert server.requests[0]["payload"]["usage"] == {"cmd_start": 2, "btn_ok": 1}
        assert client.preview()["usage"] == {"cmd_start": 1, "cmd_new": 1}
        assert _stored(client)["pending"] == {"cmd_start": 1, "cmd_new": 1}

    def test_no_envia_dos_veces_el_mismo_dia(self, make_client, server):
        client = make_client(server.url)
        assert client._tick(now=NOW) is True
        assert client._tick(now=NOW + HOUR) is False
        assert client._tick(now=NOW + DEFAULT_INTERVAL_HOURS * HOUR - 1) is False
        assert client._tick(now=NOW + DEFAULT_INTERVAL_HOURS * HOUR) is True
        assert len(server.requests) == 2

    def test_nada_antes_del_retraso_de_arranque(self, make_client, server):
        client = make_client(server.url, ready=False)
        assert client._tick(now=NOW) is False
        assert server.requests == []

    def test_las_metricas_que_fallan_van_vacias(self, make_client, server):
        def broken():
            raise RuntimeError("sin disco")

        client = make_client(server.url, metrics=broken)
        assert client._tick(now=NOW) is True
        assert server.requests[0]["payload"]["metrics"] == {}
        assert any("metrics failed" in line for line in client.logs)

    def test_unas_metricas_que_no_son_un_dict_van_vacias(self, make_client, server):
        client = make_client(server.url, metrics=lambda: ["no", "es", "un", "dict"])
        client._tick(now=NOW)
        assert server.requests[0]["payload"]["metrics"] == {}


class TestRespuestaDelServidor:
    @pytest.mark.parametrize("asked, expected", [
        (48, 48),
        (0.5, 1),
        (0, 1),
        (-5, 1),
        (10_000, MAX_INTERVAL_HOURS),
        ("48", DEFAULT_INTERVAL_HOURS),
        (True, DEFAULT_INTERVAL_HOURS),
        (None, DEFAULT_INTERVAL_HOURS),
    ])
    def test_next_ping_h_se_respeta_dentro_de_sus_limites(self, make_client, server, asked, expected):
        server.answer({"next_ping_h": asked})
        client = make_client(server.url)
        client._tick(now=NOW)
        assert client._state["interval_hours"] == expected

    def test_el_siguiente_envio_sigue_el_intervalo_pedido(self, make_client, server):
        server.answer({"next_ping_h": 48})
        client = make_client(server.url)
        client._tick(now=NOW)

        assert client._tick(now=NOW + 24 * HOUR) is False
        assert client._tick(now=NOW + 48 * HOUR) is True

    def test_el_servidor_puede_pausar_el_proyecto(self, make_client, server):
        server.answer({"enabled": False, "next_ping_h": 24})
        client = make_client(server.url)
        client.count("cmd_start")

        assert client._tick(now=NOW) is True
        # Lo enviado llegó: no se vuelve a mandar
        assert client.preview()["usage"] == {}
        assert client._state["paused_until"] == NOW + 24 * HOUR
        assert client._tick(now=NOW + 24 * HOUR - 1) is False
        # Pasado un intervalo se vuelve a preguntar
        server.answer({"enabled": True})
        assert client._tick(now=NOW + 24 * HOUR) is True
        assert client._state["paused_until"] == 0

    def test_en_pausa_no_envia_aunque_toque(self, make_client, server):
        client = make_client(server.url)
        client._state["paused_until"] = NOW + 100 * HOUR
        assert client._tick(now=NOW) is False
        assert server.requests == []

    def test_un_http_500_no_da_nada_por_enviado(self, make_client, server):
        server.answer({"error": "boom"}, status=500)
        client = make_client(server.url)
        client.count("cmd_start")

        assert client._tick(now=NOW) is False
        assert client.preview()["usage"] == {"cmd_start": 1}
        assert client._state["last_sent"] == 0
        assert any("HTTP 500" in line for line in client.logs)

    def test_tras_un_fallo_se_reintenta_pasada_una_hora(self, make_client, server):
        server.answer({}, status=503)
        client = make_client(server.url)
        client.count("cmd_start")
        assert client._tick(now=NOW) is False

        server.answer({})
        assert client._tick(now=NOW + RETRY_SECONDS - 1) is False
        assert len(server.requests) == 1
        assert client._tick(now=NOW + RETRY_SECONDS) is True
        assert server.requests[1]["payload"]["usage"] == {"cmd_start": 1}
        # El id creado en el intento fallido es el que se usa después
        assert server.requests[0]["payload"]["install_id"] == server.requests[1]["payload"]["install_id"]

    def test_sin_servidor_no_se_pierde_nada(self, make_client):
        client = make_client("http://127.0.0.1:9/v1/ping")
        client.count("cmd_start")
        assert client._tick(now=NOW) is False
        assert client.preview()["usage"] == {"cmd_start": 1}
        assert any("not sent" in line for line in client.logs)

    @pytest.mark.parametrize("body", [b"[1, 2, 3]", b'"texto"', b"42", b"null", b""])
    def test_una_respuesta_que_no_es_un_objeto_cuenta_como_entregada(self, make_client, server, body):
        server.answer(body)
        client = make_client(server.url)
        client.count("cmd_start")

        assert client._tick(now=NOW) is True
        assert client.preview()["usage"] == {}
        assert client._state["interval_hours"] == DEFAULT_INTERVAL_HOURS

    def test_un_200_que_no_es_json_cuenta_como_entregado(self, make_client, server):
        server.answer(b"<html>proxy</html>")
        client = make_client(server.url)
        client.count("cmd_start")

        assert client._tick(now=NOW) is True
        assert client.preview()["usage"] == {}


class TestInterruptores:
    @pytest.mark.parametrize("value, disabled", [
        ("false", True), ("0", True), ("no", True), ("off", True), ("FALSE", True),
        ("cualquier-cosa", True), ("", False), ("   ", False), ("true", False),
        (" TRUE ", False),
    ])
    def test_la_variable_telemetry(self, monkeypatch, value, disabled):
        monkeypatch.setenv("TELEMETRY", value)
        assert telemetry.disabled_by_environment() is disabled

    def test_sin_variable_no_desactiva(self, monkeypatch):
        monkeypatch.delenv("TELEMETRY", raising=False)
        assert telemetry.disabled_by_environment() is False

    def test_la_variable_gana_al_ajuste_y_no_cuenta_ni_envia(self, make_client, server, monkeypatch):
        client = make_client(server.url, enabled=lambda: True)
        monkeypatch.setenv("TELEMETRY", "false")
        client.count("cmd_start")
        assert client._tick(now=NOW) is False
        assert server.requests == []
        assert client.preview()["usage"] == {}
        assert client.preview()["install_id"] is None

    def test_desactivado_en_el_proyecto_no_cuenta_ni_envia(self, make_client, server):
        client = make_client(server.url, enabled=lambda: False)
        client.count("cmd_start")
        assert client.enabled() is False
        assert client._tick(now=NOW) is False
        assert server.requests == []

    def test_un_enabled_que_falla_cuenta_como_desactivado(self, make_client):
        def broken():
            raise RuntimeError("settings rotos")

        client = make_client(enabled=broken)
        assert client.enabled() is False
        client.count("cmd_start")
        assert client.preview()["usage"] == {}

    def test_en_debug_envia_una_vez_por_arranque_aunque_no_toque(self, make_client, server):
        client = make_client(server.url, debug=True)
        client._state.update(last_sent=NOW, paused_until=NOW + 100 * HOUR)

        assert client._tick(now=NOW + 1) is True
        # Ya envió en este arranque: vuelve a respetar el intervalo
        assert client._tick(now=NOW + 2 * telemetry.DEBUG_WAIT_SECONDS) is False
        assert len(server.requests) == 1


class TestEstadoEnDisco:
    def test_forget_borra_id_y_pendientes_y_lo_escribe(self, make_client, server):
        client = make_client(server.url)
        client.count("cmd_start")
        client._tick(now=NOW)
        client.count("cmd_otro")

        client.forget()

        assert client.preview()["install_id"] is None
        assert client.preview()["usage"] == {}
        stored = _stored(client)
        assert stored["install_id"] is None and stored["pending"] == {}
        # Volver a enviar empieza como una instalación nueva
        client._last_attempt = 0
        client._tick(now=NOW + DEFAULT_INTERVAL_HOURS * HOUR)
        assert server.requests[1]["payload"]["install_id"] != server.requests[0]["payload"]["install_id"]

    def test_preview_es_lo_que_se_enviaria_sin_crear_id(self, make_client, server):
        client = make_client(server.url)
        client.count("cmd_start")
        preview = client.preview()
        assert preview["install_id"] is None
        assert preview["usage"] == {"cmd_start": 1}
        assert preview["metrics"] == {"hosts": 3}

        client._tick(now=NOW - 1)  # cualquier envío anterior
        client.count("cmd_start")
        preview = client.preview()
        client._tick(now=NOW + DEFAULT_INTERVAL_HOURS * HOUR)
        sent = server.requests[-1]["payload"]
        assert preview == sent

    def test_los_contadores_sobreviven_a_un_reinicio(self, make_client):
        client = make_client()
        client._last_flush = -FLUSH_SECONDS  # el último volcado fue hace rato
        client.count("cmd_start", 4)
        client._flush()
        assert _stored(client)["pending"] == {"cmd_start": 4}

        again = make_client()
        assert again.preview()["usage"] == {"cmd_start": 4}

    def test_no_vuelca_a_disco_mas_de_una_vez_cada_cinco_minutos(self, make_client):
        client = make_client()
        client.count("cmd_start")
        client._flush(force=True)
        client.count("cmd_start")
        client._flush()
        assert _stored(client)["pending"] == {"cmd_start": 1}
        client._flush(force=True)
        assert _stored(client)["pending"] == {"cmd_start": 2}

    def test_sin_cambios_no_escribe(self, make_client):
        client = make_client()
        client._flush(force=True)
        assert not os.path.exists(client.state_path)

    def test_si_no_se_puede_escribir_no_lanza(self, tmp_path, allowed):
        blocker = tmp_path / "soy-un-fichero"
        blocker.write_text("x")
        logs = []
        client = Telemetry("dropbot", "1", str(blocker / "state" / "telemetry.json"), log=logs.append)
        client.count("cmd_start")
        client._flush(force=True)
        assert any("not written" in line for line in logs)
        assert client.preview()["usage"] == {"cmd_start": 1}

    @pytest.mark.parametrize("content", [b"{roto", b"[1, 2]", b'"texto"', b"\xff\xfe"])
    def test_un_estado_ilegible_empieza_de_cero(self, make_client, content):
        client = make_client()
        path = client.state_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)

        fresh = make_client()
        assert fresh._state == Telemetry._empty_state()

    def test_pendientes_que_no_son_un_dict_se_descartan(self, make_client):
        client = make_client()
        client.count("cmd_start")
        client._flush(force=True)
        state = _stored(client)
        state["pending"] = ["cmd_start"]
        state["clave_desconocida"] = 1
        with open(client.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

        fresh = make_client()
        assert fresh._state["pending"] == {}
        assert "clave_desconocida" not in fresh._state
        fresh.count("cmd_start")
        assert fresh.preview()["usage"] == {"cmd_start": 1}

    @pytest.mark.parametrize("field, value", [
        ("interval_hours", "24"), ("last_sent", "ayer"), ("paused_until", [1]),
    ])
    def test_un_campo_con_tipo_corrupto_no_bloquea_los_envios(self, make_client, server, field, value):
        client = make_client(server.url)
        client.count("cmd_start")
        client._flush(force=True)
        state = _stored(client)
        state[field] = value
        with open(client.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

        fresh = make_client(server.url)
        assert fresh._tick(now=NOW) is True

    def test_un_contador_corrupto_no_hace_lanzar_a_count(self, make_client):
        client = make_client()
        client.count("cmd_start")
        client._flush(force=True)
        state = _stored(client)
        state["pending"]["cmd_start"] = "muchos"
        with open(client.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

        make_client().count("cmd_start")


class TestHilo:
    def test_start_es_idempotente(self, make_client):
        client = make_client()
        runs = []
        client._run = lambda: runs.append(1)

        client.start()
        first = client._thread
        client.start()
        first.join(timeout=1)

        assert client._thread is first
        assert runs == [1]

    def test_un_tick_que_falla_no_mata_el_hilo(self, make_client, monkeypatch):
        client = make_client(debug=True)
        ticks, sleeps = [], []

        class Stop(Exception):
            pass

        def tick():
            ticks.append(1)
            raise RuntimeError("inesperado")

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 2:
                raise Stop

        client._tick = tick
        monkeypatch.setattr(telemetry.time, "sleep", fake_sleep)
        with pytest.raises(Stop):
            client._run()

        assert len(ticks) == 2
        assert sleeps == [0, telemetry.DEBUG_WAIT_SECONDS, telemetry.DEBUG_WAIT_SECONDS]
        assert any("tick failed" in line for line in client.logs)


class TestArquitectura:
    @pytest.mark.parametrize("machine, expected", [
        ("x86_64", "amd64"), ("AMD64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64"),
        ("armv7l", "armv7"), ("armv6l", "armv6"), ("i686", "386"), ("i386", "386"),
        ("riscv64", "riscv64"), ("some-very-long-machine-name", "some_very_long_m"),
        ("", "unknown"),
    ])
    def test_se_normaliza(self, monkeypatch, machine, expected):
        monkeypatch.setattr(telemetry.platform, "machine", lambda: machine)
        assert telemetry.architecture() == expected
