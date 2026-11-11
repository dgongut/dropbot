"""/settings: los ajustes del bot, editables desde Telegram y sin reiniciar.

Cada pantalla se repinta en el mismo mensaje. Todos los botones llevan
`cfg:` delante y los atiende un solo handler, que decide por lo que viene
detrás:

    cfg:<pantalla>            abre una pantalla (main, lang, fmt, send...)
    cfg:set:<campo>:<valor>   guarda un valor y repinta el selector con él marcado
    cfg:telshow               estadísticas: ver qué se envía
    cfg:fk:<tipo>             carpetas: la de un tipo (fkp la cambia, fkd la devuelve
                              a la de por defecto)
    cfg:favadd, cfg:favdel:<n>  carpetas favoritas: añadir y quitar
    cfg:tple:<plantilla>, cfg:tplr, cfg:tplrc  plantillas de renombrado: editar una,
                              restaurarlas (pregunta) y sí, restaurarlas

Los campos y sus valores salen de listas cerradas: un callback fabricado a
mano no puede escribir una clave cualquiera en settings.json.
"""

import json

from telethon import Button, events

import fileops
import folders
import name_parser
import settings
import stats
import store
from basic import md_code
from config import DOWNLOAD_PATH, DOWNLOAD_PATHS, downloads_persistent
from handlers import inputs, picker
from handlers.navigation import home_button
from logger import debug, warning
from translations import PARSE_MODE, get_text
from utils.telegram_helpers import (
    check_admin_and_warn, for_another_bot, get_topic, is_file_message, safe_answer, safe_delete,
    safe_edit, safe_send_message, warn_not_admin,
)

# Inyectada por init(): qué hacer cuando cambia un ajuste que no basta con
# releer (el idioma de los comandos del menú, el límite de descargas...)
_on_change = None

# campo del callback -> clave en settings.json
FIELDS = {
    "lang": "language",
    "fmt": "urls.auto_format",
    "send": "urls.auto_send",
    "vq": "urls.video_quality",
    "aq": "urls.audio_quality",
    "af": "urls.audio_format",
    "sb": "urls.sponsorblock",
    "plm": "urls.playlist",
    "pll": "urls.playlist_limit",
    "max": "urls.max_size_mb",
    "ext": "extract.after",
    "par": "downloads.parallel",
    "fast": "downloads.fast_connections",
    "hw": "video.hw",
    "q": "video.quality",
}

# Interruptores: su clave en settings.json y la pantalla en la que están
TOGGLES = {
    "compat": ("urls.prefer_compatible", "urlq"),
    "tags": ("urls.audio_tags", "urlq"),
    "ask": ("folders.ask", "folders"),
    "space": ("folders.low_space", "folders"),
    "ar": ("rename.auto", "ren"),
    "arr": ("rename.replace", "ren"),
    "ss": ("series.suggest", "ser"),
    "sd": ("series.direct", "ser"),
}

# A qué pantalla lleva el botón de atrás de cada selector (por defecto, la principal)
RETURN_SCREEN = {
    "fmt": "links", "send": "links", "sb": "links", "max": "links",
    "vq": "urlq", "af": "urlq", "aq": "urlq",
    "plm": "pl", "pll": "pl",
    "hw": "video", "q": "video",
}

# Cuántos valores caben por fila en cada selector. Los textos, uno por fila
PER_ROW = {"par": 4, "fast": 4, "q": 4, "pll": 3, "max": 3}

# Valor de calidad que significa "el del encoder"
DEFAULT_QUALITY = "def"

FOLDER_ORDER = settings.FOLDER_KINDS

# Un nombre de ejemplo por plantilla, con todos los campos que se pueden
# detectar: así cualquier plantilla se ve aplicada
TEMPLATE_EXAMPLES = {
    "movie": "Minions.and.Monsters.2026.1080p.HDR.Castellano.WEB-DL.AAC.2.0.H.264-HDZ.mkv",
    "series": "Breaking.Bad.2008.S01E03.720p.HDR.Castellano.HDTV.DD+5.1.x264-NTb.mkv",
    "season": "Breaking.Bad.2008.S02.1080p.HDR.Castellano.WEB-DL.DD+5.1.H.264-NTb.mkv",
}
TEMPLATE_DEFAULTS = {
    "movie": name_parser.DEFAULT_MOVIE_TEMPLATE,
    "series": name_parser.DEFAULT_SERIES_TEMPLATE,
    "season": name_parser.DEFAULT_SEASON_PACK_TEMPLATE,
}


