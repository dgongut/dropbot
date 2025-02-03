from config import TELEGRAM_ADMIN

def is_admin(id):
    return id == int(TELEGRAM_ADMIN)