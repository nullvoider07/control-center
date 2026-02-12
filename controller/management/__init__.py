"""Management utilities for CLI, config, and agent coordination"""

from .cli import main
from .config import ConfigManager

__all__ = ['main', 'ConfigManager']