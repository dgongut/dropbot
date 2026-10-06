"""/settings: los ajustes del bot, editables desde Telegram y sin reiniciar.

Cada pantalla se repinta en el mismo mensaje. Todos los botones llevan
`cfg:` delante y los atiende un solo handler, que decide por lo que viene
detrás:

    cfg:<pantalla>            abre una pantalla (main, lang, fmt, send...)
    cfg:set:<campo>:<valor>   guarda un valor y repinta el selector con él marcado
    cfg:telshow               estadísticas: ver qué se envía

Los campos y sus valores salen de listas cerradas: un callback fabricado a
mano no puede escribir una clave cualquiera en settings.json.
"""

import json

from telethon import Button, events

import settings
import stats
import store
from config import DOWNLOAD_PATH, DOWNLOAD_PATHS
from logger import debug, warning
from translations import PARSE_MODE, get_text
from utils.telegram_helpers import (
    check_admin_and_warn, safe_answer, safe_delete, safe_edit, safe_send_message,
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

FOLDER_ORDER = ("video", "audio", "photo", "torrent", "ebook", "url_video", "url_audio")


def init(bot, on_change=None):
    global _on_change
    _on_change = on_change
    bot.on(events.NewMessage(pattern=r"^/settings(@\w+)?$"))(handle_settings_command)
    bot.on(events.CallbackQuery(pattern=b"cfg:(.+)"))(handle_settings_callback)


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
        ("settings_row_telemetry", _on_off(stats.is_on()), "tel"),
    ])
    buttons.append([Button.inline(get_text("button_close"), data="cfg:close")])

    lines = [get_text("settings_title")]
    if store.settings_unreadable():
        lines += ["", get_text("settings_unreadable", store.settings_path())]
    if not store.is_persistent():
        lines += ["", get_text("settings_not_persistent")]
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
    lines = [get_text("settings_folders_title"), "", get_text("settings_folders_help"), ""]
    for kind in FOLDER_ORDER:
        path = DOWNLOAD_PATHS[kind]
        lines.append(get_text("settings_folder_line", get_text(f"settings_folder_{kind}"), path,
                              "📌" if path != DOWNLOAD_PATH else "📂"))
    lines += ["", get_text("settings_folders_how")]
    return "\n".join(lines), [_navigation()]


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
    if screen == "tel":
        return build_telemetry()
    return build_main()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_settings_command(event):
    try:
        await event.delete()
    except Exception:
        pass
    if await check_admin_and_warn(event):
        await safe_send_message(event.chat_id, get_text("user_not_admin"), parse_mode=PARSE_MODE)
        return
    stats.count("cmd_settings")
    text, buttons = build_main()
    await safe_send_message(event.chat_id, text, buttons=buttons, parse_mode=PARSE_MODE, link_preview=False)


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
        await safe_send_message(event.chat_id, text, buttons=buttons, parse_mode=PARSE_MODE, link_preview=False)
        return

    if action == "set" and len(parts) == 3:
        await _apply(event, parts[1], parts[2])
        return

    await safe_answer(event)
    await _repaint(event, action)


async def _apply(event, field, token):
    if field == "tel":
        await safe_answer(event)
        if token not in ("0", "1") or (token == "1") == bool(settings.get("telemetry")):
            return
        if token == "1":
            settings.put("telemetry", True)
            stats.count("btn_settings_tel")
        else:
            # Al apagarlas se olvida también el id de la instalación
            stats.disable()
        debug(f"[SETTINGS] telemetry set to {token == '1'}")
        await _repaint(event, "tel")
        return

    if field in TOGGLES:
        key, screen = TOGGLES[field]
        await safe_answer(event)
        value = token == "1"
        if token not in ("0", "1") or value == bool(settings.get(key)):
            return
        settings.put(key, value)
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
    await safe_answer(event)
    # Repintar el mismo mensaje sin cambios lo rechaza Telegram (MessageNotModified)
    if value == _current(field):
        return
    settings.put(key, value)
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
