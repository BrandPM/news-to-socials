"""Source adapters: RSS / Telegram / web.

Each concrete source implements :class:`Source` and is registered in
``REGISTRY`` so the scheduler can dispatch by ``Source.type`` without knowing
the concrete class.
"""

from .base import REGISTRY, Source

__all__ = ["REGISTRY", "Source"]
