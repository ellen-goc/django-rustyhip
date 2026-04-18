"""Django database backend that proxies SQL to a rustyhip Lambda over HTTP."""

from .base import DatabaseWrapper

__all__ = ["DatabaseWrapper"]
__version__ = "0.1.0"
