from config import TELEGRAM_ADMIN
import re
import unicodedata
import os

def is_admin(id):
    return id == int(TELEGRAM_ADMIN)

def sanitize_filename(filename):
    base, ext = os.path.splitext(filename)
    base = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode('ascii')
    base = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', base)
    base = re.sub(r'_+', '_', base)
    base = base.strip('_') or 'archivo'
    base = base[:255 - len(ext)]
    return base + ext