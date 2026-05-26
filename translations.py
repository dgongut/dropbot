"""
Sistema de internacionalización (i18n) para DropBot.

Características:
- Carga de traducciones desde archivos JSON
- Caché automático de traducciones para optimizar rendimiento
- Fallback a inglés si falta una traducción
- Sustitución de placeholders ($1, $2, etc.)
"""

import json
from functools import lru_cache
from pathlib import Path
from debug import warning, error
from config import LANGUAGE

# Constante para el parse_mode por defecto
PARSE_MODE = "markdown"


@lru_cache(maxsize=4)
def load_locale(locale: str) -> dict:
	"""
	Carga un archivo de locale desde disco.

	Usa LRU cache para evitar lecturas repetidas del JSON.
	El cache se mantiene para los últimos 4 locales (suficiente para ES/EN + fallbacks).

	Args:
		locale: Código del locale (ej: "es", "en")

	Returns:
		Diccionario con las traducciones

	Raises:
		FileNotFoundError: Si el archivo de locale no existe
		json.JSONDecodeError: Si el archivo no es JSON válido
	"""
	locale_path = Path(f"/app/locale/{locale}.json")

	if not locale_path.exists():
		raise FileNotFoundError(f"Locale file not found: {locale_path}")

	with open(locale_path, "r", encoding="utf-8") as file:
		return json.load(file)


def get_text(key: str, *args) -> str:
	"""
	Obtiene una cadena traducida con sustitución de placeholders.

	Busca primero en el idioma configurado, si no existe busca en inglés.
	Los placeholders $1, $2, etc. se reemplazan con los argumentos.

	Args:
		key: Clave de la traducción
		*args: Valores para sustituir en los placeholders

	Returns:
		Cadena traducida con placeholders sustituidos

	Example:
		>>> get_text("welcome_user", "John")
		"¡Bienvenido, John!"
	"""
	try:
		messages = load_locale(LANGUAGE.lower())
	except (FileNotFoundError, json.JSONDecodeError) as e:
		error(f"Error loading locale {LANGUAGE}: {e}")
		messages = {}

	if key in messages:
		translated_text = messages[key]
	else:
		# Fallback a inglés
		try:
			messages_en = load_locale("en")
			if key in messages_en:
				warning(f"Translation key '{key}' not found in {LANGUAGE}, using English fallback")
				translated_text = messages_en[key]
			else:
				error(f"Translation key '{key}' not found in {LANGUAGE} or EN")
				return f"[MISSING: {key}]"
		except (FileNotFoundError, json.JSONDecodeError):
			error(f"Could not load English fallback for key '{key}'")
			return f"[MISSING: {key}]"

	# Sustituir placeholders
	for i, arg in enumerate(args, start=1):
		placeholder = f"${i}"
		translated_text = translated_text.replace(placeholder, str(arg))

	return translated_text


def clear_translation_cache():
	"""
	Limpia el caché de traducciones.

	Útil para tests o para recargar traducciones sin reiniciar el bot.
	"""
	load_locale.cache_clear()