"""Publishers: write content to the right downstream system.

* ``sanity``       — primary publisher (Wave 1+; ADR-018). Writes drafts
                     to the existing Sanity CMS at www.iconfinance.io.
* ``telegram_bot`` — Telegram channels via Bot API (Wave 3 — postponed).
* ``meta_graph``   — Facebook pages + Instagram Business (Wave 2).
* ``dispatcher``   — channel router (kept; mostly used for Wave 2/3).
* ``directus``     — DEPRECATED writer for the previous CMS choice.
                     Kept in the repo for reference and possible reuse on
                     other brands. Not used in production. See ADR-018.
"""

from .sanity import SanityClient, SanityPublisher, SanityPostInput

__all__ = ["SanityClient", "SanityPublisher", "SanityPostInput"]