def init(bot, on_change=None):
    global _on_change
    _on_change = on_change
    # Un fichero con el pie «/settings» es de handle_files: sin el filtro se
    # borraba mientras se descargaba, como con los demás comandos
    bot.on(events.NewMessage(pattern=r"^/settings(@\w+)?$",
                             func=lambda e: not is_file_message(e)))(handle_settings_command)
    bot.on(events.CallbackQuery(pattern=b"cfg:(.+)"))(handle_settings_callback)
    picker.register("kindfolder", _kind_folder_chosen, _kind_folder_cancelled)
    picker.register("favorite", _favorite_chosen, _favorite_cancelled)
    inputs.register("template", handle_template_input)


# ---------------------------------------------------------------------------
# Cómo se muestra cada valor
# ---------------------------------------------------------------------------

def _on_off(value):
    return "✅" if value else "❌"


def _mark(selected):
    return "✅ " if selected else ""


def _language_label(code):
    return settings.LANGUAGE_NAMES.get(code, code)


def _choice_label(field, value):
    """El nombre de un valor de una lista, traducido."""
    if field == "lang":
        return _language_label(value)
    if field == "q":
        return get_text("settings_quality_default") if value is None else str(value)
    if field in ("par", "fast"):
        return str(value)
    if field == "vq" and value != "MAX":
        return f"{value}p"
    if field == "pll":
        return get_text("settings_pll_none") if not value else str(value)
    if field == "max":
        return get_text("settings_max_none") if not value else settings.size_label(value)
    return get_text(f"settings_{field}_{str(value).lower()}")


def _choices(field):
    return {
        "lang": settings.LANGUAGES,
        "fmt": settings.AUTO_FORMATS,
        "send": settings.AUTO_SEND_MODES,
        "vq": settings.VIDEO_QUALITIES,
        "aq": settings.AUDIO_QUALITIES,
        "af": settings.AUDIO_FORMATS,
        "sb": settings.SPONSORBLOCK_MODES,
        "plm": settings.PLAYLIST_MODES,
        "pll": settings.PLAYLIST_LIMIT_CHOICES,
        "max": settings.MAX_SIZE_CHOICES,
        "ext": settings.EXTRACT_AFTER_MODES,
        "par": settings.PARALLEL_CHOICES,
        "fast": settings.FAST_CONNECTIONS_CHOICES,
        "hw": settings.FFMPEG_HW_MODES,
        "q": settings.QUALITY_CHOICES,
    }[field]


def _token(value):
    """Un valor tal como viaja en el callback."""
    return DEFAULT_QUALITY if value is None else str(value)


def _current(field):
    return settings.get(FIELDS[field])


# ---------------------------------------------------------------------------
# Pantallas
# ---------------------------------------------------------------------------

def _navigation(back="main"):
    """La última fila: un paso atrás, y salir. Siempre las dos."""
    return [Button.inline(get_text("button_back"), data=f"cfg:{back}"),
            Button.inline(get_text("button_close"), data="cfg:close")]


def _rows(rows):
    """Una fila por ajuste: (clave del texto, valor que muestra, pantalla que abre)."""
    return [[Button.inline(get_text(key, *([] if value is None else [value])), data=f"cfg:{screen}")]
            for key, value, screen in rows]


