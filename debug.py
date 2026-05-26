"""
Módulo de logging (deprecated - usar logger.py).

Este módulo se mantiene para compatibilidad hacia atrás.
Internamente usa el nuevo sistema de logging profesional.
"""

from logger import debug, info, warning, error, critical

__all__ = ['debug', 'info', 'warning', 'error', 'critical']