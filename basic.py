from config import TELEGRAM_ADMIN
import re
import unicodedata
import os

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
        for part in url.split('?')[1].split('&'):
            if part.startswith('v='):
                video_id = part.split('=')[1]
                return f'https://www.youtube.com/watch?v={video_id}'

    return url
