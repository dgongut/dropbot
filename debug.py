from datetime import datetime

def debug(message):
    print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - DEBUG: {message}')

def error(message):
    print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - ERROR: {message}')

def warning(message):
    print(f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} - WARNING: {message}')