def build_main():
    buttons = _rows([
        ("settings_row_language", _language_label(settings.language()), "lang"),
        ("settings_row_links", None, "links"),
        ("settings_row_parallel", settings.parallel_downloads(), "par"),
        ("settings_row_fast", settings.fast_connections(), "fast"),
        ("settings_row_video", _choice_label("hw", settings.ffmpeg_hw()), "video"),
        ("settings_row_ext", _choice_label("ext", settings.extract_after()), "ext"),
        ("settings_row_folders", sum(1 for kind in FOLDER_ORDER if DOWNLOAD_PATHS[kind] != DOWNLOAD_PATH), "folders"),
        ("settings_row_rename", _on_off(settings.auto_rename()), "ren"),
        ("settings_row_series", _series_label(), "ser"),
        ("settings_row_telemetry", _on_off(stats.is_on()), "tel"),
    ])
    # Al menú principal (el de /start), o cerrar
    buttons.append([home_button(), Button.inline(get_text("button_close"), data="cfg:close")])

    lines = [get_text("settings_title")]
    if store.settings_unreadable():
        lines += ["", get_text("settings_unreadable", store.settings_path())]
    if not store.is_persistent():
        lines += ["", get_text("settings_not_persistent")]
    elif store.write_error():
        lines += ["", get_text("settings_not_writable")]
    if not downloads_persistent():
        lines += ["", get_text("settings_downloads_not_persistent")]
    return "\n".join(lines), buttons


def build_picker(field):
    """Una lista de valores con el actual marcado."""
    current = _current(field)
    buttons = []
    row = []
    per_row = PER_ROW.get(field, 1)
    for value in _choices(field):
        row.append(Button.inline(f"{_mark(value == current)}{_choice_label(field, value)}",
                                 data=f"cfg:set:{field}:{_token(value)}"))
        if len(row) == per_row:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(_navigation(RETURN_SCREEN.get(field, "main")))
    return f'{get_text(f"settings_{field}_title")}\n\n{get_text(f"settings_{field}_help")}', buttons


def build_video():
    quality = settings.ffmpeg_quality()
    buttons = [
        [Button.inline(get_text("settings_row_hw", _choice_label("hw", settings.ffmpeg_hw())), data="cfg:hw")],
        [Button.inline(get_text("settings_row_quality", _choice_label("q", quality)), data="cfg:q")],
        _navigation(),
    ]
    return f'{get_text("settings_video_title")}\n\n{get_text("settings_video_help")}', buttons


def _toggle(field, text_key):
    """Un interruptor, con su estado en la propia etiqueta."""
    on = bool(settings.get(TOGGLES[field][0]))
    return Button.inline(f"{_on_off(on)} {get_text(text_key)}", data=f"cfg:set:{field}:{0 if on else 1}")


def build_links():
    buttons = _rows([
        ("settings_row_fmt", _choice_label("fmt", settings.auto_format()), "fmt"),
        ("settings_row_send", _choice_label("send", settings.auto_send()), "send"),
        ("settings_row_urlq", _choice_label("vq", settings.video_quality()), "urlq"),
        ("settings_row_pl", _choice_label("plm", settings.playlist_mode()), "pl"),
        ("settings_row_sb", _choice_label("sb", settings.sponsorblock()), "sb"),
        ("settings_row_max", _choice_label("max", settings.max_size_mb()), "max"),
    ])
    buttons.append(_navigation())
    return f'{get_text("settings_links_title")}\n\n{get_text("settings_links_help")}', buttons


def build_url_quality():
    rows = [
        ("settings_row_vq", _choice_label("vq", settings.video_quality()), "vq"),
        ("settings_row_af", _choice_label("af", settings.audio_format()), "af"),
    ]
    # La calidad solo se elige al convertir a MP3: el M4A va tal cual viene
    if settings.audio_format() == "MP3":
        rows.append(("settings_row_aq", _choice_label("aq", settings.audio_quality()), "aq"))
    buttons = _rows(rows)
    buttons.append([_toggle("compat", "settings_row_compat")])
    buttons.append([_toggle("tags", "settings_row_tags")])
    buttons.append(_navigation("links"))
    return f'{get_text("settings_urlq_title")}\n\n{get_text("settings_urlq_help")}', buttons


def build_playlists():
    buttons = _rows([
        ("settings_row_plm", _choice_label("plm", settings.playlist_mode()), "plm"),
        ("settings_row_pll", _choice_label("pll", settings.playlist_limit()), "pll"),
    ])
    buttons.append(_navigation("links"))
    return f'{get_text("settings_pl_title")}\n\n{get_text("settings_pl_help")}', buttons


