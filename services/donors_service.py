"""
Servicio para obtener y mostrar la lista de donantes.
"""
import requests

from config import DONORS_URL
from debug import error
from translations import get_text, PARSE_MODE
from utils.telegram_helpers import safe_send_message


async def get_array_donors_online():
    """Obtiene la lista de donantes desde el servidor"""
    headers = {
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }

    try:
        response = requests.get(DONORS_URL, headers=headers, timeout=10)
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
                error(f"Error getting donors list: data is not a json [{response.text}]")
                return []
        else:
            error(f"Error getting donors list: error code [{response.status_code}]")
            return []
    except Exception as e:
        error(f"[DONORS] Error getting donors list: {str(e)}")
        return []


async def print_donors(chat_id):
    """Muestra la lista de donantes"""
    donors = await get_array_donors_online()
    if donors:
        result = ""
        for donor in donors:
            result += f"· {donor}\n"
        await safe_send_message(chat_id, get_text("donors_list", result), parse_mode="HTML")
    else:
        await safe_send_message(chat_id, get_text("error_getting_donors"), parse_mode=PARSE_MODE)
