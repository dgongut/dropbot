"""Flujos de /list y /manage: listar, ver acciones, renombrar, borrar, extraer.

Estos tests son la red de seguridad de un grupo de handlers que antes no tenía
ninguna: comprueban el resultado observable de cada uno (qué se envía, qué
estado se toca, qué pasa en el disco), no su implementación.
"""

import os

import pytest

CALLBACK_DATA_LIMIT = 64


def _buttons(sent_messages):
    for _, _, kwargs in sent_messages:
        for row in kwargs.get("buttons") or []:
            for button in row if isinstance(row, (list, tuple)) else [row]:
                yield button


def _payloads(sent_messages):
    return [b.data for b in _buttons(sent_messages) if getattr(b, "data", None)]


@pytest.fixture
def managed(quiet_manage, tmp_path):
    """Un fichero y una carpeta registrados en pending_file_actions."""
    dropbot = quiet_manage
    dropbot.pending_file_actions.clear()
    dropbot.pending_renames.clear()
    dropbot.list_messages.clear()

    target = tmp_path / "Una película.mkv"
    target.write_bytes(b"\0" * 2048)
    folder = tmp_path / "Una carpeta"
    folder.mkdir()
    (folder / "dentro.txt").write_text("contenido")

    dropbot.pending_file_actions["f1"] = str(target)
    dropbot.pending_file_actions["d1"] = str(folder)
    return dropbot, str(target), str(folder)


class TestAccionesDeUnElemento:
    def test_muestra_las_acciones_de_un_fichero(
        self, managed, sent_messages, texts, make_event, run_async
    ):
        dropbot, target, _ = managed

        run_async(dropbot.handle_file_action(make_event(groups=(b"f1",))))

        assert "Una película.mkv" in texts()
        payloads = _payloads(sent_messages)
        assert any(p.startswith(b"rename:") for p in payloads)
        assert any(p.startswith(b"delete:") for p in payloads)
        assert any(p.startswith(b"download:") for p in payloads), \
            "un fichero pequeño debe poder descargarse"

    def test_una_carpeta_no_ofrece_descargar(
        self, managed, sent_messages, make_event, run_async
    ):
        dropbot, _, _ = managed

        run_async(dropbot.handle_file_action(make_event(groups=(b"d1",))))

        payloads = _payloads(sent_messages)
        assert not any(p.startswith(b"download:") for p in payloads)
        assert not any(p.startswith(b"extract:") for p in payloads)

    def test_un_fichero_enorme_no_ofrece_descargar(
        self, managed, sent_messages, make_event, run_async, tmp_path
    ):
        dropbot, _, _ = managed
        huge = tmp_path / "enorme.mkv"
        with open(huge, "wb") as handle:
            handle.truncate(3 * 1024 * 1024 * 1024)
        dropbot.pending_file_actions["big"] = str(huge)

        run_async(dropbot.handle_file_action(make_event(groups=(b"big",))))

        assert not any(p.startswith(b"download:") for p in _payloads(sent_messages))

    def test_un_comprimido_ofrece_descomprimir(
        self, managed, sent_messages, make_event, run_async, tmp_path
    ):
        dropbot, _, _ = managed
        archive = tmp_path / "cosas.zip"
        archive.write_bytes(b"PK\x03\x04" + b"\0" * 100)
        dropbot.pending_file_actions["z1"] = str(archive)

        run_async(dropbot.handle_file_action(make_event(groups=(b"z1",))))

        assert any(p.startswith(b"extract:") for p in _payloads(sent_messages))

    def test_un_id_desconocido_avisa(self, managed, texts, make_event, run_async):
        dropbot, _, _ = managed

        run_async(dropbot.handle_file_action(make_event(groups=(b"noexiste",))))

        assert "MISSING" not in texts()
        assert texts(), "debe decir algo al usuario"


class TestBorrado:
    def test_pedir_borrar_no_borra_todavia(
        self, managed, sent_messages, make_event, run_async
    ):
        dropbot, target, _ = managed

        run_async(dropbot.handle_delete_file(make_event(groups=(b"f1",))))

        assert os.path.exists(target), "solo debe pedir confirmación"
        assert any(p.startswith(b"confirmdelete:") for p in _payloads(sent_messages))

    def test_confirmar_borra_el_fichero(self, managed, make_event, run_async):
        dropbot, target, _ = managed

        run_async(dropbot.handle_confirm_delete(make_event(groups=(b"f1",))))

        assert not os.path.exists(target)
        assert "f1" not in dropbot.pending_file_actions

    def test_confirmar_borra_la_carpeta_completa(self, managed, make_event, run_async):
        dropbot, _, folder = managed

        run_async(dropbot.handle_confirm_delete(make_event(groups=(b"d1",))))

        assert not os.path.exists(folder)

    def test_confirmar_algo_que_ya_no_esta_avisa(
        self, managed, texts, make_event, run_async
    ):
        dropbot, target, _ = managed
        os.remove(target)

        run_async(dropbot.handle_confirm_delete(make_event(groups=(b"f1",))))

        assert "MISSING" not in texts() and texts()


