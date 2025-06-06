from config import TELEGRAM_ADMIN, EXTENSIONS_COMPRESSED
import re
import unicodedata
import os
from pathlib import Path

def is_admin(id):
    admins = TELEGRAM_ADMIN.split(',')
    if str(id) in admins:
        return True

def sanitize_filename(filename):
    base, ext = os.path.splitext(filename)
    base = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode('ascii')
    base = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', base)
    base = re.sub(r'_+', '_', base)
    base = base.strip('_') or 'archivo'
    base = base[:255 - len(ext)]
    return base + ext

def clean_youtube_link(url):
    if 'youtu.be/' in url:
        video_id = url.split('youtu.be/')[1].split('?')[0]
        return f'https://www.youtube.com/watch?v={video_id}'

    elif 'youtube.com/watch' in url:
        parts = url.split('?')
        if len(parts) > 1:
            for part in parts[1].split('&'):
                if part.startswith('v='):
                    video_id = part.split('=')[1]
                    return f'https://www.youtube.com/watch?v={video_id}'

    elif 'youtube.com/shorts/' in url:
        video_id = url.split('youtube.com/shorts/')[1].split('?')[0]
        return f'https://www.youtube.com/watch?v={video_id}'

    return url

def is_compressed_file(file_path):
    lower = file_path.lower()
    
    if any(lower.endswith(ext) for ext in EXTENSIONS_COMPRESSED):
        return True
    
    if re.search(r'\.z\d{2,}$', lower):
        return True
    if re.search(r'\.r\d{2,}$', lower):
        return True
    if re.search(r'\.part\d+\.rar$', lower):
        return True

    return False

def get_filename_from_path(path):
    return Path(path).name

def is_split_zip(file_name):
    lower = file_name.lower()
    base = None
    if re.match(r'.*\.z\d{2,}$', lower):
        base = re.sub(r'\.z\d{2,}$', '', file_name)
    elif lower.endswith('.zip'):
        base = file_name[:-4]
    else:
        return False
    for i in range(1, 100):
        part = f"{base}.z{str(i).zfill(2)}"
        if os.path.exists(part):
            return True
    return False