def build_folders():
    chosen = folders.chosen_paths()
    lines = [get_text("settings_folders_title"), "", get_text("settings_folders_help"), ""]
    buttons = []
    row = []
    for kind in FOLDER_ORDER:
        path = DOWNLOAD_PATHS[kind]
        # ✏️ elegida aquí, 📌 por su volumen, 📂 a la general
        icon = "✏️" if kind in chosen else ("📌" if path != DOWNLOAD_PATH else "📂")
        lines.append(get_text("settings_folder_line", get_text(f"settings_folder_{kind}"), path, icon))
        row.append(Button.inline(get_text(f"settings_folder_{kind}"), data=f"cfg:fk:{kind}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    lines += ["", get_text("settings_folders_how")]
    buttons.append([Button.inline(get_text("settings_row_favorites", len(folders.favorites())), data="cfg:fav")])
    buttons.append([_toggle("ask", "settings_row_ask")])
    buttons.append([_toggle("space", "settings_row_space")])
    buttons.append(_navigation())
    return "\n".join(lines), buttons


def build_kind_folder(kind):
    """La carpeta de un tipo: cuál es, por qué, y cómo cambiarla."""
    path = DOWNLOAD_PATHS[kind]
    default = folders.default_paths()[kind]
    chosen = folders.is_chosen(kind)
    lines = [get_text("settings_kind_title", get_text(f"settings_folder_{kind}")), "",
             get_text("settings_kind_current", path),
             get_text("settings_kind_chosen" if chosen else "settings_kind_default")]
    buttons = [[Button.inline(get_text("button_folder_change"), data=f"cfg:fkp:{kind}")]]
    if chosen:
        lines.append(get_text("settings_kind_default_is", default))
        buttons.append([Button.inline(get_text("button_folder_default"), data=f"cfg:fkd:{kind}")])
    buttons.append(_navigation("folders"))
    return "\n".join(lines), buttons


def build_favorites():
    favorites = folders.favorites()
    lines = [get_text("settings_fav_title"), "", get_text("settings_fav_help"), "",
             get_text("settings_fav_hint" if favorites else "settings_fav_empty")]
    buttons = [[Button.inline(f"🗑 {picker.short_path(path)}", data=f"cfg:favdel:{index}")]
               for index, path in enumerate(favorites)]
    buttons.append([Button.inline(get_text("button_fav_add"), data="cfg:favadd")])
    buttons.append(_navigation("folders"))
    return "\n".join(lines), buttons


def _series_label():
    if settings.series_direct():
        return get_text("settings_series_direct")
    return get_text("settings_series_suggest") if settings.series_suggest() else _on_off(False)


def build_rename():
    buttons = [[_toggle("ar", "settings_row_ar")]]
    # Sustituir solo tiene sentido con el renombrado automático
    if settings.auto_rename():
        buttons.append([_toggle("arr", "settings_row_arr")])
    buttons.append([Button.inline(get_text("settings_row_tpl"), data="cfg:tpl")])
    buttons.append(_navigation())
    return f'{get_text("settings_ren_title")}\n\n{get_text("settings_ren_help")}', buttons


def build_series():
    buttons = [[_toggle("ss", "settings_row_ss")], [_toggle("sd", "settings_row_sd")], _navigation()]
    return f'{get_text("settings_ser_title")}\n\n{get_text("settings_ser_help")}', buttons


def _template_preview(kind):
    """El nombre que da la plantilla de `kind` con su ejemplo."""
    return name_parser.suggest_name(TEMPLATE_EXAMPLES[kind], **fileops.naming()) or "-"


def _template(kind):
    return settings.rename_template(kind) or TEMPLATE_DEFAULTS[kind]


def build_templates():
    lines = [get_text("settings_tpl_title"), "", get_text("settings_tpl_help")]
    buttons = []
    for kind in settings.TEMPLATE_KINDS:
        lines += ["", get_text(f"settings_tpl_{kind}"), md_code(_template(kind)),
                  get_text("settings_tpl_example", _template_preview(kind))]
        buttons.append([Button.inline(get_text(f"button_tpl_{kind}"), data=f"cfg:tple:{kind}")])
    lines += ["", get_text("settings_tpl_fields")]
    # Solo si alguna está cambiada: con todas por defecto no hacía nada
    if any(settings.rename_template(kind) for kind in settings.TEMPLATE_KINDS):
        buttons.append([Button.inline(get_text("button_tpl_reset"), data="cfg:tplr")])
    buttons.append(_navigation("ren"))
    return "\n".join(lines), buttons


def build_template_prompt(kind, problem=None):
    lines = [get_text(f"settings_tpl_{kind}"), "", get_text("settings_tpl_ask"), "",
             get_text("settings_tpl_now"), md_code(_template(kind)),
             get_text("settings_tpl_example", _template_preview(kind)), "", get_text("settings_tpl_fields")]
    if problem:
        lines += ["", problem]
    return "\n".join(lines), [[Button.inline(get_text("button_back"), data="cfg:tpl")]]


def build_telemetry():
    """Qué son, el interruptor y qué envían.

    Cuando algo fuera del menú las ha apagado no hay interruptor, solo el
    motivo: pulsarlo no cambiaría nada.
    """
    forced = stats.forced_off()
    lines = [get_text("settings_telemetry_title"), "", get_text("settings_telemetry_help")]
    buttons = []
    if forced == "volume":
        lines += ["", get_text("settings_telemetry_forced_volume")]
    elif forced == "settings":
        lines += ["", get_text("settings_telemetry_forced_settings", store.settings_path())]
    elif forced:
        lines += ["", get_text("settings_telemetry_forced_env", forced)]
    else:
        on = stats.is_on()
        buttons.append([Button.inline(f"{_on_off(on)} {get_text('button_telemetry_toggle')}",
                                      data=f"cfg:set:tel:{0 if on else 1}")])
    buttons.append([Button.inline(get_text("button_telemetry_show"), data="cfg:telshow")])
    buttons.append(_navigation())
    return "\n".join(lines), buttons


def build_telemetry_preview():
    payload, hidden = stats.preview()
    body = json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True)
    text = f'{get_text("telemetry_preview")}\n\n```\n{body}\n```'
    if hidden:
        text += f'\n{get_text("telemetry_preview_truncated", hidden)}'
    return text, [[Button.inline(get_text("button_close"), data="cfg:close")]]


def build_screen(screen):
    if screen in FIELDS:
        return build_picker(screen)
    if screen == "video":
        return build_video()
    if screen == "links":
        return build_links()
    if screen == "urlq":
        return build_url_quality()
    if screen == "pl":
        return build_playlists()
    if screen == "folders":
        return build_folders()
    if screen == "fav":
        return build_favorites()
    if screen == "ren":
        return build_rename()
    if screen == "ser":
        return build_series()
    if screen == "tpl":
        return build_templates()
    if screen == "tel":
        return build_telemetry()
    return build_main()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_settings_command(event):
    # "/settings@OtroBot" es para otro bot del grupo: ni se borra ni se contesta
    if await for_another_bot(event):
        return
    if await check_admin_and_warn(event):
        await warn_not_admin(event)
        return
    try:
        await event.delete()
    except Exception:
        pass
    stats.count("cmd_settings")
    text, buttons = build_main()
    # En un foro, al tema del comando y no a General
    await safe_send_message(event.chat_id, text, buttons=buttons, parse_mode=PARSE_MODE, link_preview=False,
                            reply_to=await get_topic(event))


async def handle_settings_callback(event):
    if await check_admin_and_warn(event):
        return
    parts = event.pattern_match.group(1).decode(errors="replace").split(":")
    action = parts[0]

    if action == "close":
        await safe_answer(event)
        await safe_delete(event)
        return

    if action == "telshow":
        await safe_answer(event)
        text, buttons = build_telemetry_preview()
        await safe_send_message(event.chat_id, text, buttons=buttons, parse_mode=PARSE_MODE, link_preview=False,
                                reply_to=await get_topic(event))
        return

    if action == "set" and len(parts) == 3:
        await _apply(event, parts[1], parts[2])
        return

    # Cualquier botón deja de esperar la plantilla que se iba a escribir
    inputs.forget(event.sender_id, "template")

    if action in ("fk", "fkp", "fkd") and len(parts) == 2 and parts[1] in FOLDER_ORDER:
        await _kind_folder_action(event, action, parts[1])
        return

    if action == "favadd":
        await safe_answer(event)
        await picker.show(event, picker.open_picker("favorite", get_text("settings_fav_add_title")))
        return

    if action == "favdel" and len(parts) == 2 and parts[1].isdigit():
        favorites = folders.favorites()
        if int(parts[1]) < len(favorites):
            folders.remove_favorite(favorites[int(parts[1])])
            stats.count("btn_settings_fav")
        await _answer_saved(event)
        await _repaint(event, "fav")
        return

    if action == "tple" and len(parts) == 2 and parts[1] in settings.TEMPLATE_KINDS:
        await safe_answer(event)
        inputs.ask(event, "template", kind=parts[1])
        text, buttons = build_template_prompt(parts[1])
        await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE, link_preview=False)
        return

    if action == "tplr":
        # Se pregunta antes: un toque borraba las tres plantillas escritas a mano
        await safe_answer(event)
        buttons = [[Button.inline(get_text("button_tpl_reset_confirm"), data="cfg:tplrc"),
                    Button.inline(get_text("button_cancel"), data="cfg:tpl")]]
        await safe_edit(event, get_text("settings_tpl_reset_ask"), buttons=buttons, parse_mode=PARSE_MODE,
                        link_preview=False)
        return

    if action == "tplrc":
        if not any(settings.rename_template(kind) for kind in settings.TEMPLATE_KINDS):
            await safe_answer(event)
            await _repaint(event, "tpl")
            return
        with store.batch():
            for kind in settings.TEMPLATE_KINDS:
                settings.put(f"rename.template_{kind}", "")
        await _answer_saved(event)
        await _repaint(event, "tpl")
        return

    await safe_answer(event)
    await _repaint(event, action)


