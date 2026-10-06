"""
Sistema de internacionalización (i18n) para DropBot.

Características:
- Carga de traducciones desde archivos JSON
- Caché automático de traducciones para optimizar rendimiento
- Fallback a inglés si falta una traducción
- Sustitución de placeholders ($1, $2, etc.)
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from logger import warning, error
import settings
from basic import md_code

# Constante para el parse_mode por defecto
PARSE_MODE = "markdown"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"


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
	locale_path = LOCALE_DIR / f"{locale}.json"

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
	# Se lee en cada llamada: el idioma se cambia desde /settings sin reiniciar
	language = settings.language()
	try:
		messages = load_locale(language.lower())
	except (FileNotFoundError, json.JSONDecodeError) as e:
		error(f"Error loading locale {language}: {e}")
		messages = {}

	if key in messages:
		translated_text = messages[key]
	else:
		# Fallback a inglés
		try:
			messages_en = load_locale("en")
			if key in messages_en:
				warning(f"Translation key '{key}' not found in {language}, using English fallback")
				translated_text = messages_en[key]
			else:
				error(f"Translation key '{key}' not found in {language} or EN")
				return f"[MISSING: {key}]"
		except (FileNotFoundError, json.JSONDecodeError):
			error(f"Could not load English fallback for key '{key}'")
			return f"[MISSING: {key}]"

	# Sustituir placeholders en una sola pasada y de mayor a menor: uno a uno,
	# "$1" se comía el principio de "$10", y un valor que contuviera "$2" (un
	# nombre de fichero, p. ej.) se volvía a sustituir con el argumento 2
	#
	# Un hueco escrito como `$1` en la traducción es un nombre que va como
	# código: se rellena con md_code, que lo deja igual que antes salvo cuando
	# el nombre trae una comilla invertida, que cerraba el bloque y lo mutilaba
	if args:
		numbers = "|".join(str(i) for i in range(len(args), 0, -1))
		pattern = re.compile(rf"`\$({numbers})`|\$({numbers})")

		def fill(match):
			if match.group(1):
				return md_code(args[int(match.group(1)) - 1])
			return str(args[int(match.group(2)) - 1])

		translated_text = pattern.sub(fill, translated_text)

	return translated_text


def clear_translation_cache():
	"""
	Limpia el caché de traducciones.

	Útil para tests o para recargar traducciones sin reiniciar el bot.
	"""
	load_locale.cache_clear()
