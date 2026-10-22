"""
Servicio para obtener y mostrar la lista de donantes.
"""
import asyncio
import html

import requests

from config import DONORS_URL
from logger import error
from translations import get_text, PARSE_MODE
from utils.telegram_helpers import SAFE_MESSAGE_LENGTH, safe_send_message, utf16_len


async def get_array_donors_online():
    """Obtiene la lista de donantes desde el servidor"""
    headers = {
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }

    try:
        # En un hilo: requests es síncrono y bloqueaba el bot entero hasta 10 s
        response = await asyncio.to_thread(requests.get, DONORS_URL, headers=headers, timeout=10)
        if response.status_code == 200:
            try:
                data = response.json()
                if isinstance(data, list):
                    data.sort()
                    return data
                else:
                    error(f"Error getting donors list: data is not a list [{str(data)}]")
                    return []
            except ValueError:
                # Recortado: un portal cautivo o un proxy mandan páginas enteras
                error(f"Error getting donors list: data is not a json [{response.text[:300]}]")
                return []
        else:
            error(f"Error getting donors list: error code [{response.status_code}]")
            return []
    except Exception as e:
        error(f"[DONORS] Error getting donors list: {str(e)}")
        return []


def _split_by_lines(text):
    """Parte `text` en mensajes que caben en Telegram, siempre entre líneas.

    Las etiquetas de la plantilla y cada nombre escapado van en una sola línea,
    así que cortar entre líneas no rompe el HTML. Se mide el texto con sus
    etiquetas, que es más largo que el que cuenta Telegram: sobra margen.
    """
    messages = []
    current = None
    for line in text.split("\n"):
        candidate = line if current is None else f"{current}\n{line}"
        if current is not None and utf16_len(candidate) > SAFE_MESSAGE_LENGTH:
            messages.append(current)
            candidate = line
        current = candidate
    messages.append(current)
    return messages


async def print_donors(chat_id, reply_to=None):
    """Muestra la lista de donantes (`reply_to`: el tema del foro, ver get_topic)"""
    donors = await get_array_donors_online()
    if donors:
        result = ""
        for donor in donors:
            # El mensaje va en HTML: un nombre con < o & lo rompía
            result += f"· {html.escape(str(donor))}\n"
        # En un solo mensaje, una lista larga pasaba del límite de Telegram y la
        # cola lo descartaba sin avisar: /donors se quedaba sin respuesta
        try:
            for text in _split_by_lines(get_text("donors_list", result)):
                await safe_send_message(chat_id, text, parse_mode="HTML", wait_for_result=True, reply_to=reply_to)
        except Exception as e:
            error(f"[DONORS] Error sending donors list: {e}")
            await safe_send_message(chat_id, get_text("error_getting_donors"), parse_mode=PARSE_MODE, reply_to=reply_to)
    else:
        await safe_send_message(chat_id, get_text("error_getting_donors"), parse_mode=PARSE_MODE, reply_to=reply_to)