# ---------------------------------------------------------------------------
# Carpetas
# ---------------------------------------------------------------------------

async def _show(event, built):
    text, buttons = built
    await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE, link_preview=False)


def _with_problem(built, problem):
    """La pantalla, con el motivo por el que no se ha hecho lo pedido: sin él
    se repintaba igual que estaba y no se sabía que había fallado."""
    text, buttons = built
    return (f"{text}\n\n{problem}" if problem else text), buttons


async def _kind_folder_action(event, action, kind):
    if action == "fkp":
        await safe_answer(event)
        title = get_text("settings_kind_pick", get_text(f"settings_folder_{kind}"))
        await picker.show(event, picker.open_picker("kindfolder", title, payload={"kind": kind}))
        return
    if action == "fkd":
        if folders.is_chosen(kind) or kind in settings.folder_paths():
            folders.set_kind_folder(kind, None)
            debug(f"[SETTINGS] The folder for {kind} is back to its default")
            await _answer_saved(event)
        else:
            await safe_answer(event)
    else:
        await safe_answer(event)
    await _show(event, build_kind_folder(kind))


async def _kind_folder_chosen(event, chosen, path):
    kind = chosen["payload"]["kind"]
    problem = None
    try:
        folders.set_kind_folder(kind, path)
    except folders.FolderError as e:
        warning(f"[SETTINGS] Could not set the folder for {kind} to {path}: {e.code}")
        problem = get_text(f"folder_error_{e.code}")
    else:
        debug(f"[SETTINGS] The folder for {kind} is now {path}")
        stats.count("btn_settings_folder")
    await _show(event, _with_problem(build_kind_folder(kind), problem))


