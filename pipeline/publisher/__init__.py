"""Publishers: write content to the right downstream system.

* ``sanity``       — primary publisher (Wave 1+; ADR-018). Writes drafts
                     to the existing Sanity CMS at www.iconfinance.io.
* ``telegram_bot`` — Telegram Bot API client. Used by the monitoring
                     alerter (NTS_073); also the Wave 3 channel publisher.
* ``meta_graph``   — Facebook pages + Instagram Business (Wave 2).

The Directus writer + channel dispatcher were removed in NTS_076 (dead
since the ADR-018 Sanity pivot).
"""

from .sanity import SanityClient, SanityPublisher, SanityPostInput

__all__ = ["SanityClient", "SanityPublisher", "SanityPostInput"]
