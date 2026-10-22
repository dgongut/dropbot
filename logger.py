"""
Sistema de logging profesional para DropBot.

Características:
- Múltiples niveles de log (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Salida a consola y archivo con rotación automática
- Formato estructurado con timestamps, niveles y contexto
- Rotación de logs para evitar archivos gigantes
- Configuración centralizada
"""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


class ColoredFormatter(logging.Formatter):
    """Formatter con colores para la consola (solo si el terminal lo soporta)"""
    
    # Códigos ANSI para colores
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Verde
        'WARNING': '\033[33m',    # Amarillo
        'ERROR': '\033[31m',      # Rojo
        'CRITICAL': '\033[35m',   # Magenta
    }
    RESET = '\033[0m'
    
    def __init__(self, *args, use_colors=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_colors = use_colors and sys.stdout.isatty()
    
    def format(self, record):
        # El registro es el mismo para todos los handlers: si se quedaba
        # coloreado, el fichero de log se llenaba de códigos ANSI
        levelname = record.levelname
        if self.use_colors and levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = levelname


# Lo que no se escribe tal cual en el log: los de control (salvo tabulador y
# salto de línea), DEL, los C1, las marcas bidi y los separadores de línea
UNSAFE_LOG_CHARS = re.compile(
    r'[\x00-\x08\x0B-\x1F\x7F-\x9F'
    r'\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069\u2028\u2029]'
)


class SafeMessageFilter(logging.Filter):
    """Deja el mensaje de forma que no pueda falsificar el log.

    Al log llegan textos que elige un tercero (el título de una playlist, el
    stderr de yt-dlp...): con un salto de línea fabricaban una línea que no se
    distinguía de un registro real, y con un ESC mandaban secuencias a la
    terminal de quien sigue `docker logs -f`. Esos caracteres se escriben en su
    forma visible (\\x1b) y las líneas de continuación van sangradas.
    """

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            # Un formato roto lo avisa el handler, como siempre
            return True
        message = UNSAFE_LOG_CHARS.sub(lambda m: ascii(m.group())[1:-1], message)
        record.msg = message.replace('\n', '\n    | ')
        record.args = None
        return True


def setup_logger(
    name: str = 'dropbot',
    log_file: Optional[str] = None,  # None por defecto para contenedores
    log_level: str = 'INFO',
    console_level: str = 'INFO',
    file_level: str = 'DEBUG',
    max_bytes: int = 10_485_760,  # 10MB
    backup_count: int = 5,
    use_colors: bool = True
) -> logging.Logger:
    """
    Configura y retorna un logger profesional.

    Args:
        name: Nombre del logger
        log_file: Ruta del archivo de log.
                 - None (default): Solo stdout (recomendado para Docker)
                 - String: Ruta a archivo con rotación
        log_level: Nivel general del logger
        console_level: Nivel para salida de consola (stdout)
        file_level: Nivel para archivo de log (solo si log_file != None)
        max_bytes: Tamaño máximo del archivo antes de rotar (default: 10MB)
        backup_count: Número de archivos de backup a mantener
        use_colors: Usar colores en consola (detecta automáticamente si es TTY)

    Returns:
        Logger configurado

    Note:
        En contenedores Docker, use log_file=None para que los logs
        vayan a stdout y sean capturables con `docker logs`.
    """
    logger = logging.getLogger(name)
    
    # Evitar duplicar handlers si ya existe
    if logger.handlers:
        return logger
    
    logger.setLevel(getattr(logging, log_level.upper()))
    logger.propagate = False
    # En el logger y no en cada handler: así se sanea una sola vez y vale
    # igual para la consola y para el fichero
    logger.addFilter(SafeMessageFilter())
    
    # Formato detallado para logs
    detailed_format = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)-8s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Handler para consola (con colores)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, console_level.upper()))
    
    if use_colors:
        console_formatter = ColoredFormatter(
            fmt='%(asctime)s - %(levelname)-8s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            use_colors=True
        )
    else:
        console_formatter = logging.Formatter(
            fmt='%(asctime)s - %(levelname)-8s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # Handler para archivo (con rotación)
    if log_file:
        try:
            # Crear directorio si no existe
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding='utf-8'
            )
            file_handler.setLevel(getattr(logging, file_level.upper()))
            file_handler.setFormatter(detailed_format)
            logger.addHandler(file_handler)
            
            logger.info(f"Log file enabled: {log_file} (max: {max_bytes/1024/1024:.1f}MB, backups: {backup_count})")
        except Exception as e:
            logger.warning(f"Could not setup file logging: {e}")
    
    return logger


# Instancia global del logger (se configura en main)
_logger: Optional[logging.Logger] = None


def get_logger() -> logging.Logger:
    """Obtiene el logger global"""
    global _logger
    if _logger is None:
        _logger = setup_logger()
    return _logger


# Funciones de conveniencia compatibles con debug.py (para migración gradual)
def debug(message: str):
    """Log a DEBUG level message"""
    get_logger().debug(message)


def info(message: str):
    """Log an INFO level message"""
    get_logger().info(message)


def warning(message: str):
    """Log a WARNING level message"""
    get_logger().warning(message)


def error(message: str):
    """Log an ERROR level message"""
    get_logger().error(message)


def critical(message: str):
    """Log a CRITICAL level message"""
    get_logger().critical(message)