async def _kind_folder_cancelled(event, chosen):
    await _show(event, build_kind_folder(chosen["payload"]["kind"]))


async def _favorite_chosen(event, chosen, path):
    problem = None
    try:
        folders.add_favorite(path)
        stats.count("btn_settings_fav")
    except folders.FolderError as e:
        warning(f"[SETTINGS] Could not add the favourite {path}: {e.code}")
        problem = get_text(f"folder_error_{e.code}")
    await _show(event, _with_problem(build_favorites(), problem))


async def _favorite_cancelled(event, chosen):
    await _show(event, build_favorites())


# ---------------------------------------------------------------------------
# Plantillas de renombrado
# ---------------------------------------------------------------------------

def _template_problem(template):
    """Por qué no vale una plantilla, para enseñarlo, o None si vale."""
    try:
        name_parser.validate_template(template)
    except name_parser.TemplateError as e:
        if e.code in ("unknown_field", "padding_not_allowed", "invalid_char", "padding_too_wide"):
            return get_text(f"settings_tpl_error_{e.code}", e.detail)
        return get_text("settings_tpl_error_invalid")
    return None


async def handle_template_input(event, text, pending, topic):
    """La plantilla escrita a mano: se guarda si vale, y si no se dice por qué."""
    kind = pending["data"]["kind"]
    message = pending["message"]
    problem = _template_problem(text)
    if problem:
        # Se sigue esperando: lo normal es corregirla y volver a mandarla
        inputs.ask(message, "template", kind=kind)
        await _show(message, build_template_prompt(kind, problem))
        return
    settings.put(f"rename.template_{kind}", text)
    debug(f"[SETTINGS] rename.template_{kind} set")
    stats.count("btn_settings_tpl")
    await _show(message, build_templates())