class TestRenombrado:
    def test_pedir_renombrar_deja_la_peticion_en_espera(
        self, managed, make_event, run_async
    ):
        dropbot, target, _ = managed
        event = make_event(groups=(b"f1",))

        run_async(dropbot.handle_rename_file(event))

        pending = dropbot.pending_renames[event.sender_id]
        assert pending and pending[0]["file_path"] == target

    def test_el_nombre_nuevo_renombra_el_fichero(
        self, managed, make_event, run_async
    ):
        dropbot, target, _ = managed
        event = make_event(groups=(b"f1",))
        run_async(dropbot.handle_rename_file(event))

        reply = _RenameInput("Otro nombre.mkv", sender_id=event.sender_id)
        run_async(dropbot.handle_rename_input(reply))

        assert not os.path.exists(target)
        assert os.path.exists(os.path.join(os.path.dirname(target), "Otro nombre.mkv"))

    def test_un_nombre_con_barras_se_rechaza(
        self, managed, texts, make_event, run_async
    ):
        dropbot, target, _ = managed
        event = make_event(groups=(b"f1",))
        run_async(dropbot.handle_rename_file(event))

        reply = _RenameInput("../fuera.mkv", sender_id=event.sender_id)
        run_async(dropbot.handle_rename_input(reply))

        assert os.path.exists(target), "no debe renombrar nada"
        assert "MISSING" not in texts()


class _RenameInput:
    """Mensaje de respuesta con el nombre nuevo."""

    def __init__(self, text, sender_id=999):
        self.raw_text = text
        self.sender_id = sender_id
        self.reply_to_msg_id = None
        self.id = 500

    async def delete(self):
        return None


class TestCerrar:
    def test_cerrar_limpia_los_mensajes_de_la_lista(
        self, managed, make_event, run_async
    ):
        dropbot, _, _ = managed
        event = make_event()
        dropbot.list_messages[event.sender_id] = [object(), object()]

        run_async(dropbot.handle_close(event))

        assert event.sender_id not in dropbot.list_messages


class TestExtraccion:
    def test_algo_que_no_es_comprimido_se_rechaza(
        self, managed, texts, make_event, run_async
    ):
        dropbot, _, _ = managed

        run_async(dropbot.handle_extract_file(make_event(groups=(b"f1",))))

        assert "MISSING" not in texts() and texts()

    @pytest.mark.parametrize("action,should_exist", [
        (b"keepcompressed", True),
        (b"delcompressed", False),
    ])
    def test_conservar_o_borrar_el_comprimido(
        self, managed, make_event, run_async, tmp_path, action, should_exist
    ):
        dropbot, _, _ = managed
        archive = tmp_path / "paquete.zip"
        archive.write_bytes(b"PK\x03\x04" + b"\0" * 50)
        dropbot.pending_file_actions["z9"] = str(archive)

        run_async(dropbot.handle_compressed_file_action(
            make_event(groups=(action, b"z9"))
        ))

        assert os.path.exists(archive) is should_exist


class TestListados:
    @pytest.mark.parametrize("handler_name", ["handle_list_category", "handle_manage_category"])
    def test_listar_no_revienta_con_la_carpeta_vacia(
        self, quiet_manage, texts, make_event, run_async, handler_name
    ):
        handler = getattr(quiet_manage, handler_name)

        run_async(handler(make_event(groups=(b"all",))))

        assert "MISSING" not in texts()

    def test_manage_registra_los_ficheros_que_lista(
        self, quiet_manage, sent_messages, make_event, run_async
    ):
        dropbot = quiet_manage
        dropbot.pending_file_actions.clear()
        target = os.path.join(dropbot.DOWNLOAD_PATHS["video"], "listado.mp4")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"\0" * 128)

        try:
            run_async(dropbot.handle_manage_category(make_event(groups=(b"all",))))
            assert target in dropbot.pending_file_actions.values()
        finally:
            os.remove(target)


class TestPayloads:
    def test_ningun_boton_se_pasa_del_limite(
        self, managed, sent_messages, make_event, run_async
    ):
        """Ningún botón del grupo puede llevar rutas en el payload."""
        dropbot, _, _ = managed

        run_async(dropbot.handle_file_action(make_event(groups=(b"f1",))))
        run_async(dropbot.handle_delete_file(make_event(groups=(b"f1",))))
        run_async(dropbot.handle_rename_file(make_event(groups=(b"f1",))))

        for payload in _payloads(sent_messages):
            assert len(payload) <= CALLBACK_DATA_LIMIT, f"{payload!r}"
