"""La plantilla de salida de yt-dlp no debe dejar "NA" en el nombre.

`%(playlist_index)s` es el número del vídeo dentro de una playlist. En un vídeo
suelto ese campo no existe y yt-dlp escribe literalmente "NA", así que todo lo
descargado de YouTube o de redes sociales acababa como "NA-Título.mp4".

La sintaxis condicional de yt-dlp (`%(campo&valor|alternativa)s`) resuelve el
campo solo si existe: con `%(playlist_index&{}-|)s` una playlist sigue dando
"1-", "2-"... y un vídeo suelto no pone nada.
"""

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_la_plantilla_no_usa_el_indice_sin_condicional(dropbot):
    template = dropbot.ytdlp_output_template(1234567890)

    assert "%(playlist_index)s" not in template, (
        "el campo sin condicional escribe NA en los vídeos sueltos"
    )
    assert "playlist_index&" in template, "falta la sintaxis condicional"


def test_la_plantilla_lleva_titulo_extension_y_marca_temporal(dropbot):
    template = dropbot.ytdlp_output_template(1234567890)

    assert "%(title).200s" in template, "el título se recorta a 200 caracteres"
    assert "%(ext)s" in template
    assert "_temp1234567890." in template


def test_la_marca_temporal_se_limpia_del_nombre_final(dropbot):
    """El sufijo _temp<timestamp> lo quita después la fase de renombrado."""
    template = dropbot.ytdlp_output_template(1234567890)
    # Nombre tal como lo dejaría yt-dlp para un vídeo suelto
    produced = template.replace("%(playlist_index&{}-|)s", "").replace(
        "%(title).200s", "Un título cualquiera").replace("%(ext)s", "mp4")

    cleaned = re.sub(r"_temp\d+\.", ".", produced)

    assert cleaned == "Un título cualquiera.mp4"


def test_el_indice_de_playlist_sigue_siendo_detectable(dropbot):
    """El progreso de playlist saca el número del vídeo del nombre del fichero.

    Si la plantilla dejase de poner el prefijo "N-" en las playlists, el
    contador de "vídeo X de Y" dejaría de funcionar en silencio.
    """
    template = dropbot.ytdlp_output_template(1234567890)
    produced = template.replace("%(playlist_index&{}-|)s", "7-").replace(
        "%(title).200s", "Séptimo vídeo").replace("%(ext)s", "mp4")

    match = re.match(r"^(\d+)-", produced)

    assert match and match.group(1) == "7"


def test_no_quedan_plantillas_escritas_a_mano(dropbot):
    """Estaba repetida en cuatro sitios; una sola copia evita que vuelva a irse."""
    source = pathlib.Path(dropbot.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "ytdlp_output_template"
    )
    helper_source = ast.get_source_segment(source, helper) or ""

    outside = source.replace(helper_source, "")
    assert "playlist_index" not in outside, (
        "hay plantillas de yt-dlp fuera de ytdlp_output_template()"
    )