async def _answer_saved(event):
    """Contesta al botón después de guardar.

    Si no se ha podido escribir, con un aviso que hay que cerrar: lo elegido
    vale hasta el reinicio, pero no se puede dar por guardado.
    """
    if store.write_error():
        await safe_answer(event, get_text("settings_not_saved"), alert=True)
    else:
        await safe_answer(event)


async def _apply(event, field, token):
    if field == "tel":
        if token not in ("0", "1") or (token == "1") == bool(settings.get("telemetry")):
            await safe_answer(event)
            return
        if token == "1":
            settings.put("telemetry", True)
            stats.count("btn_settings_tel")
        else:
            # Al apagarlas se olvida también el id de la instalación
            stats.disable()
        await _answer_saved(event)
        debug(f"[SETTINGS] telemetry set to {token == '1'}")
        await _repaint(event, "tel")
        return

    if field in TOGGLES:
        key, screen = TOGGLES[field]
        value = token == "1"
        if token not in ("0", "1") or value == bool(settings.get(key)):
            await safe_answer(event)
            return
        settings.put(key, value)
        await _answer_saved(event)
        debug(f"[SETTINGS] {key} set to {value!r}")
        stats.count(f"btn_settings_{field}")
        await _repaint(event, screen)
        return

    key = FIELDS.get(field)
    allowed = {_token(value): value for value in _choices(field)} if key else {}
    if token not in allowed:
        warning(f"[SETTINGS] Ignoring unknown setting {field}={token!r}")
        await safe_answer(event)
        return

    value = allowed[token]
    # Repintar el mismo mensaje sin cambios lo rechaza Telegram (MessageNotModified)
    if value == _current(field):
        await safe_answer(event)
        return
    settings.put(key, value)
    await _answer_saved(event)
    debug(f"[SETTINGS] {key} set to {value!r}")
    stats.count(f"btn_settings_{field}")
    if _on_change:
        try:
            await _on_change(key)
        except Exception as e:
            warning(f"[SETTINGS] Could not apply {key}: {e}")
    # Se queda en el selector, con la marca ya en el valor elegido. Si es el
    # idioma, la pantalla se repinta ya en el nuevo
    await _repaint(event, field)


async def _repaint(event, screen):
    text, buttons = build_screen(screen)
    # Sin vista previa: el texto de las estadísticas lleva enlaces, y la
    # tarjeta de la web taparía los botones
    await safe_edit(event, text, buttons=buttons, parse_mode=PARSE_MODE, link_preview=False)
